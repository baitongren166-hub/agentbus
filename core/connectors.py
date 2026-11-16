"""接入方式清单：每种 agent 怎么让 agentbus 知道它开了新对话、主人点「接入」后怎么把握手送进那个窗口。

按 agent 类型写，不按某一台电脑写：用户以后新装了哪家，这里的规则直接适用。三层兜底：
  1. 钩子（最可靠）：agent 每次新对话 / 每次发言时自己跑 `agentbus hook …`，握手随下一次发言进上下文。能一键装的一键装，
     改配置前先备份，重复装不会重复，卸载只删 agentbus 加的那几行。
  2. 手动配置：一键装不了（或主人不想让 agentbus 改别家的配置）时，给出要粘贴的配置和放哪里。
  3. 接入口令：什么都配不了的 agent，复制一句话发进那个对话框，agent 自己跑 `agentbus hello` 拿握手。

新对话检测（被动扫各家会话记录）在 activity.STARTS 里，和这里的钩子互为兜底。
"""
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path

H = os.path.expanduser
# 认得出哪些是 agentbus 装的钩子：源码版 `…/agentbus.py hook session`、打包版 `agentbus-core cli hook …`、命令 `agentbus hook …`
OURS = re.compile(r"agentbus(?:\.py|-core(?:\.exe)?['\"]?\s+cli)?['\"]?\s+hook\s+(?:session|prompt)")


def ours(text):
    return bool(OURS.search(text or ""))


def cli():
    """调 agentbus 命令行的完整命令（源码版是 python + agentbus.py，打包版是 agentbus-core cli）；钩子里一律用完整路径。"""
    import pf
    return " ".join(shlex.quote(x) for x in pf.cli_cmd())


def hook_cmd(event, agent):
    return f"{cli()} hook {event} --agent {agent}"


def join_phrase(agent):
    """兜底的接入口令：主人复制发进那个对话框，agent 自己跑命令拿握手。"""
    cmd = f"{cli()} hello --agent {agent}"  # 中英两句：粘给哪种语言的 agent 都看得懂
    return (f"接入 agentbus：在终端运行 `{cmd}`，然后照它输出的握手信息干活（有 agentbus_hello 工具的话直接调它也行）。"
            f" / Join agentbus: run `{cmd}` in the terminal and follow the handshake it prints (or call the agentbus_hello tool if you have it).")


# ---------- 各家钩子的配置格式 ----------
def _strip_groups(groups, is_ours, key="hooks"):
    """只拿掉 agentbus 自己的处理器：同一组里别家的处理器、matcher 等原样保留；组被拿空了才删组。"""
    out = []
    for g in groups or []:
        if not isinstance(g, dict):
            out.append(g)
            continue
        hs = g.get(key) or []
        keep = [h for h in hs if not is_ours(h)]
        if len(keep) == len(hs):
            out.append(g)
        elif keep:
            out.append({**g, key: keep})
    return out


_STATE = Path(H(os.environ.get("AGENTBUS_HOME") or "~/.agentbus")) / "connectors_state.json"


def _state(update=None):
    """装钩子时改过的别家开关（原来是什么），卸载时照原样还回去。"""
    try:
        st = json.load(open(_STATE))
    except (OSError, ValueError):
        st = {}
    if update:
        update(st)
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        json.dump(st, open(_STATE, "w"), ensure_ascii=False, indent=1)
    return st


def _settings_hooks(path, agent, matcher_session="startup|clear"):
    """Claude Code / CodeBuddy / WorkBuddy 同一种 settings.json：hooks.<事件> = [{matcher?, hooks: [{type, command, timeout}]}]"""
    def install(d, dry=False):
        hk = d.setdefault("hooks", {})
        for ev, event, extra in (("SessionStart", "session", {"matcher": matcher_session} if matcher_session else {}), ("UserPromptSubmit", "prompt", {})):
            groups = _strip_groups(hk.get(ev), lambda h: ours(h.get("command")))
            groups.append({**extra, "hooks": [{"type": "command", "command": hook_cmd(event, agent), "timeout": 20 if event == "prompt" else 10}]})
            hk[ev] = groups
        return d

    def remove(d):
        hk = d.get("hooks") or {}
        for ev in ("SessionStart", "UserPromptSubmit"):
            if ev in hk:
                hk[ev] = _strip_groups(hk[ev], lambda h: ours(h.get("command")))
                if not hk[ev]:
                    hk.pop(ev)
        if "hooks" in d and not d["hooks"]:  # 只剩空壳就删掉，卸载后和装之前一样
            d.pop("hooks")
        return d
    return {"path": path, "kind": "json", "install": install, "remove": remove}


