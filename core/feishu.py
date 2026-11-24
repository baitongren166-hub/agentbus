"""飞书接入：主人出门时在手机飞书上用 agentbus，操作照电脑上来。

  私聊机器人 = agentbus 主界面（新聊天、选项目、新建项目、进度、额度、账单）；
  一个项目群 = 一个项目文件夹；群里一个「新聊天」= 一个对话窗口，两大模式随时切换：
    助理（默认）：这个聊天就是一个接入了 bus 的主位窗口（可选谁当主位），按 bus 自己的规矩派活；
                      它派的活做完，用 bus 的回音把它叫醒，核对后再告诉主人。
    聊天室：派活（@ 谁谁干，值班只记录）、群聊（头脑风暴），就是 bus 的消息窗口那套。
用飞书的长连接收事件（电脑主动连飞书，不用公网地址），只认绑定过的主人。
状态在 ~/.agentbus/feishu.json，App Secret 在钥匙串（pf.secret_get("feishu-<app_id>")）。
不依赖第三方库：WebSocket 和飞书长连接用的 protobuf 帧都在这里手写（帧格式见 larksuite/oapi-sdk-python 的 ws/pb）。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import HOME, agents_dict, load_config, load_json, locked, projects, save_json, sealed  # noqa: E402

STATE = HOME / "feishu.json"
STATUS = HOME / "feishu-status.json"  # 连没连上（给命令行和界面看，不和 STATE 抢写）
BASE = "https://open.feishu.cn"
LOG = logging.getLogger("feishu")
_kick_lock = threading.Lock()
_group_lock = threading.Lock()
_token_lock = threading.Lock()
_callback = threading.local()
_channel_lock = threading.Lock()
_tok = {"v": None, "exp": 0, "identity": None}
_bot = {}


# ---------- 状态 ----------
def state():
    st = load_json(STATE, {})
    if not isinstance(st, dict):
        st = {}
    for k in ("groups", "chats", "head", "cursor", "pending", "queue", "lead_job", "mark", "prog", "lead_retry"):
        st.setdefault(k, {})
    st.setdefault("seen", [])
    return st


def update(fn):
    with locked("feishu"):
        st = state()
        r = fn(st)
        save_json(STATE, st)
        return r


def _status(**kw):
    try:
        save_json(STATUS, {**load_json(STATUS, {}), **kw, "t": time.time(), "pid": os.getpid()})
    except OSError:
        pass


def _identity(st):
    return (st.get("app_id"), st.get("revision"))


def connection_status(st=None):
    st = state() if st is None else st
    ss = load_json(STATUS, {})
    if not st.get("enabled") or ss.get("identity") != list(_identity(st)):
        return {}
    if ss.get("ok"):
        from pf import pid_alive
        if not pid_alive(ss.get("pid")):
            return {}
    return ss


def secret(app_id):
    import pf
    return pf.secret_get(f"feishu-{app_id}")


# ---------- 飞书开放接口 ----------
_OPENERS = [urllib.request.build_opener(urllib.request.ProxyHandler({})), urllib.request.build_opener()]


def _http(method, url, body=None, headers=None, raw=False, timeout=30):
    data = None
    headers = dict(headers or {})
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    for i in range(3):  # 先直连（飞书是国内服务，走代理反而慢、还会断）；直连不通再走系统代理，隔一秒重试
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _OPENERS[min(i, 1)].open(req, timeout=timeout) as r:
                out = r.read()
            break
        except urllib.error.HTTPError as e:
            out = e.read()
            if raw:
                raise
            break
        except (urllib.error.URLError, OSError) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            # POST 可能已经到达服务端。只重试确定还没建立连接的失败。
            retry_safe = method != "POST" or url.endswith(("/tenant_access_token/internal", "/callback/ws/endpoint"))
            if i == 2 or (not retry_safe and not isinstance(reason, (ConnectionRefusedError, socket.gaierror))):
                raise
            time.sleep(1 + i)
    return out if raw else json.loads(out or b"{}")


def token():
    with _token_lock:
        st = state()
        identity = _identity(st)
        if _tok["identity"] == identity and _tok["v"] and time.time() < _tok["exp"] - 300:
            return _tok["v"]
        app = st.get("app_id")
        d = _http("POST", BASE + "/open-apis/auth/v3/tenant_access_token/internal",
                  {"app_id": app, "app_secret": secret(app)})
        if d.get("code") or not d.get("tenant_access_token"):
            raise RuntimeError("获取飞书令牌失败")
        _tok.update(v=d["tenant_access_token"], exp=time.time() + d.get("expire", 7200), identity=identity)
        return _tok["v"]


def api(method, path, body=None, params=None, **kw):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    d = _http(method, url, body, {"Authorization": "Bearer " + token(), **kw.pop("headers", {})}, **kw)
    if isinstance(d, dict) and d.get("code"):
        LOG.warning("飞书接口 %s %s 出错：%s", method, path, d.get("code"))
    return d


def _card(title, md="", color="blue", buttons=None, icon=None, elements=None, sub=None):
    """飞书卡片（JSON 2.0）：标题栏可带头像（icon 是 image_key），正文 markdown，再接自定义元素和一行按钮。"""
    els = [{"tag": "markdown", "content": md[:6000]}] if md else []
    els += elements or []
    if buttons:
        els.append(_row(buttons))
    head = {"title": {"tag": "plain_text", "content": title[:60]}, "template": color}
    if sub:
        head["subtitle"] = {"tag": "plain_text", "content": sub[:60]}
    if icon:
        head["icon"] = {"tag": "custom_icon", "img_key": icon}
    return {"schema": "2.0", "config": {"update_multi": True, "width_mode": "fill"}, "header": head,
            "body": {"elements": els or [{"tag": "markdown", "content": " "}]}}


ICONS = {"project": "folder_outlined", "mode": "app_outlined", "lead": "crown_outlined", "team": "group_outlined"}


def _md(content, icon=None, color="grey"):
    """一段文字，前面可带一个飞书自带的线条图标（icon 是 ICONS 里的名字或图标 token）。"""
    el = {"tag": "markdown", "content": content}
    if icon:
        el["icon"] = {"tag": "standard_icon", "token": ICONS.get(icon, icon), "color": color}
    return el


def _btn(label, value, kind="default", submit=False):
    b = {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind, "size": "small",
         "behaviors": [{"type": "callback", "value": value}]}
    if submit:
        b.update(form_action_type="submit", name="go")
    return b


def _row(buttons, label=None, icon=None):
    """一行按钮（放不下自动换行），前面可带一个小标签（和图标），比如「模式」。"""
    cols = [{"tag": "column", "width": "auto", "vertical_align": "center", "elements": [_md(f"**{label}**", icon)]}] if label else []
    cols += [{"tag": "column", "width": "auto", "vertical_align": "center",
              "elements": [_btn(b["label"], b["value"], b.get("type", "default"))]} for b in buttons]
    return {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px", "columns": cols}


def _pic(key, size=None):
    """图片：不给 size 就整宽（按宽度缩放）；给 "56px 56px" 这种就是固定大小（头像格子）。"""
    el = {"tag": "img", "img_key": key, "alt": {"tag": "plain_text", "content": ""}, "preview": False, "transparent": True,
          "scale_type": "crop_center" if size else "fit_horizontal"}
    if size:
        el["size"] = size
    return el


def _content(kind, payload):
    if kind == "text":
        return json.dumps({"text": payload}, ensure_ascii=False)
    if kind == "image":
        return json.dumps({"image_key": payload})
    return json.dumps(payload, ensure_ascii=False)


def send(chat_id, kind, payload):
    d = api("POST", "/open-apis/im/v1/messages", {"receive_id": chat_id, "msg_type": kind, "content": _content(kind, payload)},
            {"receive_id_type": "chat_id"})
    return _message_id(d)


def _message_id(data):
    mid = (data.get("data") or {}).get("message_id")
    if not mid:
        raise RuntimeError("飞书没有返回消息编号")
    return mid


def reply(msg_id, kind, payload, thread=True):
    d = api("POST", f"/open-apis/im/v1/messages/{msg_id}/reply",
            {"msg_type": kind, "content": _content(kind, payload), "reply_in_thread": thread})
    return _message_id(d)


def patch_card(msg_id, card):
    d = api("PATCH", f"/open-apis/im/v1/messages/{msg_id}", {"content": json.dumps(card, ensure_ascii=False)})
    if d.get("code"):
        raise RuntimeError("飞书卡片更新失败")


def download(msg_id, key, kind="image"):
    import chat
    chat.FILES.mkdir(parents=True, exist_ok=True)
    data = api("GET", f"/open-apis/im/v1/messages/{msg_id}/resources/{key}", params={"type": kind}, raw=True)
    f = chat.FILES / f"feishu-{time.strftime('%m%d-%H%M%S')}-{secrets.token_hex(4)}.{'png' if kind == 'image' else 'bin'}"
    f.write_bytes(data)
    return str(f)


# ---------- WebSocket（够飞书长连接用的最小实现） ----------
class WS:
    def __init__(self, url, timeout):
        u = urlparse(url)
        if u.scheme != "wss" or not u.hostname:
            raise ValueError("飞书长连接地址无效")
        raw = socket.create_connection((u.hostname, u.port or 443), timeout=30)
        try:
            self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
            self._handshake(u)
            self.s.settimeout(timeout)
        except BaseException:
            getattr(self, "s", raw).close()
            raise
        self.wlock = threading.Lock()

    def _handshake(self, u):
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.s.recv(1)
            if not c:
                raise ConnectionError("握手时连接断了")
            head += c
            if len(head) > 16384:
                raise ConnectionError("握手响应头过大")
        lines = head.split(b"\r\n")
        if lines[0].split()[1:2] != [b"101"]:
            raise ConnectionError("握手被拒")
        headers = dict((k.lower(), v.strip()) for k, _, v in (line.partition(b":") for line in lines[1:]))
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if headers.get(b"sec-websocket-accept") != expected:
            raise ConnectionError("握手校验失败")

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c:
                raise ConnectionError("连接断了")
            buf += c
        return buf

    def recv(self):
        data, op0 = b"", None
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n + len(data) > 16 * 1024 * 1024:
                raise ValueError("飞书消息帧过大")
            mask = self._read(4) if b2 & 0x80 else None
            p = self._read(n)
            if mask:
                p = _xor(p, mask)
            if op == 8:
                raise ConnectionError("飞书关了连接")
            if op == 9:
                self.send(p, 10)
            if op >= 8:
                continue
            op0 = op or op0
            data += p
            if b1 & 0x80:
                return op0, data

    def send(self, payload, op=2):
        mask, n = os.urandom(4), len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        with self.wlock:
            self.s.sendall(head + mask + _xor(payload, mask))

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def _xor(p, mask):
    m = (mask * (len(p) // 4 + 1))[:len(p)]
    return (int.from_bytes(p, "big") ^ int.from_bytes(m, "big")).to_bytes(len(p), "big") if p else b""


# ---------- 长连接的帧（protobuf：SeqID=1 LogID=2 service=3 method=4 headers=5 payload=8 LogIDNew=9） ----------
def _varint(n):
    n &= (1 << 64) - 1  # protobuf int32/int64 负数按补码编码
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb(b):
    i, out = 0, {}
    while i < len(b):
        key = shift = 0
        while True:
            c = b[i]
            i += 1
            key |= (c & 0x7F) << shift
            shift += 7
            if c < 0x80:
                break
        f, wt = key >> 3, key & 7
        if wt == 0:
            v = shift = 0
            while True:
                c = b[i]
                i += 1
                v |= (c & 0x7F) << shift
                shift += 7
                if c < 0x80:
                    break
        elif wt == 2:
            n = shift = 0
            while True:
                c = b[i]
                i += 1
                n |= (c & 0x7F) << shift
                shift += 7
                if c < 0x80:
                    break
            v, i = b[i:i + n], i + n
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError("看不懂的帧")
        out.setdefault(f, []).append(v)
    return out


def frame_decode(b):
    d = _pb(b)
    heads = {}
    for h in d.get(5, []):
        hd = _pb(h)
        heads[hd.get(1, [b""])[0].decode()] = hd.get(2, [b""])[0].decode()
    one = lambda f, dft=0: d.get(f, [dft])[0]  # noqa: E731
    return {"seq": one(1), "log": one(2), "service": one(3), "method": one(4), "headers": heads,
            "payload": one(8, b""), "log_new": one(9, b"")}


def frame_encode(fr):
    out = bytearray()

    def by(f, v):
        out.extend(_varint(f << 3 | 2) + _varint(len(v)) + v)

    for f, k in ((1, "seq"), (2, "log"), (3, "service"), (4, "method")):
        out.extend(_varint(f << 3) + _varint(int(fr.get(k) or 0)))
    for k, v in fr.get("headers", {}).items():
        kb, vb = k.encode(), str(v).encode()
        by(5, _varint(1 << 3 | 2) + _varint(len(kb)) + kb + _varint(2 << 3 | 2) + _varint(len(vb)) + vb)
    if fr.get("payload") is not None:
        by(8, fr["payload"])
    if fr.get("log_new"):
        by(9, fr["log_new"])
    return bytes(out)


# ---------- 收：长连接 ----------
def _current(identity):
    st = state()
    return st.get("enabled") and _identity(st) == identity


def run():
    """服务里常驻：配置变化时立即重连。"""
    wait = 5
    while True:
        identity = None
        try:
            st = state()
            identity = _identity(st)
            if not (st.get("enabled") and st.get("app_id") and secret(st["app_id"])):
                if (load_json(STATUS, {}).get("identity") != list(identity)
                        or load_json(STATUS, {}).get("err") != "没开飞书接入"):
                    _status(ok=False, err="没开飞书接入", identity=identity)
                time.sleep(1)
                continue
            _session(identity)
            wait = 5
        except Exception as e:  # 不记录网络异常原文，避免握手 URL 或服务端内容进入日志
            LOG.warning("飞书长连接断了：%s", type(e).__name__)
            _status(ok=False, err=f"长连接断开（{type(e).__name__}）", identity=identity)
            for _ in range(wait):
                time.sleep(1)
                if identity and not _current(identity):
                    break
            wait = min(wait * 2, 120)


def _session(identity):
    app_id = identity[0]
    d = _http("POST", BASE + "/callback/ws/endpoint", {"AppID": app_id, "AppSecret": secret(app_id)}, {"locale": "zh"})
    if d.get("code"):
        raise RuntimeError("拿不到飞书长连接地址")
    url, cc = d["data"]["URL"], d["data"].get("ClientConfig") or {}
    ping = max(1, int(cc.get("PingInterval") or 120))
    service = int(parse_qs(urlparse(url).query).get("service_id", ["0"])[0])
    if not _current(identity):
        return
    ws = WS(url, timeout=ping * 3)
    stop = threading.Event()

    def pinger():
        last = time.monotonic()
        while not stop.wait(1):
            try:
                if not _current(identity):
                    ws.s.shutdown(socket.SHUT_RDWR)
                    return
                if time.monotonic() - last >= ping:
                    ws.send(frame_encode({"service": service, "method": 0, "headers": {"type": "ping"}}))
                    last = time.monotonic()
            except OSError:
                return

    threading.Thread(target=pinger, daemon=True).start()
    _status(ok=True, err="", since=time.time(), identity=identity)
    LOG.info("飞书长连接已连上")
    parts = {}
    try:
        # 机器人 ID 只用于识别 @，查询慢或失败时不能卡住接收事件。
        def get_bot():
            try:
                bot = api("GET", "/open-apis/bot/v3/info").get("bot") or {}
                if _current(identity):
                    _bot.update(bot)
            except Exception:
                LOG.warning("没能读取飞书机器人信息")
        _bot.clear()
        threading.Thread(target=get_bot, daemon=True).start()
        while _current(identity):
            try:
                op, data = ws.recv()
            except OSError:
                if not _current(identity):
                    return
                raise
            if op != 2 or not _current(identity):
                continue
            try:
                fr = frame_decode(data)
                h = fr["headers"]
                if fr["method"] == 0:
                    if h.get("type") == "pong" and fr["payload"]:
                        ping = max(1, int(json.loads(fr["payload"]).get("PingInterval") or ping))
                        ws.s.settimeout(ping * 3)
                    continue
                mid, total, seq = h.get("message_id", ""), int(h.get("sum") or 1), int(h.get("seq") or 0)
                payload = fr["payload"]
                if not 1 <= total <= 64 or not 0 <= seq < total:
                    raise ValueError("飞书分帧编号无效")
                if total > 1:
                    if not mid:
                        raise ValueError("飞书分帧缺消息编号")
                    now = time.monotonic()
                    parts = {k: v for k, v in parts.items() if now - v[0] < 15}
                    if mid not in parts and len(parts) >= 32:
                        parts.pop(next(iter(parts)))
                    _, chunks = parts.setdefault(mid, (now, [None] * total))
                    if len(chunks) != total:
                        parts.pop(mid)
                        raise ValueError("飞书分帧数量变化")
                    chunks[seq] = payload
                    if any(chunk is None for chunk in chunks):
                        continue
                    payload = b"".join(parts.pop(mid)[1])
                ev = json.loads(payload)
            except (AttributeError, IndexError, TypeError, ValueError):
                LOG.warning("忽略无法读取的飞书帧")
                continue
            t0 = time.monotonic()
            result = None
            try:
                result = handle(ev)
            except Exception:
                LOG.warning("处理飞书事件失败")
            resp = {"code": 200}
            if result:
                resp["data"] = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
            fr["headers"] = {**h, "biz_rt": str(int((time.monotonic() - t0) * 1000))}
            fr["payload"] = json.dumps(resp).encode()
            ws.send(frame_encode(fr))
    finally:
        stop.set()
        ws.close()
        _status(ok=False, err="", identity=identity)


def handle(ev):
    """一个事件。要在 3 秒内回飞书，所以真正的活扔到后台线程里做；卡片按钮要的提示直接返回。"""
    if not state().get("enabled"):
        return None
    head = ev.get("header") or {}
    eid = head.get("event_id")
    if eid:
        dup = update(lambda st: eid in st["seen"] or st["seen"].append(eid) or st.__setitem__("seen", st["seen"][-500:]))
        if dup:
            return None
    kind = head.get("event_type")
    body = ev.get("event") or {}
    if kind == "im.message.receive_v1":
        threading.Thread(target=_guard, args=(on_message, body), daemon=True).start()
    elif kind == "card.action.trigger":
        return on_card(body)
    return None


def _guard(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001
        LOG.warning("处理飞书消息出错：%s", fn.__name__)


# ---------- 收：消息 ----------
def _text_files(m):
    """消息里的文字（@ 机器人自己去掉，@ 别人换成名字）和图片（下载到聊天室附件目录）。"""
    kind, mid = m.get("message_type"), m.get("message_id")
    try:
        c = json.loads(m.get("content") or "{}")
    except ValueError:
        c = {}
    texts, files = [], []
    if kind == "text":
        texts.append(c.get("text", ""))
    elif kind == "post":
        c = c.get("zh_cn") or c if "content" not in c else c
        if c.get("title"):
            texts.append(c["title"])
        for line in c.get("content") or []:
            seg = []
            for x in line:
                if x.get("tag") in ("text", "a"):
                    seg.append(x.get("text", ""))
                elif x.get("tag") == "at":
                    seg.append("@" + (x.get("user_name") or ""))
                elif x.get("tag") == "img" and x.get("image_key"):
                    files.append({"path": download(mid, x["image_key"])})
            texts.append("".join(seg))
    elif kind == "image" and c.get("image_key"):
        files.append({"path": download(mid, c["image_key"])})
    text = "\n".join(t for t in texts if t)
    for mt in m.get("mentions") or []:
        is_bot = (mt.get("id") or {}).get("open_id") == _bot.get("open_id")
        text = text.replace(mt.get("key", "\0"), "" if is_bot else "@" + (mt.get("name") or ""))
    return re.sub(r"@_user_\d+", "", text).strip(), files


def on_message(ev):
    m, sender = ev.get("message") or {}, ev.get("sender") or {}
    if sender.get("sender_type") != "user":
        return
    oid = (sender.get("sender_id") or {}).get("open_id")
    st = state()
    if not st.get("enabled") or not oid:
        return
    p2p = m.get("chat_type") == "p2p"
    if not st.get("owner"):
        def pair(s):
            if s.get("enabled") and not s.get("owner") and s.get("pair") and hmac.compare_digest(s["pair"], text):
                s.update(owner=oid, pair=None)
                return True
            return False
        # 非文字消息及配对失败的请求不下载任何附件。
        try:
            text = (json.loads(m.get("content") or "{}").get("text") or "").strip() if m.get("message_type") == "text" else ""
        except (AttributeError, TypeError, ValueError):
            return
        if p2p and text.isascii() and update(pair):
            send(m["chat_id"], "text", "绑定好了，以后只听你的。")
            send(m["chat_id"], "interactive", _pick_card())
        elif p2p:
            send(m["chat_id"], "text", "我还没绑定主人。请在电脑上运行 agentbus feishu code 拿配对码，把配对码发给我。")
        return
    if oid != st["owner"]:
        if p2p:
            send(m["chat_id"], "text", "我只听主人的。")
        return
    text, files = _text_files(m)
    if p2p:
        return on_p2p(m, text)
    project = st["groups"].get(m["chat_id"])
    if not project:
        send(m["chat_id"], "text", "这个群还没对应项目。私聊我选项目，我会给项目建好群。")
        return
    key = m.get("root_id") or m["message_id"]
    if key not in st["chats"]:  # 群里直接发的一句话（或回复了不认识的消息）：开一个新聊天
        if text.strip() == "新聊天":
            return _post_new_chat(project)
        g = _gmeta(m["chat_id"])
        mode = g.get("mode") or "lead"
        _new_chat(project, m["chat_id"], key, mode, g.get("lead"))
        if mode != "lead":
            import chat
            chat.set_meta(_channel(key), mode="talk" if mode == "talk" else "work", duty="none",
                          **({"members": g.get("members") or _talkers(project)} if mode == "talk" else {}))
            _bar(key)
    if _chat_command(key, text):
        return
    c = state()["chats"][key]
    import chat
    if c["mode"] == "lead":
        if c.get("channel"):  # 记进聊天室的记录里，切回聊天室时也看得到
            chat.append(c["channel"], "user", text or "（看附件）", files=files, to_lead=True)
        wake_lead(key, owner=text or "（看附件）", files=files)
    else:
        if c["mode"] == "work" and not chat.mentions(text):  # 没写 @：认名字/简称，再不行就发给成员栏选中的那几位
            who = _named(text, c["project"]) or [a for a in c.get("targets") or [] if a in _team()]
            if who:
                text = " ".join(f"@{_name(a)}" for a in who) + " " + text
        chat.send(_channel(key), text or "（看附件）", frm="user", cwd=_root(c["project"]), files=files)
        _bar(key)


# ---------- 私聊 = agentbus 主界面：选项目（选了就开聊）、新建项目、额度、账单 ----------
def on_p2p(m, text):
    t = text.strip().lstrip("/")
    to = lambda kind, p: send(m["chat_id"], kind, p)  # noqa: E731
    o = re.match(r"^打开\s+(/.+)$", t)
    if o:
        _open(o.group(1).strip(), m["chat_id"])
    elif t == "额度":
        to("interactive", _quota_card())
    elif t == "账单":
        to("interactive", _bill_card())
    elif t in ("出门", "出门模式"):
        to("interactive", _toggle_away())
    else:
        to("interactive", _pick_card())


def _root(project):
    p = projects().get(project)
    return p.get("root") if isinstance(p, dict) else None


def _folders():
    """选项目用：登记过的项目 + 最近在电脑上干活用过的文件夹（agentbus 接入记录、Codex 会话），上次用的排最前。"""
    seen, out = set(), []

    def add(path, name=None):
        try:
            d = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return
        if str(d) in seen or d in (Path.home(), Path("/")) or not d.is_dir() or HOME in (d, *d.parents) or "/tmp" in str(d):
            return
        seen.add(str(d))
        out.append((name or d.name, str(d)))

    last = state().get("last_project")
    if _root(last or ""):
        add(_root(last), last)
    for name, p in projects().items():
        if not name.startswith("_") and isinstance(p, dict) and p.get("root"):
            add(p["root"], name)
    for f in ("joined.json", "rooms.json"):
        rows = sorted((load_json(HOME / f, {}) or {}).values(), key=lambda v: -(v.get("t", 0) if isinstance(v, dict) else 0))
        for v in rows[:40]:
            if isinstance(v, dict) and v.get("cwd"):
                add(v["cwd"])
    for f in sorted((Path.home() / ".codex" / "sessions").glob("*/*/*/*.jsonl"), key=lambda p: p.name, reverse=True)[:150]:
        try:
            with open(f, encoding="utf-8") as fh:
                cwd = (json.loads(fh.readline()).get("payload") or {}).get("cwd")
        except (OSError, ValueError):
            continue
        if cwd:
            add(cwd)
    return out[:8]


def _parents():
    """新建项目放哪：现有项目所在的上级文件夹 + 桌面、文稿、用户目录。上次用的项目的上级排最前。"""
    out, seen = [], set()

    def add(p, label=None):
        p = Path(p).expanduser()
        if str(p) not in seen and p.is_dir():
            seen.add(str(p))
            out.append(("用户目录" if p == Path.home() else label or p.name or str(p), str(p)))

    if state().get("new_root"):
        add(state()["new_root"])
    last = _root(state().get("last_project") or "")
    if last:
        add(Path(last).parent)
    for name, p in projects().items():
        if not name.startswith("_") and isinstance(p, dict) and p.get("root") and "/tmp" not in p["root"]:
            add(Path(p["root"]).parent)
    add(Path.home() / "Desktop", "桌面")
    add(Path.home() / "Documents", "文稿")
    add(Path.home(), "用户目录")
    return out[:8]


def _pick_card():
    els = []
    for name, path in _folders():
        els.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center",
             "elements": [_md(f"**{name}**\n<font color='grey'>{path}</font>", "project")]},
            {"tag": "column", "width": "auto", "vertical_align": "center",
             "elements": [_btn("开聊", {"act": "open", "path": path}, "primary")]}]})
    els.append({"tag": "hr"})
    els.append(_row([{"label": "📂 浏览文件夹 / 新建项目", "type": "primary", "value": {"act": "cd", "path": _parents()[0][1]}}]))
    els.append({"tag": "hr"})
    import away
    on = away.state().get("on")
    els.append(_row([{"label": "额度", "value": {"act": "quota"}}, {"label": "账单", "value": {"act": "bill"}},
                     {"label": "🧳 出门模式：开着" if on else "🧳 出门模式", "type": "primary_filled" if on else "default", "value": {"act": "away"}}]))
    return _card("选一个项目", elements=els, sub="选好就进项目群，开一个新聊天", icon=_bus_icon())


def _subdirs(d):
    try:
        return sorted((x for x in Path(d).iterdir() if x.is_dir() and not x.name.startswith((".", "$", "~"))), key=lambda x: x.name.lower())
    except OSError:
        return []


def _dir_row(x, indent=False):
    """一行一个文件夹：左边名字（有子文件夹的标出几个），右边「进入」。"""
    n = len(_subdirs(x))
    md = x.name + (f"　<font color='grey'>{n} 个子文件夹</font>" if n else "")
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "margin": "0px", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [_md(md, "project")]},
        {"tag": "column", "width": "auto", "vertical_align": "center", "elements": [_btn("进入 ›", {"act": "cd", "path": str(x)}, "text")]}]}


def _count(o):
    """数一数卡片里有多少个元素（带 tag 的都算），别超过飞书的上限。"""
    if isinstance(o, dict):
        return ("tag" in o) + sum(_count(v) for v in o.values())
    return sum(_count(v) for v in o) if isinstance(o, list) else 0


def _kid(x):
    """子分类里的一项：整行可点（文字靠左），点了直接跳进去。"""
    return {"tag": "interactive_container", "width": "fill", "height": "auto", "has_border": False, "corner_radius": "6px",
            "padding": "4px 8px 4px 8px", "behaviors": [{"type": "callback", "value": {"act": "cd", "path": str(x)}}],
            "elements": [_md(f"{x.name}　<font color='grey'>›</font>", "project")]}


def _browse_card(path):
    """像访达一样一层层点进去：上面下拉选常用位置；中间一行一个子文件夹，有下一层的可以展开看一眼（子分类）；
    下面「就用这个开聊」或「在这里新建」。"""
    d = Path(path).expanduser()
    if not d.is_dir():
        d = Path.home()
    subs = _subdirs(d)
    places = _places()
    els = [{"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "auto", "vertical_align": "center", "elements": [{"tag": "markdown", "content": "**位置**"}]},
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [
            {"tag": "select_static", "placeholder": {"tag": "plain_text", "content": "常用位置"}, "width": "fill",
             "initial_option": next((p for _, p in places if Path(p) == d), None),
             "options": [{"text": {"tag": "plain_text", "content": n}, "value": p} for n, p in places],
             "behaviors": [{"type": "callback", "value": {"act": "cd"}}]}]}]}]
    crumbs = " / ".join(d.parts[-3:]) if len(d.parts) > 3 else str(d)
    head = [{"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center",
             "elements": [{"tag": "markdown", "content": f"📂 **{d.name or str(d)}**\n<font color='grey'>{crumbs}</font>"}]}]
    if d.parent != d:
        head.append({"tag": "column", "width": "auto", "vertical_align": "center", "elements": [_btn("⬆ 上一级", {"act": "cd", "path": str(d.parent)})]})
    els.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": head})
    els.append({"tag": "hr"})
    shown = 0
    for x in subs[:12]:  # 飞书一张卡片最多 200 个元素：一层最多 12 个，子分类里每个最多 6 个，快满了就停
        if _count(els) > 150:
            break
        shown += 1
        kids = _subdirs(x)
        if kids:  # 有下一层：做成可展开的子分类，展开能直接跳进孙文件夹
            els.append({"tag": "collapsible_panel", "expanded": False, "vertical_spacing": "4px", "padding": "4px 8px 4px 8px",
                        "header": {"title": {"tag": "markdown", "content": f"**{x.name}**　<font color='grey'>{len(kids)} 个子文件夹</font>"},
                                   "vertical_align": "center", "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
                                   "icon_position": "right", "icon_expanded_angle": -180},
                        "border": {"color": "grey", "corner_radius": "6px"},
                        "elements": [_kid(k) for k in kids[:6]] +
                                    ([{"tag": "markdown", "content": f"<font color='grey'>还有 {len(kids) - 6} 个，进去看全部</font>"}] if len(kids) > 6 else []) +
                                    [dict(_btn(f"进入「{x.name[:12]}」", {"act": "cd", "path": str(x)}, "primary"), width="fill")]})
        else:
            els.append(_dir_row(x))
    if len(subs) > shown:
        els.append({"tag": "markdown", "content": f"<font color='grey'>还有 {len(subs) - shown} 个没列出来：{'、'.join(x.name for x in subs[shown:shown + 15])}…\n发「打开 /完整路径」可以直接打开</font>"})
    if not subs:
        els.append({"tag": "markdown", "content": "<font color='grey'>这里面没有子文件夹</font>"})
    els.append({"tag": "hr"})
    els.append({"tag": "form", "name": "newp", "elements": [
        {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [
                {"tag": "input", "name": "pname", "placeholder": {"tag": "plain_text", "content": "在这里新建项目：填名字"}, "required": True}]},
            {"tag": "column", "width": "auto", "vertical_align": "center", "elements": [
                _btn("新建并开聊", {"act": "create", "parent": str(d)}, "primary_filled", submit=True)]}]}]})
    els.append(_row([{"label": f"就用「{(d.name or str(d))[:14]}」开聊", "type": "primary", "value": {"act": "open", "path": str(d)}}]))
    return _card("选位置", elements=els, sub=str(d), icon=_bus_icon())


def _places():
    """浏览文件夹的起点：常用的上级文件夹、外接硬盘、桌面、文稿、用户目录。"""
    out, seen = [], set()
    for label, p in _parents():
        if p not in seen:
            seen.add(p)
            out.append((label, p))
    vols = Path("/Volumes")
    if vols.is_dir():
        for v in sorted(vols.iterdir()):
            if v.is_dir() and not v.is_symlink() and str(v) not in seen and not v.name.startswith("."):
                seen.add(str(v))
                out.append((v.name, str(v)))
    return out[:8]


def _away_card(st):
    """出门模式的状态：开没开、合盖睡不睡、电源 / 代理 / 硬盘 / 飞书各在不在线。"""
    lines = [f"{'✅' if c['ok'] else '⚠️'} **{c['name']}**　<font color='grey'>{c['detail']}</font>" for c in st["checks"]]
    if st["on"]:
        head = "电脑保持唤醒" + ("，合盖也不睡" if st.get("lid") else "")
        if st["laptop"] and not st.get("lid"):
            head += "\n⚠️ 合盖还会睡：要先在电脑上点一次悬浮窗的「出门模式」，输一次开机密码授权（以后不用再输）。出门别合盖"
    else:
        head = "已关"
    return _card("🧳 出门模式" + ("：开着" if st["on"] else "：关了"), head + "\n\n" + "\n".join(lines), "orange" if st["on"] else "grey",
                 [{"label": "关掉" if st["on"] else "打开", "type": "default" if st["on"] else "primary", "value": {"act": "away"}}],
                 icon=_bus_icon(), sub="锁屏没关系，大部分活照常干；录屏这类要看屏幕的等你回来")


def _toggle_away():
    import away
    return _away_card(away.set_on())


def _new_project(name, parent, dm):
    name = re.sub(r"[/\\:\0]", "", name or "").strip()
    if name in ("", ".", ".."):
        return send(dm, "text", "项目名无效。")
    d = Path(parent or _parents()[0][1]) / name
    if d.exists() and not d.is_dir():
        return send(dm, "text", f"{d} 已经有同名文件了，换个名字。")
    d.mkdir(parents=True, exist_ok=True)
    update(lambda s: s.update(new_root=str(d.parent)))
    _open(str(d), dm, new=True)


def _open(path, dm, new=False):
    """打开一个文件夹：登记成项目（已登记的用原名），然后在私聊里选模式（和主位 / 群聊成员），点「进群开聊」进对应的群。"""
    from config import project_of_dir
    d = Path(path).expanduser()
    if not d.is_dir():
        return send(dm, "text", f"找不到文件夹：{path}")
    name = next((k for k, p in projects().items() if isinstance(p, dict) and p.get("root") and Path(p["root"]).resolve() == d.resolve()), None) \
        or project_of_dir(str(d))
    if not name or name == "通用":
        return send(dm, "text", f"这个文件夹不能当项目：{path}")
    update(lambda s: s.update(last_project=name))
    send(dm, "interactive", _launcher_card(name, new))


# ---------- 群：一个「项目 + 模式（+ 主位）」一个群 ----------
#   助理：「助理 · 主位名 · 📁 项目」，头像是主位的 Q 版头像（+ 文件夹角标）；选不同主位就是不同的群，互不影响
#   派活：「项目 · 派活」，头像是一圈队员；群聊：「项目 · 群聊」，头像是拉进来的那几位拼在一起
MODES = {"lead": "助理", "work": "派活", "talk": "群聊"}
MODE_COLOR = {"lead": "indigo", "work": "turquoise", "talk": "violet"}  # 一眼分清：助理靛蓝、派活青绿、群聊紫


def _gmeta(cid):
    st = state()
    return (st.get("gmeta") or {}).get(cid) or {"project": st["groups"].get(cid), "mode": "lead", "lead": _group_lead(st["groups"].get(cid) or "")}


FOLDER = "📁"  # 群名里项目前面的文件夹标识（飞书群名只能放文字，线条文件夹符号在苹果设备上显示不出来）


def _group_name(m):
    """模式在前：「助理 · GPT · 📁 示例项目」「派活 · 📁 示例项目」「群聊 · 📁 示例项目」。"""
    return " · ".join([MODES[m["mode"]]] + ([_name(m["lead"])] if m["mode"] == "lead" else []) + [f"{FOLDER} {m['project']}"])


def _group_lead(project):
    return (state().get("group_lead") or {}).get(project) or _lead()


def _group_mode(project):
    return (state().get("group_mode") or {}).get(project) or "lead"


def _group_for(project, mode, lead=None):
    """找这个「项目 + 模式（+ 主位）」的群，没有就建一个（起好名字、换好头像、发一张欢迎卡片）。"""
    with _group_lock:
        return _find_or_create_group(project, mode, lead)


def _find_or_create_group(project, mode, lead):
    st = state()
    for cid, m in (st.get("gmeta") or {}).items():
        if m.get("project") == project and m.get("mode") == mode and (mode != "lead" or m.get("lead") == lead):
            return cid
    m = {"project": project, "mode": mode, "lead": lead if mode == "lead" else None, "members": _talkers(project) if mode == "talk" else None}
    d = api("POST", "/open-apis/im/v1/chats", {"name": _group_name(m), "user_id_list": [st["owner"]],
                                               "group_message_type": "thread", "description": f"agentbus 项目：{project}"},
            {"user_id_type": "open_id", "set_bot_manager": "true"})
    cid = (d.get("data") or {}).get("chat_id")
    if cid:
        update(lambda s: (s["groups"].__setitem__(cid, project), s.setdefault("gmeta", {}).__setitem__(cid, m)))
        try:
            _brand_group(cid)
        except (ImportError, OSError, ValueError, RuntimeError):
            LOG.warning("没能设置飞书群头像")
    return cid


def _brand_group(cid):
    import feishu_art
    m = _gmeta(cid)
    face = lambda a: ((_agents().get(a) or {}).get("avatar"), (_agents().get(a) or {}).get("color"))  # noqa: E731
    if m["mode"] == "lead":
        img = feishu_art.group_avatar(*face(m["lead"]))
    elif m["mode"] == "work":
        img = feishu_art.ring_avatar([face(a) for a in _team()[:8]])
    else:
        img = feishu_art.collage_avatar([face(a) for a in (m.get("members") or [])])
    key = _imgkey(img, "avatar")
    api("PUT", f"/open-apis/im/v1/chats/{cid}", {"name": _group_name(m), **({"avatar": key} if key else {})})


def _welcome_card(cid):
    m = _gmeta(cid)
    tip = "点下方「新建话题」写第一句话，就开一个新对话\n<font color='grey'>也可以直接回复这张卡片</font>"
    if m["mode"] == "lead":
        body = f"跟主位 **{_name(m['lead'])}** 聊，它负责拆任务、在后台派活，队员交活后它核对完告诉你。\n\n" + tip
    elif m["mode"] == "work":
        body = "写队员的名字或简称（GPT、反重力、oc…）就派给他；话题底部的成员栏可以多选默认发给谁。值班的只记录，不插话。\n\n" + tip
    else:
        body = f"在聊：**{'、'.join(_name(a) for a in m.get('members') or []) or '还没人'}**。你说一句，他们按顺序接着聊（只聊不动手）。\n\n" + tip
    return _card(_group_name(m), body, MODE_COLOR[m["mode"]], icon=_face(m["lead"]) if m["mode"] == "lead" else _bus_icon())


def _new_chat(project, chat_id, key, mode="lead", lead=None):
    c = {"project": project, "chat_id": chat_id, "mode": mode, "lead": lead or _group_lead(project), "conv": "fs-" + secrets.token_hex(5)[:9],
         "channel": None, "t": time.time()}
    update(lambda s: (s["chats"].__setitem__(key, c), s.update(last_project=project)))
    return c


def _post_new_chat(project):
    """（群里发「新聊天」时）把这个项目上次用的模式的群里再发一张欢迎卡片。"""
    cid = _group_for(project, _group_mode(project), _group_lead(project))
    if cid:
        send(cid, "interactive", _welcome_card(cid))


def _launcher_card(project, new=False):
    """私聊里选好项目之后：① 选模式（助理还要选主位，群聊要选拉谁）② 点「进群开聊」，进这个组合对应的群。"""
    mode, lead = _group_mode(project), _group_lead(project)
    btns = [{"label": v, "type": "primary_filled" if k == mode else "default", "value": {"act": "gmode", "project": project, "mode": k}}
            for k, v in MODES.items()]
    els = [{"tag": "markdown", "content": "**① 选模式　② 点「进群开聊」**，进群后点「新建话题」写第一句话就开一个新对话"},
           _row(btns, "模式", "mode"), {"tag": "hr"}]
    if mode == "lead":
        els.append(_md("**主位**　<font color='grey'>每位主位一个群：「助理 · 主位名 · 📁 项目」</font>", "lead"))
        els += _grid([(a, _btn(_name(a), {"act": "glead", "project": project, "lead": a}, "primary_filled" if a == lead else "default"))
                      for a in _team()[:8]])
    elif mode == "work":
        els.append(_md("**派活**　<font color='grey'>写队员的名字或简称就派给他，话题底部成员栏可以多选。群叫「项目 · 派活」</font>", "team"))
    else:
        sel = _talkers(project)
        els.append(_md("**拉谁进来聊**　<font color='grey'>可以多选；群叫「项目 · 群聊」，群头像就是这几位</font>", "team"))
        els += _grid([(a, _btn(("✓ " if a in sel else "") + _name(a), {"act": "gtalk", "project": project, "aid": a},
                                "primary_filled" if a in sel else "default")) for a in _team()[:8]])
    target = _group_name({"project": project, "mode": mode, "lead": lead})
    els += [{"tag": "hr"}, _row([{"label": f"进群开聊：{target}", "type": "primary_filled", "value": {"act": "go", "project": project}}])]
    return _card(f"{'新建好了' if new else '打开了'}「{project}」", elements=els, sub=str(_root(project) or ""),
                 icon=_face(lead) if mode == "lead" else _bus_icon(), color=MODE_COLOR[mode])


def _go(project, dm):
    mode, lead = _group_mode(project), _group_lead(project)
    cid = _group_for(project, mode, lead)
    if not cid:
        return send(dm, "text", "建群失败了，看一下电脑上 agentbus 的日志。")
    if mode == "talk":  # 群聊换了人：只更新这一个群的成员和头像
        mem = _talkers(project)
        if _gmeta(cid).get("members") != mem:
            update(lambda s: s["gmeta"][cid].__setitem__("members", mem))
            _brand_group(cid)
    send(cid, "interactive", _welcome_card(cid))
    send(dm, "text", f"去群「{_group_name(_gmeta(cid))}」，点「新建话题」写第一句话就开始。")


def _toggle(lst, x):
    return [a for a in lst if a != x] if x in lst else lst + [x]


def _set_group(project, mode=None, lead=None):
    if mode in MODES:
        update(lambda s: s.setdefault("group_mode", {}).__setitem__(project, mode))
    if lead in _team():
        update(lambda s: s.setdefault("group_lead", {}).__setitem__(project, lead))
    return _launcher_card(project)


def _head(key, mid=None):
    """聊天的头卡片（只在聊天里发「模式」「主位」时出）：说明这个聊天是什么模式、谁当主位。模式和主位由群决定。"""
    c = state()["chats"][key]
    m = _gmeta(c["chat_id"])
    body = "这个群的模式和主位是固定的。要换：私聊机器人，选项目后选别的模式或主位，进对应的群。"
    card = _card(_group_name({**m, "lead": c["lead"]}), body, MODE_COLOR[c["mode"]], icon=_face(c["lead"]) if c["mode"] == "lead" else _bus_icon())
    if mid:
        patch_card(mid, card)
    else:
        mid = reply(key, "interactive", card)
    update(lambda s: s["head"].__setitem__(key, mid))


def _mode_card(key):
    """选了模式之后弹的那张：助理 → 选主位；派活 → 能 @ 的队员；群聊 → 一起聊的队员。"""
    c = state()["chats"][key]
    if c["mode"] == "lead":
        cells = [(a, _btn(_name(a), {"act": "lead", "key": key, "lead": a}, "primary_filled" if a == c["lead"] else "default")) for a in _team()[:8]]
        return _card("选主位", elements=_grid(cells), sub="它陪你聊、拆任务、在后台派活", icon=_face(c["lead"]))
    if c["mode"] == "work":
        cells = [(a, {"tag": "markdown", "content": f"@{_name(a)}", "text_align": "center"}) for a in _team()[:8]]
        return _card("派活", "直接写 **@队员名 要做的事**，谁被 @ 谁干，干完直接回你。值班的只记录，不插话。", elements=_grid(cells), icon=_bus_icon())
    import chat
    cells = [(a, {"tag": "markdown", "content": _name(a), "text_align": "center"}) for a in chat.talk_members(_channel(key))]
    return _card("群聊", "你说一句，下面这些队员按顺序接着聊（只聊不动手）。想让谁干活就 @ 他。", elements=_grid(cells), icon=_bus_icon())


def _grid(cells, per=4, px=56):
    """头像格子：一行 4 个，每格上面头像下面一个按钮或名字。"""
    rows = []
    for i in range(0, len(cells), per):
        cols = []
        for aid, under in cells[i:i + per]:
            face = _face(aid, 112)
            cols.append({"tag": "column", "width": "weighted", "weight": 1, "horizontal_align": "center",
                         "elements": ([_pic(face, f"{px}px {px}px")] if face else []) + [under]})
        cols += [{"tag": "column", "width": "weighted", "weight": 1, "elements": []} for _ in range(per - len(cols))]
        rows.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": cols})
    return rows


ALIASES = {"codex": ["GPT", "codex"], "gemini": ["反重力", "Antigravity", "AG", "Gemini"], "opencode": ["oc", "opencode"],
           "zcode": ["zc", "zcode"], "deepseek": ["ds", "DeepSeek"], "claude": ["Claude"]}


def _named(text, project=""):
    """不写 @ 时，话里点了谁的名字/简称就派给谁（GPT、反重力、oc……）。和项目名重的词不算，免得聊「豆包项目」就派给了豆包。"""
    cfg_agents = agents_dict(load_config())
    out = []
    for aid in _team():
        keys = {aid, _name(aid), *(cfg_agents.get(aid, {}).get("aliases") or []), *ALIASES.get(aid, [])}
        for k in sorted(keys, key=len, reverse=True):
            if len(k) < 2 or (project and k.lower() in project.lower()):
                continue
            hit = (k in text) if re.search(r"[^\x00-\x7f]", k) else re.search(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])", text, re.I)
            if hit:
                out.append(aid)
                break
    return out


def _bar(key):
    """聊天室模式的底部成员栏：每次都删掉上一个、在最下面重发，像聊天室底部那排 @ 按钮。"""
    c = state()["chats"].get(key)
    if not c or c["mode"] == "lead":
        return
    old = c.get("bar")
    if old:
        api("DELETE", f"/open-apis/im/v1/messages/{old}")
    mid = reply(key, "interactive", _bar_card(key))
    update(lambda s: s["chats"][key].__setitem__("bar", mid))


def _bar_card(key):
    import chat
    c = state()["chats"][key]
    if c["mode"] == "work":
        sel = [a for a in c.get("targets") or [] if a in _team()]
        cells = [(a, _btn(("✓ " if a in sel else "") + _name(a), {"act": "target", "key": key, "aid": a}, "primary_filled" if a in sel else "default"))
                 for a in _team()[:8]]
        hint = (f"没写名字的话发给 **{'、'.join(_name(a) for a in sel)}**（可多选，再点取消）" if sel
                else "可以多选：点了的人收你没写名字的话；直接写名字或简称（GPT、反重力、oc…）也行")
    else:
        sel = chat.talk_members(_channel(key))
        cells = [(a, _btn(("✓ " if a in sel else "") + _name(a), {"act": "member", "key": key, "aid": a}, "primary_filled" if a in sel else "default"))
                 for a in _team()[:8]]
        hint = f"在聊：**{'、'.join(_name(a) for a in sel) or '还没人'}**（点头像拉人进来或请出去）；想让谁动手就 @ 他"
    return _card(f"聊天室 · {MODES[c['mode']]}", f"<font color='grey'>{hint}</font>", MODE_COLOR[c["mode"]], elements=_grid(cells, 4, 40))


def _talkers(project):
    """群聊默认拉谁：入口卡片上选过的，没选过就用 bus 群聊的默认（主位办公室前 4 位）。"""
    import chat
    got = (state().get("group_talk") or {}).get(project)
    return [a for a in got if a in _team()] if got else chat.talk_members("")


def _chat_command(key, text):
    t = text.strip().lstrip("/")
    if t in ("模式", "主位", "切换模式", "换主位"):
        _head(key)
        return True
    if t == "新聊天":
        _post_new_chat(state()["chats"][key]["project"])
        return True
    return False


def _set_mode(key, mode):
    import chat
    c = state()["chats"].get(key)
    if not c or mode not in MODES:
        return
    if mode != "lead":
        chat.set_meta(_channel(key), mode="talk" if mode == "talk" else "work", duty="none")
    update(lambda s: s["chats"][key].update(mode=mode))
    hm = state()["head"].get(key)
    _head(key, hm) if hm else _head(key)
    if mode == "lead":
        reply(key, "interactive", _mode_card(key))
    else:
        _bar(key)


def _set_lead(key, lead):
    c = state()["chats"].get(key)
    if not c or lead == c["lead"] or lead not in _team():
        return
    # 换主位 = 换一个窗口：新主位从头开始，先接入 bus。这个项目以后的新聊天也默认它，群名和群头像跟着换
    update(lambda s: s["chats"][key].update(lead=lead, conv="fs-" + secrets.token_hex(5)[:9], briefed=False))
    hm = state()["head"].get(key)
    _head(key, hm) if hm else _head(key)
    reply(key, "interactive", _card(f"主位换成 {_name(lead)} 了", "接下来跟它聊。", icon=_face(lead)))


def _channel(key):
    """聊天室模式用的频道：第一次切到聊天室时建，名字带项目，和电脑上消息窗口里看到的一样。"""
    with _channel_lock:
        return _ensure_channel(key)


def _ensure_channel(key):
    import chat
    c = state()["chats"][key]
    if c.get("channel"):
        return c["channel"]
    base = f"{c['project']}·飞书{time.strftime('%m%d-%H%M')}"[:26]
    base = re.sub(r"[/\\:\0]", "-", base).lstrip(".")
    names = {x["name"] for x in chat.channels()} | set(load_json(chat.CHAT / "channels.json", {}))
    ch, n = base, 2
    while ch in names:
        ch, n = f"{base}-{n}", n + 1
    chat.set_meta(ch, mode="work", duty="none", feishu=True, project=c["project"])
    last = (chat.read(ch, 1) or [{}])[-1].get("id")
    update(lambda s: (s["chats"][key].update(channel=ch), s["cursor"].__setitem__(ch, last)))
    return ch


def _lead():
    return state().get("lead") or "codex"


def _name(aid):
    return (_agents().get(aid) or {}).get("name") or (agents_dict(load_config()).get(aid) or {}).get("name") or aid


def _team():
    """能派活的队员：和 agentbus 界面上看到的一致（隐藏的杂活号、封印的、只能传话的不列）。"""
    shown = _agents()
    return [aid for aid, a in agents_dict(load_config()).items() if not sealed(a) and not a.get("hidden") and a.get("run")
            and a.get("billing") != "manual" and (not shown or aid in shown)]


# ---------- 头像、图片 ----------
_ainfo = {"t": 0, "v": {}, "q": {}}


def _agents():
    """队员的名字、头像、主题色、额度：直接问本机 agentbus 服务（和界面看到的一样），一分钟刷新一次。"""
    if not getattr(_callback, "active", False) and time.time() - _ainfo["t"] > 60:
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            d = json.loads(op.open(f"http://127.0.0.1:{os.environ.get('AGENTBUS_PORT', '8790')}/api/state", timeout=20).read())
            _ainfo.update(t=time.time(), v={a["id"]: a for a in d.get("agents") or []}, q=d.get("quota") or {})
        except (OSError, ValueError):
            _ainfo["t"] = time.time() - 50
    return _ainfo["v"]


def _imgkey(data, kind="message"):
    """上传一张图拿 image_key；同一张图只传一次（按内容记在 feishu.json）。"""
    h = hashlib.sha1(data).hexdigest()[:16] + kind[0]
    k = (state().get("imgs") or {}).get(h)
    if k or getattr(_callback, "active", False):
        return k
    b = "----agentbus" + secrets.token_hex(8)
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\n{kind}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a.png\"\r\n"
            f"Content-Type: image/png\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    k = (api("POST", "/open-apis/im/v1/images", body, headers={"Content-Type": f"multipart/form-data; boundary={b}"}).get("data") or {}).get("image_key")
    if k:
        update(lambda s: s.setdefault("imgs", {}).__setitem__(h, k))
    return k


def _face(aid, size=96):
    import feishu_art
    a = _agents().get(aid) or {}
    try:
        return _imgkey(feishu_art.png(feishu_art.avatar(a.get("avatar"), size, a.get("color"))))
    except Exception:  # noqa: BLE001  头像出不来就不放，不影响卡片
        return None


def _ui_dir():
    if os.environ.get("AGENTBUS_UI"):
        return Path(os.environ["AGENTBUS_UI"])
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return executable.resolve().parent.parent / "ui"
    return Path(__file__).resolve().parent.parent / "ui"


def _bus_icon():
    ui = _ui_dir()
    f = ui / "assets" / "app-icon.png"
    try:
        return _imgkey(f.read_bytes()) if f.exists() else None
    except (OSError, ValueError, RuntimeError):
        return None


# ---------- 额度和消耗 ----------
def _left(aid):
    """这位队员还剩多少：(0~1 或 None, 一句话)。GPT 看 5 小时/本周里用得多的那个；按钱算的写余额；反重力这类看模型窗口。"""
    _agents()
    a, q = _ainfo["v"].get(aid) or {}, _ainfo["q"]
    b = a.get("billing")
    if b == "codex" and (q.get("codex") or {}).get("ok"):
        used = max(q["codex"].get("five_hour") or 0, q["codex"].get("weekly") or 0)
        return 1 - used / 100, f"剩 {100 - used:.0f}%"
    if b == "deepseek" and (q.get("deepseek") or {}).get("ok"):
        accs = q["deepseek"].get("accounts") or []
        bal = sum(x.get("balance") or 0 for x in accs if aid in (x.get("used_by") or [])) or q["deepseek"].get("total")
        return None, f"余额 ¥{bal:.1f}" if bal is not None else ""
    res = (a.get("balance") or {}).get("result") or {}
    vals = [w["remain"] for w in res.get("windows") or [] if w.get("remain") is not None]
    if res.get("ok") and vals:
        v = max(set(vals), key=vals.count)
        return v, f"剩 {v * 100:.0f}%"
    if res.get("ok") and res.get("total") is not None:
        return None, f"余额 ¥{res['total']:.1f}"
    return None, ""


def _cost(rows):
    """几单活合起来花了多少：按钱的加钱，按额度的加百分比。"""
    cny = sum((r.get("cost") or {}).get("cny") or 0 for r in rows)
    pct = sum(max((r.get("cost") or {}).get("five_hour") or 0, (r.get("cost") or {}).get("weekly") or 0) for r in rows)
    parts = ([f"¥{cny:.2f}"] if cny else []) + ([f"+{pct:.0f}%"] if pct else [])
    return " ".join(parts) or "<1%"


def _usage_strip(c):
    """这一轮（上次结算之后）这个窗口用到的队员：每人一行小条，挂在主位回复的最下面。"""
    import feishu_art
    since = c.get("billed", c.get("t", 0))
    rows = [r for r in _ledger_rows() if r.get("conv") == c["conv"][:12] and r.get("end", 0) > since]
    agents = list(dict.fromkeys([c["lead"]] + [r["agent"] for r in rows]))
    out = []
    for aid in agents:
        a = _agents().get(aid) or {}
        pct, left = _left(aid)
        mine = [r for r in rows if r["agent"] == aid]
        out.append({"avatar": a.get("avatar"), "color": a.get("color"), "name": _name(aid), "pct": pct,
                    "right": " · ".join(x for x in (f"本轮 {_cost(mine)}" if mine else "", left) if x)})
    try:
        return _imgkey(feishu_art.bars(out)) if out else None
    except Exception:  # noqa: BLE001
        LOG.exception("画消耗条出错")
        return None


def _quota_card():
    import feishu_art
    rows = []
    for aid in _team():
        pct, left = _left(aid)
        if left:
            a = _agents().get(aid) or {}
            rows.append({"avatar": a.get("avatar"), "color": a.get("color"), "name": _name(aid), "pct": pct, "right": left})
    els = [_pic(_imgkey(feishu_art.bars(rows, big=True)))] if rows else []
    return _card("全部额度", "" if rows else "还没有读到额度。", elements=els, icon=_bus_icon(), sub=time.strftime("%m-%d %H:%M 更新"))


def _bill_card():
    import feishu_art
    from config import ledger_read
    t0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    rows = [r for r in ledger_read() if not r.get("type") and r.get("end", 0) >= t0]
    out, projs = [], {}
    for aid in dict.fromkeys(r["agent"] for r in rows):
        mine = [r for r in rows if r["agent"] == aid]
        a = _agents().get(aid) or {}
        pct, _ = _left(aid)
        out.append({"avatar": a.get("avatar"), "color": a.get("color"), "name": _name(aid), "pct": pct,
                    "right": f"{len(mine)} 单 · {_cost(mine)}"})
    for r in rows:
        projs.setdefault(r.get("project") or "—", []).append(r)
    md = "\n".join(f"- **{p}**：{len(v)} 单 · {_cost(v)}" for p, v in projs.items()) or "今天还没有派过活。"
    els = [_pic(_imgkey(feishu_art.bars(out, big=True)))] if out else []
    return _card("今天的账单", md, elements=els, icon=_bus_icon(), sub="按队员和项目合计（进度条是剩余额度）")


# ---------- 助理：这个聊天就是一个接入了 bus 的对话窗口 ----------
JOIN = ("（这是主人在飞书上新开的对话窗口，主人在手机上看，回复写短一点。先接入 agentbus：调用 agentbus_hello 工具，explicit 设为 true；"
        "没有这个工具就在终端运行 `agentbus hello --agent {lead}`。然后照握手信息干活。）")


def wake_lead(key, owner=None, files=None, echo=None):
    item = {"kind": "owner", "text": owner, "files": [f["path"] for f in files or []]} if owner is not None else {"kind": "echo", "text": echo}
    update(lambda s: s["queue"].setdefault(key, []).append(item))
    _kick(key)


def _kick(key, retry_job=None):
    """同一个窗口一次只跑一轮。付费重试只重放对应工单的原请求。"""
    import mcp_server
    with _kick_lock:
        st = state()
        c = st["chats"].get(key)
        retry = st["lead_retry"].get(key) or {}
        if not st.get("enabled") or not c or st["lead_job"].get(key):
            return
        if retry_job and (retry.get("job") != retry_job or retry.get("conv") != c["conv"]):
            return
        items = retry.get("items") if retry_job else st["queue"].get(key)
        if not items:
            return
        parts = [] if c.get("briefed") else [JOIN.format(lead=c["lead"])]
        last = st["mark"].get(key)
        if c.get("channel") and not retry_job:
            side, last = _side(c["channel"], last)
            if side:
                parts.append("（聊天室模式下，主人在这个聊天里直接安排队员做的事，知道就行，不用总结：\n" + side + "）")
        for item in items:
            parts.append(item["text"] + "".join(f"\n附件：{f}" for f in item.get("files") or []))
        error = None
        try:
            jid = mcp_server.start_job({"agent": c["lead"], "task": "\n\n".join(parts), "new": not c.get("briefed"),
                                        "project": c["project"], "allow_credits": bool(retry_job)},
                                       env_extra={"AGENTBUS_CONV": c["conv"], "AGENTBUS_WINDOW": "1", "AGENTBUS_FEISHU": key,
                                                  "AGENTBUS_SELF": "user"},
                                       cwd=_root(c["project"]))
        except (OSError, ValueError) as e:
            error = e
            if not retry_job:
                update(lambda ss: ss["queue"].get(key, []).__delitem__(slice(0, len(items))))
        else:
            def started(ss):
                chat = ss["chats"].get(key)
                if not chat or chat["conv"] != c["conv"]:
                    return
                if retry_job:
                    ss["lead_retry"].pop(key, None)
                else:
                    del ss["queue"][key][:len(items)]
                    ss["mark"][key] = last
                ss["lead_job"][key] = {"job": jid, "t": time.time(), "items": items, "card": None, "card_ready": False,
                                       "conv": c["conv"], "lead": c["lead"], "retried": bool(retry_job)}
            update(started)
    if error is not None:
        reply(key, "interactive", _card(f"{_name(c['lead'])} 没接上", "没能启动工单，请在电脑上检查队员或项目状态。", "red",
                                        icon=_face(c["lead"])))
        return
    echo_only = all(item["kind"] == "echo" for item in items)
    card = None
    try:
        card = reply(key, "interactive", _card(_name(c["lead"]), "<font color='grey'>" + ("队员交活了，核对中…" if echo_only else "在想…") + "</font>",
                                               "indigo", icon=_face(c["lead"])))
    finally:
        def set_card(ss):
            job = ss["lead_job"].get(key)
            if job and job["job"] == jid:
                job.update(card=card, card_ready=True)
        update(set_card)


def _side(ch, mark):
    import chat
    rows = chat.read(ch, 200)
    ids = [r.get("id") for r in rows]
    new = rows[ids.index(mark) + 1:] if mark in ids else rows
    lines = [f"{'主人' if r.get('frm') == 'user' else _name(r.get('frm'))}：{(r.get('text') or '')[:800]}"
             for r in new if not (r.get("to_lead") or r.get("lead_reply") or r.get("frm") == "agentbus")]
    return "\n".join(lines)[-4000:], (ids[-1] if ids else mark)


def _lead_done(key, lj):
    import agentbus
    import chat
    import mcp_server
    try:
        out = mcp_server.job_state(lj["job"])[1]
    except FileNotFoundError:
        out = "找不到本机工单记录"
    c = state()["chats"][key]
    if lj.get("conv", c["conv"]) != c["conv"]:
        update(lambda s: s["lead_job"].pop(key, None))
        return
    ok = "[agentbus] 做完了" in out[-600:]
    text = "\n".join(l for l in out.splitlines() if not l.startswith("[agentbus]")).strip()
    btns = None
    if ok:
        title, body_md, color = _name(c["lead"]), text or "（没有回复）", "indigo"
    else:
        if "credits" in out and "额度" in out and not lj.get("retried") and lj.get("items"):
            btns = [{"label": "用 credits 继续", "type": "primary", "value": {"act": "lead_credits", "key": key, "job": lj["job"]}}]
            update(lambda s: s["lead_retry"].__setitem__(key, {"job": lj["job"], "conv": c["conv"], "items": lj["items"]}))
        title, body_md, color = f"{_name(c['lead'])} 没做成", (text or out)[-1500:], "red"
    _ledger["mt"] = None  # 账本可能刚写完：重读一次，这一轮的消耗才算得进去
    try:
        strip = _usage_strip(c) if ok else None
    except (ImportError, OSError, ValueError, RuntimeError):
        strip = None
        LOG.warning("没能生成飞书消耗条")
    mid = lj.get("card") or reply(key, "interactive", _card(title, body_md, color, btns, icon=_face(c["lead"])))
    # 这张卡片就是主位最新的一条回复：它派的活的进度、这一轮的消耗都挂在它下面；上一张下面已经结束的进度清掉
    update(lambda s: (s["chats"][key].update(last={"card": mid, "title": title, "text": body_md, "color": color, "strip": strip, "buttons": btns},
                                             billed=time.time(), briefed=ok or c.get("briefed")),
                      s["prog"].__setitem__(key, {k: v for k, v in s["prog"].get(key, {}).items() if v["state"] == "run"})))
    _render(key, btns)
    if ok and c.get("channel"):
        chat.append(c["channel"], c["lead"], text, lead_reply=True)
    # 主位这一轮里同步派的活（agentbus ask 一直等到做完）它已经亲眼看到结果、汇报过了，不再回音；
    # 后台派的（agentbus_dispatch、--detach，有工单号）不管多快做完，主位都没看到，一律回音叫醒它
    rs = agentbus.unseen_results(me="conv:" + c["conv"][:12])
    late = [r for r in rs if r.get("frm") != "user" and (_detached(c, r) or r.get("start", 0) < lj["t"])]
    if late:
        wake_lead(key, echo="[agentbus 回音] 你派出去的活有新结果，先处理再继续：\n" + "\n\n".join(agentbus._result_text(r) for r in late))
    if ok:
        sent = set(c.get("imgs") or [])
        for p in [p for p in dict.fromkeys(IMG.findall(text)) if p not in sent][:4]:
            _send_image(key, p)
            sent.add(p)
        update(lambda s: s["chats"][key].__setitem__("imgs", list(sent)[-50:]))
    update(lambda s: s["lead_job"].pop(key, None))


_live_seen = {}
ICON = {"想": "💭", "做": "🔧", "说": "💬", "结果": "📄", "错": "⚠️", "补充": "📝"}


def _live(key, c, lj, prog, force=False):
    """主位正在跑：把它最近几步（在想什么、在跑什么命令、在说什么）和它派出去的活，原地写进「在想」那张卡片。最多 3 秒刷一次。"""
    import jobs
    if not lj.get("card"):
        return
    seen = _live_seen.setdefault(key, {"t": 0, "md": ""})
    if not force and time.time() - seen["t"] < 3:
        return
    rec = next((r for r in (jobs.find(f.stem) for f in (HOME / "running").glob("*.json"))
                if r and r.get("conv") == c["conv"][:12] and r.get("frm") == "user"), None)
    evs = [e for e in (jobs.events(rec, 40) if rec else []) if e.get("text")][-5:]
    steps = [f"{ICON.get(e['k'], '·')} {e['text'][:80]}" for e in evs]
    mins = (time.time() - lj["t"]) / 60
    lines = [f"⏳ {_name(v['agent'])} · {v.get('title') or '干活'}" if v["state"] == "run" else
             f"{'✅' if v['state'] == 'ok' else '❌'} {_name(v['agent'])} · {v.get('title') or '干活'}" for v in prog.values()]
    md = "<font color='grey'>" + ("\n".join(steps) or "在想…") + "</font>"
    if md == seen["md"] and not force:
        return
    seen.update(t=time.time(), md=md)
    els = [{"tag": "hr"}, {"tag": "markdown", "content": "\n".join(lines)}] if lines else []
    patch_card(lj["card"], _card(_name(c["lead"]), md, "indigo", icon=_face(c["lead"]), elements=els,
                                 sub=f"干活中 · {mins:.0f} 分钟" if mins >= 1 else "干活中"))


def _detached(c, r):
    """这单是不是后台派的：后台派活都有工单（jobs/*.json，记着会话和队员），同步的 agentbus ask 没有。"""
    if "job" in r:
        return bool(r["job"])
    import mcp_server
    for jf in sorted(mcp_server.JOBS.glob("*.json"))[-80:]:
        j = load_json(jf, {})
        if j.get("conv") == c["conv"][:12] and j.get("agent") in (r.get("agent"), None) and abs(j.get("start", 0) - r.get("start", 0)) < 60:
            return True
    return False


def _render(key, btns=None):
    """主位最新那条回复：正文 + 它派出去的活的进度 + 这一轮的消耗小条（一张卡片原地更新，不另发消息）。"""
    st = state()
    c = st["chats"][key]
    last, prog = c.get("last"), st["prog"].get(key) or {}
    if not last or not last.get("card"):
        return
    lines = []
    for v in sorted(prog.values(), key=lambda v: v["start"]):
        what = f"**{_name(v['agent'])}** · {v.get('title') or '干活'}"
        if v["state"] == "run":
            lines.append(f"⏳ {what}　<font color='grey'>{time.strftime('%H:%M', time.localtime(v['start']))} 开工</font>")
        else:
            mins = max(0.1, (v.get("end", v["start"]) - v["start"]) / 60)
            lines.append(f"{'✅' if v['state'] == 'ok' else '❌'} {what}　<font color='grey'>{mins:.1f} 分钟，交给主位核对</font>")
    els = ([{"tag": "hr"}, {"tag": "markdown", "content": "\n".join(lines)}] if lines else []) + \
          ([_pic(last["strip"])] if last.get("strip") else [])
    patch_card(last["card"], _card(last["title"], last["text"], last["color"], btns if btns is not None else last.get("buttons"), icon=_face(c["lead"]), elements=els))


def _watch_lead(key, c):
    """主位窗口：它派出去的活的进度挂在它最新那条回复下面；它闲下来时，有新结果就用 bus 的回音叫醒它。"""
    import agentbus
    import mcp_server
    st = state()
    prog = dict(st["prog"].get(key) or {})
    running = {}
    for f in (HOME / "running").glob("*.json"):
        r = load_json(f, {})
        if r.get("conv") == c["conv"][:12] and r.get("frm") != "user" and r.get("id"):  # 主位派的（主位自己这一轮是主人派的）
            running[r["id"]] = r
    changed = False
    for rid, r in running.items():
        if rid not in prog:
            prog[rid] = {"agent": r.get("agent"), "title": r.get("title"), "start": r.get("start") or time.time(), "state": "run"}
            changed = True
    for rid, v in prog.items():
        if v["state"] == "run" and rid not in running:
            row = next((x for x in _ledger_rows() if x.get("id") == rid), {})
            v.update(state="ok" if row.get("ok", True) else "fail", end=row.get("end") or time.time())
            changed = True
    if changed:
        update(lambda s: s["prog"].__setitem__(key, prog))
    lj = st["lead_job"].get(key)
    if lj:
        if lj.get("card_ready") is False and time.time() - lj["t"] < 120:
            return
        try:
            alive = mcp_server.job_state(lj["job"])[0]
        except FileNotFoundError:
            alive = False
        if not alive:
            _lead_done(key, lj)
            _kick(key)
        else:
            _live(key, c, lj, prog, force=changed)
        return
    if changed:
        _render(key)
    rs = [r for r in agentbus.unseen_results(me="conv:" + c["conv"][:12]) if r.get("frm") != "user"]
    if rs:
        wake_lead(key, echo="[agentbus 回音] 你派出去的活有新结果，先处理再继续：\n" + "\n\n".join(agentbus._result_text(r) for r in rs))
    elif st["queue"].get(key):
        _kick(key)


_ledger = {"mt": None, "rows": []}


def _ledger_rows():
    from config import LEDGER, ledger_read
    try:
        mt = LEDGER.stat().st_mtime
    except OSError:
        return []
    if mt != _ledger["mt"]:
        _ledger.update(mt=mt, rows=[r for r in ledger_read()[-400:] if not r.get("type")])
    return _ledger["rows"]


def _send_image(key, p):
    f = Path(p)
    if f.is_file() and f.stat().st_size < 10 * 1024 * 1024:
        k = _imgkey(f.read_bytes())
        if k:
            reply(key, "image", k)


# ---------- 卡片按钮 ----------
def on_card(ev):
    _callback.active = True
    try:
        return _on_card(ev)
    finally:
        _callback.active = False


def _on_card(ev):
    oid = (ev.get("operator") or {}).get("open_id")
    st = state()
    if not st.get("enabled") or not oid or oid != st.get("owner"):
        return {"toast": {"type": "error", "content": "只听主人的"}}
    action = ev.get("action") or {}
    v = action.get("value") or {}
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    form = action.get("form_value") or {}
    dm = (ev.get("context") or {}).get("open_chat_id")
    act = v.get("act")
    jobs = {"open": (_open, v.get("path"), dm), "create": (_new_project, form.get("pname"), v.get("parent") or form.get("parent"), dm), "cd": (None,),
            "quota": (lambda d: send(d, "interactive", _quota_card()), dm), "bill": (lambda d: send(d, "interactive", _bill_card()), dm),
            "mode": (_set_mode, v.get("key"), v.get("mode")), "lead": (_set_lead, v.get("key"), v.get("lead")),
            "lead_credits": (_retry_lead, v.get("key"), v.get("job")), "credits": (_credits, v), "gmode": (None,), "glead": (None,), "target": (None,), "member": (None,), "gtalk": (None,), "away": (None,), "go": (_go, v.get("project"), dm)}
    if act not in jobs:
        return {"toast": {"type": "info", "content": "这个按钮过期了"}}
    if act == "create" and not (form.get("pname") or "").strip():
        return {"toast": {"type": "error", "content": "先填项目名"}}
    if act in ("gmode", "glead"):  # 入口卡片上选模式 / 选主位：卡片立刻变
        return {"card": {"type": "raw", "data": _set_group(v.get("project"), mode=v.get("mode"), lead=v.get("lead"))}}
    if act in ("target", "member"):  # 成员栏多选：派活 = 没写名字的话发给谁；群聊 = 谁在聊
        k, aid = v.get("key"), v.get("aid")
        if k not in state()["chats"]:
            return {"toast": {"type": "info", "content": "这个按钮过期了"}}
        if act == "target":
            update(lambda s: s["chats"][k].__setitem__("targets", _toggle(s["chats"][k].get("targets") or [], aid)))
        else:
            import chat
            ch = _channel(k)
            chat.set_meta(ch, members=_toggle(chat.talk_members(ch), aid))
        return {"card": {"type": "raw", "data": _bar_card(k)}}
    if act == "gtalk":
        pj = v.get("project")
        update(lambda s: s.setdefault("group_talk", {}).__setitem__(pj, _toggle(_talkers(pj), v.get("aid"))))
        return {"card": {"type": "raw", "data": _launcher_card(pj)}}
    if act == "away":  # 出门模式开关：回一张状态卡片
        threading.Thread(target=_guard, args=(lambda d: send(d, "interactive", _toggle_away()), dm), daemon=True).start()
        return {"toast": {"type": "success", "content": "好的"}}
    if act == "cd":  # 下拉选位置时路径在 option 里，按钮在 value 里
        return {"card": {"type": "raw", "data": _browse_card(action.get("option") or v.get("path"))}}
    fn, *args = jobs[act]
    threading.Thread(target=_guard, args=(fn, *args), daemon=True).start()
    return {"toast": {"type": "success", "content": "好的"}}


def _retry_lead(key, job):
    if job:
        _kick(key, retry_job=job)


def _credits(v):
    """付费确认只对同一张失败卡片的原始消息和队员生效。"""
    import chat
    ch, aid, rid = v.get("ch"), v.get("agent"), v.get("row")
    if not rid or not ch or not chat.valid(ch):
        return
    with locked("chat-" + chat.conv_of(ch)):
        rows = {r.get("id"): r for r in chat.read(ch, 500)}
        row = rows.get(rid) or {}
        issue = row.get("issue") or {}
        src = rows.get(row.get("source"))
        if not src or issue.get("status") != "quota" or issue.get("agent") != aid or row.get("retried"):
            return
        def reserve(st):
            used = st.setdefault("credits_used", {})
            if rid in used:
                return False
            st["credits_used"] = {k: t for k, t in used.items() if time.time() - t < 86400 * 30}
            st["credits_used"][rid] = time.time()
            return True
        if not update(reserve):
            return
        jobs = chat._dispatch_all(ch, [aid], agents_dict(load_config()), src["text"], src["frm"],
                                  int(src.get("hops") or 0), _root(chat.meta(ch).get("project")), src.get("files"),
                                  chat.lim(), source=src["id"], allow_credits=True)
        if not jobs:
            update(lambda st: st["credits_used"].pop(rid, None))


# ---------- 推送循环：主位窗口看结果；聊天室把频道里的新消息推回飞书 ----------
IMG = re.compile(r"(/[^\s`'\"()（）<>]+\.(?:png|jpe?g|webp|gif))", re.I)


def pump():
    import chat
    mtimes = {}
    while True:
        time.sleep(2)
        try:
            st = state()
            if not st.get("enabled") or not st.get("owner"):
                continue
            for key, c in list(st["chats"].items()):
                if c["mode"] == "lead" or state()["lead_job"].get(key):
                    try:
                        _watch_lead(key, c)
                    except Exception as e:  # noqa: BLE001
                        LOG.warning("看主位窗口出错：%s", type(e).__name__)
                ch = c.get("channel")
                if not ch:
                    continue
                try:
                    mt = chat._file(ch).stat().st_mtime
                except (OSError, ValueError):
                    continue
                if mtimes.get(ch) == mt:
                    continue
                rows = chat.display_messages(chat.read(ch, 500))
                ids = [r.get("id") for r in rows]
                cur = state()["cursor"].get(ch)
                new = rows[ids.index(cur) + 1:] if cur in ids else rows[-3:]
                for r in new:
                    try:
                        _out(ch, key, r)
                    except Exception as e:  # noqa: BLE001
                        LOG.warning("推送到飞书出错：%s", type(e).__name__)
                        break
                    update(lambda s: s["cursor"].__setitem__(ch, r.get("id")))
                else:
                    mtimes[ch] = mt
        except Exception as e:  # noqa: BLE001
            LOG.warning("飞书推送循环出错：%s", type(e).__name__)


def _out(ch, key, r):
    """聊天室模式：队员接活一张小卡片，交活时原地改成它的回复（带它的头像）。"""
    frm, text = r.get("frm"), r.get("text") or ""
    if frm == "user" or r.get("lead_reply") or r.get("to_lead"):
        return
    if frm == "agentbus":
        if r.get("job") and r.get("agent"):
            mid = reply(key, "interactive", _card(_name(r["agent"]), "<font color='grey'>接活了，干完回你…</font>", "indigo", icon=_face(r["agent"])))
            if mid:
                update(lambda s: s["pending"].__setitem__(f"{ch}|{r['job']}", [mid, time.time()]))
            return
        if text.startswith("没 @ 人") or text.startswith("🔀"):  # 值班只记录：不回话
            return
        issue = r.get("issue") or {}
        aid = issue.get("agent")
        pending, card = _pending(ch, r.get("reply_job"), aid)
        btns = ([{"label": "用 credits 继续", "type": "primary", "value": {"act": "credits", "ch": ch, "agent": aid, "row": r["id"]}}]
                if issue.get("status") == "quota" and aid and r.get("source") and not r.get("retried") else None)
        body = _card(f"{_name(aid)} 没做成" if aid else "agentbus", text, "red" if aid else "grey", btns, icon=_face(aid) if aid else _bus_icon())
        _deliver(key, pending, card, body)
        return
    pending, card = _pending(ch, r.get("reply_job"), frm)
    color = MODE_COLOR.get((state()["chats"].get(key) or {}).get("mode"), "turquoise")
    body = _card(_name(frm), text, color, icon=_face(frm), sub=f"用时 {(time.time() - card[1]) / 60:.1f} 分钟" if card else None)
    _deliver(key, pending, card, body)
    for p in list(dict.fromkeys(IMG.findall(text)))[:4]:
        _guard(_send_image, key, p)
    _guard(_bar, key)


def _pending(ch, job, aid):
    pending = state()["pending"]
    key = f"{ch}|{job}" if job else f"{ch}|{aid}"
    return key, pending.get(key)


def _deliver(key, pending, card, body):
    if card:
        patch_card(card[0], body)
        update(lambda s: s["pending"].pop(pending, None))
    else:
        reply(key, "interactive", body)


def start():
    """service.py 启动时调：收和发各一个后台线程。"""
    _status(ok=False, err="", identity=_identity(state()))
    threading.Thread(target=run, daemon=True, name="feishu-ws").start()
    threading.Thread(target=pump, daemon=True, name="feishu-pump").start()


# ---------- 初次接入 ----------
def _ask_secret(app):
    """在主人屏幕上弹一个系统输入框（输入内容隐藏）让主人粘贴 App Secret：密钥直接进钥匙串，不经过任何 agent。"""
    msg = f"agentbus 接入飞书：请粘贴应用 {app} 的 App Secret（在飞书开放平台「凭证与基础信息」页复制）"
    if sys.platform == "darwin":
        import subprocess
        r = subprocess.run(["osascript", "-e", f'text returned of (display dialog "{msg}" default answer "" with hidden answer '
                                               f'with title "agentbus" buttons {{"取消", "存好"}} default button "存好")'], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    return None



def bot_name():
    """机器人默认叫「Agentbus-电脑名」：电脑名去掉「的MacBook Pro」这类机型后缀。主人改过就用主人的。"""
    configured = state().get("bot_name")
    if configured:
        return configured
    import platform
    import subprocess
    name = ""
    if sys.platform == "darwin":
        name = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True).stdout.strip()
    name = name or os.environ.get("COMPUTERNAME") or platform.node().split(".")[0]
    short = re.sub(r"(的|’s|'s|-)?\s*(MacBook|iMac|Mac mini|Mac Studio|Mac Pro|Laptop|Desktop|PC)\b.*$", "", name, flags=re.I).strip(" -_")
    return f"Agentbus-{short or name or '电脑'}"


def guide():
    ui = _ui_dir()
    icon = Path.home() / "Downloads" / "agentbus-feishu-icon.png"  # 浏览器选文件时好找
    src = ui / "assets" / "app-icon.png"
    if src.exists() and not icon.exists():
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(src.read_bytes())
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return ((bundle / "feishu_setup.md").read_text("utf-8")
            .replace("{bot_name}", bot_name()).replace("{icon}", str(icon)))


# ---------- 设置页「飞书」：一键接入 ----------
def info():
    st = state()
    ss = connection_status(st)
    return {"app_id": st.get("app_id"), "enabled": bool(st.get("enabled")), "has_secret": bool(st.get("app_id") and secret(st["app_id"])),
            "connected": bool(ss.get("ok")), "err": ss.get("err"), "owner": bool(st.get("owner")), "bot_name": bot_name(),
            "pair": None if st.get("owner") else st.get("pair"), "groups": len(st.get("groups") or {}), "lead": _lead(),
            "job": st.get("setup_job"), "phrase": PHRASE}


def save_creds(app_id, sec):
    import pf
    app_id = (app_id or "").strip()
    if not re.fullmatch(r"cli_[0-9a-z]+", app_id):
        raise ValueError("App ID 应该是 cli_ 开头的一串字母数字")
    def save(s):
        if sec and sec.strip():
            pf.secret_set(f"feishu-{app_id}", sec.strip())
        if not secret(app_id):
            raise ValueError("还没填 App Secret")
        if s.get("app_id") not in (None, app_id):
            for k in ("owner", "pair", "groups", "gmeta", "chats", "seen", "imgs", "head", "cursor",
                      "pending", "queue", "lead_job", "mark", "prog", "lead_retry"):
                s.pop(k, None)
        s.update(app_id=app_id, enabled=True, revision=secrets.token_hex(8))
        if not s.get("owner") and not s.get("pair"):
            s["pair"] = f"{secrets.randbelow(10 ** 6):06d}"
    update(save)
    return info()


PHRASE = "接入飞书：运行 `agentbus feishu guide` 拿任务书照着做，要我动手的地方（登录飞书、粘贴密钥、发配对码）一步步教我。"


def start_setup(agent=None):
    """一键接入：交给主位——接入 bus 的那个对话窗口。它下一句话时会收到任务（没装钩子的窗口，把 PHRASE 贴进去也一样），
    它去开放平台建机器人，要主人动手的地方在对话里教主人；agentbus 界面上不用填任何东西。"""
    import agentbus
    from config import LEAD_FILE, ROOMS_FILE, active_lead
    lead = agent or active_lead()
    if lead and sealed(lead):
        lead = None
    joined = load_json(HOME / "joined.json", {})
    rooms = load_json(ROOMS_FILE, {})
    candidates = [k for k, r in joined.items() if not r.get("declined") and r.get("agent") == lead
                  and not k.startswith("fs-")]
    active = load_json(LEAD_FILE, {})
    conv = active.get("conv") if active.get("agent") == lead and active.get("conv") in candidates else None
    conv = conv or max(candidates, key=lambda k: (rooms.get(k) or joined[k]).get("t", 0), default=None)
    agentbus.add_lead_task("主人在 agentbus 里点了「一键接入飞书」。" + PHRASE.split("：", 1)[1].replace("教我", "教主人").replace("要我", "要主人"),
                           agent=lead, conv=conv)
    update(lambda s: s.update(setup_job={"via": "lead", "agent": lead, "t": time.time()}))
    return {"agent": lead, "name": _name(lead) if lead else "接入的窗口", "phrase": PHRASE}


# ---------- 命令行：agentbus feishu ... ----------
def cli(argv):
    import getpass
    cmd = argv[0] if argv else "status"
    code = lambda s: s.update(pair=s.get("pair") or f"{secrets.randbelow(10 ** 6):06d}") or s["pair"]  # noqa: E731
    if cmd == "guide":  # 初次接入：给会用浏览器的 agent 的任务书（默认机器人名字、图标都填好了）
        print(guide())
    elif cmd == "name" and len(argv) > 1:
        update(lambda s: s.update(bot_name=argv[1] if argv[1].lower().startswith("agentbus") else f"Agentbus-{argv[1]}"))
        print(f"机器人名字记成 {state()['bot_name']}（要在开发者后台改了才生效，见 agentbus feishu guide）。")
    elif cmd == "setup":
        app = next((arg for arg in argv[1:] if arg != "--again"), None) or input("App ID：").strip()
        if not re.fullmatch(r"cli_[0-9a-z]+", app):
            sys.exit("App ID 应该是 cli_ 开头的一串字母数字")
        sec = None
        if not secret(app) or "--again" in argv:
            sec = getpass.getpass("App Secret（输入时不显示）：") if sys.stdin.isatty() else _ask_secret(app)
            if not sec:
                sys.exit("没拿到 App Secret，请主人在终端里运行 agentbus feishu setup " + app)
        result = save_creds(app, sec)
        print("飞书接入已打开，服务会自动连接。")
        if result["pair"]:
            print(f"配对码：{result['pair']}（在飞书里私聊机器人发这串数字，完成绑定）")
    elif cmd == "code":
        print("已经绑定了主人。要换人先运行 agentbus feishu unbind。" if state().get("owner") else "配对码：" + update(code))
    elif cmd == "unbind":
        update(lambda s: s.update(owner=None, pair=None))
        print("已解绑主人。")
    elif cmd in ("on", "off"):
        update(lambda s: s.update(enabled=cmd == "on"))
        print("已" + ("打开" if cmd == "on" else "关闭") + "飞书接入。")
    elif cmd == "lead" and len(argv) > 1:
        update(lambda s: s.update(lead=argv[1]))
        print(f"默认主位改成 {argv[1]}（新聊天生效）。")
    elif cmd == "root" and len(argv) > 1:
        update(lambda s: s.update(new_root=argv[1]))
        print(f"新建项目放在 {argv[1]} 下面。")
    else:
        st = state()
        ss = connection_status(st)
        print(f"应用：{st.get('app_id') or '未配置'}　接入：{'开' if st.get('enabled') else '关'}　默认主位：{_lead()}")
        print(f"长连接：{'已连上' if ss.get('ok') else '没连上 ' + (ss.get('err') or '')}")
        print(f"主人：{'已绑定' if st.get('owner') else '未绑定（agentbus feishu code 拿配对码）'}")
        for cid, p in st["groups"].items():
            print(f"  群 {cid} → 项目 {p}")
        print(f"聊天：{len(st['chats'])} 个")


if __name__ == "__main__":
    cli(sys.argv[1:])
