"""从各 agent 自己的日志里读它最近在干什么（只读，不影响它干活）。规则翻成大白话，不调用 AI。
"""
import glob, json, os, re, sqlite3, time

H = os.path.expanduser


def short(s, n=400):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def action_brief(k, x):
    """UI summary only; original x and source/state logic remain intact."""
    if k == "想":
        return "在想下一步"
    if k == "说":
        return "写好回复了"
    raw = str(x or "")
    m = re.match(r"\[([^]]+)\]", raw)
    tool = m.group(1).lower() if m else ""
    body = raw[m.end():] if m else raw
    # Orchestration calls may wrap the actual tool in a short JavaScript snippet.
    probe = (tool + " " + body[:180]).lower()
    if any(v in probe for v in ("imagegen", "image_gen", "image_generate")):
        label = "在画角色"
    elif any(v in probe for v in ("screenshot", "snapshot", "view_image", "emitimage")):
        label = "在看截图"
    elif any(v in probe for v in ("apply_patch", "write_file", "edit_file", "multiedit")) or tool in ("edit", "write"):
        label = "在改文件"
    elif any(v in probe for v in ("search", "web__", "web.run", "browse")):
        label = "在查资料"
    elif any(v in probe for v in ("read_file", "readfile")) or tool == "read":
        label = "在读文件"
    elif any(v in probe for v in ("wait", "sleep", "poll")):
        label = "在等结果"
    elif any(v in probe for v in ("exec", "bash", "shell", "terminal", "command")):
        label = "在跑命令"
    elif any(v in probe for v in ("cua", "click", "browser", "playwright")):
        label = "在操作页面"
    else:
        label = "在处理任务"
    if label in ("在改文件", "在读文件"):
        filenames = re.findall(r"[A-Za-z0-9_-]+\.(?:html|css|js|py|md|json|tsx|ts)\b", body)
        if filenames:
            prefix = "在改" if label == "在改文件" else "在读"
            candidate = prefix + filenames[0]
            if len(candidate) <= 12:
                label = candidate
    return label[:12]


DOUBAO_CMD = {"点": "在豆包里点「{a}」", "输入": "往豆包输入框里填内容", "发送": "把内容发给豆包", "滚": "滚动豆包页面",
              "等完": "等豆包回答完", "读": "看豆包回了什么", "状态": "看豆包是不是还在生成", "截图": "截一张豆包画面", "全文": "保存豆包对话全文"}


