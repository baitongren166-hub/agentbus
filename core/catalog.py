"""agentbus 的固定资料：岗位词条分类、市面上常见 agent 的接入方式。

用户自己的改动（谁在办公室、谁挂什么岗位、偏好）不写在这里，写在 ~/.agentbus/config.json。
"""

# 岗位词条：大类 → 具体词条。分配时可以给一整个大类，也可以单独给某个词条；单独配的优先。
CATEGORIES = [
    ("coord", "统筹管理", ["统筹派活", "任务拆解", "进度跟踪", "验收质检", "复盘总结", "会议主持", "决策建议", "风险评估"]),
    ("writing", "文字创作", ["文案策划", "短视频脚本", "长文写作", "标题配文", "故事小说", "诗歌歌词", "演讲稿", "广告语"]),
    ("editing", "编辑校对", ["审稿改稿", "润色降 AI 味", "校对纠错", "摘要提炼", "改写扩写", "风格模仿", "事实核查"]),
    ("lang", "翻译语言", ["中英翻译", "多语种翻译", "本地化", "字幕翻译", "术语整理"]),
    ("visual", "视觉设计", ["封面", "海报", "插画", "图标 Logo", "UI 设计", "信息图", "PPT", "修图抠图", "聊天截图 / 仿真图", "表情包制作"]),
    ("av", "音视频", ["视频剪辑", "字幕", "配音 TTS", "音效配乐", "打码后期", "录屏", "动画动效", "直播切片"]),
    ("computer", "电脑操作", ["操控桌面应用", "操控浏览器", "填写表单", "批量整理文件", "跑脚本", "定时任务", "手机 / 模拟器操作"]),
    ("research", "调研分析", ["网络搜索", "竞品分析", "行业研究", "论文文献", "读文档 PDF", "数据核对", "舆情监测"]),
    ("assets", "素材搜集", ["找图片素材", "找表情包", "找视频素材", "找音乐音效", "版权检查"]),
    ("dev", "开发技术", ["写代码", "修 bug", "代码审查", "测试", "前端页面", "后端接口", "数据库", "部署运维", "自动化脚本"]),
    ("office", "数据办公", ["表格处理", "飞书 / 多维表格", "文档整理", "报表", "数据分析可视化", "邮件草稿", "会议纪要"]),
    ("ops", "运营营销", ["选题策划", "小红书运营", "抖音运营", "评论回复草稿", "SEO", "投放分析", "社群文案"]),
    ("edu", "学习教育", ["出题", "讲解答疑", "课件", "批改", "学习计划"]),
    ("ideation", "头脑风暴", ["出点子", "反方质疑", "方案对比", "起名命名"]),
]

LEVELS = {"prefer": "首选", "ok": "可用", "avoid": "不用"}

# 能力标签：经验包里每一步写“需要什么能力”，按能力匹配到本机的 agent（第一期按外壳标，模型带来的能力以后叠加）
CAPS = {"computer": "操控电脑", "browser": "操控浏览器", "image": "生图", "files": "读写文件", "code": "写代码",
        "web": "联网查资料", "vision": "看图", "long": "长文本", "cn": "中文写作", "cheap": "便宜跑批量"}


def tag_key(tag):
    return "tag:" + tag


def cat_key(cid):
    return "cat:" + cid


def category_of(tag):
    for cid, _, tags in CATEGORIES:
        if tag in tags:
            return cid
    return None


def all_tags():
    return [t for _, _, tags in CATEGORIES for t in tags]


