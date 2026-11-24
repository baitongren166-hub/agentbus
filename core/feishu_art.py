"""飞书卡片用的图：队员圆头像、项目群头像（主位头像 + 文件夹角标）、额度消耗条。
飞书卡片自己画不出头像 + 进度条，这里用 PIL 画成图，再上传成 image_key 放进卡片。"""
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from feishu import _ui_dir

UI = _ui_dir()
FONTS = ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
         "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"]
INK, SUB, TRACK = (38, 38, 36), (128, 126, 120), (236, 234, 228)


def font(size):
    for f in FONTS:
        if Path(f).exists():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return ImageFont.load_default()


def rgb(color, fallback=(127, 119, 221)):
    try:
        c = color.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except (AttributeError, ValueError):
        return fallback


def tint(c, k):
    """往白色里混：k=0 原色，k=1 纯白。"""
    return tuple(int(v + (255 - v) * k) for v in c)


def avatar(path, size, color=None):
    """圆形头像；没有图就用主题色圆 + 名字首字。"""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    src = UI / path if path and not Path(path).is_absolute() else Path(path or "")
    if path and src.is_file():
        try:
            with Image.open(src) as image:
                im = image.convert("RGBA")
        except OSError:
            im = None
    else:
        im = None
    if im is not None:
        bg = Image.new("RGBA", im.size, tint(rgb(color), 0.85) + (255,))
        bg.alpha_composite(im)
        out.paste(bg.resize((size, size), Image.LANCZOS), (0, 0), mask)
    else:
        out.paste(Image.new("RGBA", (size, size), rgb(color) + (255,)), (0, 0), mask)
    return out


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def folder_avatar(name, color="#7b6cf0"):
    """项目群头像：一个文件夹，上面写项目名的头一两个字。固定不变——飞书的话题没有自己的头像，列表里显示的都是群头像，
    所以群头像不能跟着某个聊天的主位变，不然换一次主位所有话题的头像都跟着变。"""
    S = 640
    c = rgb(color)
    im = Image.new("RGBA", (S, S), tint(c, 0.86) + (255,))
    d = ImageDraw.Draw(im)
    x, y, w, h = 90, 140, 460, 360
    d.rounded_rectangle((x, y, x + 190, y + 70), 30, fill=tint(c, 0.25) + (255,))
    d.rounded_rectangle((x, y + 40, x + w, y + h), 40, fill=tint(c, 0.25) + (255,))
    d.rounded_rectangle((x, y + 90, x + w, y + h), 40, fill=c + (255,))
    label = (name or "?").strip()
    label = label[:2] if not label[:2].isascii() else label[:3]
    f = font(150 if len(label) <= 2 else 120)
    d.text((x + w // 2, y + 90 + (h - 90) // 2), label, font=f, fill=(255, 255, 255), anchor="mm")
    return png(im)


def ring_avatar(faces):
    """派活群头像：一圈队员的头像围着 agentbus 的星球（谁都能派）。faces: [(头像路径, 颜色)]"""
    import math
    S = 640
    im = Image.new("RGBA", (S, S), (236, 244, 241, 255))
    icon = UI / "assets" / "app-icon.png"
    if icon.exists():
        try:
            with Image.open(icon) as image:
                c = image.convert("RGBA").resize((230, 230), Image.LANCZOS)
            im.alpha_composite(c, ((S - 230) // 2, (S - 230) // 2))
        except OSError:
            pass
    faces = faces[:8]
    n, R, size = len(faces), 228, 150 if len(faces) <= 6 else 128
    for i, (path, color) in enumerate(faces):
        ang = -math.pi / 2 + 2 * math.pi * i / max(n, 1)
        x, y = S / 2 + R * math.cos(ang) - size / 2, S / 2 + R * math.sin(ang) - size / 2
        ring = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse((0, 0, size + 11, size + 11), fill=(255, 255, 255, 255))
        im.alpha_composite(ring, (int(x) - 6, int(y) - 6))
        im.alpha_composite(avatar(path, size, color), (int(x), int(y)))
    return png(im)


def collage_avatar(faces):
    """群聊群头像：选中拉进来的那几位的头像拼在一起（像微信群头像）。"""
    S, gap, box = 640, 12, 470  # 飞书把群头像裁成圆：拼图只放在中间的内接方块里，四角不会被裁掉
    faces = faces[:9] or [(None, None)]
    k = 1 if len(faces) == 1 else 2 if len(faces) <= 4 else 3
    cell = (box - gap * (k - 1)) // k
    im = Image.new("RGBA", (S, S), (238, 235, 250, 255))
    rows = [faces[i:i + k] for i in range(0, len(faces), k)]
    y0 = (S - len(rows) * cell - (len(rows) - 1) * gap) // 2
    for r, row in enumerate(rows):
        x0 = (S - len(row) * cell - (len(row) - 1) * gap) // 2
        for c, (path, color) in enumerate(row):
            im.alpha_composite(avatar(path, cell, color), (x0 + c * (cell + gap), y0 + r * (cell + gap)))
    return png(im)


def group_avatar(path, color):
    """项目群头像：主位的 Q 版头像，右下角一个文件夹角标（一眼看出是哪个项目、谁当家）。"""
    S = 640
    c = rgb(color)
    im = Image.new("RGBA", (S, S), tint(c, 0.82) + (255,))
    im.alpha_composite(avatar(path, 520, color), (60, 40))
    d = ImageDraw.Draw(im)
    x, y, w, h = 392, 402, 210, 160  # 文件夹：后片带耳朵 + 前片
    d.rounded_rectangle((x, y, x + w, y + h), 22, fill=(255, 255, 255, 255))
    d.rounded_rectangle((x + 10, y + 8, x + 90, y + 40), 12, fill=tint(c, 0.15) + (255,))
    d.rounded_rectangle((x + 10, y + 26, x + w - 10, y + h - 10), 16, fill=tint(c, 0.15) + (255,))
    d.rounded_rectangle((x + 10, y + 50, x + w - 10, y + h - 10), 16, fill=c + (255,))
    return png(im)


def bars(rows, big=False):
    """一行一个队员：头像、名字、剩余进度条、右边一句话。rows: [{avatar, color, name, pct(0~1 或 None), right}]。
    big=False 是聊天里每轮结束挂的小条（尽量矮），big=True 是私聊里看的总表。"""
    K = 2  # 两倍画，缩小显示更清楚
    rh, av, fs, W = ((58, 44, 23, 600) if big else (44, 32, 20, 600))  # 卡片里按整宽显示（手机上约 330 宽），字要够大
    rh, av, fs, W = rh * K, av * K, fs * K, W * K
    pad = 8 * K
    im = Image.new("RGBA", (W, rh * len(rows) + pad * 2), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    f, fsub = font(fs), font(int(fs * 0.9))
    name_w = int(W * 0.24)
    bar_x = pad + av + 10 * K + name_w
    right_w = int(W * 0.34)
    bar_w = W - bar_x - right_w - 12 * K - pad
    for i, r in enumerate(rows):
        y = pad + i * rh
        cy = y + rh // 2
        im.alpha_composite(avatar(r.get("avatar"), av, r.get("color")), (pad, cy - av // 2))
        d.text((pad + av + 8 * K, cy), r["name"][:12], font=f, fill=INK, anchor="lm")
        bh = max(4 * K, int(rh * 0.2))
        if r.get("pct") is not None:  # 只有按百分比算的额度画进度条；按钱算的只写余额
            d.rounded_rectangle((bar_x, cy - bh // 2, bar_x + bar_w, cy + bh // 2), bh // 2, fill=TRACK)
            p = max(0.0, min(1.0, r["pct"]))
            col = rgb(r.get("color")) if p > 0.2 else (226, 75, 74)
            if p > 0:
                d.rounded_rectangle((bar_x, cy - bh // 2, bar_x + max(bh, int(bar_w * p)), cy + bh // 2), bh // 2, fill=col)
        d.text((W - pad, cy), r.get("right", ""), font=fsub, fill=SUB, anchor="rm")
    return png(im)


if __name__ == "__main__":  # 出样图看效果：python feishu_art.py 输出目录
    o = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    rows = [{"avatar": "assets/avatars/v2-gpt.png", "color": "#8b7fd6", "name": "GPT", "pct": 0.87, "right": "本轮 +1% · 剩 87%"},
            {"avatar": "assets/avatars/v2-opencode.png", "color": "#4f9a6a", "name": "opencode", "pct": None, "right": "本轮 ¥0.02 · 余额 ¥92.8"}]
    (o / "bars-small.png").write_bytes(bars(rows))
    (o / "bars-big.png").write_bytes(bars(rows + [{"avatar": "assets/avatars/v2-gemini.png", "color": "#7b6cf0", "name": "Antigravity", "pct": 0.66, "right": "剩 66%"}], big=True))
    (o / "group.png").write_bytes(group_avatar("assets/avatars/v2-gpt.png", "#8b7fd6"))
