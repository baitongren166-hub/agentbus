"""项目经验包：一支队伍做一个项目的“工作流 + 步骤 skill + 经验条目 + 偏好”。

放在项目总线目录的 playbook/ 下，可以整包拷给别人：
  playbook/
    manifest.json          名字、版本、作者、说明、需要的能力、更新记录
    workflow.md            整条工作流（给主位看：先后顺序、每步谁合适）
    steps/<id>/SKILL.md    每一步一个标准 skill（frontmatter：name / description / step / tags / caps / scope）
    lessons.jsonl          经验条目（一行一条：id / text / tags / scope / source / date）
本机才有、不进包的：
  playbook/usage.json      每条经验、每一步被用过几次
  ~/.agentbus/config.json 里 lineups[项目].playbook   这个项目用不用经验包、关掉了哪些步骤和经验

经验写给“岗位（职能词条）和能力”，不写给具体哪个 agent，这样换一套阵容也能用。
"""
import json
import os
import re
import time
from pathlib import Path

from catalog import CAPS, category_of, all_tags

FRONT = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def _front(text):
    """极简 frontmatter：key: value，列表写成 [a, b]。"""
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        meta[k.strip()] = v
    return meta, m.group(2).strip()


def pb_dir(bus):
    return Path(bus) / "playbook"


def load(bus):
    """读经验包；没有就返回 None。"""
    d = pb_dir(bus)
    if not (d / "manifest.json").exists():
        return None
    manifest = json.loads((d / "manifest.json").read_text("utf-8"))
    steps = []
    for f in sorted((d / "steps").glob("*/SKILL.md")):
        meta, body = _front(f.read_text("utf-8"))
        steps.append({"id": f.parent.name, "name": meta.get("name", f.parent.name), "description": meta.get("description", ""),
                      "step": int(meta.get("step") or 0), "tags": meta.get("tags") or [], "caps": meta.get("caps") or [],
                      "scope": meta.get("scope", "本项目"), "body": body,
                      "_after": meta["after"] if isinstance(meta.get("after"), list) else ([meta["after"]] if meta.get("after") else None)})
    steps.sort(key=lambda s: (s["step"], s["id"]))
    # 前置步骤：SKILL.md 里写 after: [步骤id]（可以多个 = 汇合；几步都 after 同一步 = 并行）；没写就默认接在上一步后面
    ids = {s["id"] for s in steps}
    for i, s in enumerate(steps):
        a = s.pop("_after", None)
        s["after"] = [x for x in a if x in ids] if a is not None else ([steps[i - 1]["id"]] if i else [])
    lessons = []
    lf = d / "lessons.jsonl"
    if lf.exists():
        for line in lf.read_text("utf-8").splitlines():
            try:
                lessons.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    usage = json.loads((d / "usage.json").read_text("utf-8")) if (d / "usage.json").exists() else {}
    workflow = (d / "workflow.md").read_text("utf-8") if (d / "workflow.md").exists() else ""
    return {"manifest": manifest, "steps": steps, "lessons": lessons, "usage": usage, "workflow": workflow}


def equip_of(cfg, project_name):
    """这个项目派活时带不带经验包：默认不带。干活以本地文档（工作流、复盘经验）为准，经验包是分享 / 导入用的。"""
    lu = (cfg.get("lineups") or {}).get(project_name) or {}
    return dict({"enabled": False, "steps_off": [], "lessons_off": []}, **(lu.get("playbook") or {}))


def _match(tags, want):
    """词条命中：同一个词条，或者同一个大类；“全流程”的总是命中。"""
    if "全流程" in tags:
        return True
    for t in tags:
        if t in want:
            return True
        cid = category_of(t)
        if cid and any(category_of(w) == cid for w in want):
            return True
    return False


def guess_tags(task):
    """派活没写 --tag 时，从任务文字里认出提到的职能词条。"""
    return [t for t in all_tags() if t.replace(" ", "") in task.replace(" ", "")]


def _terms(st):
    """一步的关键词：职能词条 + 步骤名拆开的词（「找网络素材和表情包」→ 找网络素材、表情包）。"""
    words = list(st["tags"]) + [st["name"]] + [w for w in re.split(r"[与和、/ ]+", st["name"]) if len(w) >= 2]
    return list(dict.fromkeys(w.replace(" ", "") for w in words if w))


