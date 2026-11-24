"""出门模式：主人出门时让电脑一直醒着，好在手机上（飞书）继续指挥队员。

  打开：电脑不睡、硬盘不停转（Mac 用 caffeinate 绑在服务进程上，服务重启会自动接上；Windows 用系统的「保持唤醒」）；
        是笔记本的话再让合盖也不睡（pmset disablesleep，要管理员权限——第一次点「授权」输一次密码，
        装一条只允许这两条命令免密的规则，之后开关都不再问）。
  关闭：全部恢复。
  检查：电源、代理、项目所在的外置硬盘、飞书长连接，哪一项不在线就提醒。
锁屏不管：锁着屏大部分活照常干，只有录屏、操作界面这类要等主人回来（调度器本来就会排队）。
"""
import json
import logging
import re
import shlex
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import HOME, load_json, projects, save_json  # noqa: E402

STATE = HOME / "away.json"
SUDOERS = "/etc/sudoers.d/agentbus-away"
PMSET = "/usr/bin/pmset"
IS_MAC, IS_WIN = sys.platform == "darwin", os.name == "nt"
_proc = {"p": None}
_win = {"stop": None}
_lock = threading.RLock()
LOG = logging.getLogger(__name__)


def state():
    return load_json(STATE, {})


def laptop():
    if IS_MAC:
        return "InternalBattery" in _batt()
    if IS_WIN:
        try:
            import ctypes

            class SPS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                            ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
            s = SPS()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
                return False
            return s.BatteryFlag not in (-128, -1)  # 128 = 没有电池
        except Exception:  # noqa: BLE001
            return False
    return False


