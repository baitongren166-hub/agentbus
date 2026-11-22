"""从 App 更新日志提取 Git 标签对应的 GitHub Release 正文。"""

import argparse
import re
from pathlib import Path


CHANGELOG = Path(__file__).resolve().parents[1] / "app" / "CHANGELOG.md"
SECTION = re.compile(r"^##[ \t]+.*$", re.MULTILINE)


def release_notes(changelog, version):
    headings = list(SECTION.finditer(changelog))
    version_heading = re.compile(rf"##[ \t]+\[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*")
    matches = [index for index, heading in enumerate(headings) if version_heading.fullmatch(heading.group())]
    if len(matches) != 1:
        raise ValueError(f"app/CHANGELOG.md 需要且只能有一个 [{version}] 版本章节。")
    index = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
    notes = changelog[headings[index].end():end].strip()
    if not notes:
        raise ValueError(f"app/CHANGELOG.md 的 [{version}] 章节需要填写更新内容。")
    return notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Git 版本标签，例如 v0.6.1")
    parser.add_argument("output", type=Path, help="Release 正文输出路径")
    args = parser.parse_args()
    try:
        notes = release_notes(CHANGELOG.read_text(encoding="utf-8"), args.tag.removeprefix("v"))
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    args.output.write_text(notes + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