def step_for(steps, tag=None, head="", full=""):
    """这次派活是哪一步。返回 (步骤, 说明)。
    写了职能：词条完全相同最好，其次互相包含（「素材」能对上「找图片素材」）。
    没写：给每一步打分——一句话交代里出现一次算 3 分，任务书里出现一次算 1 分（最多 5），分最高且明显领先才算，
    差不多的就不带步骤、提醒补 --tag，免得像「找素材」被认成「录屏」。"""
    if tag:
        t = tag.replace(" ", "")
        exact = [s for s in steps if t in [x.replace(" ", "") for x in s["tags"]]]
        if exact:
            return exact[0], f"职能「{tag}」"
        part = [s for s in steps if any(t in w or w in t for w in _terms(s) if len(w) >= 2)]
        if len(part) == 1:
            return part[0], f"职能「{tag}」对上「{part[0]['name']}」"
        if part:
            head_score = lambda s: sum(head.count(w) for w in _terms(s))
            part.sort(key=head_score, reverse=True)
            return part[0], f"职能「{tag}」最接近「{part[0]['name']}」"
    h, f = head.replace(" ", ""), full.replace(" ", "")
    scored = []
    for s in steps:
        sc = sum(3 * h.count(w) + min(5, f.count(w)) for w in _terms(s))
        scored.append((sc, s))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 3:
        return None, "没写职能，也认不出是哪一步"
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 3:
        return None, f"没写职能，像「{scored[0][1]['name']}」也像「{scored[1][1]['name']}」，没带步骤"
    return scored[0][1], f"没写职能，按任务内容认成「{scored[0][1]['name']}」"


def pick(pb, equip, tag, task, max_lessons=8, head=""):
    """挑出这次派活用得上的一步和几条经验。head = 一句话交代（比附带的任务书更能说明这次干什么）。"""
    steps = [s for s in pb["steps"] if s["id"] not in equip["steps_off"]]
    step, why = step_for(steps, tag, head or task[:200], task)
    pick.why = why
    want = ([tag] if tag else guess_tags(head or task)) + (step["tags"] if step else [])
    lessons = [x for x in pb["lessons"] if x["id"] not in equip["lessons_off"]]
    near = want
    def score(x):  # 词条完全命中 3 分，同一大类 1 分
        tags = [t for t in (x.get("tags") or []) if t != "全流程"]
        exact = sum(1 for t in tags if t in near)
        cat = 1 if any(t not in near and category_of(t) and any(category_of(w) == category_of(t) for w in near) for t in tags) else 0
        return exact * 3 + cat
    hit = sorted([x for x in lessons if score(x) >= 3], key=lambda x: -score(x))
    if len(hit) < 3:  # 完全命中的太少，再用同一大类的补几条
        hit += sorted([x for x in lessons if 0 < score(x) < 3], key=lambda x: -score(x))[:3 - len(hit)]
    general = [x for x in lessons if "全流程" in (x.get("tags") or []) and x not in hit][:3]
    return step, (hit[:max_lessons - len(general)] + general)


def prompt_block(pb, step, lessons, personal=None, mine=None):
    """mine：主人自己的习惯（个人层里和这次相关的几条），和经验包冲突时以它为准。"""
    personal, mine = personal or {}, mine or []
    if not step and not lessons and not mine:
        return ""
    m = pb["manifest"]
    parts = [f"## 项目经验包「{m.get('name', '')}」v{m.get('version', '1')}（主人调教好的做法，照着来）"]
    if mine or (step and step["id"] in (personal.get("steps") or {})):
        lines = []
        if step and step["id"] in (personal.get("steps") or {}):
            lines.append(f"- 这一步按主人的习惯改：{personal['steps'][step['id']]['note']}")
        lines += [f"- {x['text']}" for x in mine]
        parts.append("### 主人自己的习惯（和下面经验包冲突时，以这里为准）\n" + "\n".join(lines))
    if step:
        body = step["body"]
        parts.append(f"### 你负责的这一步：{step['name']}\n{body[:3000]}" + ("…" if len(body) > 3000 else ""))
    if lessons:
        parts.append("### 相关经验\n" + "\n".join(f"- {x['text']}" for x in lessons))
    return "\n\n".join(parts)


def pick_personal(personal, step, tag, task, n=4):
    want = ([tag] if tag else guess_tags(task)) + (step["tags"] if step else [])
    pool = personal.get("lessons") or []
    exact = [x for x in pool if any(t in want for t in x.get("tags") or [])]
    near = [x for x in pool if x not in exact and _match([t for t in x.get("tags") or [] if t != "全流程"], want)] if len(exact) < 2 else []
    general = [x for x in pool if "全流程" in (x.get("tags") or []) and x not in exact]
    return (exact + near)[:n - min(1, len(general))] + general[:1]


