"""合并翻译：ui/i18n/work/part*.en.json + ui/i18n/extra.en.json（手补的）→ ui/i18n/en.json 和页面加载用的 ui/i18n/en.js。
每条都检查占位符个数、编号和原文一致，不一致的列出来，不收进词典。
用法：python3 scripts/i18n-build.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "ui" / "i18n"
PH = re.compile(r"\{\d+\}")
CJK = re.compile(r"[㐀-鿿]")


def main():
    keys = json.loads((D / "keys.json").read_text("utf-8"))
    old = json.loads((D / "en.json").read_text("utf-8")) if (D / "en.json").exists() else {}
    got = dict(old)
    for f in sorted((D / "work").glob("part*.en.json")) + [D / "extra.en.json"]:
        if f.exists():
            got.update(json.loads(f.read_text("utf-8")))
    out, bad = {}, []
    for k, v in got.items():
        if not isinstance(v, str) or not v.strip():
            continue
        if sorted(PH.findall(k)) != sorted(PH.findall(v)):
            bad.append((k, v))
            continue
        if CJK.search(v):  # 译文里还有中文：当没译
            bad.append((k, v))
            continue
        out[k] = v
    missing = [k for k in keys if k not in out]
    (D / "en.json").write_text(json.dumps(out, ensure_ascii=False, indent=0, sort_keys=True), "utf-8")
    (D / "en.js").write_text("// 自动生成：python3 scripts/i18n-build.py（不要手改，改 work/ 或 extra.en.json）\nwindow.AB_EN = "
                             + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n", "utf-8")
    (D / "missing.json").write_text(json.dumps(missing, ensure_ascii=False, indent=0), "utf-8")
    print(f"词典 {len(out)} 条；不合格 {len(bad)} 条；还没译 {len(missing)} 条（见 ui/i18n/missing.json）")
    for k, v in bad[:15]:
        print("  不合格：", k, "→", v)


if __name__ == "__main__":
    main()