def plain(k, x):
    """把一步动作翻成大白话（只按规则翻，不调用 AI，不耗额度）。"""
    raw = str(x or "")
    zh = lambda s: len(re.findall(r"[\u4e00-\u9fff]", s)) >= max(4, len(s) * 0.2)
    clean = lambda s: re.sub(r"\s+", " ", re.sub(r"[`*#>\[\]()]|https?://\S+|/[\w./\u4e00-\u9fff-]{12,}", "", s)).strip()
    if k == "想":
        t = clean(raw)
        return (re.split(r"[。！？\n]", t)[0][:60] + "…") if zh(t) else "在想下一步怎么做"
    if k == "说":
        t = clean(raw)
        return t[:80] + ("…" if len(t) > 80 else "") if t else "交了一段回复"
    m = re.match(r"\[([^]]+)\]\s*", raw)
    tool = (m.group(1) if m else "").lower(); body = raw[m.end():] if m else raw
    def base(p):
        mm = re.findall(r"[^\s'\"{}:,]+\.[A-Za-z0-9]{1,5}", p)
        return os.path.basename(mm[-1]) if mm else os.path.basename(p.strip()) or p
    if tool in ("read", "view_image"):
        return f"看文件「{base(body)}」"
    if tool in ("write",):
        return f"写文件「{base(body)}」"
    if tool in ("edit", "multiedit", "apply_patch"):
        return f"改文件「{base(body)}」"
    b = body
    if zh(b[:60]) and not re.search(r"[/=]|\\b(cd|python3|ffmpeg|node)\\b", b[:60]):   # Claude 的工具调用自带中文说明，直接用
        return clean(b)[:60]
    if "豆包遥控" in b:
        mm = re.search(r"豆包遥控\.mjs\S*\s+(\S+)\s*\"?([^\"]*)", b)
        if mm and mm.group(1) in DOUBAO_CMD:
            return DOUBAO_CMD[mm.group(1)].format(a=mm.group(2)[:12])
        return "操作豆包"
    if "录窗口" in b:
        return "结束录屏" if ".stop" in b else "开始录豆包窗口"
    if "假光标" in b:
        return "移动画面里的假光标"
    if "agentbus" in b:
        mm = re.search(r"agentbus\s+ask\s+(\w+)", b)
        return f"给 {mm.group(1)} 派活" if mm else "看协作总线"
    if "lark-cli" in b:
        return "读写飞书表格 / 文档"
    if "search.py" in b:
        return "在表情包库里搜表情包"
    if re.search(r"ffmpeg|ffprobe", b):
        if "gblur" in b or "boxblur" in b: return "给视频打码"
        if "ebur128" in b or "volumedetect" in b or "loudnorm" in b: return "检查 / 调整音量"
        if "-frames:v" in b or "fps=" in b: return "从视频里截图检查"
        if "ffprobe" in b and "ffmpeg" not in b: return "查看视频时长和尺寸"
        return "剪辑 / 处理视频"
    if tool in ("sleep", "wait") or b.startswith('{"duration'):
        return "等一会儿再看结果"
    if "generated_images" in b:
        return "保存刚生成的图片"
    if re.search(r"imagegen|image_gen|生图", b, re.I):
        return "生成图片"
    mm = re.search(r"python3\s+(?:-\S+\s+)*(\S+\.py)", b)
    if mm:
        return f"运行脚本「{base(mm.group(1))}」"
    if re.search(r"\bswiftc?\b", b): return "编译一个小工具"
    if re.search(r"\bcurl\b|fetch\(", b): return "联网查询 / 检查服务"
    if re.search(r"\b(cp|mv|mkdir|rm)\b", b): return "整理文件"
    if re.search(r"\b(cat|head|tail|sed|grep|ls|find|wc)\b", b): return "查看文件内容"
    if re.search(r"cua\.|click|screenshot", b, re.I): return "操作 / 查看页面"
    if tool in ("wait",): return "等待结果"
    EN = [("render", "出片 / 渲染视频"), ("dispatch", "派活给别的 AI"), ("measure", "测量"), ("ocr", "识别画面里的文字"),
          ("mask", "打码"), ("package", "打包素材"), ("backfill", "回填飞书"), ("upload", "上传文件"), ("download", "下载文件"),
          ("fix", "修改"), ("add", "新增内容"), ("write", "写文件"), ("create", "新建"), ("update", "更新"), ("record", "记录"),
          ("test", "测试"), ("check", "检查"), ("inspect", "检查"), ("verify", "核对"), ("read", "查看"), ("show", "查看"),
          ("list", "列出"), ("find", "查找"), ("search", "搜索"), ("count", "统计"), ("run", "运行"), ("build", "制作"),
          ("sample", "抽帧检查"), ("extract", "提取"), ("compare", "对比"), ("move", "移动文件"), ("copy", "复制文件")]
    low = b.lower()
    for en, cn in EN:
        if re.match(rf"\s*{en}", low):
            return cn
    return "执行一条命令"


def newest(pattern):
    fs = glob.glob(H(pattern))
    return max(fs, key=os.path.getmtime) if fs else None


def tail_lines(path, nbytes=400_000):
    with open(path, "rb") as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - nbytes))
        data = f.read().decode("utf-8", "ignore")
    lines = data.splitlines()
    return lines[1:] if size > nbytes else lines


def iso_ts(s):
    """ISO 时间 → 时间戳。带 Z / +08:00 就按它算；不带的按 UTC（各家 agent 日志写的都是 UTC）。不受夏令时影响。"""
    import datetime as _dt
    try:
        if not s:
            return None
        s = str(s).strip().replace("Z", "+00:00")
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)  # 小数秒统一成 6 位（Python 3.9 只认 3 或 6 位）
        d = _dt.datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).timestamp()
    except ValueError:
        return None


