"""读真实额度：GPT 走 opencodex 自带的额度接口，DeepSeek 直接查账户余额。

只在本机读配置里的 key，不打印、不外传；返回结果里只有 key 的指纹（前 8 位哈希）。
"""
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

H = os.path.expanduser
OCX = "http://127.0.0.1:10100"


def _fp(k):
    return hashlib.sha256(k.encode()).hexdigest()[:8]


def _get(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def codex(refresh=False):
    """GPT（Codex 登录）额度：5 小时窗口、周额度的已用百分比，credits 余额。"""
    out = {"ok": False, "source": "opencodex"}
    try:
        token = Path(H("~/.opencodex/admin-api-token")).read_text().strip()
        auth = {"Authorization": "Bearer " + token}
        rep = _get(OCX + "/api/provider-quotas" + ("?refresh=1" if refresh else ""), auth, timeout=20 if refresh else 8)
        for r in rep.get("reports") or []:
            if r.get("provider") == "openai":
                cur = (r.get("aggregation") or {}).get("currentAccount") or {}
                q = cur.get("quota") or r.get("quota") or {}
                out.update(ok=True, five_hour=q.get("fiveHourPercent"), weekly=q.get("weeklyPercent"),
                           five_hour_reset=q.get("fiveHourResetAt"), weekly_reset=q.get("weeklyResetAt"),
                           plan=cur.get("plan"), updated=(q.get("updatedAt") or r.get("updatedAt") or 0) / 1000)
        try:
            main = (_get(OCX + "/api/codex-auth/quota", auth).get("quotas") or {}).get("__main__") or {}
            out["credits"] = (main.get("credits") or {}).get("balance")
        except Exception:
            pass
    except Exception as e:
        # 代理没开时退回读它的缓存文件
        try:
            main = json.loads(Path(H("~/.opencodex/codex-quota-cache.json")).read_text())["quotas"]["__main__"]
            out.update(ok=True, stale=True, five_hour=main.get("shortPercent"), weekly=main.get("weeklyPercent"),
                       five_hour_reset=main.get("shortResetAt"), weekly_reset=main.get("weeklyResetAt"),
                       credits=(main.get("credits") or {}).get("balance"), updated=main.get("updatedAt", 0) / 1000)
        except Exception:
            out["error"] = f"opencodex 没开，也没有缓存：{e}"
    return out


def deepseek_keys():
    """找本机各工具配置里的 DeepSeek key，返回 {指纹: {"key":…, "used_by": [...]}}。"""
    found = {}

    def add(k, who):
        if isinstance(k, str) and k.startswith("sk-"):
            found.setdefault(_fp(k), {"key": k, "used_by": []})["used_by"].append(who)

    try:  # opencode：auth.json 里名字带 deepseek 的 provider
        for name, v in json.loads(Path(H("~/.local/share/opencode/auth.json")).read_text()).items():
            if "deepseek" in name.lower():
                add(v.get("key"), "opencode")
    except Exception:
        pass
    try:  # zcode：v2/provider_config.json 里 providerId=deepseek 的规则
        pc = json.loads(Path(H("~/.zcode/v2/provider_config.json")).read_text())
        for r in pc["config"]["providerConfigRules"]["providerRules"]:
            if "deepseek" in str(r.get("providerId", "")).lower():
                add(((r.get("config") or {}).get("access") or {}).get("apiKey"), "zcode")
    except Exception:
        pass
    extra = os.environ.get("DEEPSEEK_API_KEY")
    if extra:
        add(extra, "环境变量")
    return found


_SAME = os.path.join(os.environ.get("AGENTBUS_HOME") or H("~/.agentbus"), "deepseek-accounts.json")


def _accounts(by_key):
    """{指纹: 余额} → [[指纹, ...], ...]：哪些 key 是同一个账户。
    不再按“余额差不到 1 块”猜（两个独立账户余额接近就会被合错）。认定同一账户只靠：
      1. 主人在设置里写明的对应（settings.deepseek_accounts = {指纹: 账户名}）；
      2. 某一次同时查到的余额**分毫不差**（同一账户的几个 key 共用一个余额）——见过一次就记住（deepseek-accounts.json），
         以后余额因为查询先后差几分钱，也还认得是同一个。"""
    try:
        known = json.load(open(_SAME))
    except (OSError, ValueError):
        known = {}
    try:
        from config import load_config
        named = (load_config().get("settings") or {}).get("deepseek_accounts") or {}
    except Exception:
        named = {}
    parent = {k: k for k in by_key}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def join(a, b):
        parent[root(a)] = root(b)
    keys = sorted(by_key)
    learned = False
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            pair = "|".join(sorted((a, b)))
            exact = round(by_key[a], 2) == round(by_key[b], 2) and by_key[a] > 0
            if exact and pair not in known:
                known[pair], learned = True, True
            if known.get(pair) or exact or (named.get(a) and named.get(a) == named.get(b)):
                join(a, b)
    if learned:
        try:
            json.dump(known, open(_SAME, "w"))
        except OSError:
            pass
    groups = {}
    for k in keys:
        groups.setdefault(root(k), []).append(k)
    return list(groups.values())


def deepseek():
    """DeepSeek 账户余额（人民币）。同一个账户的多个 key 余额一样，合并成一个账户，不重复算。"""
    by_key, used, errors = {}, {}, []
    for fp, info in deepseek_keys().items():
        try:
            d = _get("https://api.deepseek.com/user/balance", {"Authorization": "Bearer " + info["key"]})
            bal = next((float(b["total_balance"]) for b in d.get("balance_infos") or [] if b.get("currency") == "CNY"), None)
            if bal is not None:
                by_key[fp], used[fp] = bal, info["used_by"]
        except Exception as e:
            errors.append(f"{fp}：{e}")
    accs = [{"balance": min(by_key[k] for k in g), "keys": g, "used_by": sum((used[k] for k in g), [])} for g in _accounts(by_key)]
    return {"ok": bool(accs), "accounts": accs, "by_key": by_key, "total": round(sum(a["balance"] for a in accs), 2) if accs else None,
            "errors": errors, "updated": time.time()}


def _deepseek_cost(before, after):
    """按 key 逐个相减，再按账户合并（同一账户的几个 key 只算一次）；老记录没有 by_key 就退回总额相减。"""
    b, a = before.get("by_key") or {}, after.get("by_key") or {}
    common = {k: a[k] for k in a if k in b}
    if not common:
        return _sub(before.get("total"), after.get("total"))
    return round(sum(max(b[k] - a[k] for k in g) for g in _accounts(common)), 4)


def claude_usage():
    """Claude 订阅额度的百分比读不到（登录凭证加密），退而求其次：从本机对话记录数最近 5 小时 / 7 天用了多少 token。
    口径：tokens = 输入 + 输出 + 写缓存（真正新处理的量）；读缓存另算在 cache_read_*（命中缓存很便宜、量又大，混在一起会看不出实际消耗）。
    同一条回复（message.id）会被记好几行、用量逐步更新：按 id 只算最后一次。只读本机文件，不调接口；解封后才会用到。"""
    import glob as _g
    now, last = time.time(), {}
    for f in _g.glob(H("~/.claude/projects/*/*.jsonl")):
        try:
            if now - os.path.getmtime(f) > 7 * 86400:
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for i, l in enumerate(fh):
                    if '"usage"' not in l or '"assistant"' not in l:
                        continue
                    try:
                        d = json.loads(l)
                    except json.JSONDecodeError:
                        continue
                    m = d.get("message") or {}
                    u = m.get("usage") or {}
                    t = _ts(d.get("timestamp"))
                    if not isinstance(u, dict) or not t or now - t >= 7 * 86400:
                        continue
                    try:
                        n = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
                        cr = int(u.get("cache_read_input_tokens") or 0)
                    except (TypeError, ValueError):
                        continue
                    last[m.get("id") or f"{f}:{i}"] = (t, n, cr)
        except OSError:
            continue
    h5 = d7 = c5 = c7 = 0
    for t, n, cr in last.values():
        d7 += n
        c7 += cr
        if now - t < 5 * 3600:
            h5 += n
            c5 += cr
    return {"ok": True, "tokens_5h": h5, "tokens_7d": d7, "cache_read_5h": c5, "cache_read_7d": c7, "updated": now}


def _gemini_like(root):
    """Gemini CLI（和从它分出来的 Qwen Code）：<root>/tmp/<项目>/chats/ 下的会话文件，每条回复带 tokens.total 和 timestamp。
    新版是 .jsonl（一行一条），旧版是 .json（{messages: [...]}）。"""
    import glob as _g
    now, h5, d7 = time.time(), 0, 0
    for f in _g.glob(H(root + "/tmp/*/chats/*.json*")):
        try:
            if now - os.path.getmtime(f) > 7 * 86400:
                continue
            raw = open(f, encoding="utf-8", errors="ignore").read()
            if f.endswith(".jsonl"):
                msgs = [json.loads(l) for l in raw.splitlines() if l.strip().startswith("{")]
            else:
                d = json.loads(raw or "{}")
                msgs = d.get("messages") or [] if isinstance(d, dict) else []
        except (OSError, ValueError):
            continue
        for m in msgs:
            tk = (m or {}).get("tokens") if isinstance(m, dict) else None
            t = _ts((m or {}).get("timestamp")) if isinstance(m, dict) else None
            if not tk or not t:
                continue
            n = int(tk.get("total") or (tk.get("input") or 0) + (tk.get("output") or 0))
            if now - t < 7 * 86400:
                d7 += n
                if now - t < 5 * 3600:
                    h5 += n
    return {"ok": True, "unit": "tokens", "h5": h5, "d7": d7, "updated": now}


def workbuddy_usage():
    """WorkBuddy：~/.workbuddy/workbuddy.db 的 session_usage.credit_json 是每个对话的**累计**积分。
    累计数不能整笔算到“最后更新时间”上（老对话今天多聊一句，几天前的消耗会全算进最近 5 小时），
    所以本机存一份水位（~/.agentbus/usage-workbuddy.json）：每次读到比上次多出来的那部分，才按这次的更新时间记一笔。
    第一次见到的对话：7 天内新建的，整笔算在它的创建时间；更早的只记水位不算。剩余积分是账号层面的，本机读不到。"""
    import sqlite3
    p = H("~/.workbuddy/workbuddy.db")
    if not os.path.exists(p):
        return {"ok": False}
    now = time.time()
    wf = os.path.join(os.environ.get("AGENTBUS_HOME") or H("~/.agentbus"), "usage-workbuddy.json")
    try:
        st = json.load(open(wf))
    except (OSError, ValueError):
        st = {}
    marks, events = st.get("marks") or {}, [e for e in st.get("events") or [] if now - e[0] < 7 * 86400]
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
        created = dict(con.execute("SELECT id, created_at FROM sessions"))
        for sid, upd, cj in con.execute("SELECT session_id, updated_at, credit_json FROM session_usage"):
            try:
                n = sum(float(v) for v in (json.loads(cj or "{}") or {}).values())
            except (ValueError, TypeError, AttributeError):
                continue
            if sid not in marks:
                c = (created.get(sid) or 0) / 1000
                if c and now - c < 7 * 86400 and n > 0:
                    events.append([c, n])
            elif n > marks[sid]:
                events.append([upd / 1000, n - marks[sid]])
            marks[sid] = n
        con.close()
    except sqlite3.Error:
        return {"ok": False}
    try:
        os.makedirs(os.path.dirname(wf), exist_ok=True)
        tmp = wf + ".tmp"
        json.dump({"marks": marks, "events": events}, open(tmp, "w"))
        os.replace(tmp, wf)
    except OSError:
        pass
    h5 = sum(n for t, n in events if now - t < 5 * 3600)
    d7 = sum(n for t, n in events)
    return {"ok": True, "unit": "积分", "h5": round(h5, 2), "d7": round(d7, 2), "updated": now}


# 读不到剩余额度的队员：按身份自动找本机的用量记录（新装的 agent 有记录了就自动显示）
LOCAL_USAGE = {
    "claude": lambda: (lambda c: {**c, "unit": "tokens", "h5": c["tokens_5h"], "d7": c["tokens_7d"]})(claude_usage()),
    "qwen": lambda: _gemini_like("~/.qwen"),
    "codebuddy": workbuddy_usage,
}
def _ts(s):
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def claude():
    # 桌面端的登录信息是加密存的，第三方读不了；用户在终端跑 `claude setup-token` 后可以接（第一期未接）。
    return {"ok": False, "error": "Claude 订阅额度暂未接入：桌面端登录信息加密存储，第三方无法安全读取"}


def snapshot(billing, refresh=False):
    if billing == "codex":
        return codex(refresh)
    if billing == "deepseek":
        return deepseek()
    if billing == "claude":
        return claude()
    return {"ok": billing in ("free",), "free": billing == "free"}


def delta(billing, before, after):
    """一次派活的真实消耗。"""
    if not (before.get("ok") and after.get("ok")):
        return {"ok": False, "note": before.get("error") or after.get("error") or "没有额度数据"}
    if billing == "codex":
        d = {"ok": True, "five_hour": _sub(after.get("five_hour"), before.get("five_hour")),
             "weekly": _sub(after.get("weekly"), before.get("weekly")),
             "credits": _sub(before.get("credits"), after.get("credits"))}
        # 窗口重置后百分比会变小，算出负数就说明跨了重置
        if (d["five_hour"] or 0) < 0 or (d["weekly"] or 0) < 0:
            d["note"] = "期间额度重置过，百分比不准"
        return d
    if billing == "deepseek":
        return {"ok": True, "cny": _deepseek_cost(before, after)}
    if billing == "free":
        return {"ok": True, "cny": 0}
    return {"ok": False}


def _sub(a, b):
    try:
        return round(float(a) - float(b), 4)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    print(json.dumps({"codex": codex(), "deepseek": {k: v for k, v in deepseek().items()}}, ensure_ascii=False, indent=2))
