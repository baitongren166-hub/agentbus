"""钱包：API 账户。

两种来源：
  - 关联（linked）：本机别的工具里已经有的 key（opencode、zcode、环境变量）。钱包只记“在哪”，用的时候现读，不复制、不打印。
  - 自己的（keychain）：主人在 agentbus 里填的 key，存进 macOS 钥匙串（服务名 agentbus-wallet），配置文件里没有明文。
钱包文件 ~/.agentbus/wallet.json 只放账户名、供应商、地址、来源和并发上限。
"""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pf
from config import HOME, load_json, locked, save_json

H = os.path.expanduser
WALLET = HOME / "wallet.json"
KC_SERVICE = "agentbus-wallet"

# 常见供应商：地址、查余额方式、品牌色。都走兼容 OpenAI 的 /chat/completions
PRESETS = {
    "deepseek": {"name": "DeepSeek", "base": "https://api.deepseek.com", "balance": "deepseek", "color": "#4d6bfe"},
    "zhipu": {"name": "智谱 GLM", "base": "https://open.bigmodel.cn/api/paas/v4", "color": "#3a5cf5"},
    "moonshot": {"name": "Kimi", "base": "https://api.moonshot.cn/v1", "balance": "moonshot", "color": "#16191e"},
    "qwen": {"name": "通义千问", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "color": "#615ced"},
    "siliconflow": {"name": "硅基流动", "base": "https://api.siliconflow.cn/v1", "color": "#7c3aed"},
    "openrouter": {"name": "OpenRouter", "base": "https://openrouter.ai/api/v1", "color": "#6467f2"},
    "openai": {"name": "OpenAI", "base": "https://api.openai.com/v1", "color": "#10a37f"},
    "xai": {"name": "Grok", "base": "https://api.x.ai/v1", "color": "#111111"},
    "opencode-go": {"name": "OpenCode Go", "base": "https://opencode.ai/zen/go/v1", "color": "#2b2b2b"},
    "custom": {"name": "自定义（兼容 OpenAI）", "base": "", "color": "#888888"},
}
ENV_KEYS = {"DEEPSEEK_API_KEY": "deepseek", "ZHIPUAI_API_KEY": "zhipu", "MOONSHOT_API_KEY": "moonshot", "DASHSCOPE_API_KEY": "qwen",
            "SILICONFLOW_API_KEY": "siliconflow", "OPENROUTER_API_KEY": "openrouter", "OPENAI_API_KEY": "openai", "XAI_API_KEY": "xai"}


def guess_provider(base):
    host = re.sub(r"^https?://", "", base or "").split("/")[0]
    for pid, p in PRESETS.items():
        if p["base"] and host and host == re.sub(r"^https?://", "", p["base"]).split("/")[0]:
            return pid
    return "custom"


