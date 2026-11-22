"""更新推送：查 GitHub Releases 有没有比本机新的版本（开源发布用）。
仓库在 设置 → 关于 里填（owner/name），默认 DEFAULT_REPO；每 6 小时查一次，结果缓存在 ~/.agentbus/update.json。
只读 GitHub 的公开接口，不上传任何东西。
"""
import json
import re
import time
import urllib.request

from config import HOME, load_config, load_json, save_json

VERSION = "0.6.1"
DEFAULT_REPO = "baitongren166-hub/agentbus"  # 开源后填成正式仓库，比如 "someone/agentbus"
CACHE = HOME / "update.json"
EVERY = 6 * 3600


def repo():
    return ((load_config().get("settings") or {}).get("update_repo") or DEFAULT_REPO).strip().strip("/")


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0"))[:3]) or (0,)


def check(fresh=False):
    """返回 {repo, current, latest, available, url, notes, published, assets, error, checked}。"""
    r = repo()
    cur = load_json(CACHE, {})
    if not fresh and cur.get("repo") == r and time.time() - cur.get("checked", 0) < EVERY:
        return {**cur, "current": VERSION, "available": _ver(cur.get("latest")) > _ver(VERSION)}
    out = {"repo": r, "current": VERSION, "checked": time.time()}
    if not r or "/" not in r:
        out["error"] = "还没填 GitHub 仓库（设置 → 关于）"
        return out
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{r}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "agentbus/" + VERSION})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode())
        out.update(latest=(d.get("tag_name") or "").lstrip("v"), url=d.get("html_url"), notes=(d.get("body") or "")[:1500],
                   published=d.get("published_at"), name=d.get("name"),
                   assets=[{"name": a.get("name"), "url": a.get("browser_download_url"), "size": a.get("size")} for a in d.get("assets") or []])
    except Exception as e:
        out["error"] = f"没查到：{e}"
    save_json(CACHE, out)
    out["available"] = _ver(out.get("latest")) > _ver(VERSION)
    return out