def _zcode(path):
    """ZCode：hooks.enabled + hooks.events.<事件> = [{hooks: [{type: process, command, args, timeoutMs}]}]，输出必须是 JSON。"""
    def h(event):
        import pf
        c = pf.cli_cmd()
        return {"type": "process", "command": c[0], "args": c[1:] + ["hook", event, "--agent", "zcode"], "timeoutMs": 20000 if event == "prompt" else 10000}

    def mine(x):
        return ours(" ".join([x.get("command") or ""] + [str(a) for a in x.get("args") or []]))

    def install(d, dry=False):
        had = isinstance(d.get("hooks"), dict) and "enabled" in d["hooks"]
        prev = d["hooks"].get("enabled") if had else None
        hk = d.setdefault("hooks", {})
        if not dry and not (_state().get(path) or {}).get("zcode"):  # 第一次装：记下 ZCode 原来的开关（重装不覆盖这份原值）
            _state(lambda st: st.__setitem__(path, {"zcode": {"had": had, "enabled": prev}}))
        hk["enabled"] = True
        evs = hk.setdefault("events", {})
        for ev, event in (("SessionStart", "session"), ("UserPromptSubmit", "prompt")):
            evs[ev] = _strip_groups(evs.get(ev), mine) + [{"hooks": [h(event)]}]
        return d

    def remove(d):
        hk = d.get("hooks")
        if not isinstance(hk, dict):
            return d
        evs = hk.get("events") or {}
        for ev in ("SessionStart", "UserPromptSubmit"):
            if ev in evs:
                evs[ev] = _strip_groups(evs[ev], mine)
                if not evs[ev]:
                    evs.pop(ev)
        before = (_state().get(path) or {}).get("zcode")
        if before:  # 开关还回装之前的样子
            if before.get("had"):
                hk["enabled"] = before.get("enabled")
            else:
                hk.pop("enabled", None)
            _state(lambda st: st.pop(path, None))
        if not evs:
            hk.pop("events", None)
        if not hk:
            d.pop("hooks")
        return d
    return {"path": path, "kind": "json", "install": install, "remove": remove}


def _antigravity(path):
    """反重力：hooks.json 顶层是命名的 hook 组；SessionStart / PreInvocation 直接是处理器数组。输入 camelCase，输出 injectSteps。"""
    def is_ours(grp):
        return isinstance(grp, dict) and all(ours(h.get("command")) for hs in grp.values() if isinstance(hs, list) for h in hs if isinstance(h, dict))

    def install(d, dry=False):
        if "agentbus-join" in d and not is_ours(d["agentbus-join"]):
            raise ValueError("反重力的 hooks.json 里已经有一个叫 agentbus-join 的组，但不是 agentbus 装的：没改它，请手动处理")
        d["agentbus-join"] = {"SessionStart": [{"type": "command", "command": hook_cmd("session", "gemini"), "timeout": 10}],
                              "PreInvocation": [{"type": "command", "command": hook_cmd("prompt", "gemini"), "timeout": 20}]}
        return d

    def remove(d):
        if is_ours(d.get("agentbus-join")):
            d.pop("agentbus-join", None)
        return d
    return {"path": path, "kind": "json", "install": install, "remove": remove}