def _pb_lock(bus):
    """经验包的事务锁：计数、提议、采纳、归档、导入共用（不同项目各一把）。"""
    from config import locked
    import hashlib
    return locked("playbook-" + hashlib.md5(str(bus).encode()).hexdigest()[:8])


def _atomic(f, text):
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, f)


def bump(bus, step, lessons):
    """记一笔使用次数（本机，不进包）。统计出错不影响派活。"""
    try:
        with _pb_lock(bus):
            f = pb_dir(bus) / "usage.json"
            try:
                u = json.loads(f.read_text("utf-8")) if f.exists() else {}
            except ValueError:
                u = {}
            for k in ([f"step:{step['id']}"] if step else []) + [f"lesson:{x['id']}" for x in lessons]:
                u[k] = u.get(k, 0) + 1
            u["_last"] = time.time()
            _atomic(f, json.dumps(u, ensure_ascii=False, indent=2))
    except Exception:
        pass


def summary(pb, equip, agents, who_fn):
    """给界面的：每一步需要什么能力、按现在的阵容由谁来做、缺什么能力。"""
    if not pb:
        return None
    steps = []
    for s in pb["steps"]:
        cands = []
        for t in s["tags"]:
            for r in who_fn(t):
                a = agents.get(r["id"])
                if a and not a.get("hidden") and r["id"] not in [c["id"] for c in cands]:
                    lack = [c for c in s["caps"] if c not in (a.get("caps") or [])]
                    cands.append({"id": r["id"], "name": a["name"], "level": r["level"], "lack": lack})
        cands.sort(key=lambda c: (bool(c["lack"]), c["level"] != "prefer"))
        steps.append({k: s[k] for k in ("id", "name", "description", "step", "tags", "caps", "scope", "after")}
                     | {"on": s["id"] not in equip["steps_off"], "uses": pb["usage"].get(f"step:{s['id']}", 0),
                        "who": cands[:3], "caps_names": [CAPS.get(c, c) for c in s["caps"]]})
    lessons = [dict(x, on=x["id"] not in equip["lessons_off"], uses=pb["usage"].get(f"lesson:{x['id']}", 0)) for x in pb["lessons"]]
    return {"manifest": pb["manifest"], "enabled": equip["enabled"], "steps": steps, "lessons": lessons, "workflow": pb["workflow"]}


# ---------- 融入个人习惯：主位分析 → 主人确认 → 个人层 ----------
# 个人层 playbook/personal.json（本机，不进包）：
#   {"steps": {步骤id: {"decision": "use|adapt|skip", "note": 按主人习惯怎么改}}, "lessons": [{"id": "P001", "text", "tags", "reason"}], "applied": 日期}
# 主位的建议 playbook/proposal.json（同样本机），格式见 FIT_SCHEMA。

FIT_SCHEMA = """{
  "summary": "一两句话：这个经验包和主人习惯的契合度、最主要的差别",
  "steps": [{"id": "步骤id", "decision": "use|adapt|skip", "reason": "为什么", "note": "decision=adapt 时：按主人习惯这一步怎么改（写成给干活 agent 的一两句指令）"}],
  "lessons": [{"id": "经验id", "decision": "use|skip", "reason": "为什么（skip 时必须说清和主人哪条习惯冲突或用不上）"}],
  "conflicts": [{"pack": "经验包里的说法", "habit": "主人的习惯", "suggest": "建议听谁的、怎么改"}],
  "personal": [{"text": "从主人习惯里提炼出来、经验包里没有的一条规矩", "tags": ["职能词条"], "reason": "出处"}],
  "assign": [{"step": "步骤id", "agent": "agent id", "reason": "按主人的习惯和历史派活记录，这一步交给谁"}]
}"""


def personal_of(bus):
    f = pb_dir(bus) / "personal.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else {"steps": {}, "lessons": []}


def memory_dir(root):
    """Claude 给这个项目记的记忆笔记目录（里面基本都是主人定下的规矩）。"""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    d = Path.home() / ".claude" / "projects" / slug / "memory"
    return d if d.exists() else None


def habit_sources(root):
    root = Path(root)
    out = [p for p in (Path.home() / ".codex/AGENTS.md", Path.home() / ".claude/CLAUDE.md", root / "CLAUDE.md", root / "AGENTS.md") if p.exists()]
    md = memory_dir(root)
    if md:
        out += sorted(p for p in md.glob("*.md") if p.name != "MEMORY.md")
    return [str(p) for p in out]


