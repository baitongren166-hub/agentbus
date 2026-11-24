"""把 core/ 打成一个可执行文件 agentbus-core（Windows 上是 agentbus-core.exe）。界面文件整理到 build/ui-stage/，
程序（文件夹版，build/dist/agentbus-core/）和界面都由 Tauri 作为资源打进应用：Mac 在 Contents/Resources/{core-bin,ui}，
Windows 在安装目录 {core-bin,ui}；启动服务时用 AGENTBUS_UI 告诉它界面在哪。
Windows 用户不用装 Python。打好后放到 app/src-tauri/binaries/，名字带上目标平台（Tauri 的 externalBin 规矩）。

用法（在 agentbus 根目录）：python scripts/build-core.py
需要：pip install pyinstaller Pillow
"""
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认不是 UTF-8：打印中文会直接报错退出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = ";" if os.name == "nt" else ":"
MODS = ["service", "agentbus", "mcp_server", "playbook", "wallet", "api_agent", "scheduler", "brains", "quota", "activity", "catalog", "config", "pf",
        "chat", "setup", "update", "feishu", "feishu_art", "away", "jobs"]  # 都是函数里才 import 的，不写 PyInstaller 找不到


def triple():
    r = subprocess.run(["rustc", "-vV"], capture_output=True, text=True)
    return next(l.split(": ")[1] for l in r.stdout.splitlines() if l.startswith("host: "))


def stage_ui():
    """打包用的界面副本：去掉美术备份（*.bak*、old-*）和预览大图，免得安装包白白变大。"""
    dst = ROOT / "build" / "ui-stage"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(ROOT / "ui", dst, ignore=shutil.ignore_patterns("*.bak*", "old-*", "preview.png", "._*", ".DS_Store", "work"))
    return dst


def main():
    from importlib.util import find_spec
    if find_spec("PIL") is None:
        sys.exit("飞书头像依赖 Pillow，请先运行 python -m pip install Pillow")
    stage_ui()
    cmd = [sys.executable, "-m", "PyInstaller", "--onedir", "--noconfirm", "--clean", "--name", "agentbus-core",
           "--paths", str(ROOT / "core"),  # 界面文件不打进程序（每次启动都要解压，慢）；作为应用资源单独放，见 tauri.*.conf.json
           "--distpath", str(ROOT / "build" / "dist"), "--workpath", str(ROOT / "build" / "work"), "--specpath", str(ROOT / "build")]
    for m in MODS:
        cmd += ["--hidden-import", m]
    cmd += ["--add-data", f"{ROOT / 'core' / 'feishu_setup.md'}{SEP}."]  # 飞书初次接入的任务书（agentbus feishu guide 读它）
    cmd.append(str(ROOT / "core" / "entry.py"))
    subprocess.run(cmd, check=True)
    # 文件夹版（--onedir）：单文件版每次启动都要解压到临时目录，macOS 每次都会重新扫描，派一次活要等十几秒
    out = ROOT / "build" / "dist" / "agentbus-core"
    print("好了：", out, "（作为应用资源打进 Mac / Windows 安装包，见 app/src-tauri/tauri.*.conf.json）")


if __name__ == "__main__":
    main()