# 能接入的 agent。run：怎么无界面地派活；acts：从哪读它在干什么；billing：额度怎么算。
#   billing = codex（opencodex 额度百分比）/ deepseek（DeepSeek 账户余额）/ claude（订阅额度，暂未接入）/ free / manual
#   sprite = 有 Q 版小人的用小人，没有的用图标
KNOWN_AGENTS = {
    "claude":   {"name": "Claude", "caps": ["files", "code", "web", "long", "vision", "cn"], "abbr": "C", "vendor": "Anthropic", "bin": "claude", "run": "claude", "acts": "claude",
                 "billing": "claude", "sprite": "claude.png", "color": "#d97757", "desc": "Claude Code"},
    "codex":    {"name": "GPT", "caps": ["computer", "browser", "image", "files", "code", "web", "vision"], "abbr": "G", "vendor": "OpenAI", "bin": "codex", "run": "codex", "acts": "codex",
                 "billing": "codex", "sprite": "gpt.png", "color": "#8b7fd6", "desc": "Codex CLI，可操控电脑、生图"},
    "opencode": {"name": "opencode", "caps": ["computer", "files", "code", "cheap"], "abbr": "O", "vendor": "DeepSeek", "bin": "opencode", "run": "opencode", "acts": "opencode",
                 "billing": "deepseek", "sprite": "opencode.png", "color": "#4f9a6a", "desc": "opencode + DeepSeek，带电脑操控插件"},
    "deepseek": {"name": "DeepSeek", "caps": ["files", "code", "cheap", "cn"], "abbr": "DS", "hidden": True, "vendor": "DeepSeek", "bin": "opencode", "run": "opencode", "acts": None,
                 "billing": "deepseek", "sprite": None, "color": "#4d6bfe", "desc": "opencode 跑 DeepSeek 的杂活号（和 opencode 共用日志）"},
    "zcode":    {"name": "zcode", "caps": ["computer", "files", "code", "cheap"], "abbr": "Z", "vendor": "智谱 / DeepSeek", "bin": "zcode", "run": "zcode", "acts": "zcode",
                 "billing": "deepseek", "sprite": "zcode.png", "color": "#3f7fd0", "desc": "ZCode CLI，当前默认模型 DeepSeek"},
    # Google 的 agent：反重力 CLI（agy）。Gemini CLI 的个人免费额度 Google 已经停了，个人账号走反重力；id 沿用 gemini（职能、战备都不丢）
    "gemini":   {"name": "Antigravity", "caps": ["files", "code", "long", "vision", "cn", "web"], "abbr": "AG", "vendor": "Google", "bin": "agy", "run": "agy", "acts": None,
                 "billing": "antigravity", "sprite": "gemini.png", "color": "#7b6cf0", "desc": "反重力 CLI（agy），Google 个人账号的 Gemini 额度走它"},
    "dsh":      {"name": "DSH", "caps": ["files", "code", "cheap", "cn"], "abbr": "DSH", "vendor": "DeepSeek", "bin": "dsh", "run": "dsh", "acts": None,
                 "billing": "deepseek", "sprite": None, "color": "#8b6cf6", "desc": "DeepSeek Harness（dsh --profile headless），需要先配 API key"},
    "codebuddy": {"name": "WorkBuddy", "caps": ["files", "code", "cn"], "abbr": "WB", "vendor": "腾讯", "bin": "codebuddy", "run": "codebuddy", "acts": None,
                  "billing": "none", "sprite": None, "color": "#14b8a6", "desc": "CodeBuddy Code CLI（WorkBuddy 同源），需要另装"},
    "doubao":   {"name": "豆包", "caps": ["cn", "web", "cheap"], "abbr": "豆", "vendor": "字节跳动", "bin": None, "run": "doubao", "acts": None,
                 "billing": "free", "sprite": None, "color": "#25a5f0", "desc": "豆包电脑版，走豆包遥控（调试端口 9225）"},
    "qwen":     {"name": "Qwen Code", "caps": ["files", "code", "cn", "cheap"], "abbr": "Q", "vendor": "阿里", "bin": "qwen", "run": "generic", "acts": None,
                 "billing": "none", "sprite": None, "color": "#615ced", "desc": "qwen -p", "cmd": ["qwen", "-p", "{prompt}"]},
    "kimi":     {"name": "Kimi CLI", "caps": ["files", "code", "long", "cn"], "abbr": "K", "vendor": "月之暗面", "bin": "kimi", "run": "generic", "acts": None,
                 "billing": "none", "sprite": None, "color": "#3d3d46", "desc": "kimi --print", "cmd": ["kimi", "--print", "-c", "{prompt}"]},
    "cursor":   {"name": "Cursor Agent", "caps": ["files", "code"], "abbr": "Cu", "vendor": "Cursor", "bin": "cursor-agent", "run": "generic", "acts": None,
                 "billing": "none", "sprite": None, "color": "#5b5b66", "desc": "cursor-agent -p", "cmd": ["cursor-agent", "-p", "{prompt}"]},
}

# 一键打开：桌面端（/Applications 里的 App 名，或网页地址）和命令行
OPENERS = {
    "claude": {"app": "Claude", "cli": "claude"},
    "codex": {"app": "ChatGPT", "cli": "codex"},
    "opencode": {"app": "OpenCode", "cli": "opencode"},
    "zcode": {"app": "ZCode", "cli": "zcode"},
    "gemini": {"app": "Antigravity", "cli": "agy"},  # 反重力
    "dsh": {"app": "DeepSeek Harness", "cli": "dsh"},
    "codebuddy": {"app": "WorkBuddy", "cli": "codebuddy"},
    "doubao": {"app": "Doubao 2"},
    "qwen": {"cli": "qwen"},
    "kimi": {"app": "Kimi", "cli": "kimi"},
    "cursor": {"app": "Cursor", "cli": "cursor-agent"},
}
# Windows 上桌面端 exe 的名字（和 Mac 的 App 名不一样的才写）
for _k, _exe in {"codex": "ChatGPT", "doubao": "Doubao", "codebuddy": "WorkBuddy"}.items():
    OPENERS[_k]["win_app"] = _exe