def fit_context(pb, root, bus, cfg, agents, ledger):
    """给主位的分析材料：经验包要点 + 主人习惯的出处 + agentbus 里记下的派活习惯。"""
    lines = [f"# 经验包「{pb['manifest'].get('name')}」v{pb['manifest'].get('version')}", "", "## 步骤"]
    for s in pb["steps"]:
        lines.append(f"- {s['id']}（第 {s['step']} 步 {s['name']}）：{s['description']}｜岗位 {'、'.join(s['tags'])}｜需要 {', '.join(s['caps'])}")
    lines += ["", "## 经验"] + [f"- {x['id']}（{x.get('scope')}，{'、'.join(x.get('tags') or [])}）：{x['text']}" for x in pb["lessons"]]
    lines += ["", "## 主人的习惯从哪里看（请逐个读）"] + [f"- {p}" for p in habit_sources(root)]
    lines += ["", "## 主人现在的阵容和职能偏好"]
    for aid in cfg["roster"]["office"]:
        a = agents.get(aid)
        if a:
            jobs = "、".join(f"{k[4:]}{'(整类)' if k.startswith('cat:') else ''}={v}" for k, v in (cfg["jobs"].get(aid) or {}).items())
            lines.append(f"- {aid}（{a['name']}，能力 {', '.join(a.get('caps') or [])}）：{jobs or '没配职能'}")
    stat = {}
    for r in ledger:
        if r.get("type") or not r.get("tag"):
            continue
        stat.setdefault(r["tag"], {}).setdefault(r["agent"], 0)
        stat[r["tag"]][r["agent"]] += 1
    if stat:
        lines += ["", "## 以往派活记录（岗位：agent×次数）"] + [f"- {t}：{'、'.join(f'{a}×{n}' for a, n in v.items())}" for t, v in stat.items()]
    return "\n".join(lines)


def apply_fit(bus, cfg, name, choices):
    """主人确认后生效：卸下 skip 的步骤和经验，adapt 的改法和新增的个人习惯写进个人层，交给谁写进职能偏好。"""
    import time as _t
    lu = cfg.setdefault("lineups", {}).setdefault(name, {"roster": cfg["roster"], "jobs": cfg["jobs"]})
    eq = lu.setdefault("playbook", {})
    eq["enabled"] = True
    eq["steps_off"] = sorted(k for k, v in (choices.get("steps") or {}).items() if v.get("decision") == "skip")
    eq["lessons_off"] = sorted(k for k, v in (choices.get("lessons") or {}).items() if v is False)
    personal = {"steps": {k: v for k, v in (choices.get("steps") or {}).items() if v.get("decision") == "adapt" and v.get("note")},
                "lessons": [dict(x, id=f"P{i:03d}") for i, x in enumerate(choices.get("personal") or [], 1)],
                "applied": _t.strftime("%Y-%m-%d %H:%M")}
    (pb_dir(bus) / "personal.json").write_text(json.dumps(personal, ensure_ascii=False, indent=2), "utf-8")
    for a in choices.get("assign") or []:  # 这一步交给谁 → 这一步的岗位设为他的主力
        steps = {s["id"]: s for s in load(bus)["steps"]}
        st = steps.get(a.get("step"))
        if st and a.get("agent"):
            jobs = lu["jobs"].setdefault(a["agent"], {})
            for t in st["tags"]:
                jobs["tag:" + t] = "prefer"
    p = pb_dir(bus) / "proposal.json"
    if p.exists():
        p.rename(p.with_name("proposal.applied.json"))
    return personal


# ---------- 第二期：导出（脱敏）/ 导入 / 经验提议-采纳-归档 ----------
PACK_FILES = ("manifest.json", "workflow.md", "lessons.jsonl")  # 再加 steps/*/SKILL.md；个人层、用量、建议都不导出
EXPORTS = Path(os.environ.get("AGENTBUS_HOME") or os.path.expanduser("~/.agentbus")) / "exports"


