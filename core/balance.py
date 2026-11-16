"""接入余额：用 agent 自己的登录凭证去它官方的接口查剩余额度（队员页「接入余额」开关打开后才会跑）。

每家一个查询函数，返回 {ok, steps: [{name, ok, detail}], windows: [{label, remain(0~1), reset}], error}。
steps 把链路的每一步摆出来（找凭证 → 刷新令牌 → … → 查询额度），哪一步断了界面上一眼能看到。

凭证只在内存里用、只发给那一家的官方接口；不写日志、不改对方的凭证文件。
新加一家：写一个查询函数，登记到 PROVIDERS。
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

H = os.path.expanduser
_tok = {}  # 刷新后的访问令牌（只在内存里）：agent → (token, 过期时间)


def _post(url, body, headers=None, form=False, timeout=15):
    data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        msg = e.read()[:300].decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}：{msg}") from None


# ---------- Gemini CLI（Google 账号登录，Code Assist 免费 / 订阅额度） ----------
# 和 Gemini CLI 自己查额度的路子一样（@google/gemini-cli 的 CodeAssistServer）：
#   ~/.gemini/oauth_creds.json 或钥匙串 gemini-cli-oauth → 过期就刷新 → loadCodeAssist 拿项目 → retrieveUserQuota 拿每个模型剩多少
# 刷新 Gemini CLI 的登录令牌要带上它自己的“已安装应用”客户端 id / secret：不抄进本仓库，用的时候从本机装好的 Gemini CLI 包里读
_gc = None


def _gemini_client():
    global _gc
    if _gc is None:
        import glob
        import re as _re
        import shutil
        roots = []
        g = shutil.which("gemini")
        if g:
            real = os.path.realpath(g)  # …/lib/node_modules/@google/gemini-cli/… 往上找到包目录
            i = real.find("@google/gemini-cli")
            if i > 0:
                roots.append(real[:i] + "@google/gemini-cli")
        roots += glob.glob(H("~/.npm-global/lib/node_modules/@google/gemini-cli")) + glob.glob("/opt/homebrew/lib/node_modules/@google/gemini-cli") + glob.glob("/usr/local/lib/node_modules/@google/gemini-cli")
        _gc = ("", "")
        for r in roots:
            for f in glob.glob(r + "/**/*.js", recursive=True):
                try:
                    t = open(f, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                cid = _re.search(r"OAUTH_CLIENT_ID\s*=\s*[\"']([^\"']+)", t)
                sec = _re.search(r"OAUTH_CLIENT_SECRET\s*=\s*[\"']([^\"']+)", t)
                if cid and sec:
                    _gc = (cid.group(1), sec.group(1))
                    return _gc
    return _gc


class _GClient:
    def __getitem__(self, i):
        c = _gemini_client()
        if not c[0]:
            raise RuntimeError("本机没找到 Gemini CLI，读不了它的登录")
        return c[i]


GEMINI_CLIENT = _GClient()
CODE_ASSIST = "https://cloudcode-pa.googleapis.com/v1internal"


def _gemini_creds():
    """返回 (access_token, refresh_token, 过期时间秒, 从哪读的)；找不到返回 None。"""
    f = H("~/.gemini/oauth_creds.json")
    if os.path.exists(f):
        d = json.load(open(f))
        exp = (d.get("expiry_date") or 0) / 1000
        return d.get("access_token"), d.get("refresh_token"), exp, f
    try:  # 新版 Gemini CLI 存钥匙串（服务名 gemini-cli-oauth，账号 main-account）
        r = subprocess.run(["security", "find-generic-password", "-s", "gemini-cli-oauth", "-a", "main-account", "-w"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout.strip())
            t = d.get("token") or d
            return t.get("accessToken") or t.get("access_token"), t.get("refreshToken") or t.get("refresh_token"), \
                (t.get("expiresAt") or t.get("expiry_date") or 0) / 1000, "钥匙串 gemini-cli-oauth"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if os.path.exists(H("~/.gemini/gemini-credentials.json")):
        raise RuntimeError("Gemini CLI 把凭证加密存在 ~/.gemini/gemini-credentials.json，agentbus 读不了。"
                           "可以设环境变量 GEMINI_FORCE_FILE_STORAGE=true 后重新登录一次，让它存成 oauth_creds.json")
    return None


def gemini():
    steps = []

    def step(name, ok, detail=""):
        steps.append({"name": name, "ok": ok, "detail": detail})

    try:
        c = _gemini_creds()
    except RuntimeError as e:
        step("找到登录凭证", False, str(e))
        return {"ok": False, "steps": steps, "error": str(e)}
    if not c or not (c[0] or c[1]):
        msg = "还没登录 Google：点下面的「用 Google 登录」，在浏览器里登录一次就好"
        step("找到登录凭证", False, msg)
        return {"ok": False, "steps": steps, "error": msg}
    access, refresh, exp, src = c
    step("找到登录凭证", True, f"来自 {src}")
    cached = _tok.get("gemini")
    if cached and cached[1] > time.time() + 60:
        access = cached[0]
        step("令牌有效", True, "用上次刷新的令牌")
    elif access and exp > time.time() + 60:
        step("令牌有效", True, "还没过期")
    else:
        if not refresh:
            step("刷新令牌", False, "令牌过期了，凭证里没有 refresh token：点「用 Google 登录」重新登录一次")
            return {"ok": False, "steps": steps, "error": steps[-1]["detail"]}
        try:
            r = _post("https://oauth2.googleapis.com/token", {"grant_type": "refresh_token", "refresh_token": refresh,
                                                              "client_id": GEMINI_CLIENT[0], "client_secret": GEMINI_CLIENT[1]}, form=True)
            access = r["access_token"]
            _tok["gemini"] = (access, time.time() + int(r.get("expires_in") or 3000))
            step("刷新令牌", True, "换到了新的访问令牌（只存在内存里）")
        except Exception as e:
            step("刷新令牌", False, f"{e}；可能登录已失效，点「用 Google 登录」重新登录一次")
            return {"ok": False, "steps": steps, "error": steps[-1]["detail"]}
    auth = {"Authorization": "Bearer " + access}
    try:
        meta = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
        proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
        r = _post(f"{CODE_ASSIST}:loadCodeAssist", {"cloudaicompanionProject": proj, "metadata": meta} if proj else {"metadata": meta}, auth)
        p = r.get("cloudaicompanionProject")
        project = (p.get("id") if isinstance(p, dict) else p) or proj
        tier = ((r.get("currentTier") or {}).get("name") or (r.get("currentTier") or {}).get("id") or "")
        gone = next((t for t in r.get("ineligibleTiers") or [] if t.get("reasonCode") == "UNSUPPORTED_CLIENT"), None)
        if not project and gone:  # Google 已经不让 Gemini CLI 用个人免费额度了，个人用户改走反重力：换成读反重力的额度
            step("确认账号项目", False, "Google 已不再给 Gemini CLI 个人免费额度（官方说法：个人用户请改用反重力 Antigravity），改读反重力的额度")
            ag = antigravity()
            return {**ag, "steps": steps + ag["steps"]}
        if not project:  # 新账号还没开通：自动按默认（免费）档开通
            project = _gemini_onboard(auth, meta, r)
            if project:
                step("开通 Gemini 免费额度", True, f"项目 {project}")
        if not project:
            step("确认账号项目", False, "这个账号没能自动开通 Gemini Code Assist（可能是企业账号要管理员开通）")
            return {"ok": False, "steps": steps, "error": steps[-1]["detail"]}
        step("确认账号项目", True, f"{tier} · 项目 {project}" if tier else f"项目 {project}")
    except Exception as e:
        step("确认账号项目", False, str(e))
        return {"ok": False, "steps": steps, "error": str(e)}
    try:
        q = _post(f"{CODE_ASSIST}:retrieveUserQuota", {"project": project}, auth)
    except Exception as e:
        step("查询额度", False, str(e))
        return {"ok": False, "steps": steps, "error": str(e)}
    wins = []
    for b in q.get("buckets") or []:
        if b.get("remainingFraction") is None:
            continue
        model = str(b.get("modelId") or b.get("tokenType") or "额度")
        short = model.replace("gemini-", "").replace("-preview", "")
        wins.append({"label": short, "remain": max(0.0, min(1.0, float(b["remainingFraction"]))), "reset": b.get("resetTime"), "model": model})
    if not wins:
        step("查询额度", False, "接口没返回额度数字（可能是付费 API 账号，没有按日额度）")
        return {"ok": False, "steps": steps, "error": steps[-1]["detail"]}
    wins.sort(key=lambda w: w["remain"])
    step("查询额度", True, f"{len(wins)} 个模型的额度")
    return {"ok": True, "steps": steps, "windows": wins, "updated": time.time()}


# ---------- 反重力（Antigravity）：Google 个人免费额度现在走它 ----------
# 反重力桌面端开着时，本机有一个 language_server 进程；它的 GetUserStatus 接口里带每个模型的 quotaInfo（剩余比例、恢复时间），
# 反重力自己界面上的额度就是从这里来的。端口和 CSRF 令牌在这个进程的启动参数里（本机进程，只有当前用户能看到）。
def _antigravity_ls():
    """找到正在跑的反重力 language_server：返回 (pid, csrf, [端口…])；没开就 None。"""
    import re as _re
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        pid, cmd = parts
        exe = _re.split(r"\s--", cmd, maxsplit=1)[0]  # 程序路径本身（不看参数里碰巧出现的字）
        if not (_re.search(r"/language_server[^/]*$", exe) and "ntigravity" in exe):
            continue
        line = cmd
        csrf = _re.search(r"--csrf_token[= ](\S+)", line)
        ports = [int(m) for m in _re.findall(r"--(?:extension_server_port|https_server_port|http_server_port|server_port|lsp_port)[= ](\d+)", line)]
        try:  # 参数里没写端口（随机端口）：看这个进程在听哪些端口
            ls = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", pid], capture_output=True, text=True, timeout=5).stdout
            ports += [int(m) for m in _re.findall(r":(\d+) \(LISTEN\)", ls)]
        except (OSError, subprocess.SubprocessError):
            pass
        return pid, csrf.group(1) if csrf else "", list(dict.fromkeys(ports))
    return None


def _find_quota(obj, out, label=None):
    """在 GetUserStatus 的返回里找所有 quotaInfo（不同版本嵌套位置不一样，递归找）。"""
    if isinstance(obj, dict):
        lab = obj.get("label") or obj.get("displayName") or obj.get("modelName") or (obj.get("modelOrAlias") or {}).get("model") if isinstance(obj.get("modelOrAlias"), dict) else obj.get("label") or obj.get("displayName") or obj.get("modelName") or label
        q = obj.get("quotaInfo")
        if isinstance(q, dict) and q.get("remainingFraction") is not None:
            out.append({"label": str(lab or "额度"), "remain": max(0.0, min(1.0, float(q["remainingFraction"]))), "reset": q.get("resetTime")})
        for v in obj.values():
            _find_quota(v, out, lab)
    elif isinstance(obj, list):
        for v in obj:
            _find_quota(v, out, label)


def antigravity():
    steps = []

    def step(name, ok, detail=""):
        steps.append({"name": name, "ok": ok, "detail": detail})

    ls = _antigravity_ls()
    if not ls:
        msg = "反重力没开：打开反重力（Antigravity）应用，额度就能读到（它开着时每 5 分钟自动刷新）"
        step("找到反重力", False, msg)
        return {"ok": False, "steps": steps, "error": msg}
    pid, csrf, ports = ls
    step("找到反重力", True, f"进程 {pid}，端口 {', '.join(map(str, ports)) or '未知'}")
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # 本机自签证书，只连 127.0.0.1
    body = json.dumps({"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "zh"}}).encode()
    last = ""
    for port in ports:
        for scheme in ("https", "http"):
            req = urllib.request.Request(f"{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1", "X-Codeium-Csrf-Token": csrf})
            try:
                with urllib.request.urlopen(req, timeout=4, context=ctx if scheme == "https" else None) as r:
                    data = json.loads(r.read() or b"{}")
            except Exception as e:
                last = f"{scheme}:{port} {type(e).__name__}: {str(e)[:80]}"
                continue
            wins = []
            _find_quota(data, wins)
            seen, uniq = set(), []
            for w in sorted(wins, key=lambda w: w["remain"]):
                if w["label"] not in seen:
                    seen.add(w["label"])
                    uniq.append(w)
            if not uniq:
                step("查询额度", False, "反重力的状态接口里没有额度数字（可能版本变了）")
                return {"ok": False, "steps": steps, "error": steps[-1]["detail"]}
            step("查询额度", True, f"{len(uniq)} 个模型的额度")
            return {"ok": True, "steps": steps, "windows": uniq, "updated": time.time(), "source": "antigravity"}
    step("查询额度", False, f"连不上反重力的本地接口（{last}）")
    return {"ok": False, "steps": steps, "error": steps[-1]["detail"]}


# 队员 id（分身用本体的）→ 查询函数
PROVIDERS = {"gemini": antigravity, "antigravity": antigravity}  # 队员 gemini 现在就是反重力 CLI（agy）；Gemini CLI 的查法（gemini()）留着备用


# ---------- 一键登录（新手不用开终端）：和 Gemini CLI 一样的 Google 授权，登录完 Gemini CLI 自己也算登录好了 ----------
_login = {}  # state → {verifier, t, port}
GEMINI_SCOPES = ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/userinfo.email",
                 "https://www.googleapis.com/auth/userinfo.profile"]


def login_start(agent_id, port):
    """返回要在浏览器里打开的 Google 登录地址。回调回到本机服务的 /oauth2callback（带 PKCE 和一次性 state）。"""
    if agent_id != "gemini":
        raise ValueError("这家还没有一键登录")
    import base64
    import hashlib
    import secrets
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, v in _login.items() if now - v["t"] > 900]:  # 15 分钟没回来的作废
        _login.pop(k, None)
    _login[state] = {"verifier": verifier, "t": now, "port": port, "agent": agent_id}
    q = {"client_id": GEMINI_CLIENT[0], "redirect_uri": f"http://127.0.0.1:{port}/oauth2callback", "response_type": "code",
         "scope": " ".join(GEMINI_SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(q)


def login_pending(state):
    v = _login.get(state)
    return bool(v and time.time() - v["t"] <= 900)  # 15 分钟没回来的作废


def login_finish(state, code):
    """Google 跳回来时：用授权码换令牌，存成 Gemini CLI 的凭证（旧的先备份），并告诉 Gemini CLI 用 Google 登录。返回登录的邮箱。"""
    st = _login.pop(state, None)
    if not st or time.time() - st["t"] > 900:
        raise RuntimeError("这次登录已经过期了，回 agentbus 再点一次登录")
    r = _post("https://oauth2.googleapis.com/token", {"grant_type": "authorization_code", "code": code, "code_verifier": st["verifier"],
                                                      "redirect_uri": f"http://127.0.0.1:{st['port']}/oauth2callback",
                                                      "client_id": GEMINI_CLIENT[0], "client_secret": GEMINI_CLIENT[1]}, form=True)
    creds = {"access_token": r.get("access_token"), "refresh_token": r.get("refresh_token"), "scope": r.get("scope"),
             "token_type": r.get("token_type") or "Bearer", "id_token": r.get("id_token"),
             "expiry_date": int((time.time() + int(r.get("expires_in") or 3600)) * 1000)}
    d = H("~/.gemini")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "oauth_creds.json")
    if os.path.exists(f):
        import shutil
        shutil.copy2(f, f + time.strftime(".bak-agentbus-%m%d-%H%M%S"))
    with open(f, "w") as fh:
        json.dump(creds, fh, indent=2)
    os.chmod(f, 0o600)
    email = ""
    try:  # 记下当前账号（Gemini CLI 的 google_accounts.json 同样格式）
        req = urllib.request.Request("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": "Bearer " + creds["access_token"]})
        email = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("email") or ""
        if email:
            ga = os.path.join(d, "google_accounts.json")
            cur = json.load(open(ga)) if os.path.exists(ga) else {"active": None, "old": []}
            if cur.get("active") and cur["active"] != email:
                cur.setdefault("old", []).append(cur["active"])
            cur["active"] = email
            json.dump(cur, open(ga, "w"), indent=2)
    except Exception:
        pass
    sp = os.path.join(d, "settings.json")  # 认证方式设成 Google 登录：之后派活跑 gemini 不会卡在“选登录方式”
    try:
        cfg = json.load(open(sp)) if os.path.exists(sp) else {}
        auth = cfg.setdefault("security", {}).setdefault("auth", {})
        if not auth.get("selectedType"):
            auth["selectedType"] = "oauth-personal"
            json.dump(cfg, open(sp, "w"), indent=2)
    except (OSError, ValueError):
        pass
    _tok.pop("gemini", None)
    return email


def _gemini_onboard(auth, meta, r):
    """账号还没开通 Gemini Code Assist（新账号常见）：和 Gemini CLI 一样用默认（免费）档开通，开通是个长任务，轮询到完成。"""
    tiers = r.get("allowedTiers") or []
    tier = next((t for t in tiers if t.get("isDefault")), tiers[0] if tiers else None)
    if not tier:
        return None
    body = {"tierId": tier.get("id"), "metadata": meta}
    for _ in range(10):
        op = _post(f"{CODE_ASSIST}:onboardUser", body, auth)
        if op.get("done"):
            p = ((op.get("response") or {}).get("cloudaicompanionProject") or {})
            return p.get("id") if isinstance(p, dict) else p
        time.sleep(3)
    return None


# 查不了的写清楚原因（队员页上直接显示），不只说“不支持”
WHY_NOT = {
    "codebuddy": "WorkBuddy 把登录令牌用它自己的格式加密存在本机（密钥在钥匙串里），agentbus 不去破解它的加密，所以读不到剩余积分。"
                 "名下显示的是本机记录的已用积分，剩余积分请在 WorkBuddy 里看。",
    "claude": "Claude 的登录凭证加密存放，订阅额度只能显示本机记录的用量。",
}


def why_not(agent_id):
    return WHY_NOT.get(agent_id)


LOGIN = set()  # 能在 agentbus 里内置授权登录的（Gemini CLI 的 Google 授权留着备用；反重力在它自己的应用里登录）


def can_login(agent_id):
    return agent_id in LOGIN


def supported(agent_id):
    return agent_id in PROVIDERS


def check(agent_id):
    fn = PROVIDERS.get(agent_id)
    if not fn:
        return {"ok": False, "steps": [], "error": "这家还没有接入余额的方式"}
    try:
        return fn()
    except Exception as e:  # 任何意外都变成一句能看懂的错误，不让后台循环出事
        return {"ok": False, "steps": [{"name": "查询", "ok": False, "detail": f"{type(e).__name__}: {e}"}], "error": str(e)}
