"""大脑：每个队员（或分身）用哪个模型、思考强度多少。

外壳（agent）决定能干什么，大脑（模型）决定干得好不好；同一个外壳可以开几个分身，各用各的大脑。
模型列表和强度档位都从本机自动识别，不写死，换一台电脑也能用：
  Codex    读 ~/.codex/models_cache.json（Codex 自己维护的模型目录，含每个模型支持的强度和默认强度）
  opencode 跑 `opencode models --standalone`（要十几秒，后台跑，结果存盘）+ 历史会话里用过的强度
  zcode    用它的协议（app-server）开个不落盘的临时会话，读模型和档位；派活时短暂改它的默认模型（见 zcode_brain）
派活时怎么带上：Codex -m + -c model_reasoning_effort=，opencode -m 供应商/模型#强度。
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

H = os.path.expanduser
CACHE = Path(H("~/.agentbus/brains.json"))
ORDER = ["disabled", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
EFFORT_NAME = {"minimal": "极简", "none": "关", "disabled": "关", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "极限", "ultra": "满血"}


def _sort(efforts):
    return sorted(set(efforts), key=lambda e: ORDER.index(e) if e in ORDER else 99)


def _codex():
    models, current = [], {}
    for f in ("~/.codex/models_cache.json", "~/.codex/opencodex-catalog.json"):
        try:
            for m in json.loads(Path(H(f)).read_text()).get("models") or []:
                mid = m.get("slug") or m.get("id")
                if mid and m.get("visibility") != "hide":
                    models.append({"id": mid, "name": m.get("display_name") or mid, "default": m.get("default_reasoning_level"),
                                   "efforts": [e["effort"] for e in m.get("supported_reasoning_levels") or []]})
            if models:
                break
        except Exception:
            pass
    try:
        t = Path(H("~/.codex/config.toml")).read_text()
        m = re.search(r'^model\s*=\s*"([^"]+)"', t, re.M)
        e = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', t, re.M)
        current = {"model": m.group(1) if m else None, "effort": e.group(1) if e else None}
    except Exception:
        pass
    return {"models": models, "current": current, "switch": True}


def _help(cmd):
    import pf

    try:
        return subprocess.run(pf.resolve(cmd), capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


def _opencode_default():
    for f in ("~/.config/opencode/opencode.jsonc", "~/.config/opencode/opencode.json"):
        try:
            m = re.search(r'^\s*"model"\s*:\s*"([^"]+)"', Path(H(f)).read_text(), re.M)
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


def _opencode():
    seen = {}
    try:  # 用过的模型和强度（opencode 自己的会话库）
        con = sqlite3.connect(f"file:{H('~/.local/share/opencode/opencode.db')}?mode=ro", uri=True, timeout=1)
        for (m,) in con.execute("select distinct model from session where model is not null order by time_updated desc limit 300"):
            try:
                d = json.loads(m)
                seen.setdefault(f"{d['providerID']}/{d['id']}", set()).update([d["variant"]] if d.get("variant") not in (None, "", "default") else [])
            except Exception:
                pass
        con.close()
    except Exception:
        pass
    out = _help(["opencode", "models", "--standalone"]) if _which("opencode") else ""
    ids = [x.strip() for x in out.splitlines() if re.match(r"^[\w.-]+/[\w.:-]+$", x.strip())]
    for k in seen:
        if k not in ids:
            ids.append(k)
    base = ["low", "medium", "high"]
    models = [{"id": i, "name": i.split("/", 1)[-1], "efforts": _sort(base + list(seen.get(i, ())))} for i in ids]
    # 用过的排前面
    models.sort(key=lambda m: m["id"] not in seen)
    # agentbus 派活时 opencode 默认用这个模型（不是 opencode 自己界面里的默认）
    cur = os.environ.get("AGENTBUS_DEEPSEEK_MODEL", "deepseek/deepseek-flash")
    return {"models": models, "current": {"model": cur, "effort": None}, "switch": True, "own_default": _opencode_default()}


ZCODE_CFG = Path(H("~/.zcode/v2/provider_config.json"))


def _zcode_protocol_models():
    """问 zcode 自己：开一个不落盘的临时会话（persistence=deferred），读它报的模型和每个模型的思考档位，再关掉。"""
    import queue
    p = subprocess.Popen(["zcode", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=H("~"))
    q = queue.Queue()

    def rd():
        for line in p.stdout:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if "method" in d and "id" in d:  # zcode 反过来问客户端的事：只答运行偏好，其余说不支持
                ans = {"result": {"nativeSearchEnhancementsEnabled": False}} if d["method"] == "session/requestRuntimePreferences" \
                    else {"error": {"code": -32601, "message": "agentbus 不支持"}}
                p.stdin.write(json.dumps(dict(ans, id=d["id"])) + "\n")
                p.stdin.flush()
            elif "id" in d:
                q.put(d)
    threading.Thread(target=rd, daemon=True).start()
    try:
        ws = H("~")
        p.stdin.write(json.dumps({"id": 1, "method": "session/create", "params": {
            "workspace": {"workspacePath": ws, "workspaceKey": ws}, "persistence": "deferred", "titleGenerationEnabled": False}}) + "\n")
        p.stdin.flush()
        r = q.get(timeout=40)
        res = r.get("result") or {}
        sid = (res.get("session") or {}).get("sessionId")
        if sid:
            p.stdin.write(json.dumps({"id": 2, "method": "session/close", "params": {"sessionId": sid}}) + "\n")
            p.stdin.flush()
            try:
                q.get(timeout=5)
            except Exception:
                pass
        return ((res.get("settings") or {}).get("model") or {}).get("available") or []
    finally:
        p.terminate()


def _zcode():
    models, cur = [], {}
    try:
        for x in _zcode_protocol_models():
            ref = x.get("ref") or {}
            rs = x.get("reasoning") or {}
            models.append({"id": f"{ref['providerId']}/{ref['modelId']}", "name": x.get("label") or ref["modelId"], "group": x.get("providerLabel"),
                           "efforts": _sort(lv["value"] for lv in rs.get("levels") or []), "default": rs.get("defaultLevel")})
    except Exception:
        pass
    try:
        pc = json.loads(ZCODE_CFG.read_text())["config"]
        if not models:  # 问不到就退回读配置文件（没有档位信息）
            for r in pc["providerConfigRules"]["providerRules"]:
                for m in (r.get("config") or {}).get("modelOrder") or []:
                    models.append({"id": f"{r.get('providerId') or r.get('id')}/{m}", "name": m, "efforts": []})
        d = pc.get("defaultModelSelection") or {}
        if d:
            cur = {"model": f"{d['providerId']}/{d['modelId']}", "effort": (d.get("options") or {}).get("reasoningLevel")}
    except Exception:
        pass
    return {"models": models, "current": cur, "switch": True}


_ZSWAP = Path(H("~/.agentbus/zcode-swap.json"))  # 换模型期间存一份原配置：进程被强杀没来得及换回，下次开工 / 服务启动时补换回


def _zatomic(data):
    tmp = ZCODE_CFG.with_name(ZCODE_CFG.name + ".agentbus-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, ZCODE_CFG)


def zcode_recover():
    """上次换了模型却没换回（被强杀）：配置还是我们写的那份，就换回原来的；主人或别的进程改过就不动。"""
    try:
        st = json.loads(_ZSWAP.read_text("utf-8"))
        import base64
        if ZCODE_CFG.read_bytes() == base64.b64decode(st["mine"]):
            _zatomic(base64.b64decode(st["orig"]))
    except (OSError, ValueError, KeyError):
        pass
    _ZSWAP.unlink(missing_ok=True)


class zcode_brain:
    """zcode 的无界面模式没有 --model：开工那几秒把它的默认模型换成选好的，读进去以后马上换回来。
    用锁排队，避免两个活同时换；换回只做一次（定时器和收尾谁先到谁换），换回前先看文件有没有被别人改过，改过就不动；
    写文件用临时文件整体替换，别的进程不会读到写了一半的配置。"""
    HOLD = 6

    def __init__(self, brain):
        self.brain, self.orig, self.mine, self.lock, self.timer = brain or {}, None, None, None, None
        self._once = threading.Lock()

    def __enter__(self):
        m = self.brain.get("model")
        if not self.brain.get("effort") and (not m or "/" not in m):
            return self  # 没选模型也没选强度：用它自己的设置
        import pf
        self.lock = open(H("~/.agentbus/zcode-swap.lock"), "a+")
        pf.lock(self.lock)
        zcode_recover()
        try:
            self.orig = ZCODE_CFG.read_bytes()
            d = json.loads(self.orig)
            cur = d["config"].get("defaultModelSelection") or {}
            if m and "/" in m:
                prov, mid = m.split("/", 1)
            else:  # 只选了思考强度：模型用它现在的默认，只换强度
                prov, mid = cur["providerId"], cur["modelId"]
            sel = {"providerId": prov, "modelId": mid}
            e = self.brain.get("effort") or (d["config"].get("defaultModelSelection") or {}).get("options", {}).get("reasoningLevel")
            if self.brain.get("effort") or e:
                sel["options"] = {"reasoningLevel": self.brain.get("effort") or e}
            d["config"]["defaultModelSelection"] = sel
            self.mine = json.dumps(d, ensure_ascii=False, indent=2).encode()
            import base64
            _ZSWAP.write_text(json.dumps({"orig": base64.b64encode(self.orig).decode(), "mine": base64.b64encode(self.mine).decode(), "t": time.time()}), "utf-8")
            _zatomic(self.mine)
            self.timer = threading.Timer(self.HOLD, self.restore)
            self.timer.start()
        except Exception:
            self.restore()
        return self

    def restore(self):
        with self._once:
            if self.orig is not None:
                try:
                    if ZCODE_CFG.read_bytes() == self.mine:
                        _zatomic(self.orig)
                except Exception:
                    pass
                _ZSWAP.unlink(missing_ok=True)
                self.orig = None
            if self.lock:
                import pf
                pf.unlock(self.lock)
                self.lock.close()
                self.lock = None

    def __exit__(self, *a):
        if self.timer:
            self.timer.cancel()
        self.restore()


def _which(cmd):
    import shutil
    return shutil.which(cmd)


def _agy():
    """反重力：`agy models` 每行“模型id<TAB>名字”，强度写在 id 里（gemini-4-hydrogen-high 这种），所以不单列强度。"""
    import pf

    try:
        result = subprocess.run(pf.resolve(["agy", "models"]), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode:
        return None
    models = []
    for line in result.stdout.splitlines():
        model, tab, name = line.partition("\t")
        model = model.strip()
        if tab and model and not any(c.isspace() for c in model):
            models.append({"id": model, "name": name.strip() or model, "efforts": []})
    if not models:
        return None
    return {"models": models, "current": {}, "switch": True}


def from_help(bin_):
    """通用：没有专门读法的命令行，从它的 --help 里认 --model（后面列了可选值就做成列表，没列就让主人自己填）
    和 --effort / --reasoning-effort 的档位。新接进来的 agent 只要命令行有 --model，就能在战备里选模型。"""
    h = _help([bin_, "--help"]) or _help([bin_, "-h"])
    mm = re.search(r"^\s*(--model)\b[^\n]*", h, re.M)
    if not mm:
        return {"models": [], "current": {}, "switch": False, "note": "它的命令行没有选模型的参数，用它自己的默认模型"}
    line = mm.group(0)
    lst = re.search(r"(?:supported|choices|available|可选)[^(:：]*[:：(]\s*\(?([^)\n]+)", line, re.I)
    models = [x.strip(" .'\"") for x in lst.group(1).split(",") if x.strip(" .'\"")] if lst else []
    em = re.search(r"^\s*(--(?:effort|reasoning-effort|thinking-effort))\b[^\n]*?\(([^)]+)\)", h, re.M)
    efforts = [x.strip() for x in em.group(2).split(",") if x.strip()] if em else []
    return {"models": [{"id": x, "name": x, "efforts": efforts} for x in models], "current": {}, "switch": True, "free": not models,
            "flags": {"model": mm.group(1), "effort": em.group(1) if em else None}}


READERS = {"codex": _codex, "opencode": _opencode, "zcode": _zcode, "agy": _agy}
TTL = 6 * 3600
_mem, _busy = {}, set()
_agy_lock = threading.Lock()
_agy_next_attempt = 0


def _load_disk():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _detect(kind):
    try:
        r = READERS[kind]() if kind in READERS else from_help(kind.split(":", 1)[1])
        if r is None:  # 反重力退出登录、超时或返回空目录时继续用上次成功的列表
            return
        r["t"] = time.time()
        _mem[kind] = r
        from config import update_json
        update_json(CACHE, lambda disk: disk.__setitem__(kind, r))
    finally:
        _busy.discard(kind)


def _agy_options(fresh, discover):
    global _agy_next_attempt
    with _agy_lock:
        hit = _mem.get("agy") or _load_disk().get("agy") or {}
        if hit.get("models"):
            _mem.setdefault("agy", hit)
        now = time.time()
        stale = not hit.get("models") or now - hit.get("t", 0) > TTL
        if discover and "agy" not in _busy and (fresh or (stale and now >= _agy_next_attempt)):
            _busy.add("agy")
            _agy_next_attempt = now + 60
            threading.Thread(target=_detect, args=("agy",), daemon=True).start()
        if not hit.get("models"):
            hit = {"models": [], "current": {}, "switch": True}
        return dict(hit, free=True, detecting="agy" in _busy)


def options(agent, fresh=False, *, discover=True):
    """agent 是 agents_dict 里的一项（分身也行）。慢的识别放后台，先返回上次的结果。"""
    from config import CLAUDE_SEALED, sealed

    if sealed(agent):
        return {"models": [], "current": {}, "switch": False, "note": CLAUDE_SEALED}
    kind = agent.get("run")
    if kind == "agy":
        return _agy_options(fresh, discover)
    if kind == "api":  # API 队员：模型来自钱包账户
        import wallet
        ms = wallet.models(agent.get("account"), fresh) if discover else []
        return {"models": [{"id": m, "name": m, "efforts": []} for m in ms], "current": {"model": agent.get("model")}, "switch": True}
    if kind not in READERS:
        if not agent.get("bin") or kind in ("doubao",):
            return {"models": [], "current": {}, "switch": False, "note": "这个 agent 暂不支持在 agentbus 里换模型"}
        kind = "help:" + agent["bin"]  # 通用读法：看它 --help 里有没有 --model
    hit = _mem.get(kind) or _load_disk().get(kind)
    stale = fresh or not hit or time.time() - hit.get("t", 0) > TTL
    if discover and stale and kind not in _busy:
        _busy.add(kind)
        if kind != "codex":
            threading.Thread(target=_detect, args=(kind,), daemon=True).start()
        else:
            _detect(kind)
    hit = _mem.get(kind) or _load_disk().get(kind) or {"models": [], "current": {}, "switch": True}
    if kind not in _mem:
        _mem[kind] = hit
    return dict(hit, detecting=kind in _busy)


def chosen(cfg, agent_id):
    """主人在 agentbus 里给这个队员选的大脑（没选就是空，用它自己的默认）。"""
    return (cfg.get("brains") or {}).get(agent_id) or {}


def flags(kind, brain):
    """把选好的大脑翻成命令行参数。"""
    m, e = brain.get("model"), brain.get("effort")
    if kind == "codex":
        return (["-m", m] if m else []) + (["-c", f'model_reasoning_effort="{e}"'] if e else [])
    if kind == "agy":
        return (["--model", m] if m else []) + (["--effort", e] if e else [])
    return []


def generic_flags(bin_, brain):
    """通用读法认出来的 agent：按它 --help 里的参数名传模型和强度。"""
    m, e = brain.get("model"), brain.get("effort")
    if not (m or e) or not bin_:
        return []
    hit = _mem.get("help:" + bin_) or _load_disk().get("help:" + bin_)
    if not hit:  # 还没识别过（界面没打开过它的模型菜单）：现在识别一次，选了的模型一定要传进去
        _detect("help:" + bin_)
        hit = _mem.get("help:" + bin_) or {}
    f = hit.get("flags") or {}
    return ([f["model"], m] if m and f.get("model") else []) + ([f["effort"], e] if e and f.get("effort") else [])


def opencode_model(brain, default):
    m = brain.get("model") or default
    return f"{m}#{brain['effort']}" if brain.get("effort") else m


def effective(brain, opts):
    """实际会用的模型和强度：主人选的 > 它自己的当前设置 > 模型默认强度。"""
    cur = opts.get("current") or {}
    m = brain.get("model") or cur.get("model") or ""
    info = next((x for x in opts.get("models") or [] if x["id"] == m), {})
    e = brain.get("effort") or cur.get("effort") or info.get("default")
    return m, e, info


def label(brain, opts):
    """名牌上显示的短标签，像 GPT 那样：“GPT-6.1-Sol 中”。"""
    m, e, info = effective(brain, opts)
    name = info.get("name") or m.split("/")[-1] or "默认"
    return " ".join(x for x in (name, EFFORT_NAME.get(e, e or "")) if x)
