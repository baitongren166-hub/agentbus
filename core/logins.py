"""一键登录：新手不用开终端也能把 agent 登录好。按 agent 类型登记，能多自动就多自动：

  oauth     agentbus 内置的浏览器授权（Gemini CLI：Google 登录，凭证存回 Gemini CLI 自己的位置）
  cli_bg    后台跑它自带的登录命令，它自己会打开浏览器（Codex、ZCode、Cursor Agent）
  app       打开它的桌面应用，在应用里登录
  terminal  兜底：开一个终端窗口跑它的登录命令，照提示操作（OpenCode 要选供应商、填 key，只能交互）

logged_in 按它自己存凭证的地方判断：True 已登录 / False 确定没登录 / None 判断不了。
只有确定的时候才说“没登录”（比如 OpenCode 也可以把 key 写在配置里、不走登录文件），拿不准就不下结论，只给登录按钮。
没登记的命令行 agent 也有兜底：开终端跑它的主命令。
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

H = os.path.expanduser


def _exists(*paths):
    return any(os.path.exists(H(p)) for p in paths)


def _nonempty_json(path):
    try:
        return bool(json.load(open(H(path))))
    except (OSError, ValueError):
        return False


def _keychain(service):
    try:
        return subprocess.run(["security", "find-generic-password", "-s", service], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _codex_auth():
    """auth.json 里真有凭证（ChatGPT 登录的 token，或 API key）才算登录过；空文件、只剩模式字段的不算。过没过期判断不了。"""
    try:
        d = json.load(open(H("~/.codex/auth.json")))
    except (OSError, ValueError):
        return None
    t = d.get("tokens") or {}
    return True if (isinstance(t, dict) and (t.get("refresh_token") or t.get("access_token"))) or d.get("OPENAI_API_KEY") else None


LOGINS = {
    "gemini": {"kind": "terminal", "cmd": ["agy"], "label": "登录反重力 CLI", "note": "在终端里的反重力 CLI 按提示完成 Google 登录。",
               "logged_in": lambda: None},  # 对话目录在不代表账号还登着（退出登录后目录也还在）：判断不了就不下结论
    "codex": {"kind": "cli_bg", "cmd": ["codex", "login"], "label": "登录 ChatGPT",
              "logged_in": lambda: _codex_auth()},
    "zcode": {"kind": "cli_bg", "cmd": ["zcode", "login"], "label": "登录 Z.AI", "logged_in": lambda: None},
    "cursor": {"kind": "cli_bg", "cmd": ["cursor-agent", "login"], "label": "登录 Cursor", "logged_in": lambda: None},
    "opencode": {"kind": "terminal", "cmd": ["opencode", "auth", "login"], "label": "登录 / 添加供应商",
                 "note": "OpenCode 登录要选供应商、填 key，会开一个终端窗口，照里面的提示一步步选就行。",
                 "logged_in": lambda: _nonempty_json("~/.local/share/opencode/auth.json") or None},
    "codebuddy": {"kind": "terminal", "cmd": ["codebuddy"], "label": "登录 CodeBuddy CLI",
                  "note": "在打开的 CodeBuddy 终端输入 /login，按提示完成授权。", "logged_in": lambda: None},
    "qwen": {"kind": "terminal", "cmd": ["qwen"], "label": "登录 Qwen Code", "note": "会开一个终端窗口，按里面的提示选登录方式。",
             "logged_in": lambda: _nonempty_json("~/.qwen/oauth_creds.json") or None},
    "kimi": {"kind": "terminal", "cmd": ["kimi"], "label": "登录 Kimi CLI", "note": "会开一个终端窗口，按里面的提示登录。", "logged_in": lambda: None},
}

LOG = Path(H(os.environ.get("AGENTBUS_HOME") or "~/.agentbus")) / "logins"
_running = {}  # agent → Popen（后台登录命令）
_logs = {}  # agent → 这次登录的日志文件
import threading as _th
_start_lock = _th.Lock()


def method(agent):
    """这位队员的登录方式。没登记的命令行 agent：兜底开终端跑它的主命令。"""
    from config import sealed

    if sealed(agent):
        return None
    base = agent.get("clone_of") or agent.get("id")
    if base == "doubao":
        from config import doubao_cli_cmd
        cmd = doubao_cli_cmd()
        if cmd:
            return {"id": base, "kind": "terminal", "cmd": [*cmd, "login", "--timeout", "300"],
                    "label": "登录豆包 Headless", "note": "在终端里用手机豆包扫码授权。", "logged_in": lambda: None}
    m = LOGINS.get(base)
    if m:
        return dict(m, id=base)
    run = agent.get("run")
    if run in (None, "api", "doubao") or agent.get("billing") == "manual":
        return None  # API 队员用 key、不用登录；网页版在网页里登
    exe = (agent.get("cmd") or [run])[0] if isinstance(agent.get("cmd"), list) else run
    return {"id": base, "kind": "terminal", "cmd": [exe], "label": f"登录 {agent.get('name') or base}",
            "note": "会开一个终端窗口运行它，按里面的提示登录。", "logged_in": lambda: None}


def status(agent):
    m = method(agent)
    if not m:
        return None
    try:
        li = m["logged_in"]()
    except Exception:
        li = None
    p = _running.get(m["id"])
    return {"kind": m["kind"], "label": m["label"], "note": m.get("note"), "logged_in": li, "busy": bool(p and p.poll() is None)}


def dispatch_action(agent, status_code, reply=""):
    """派活失败后的操作选项，仅供用户主动选择。"""
    from config import sealed
    if not agent:
        return None
    if (agent.get("run") == "codebuddy" and status_code == "failed" and reply.startswith("（WorkBuddy 没有回复，退出码")
            and "Please use /login command" in reply):  # 升级前的失败记录仍能打开 CLI 登录
        status_code = "cli_login"
    if status_code not in ("cli_login", "cli_unavailable") or sealed(agent):
        return None
    if status_code == "cli_login":
        m = method(agent)
        if m and m["kind"] in ("cli_bg", "terminal", "oauth"):
            return {"kind": "login", "label": m["label"]}
    if status_code == "cli_unavailable":
        from catalog import OPENERS
        import pf
        opener = OPENERS.get(agent.get("clone_of") or agent.get("id"))
        if opener and pf.app_exists(opener):
            return {"kind": "desktop", "label": "询问使用桌面端"}
    return None


def start(agent, port=None):
    """开始登录。返回 {ok, kind, url?, note}：oauth 返回要打开的地址（服务端已打开），其余按方式启动。"""
    import pf
    from config import CLAUDE_SEALED, sealed

    if sealed(agent):
        raise ValueError(CLAUDE_SEALED)
    m = method(agent)
    if not m:
        raise ValueError("这位队员不需要登录（API 队员用 key，网页版在网页里登录）")
    if m["kind"] == "oauth":
        import balance
        url = balance.login_start(m["id"], port)
        pf.open_url(url)
        return {"ok": True, "kind": "oauth", "url": url, "note": "浏览器里打开了 Google 登录页，登录并点“允许”后回到 agentbus 就好。"}
    if m["kind"] == "app":
        pf.open_app({"app": m["app"]})
        return {"ok": True, "kind": "app", "note": f"已打开 {m['app']}，在应用里登录就行。"}
    exe = pf.which(m["cmd"][0]) or shutil.which(m["cmd"][0])
    if not exe:
        raise ValueError(f"本机找不到 {m['cmd'][0]} 命令：先在休息室招募安装它")
    cmd = [exe, *m["cmd"][1:]]
    if m["kind"] == "cli_bg":  # 它自己会打开浏览器；输出记到日志，登录完自己退出
        with _start_lock:  # 同一位同时点两次 / 两个窗口一起点：只起一个登录进程
            p = _running.get(m["id"])
            if p and p.poll() is None:
                return {"ok": True, "kind": "cli_bg", "busy": True, "note": "已经在登录了：去浏览器里把登录做完就好。"}
            LOG.mkdir(parents=True, exist_ok=True)
            f = LOG / f"{m['id']}-{time.strftime('%m%d-%H%M%S')}.log"  # 每次一个新日志，只有自己能读（里面可能有登录链接）
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as log:
                _running[m["id"]] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **pf.group_kwargs())
            _logs[m["id"]] = f
        return {"ok": True, "kind": "cli_bg", "note": "浏览器会自动打开登录页，登录完回到 agentbus 就好（这里会自动刷新）。"}
    pf.open_terminal(Path.home(), cmd)
    return {"ok": True, "kind": "terminal", "note": m.get("note") or "开了一个终端窗口，照里面的提示登录。"}


def tail(agent_id, n=1500):
    f = _logs.get(agent_id) or LOG / f"{agent_id}.log"
    return f.read_text("utf-8", "ignore")[-n:] if f.exists() else ""