# 可招募的 agent：怎么装（界面上点“招募”会先弹窗确认，把命令给用户看）
#   npm：Node 包；uv：Python 工具；mac/win：只在那个系统上能用的安装脚本
INSTALL_SPEC = {
    "codebuddy": {"npm": "@tencent-ai/codebuddy-code"},
    "qwen": {"npm": "@qwen-code/qwen-code"},
    "kimi": {"uv": "kimi-cli", "python": "3.13"},
    "cursor": {"mac": "curl -fsS https://cursor.com/install | bash"},  # Cursor 命令行版暂时没有 Windows 一键安装
}
# 没有可靠一键安装命令的：招募时打开官网下载页（反重力的 agy 命令随桌面端一起装）
INSTALL_GUIDE = {"gemini": "https://antigravity.google/download"}
NEEDS = {"npm": ("npm", "Node.js", "https://nodejs.org"), "uv": ("uv", "uv（Python 工具管理）", "https://docs.astral.sh/uv/")}


def _install_dir(install_dir):
    """主人选的安装位置：展开 ~，必须是绝对路径、不带控制字符；不合规返回 None 并报错给调用方。"""
    import os as _os
    if not install_dir:
        return None
    d = _os.path.expanduser(str(install_dir).strip())
    if not _os.path.isabs(d) or any(ord(c) < 32 for c in d):
        raise ValueError("安装位置要写完整路径（比如 ~/tools 或 D:\\tools）")
    return d


def install_argv(aid, install_dir=None):
    """真正执行用：(参数列表, 额外环境变量)。目录作为单独参数 / 环境变量传，不拼进 shell，路径里有 $() 也只是普通字符。
    只有 catalog 里写死的官方安装脚本（不带目录）才走 shell，返回 (None, 脚本)。"""
    import shutil
    spec = INSTALL_SPEC.get(aid)
    if not spec:
        return None
    d = _install_dir(install_dir)
    if "npm" in spec:
        npm = shutil.which("npm") or "npm"
        return [npm, "install", "-g", *(["--prefix", d] if d else []), spec["npm"]], {}
    if "uv" in spec:
        import os as _os
        env = {"UV_TOOL_DIR": _os.path.join(d, "tools"), "UV_TOOL_BIN_DIR": _os.path.join(d, "bin")} if d else {}
        return [shutil.which("uv") or "uv", "tool", "install", "--python", spec.get("python", "3.12"), spec["uv"]], env
    script = install_cmd(aid)
    return (None, script) if script else None


def install_cmd(aid, install_dir=None, os_name=None):
    """给主人看的安装命令（按对应 shell 的规矩加引号）；装不了返回 None。真正执行用 install_argv。"""
    import os as _os
    import shlex
    spec = INSTALL_SPEC.get(aid)
    if not spec:
        return None
    win = (os_name or ("windows" if _os.name == "nt" else "mac")) == "windows"
    try:
        d = _install_dir(install_dir)
    except ValueError:
        d = None
    q = (lambda v: "'" + v.replace("'", "''") + "'") if win else shlex.quote  # PowerShell 单引号里只有 ' 要双写
    if "npm" in spec:
        return f'npm install -g --prefix {q(d)} {spec["npm"]}' if d else f"npm install -g {spec['npm']}"
    if "uv" in spec:
        base = f"uv tool install --python {spec.get('python', '3.12')} {spec['uv']}"
        if not d:
            return base
        sep = "\\" if win else "/"
        return (f'$env:UV_TOOL_DIR={q(d + sep + "tools")}; $env:UV_TOOL_BIN_DIR={q(d + sep + "bin")}; {base}' if win
                else f'UV_TOOL_DIR={q(d + "/tools")} UV_TOOL_BIN_DIR={q(d + "/bin")} {base}')
    return spec.get("win" if win else "mac")


def install_needs(aid):
    """装之前要先有什么（比如 Node.js）；本机缺的话返回 (名字, 官网)。"""
    import shutil
    spec = INSTALL_SPEC.get(aid) or {}
    for k, (cmd, name, url) in NEEDS.items():
        if k in spec and not shutil.which(cmd):
            return {"name": name, "url": url}
    return None


INSTALL = {k: install_cmd(k) for k in INSTALL_SPEC}  # 默认位置的命令（老代码和界面展示用）
INSTALL = {k: v for k, v in INSTALL.items() if v}


DEFAULT_CONFIG = {
    # 新装的电脑：先空着，第一次打开时自动评估本机的 agent 再装配（core/setup.py）
    "roster": {"office": [], "lounge": []},
    "jobs": {},
    "custom_agents": {},
}
