"""派出去的一单活：边跑边看它在干什么、中途补充、停下改方向、停止、接管。所有执行器走同一套，不分是谁。

数据都在 running/<单号>.json 旁边：
  running/<单号>.json          在跑的记录（agentbus ask 写；live = 执行器输出实时写到哪，logs = 它自己的会话日志）
  running/<单号>.notes.jsonl   主人在界面里补充的话（agentbus ask 这一轮跑完就接着同一个会话发给它）
工作过程从执行器的实时输出里读：codex / opencode / 反重力 / CodeBuddy 边跑边吐 JSON 事件；只在最后才输出的（zcode）读它自己的会话日志；
别的就显示原始输出的最后几行。规则翻译，不调 AI、不耗额度。
"""
import json
import os
import re
import time
from pathlib import Path

from config import HOME, locked, load_json, save_json, sealed, update_json

RUNNING = HOME / "running"


def short(s, n=300):
    import html
    s = " ".join(html.unescape(str(s or "")).split())  # 反重力会把命令里的 & < > 转义
    return s if len(s) <= n else s[:n] + "…"


def find(job_id):
    """按单号（项目/文件名 或 只给文件名）找在跑的记录，带上 _file。"""
    tail = str(job_id).split("/")[-1]
    f = RUNNING / (tail + ".json")
    if not f.exists():
        return None
    try:
        r = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    r["_file"] = str(f)
    return r


def _tail(path, nbytes=600_000, start=0):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            offset = max(start, size - nbytes)
            fh.seek(offset)
            data = fh.read().decode("utf-8", "ignore")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if offset > start else lines


def _ev(out, ts, k, text, detail=""):
    """k：想 / 做 / 说 / 结果 / 错 / 补充。"""
    if text:
        out.append({"ts": ts, "k": k, "text": short(text, 240), "detail": short(detail, 1200) if detail else ""})


def _doubao_events(lines, out, ts):
    decoder = json.JSONDecoder()
    remaining = "\n".join(lines).lstrip()
    while remaining.startswith("{"):
        try:
            event, end = decoder.raw_decode(remaining)
        except ValueError:  # 在跑的任务可能还没写完下一段回执
            break
        remaining = remaining[end:].lstrip()
        if event.get("platform") != "headless":
            continue
        reply = event.get("reply")
        if isinstance(reply, dict) and reply.get("text"):
            _ev(out, ts, "说", reply["text"])
        elif event.get("message") or event.get("error"):
            _ev(out, ts, "错", event.get("message") or event["error"])
        elif event.get("accepted"):
            _ev(out, ts, "做", "豆包已接单")
        elif event.get("status") == "waiting_input":
            _ev(out, ts, "做", "豆包正在等待用户确认")


def _codex(d, out, now):
    t = d.get("type")
    it = d.get("item") or {}
    if t == "item.started" and it.get("type") == "command_execution":
        _ev(out, now, "做", "跑命令：" + re.sub(r"^/bin/\w+ -lc ", "", it.get("command") or ""))
    elif t == "item.completed":
        ty = it.get("type")
        if ty == "agent_message":
            _ev(out, now, "说", it.get("text"))
        elif ty == "reasoning":
            _ev(out, now, "想", it.get("text") or it.get("summary"))
        elif ty == "command_execution":
            code = it.get("exit_code")
            _ev(out, now, "结果" if not code else "错", f"命令{'完成' if not code else f'出错（退出码 {code}）'}", it.get("aggregated_output"))
        elif ty == "file_change":
            files = [c.get("path", "") for c in it.get("changes") or []]
            _ev(out, now, "做", "改文件：" + "、".join(os.path.basename(x) for x in files[:6]))
        elif ty == "mcp_tool_call":
            _ev(out, now, "做", f"用工具 {it.get('server')}.{it.get('tool')}", json.dumps(it.get("arguments") or {}, ensure_ascii=False))
        elif ty == "web_search":
            _ev(out, now, "做", "上网搜：" + str(it.get("query") or ""))
        elif ty == "todo_list":
            _ev(out, now, "想", "计划：" + "；".join(x.get("text", "") for x in it.get("items") or []))
    elif t in ("error", "turn.failed"):
        er = d.get("error") if isinstance(d.get("error"), dict) else {}
        _ev(out, now, "错", er.get("message") or d.get("message") or "出错")