def _codex(path):
    """Codex：config.toml 里的 [[hooks.SessionStart]] / [[hooks.UserPromptSubmit]]（桌面和命令行共用）。
    不解析整份 TOML，只在文件末尾追加一段带标记的块，卸载时按标记删掉。装好后要在 Codex 里信任一次（设置 → Hooks）。"""
    begin, end = "# >>> agentbus hooks（agentbus 自动添加，卸载会删掉这一段）", "# <<< agentbus hooks"

    def block():
        q = lambda s: json.dumps(s, ensure_ascii=False)
        return "\n".join([begin,
                          "[[hooks.SessionStart]]", 'matcher = "^(startup|clear)$"', "",
                          "[[hooks.SessionStart.hooks]]", 'type = "command"', f"command = {q(hook_cmd('session', 'codex'))}", "timeout = 10", "",
                          "[[hooks.UserPromptSubmit]]", "",
                          "[[hooks.UserPromptSubmit.hooks]]", 'type = "command"', f"command = {q(hook_cmd('prompt', 'codex'))}", "timeout = 20",
                          end, ""])

    def strip(text):
        return re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)

    def install(text):
        return strip(text).rstrip("\n") + "\n\n" + block()

    def remove(text):
        return strip(text)
    return {"path": path, "kind": "text", "install": install, "remove": remove, "block": block,
            "note": "装好后打开 Codex → 设置 → Hooks，信任 agentbus 的两条钩子（Codex 要求新钩子先经过你确认才生效）。"}


# ---------- 清单 ----------
# exists：这台电脑上装没装这家（目录或命令在就算）；hook：怎么装钩子（None = 没有可靠的一键方式，走手动 / 口令兜底）
CONNECTORS = {
    "claude": {"name": "Claude Code", "exists": ["~/.claude"], "hook": lambda: _settings_hooks("~/.claude/settings.json", "claude", None)},
    "codex": {"name": "Codex（桌面 / 命令行）", "exists": ["~/.codex"], "hook": lambda: _codex("~/.codex/config.toml")},
    "zcode": {"name": "ZCode", "exists": ["~/.zcode"], "hook": lambda: _zcode("~/.zcode/cli/config.json")},
    "codebuddy": {"name": "WorkBuddy", "exists": ["~/.workbuddy"], "hook": lambda: _settings_hooks("~/.workbuddy/settings.json", "codebuddy")},
    "codebuddy-cli": {"name": "CodeBuddy 命令行", "agent": "codebuddy", "exists": ["~/.codebuddy"], "hook": lambda: _settings_hooks("~/.codebuddy/settings.json", "codebuddy")},
    "antigravity": {"name": "反重力 Antigravity", "agent": "gemini", "exists": ["~/.gemini/antigravity", "~/.gemini/antigravity-cli"], "hook": lambda: _antigravity("~/.gemini/config/hooks.json")},
    "opencode": {"name": "OpenCode", "exists": ["~/.config/opencode", "~/.local/share/opencode"], "hook": None,
                 "why": "OpenCode 的钩子要写插件，新版插件接口还没验证过，先不自动装（装坏了会影响它启动）。"},
    "qwen": {"name": "Qwen Code", "exists": ["~/.qwen"], "hook": None, "why": "还没核实它的钩子格式。"},
    "kimi": {"name": "Kimi CLI", "exists": ["~/.kimi"], "hook": None, "why": "还没核实它的钩子格式。"},
    "cursor": {"name": "Cursor Agent", "exists": ["~/.cursor"], "hook": None, "why": "还没核实它的钩子格式。"},
}


def _read(spec):
    p = Path(H(spec["path"]))
    if not p.exists():
        return p, ({} if spec["kind"] == "json" else "")
    raw = p.read_text("utf-8")
    if spec["kind"] == "text":
        return p, raw
    try:
        return p, json.loads(raw or "{}")
    except json.JSONDecodeError:
        # 带注释的 JSONC 之类读不了：不硬改，交给手动配置
        raise ValueError(f"{p} 不是标准 JSON（可能带注释），没法自动改，请按手动配置加")


def hook_installed(cid):
    c = CONNECTORS.get(cid) or {}
    if not c.get("hook"):
        return False
    p = Path(H(c["hook"]()["path"]))
    try:
        t = p.read_text("utf-8")
    except OSError:
        return False
    # 要有“每次发言”的那条钩子才送得到握手（只剩新对话那条不算）；ZCode 还要总开关开着
    if spec_kind(cid) == "text":
        return bool(re.search(r"hook\s+prompt", t)) and ours(t)
    try:
        d = json.loads(t)
    except ValueError:
        return False
    if cid == "zcode":
        hk = d.get("hooks") or {}
        return hk.get("enabled") is not False and any(
            "hook prompt" in " ".join([x.get("command") or ""] + [str(a) for a in x.get("args") or []]) and ours(" ".join([x.get("command") or ""] + [str(a) for a in x.get("args") or []]))
            for g in (hk.get("events") or {}).get("UserPromptSubmit") or [] for x in g.get("hooks") or [])
    blob = json.dumps(d, ensure_ascii=False)
    return any(ours(m.group(0)) for m in re.finditer(r"[^\"]*hook\s+prompt[^\"]*", blob))


