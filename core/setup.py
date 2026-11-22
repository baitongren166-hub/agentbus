"""首次安装的自动评估和装配：看这台电脑上装了哪些 agent、有哪些模型，给每位配好职能、选好主位、排好上阵，
用户点一下「就这样」就能开工。

评估用的是内置的经验表（不联网也能用）：每个 agent 的能力（catalog.CAPS）+ 下面的「擅长」表 + 本机识别到的模型。
以后有了排行榜数据，可以换成按测评分来排。
"""
import re

import brains
from catalog import CATEGORIES, CAPS
from config import agents_dict, installed, load_config, save_config, sealed

# 每个职能大类最看重的能力（越靠前越重要）
CAT_NEEDS = {
    "coord": ["long", "files", "cn"], "writing": ["cn", "long"], "editing": ["cn", "long"], "lang": ["cn", "long"],
    "visual": ["image", "vision"], "av": ["computer", "files", "vision"], "computer": ["computer", "browser"],
    "research": ["web", "long"], "assets": ["web", "image", "vision"], "dev": ["code", "files"],
    "office": ["files", "cheap"], "ops": ["cn", "web"], "edu": ["cn", "long"], "ideation": ["cn", "long"],
}
# 各家 agent 公认的强项（在能力之外加分），以及适不适合当主位（能长时间统筹、有桌面端或命令行可以直接对话）
STRENGTH = {
    "claude": {"coord": 3, "writing": 2, "editing": 2, "dev": 2, "research": 1, "ideation": 1},
    "codex": {"visual": 3, "computer": 2, "assets": 2, "dev": 2, "av": 1},
    "opencode": {"computer": 1, "dev": 1, "office": 1},
    "zcode": {"computer": 2, "av": 2, "dev": 1},
    "gemini": {"writing": 2, "research": 2, "lang": 1, "ideation": 1},
    "dsh": {"dev": 2, "office": 1},
    "codebuddy": {"office": 2, "dev": 1, "writing": 1},
    "doubao": {"writing": 1, "ops": 2, "research": 1},
    "qwen": {"dev": 2, "lang": 1, "writing": 1},
    "kimi": {"research": 2, "editing": 1, "office": 1},
    "cursor": {"dev": 3},
}
LEAD_ORDER = ["codex", "gemini", "kimi", "codebuddy", "opencode", "qwen", "cursor", "zcode", "dsh"]

# 模型擅长什么（按名字认，没认出来的不贴标签）
MODEL_TAGS = [
    (r"opus|fable", ["推理强", "写作好", "适合统筹"]),
    (r"sonnet", ["写代码", "日常主力"]),
    (r"haiku", ["快", "便宜"]),
    (r"gpt-?6(\.\d)?-?sol|gpt-?5\.\d-?sol", ["全能", "推理强"]),
    (r"luna", ["快", "省额度"]),
    (r"terra|astra", ["均衡"]),
    (r"deepseek.*(pro|reason|r\d)", ["推理强", "写代码"]),
    (r"deepseek(?!.*(pro|reason|r\d))", ["便宜", "中文好"]),
    (r"glm", ["中文好", "写代码"]),
    (r"kimi|moonshot", ["长文档"]),
    (r"qwen", ["中文好", "写代码"]),
    (r"gemini", ["看图", "长上下文"]),
    (r"grok", ["实时信息"]),
    (r"minimax", ["长文本", "多模态"]),
    (r"mimo", ["推理"]),
    (r"flash|lite|mini(?!max)", ["快", "便宜"]),
]


def model_tags(mid):
    tags = []
    for pat, ts in MODEL_TAGS:
        if re.search(pat, mid, re.I):
            tags += [t for t in ts if t not in tags]
    return tags[:3]


