#!/usr/bin/env python3
"""从第二批分层立绘（ui/assets/art/v2/char-*.png）裁出圆头像：ui/assets/avatars/v2-<名字>.png。
立绘规格：三格各 512×1024，头顶在 y≈260–300，身体中线 x=256。取“摸鱼”格（中间那格，表情最放松）。
用法：python3 make_avatars.py [--preview 输出.png]
"""
import sys
from pathlib import Path

from PIL import Image

UI = Path(__file__).resolve().parent.parent / "ui"
ART, OUT = UI / "assets" / "art" / "v2", UI / "assets" / "avatars"


def crop(path):
    im = Image.open(path).convert("RGBA")
    cell = im.crop((512, 0, 1024, 1024))
    alpha = cell.split()[3].point(lambda v: 255 if v > 40 else 0)
    box = alpha.crop((0, 0, 512, 760)).getbbox()  # 只看桌沿线以上
    if not box:
        return None
    top = box[1]
    size = 400
    y0 = max(0, top + 10)
    return cell.crop((256 - size // 2, y0, 256 + size // 2, y0 + size)).resize((256, 256), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for f in sorted(p for p in ART.glob("char-*.png") if ".bak" not in p.name):
        name = f.stem[len("char-"):]
        av = crop(f)
        if av:
            av.save(OUT / f"v2-{name}.png")
            made.append((name, av))
    print("做好头像：", "、".join(n for n, _ in made) or "（还没有立绘）")
    if "--preview" in sys.argv and made:
        out = sys.argv[sys.argv.index("--preview") + 1]
        sheet = Image.new("RGBA", (160 * len(made), 160), (236, 228, 216, 255))
        for i, (_, av) in enumerate(made):
            sheet.alpha_composite(av.resize((160, 160)), (i * 160, 0))
        sheet.convert("RGB").save(out)


if __name__ == "__main__":
    main()