def acts_claude():
    f = newest("~/.claude/projects/*/*.jsonl")
    out = []
    if not f:
        return out
    for l in tail_lines(f):
        try:
            d = json.loads(l)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        t = iso_ts(d.get("timestamp"))
        for c in (d.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                i = c.get("input") or {}
                out.append((t, "做", f"[{c.get('name')}] " + short(i.get("description") or i.get("command") or i.get("file_path") or "")))
            elif c.get("type") == "text" and c.get("text", "").strip():
                out.append((t, "说", short(c["text"])))
    return out


def acts_zcode():
    f = newest("~/.zcode/cli/rollout/model-io-*.jsonl")
    out = []
    if not f:
        return out
    for l in tail_lines(f, 2_000_000)[-40:]:
        try:
            d = json.loads(l)
        except json.JSONDecodeError:
            continue
        r = d.get("response") or {}
        t = iso_ts(d.get("completedAt"))
        if r.get("reasoningText"):
            out.append((t, "想", short(r["reasoningText"])))
        for c in r.get("toolCalls") or []:
            i = c.get("input") or {}
            out.append((t, "做", f"[{c.get('name')}] " + short(i.get("description") or i.get("command") or i.get("file_path") or "")))
        if r.get("text"):
            out.append((t, "说", short(r["text"])))
    return out


def acts_codex():
    f = newest("~/.codex/sessions/*/*/*/*.jsonl")
    out = []
    if not f:
        return out
    for l in tail_lines(f):
        try:
            d = json.loads(l)
        except json.JSONDecodeError:
            continue
        p = d.get("payload") or {}
        t = iso_ts(d.get("timestamp"))
        if d.get("type") != "response_item":
            continue
        if p.get("type") == "message" and p.get("role") == "assistant":
            for c in p.get("content") or []:
                if c.get("text"):
                    out.append((t, "说", short(c["text"])))
        elif p.get("type") in ("function_call", "custom_tool_call"):
            out.append((t, "做", f"[{p.get('name')}] " + short(p.get("arguments") or p.get("input"))))
        elif p.get("type") == "reasoning":
            for s in p.get("summary") or []:
                if s.get("text"):
                    out.append((t, "想", short(s["text"])))
    return out


def acts_opencode():
    db = H("~/.local/share/opencode/opencode.db")
    out = []
    if not os.path.exists(db):
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        rows = con.execute("select time_updated, data from part order by time_updated desc limit 30").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    # opencode v2（2.x）把对话写在 session_message 里，一条消息的 content 是 reasoning / tool / text 列表
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        rows2 = con.execute("select time_updated, data from session_message where type='assistant' order by time_updated desc limit 12").fetchall()
        con.close()
    except sqlite3.Error:
        rows2 = []
    for tu, data in rows2:
        try:
            for c in (json.loads(data).get("content") or []):
                c2 = {"type": c.get("type"), "text": c.get("text"), "tool": c.get("name"), "state": c.get("state")}
                rows.append((tu, json.dumps(c2, ensure_ascii=False)))
        except (json.JSONDecodeError, AttributeError):
            pass
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:30]
    for tu, data in reversed(rows):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        t = tu / 1000
        if d.get("type") == "tool":
            i = (d.get("state") or {}).get("input") or {}
            out.append((t, "做", f"[{d.get('tool')}] " + short(i.get("description") or i.get("command") or i.get("filePath") or i)))
        elif d.get("type") == "reasoning" and d.get("text"):
            out.append((t, "想", short(d["text"])))
        elif d.get("type") == "text" and d.get("text"):
            out.append((t, "说", short(d["text"])))
    return out


AG_ROOTS = ("~/.gemini/antigravity", "~/.gemini/antigravity-cli")  # 反重力桌面 / 命令行 agy 的数据目录
AG_TRANSCRIPT = "/brain/*/.system_generated/logs/transcript.jsonl"  # 一个对话一份，一步一行（追加写）


def _ag_main(f):
    """主人（或派活）开的对话第一步是 USER_EXPLICIT 的输入；反重力自己起的子对话（生图、Browser Viewer…）第一步是系统消息。"""
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            d0 = json.loads(fh.readline() or "{}")
        return isinstance(d0, dict) and d0.get("type") == "USER_INPUT" and d0.get("source") == "USER_EXPLICIT"
    except (OSError, ValueError):
        return False


