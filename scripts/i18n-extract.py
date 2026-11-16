"""从界面源码里抽出所有中文文案，生成 ui/i18n/keys.json（翻译词典的键）。

规则：JS 字符串 / 模板里的文字按 HTML 标签切成一段一段（就是渲染后的一个文字节点），
${…} 换成 {0}{1}… 占位；标签里 title / placeholder / aria-label 的值单独算一段。
占位符在中间切开的每一小段也单独收（变量是图标之类的元素时，文字节点只剩那一小段）。
用法：python3 scripts/i18n-extract.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r"[㐀-鿿！-～　-〿]")
FILES = ["ui/app.js", "ui/hud.js", "ui/hud-stage.js", "ui/shared.js", "ui/index.html", "ui/hud.html"]


def literals(src):
    """粗略的 JS 词法：返回所有字符串 / 模板字面量的“文字”，模板里的 ${} 记成 \0。"""
    out, i, n = [], 0, len(src)

    def template(i):  # src[i] == '`'
        buf, i = [], i + 1
        while i < n:
            c = src[i]
            if c == "\\":
                buf.append(src[i:i + 2]); i += 2; continue
            if c == "`":
                return "".join(buf), i + 1
            if c == "$" and src[i + 1:i + 2] == "{":
                buf.append("\0")
                i = expr(i + 2)
                continue
            buf.append(c); i += 1
        return "".join(buf), i

    def string(i, q):
        buf, i = [], i + 1
        while i < n and src[i] != q:
            if src[i] == "\\":
                buf.append(src[i + 1:i + 2]); i += 2; continue
            buf.append(src[i]); i += 1
        return "".join(buf), i + 1

    def expr(i):  # 读到配对的 }，里面的字符串 / 模板也收进来
        depth = 1
        while i < n:
            c = src[i]
            if c == "/" and not src.startswith("//", i) and not src.startswith("/*", i) and regex_ok(i):
                i = skip_regex(i); continue
            if c in "'\"":
                s, i = string(i, c); out.append(s); continue
            if c == "`":
                s, i = template(i); out.append(s); continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return i

    def regex_ok(i):  # 这个 / 前面是运算位置（( , = : [ ! & | ? { } ; return 等）才是正则字面量，不是除号
        j = i - 1
        while j >= 0 and src[j] in " \t\n":
            j -= 1
        if j < 0 or src[j] in "(,=:[!&|?{};+-*%<>~^":
            return True
        return bool(re.search(r"(?:return|typeof|case|in|of)$", src[max(0, j - 6):j + 1]))

    def skip_regex(i):
        i += 1
        cls = False
        while i < n and src[i] != "\n":
            c = src[i]
            if c == "\\":
                i += 2; continue
            if c == "[":
                cls = True
            elif c == "]":
                cls = False
            elif c == "/" and not cls:
                i += 1
                while i < n and src[i].isalpha():
                    i += 1
                return i
            i += 1
        return i

    while i < n:
        c = src[i]
        if c == "/" and not src.startswith("//", i) and not src.startswith("/*", i) and regex_ok(i):
            i = skip_regex(i); continue
        if src.startswith("//", i):
            i = src.find("\n", i); i = n if i < 0 else i; continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        if c in "'\"":
            s, i = string(i, c); out.append(s); continue
        if c == "`":
            s, i = template(i); out.append(s); continue
        i += 1
    return out


def renumber(seg):
    k = 0
    def rep(_):
        nonlocal k
        k += 1
        return "{" + str(k - 1) + "}"
    return re.sub("\0", rep, seg)


def segments(text):
    segs = []
    for m in re.finditer(r'(?:title|placeholder|aria-label|alt)="([^"]*)"', text):  # 属性值
        segs.append(m.group(1))
    for part in re.split(r"<[^>]*>", text):
        segs.append(part)
    keys = set()
    for s in segs:
        s = re.sub(r"\s+", " ", s).strip()
        if not CJK.search(s):
            continue
        keys.add(renumber(s))
        if "\0" in s:  # 占位符切开的小段
            for p in s.split("\0"):
                p = p.strip()
                if CJK.search(p):
                    keys.add(p)
    return keys


def html_text(src):
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"<script.*?</script>|<style.*?</style>", "", src, flags=re.S)
    return segments(src)


def main():
    keys = set()
    for f in FILES:
        src = (ROOT / f).read_text("utf-8")
        if f.endswith(".html"):
            keys |= html_text(src)
        else:
            for lit in literals(src):
                keys |= segments(lit)
    keys = sorted(k for k in keys if len(k) < 400)
    out = ROOT / "ui" / "i18n" / "keys.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(keys, ensure_ascii=False, indent=0), "utf-8")
    print(len(keys), "条 →", out)



# ---------- 后端送到界面上的短文案（职能分类、额度文字、动态、系统消息……） ----------
PY = ["core/catalog.py", "core/quota.py", "core/activity.py", "core/chat.py", "core/service.py", "core/setup.py", "core/wallet.py", "core/update.py", "core/config.py", "core/brains.py", "core/scheduler.py", "core/connectors.py"]


def py_keys():
    import ast
    keys = set()
    for f in PY:
        tree = ast.parse((ROOT / f).read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                parts = []
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    else:
                        parts.append("\0")
                s = "".join(parts)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
            else:
                continue
            if len(s) > 120 or "\n" in s.strip():  # 长的是给 agent 的提示词，不在界面上
                continue
            keys |= segments(s)
    return keys


_main = main


def main():  # noqa: F811
    _main()
    out = ROOT / "ui" / "i18n" / "keys.json"
    keys = set(json.loads(out.read_text("utf-8"))) | py_keys()
    keys = sorted(k for k in keys if len(k) < 400)
    out.write_text(json.dumps(keys, ensure_ascii=False, indent=0), "utf-8")
    print("加上后端：", len(keys), "条")


if __name__ == "__main__":
    main()