def evaluate():
    """返回建议：主位、上阵、每位的主力职能和理由、每位能用的模型和擅长什么。"""
    cfg = load_config()
    ags = agents_dict(cfg)
    found = [(aid, a) for aid, a in ags.items()
             if not a.get("hidden") and not a.get("clone_of") and not sealed(a) and installed(a) and a.get("run")]
    usable = [(aid, a) for aid, a in found if a.get("billing") != "manual"]  # 网页版要人传话，不自动排进阵容
    cat_name = {c[0]: c[1] for c in CATEGORIES}

    def score(aid, a, cat):
        caps = a.get("caps") or []
        s = sum((3 - i) for i, c in enumerate(CAT_NEEDS.get(cat, [])) if c in caps)
        return s + (STRENGTH.get(aid) or {}).get(cat, 0)

    ids = [aid for aid, _ in usable]
    lead = next((x for x in LEAD_ORDER if x in ids), ids[0] if ids else None)
    jobs, load = {aid: {} for aid in ids}, {aid: 0 for aid in ids}
    if lead:
        jobs[lead]["cat:coord"], load[lead] = "prefer", 1
    # 每个大类给最合适的人当主力；已经扛了好几类的人扣点分，免得全压在一个人身上
    for cat in CAT_NEEDS:
        if cat == "coord" and lead:
            continue
        raw = {aid: score(aid, a, cat) for aid, a in usable}
        ranked = sorted((aid for aid in ids if raw[aid] >= 3), key=lambda x: raw[x] - 1.5 * load[x], reverse=True)
        if not ranked:
            continue
        jobs[ranked[0]]["cat:" + cat] = "prefer"
        load[ranked[0]] += 1
        if len(ranked) > 1:
            jobs[ranked[1]].setdefault("cat:" + cat, "ok")
    # 每个能用的人至少有一类主力：挑它分最高的那类
    for aid, a in usable:
        if not any(v == "prefer" for v in jobs[aid].values()):
            cat = max(CAT_NEEDS, key=lambda c: score(aid, a, c))
            if score(aid, a, cat) >= 3:
                jobs[aid]["cat:" + cat] = "prefer"
    # 上阵：主位 + 主力职能最多的，最多 6 位；其余进休息室
    by_load = sorted(ids, key=lambda x: (x != lead, -sum(1 for v in jobs[x].values() if v == "prefer")))
    office = by_load[:6]
    agents = []
    for aid, a in found:
        caps = [CAPS[c] for c in (a.get("caps") or []) if c in CAPS]
        main = [cat_name[k[4:]] for k, v in jobs.get(aid, {}).items() if v == "prefer"]
        opts = brains.options(a) if a.get("billing") != "manual" else {"models": []}
        models = [{"id": m["id"], "name": m.get("name") or m["id"], "tags": model_tags(m["id"])} for m in (opts.get("models") or [])][:8]
        agents.append({"id": aid, "name": a["name"], "caps": caps, "main": main, "lead": aid == lead, "office": aid in office,
                       "web": a.get("billing") == "manual", "api": bool(a.get("api")),
                       "why": ("、".join(caps[:3]) + (" → " + "、".join(main[:3]) if main else "")) if caps else "",
                       "models": models})
    try:
        import wallet
        keys = len([d for d in wallet.discover() if not d["added"]])
    except Exception:
        keys = 0
    missing = [a["name"] for aid, a in ags.items() if not a.get("hidden") and not a.get("clone_of") and not a.get("api")
               and not installed(a) and a.get("run")]
    return {"lead": lead, "office": office, "jobs": jobs, "agents": agents, "found": len(found), "keys": keys, "missing": missing}


def apply(plan):
    """照建议装配：主位、上阵、职能（只动自动装配涉及的队员，别的不碰）。"""
    cfg = load_config()
    ags = agents_dict(cfg)
    office = [x for x in plan.get("office") or [] if x in ags][:6]
    lounge = [x for x in ags if x not in office]
    cfg["roster"]["office"], cfg["roster"]["lounge"] = office, lounge
    if plan.get("lead") in ags:
        cfg["roster"]["lead"] = plan["lead"]
    for aid, js in (plan.get("jobs") or {}).items():
        if aid in ags:
            cfg["jobs"][aid] = dict(js)
    cfg.setdefault("settings", {})["setup_done"] = True
    save_config(cfg)