def acts_antigravity():
    """反重力最近动过的那个对话：PLANNER_RESPONSE 一步里有思考（thinking）、工具调用（tool_calls）、回复（content）。"""
    fs = [f for r in AG_ROOTS for f in _recent_files(r + AG_TRANSCRIPT, 30 * 60)]
    f = max(fs, key=os.path.getmtime) if fs else None
    out = []
    if not f:
        return out
    for l in tail_lines(f, 400_000)[-60:]:
        try:
            d = json.loads(l)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "PLANNER_RESPONSE":
            continue
        t = iso_ts(d.get("created_at"))
        if d.get("thinking"):
            out.append((t, "想", short(d["thinking"])))
        for c in d.get("tool_calls") or []:
            i = c.get("args") or {}
            i = {k: (v.strip('"') if isinstance(v, str) else v) for k, v in i.items()} if isinstance(i, dict) else {}
            out.append((t, "做", f"[{c.get('name')}] " + short(i.get("CommandLine") or i.get("Path") or i.get("AbsolutePath") or i.get("TargetFile") or i)))
        if d.get("content"):
            out.append((t, "说", short(d["content"])))
    return out


SOURCES = {"claude": acts_claude, "zcode": acts_zcode, "codex": acts_codex, "opencode": acts_opencode, "antigravity": acts_antigravity}


def recent(kind, n=12):
    fn = SOURCES.get(kind)
    if not fn:
        return []
    try:
        acts = [x for x in fn() if x[0]][-n:]
    except Exception as e:  # 日志格式变了也别让页面挂掉
        acts = [(time.time(), "说", f"读日志出错：{e}")]
    return [{"ts": t, "t": time.strftime("%H:%M:%S", time.localtime(t)), "k": k, "plain": plain(k, x)} for t, k, x in acts][::-1]


# ---------- 在不在一轮对话里（直接用 agent 时也能认出“在干活”，不靠派活记录）----------
# 只看日志里的动作时间，模型长时间思考、生图、跑长命令时会几分钟不写日志，被误判成“休息”；
# 所以再看它自己记的“这一轮开始了 / 结束了”。
def _recent_files(pattern, within):
    now = time.time()
    out = []
    for f in glob.glob(H(pattern)):
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if now - m < within:
            out.append((m, f))
    return [f for _, f in sorted(out, reverse=True)[:6]]


def _codex_head(f):
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            p = (json.loads(fh.readline()) or {}).get("payload") or {}
        return p
    except (OSError, json.JSONDecodeError):
        return {}


def sessions_codex():
    """GPT 正在干活的窗口（桌面端 / 命令行自己开的会话）。agentbus 派活起的（originator=codex_exec）不算窗口，它们是队员。"""
    out = []
    for f in _recent_files("~/.codex/sessions/*/*/*/*.jsonl", 30 * 60):
        head = _codex_head(f)
        if head.get("originator") == "codex_exec":
            continue
        last = None
        for l in tail_lines(f, 300_000):
            if '"task_' in l or '"turn_aborted"' in l:
                m = re.search(r'"type":\s*"(task_started|task_complete|turn_aborted)"', l)
                if m:
                    last = m.group(1)
        if last == "task_started":
            cwd = head.get("cwd") or ""
            out.append({"agent": "codex", "conv": (head.get("id") or os.path.basename(f)[-41:-6])[:12],
                        "title": os.path.basename(cwd.rstrip("/")) or "GPT", "cwd": cwd, "t": os.path.getmtime(f)})
    return out