def _fp(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _jsonc(path):
    t = Path(path).read_text("utf-8")
    t = re.sub(r'("(?:[^"\\]|\\.)*")|//[^\n]*', lambda m: m.group(1) or "", t)  # 去掉注释，保留字符串里的 //
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", t))


# ---------- 读 key ----------
def _read_source(src):
    k = src.get("kind")
    try:
        if k == "keychain":
            return pf.secret_get(src["account"])
        if k == "opencode-auth":
            return json.loads(Path(H("~/.local/share/opencode/auth.json")).read_text())[src["name"]].get("key")
        if k == "opencode-config":
            return ((_jsonc(H("~/.config/opencode/opencode.jsonc"))["provider"][src["name"]].get("options") or {}).get("apiKey"))
        if k == "zcode":
            for r in json.loads(Path(H("~/.zcode/v2/provider_config.json")).read_text())["config"]["providerConfigRules"]["providerRules"]:
                if (r.get("providerId") or r.get("id")) == src["name"]:
                    return ((r.get("config") or {}).get("access") or {}).get("apiKey")
        if k == "env":
            return os.environ.get(src["var"])
    except Exception:
        return None
    return None


SOURCE_NAME = {"keychain": pf.KEYSTORE, "opencode-auth": "opencode", "opencode-config": "opencode", "zcode": "zcode", "env": "环境变量"}


def discover():
    """本机已经有的 key（只返回位置和指纹，不返回 key 本身）。"""
    out = []

    def add(src, base, name, models=None):
        key = _read_source(src)
        if isinstance(key, str) and len(key) > 8 and not key.startswith("{env:"):
            pid = guess_provider(base)
            out.append({"fp": _fp(key), "source": src, "base": base or PRESETS[pid]["base"], "provider": pid,
                        "name": name, "from": SOURCE_NAME[src["kind"]], "models": models or []})
    cfg = {}
    try:
        cfg = _jsonc(H("~/.config/opencode/opencode.jsonc")).get("provider") or {}
    except Exception:
        pass
    try:
        for n in json.loads(Path(H("~/.local/share/opencode/auth.json")).read_text()):
            base = ((cfg.get(n) or {}).get("options") or {}).get("baseURL") or (PRESETS.get(n) or {}).get("base") or ""
            if not base and "deepseek" in n:
                base = PRESETS["deepseek"]["base"]
            add({"kind": "opencode-auth", "name": n}, base, (cfg.get(n) or {}).get("name") or PRESETS.get(n, {}).get("name") or n,
                list(((cfg.get(n) or {}).get("models") or {}).keys()))
    except Exception:
        pass
    for n, v in cfg.items():
        if (v.get("options") or {}).get("apiKey"):
            add({"kind": "opencode-config", "name": n}, (v.get("options") or {}).get("baseURL"), v.get("name") or n, list((v.get("models") or {}).keys()))
    try:
        for r in json.loads(Path(H("~/.zcode/v2/provider_config.json")).read_text())["config"]["providerConfigRules"]["providerRules"]:
            c = r.get("config") or {}
            pid = r.get("providerId") or r.get("id")
            base = (c.get("api") or {}).get("baseUrl") or (PRESETS.get(pid) or {}).get("base") or ""
            add({"kind": "zcode", "name": pid}, base, f"zcode · {PRESETS.get(guess_provider(base), {}).get('name', pid)}", c.get("modelOrder") or [])
    except Exception:
        pass
    for var, pid in ENV_KEYS.items():
        if os.environ.get(var):
            add({"kind": "env", "var": var}, os.environ.get(var.replace("API_KEY", "BASE_URL")) or PRESETS[pid]["base"], f"{PRESETS[pid]['name']}（环境变量）")
    # 同一个 key 在好几个地方：只留一条，记下都在哪
    seen = {}
    for d in out:
        if d["fp"] in seen:
            seen[d["fp"]]["also"] = seen[d["fp"]].get("also", []) + [d["from"]]
        else:
            seen[d["fp"]] = d
    added = {a.get("fp") for a in accounts().values()}
    return [dict(d, added=d["fp"] in added) for d in seen.values()]


# ---------- 账户 ----------
def accounts():
    return load_json(WALLET, {}).get("accounts") or {}


def _txn(fn):
    """改账户的函数整段在钱包锁里跑：读→分配编号→写回是一个事务，并发添加不会撞号、不会覆盖别人刚加的账户。"""
    import functools

    @functools.wraps(fn)
    def run(*a, **kw):
        with locked("wallet"):
            return fn(*a, **kw)
    return run


def _save(accs):
    w = load_json(WALLET, {})
    w["accounts"] = accs
    save_json(WALLET, w)


def _new_id(provider):
    accs = accounts()
    n = 1
    while f"{provider}-{n}" in accs:
        n += 1
    return f"{provider}-{n}"


def _origin(url):
    from urllib.parse import urlparse
    u = urlparse((url or "").strip())
    return (u.scheme.lower(), (u.hostname or "").lower(), u.port)


@_txn
def link(fp, base=None, models=None):
    """把发现的 key 关联进钱包（不复制 key）。"""
    d = next((x for x in discover() if x["fp"] == fp), None)
    if not d:
        raise ValueError("没找到这个 key")
    # 发现的 key 只能发往它原来配置的那家：换成别的域名等于把主人的 key 交给别人（要用别的中转，就在钱包里“添加”一个新账户、自己填 key）
    if base and _origin(base) != _origin(d["base"]):
        raise ValueError("关联发现的 key 不能改成别的地址")
    accs = accounts()
    aid = _new_id(d["provider"])
    accs[aid] = {"name": d["name"], "provider": d["provider"], "base": (base or d["base"]).rstrip("/"), "source": d["source"], "fp": fp,
                 "models": models or d["models"], "models_t": time.time() if models else 0, "limit": 4, "added": time.strftime("%Y-%m-%d")}
    _save(accs)
    return aid


@_txn
def add(provider, key, base=None, name=None, models=None):
    """主人自己填的 key：存进钥匙串。"""
    p = PRESETS.get(provider) or PRESETS["custom"]
    base = (base or p["base"] or "").rstrip("/")
    if not key or not base:
        raise ValueError("要填 key 和地址")
    accs = accounts()
    aid = _new_id(provider if provider in PRESETS else "custom")
    pf.secret_set(aid, key)
    accs[aid] = {"name": name or p["name"], "provider": provider, "base": base, "source": {"kind": "keychain", "account": aid},
                 "fp": _fp(key), "models": models or [], "models_t": time.time() if models else 0, "limit": 4, "added": time.strftime("%Y-%m-%d")}
    _save(accs)
    return aid


@_txn
def remove(aid):
    accs = accounts()
    a = accs.pop(aid, None)
    if not a:
        raise ValueError("没有这个账户")
    if a["source"].get("kind") == "keychain":
        pf.secret_delete(aid)
    _save(accs)


@_txn
def update(aid, **kw):
    accs = accounts()
    if aid not in accs:
        raise ValueError("没有这个账户")
    for k in ("name", "limit"):
        if k in kw and kw[k] not in (None, ""):
            accs[aid][k] = int(kw[k]) if k == "limit" else str(kw[k])
    _save(accs)


def key_of(aid):
    a = accounts().get(aid)
    return _read_source(a["source"]) if a else None


def _get(url, key, timeout=15):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key, "User-Agent": "agentbus"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def models(aid, fresh=False):
    """账户能用的模型：问供应商的 /models，一天一更；问不到就用发现时记下的。"""
    accs = accounts()
    a = accs.get(aid)
    if not a:
        return []
    if not fresh and a.get("models") and time.time() - a.get("models_t", 0) < 86400:
        return a["models"]
    key = key_of(aid)
    if key:
        try:
            ids = sorted({m["id"] for m in _get(a["base"] + "/models", key).get("data") or [] if m.get("id")})
            if ids:  # 网络查询在锁外；写回时重新读一遍，只改这一个账户的模型列表
                a["models"], a["models_t"] = ids, time.time()
                with locked("wallet"):
                    cur = accounts()
                    if aid in cur:
                        cur[aid].update(models=ids, models_t=a["models_t"])
                        _save(cur)
        except Exception:
            pass
    return a.get("models") or []


def balance(aid):
    """查余额（支持的供应商才有）：{"ok", "cny"/"usd", "text"}。"""
    a = accounts().get(aid) or {}
    kind = (PRESETS.get(a.get("provider")) or {}).get("balance")
    key = key_of(aid) if kind else None
    if not key:
        return {"ok": False}
    try:
        if kind == "deepseek":
            d = _get("https://api.deepseek.com/user/balance", key)
            b = next((float(x["total_balance"]) for x in d.get("balance_infos") or [] if x.get("currency") == "CNY"), None)
            return {"ok": b is not None, "cny": b, "text": f"¥{b:.2f}" if b is not None else ""}
        if kind == "moonshot":
            d = _get(a["base"] + "/users/me/balance", key)
            b = float((d.get("data") or {}).get("available_balance"))
            return {"ok": True, "cny": b, "text": f"¥{b:.2f}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": False}


def public():
    """给界面看的账户列表（没有 key）。"""
    out = []
    for aid, a in accounts().items():
        p = PRESETS.get(a.get("provider")) or PRESETS["custom"]
        out.append({"id": aid, "name": a["name"], "provider": a.get("provider"), "provider_name": p["name"], "color": p["color"],
                    "base": a["base"], "from": SOURCE_NAME.get(a["source"].get("kind"), ""), "limit": a.get("limit", 4),
                    "models": len(a.get("models") or []), "ok": bool(key_of(aid)), "can_balance": bool(p.get("balance"))})
    return out


def test(base, key):
    """不保存，先试试能不能连上：问一下 /models。中转常常漏写 /v1，两种都试。"""
    base = (base or "").strip().rstrip("/")
    if not base.startswith("http"):
        return {"ok": False, "error": "地址要以 http:// 或 https:// 开头"}
    if not key:
        return {"ok": False, "error": "还没填 key"}
    cands = [base] + ([base + "/v1"] if not base.endswith("/v1") else [])
    last = ""
    for b in cands:
        try:
            ids = sorted({m["id"] for m in _get(b + "/models", key, timeout=12).get("data") or [] if m.get("id")})
            return {"ok": True, "base": b, "models": ids}
        except urllib.error.HTTPError as e:
            last = "key 不对或没有权限（401/403）" if e.code in (401, 403) else f"HTTP {e.code}"
            if e.code in (401, 403):
                break
        except Exception as e:
            last = "连不上：" + str(e)[:80]
    return {"ok": False, "error": last or "连不上"}


def test_found(fp):
    d = next((x for x in discover() if x["fp"] == fp), None)
    if not d:
        return {"ok": False, "error": "没找到这个 key"}
    r = test(d["base"], _read_source(d["source"]))
    if not r["ok"] and d["models"]:  # 有的中转不给列模型，退回配置里写的
        return {"ok": True, "base": d["base"], "models": d["models"], "note": "这个地址不给列模型，用配置里写的"}
    return r