def _opencode(d, out, now):
    ts = (d.get("timestamp") or 0) / 1000 or now
    p = d.get("part") or {}
    t = d.get("type")
    if t == "text":
        _ev(out, ts, "说", p.get("text"))
    elif t == "reasoning":
        _ev(out, ts, "想", p.get("text"))
    elif t == "tool_use":
        st = p.get("state") or {}
        i = st.get("input") or {}
        what = i.get("description") or i.get("command") or i.get("filePath") or i.get("pattern") or i.get("url") or ""
        _ev(out, ts, "做", f"{p.get('tool')}：{what}", st.get("output") if isinstance(st.get("output"), str) else "")
    elif t == "error":
        er = d.get("error") or {}
        _ev(out, ts, "错", er.get("message") or er.get("type") or "出错")


def _agy(d, out, now, acc):
    ev = d.get("event")
    if ev == "step_update":
        s = d.get("step_update") or {}
        ty, st = s.get("step_type"), s.get("state")
        if ty == "tool":
            info = s.get("tool_info") or {}
            par = info.get("parameters") or {}
            what = par.get("CommandLine") or par.get("AbsolutePath") or par.get("TargetFile") or par.get("Query") or par.get("Url") or ""
            if st == "ACTIVE":
                _ev(out, now, "做", f"{s.get('tool_name')}：{what}")
            elif st == "DONE":
                _ev(out, now, "结果", f"{s.get('tool_name')} 完成", info.get("output") or "")
        elif ty == "agent_response":
            acc["text"] = acc.get("text", "") + (s.get("text_delta") or "")
            if st == "DONE" and acc.get("text", "").strip():
                _ev(out, now, "说", acc.pop("text"))
    elif ev == "result":
        r = d.get("result") or {}
        if r.get("status") not in (None, "SUCCESS"):
            _ev(out, now, "错", f"结束：{r.get('status')}")


def _codebuddy_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict)
                         and b.get("type") in ("text", "output_text") and isinstance(b.get("text"), str)).strip()
    return ""


def codebuddy_terminal(newest_first):
    return next((event for event in newest_first if isinstance(event, dict) and event.get("type") == "result"), {})


def _codebuddy(d, out, now, tools):
    typ = d.get("type")
    if typ == "system" and d.get("subtype") == "init":
        _ev(out, now, "做", "CodeBuddy CLI 已启动")
    elif typ in ("assistant", "user"):
        message = d.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if typ == "assistant":
            _ev(out, now, "说", _codebuddy_text(content))
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                if block.get("id"):
                    tools[block["id"]] = block["name"]
                _ev(out, now, "做", f"调用工具 {block['name']}")
            elif block.get("type") == "tool_result":
                name = tools.get(block.get("tool_use_id")) or "工具"
                preview = _codebuddy_text(block.get("content"))
                _ev(out, now, "错" if block.get("is_error") else "结果", f"{name} {'出错' if block.get('is_error') else '完成'}", preview)
    elif typ == "result" or (not typ and "result" in d):
        reply = _codebuddy_text(d.get("result"))
        if d.get("is_error") or str(d.get("subtype") or "").startswith("error_"):
            _ev(out, now, "错", reply or d.get("error") or "CodeBuddy CLI 执行失败")
        elif reply and not any(e["k"] == "说" and e["text"] == short(reply, 240) for e in out):
            _ev(out, now, "说", reply)