def _sanitize(text, root, names):
    """去掉只属于这台电脑、这个人的东西，返回 (新文本, {类别: 次数})。"""
    import re as _re
    hits = {}

    def sub(pat, rep, label, t, flags=0):
        n = len(_re.findall(pat, t, flags))
        if n:
            hits[label] = hits.get(label, 0) + n
        return _re.sub(pat, rep, t, flags=flags)
    if root:
        text = sub(_re.escape(str(root).rstrip("/")), "{项目根目录}", "项目路径", text)
    text = sub(_re.escape(os.path.expanduser("~")), "~", "用户目录", text)
    text = sub(r"/Volumes/[^/\s`'\"）)]+", "{磁盘}", "磁盘名", text)
    text = sub(r"\b(sk|ak|pk)-[A-Za-z0-9_\-]{16,}", "{密钥已去掉}", "密钥", text)
    text = sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "{邮箱已去掉}", "邮箱", text)
    text = sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "{手机号已去掉}", "手机号", text)
    text = sub(r"https?://[\w.-]*(feishu\.cn|larksuite\.com|larkoffice\.com)/[^\s)）\"'`]+", "{飞书链接已去掉}", "飞书链接", text)
    for n in names:
        if n:
            text = sub(_re.escape(n), "主人", "主人名字", text)
    return text, hits


def export_pack(bus, root, hide_names=True):
    """打成 <名字>-v<版本>.agentpack.zip，返回 (路径, 脱敏报告)。"""
    import zipfile
    d = pb_dir(bus)
    man = json.loads((d / "manifest.json").read_text("utf-8"))
    names = [man.get("author", "")] if hide_names else []
    files = [d / f for f in PACK_FILES if (d / f).exists()] + sorted((d / "steps").glob("*/SKILL.md"))
    report = {}
    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = EXPORTS / f"{re.sub(r'[^0-9A-Za-z一-龥_-]+', '-', man.get('name', 'pack'))}-v{man.get('version', '1')}.agentpack.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            t, hits = _sanitize(f.read_text("utf-8"), root, names)
            for k, v in hits.items():
                report[k] = report.get(k, 0) + v
            rel = f.relative_to(d).as_posix()
            if rel == "manifest.json":
                m = json.loads(t)
                m["exported"] = time.strftime("%Y-%m-%d")
                m.pop("source", None)  # 原始文档在主人电脑上，别人拿不到
                t = json.dumps(m, ensure_ascii=False, indent=2)
            z.writestr(rel, t)
    return out, report


def import_pack(bus, root, data):
    """导入 .agentpack.zip：先在旁边的临时目录里解开、把每个文件都校验一遍（CRC、UTF-8、manifest、数量和总大小），
    全部通过才把原来的经验包改名备份（不删）、换上新的；任何一步不对，原来的经验包原样不动。{项目根目录} 换成这个项目的路径。"""
    import io
    import shutil
    import zipfile
    d = pb_dir(bus)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("文件坏了，不是有效的经验包（zip）")
    names = z.namelist()
    if "manifest.json" not in names:
        raise ValueError("这不是经验包（缺 manifest.json）")
    ok = [n for n in names if n in PACK_FILES or re.fullmatch(r"steps/[\w.-]+/SKILL\.md", n)]
    if len(ok) > 500 or sum(z.getinfo(n).file_size for n in ok) > 20 * 1024 * 1024:
        raise ValueError("经验包太大（文件数或解压后大小超出上限）")
    files = {}
    for n in ok:
        try:
            files[n] = z.read(n).decode("utf-8").replace("{项目根目录}", str(root))
        except zipfile.BadZipFile:
            raise ValueError(f"经验包里的 {n} 损坏了")
        except UnicodeDecodeError:
            raise ValueError(f"经验包里的 {n} 不是 UTF-8 文本")
    try:
        man = json.loads(files["manifest.json"])
        if not isinstance(man, dict):
            raise ValueError
    except ValueError:
        raise ValueError("经验包的 manifest.json 格式不对")
    man["imported"] = time.strftime("%Y-%m-%d")
    files["manifest.json"] = json.dumps(man, ensure_ascii=False, indent=2)
    with _pb_lock(bus):
        stage = d.with_name("playbook.importing")
        shutil.rmtree(stage, ignore_errors=True)
        try:
            for n, t in files.items():
                (stage / n).parent.mkdir(parents=True, exist_ok=True)
                (stage / n).write_text(t, "utf-8")
        except OSError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        backup = None
        if d.exists():
            backup, k = d.with_name("playbook.bak-" + time.strftime("%m%d-%H%M%S")), 1
            while backup.exists():
                k += 1
                backup = d.with_name("playbook.bak-" + time.strftime("%m%d-%H%M%S") + f"-{k}")
            d.rename(backup)
        stage.rename(d)
    return {"name": man.get("name"), "steps": len([n for n in ok if n.startswith("steps/")]), "backup": str(backup) if backup else None}