def sessions_claude():
    """Claude 正在干活的窗口：每个对话一个文件，最后一条不是“说完了”（end_turn）就是在干活。标题用对话名，没有就用文件夹名。"""
    out = []
    for f in _recent_files("~/.claude/projects/*/*.jsonl", 10 * 60):
        last, title, cwd = None, None, None
        for l in tail_lines(f, 200_000)[-200:]:
            if '"custom-title"' in l:
                m = re.search(r'"customTitle":"([^"]*)"', l)
                title = m.group(1) if m else title
            if cwd is None and '"cwd":"' in l:
                m = re.search(r'"cwd":"([^"]*)"', l)
                cwd = m.group(1) if m else None
            if not (l.startswith('{"') and ('"type":"assistant"' in l or '"type":"user"' in l)):
                continue
            try:
                d = json.loads(l)
            except json.JSONDecodeError:
                continue
            if d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
                last = d
        if last and not (last["type"] == "assistant" and ((last.get("message") or {}).get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens", "refusal"))):
            out.append({"agent": "claude", "conv": os.path.basename(f)[:12], "title": title or os.path.basename((cwd or "").rstrip("/")) or "Claude",
                        "cwd": cwd, "t": os.path.getmtime(f)})
    return out


SESSIONS = {"codex": sessions_codex, "claude": sessions_claude}


def live_sessions(kinds=("claude", "codex")):
    out = []
    for k in kinds:
        try:
            out += SESSIONS[k]()
        except Exception:
            pass
    return out


def live_codex():
    return bool(sessions_codex())


def live_claude():
    return bool(sessions_claude())


def live_antigravity():
    """反重力有对话在一轮中间：最后一步不是“不带工具调用的回复”（主人刚发话 / 刚调了工具 / 工具在跑）。
    它不记“这一轮结束了”，被中途停掉的对话最后一步会一直停在工具上，所以只看 10 分钟内写过的。"""
    for r in AG_ROOTS:
        for f in _recent_files(r + AG_TRANSCRIPT, 10 * 60):
            if not _ag_main(f):
                continue  # 子对话在跑时主对话自己也停在工具调用上，看主对话就够了
            last = None
            for l in tail_lines(f, 200_000)[-20:]:
                try:
                    row = json.loads(l)
                    if isinstance(row, dict):
                        last = row
                except json.JSONDecodeError:
                    pass
            if last and not (last.get("type") == "PLANNER_RESPONSE" and not last.get("tool_calls")):
                return True
    return False


LIVE = {"codex": live_codex, "claude": live_claude, "antigravity": live_antigravity}


def live(kind, acts=None):
    """这个 agent 现在是不是在一轮对话中间（在干活）。没有轮次标记的看最近 90 秒有没有动作。"""
    fn = LIVE.get(kind)
    try:
        if fn:
            return fn()
    except Exception:
        return False
    return bool(acts) and time.time() - acts[0]["ts"] < 90


# ---------- 新开的对话：被动检测（不靠 agent 自己报到），悬浮窗据此问主人要不要接入 ----------
# 每个函数返回最近 within 秒内**开始**的对话：{agent, conv（完整 id）, cwd, t（开始时间）, first（第一句话）}。
# agentbus 派活起的会话（队员）要排除掉，它们不是主人开的窗口。

def starts_codex(within):
    out = []
    for f in _recent_files("~/.codex/sessions/*/*/*/*.jsonl", within):
        head = _codex_head(f)
        if not head.get("id") or head.get("originator") == "codex_exec":  # codex exec = agentbus 派活起的
            continue
        t = iso_ts(head.get("timestamp")) or os.path.getctime(f)
        out.append({"agent": "codex", "conv": head["id"], "cwd": head.get("cwd") or "", "t": t})
    return out


def starts_claude(within):
    out = []
    for f in _recent_files("~/.claude/projects/*/*.jsonl", within):
        try:
            st = os.stat(f)
        except OSError:
            continue
        t = getattr(st, "st_birthtime", st.st_ctime)
        cwd = None
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for _ in range(30):
                    l = fh.readline()
                    if not l:
                        break
                    m = re.search(r'"cwd":"([^"]*)"', l)
                    if m:
                        cwd = m.group(1)
                        break
        except OSError:
            pass
        out.append({"agent": "claude", "conv": os.path.basename(f)[:-6], "cwd": cwd or "", "t": t})
    return out


def _ro(path):
    """只读打开别家的 SQLite（不加锁、不改 journal 模式）；打不开返回 None，下一轮再试。"""
    import sqlite3
    p = H(path)
    if not os.path.exists(p):
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
        con.execute("PRAGMA query_only=1")
        return con
    except sqlite3.Error:
        return None


def starts_zcode(within):
    """ZCode 桌面和命令行共用 ~/.zcode/cli/db/db.sqlite：session 一行一个对话；parent_id 有值的是子任务。"""
    con = _ro("~/.zcode/cli/db/db.sqlite")
    if not con:
        return []
    out = []
    try:
        since = (time.time() - within) * 1000
        for sid, t, cwd in con.execute("SELECT id, time_created, directory FROM session WHERE time_created >= ? AND parent_id IS NULL"
                                       " AND time_deleted IS NULL ORDER BY time_created", (since,)):
            first = con.execute("""SELECT json_extract(p.data, '$.text') FROM message m JOIN part p ON p.message_id=m.id
                WHERE m.session_id=? AND json_extract(m.data, '$.role')='user' AND json_extract(p.data, '$.type')='text'
                  AND COALESCE(json_extract(p.data, '$.synthetic'), 0)=0
                ORDER BY m.sequence, m.time_created, p.id LIMIT 1""", (sid,)).fetchone()
            out.append({"agent": "zcode", "conv": sid, "cwd": cwd or "", "t": t / 1000, "first": first[0] if first else None})
    except Exception:
        pass
    finally:
        con.close()
    return out


def starts_opencode(within):
    """OpenCode 2.x 用 session_v2 / session_message；老版本退回 session 表。parent_id / fork 的是子会话，不问。"""
    con = _ro("~/.local/share/opencode/opencode.db")
    if not con:
        return []
    out = []
    since = (time.time() - within) * 1000
    try:
        rows = con.execute("""SELECT s.id, s.time_created, s.directory,
                (SELECT json_extract(m.data, '$.text') FROM session_message m WHERE m.session_id=s.id AND m.type='user' ORDER BY m.seq LIMIT 1)
            FROM session_v2 s WHERE s.time_created >= ? AND s.parent_id IS NULL AND s.fork_session_id IS NULL""", (since,)).fetchall()
    except Exception:
        try:
            rows = con.execute("""SELECT s.id, s.time_created, s.directory,
                    (SELECT json_extract(p.data, '$.text') FROM message m JOIN part p ON p.message_id=m.id
                     WHERE m.session_id=s.id AND json_extract(m.data, '$.role')='user' AND json_extract(p.data, '$.type')='text'
                     ORDER BY m.time_created LIMIT 1)
                FROM session s WHERE s.time_created >= ? AND s.parent_id IS NULL""", (since,)).fetchall()
        except Exception:
            rows = []
    finally:
        con.close()
    return [{"agent": "opencode", "conv": sid, "cwd": cwd or "", "t": t / 1000, "first": first} for sid, t, cwd, first in rows]


def starts_antigravity(within):
    """反重力（桌面 ~/.gemini/antigravity、命令行 agy ~/.gemini/antigravity-cli）：conversations/<UUID>.db 一个对话。
    它的库普通只读打不开，所以不碰库：按新出现的文件找对话，第一句从 brain/<UUID>/…/transcript.jsonl 里读 <USER_REQUEST>。"""
    out = []
    for root in AG_ROOTS:
        for f in _recent_files(root + "/conversations/*.db", within):
            try:
                st = os.stat(f)
            except OSError:
                continue
            uid = os.path.basename(f)[:-3]
            first, helper = None, False
            tr = H(f"{root}/brain/{uid}/.system_generated/logs/transcript.jsonl")
            try:  # 主人开的对话第一步一定是主人的输入（USER_EXPLICIT）；反重力自己起的子对话（比如 Browser Viewer）第一步是系统消息
                with open(tr, encoding="utf-8", errors="ignore") as fh:
                    l = fh.readline()
                    if l:
                        d0 = json.loads(l)
                        if d0.get("type") != "USER_INPUT" or d0.get("source") != "USER_EXPLICIT":
                            helper = True
                        else:
                            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", d0.get("content") or "", re.S)
                            first = m.group(1) if m else ""
            except (OSError, ValueError):
                pass
            if helper:
                continue
            out.append({"agent": "gemini", "name": "Antigravity", "conv": uid, "cwd": "", "t": getattr(st, "st_birthtime", st.st_ctime), "first": first,
                        "desktop": root == AG_ROOTS[0]})  # 派活只走命令行 agy，桌面目录里的一定是主人自己开的
    return out


def starts_workbuddy(within):
    """WorkBuddy 桌面：~/.workbuddy/workbuddy.db 的 sessions；后台自动化、云端同步的不问。第一句在 projects/*/<id>.jsonl 的 <user_query>。"""
    con = _ro("~/.workbuddy/workbuddy.db")
    if not con:
        return []
    out = []
    try:
        rows = con.execute("""SELECT id, cwd, created_at FROM sessions WHERE created_at >= ? AND deleted_at IS NULL
            AND COALESCE(is_background_automation, 0)=0 AND COALESCE(transport, 'local')='local'""", ((time.time() - within) * 1000,)).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    for sid, cwd, t in rows:
        first = None
        for f in glob.glob(H(f"~/.workbuddy/projects/*/{sid}.jsonl"))[:1]:
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    for _ in range(30):
                        l = fh.readline()
                        if not l:
                            break
                        m = re.search(r"<user_query>(.*?)</user_query>", l, re.S)
                        if m:
                            first = json.loads('"' + m.group(1).replace('"', '\\"') + '"') if "\\" in m.group(1) else m.group(1)
                            break
            except (OSError, ValueError):
                pass
        out.append({"agent": "codebuddy", "name": "WorkBuddy", "conv": sid, "cwd": cwd or "", "t": t / 1000, "first": first})
    return out


STARTS = {"codex": starts_codex, "claude": starts_claude, "zcode": starts_zcode, "opencode": starts_opencode,
          "antigravity": starts_antigravity, "workbuddy": starts_workbuddy}

# agentbus 自己派的活：提示词开头是固定模板（core/agentbus.py build_prompt），群聊、聊天室也有固定开头
_DISPATCH = re.compile(r"^\s*(# 你是 .{1,40}在「.*」项目里接活|你在 agentbus 的群聊|【agentbus )")


def _dispatched_ids():
    """派活记录里出现过的会话 id（各项目 bus/sessions.json 的值）。"""
    ids = set()
    try:
        from config import projects, HOME as _HOME
        buses = [p.get("bus") for p in projects().values() if isinstance(p, dict)] + [str(_HOME / "general")]
        for b in buses:
            try:
                ids.update(v for v in json.load(open(os.path.join(b, "sessions.json"))).values() if isinstance(v, str))
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    return ids


def _dispatch_windows():
    """agentbus 派活的开工时刻：在跑的 + 最近 15 分钟收尾的。[(队员底子, 开工时间, 项目文件夹)]。
    派活起的会话 id 要等收尾才写进 sessions.json，运行期间靠“同一个队员、开工那几十秒里新开的对话”认出来。"""
    out = []
    try:
        from config import HOME as _H, LEDGER, agents_dict, projects
        ags, ps, now = agents_dict(), projects(), time.time()
        recs = []
        for f in (_H / "running").glob("*.json"):
            try:
                recs.append(json.load(open(f)))
            except (OSError, ValueError):
                pass
        try:
            with open(LEDGER, "rb") as fh:  # 只看账本末尾
                fh.seek(max(0, os.path.getsize(LEDGER) - 200_000))
                for line in fh.read().decode("utf-8", "ignore").splitlines()[1:]:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("start") and now - (r.get("end") or r["start"]) < 900:
                        recs.append(r)
        except OSError:
            pass
        for r in recs:
            a = ags.get(r.get("agent")) or {}
            base = a.get("clone_of") or r.get("agent")
            root = (ps.get(r.get("project")) or {}).get("root") if isinstance(ps.get(r.get("project")), dict) else None
            for t in (r.get("start"), r.get("retry_t")):
                if t:
                    out.append((base, float(t), root))
    except Exception:
        pass
    return out


def _from_dispatch(x, wins):
    for base, t0, root in wins:
        if base == x.get("agent") and t0 - 5 <= x["t"] <= t0 + 45:
            if not root or not x.get("cwd") or os.path.realpath(x["cwd"]) == os.path.realpath(root):
                return True
    return False


def new_conversations(within=600):
    """各家新开的对话，去掉 agentbus 派活起的。first=None 表示第一句还没落盘（调用方先等等再问）。"""
    out, mine, wins = [], None, []
    for k, fn in STARTS.items():
        try:
            for x in fn(within):
                if time.time() - x["t"] >= within:
                    continue
                if x.get("first") and _DISPATCH.search(x["first"]):
                    continue
                if mine is None:
                    mine, wins = _dispatched_ids(), _dispatch_windows()
                if x["conv"] in mine or (not x.get("desktop") and _from_dispatch(x, wins)):
                    continue
                out.append(x)
        except Exception:
            pass
    return out



# 看对话记录里真的用过 agentbus：调过 agentbus 的工具 / 跑过 agentbus 命令，或者主人自己在对话里说了“agentbus”。
# 派活起的 GPT 会话（originator=codex_exec）是队员，不算窗口。每个文件只往后读新增的部分，结果存在 ~/.agentbus/buswin.json。
BUSWIN_CACHE = os.path.join(os.environ.get("AGENTBUS_HOME") or os.path.expanduser("~/.agentbus"), "buswin.json")
_bw = None


# 真的用过 agentbus：调过 agentbus 的 MCP 工具，或者执行过 agentbus 命令（ask / wait / news / post / hello…）。
# 只是聊到 agentbus、窗口名里带 agentbus、读 agentbus 的代码都不算。
_CLI = re.compile(r"(?:^|[\s;&|(\"'`])(?:~/\.local/bin/)?agentbus\s+(?:-p\s+\S+\s+)?(?:ask|wait|news|post|inbox|hello|who|quota|bill|batch|status|hook|move|assign|suggest|lesson)\b")


def _bus_hit(kind, line):
    if "agentbus" not in line:
        return False
    if kind == "claude":
        if '"type":"tool_use"' not in line:
            return False
        if '"name":"mcp__agentbus__' in line:
            return True
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return False
        for c in (d.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash" and _CLI.search(str((c.get("input") or {}).get("command", ""))):
                return True
        return False
    if '"custom_tool_call"' not in line and '"type":"function_call"' not in line:
        return False
    try:
        p = (json.loads(line).get("payload") or {})
    except json.JSONDecodeError:
        return False
    body = str(p.get("input") or p.get("arguments") or "")
    if "agentbus_" in str(p.get("name") or "") or "mcp__agentbus__" in body:  # MCP 工具（GPT 也会在 exec 的代码里调 tools.mcp__agentbus__…）
        return True
    return bool(_CLI.search(body))


def bus_windows(days=7):
    """[(agent, conv12, cwd, mtime)]：近几天接入过 agentbus 的 Claude / GPT 对话窗口。"""
    global _bw
    if _bw is None:
        try:
            _bw = json.load(open(BUSWIN_CACHE))
        except (OSError, json.JSONDecodeError):
            _bw = {}
        if _bw.get("_v") != 3:  # 判定规则改过：旧结果作废，重扫
            _bw = {"_v": 3}
    now, changed, out = time.time(), False, []
    for kind, pattern in (("claude", "~/.claude/projects/*/*.jsonl"), ("codex", "~/.codex/sessions/*/*/*/*.jsonl")):
        for f in _recent_files(pattern, days * 86400) if False else glob.glob(H(pattern)):
            try:
                st = os.stat(f)
            except OSError:
                continue
            if now - st.st_mtime > days * 86400:
                continue
            c = _bw.get(f) or {"off": 0, "hit": False, "skip": False, "conv": None, "cwd": None}
            if not c["hit"] and not c["skip"] and st.st_size > c["off"]:
                with open(f, "rb") as fh:
                    if c["off"] == 0 and kind == "codex":
                        head = (json.loads(fh.readline() or b"{}") or {}).get("payload") or {}
                        if head.get("originator") == "codex_exec":
                            c["skip"] = True
                        c["conv"], c["cwd"] = (head.get("id") or "")[:12], head.get("cwd")
                        fh.seek(0)
                    if kind == "claude":
                        c["conv"] = os.path.basename(f)[:12]
                    fh.seek(c["off"])
                    data = fh.read()
                text = data.decode("utf-8", "ignore")
                last_nl = text.rfind("\n")
                for l in text[:last_nl + 1].splitlines() if last_nl >= 0 else []:
                    if c["cwd"] is None and kind == "claude" and '"cwd":"' in l:
                        m = re.search(r'"cwd":"([^"]*)"', l)
                        c["cwd"] = m.group(1) if m else None
                    if not c["skip"] and _bus_hit(kind, l):
                        c["hit"] = True
                        break
                c["off"] = c["off"] + len(text[:last_nl + 1].encode("utf-8")) if not c["hit"] else st.st_size
                _bw[f] = c
                changed = True
            if c.get("hit") and not c.get("skip"):
                out.append((kind, c["conv"], c.get("cwd"), st.st_mtime))
    if changed:
        _bw = {k: v for k, v in _bw.items() if k == "_v" or os.path.exists(k)}
        tmp = BUSWIN_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_bw, fh)
        os.replace(tmp, BUSWIN_CACHE)
    return out
