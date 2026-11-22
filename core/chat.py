"""agentbus 消息：像企业群一样的聊天室。设计借鉴 agentchattr（MIT，github.com/bcurts/agentchattr）：频道、@ 叫人、
循环保护（agent 之间互相 @ 最多接力几次）；叫醒 agent 不用往终端里打字，而是走 agentbus 自己的派活（agentbus ask）。

  在群里 @某个队员 → 后台派活给它（带上群里最近几条当上下文）→ 它的回复自动发回群里；回复里再 @别人就接力派下去。
  不需要主位：主人直接在消息窗口里就能给任何队员派活。主界面和悬浮窗用的是同一个接口。
消息存在 ~/.agentbus/chat/<频道>.jsonl。
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

from config import HOME, agents_dict, load_config, load_json, save_json, sealed

CHAT = HOME / "chat"
FILES = CHAT / "files"  # 附件（粘贴的图片、选的文件）放这里，消息里给队员的是这里的完整路径
MAX_HOPS = 4          # agent 之间互相 @ 最多接力几次（主人 @ 的不算）
CONTEXT_CHARS = 8000  # 派活 / 群聊时带上的上下文最多多少字（压缩要点 + 最近的消息）
COMPRESS_AT = 16000   # 没压缩过的部分超过这么多字，后台把旧消息压成要点
KEEP_RECENT = 10      # 压缩时最近这几条原样保留
MAX_TEXT = 8000       # 一条消息最多多少字
MAX_RUNNING = 3       # 一个频道同时最多几单在跑
TALK_ROUNDS = 3       # 群聊（头脑风暴）最多几轮
TALK_MAX = 12         # 一次群聊最多几条发言
TALK_CHARS = 800      # 群聊里一条发言最多多少字
# 上面是默认值；主人在设置 → 聊天室里改过的以设置为准（开源给别人用，不能写死成某台电脑的配置）
DEFAULTS = {"duty": "auto", "hops": MAX_HOPS, "context": CONTEXT_CHARS, "compress_at": COMPRESS_AT, "max_running": MAX_RUNNING,
            "talk_rounds": 2, "talk_max": TALK_MAX, "talk_chars": TALK_CHARS, "max_text": MAX_TEXT}


def lim():
    return {**DEFAULTS, **((load_config().get("settings") or {}).get("chat") or {})}


def meta(channel):
    m = load_json(CHAT / "channels.json", {}).get(channel) or {}
    return {"mode": "work", "duty": lim()["duty"], "members": [], "rounds": lim()["talk_rounds"], "summary": "", "summary_upto": None, "compressions": 0, **m}


def set_meta(channel, **kw):
    from config import update_json
    CHAT.mkdir(parents=True, exist_ok=True)
    update_json(CHAT / "channels.json", lambda allm: allm.__setitem__(channel, {**(allm.get(channel) or {}), **kw}))


def resolve_duty(duty):
    """没 @ 人时谁来回。"auto"（默认）= 优先第一个 API 队员（便宜、快），没有就用主位办公室战备里的第一位；
    "none" = 没人回；其他 = 指定的那位（不在了就退回自动）。"""
    cfg = load_config()
    agents = agents_dict(cfg)
    if duty == "none":
        return None
    if duty and duty != "auto" and duty in agents:
        return duty
    for aid, a in agents.items():
        if a.get("api") and a.get("run"):
            return aid
    from config import active_lead
    own = (cfg.get("office_lineups") or {}).get(active_lead(cfg) or "")
    for aid in (own or {}).get("office") or cfg["roster"]["office"]:
        if agents.get(aid, {}).get("run") and not sealed(agents.get(aid, {})):
            return aid
    return None


def default_duty():
    return resolve_duty("auto")


def channels():
    CHAT.mkdir(parents=True, exist_ok=True)
    meta = load_json(CHAT / "channels.json", {})
    names = [p.stem for p in sorted(CHAT.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime)]
    if "大厅" not in names:
        names.insert(0, "大厅")
    return [{"name": n, **(meta.get(n) or {})} for n in names]


def valid(channel):
    """频道名只能是一个普通名字：不能带路径分隔符、不能以点开头、不能太长——拼成文件路径时出不了聊天目录。"""
    return (isinstance(channel, str) and 0 < len(channel) <= 30 and not channel.startswith(".")
            and not any(c in channel for c in '/\\\0:') and channel.isprintable())


def _file(channel):
    if not valid(channel):
        raise ValueError(f"频道名不对：{channel!r}")
    f = CHAT / f"{channel}.jsonl"
    if f.resolve().parent != CHAT.resolve():
        raise ValueError(f"频道名不对：{channel!r}")
    return f


def read(channel, n=200):
    f = _file(channel)
    if not f.exists():
        return []
    rows = []
    for l in f.read_text("utf-8", "ignore").splitlines()[-n:]:
        try:
            rows.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return rows


def append(channel, frm, text, **extra):
    CHAT.mkdir(parents=True, exist_ok=True)
    msg = {"id": uuid.uuid4().hex[:10], "t": time.time(), "frm": frm, "text": text, **extra}
    f = _file(channel)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return msg


def failure_issue(agent, status, detail=""):
    """把执行器错误转成可读消息。诊断原文留在工单日志。"""
    from logins import dispatch_action

    aid = agent.get("id") or ""
    name = agent.get("name") or aid or "队员"
    detail = str(detail or "")
    if sealed(agent):
        status = "sealed"
    elif status in (None, "error", "failed"):
        matches = (
            ("cli_login", r"cli_login|login.required|authentication (?:required|failed)|sign in|not logged in|invalid_grant|请先登录|需要登录|登录豆包 Headless"),
            ("cli_unavailable", r"command not found|找不到.*cli|cli 不可用|exec format error"),
            ("quota", r"quota|insufficient[_ -](?:balance|credits)|rate.limit|\b429\b|额度用完|限流|余额不足"),
            ("model", r"provider.no.route|model.not.found|unknown model|unsupported model|模型不存在|模型不可用"),
            ("permission", r"permission denied|access denied|\bEACCES\b|拒绝访问|权限不足"),
            ("network", r"\b(?:ECONNREFUSED|ECONNRESET|ENOTFOUND|ETIMEDOUT)\b|network error|connection (?:refused|closed)|网络异常"),
            ("timeout", r"超时|timed? out|timeout"),
            ("waiting_input", r"等待用户|waiting_input"),
        )
        status = next((code for code, pattern in matches if re.search(pattern, detail, re.I)), "failed")
        if "任务 " in detail and "会话 " in detail and status in ("timeout", "failed", "cli_login"):
            if status != "cli_login":
                status = "remote_pending"
    notice = {
        "sealed": "已永久封印。请选择其他队员。",
        "cli_login": "的命令行需要登录。登录后，请先确认原任务状态，再决定是否重试。",
        "cli_unavailable": "的命令行目前无法启动。请在队员页检查安装。",
        "channel_busy": "这单没派出去。频道里的任务达到并发上限，请等一单结束后再发。",
        "unknown_agent": "没找到指定的队员。请从输入框旁的队员列表选一个。",
        "manual": "只能接收留言。要自动执行，请换一位支持派活的队员。",
        "not_started": "没有开工。请在任务记录查看原因，或换一位队员。",
        "stalled": "长时间没有进展，看门狗已停止这单。请先查看已产出的内容。",
        "submit_failed": "的任务请求没能提交。请刷新聊天室，确认任务是否出现。",
        "stopped": "的任务已停止。可在任务记录查看做到了哪一步。",
        "lost": "的执行进程意外退出。请在任务记录查看已留下的进度。",
        "quota": "遇到用量限制或余额不足。请查看额度，或换一位队员。",
        "model": "无法使用当前模型。请去队员页检查模型设置。",
        "permission": "没有权限访问所需文件或服务。请检查任务用到的目录或授权。",
        "network": "连接服务失败。请检查网络，再确认原任务是否已受理。",
        "timeout": "等待服务响应超时。请先确认原任务状态，再决定是否重试。",
        "remote_pending": "的任务已受理，但 Bus 还没拿到结果。请用任务记录里的会话和任务编号查询原任务。",
        "waiting_input": "正在等待你在服务里确认。请在原任务中完成确认。",
        "failed": "没能完成任务。请打开任务记录查看详情。",
    }.get(status, "没能完成任务。请打开任务记录查看详情。")
    if status in ("channel_busy", "unknown_agent"):
        text = notice
    else:
        text = name + notice
    action = dispatch_action(agent, status)
    if action:
        action = dict(action, agent=aid)
    elif status in ("cli_unavailable", "model") and aid:
        from urllib.parse import quote
        action = {"kind": "page", "page": "hero/" + quote(aid, safe=""), "label": "打开队员设置"}
    elif status == "unknown_agent" or status == "sealed":
        action = {"kind": "page", "page": "team", "label": "查看队员"}
    elif status == "quota":
        action = {"kind": "page", "page": "report", "label": "查看用量和余额"}
    else:
        action = {"kind": "page", "page": "quests", "label": "查看任务记录"}
    return {"text": text, "action": action, "status": status, "agent": aid}


def failure(channel, agent, status, detail="", *, issues=None):
    issue = failure_issue(agent, status, detail)
    if issues is not None:
        issues.append(issue)
    return append(channel, "agentbus", issue["text"], system=True, issue=issue)


def display_messages(messages):
    """旧版失败消息保留原始记录，读取时用同一套提示。"""
    agents = None
    result = []
    for message in messages:
        text = message.get("text") or ""
        if not message.get("issue") and (text.startswith("❌ （") or (message.get("talk") and
                re.match(r"（[^\n]{0,60}(?:没有回复|出错|提前退出|需要登录|没开工|没接上|不可用)", text))):
            if agents is None:
                agents = agents_dict(load_config())
            aid = message.get("frm") or ""
            agent = agents.get(aid) or {"id": aid, "name": aid}
            issue = failure_issue(agent, "failed", text)
            message = dict(message, text=issue["text"], system=True, issue=issue)
        result.append(message)
    return result


def mentions(text, cfg=None):
    """消息里 @ 了谁：认 agent id 和名字（@GPT、@codex、@zcode、@小深…），@所有人 = 战备里的所有队员。"""
    cfg = cfg or load_config()
    agents = agents_dict(cfg)
    keys = []
    for aid, a in agents.items():
        for k in {aid, a.get("name") or aid}:
            keys.append((k.lower(), aid))
    keys.sort(key=lambda kv: -len(kv[0]))  # 长的先认：“GPT 2号”优先于“GPT”
    out, low = [], text.lower()
    for m in re.finditer(r"@", low):
        rest = low[m.end():]
        if rest.startswith(("所有人", "all", "everyone")):
            from config import active_lead
            own = (cfg.get("office_lineups") or {}).get(active_lead(cfg) or "")
            for aid in ((own or {}).get("office") or cfg["roster"]["office"])[:6]:
                if aid not in out and not sealed(agents.get(aid) or aid):
                    out.append(aid)
            continue
        for k, aid in keys:
            if rest.startswith(k) and aid not in out:
                out.append(aid)
                break
    return out


def _line(m, names):
    att = "".join(f"\n  （附件：{x['path']}）" for x in m.get("files") or [])
    return f"[{time.strftime('%m-%d %H:%M', time.localtime(m['t']))}] {names.get(m['frm'], m['frm'])}：{m['text']}{att}"


def unsummarized(channel):
    mt, rows = meta(channel), read(channel, 2000)
    if mt.get("summary_upto"):
        ids = [m["id"] for m in rows]
        if mt["summary_upto"] in ids:
            rows = rows[ids.index(mt["summary_upto"]) + 1:]
        elif mt.get("summary_upto_t"):  # 压缩点已经不在最近 2000 条里：按时间切，不把已经总结过的再算一遍
            rows = [m for m in rows if m.get("t", 0) > mt["summary_upto_t"]]
    return [m for m in rows if not m.get("system")]


def context(channel, budget=None):
    """给队员的上下文：前面的压缩要点 + 最近的消息（从新往旧放，放满 budget 字为止）。"""
    names = {k: v.get("name", k) for k, v in agents_dict(load_config()).items()}
    names["user"] = "主人"
    budget = budget or lim()["context"]
    mt = meta(channel)
    total, more = budget, "（更早的消息太长，省略了）"
    head = ""
    if mt.get("summary"):  # 要点最多占总预算的八成（太长就截掉尾巴），剩下的留给最近的消息；合起来不超过总预算
        pre, cut = "【前面聊过的要点（已压缩）】\n", "…（要点太长，后面省略了）"
        room = int(total * 0.8) - len(pre) - 2
        sm = mt["summary"] if len(mt["summary"]) <= room else mt["summary"][:max(0, room - len(cut))] + cut
        head = f"{pre}{sm}\n\n"
    budget = total - len(head)
    lines, used = [], 0
    for m in reversed(unsummarized(channel)):
        l = _line(m, names)
        sep = 1 if lines else 0
        if used + sep + len(l) > budget:
            if lines and used + sep + len(more) <= budget:
                lines.append(more)
            elif not lines:
                lines.append(l[:max(0, budget)])
            break
        lines.append(l)
        used += sep + len(l)
    return head + "\n".join(reversed(lines))


def usage(channel):
    rows = unsummarized(channel)
    return {"chars": sum(len(m["text"]) for m in rows), "messages": len(rows), "budget": lim()["context"], "compress_at": lim()["compress_at"],
            "summary": bool(meta(channel).get("summary")), "compressions": meta(channel).get("compressions", 0)}


def compress(channel, force=False):
    """把旧消息压成要点（最近 KEEP_RECENT 条原样留着）。有 API 队员就让它总结，没有就退回截断。返回说明文字。"""
    rows = unsummarized(channel)
    if not force and sum(len(m["text"]) for m in rows) < lim()["compress_at"]:
        return None
    old = rows[:-KEEP_RECENT] if len(rows) > KEEP_RECENT else []
    if not old:
        return None
    names = {k: v.get("name", k) for k, v in agents_dict(load_config()).items()}
    names["user"] = "主人"
    mt = meta(channel)
    text = (f"已有要点：\n{mt['summary']}\n\n" if mt.get("summary") else "") + "\n".join(_line(m, names) for m in old)
    summary, who = None, None
    duty = next((aid for aid, a in agents_dict(load_config()).items() if a.get("api") and a.get("run")), None)  # 压缩用 API 队员（便宜）
    if duty:
        import subprocess
        import pf
        p = pf.run_tree([*pf.cli_cmd(), "ask", duty, "把下面这段群聊压缩成要点，给后来的队员当上下文用：保留谁要了什么、定了什么、谁做了什么、产出文件路径、还没解决的问题；"
                            "去掉寒暄和重复。用条目写，不超过 800 字，只输出要点本身。\n\n" + text[-30000:], "--new", "--tag", "整理总结"],
                        timeout=300, env={**os.environ, "AGENTBUS_CONV": "chat-compress"})
        if p.returncode == 0 and p.stdout.strip():
            summary, who = p.stdout.strip()[:2000], duty
    if not summary:  # 没有 API 队员或者没成功：每条只留开头
        summary = (mt.get("summary", "") + "\n" + "\n".join(_line(m, names)[:160] for m in old))[-3000:]
    set_meta(channel, summary=summary, summary_upto=old[-1]["id"], summary_upto_t=old[-1].get("t", 0), compressions=mt.get("compressions", 0) + 1, summary_t=time.time())
    append(channel, "agentbus", f"📝 前面 {len(old)} 条消息压缩成了要点（{('由 ' + names.get(who, who) + ' 总结') if who else '截断'}），之后给队员的上下文用要点 + 最近的消息。", system=True)
    return summary


def running(channel):
    """这个频道现在在跑的活。"""
    out = []
    for f in (HOME / "running").glob("*.json"):
        r = load_json(f, {})
        if (r.get("conv") or "").startswith(conv_of(channel)):
            out.append(dict(r, _file=str(f)))  # 停止时要写“已取消”、5 秒后重读执行器进程
    return out


def conv_of(channel):
    import hashlib
    return "chat-" + hashlib.md5(channel.encode()).hexdigest()[:7]  # 12 位，一个频道一个，派活记录和账单按它分


def send(channel, text, frm="user", hops=0, cwd=None, files=None, *, target=None, issues=None):
    """发一条消息。派活模式：@ 到的队员各派一单（没 @ 人就交给值班的）；群聊模式：主人一句话，队员轮流接着聊。
    返回 (消息, [派出去的 job id])。"""
    cfg = load_config()
    L = lim()
    text = text[:L["max_text"]]
    mt = meta(channel)
    msg = append(channel, frm, text, hops=hops, files=files or [])
    jobs = []
    if frm != "user" and hops >= L["hops"]:
        append(channel, "agentbus", f"⏸ 队员之间已经接力了 {hops} 次，先停一下，等主人看看（主人 @ 谁都会继续）。", system=True)
        return msg, jobs
    agents = agents_dict(cfg)
    who = [target] if target else mentions(text, cfg)
    if (target and target not in agents) or (frm == "user" and not who and text.lstrip().startswith("@")):
        failure(channel, {}, "unknown_agent", issues=issues)
        return msg, jobs
    if frm == "user" and mt["mode"] == "talk" and not who:  # 群聊：没点名就让成员轮流聊
        start_talk(channel, msg["id"], cwd)
        return msg, jobs
    if frm == "user" and not who:
        duty = resolve_duty(mt.get("duty"))
        if duty:
            who = [duty]
        else:
            append(channel, "agentbus", "没 @ 人，这个频道设成了“没人回”：这条只记下来。要人回就 @ 他，或者在频道设置里改。", system=True)
            return msg, jobs
    from config import locked
    with locked("chat-" + conv_of(channel)):  # 同一个频道的准入和派活排队做：几条消息同时到也不会超出并发上限
        return msg, _dispatch_all(channel, who, agents, text, frm, hops, cwd, files, L, issues)


def _wait_registered(jid, timeout=6):
    """等刚派出去的这单登记进 running（派活进程起来要一两秒），这样下一次准入能数到它。"""
    import mcp_server
    try:
        pid = json.loads((mcp_server.JOBS / f"{jid}.json").read_text()).get("pid")
    except (OSError, ValueError):
        return
    end = time.time() + timeout
    while time.time() < end:
        if any(load_json(f, {}).get("pid") == pid for f in (HOME / "running").glob("*.json")):
            return
        time.sleep(0.2)


def _dispatch_all(channel, who, agents, text, frm, hops, cwd, files, L, issues=None):
    jobs = []
    busy = len(running(channel))
    for aid in who:
        if aid == frm:
            continue
        a = agents.get(aid) or {}
        if sealed(a):
            failure(channel, a, "sealed", issues=issues)
            continue
        if a.get("billing") == "manual" or not a.get("run"):  # 网页版没法派
            import subprocess
            import pf
            subprocess.run([*pf.cli_cmd(), "post", aid, f"[聊天室·{channel}] {text}", "--from", frm], capture_output=True, timeout=30)
            failure(channel, a, "manual", issues=issues)
            continue
        if busy >= L["max_running"]:
            failure(channel, a, "channel_busy", issues=issues)
            continue
        from config import dispatch_backend
        if not dispatch_backend(a):
            failure(channel, a, "cli_unavailable", issues=issues)
            continue
        try:
            jid = dispatch(channel, aid, text, frm, hops, cwd, files)
        except (OSError, ValueError, SystemExit):
            import logging
            logging.exception("无法提交聊天室工单给 %s", aid)
            failure(channel, a, "submit_failed", issues=issues)
            continue
        jobs.append(jid)
        busy += 1
        _wait_registered(jid)
    return jobs


def dispatch(channel, aid, text, frm, hops, cwd=None, files=None):
    import mcp_server
    names = {k: v.get("name", k) for k, v in agents_dict(load_config()).items()}
    att = "".join(f"\n附件：{x['path']}" for x in files or [])
    task = (f"【agentbus 聊天室 · 频道「{channel}」】{('主人' if frm == 'user' else names.get(frm, frm))} 在群里找你：\n{text}{att}\n\n"
            f"群里最近的消息（参考上下文）：\n{context(channel)}\n\n"
            "要求：按这条消息把事做完；你最后的回复会原样发回群里，所以写给大家看，简短说清做了什么、产出在哪。"
            "需要别的队员接着做，就在回复里 @ 他（比如 @GPT），他会被叫醒接力。")
    env = {"AGENTBUS_CHAT": channel, "AGENTBUS_CHAT_HOPS": str(hops + (0 if frm == "user" else 1)), "AGENTBUS_CONV": conv_of(channel),
           "AGENTBUS_SELF": frm if frm != "user" else "user"}
    args = {"agent": aid, "task": task, "tag": None, "new": True}
    jid = mcp_server.start_job(args, env_extra=env, cwd=cwd)
    append(channel, "agentbus", f"⏳ 已提交给 {names.get(aid, aid)}，等待执行器启动", system=True, job=jid, agent=aid)
    return jid


# ---------- 群聊（头脑风暴）：主人一句话，成员按顺序挨个读群里的内容接着聊 ----------
def talk_members(channel):
    cfg = load_config()
    agents = agents_dict(cfg)
    ms = [m for m in meta(channel).get("members") or [] if m in agents]
    if not ms:  # 没选就用主位办公室战备里的前 4 位
        from config import active_lead
        own = (cfg.get("office_lineups") or {}).get(active_lead(cfg) or "")
        ms = ((own or {}).get("office") or cfg["roster"]["office"])[:4]
    return [m for m in ms if agents.get(m, {}).get("run") and not sealed(agents.get(m, {})) and agents.get(m, {}).get("billing") != "manual"]


def start_talk(channel, msg_id, cwd=None):
    import subprocess
    import pf
    from config import locked
    with locked("chat-" + conv_of(channel)):
        pid = meta(channel).get("talk_pid")
        if pid and _alive(pid):  # 这个频道已经在群聊：不再开第二场，新消息下一位发言时会读到
            append(channel, "agentbus", "💬 群聊还在进行，你这条已经加进去了，下一位发言时会看到。", system=True)
            return
        _start_talk(channel, msg_id, cwd)


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _start_talk(channel, msg_id, cwd=None):
    import subprocess
    import pf
    (CHAT / f".stop-{conv_of(channel)}").unlink(missing_ok=True)
    p = subprocess.Popen([*pf.cli_cmd(), "chat-talk", channel, msg_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=cwd or str(Path.home()), **pf.group_kwargs())
    set_meta(channel, talk_pid=p.pid, talk_t=time.time())
    names = {k: v.get("name", k) for k, v in agents_dict(load_config()).items()}
    append(channel, "agentbus", f"💬 群聊开始：{'、'.join(names.get(m, m) for m in talk_members(channel))} 按顺序接着聊（最多 {meta(channel)['rounds']} 轮、{lim()['talk_max']} 条，随时可以点停止）。", system=True)


def stopped(channel):
    return (CHAT / f".stop-{conv_of(channel)}").exists()


def stop(channel):
    """停：群聊不再往下轮；这个频道在跑的活整组停掉。"""
    import pf
    (CHAT / f".stop-{conv_of(channel)}").write_text("1")
    n = 0
    for r in running(channel):
        try:
            n += pf.kill_job(r)  # 外层和实际执行器两组都停，5 秒后没停的强杀
        except (OSError, ValueError):
            pass
    pid = meta(channel).get("talk_pid")
    if pid:
        try:
            pf.kill_job({"pid": pid})
        except (OSError, ValueError):
            pass
    append(channel, "agentbus", f"⏹ 主人喊停了{f'（停掉 {n} 单在跑的活）' if n else ''}。", system=True)


def _similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a[:600], b[:600]).ratio()


def run_talk(channel, msg_id):
    """群聊主循环（单独的进程跑）：一轮里成员挨个发言；跳过 / 重复的不发；一整轮没人说话就结束。"""
    import subprocess
    import pf
    cfg = load_config()
    agents = agents_dict(cfg)
    names = {k: v.get("name", k) for k, v in agents.items()}
    L = lim()
    members, rounds, said = talk_members(channel), max(1, min(TALK_ROUNDS, int(meta(channel).get("rounds") or L["talk_rounds"]))), 0
    topic = next((m["text"] for m in read(channel, 200) if m["id"] == msg_id), "")
    for r in range(rounds):
        spoke = 0
        for aid in members:
            if stopped(channel) or said >= L["talk_max"]:
                break
            a = agents[aid]
            jobs = "、".join(k[4:] for k, v in (cfg.get("jobs") or {}).get(aid, {}).items() if v == "prefer") or "通用"
            prompt = (f"你在 agentbus 的群聊「{channel}」里，你是 {names[aid]}（{a.get('vendor', '')}，擅长：{jobs}）。"
                      f"主人发起的话题：{topic}\n\n群里到现在的消息：\n{context(channel)}\n\n"
                      f"现在轮到你发一条群聊消息（第 {r + 1} 轮）：从你擅长的角度说点有用的——补充、反驳、提问、给具体建议都行，可以 @ 某位队员问他。"
                      f"不超过 {L['talk_chars'] // 2} 字，不要客套，不要重复别人说过的。只发言，**不要动手改文件、不要跑命令**。"
                      "如果你没有新东西可说，就只回复「（跳过）」。")
            p = None
            try:
                # 和群聊进程同一组：主人喊停时整组一起停；超时则先 SIGTERM，让 ask 自己停掉执行器再收尾
                p = subprocess.Popen([*pf.cli_cmd(), "ask", aid, prompt, "--new", "--tag", "头脑风暴"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True, env={**os.environ, "AGENTBUS_CONV": conv_of(channel), "AGENTBUS_SELF": "user"})
                try:
                    reply = (p.communicate(timeout=600)[0] or "").strip()
                except subprocess.TimeoutExpired:
                    p.terminate()
                    try:
                        p.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                    reply = "等待响应超时"
            except OSError:
                reply = "无法启动执行器"
            if stopped(channel):
                break
            if p is None or p.returncode:
                failure(channel, a, "failed", reply)
                continue
            if not reply or "（跳过）" in reply[:20] or reply.startswith("（") and "没有回复" in reply[:30]:
                continue
            reply = reply[:L["talk_chars"]]
            if any(_similar(reply, m["text"]) > 0.85 for m in read(channel, 30) if not m.get("system")):
                continue  # 和前面说过的差不多：不发
            append(channel, aid, reply, talk=True)
            said += 1
            spoke += 1
        if stopped(channel) or said >= L["talk_max"] or spoke == 0:
            break
    if not stopped(channel):
        append(channel, "agentbus", f"💬 这轮群聊结束：{said} 条发言。想继续就再说一句，或者 @ 某位单独聊。", system=True)
    if meta(channel).get("talk_pid") in (os.getpid(), None):  # 只清自己这一场的标记，不误清后来开的
        set_meta(channel, talk_pid=None)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4 and sys.argv[1] == "talk":
        run_talk(sys.argv[2], sys.argv[3])