def _codebuddy_native_event(d, out, ts):
    kind = d.get("type")
    if kind == "message" and d.get("role") == "assistant":
        _ev(out, ts, "说", _codebuddy_text(d.get("content")))
    elif kind == "function_call" and d.get("name"):
        _ev(out, ts, "做", f"调用工具 {d['name']}")
    elif kind == "function_call_result" and d.get("name"):
        failed = d.get("status") in ("failed", "error", "incomplete")
        _ev(out, ts, "错" if failed else "结果", f"{d['name']} {'出错' if failed else '完成'}")


def _codebuddy_native_logs(rec, out):
    """旧启动方式下，只读这单已经核实绑定的原生会话。"""
    start = rec.get("start") or 0
    for path in rec.get("logs") or []:
        for line in _tail(path):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or not isinstance(d.get("timestamp"), (int, float)):
                continue
            ts = d["timestamp"] / 1000
            if ts < start or (rec.get("session") and d.get("sessionId") != rec["session"]):
                continue
            _codebuddy_native_event(d, out, ts)


def _codebuddy_saved_array(path):
    try:
        with path.open(encoding="utf-8") as f:
            if not f.read(64).lstrip().startswith("["):
                return None
            f.seek(0)
            entries = json.load(f)
    except (OSError, ValueError):
        return None
    return entries if isinstance(entries, list) else None


def _zcode_logs(files, out):
    for f in files:
        for l in _tail(f, 2_000_000)[-80:]:
            try:
                d = json.loads(l)
            except ValueError:
                continue
            r = d.get("response") or {}
            import activity
            ts = activity.iso_ts(d.get("completedAt")) or time.time()
            if r.get("reasoningText"):
                _ev(out, ts, "想", r["reasoningText"])
            for c in r.get("toolCalls") or []:
                i = c.get("input") or {}
                _ev(out, ts, "做", f"{c.get('name')}：{i.get('description') or i.get('command') or i.get('file_path') or ''}")
            if r.get("text"):
                _ev(out, ts, "说", r["text"])


def events(rec, n=60, codebuddy_array=None):
    """这单到现在的工作过程（最新的在最后）。"""
    out, acc = [], {}
    kind = rec.get("kind")
    live = rec.get("out")
    lines = _tail(live) if live and codebuddy_array is None else []
    mtime = os.path.getmtime(live) if live and os.path.exists(live) else time.time()
    if kind == "doubao":
        _doubao_events(lines, out, mtime)
    parsed = False
    if codebuddy_array is not None:
        for d in codebuddy_array:
            if isinstance(d, dict):
                ts = d.get("timestamp")
                ts = ts / 1000 if isinstance(ts, (int, float)) else mtime
                _codebuddy_native_event(d, out, ts)
                _codebuddy(d, out, ts, acc)
        parsed = True
    for l in lines:
        l = l.strip()
        if not l.startswith("{"):
            continue
        try:
            d = json.loads(l)
        except ValueError:
            continue
        parsed = True
        if kind == "codex":
            _codex(d, out, mtime)
        elif kind == "opencode":
            _opencode(d, out, mtime)
        elif kind == "agy":
            _agy(d, out, mtime, acc)
        elif kind == "codebuddy":
            _codebuddy(d, out, mtime, acc)
    if kind == "zcode" and rec.get("logs"):
        _zcode_logs(rec["logs"], out)
        parsed = True
    elif kind == "codebuddy" and not out and rec.get("logs"):
        _codebuddy_native_logs(rec, out)
        parsed = bool(out)
    elif not parsed and lines and lines[0].strip() == "{":  # 跑完才给一整个 JSON 的（zcode --json 这类）：只能看到结果
        try:
            whole = json.loads("\n".join(lines))
            if isinstance(whole, dict):
                if kind == "codebuddy":
                    _codebuddy(whole, out, mtime, acc)
                else:
                    _ev(out, mtime, "说", whole.get("response") or whole.get("result") or "")
                parsed = True
        except ValueError:
            pass
    if not parsed and not out and lines:  # 不吐事件的执行器：给原始输出最后几行
        for l in [x for x in lines if x.strip()][-15:]:
            _ev(out, mtime, "说", l)
    for nt in notes(rec):  # 主人的补充也放进时间线
        _ev(out, nt["t"], "补充", nt["text"] + ("" if nt.get("used") else "（等这一轮做完就发给它）"))
    out.sort(key=lambda e: e["ts"] or 0)
    no_time = kind == "codex"  # codex 的事件不带时间（只有文件改动时间），不显示，免得每步都是同一个时刻
    for e in out:
        e["t"] = time.strftime("%H:%M:%S", time.localtime(e["ts"])) if e["ts"] and (not no_time or e["k"] == "补充") else ""
    return out[-n:]