def proposals(bus):
    f = pb_dir(bus) / "lessons.proposed.jsonl"
    return [json.loads(l) for l in f.read_text("utf-8").splitlines() if l.strip()] if f.exists() else []


def propose(bus, text, tags, scope, frm, run=""):
    d = pb_dir(bus)
    if not d.exists():
        raise ValueError("这个项目还没有经验包")
    import uuid
    rec = {"pid": "P" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4], "text": text.strip(), "tags": tags, "scope": scope or "本项目", "from": frm, "run": run,
           "date": time.strftime("%Y-%m-%d")}
    with _pb_lock(bus), open(d / "lessons.proposed.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["pid"]


def _write_jsonl(f, rows):
    _atomic(f, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def decide(bus, pid, adopt, text=None):
    """采纳 = 写进 lessons.jsonl（新编号）；不要 = 记进 lessons.declined.jsonl。都从待采纳里拿掉。
    先把结果写好、最后才撤提议：中途出错提议还在，不会两头都丢。"""
    with _pb_lock(bus):
        d = pb_dir(bus)
        ps = proposals(bus)
        p = next((x for x in ps if x["pid"] == pid), None)
        if not p:
            raise ValueError("没有这条提议")
        out = None
        if adopt:
            lf = d / "lessons.jsonl"
            rows = [json.loads(l) for l in lf.read_text("utf-8").splitlines() if l.strip()] if lf.exists() else []
            n = max([int(re.sub(r"\D", "", r["id"]) or 0) for r in rows] + [0]) + 1
            rows.append({"id": f"L{n:03d}", "text": (text or p["text"]).strip(), "tags": p["tags"], "scope": p["scope"],
                         "source": f"{p['from']} 提议，主人采纳", "date": time.strftime("%Y-%m-%d")})
            _write_jsonl(lf, rows)
            out = rows[-1]["id"]
        else:
            with open(d / "lessons.declined.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(p, declined=time.strftime("%Y-%m-%d")), ensure_ascii=False) + "\n")
        _write_jsonl(d / "lessons.proposed.jsonl", [x for x in proposals(bus) if x["pid"] != pid])
        return out


def archive(bus, lid):
    """归档一条经验（不删）：从 lessons.jsonl 挪到 lessons.archived.jsonl，以后不再派活注入。先写归档、再从正式经验里拿掉。"""
    with _pb_lock(bus):
        d = pb_dir(bus)
        lf = d / "lessons.jsonl"
        rows = [json.loads(l) for l in lf.read_text("utf-8").splitlines() if l.strip()]
        hit = [r for r in rows if r["id"] == lid]
        if not hit:
            raise ValueError("没有这条经验")
        with open(d / "lessons.archived.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(hit[0], archived=time.strftime("%Y-%m-%d")), ensure_ascii=False) + "\n")
        _write_jsonl(lf, [r for r in rows if r["id"] != lid])


def merge_context(pb, root, bus):
    """导入别人的经验包后，交给主位「融合进本地文档」的材料：经验包全文 + 本地工作流和复盘文档的位置。"""
    m = pb["manifest"]
    d = pb_dir(bus)
    lines = [f"# 要融合的经验包「{m.get('name')}」v{m.get('version')}（作者：{m.get('author', '未知')}，导入于 {m.get('imported', '')}）", "",
             f"经验包文件在 `{d}`：manifest.json、workflow.md、steps/*/SKILL.md、lessons.jsonl。请逐个读。", "",
             "## 本地的工作流和经验（以这些为准，请逐个读）"]
    lines += [f"- {p}" for p in habit_sources(root)]
    lines += ["", "## 怎么融合",
              "1. 逐条对比：经验包里的每一步做法、每一条经验，本地文档里有没有、说法是否一致。",
              "2. 分三类列给主人看：**本地没有、值得加的**（写清加到哪个文件的哪一节、原文怎么写）；**和本地冲突的**（两边各怎么说，默认以本地为准，问主人要不要改）；**本地已经有的**（一句话带过）。",
              "3. 主人确认哪几条要加以后，才去改本地文件；改的时候沿用本地文档原来的写法和结构，不另开新文件。",
              "4. 不要打开 agentbus 的「派活时带经验包」，队员照本地文档干活。",
              "5. 改完告诉主人改了哪些文件、各加了几条。"]
    return "\n".join(lines)