def spec_kind(cid):
    return CONNECTORS[cid]["hook"]()["kind"]


def _backup(p):
    if p.exists():
        b, n = p.with_name(p.name + time.strftime(".bak-agentbus-%m%d-%H%M%S")), 1
        while b.exists():  # 同一秒里装了又卸：不覆盖前一份备份
            n += 1
            b = p.with_name(p.name + time.strftime(".bak-agentbus-%m%d-%H%M%S") + f"-{n}")
        shutil.copy2(p, b)
        return str(b)
    return None


def install(cid):
    """一键装钩子：先备份，再合并写回。返回 {ok, path, backup, note}。"""
    c = CONNECTORS.get(cid)
    if not c or not c.get("hook"):
        raise ValueError((c or {}).get("why") or "这个 agent 没有一键装钩子的方式，请用手动配置或接入口令")
    spec = c["hook"]()
    p, cur = _read(spec)
    new = spec["install"](cur)
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = _backup(p)
    p.write_text(new if spec["kind"] == "text" else json.dumps(new, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"ok": True, "path": str(p), "backup": bak, "note": spec.get("note") or "装好了：在那个 agent 里新开对话就会自动报到，接入后下一次发言就能收到握手。"}


def remove(cid):
    c = CONNECTORS.get(cid)
    if not c or not c.get("hook"):
        return {"ok": True}
    spec = c["hook"]()
    p, cur = _read(spec)
    if not p.exists():
        return {"ok": True}
    bak = _backup(p)
    new = spec["remove"](cur)
    p.write_text(new if spec["kind"] == "text" else json.dumps(new, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"ok": True, "path": str(p), "backup": bak}


def manual(cid):
    """手动配置说明：放哪个文件、贴什么。"""
    c = CONNECTORS.get(cid) or {}
    if not c.get("hook"):
        return None
    spec = c["hook"]()
    if spec["kind"] == "text":
        snippet = spec["block"]()
        how = f"把下面这段加到 {spec['path']} 末尾。"
    else:
        snippet = json.dumps(spec["install"]({}, dry=True), ensure_ascii=False, indent=2)
        how = f"把下面的内容合并进 {spec['path']}（已有 hooks 的话，把这几项加进去，不要覆盖原来的）。"
    return {"path": spec["path"], "how": how + (" " + spec["note"] if spec.get("note") else ""), "snippet": snippet}


def of_agent(agent_id):
    """队员 id → 接入方式 id（WorkBuddy 的队员 id 是 codebuddy，反重力没有队员 id）。"""
    return agent_id if agent_id in CONNECTORS else next((k for k, v in CONNECTORS.items() if v.get("agent") == agent_id), agent_id)


def status():
    """设置页用：每家的接入情况。"""
    out = []
    for cid, c in CONNECTORS.items():
        present = any(os.path.exists(H(x)) for x in c["exists"])
        out.append({"id": cid, "name": c["name"], "present": present, "can_hook": bool(c.get("hook")), "hooked": hook_installed(cid),
                    "why": c.get("why"), "manual": manual(cid), "phrase": join_phrase(c.get("agent") or cid)})
    return out


def delivery(agent_id):
    """主人点了「接入」之后，这个窗口怎么拿到握手：hook = 装了钩子，下一次发言自动送到；否则给兜底选项。"""
    cid = of_agent(agent_id)
    c = CONNECTORS.get(cid) or {}
    if hook_installed(cid):  # 钩子配好了；Codex 新钩子还要主人在它的设置里信任一次，确认不了，所以口令也一起给上
        return {"via": "hook", "connector": cid, "phrase": join_phrase(c.get("agent") or agent_id),
                "unsure": c.get("hook")().get("note") if cid == "codex" else None}
    return {"via": "none", "connector": cid, "name": c.get("name") or agent_id, "can_hook": bool(c.get("hook")),
            "manual": manual(cid), "phrase": join_phrase(c.get("agent") or agent_id), "why": c.get("why")}