def _notes_file(rec):
    return Path(rec["_file"]).with_suffix(".notes.jsonl") if rec.get("_file") else RUNNING / (str(rec.get("run")) + ".notes.jsonl")


def notes(rec):
    f = _notes_file(rec)
    out = []
    try:
        lines = f.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return out
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def add_note(rec, text, interrupt=False):
    """主人补充一句。interrupt=True：现在就停下这一轮，带着补充接着同一个会话做（执行器停，派活那层不停）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("补充的内容是空的")
    f = _notes_file(rec)
    with locked(f.stem):
        running = load_json(rec["_file"], {})
        if not running or running.get("accept_notes") is False or running.get("stop"):
            raise ValueError("这单正在收尾。请等结果出来，再发下一条任务。")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "text": text[:4000], "used": False}, ensure_ascii=False) + "\n")
    if interrupt:
        interrupt_worker(rec)


def take_notes(running_file, close_when_empty=False):
    """agentbus ask 用：取出还没发给队员的补充，标成已用。"""
    rec = {"_file": str(running_file)}
    f = _notes_file(rec)
    with locked(f.stem):
        rows = notes(rec)
        new = [r for r in rows if not r.get("used")]
        if new:
            for r in new:
                r["used"] = True
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
            os.replace(tmp, f)
        elif close_when_empty and Path(running_file).exists():
            update_json(running_file, lambda row: row.update(accept_notes=False))
        return [r["text"] for r in new]


def interrupt_worker(rec):
    """只停执行器这一轮（派活那层收到标记后，带着补充续同一个会话）。"""
    import pf
    f = rec.get("_file")
    try:
        if not Path(f).exists():
            return False
        rec = update_json(f, lambda row: row.update(interrupt=time.time()))
    except (OSError, ValueError, TypeError):
        pass
    w = rec.get("worker")
    if not w:
        return False
    return pf.kill_job({"worker": w})


def stop(rec, takeover=False):
    """整单停掉：派活那层会写一条“被主人停掉了”（或“主人接管了”）的回复给派活的人。
    先在记录上标“主人停的”，再叫外层自己收尾（它会停掉执行器）；外层不是进程组长时（比如从终端派的）也照样停得掉。"""
    import pf
    import signal
    f = rec.get("_file")
    try:
        if not Path(f).exists():
            return False
        rec = dict(update_json(f, lambda row: row.update(stop=time.time(), takeover=takeover, cancel=True)), _file=f)
    except (OSError, ValueError, TypeError):
        pass
    if not pf.IS_WIN and rec.get("pid"):
        try:
            os.kill(int(rec["pid"]), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    return pf.kill_job(rec)


# 接管：在终端里接着这单的会话聊（先停掉后台那一单，避免两边同时写一个会话）
RESUME = {
    "codex": ["codex", "resume", "{sid}"],
    "agy": ["agy", "--conversation", "{sid}"],
    "opencode": ["opencode", "--session", "{sid}"],
    "zcode": ["zcode", "--resume", "{sid}"],
    "codebuddy": ["codebuddy", "--resume", "{sid}"],
}


def resume_cmd(kind, sid):
    if not sid or kind not in RESUME:
        return None
    return [x.replace("{sid}", sid) for x in RESUME[kind]]


def event_session(events, kind):
    fields = {"codex": ("thread_id",), "agy": ("conversation_id",),
              "opencode": ("sessionID",), "zcode": ("sessionId",),
              "codebuddy": ("session_id",), "doubao": ("conversationId",)}
    sid = None
    for event in events:
        if not isinstance(event, dict):
            continue
        if kind == "codebuddy" and event.get("type") not in ("system", "result"):
            continue
        for field in fields.get(kind, ()):
            value = event.get(field)
            if isinstance(value, str) and value:
                sid = value
    return sid


def session_of(rec):
    """这单现在的会话号：从实时输出里找（codex thread_id / 反重力 conversation_id / opencode sessionID / zcode sessionId），
    找不到就用开工时续的那个。"""
    kind = rec.get("kind")
    path = rec.get("out")
    start = rec.get("out_offset") or 0

    def events_in(lines):
        for line in lines:
            try:
                yield json.loads(line)
            except ValueError:
                continue

    sid = event_session(events_in(_tail(path, 200_000, start)), kind) if path else None
    if not sid and not rec.get("session") and path:
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                opening = fh.read(64_000).decode("utf-8", "ignore").splitlines()
            sid = event_session(events_in(opening), kind)
        except OSError:
            pass
    if not sid and rec.get("kind") == "zcode":
        for f in rec.get("logs") or []:
            m = re.search(r"(sess_[\w-]+)", f)
            if m:
                sid = m.group(1)
    return sid or rec.get("session")


def view(rec):
    """给界面的：这单的基本信息 + 工作过程 + 能做哪些操作。"""
    sid = session_of(rec)
    return {"id": rec.get("id"), "agent": rec.get("agent"), "task": rec.get("task"), "title": rec.get("title"), "start": rec.get("start"),
            "queued": rec.get("queued"), "round": rec.get("round", 1), "brain": rec.get("brain"), "retry": rec.get("retry"), "kind": rec.get("kind"),
            "stalled": rec.get("stalled"),
            "events": events(rec), "session": sid, "can_takeover": not sealed(rec.get("agent") or "") and bool(resume_cmd(rec.get("kind"), sid)),
            "streams": rec.get("kind") in ("codex", "opencode", "agy", "doubao", "codebuddy") or bool(rec.get("logs"))}


def last_done(agent_id, kind, buses):
    """这位队员最近做完的一单（没在干活时，详情页给看上一单的过程）。buses：要找的项目总线目录。"""
    best = None
    for b in buses:
        for f in Path(b, "runs").glob(f"*-{agent_id}.jsonl"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if not best or m > best[0]:
                best = (m, f)
    if not best:
        return None
    m, f = best
    task = ""
    md = f.with_suffix(".md")
    try:  # .md 里有发出的提示和回复：任务取“## 回复”前面最后一段太长，就取标题行之后第一句有字的
        txt = md.read_text("utf-8")
        reply = txt.split("\n## 回复\n", 1)[1].strip() if "\n## 回复\n" in txt else ""
    except OSError:
        reply = ""
    array = _codebuddy_saved_array(f) if kind == "codebuddy" else None
    if array is not None and reply.startswith("（CodeBuddy CLI 没有回复，退出码 0）"):
        terminal = codebuddy_terminal(reversed(array))
        if terminal.get("subtype") == "success" and not terminal.get("is_error"):
            reply = _codebuddy_text(terminal.get("result")) or reply
    rec = {"id": None, "agent": agent_id, "kind": kind, "out": str(f), "run": f.stem}
    out = {"id": None, "agent": agent_id, "done": True, "end": m, "run": f.stem, "kind": kind, "task": task,
           "reply": reply[:4000] if kind == "codebuddy" else short(reply, 400), "events": events(rec, codebuddy_array=array), "streams": True}
    return out