def _batt():
    try:
        return subprocess.run([PMSET, "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def authorized():
    """合盖不睡那两条命令是不是已经免密了（sudo -n 不会弹密码，没授权就直接失败）。"""
    if not IS_MAC:
        return True
    try:
        return all(subprocess.run(["/usr/bin/sudo", "-n", "-l", PMSET, "-a", "disablesleep", value],
                                  capture_output=True, timeout=3).returncode == 0 for value in ("1", "0"))
    except (OSError, subprocess.SubprocessError):
        return False


def authorize():
    """第一次：弹 macOS 自己的管理员密码框，装一条规则——只有当前用户、只有这两条 pmset 命令可以免密。"""
    if not IS_MAC:
        return {"ok": True}
    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        return {"ok": False, "error": "无法为当前系统用户写入授权规则"}
    rule = f"{user} ALL=(root) NOPASSWD: {PMSET} -a disablesleep 1, {PMSET} -a disablesleep 0\n"
    sh = ('umask 077; f=$(/usr/bin/mktemp /etc/sudoers.d/.agentbus-away.XXXXXX) || exit 1; '
          "trap '/bin/rm -f \"$f\"' EXIT; "
          f'/usr/bin/printf %s {shlex.quote(rule)} > "$f" && /usr/sbin/visudo -cf "$f" && '
          f'/bin/chmod 0440 "$f" && /bin/mv -f "$f" {shlex.quote(SUDOERS)}')
    # AppleScript 字符串用转义的换行，sudoers 内容只在 root 创建的临时文件里写。
    r = subprocess.run(["/usr/bin/osascript", "-e", f'do shell script {json.dumps(sh, ensure_ascii=False)} with administrator privileges '
                                                  f'with prompt "agentbus 出门模式：允许合盖时电脑不睡（只授权这一次）"'],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0 and authorized(), "error": (r.stderr or "").strip()[-200:]}


def _lid(on):
    try:
        r = subprocess.run(["/usr/bin/sudo", "-n", PMSET, "-a", "disablesleep", "1" if on else "0"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _sleep_disabled():
    try:
        r = subprocess.run([PMSET, "-g"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^\s*SleepDisabled\s+([01])\s*$", r.stdout, re.M)
    return match[1] == "1" if match else None


def _keep_awake():
    """电脑不睡、硬盘不停转、屏幕可以熄。绑在服务进程上：服务没了它也跟着结束，不会留一个一直挡着睡眠的野进程。"""
    _stop_awake()
    if IS_MAC:
        _proc["p"] = subprocess.Popen(["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())])
    elif IS_WIN:
        import ctypes
        ev = threading.Event()
        ready = threading.Event()
        results = []

        def hold():  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED 必须由同一个线程设置和清除
            try:
                results.append(ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001))
            finally:
                ready.set()
            if results[-1]:
                ev.wait()
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

        threading.Thread(target=hold, daemon=True).start()
        if not ready.wait(3) or not any(results):
            ev.set()
            raise OSError("Windows 保持唤醒失败")
        _win["stop"] = ev
    else:
        raise OSError("当前系统不支持出门模式")


def _stop_awake():
    p = _proc.pop("p", None)
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=2)
        except ProcessLookupError:
            pass
    ev = _win.pop("stop", None)
    if ev:
        ev.set()


def set_on(on=None):
    with _lock:
        st = state()
        on = not st.get("on") if on is None else on
        try:
            if on:
                p = _proc.get("p")
                if not ((p and p.poll() is None) or _win.get("stop")):
                    _keep_awake()
                if not st.get("on"):
                    st = {"on": True, "since": time.time(), "lid": None, "lid_changed": False}
                if IS_MAC and laptop() and not st.get("lid"):
                    before = _sleep_disabled()
                    if before is not None:
                        st["lid"] = before or _lid(True)
                        st["lid_changed"] = st["lid"] and not before
            else:
                if st.get("lid_changed", st.get("lid")) and IS_MAC and not _lid(False):
                    return status("恢复合盖睡眠失败，请重新授权后再关闭")
                _stop_awake()
                st = {"on": False}
            save_json(STATE, st)
        except (OSError, subprocess.SubprocessError) as e:
            return status(str(e))
        return status()


def resume():
    """服务启动时恢复保持唤醒。失败时服务仍正常启动，界面显示实际状态。"""
    with _lock:
        st = state()
        if st.get("on"):
            try:
                _keep_awake()
            except (OSError, subprocess.SubprocessError):
                LOG.warning("出门模式未能恢复")


def checks():
    out = []
    if IS_MAC:
        b = _batt()
        if "InternalBattery" in b:
            out.append({"name": "电源", "ok": "AC Power" in b, "detail": "插着电" if "AC Power" in b else "在用电池，出门前插上电源"})
    import urllib.request
    from urllib.parse import urlparse
    try:
        sysp = urllib.request.getproxies()
        px = sysp.get("https") or sysp.get("http") or sysp.get("all")
        if px:
            u = urlparse(px if "://" in px else "http://" + px)
            host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
            if not host:
                raise ValueError("无代理主机")
            try:
                socket.create_connection((host, port), timeout=1).close()
            except OSError:
                out.append({"name": "代理", "ok": False, "detail": f"{host}:{port} 连不上"})
    except (OSError, ValueError):
        out.append({"name": "代理", "ok": False, "detail": "代理地址无法读取"})
    vols = {}
    for name, p in projects().items():
        root = isinstance(p, dict) and p.get("root")
        if root and root.startswith("/Volumes/"):
            vols.setdefault("/".join(root.split("/")[:3]), []).append(name)
    for v, names in vols.items():
        ok = Path(v).is_dir()
        out.append({"name": f"硬盘 {Path(v).name}", "ok": ok, "detail": ("已挂载" if ok else "没挂上") + f"（{'、'.join(names[:3])}）"})
    fs = load_json(HOME / "feishu.json", {})
    if fs.get("enabled"):
        import feishu
        ok = feishu.connection_status(fs).get("ok")
        out.append({"name": "飞书", "ok": bool(ok), "detail": "已连上" if ok else "长连接没连上"})
    return out


def status(error=None):
    st = state()
    lap = laptop()
    p = _proc.get("p")
    active = bool((p and p.poll() is None) or _win.get("stop"))
    items = checks()
    if error or (st.get("on") and not active):
        items.insert(0, {"name": "保持唤醒", "ok": False, "detail": error or "保持唤醒进程未运行"})
    return {"on": bool(st.get("on") and (active or st.get("lid"))), "since": st.get("since"), "laptop": lap, "lid": st.get("lid"),
            "authorized": authorized() if lap else True, "checks": items}
