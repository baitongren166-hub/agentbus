# AgentBus 更新日志

日常改动记在「未发布」下。发版前，将本次要发布的条目归入 `## [版本号] - YYYY-MM-DD`，例如 `## [0.6.1] - 2026-11-21`。版本号与 Git 标签一致，标题里省略 `v`。正文首行用一句话概括本次更新，供 App 的更新横幅展示，后面列出具体变化。

带版本标签的构建会把对应版本的正文写入 GitHub Release 草稿。App 从已发布的 GitHub Release 获取更新内容。

## [未发布]

修复英特尔 Mac 上悬浮窗闪一下就消失的问题；招募分桌面版和命令行版，只装了桌面版的 ZCode 也能接上命令行、进聊天室。

- 修复英特尔 Mac 上悬浮窗闪一下就消失的问题（未在英特尔机器上实测）。本地服务慢的时候不再被当成挂了，也不再重复拉起服务、反复刷新悬浮窗。悬浮窗每 10 秒报一次平安，45 秒收不到就自动重新加载。悬浮窗不再跟着应用一起收起。应用的事件记在临时目录的 `agentbus-app.log`。
- 招募分桌面版和命令行版：两行各自显示装没装，能打开、去官网下载、一键安装或卸载（桌面版卸载是移到废纸篓）。GPT（Codex CLI）和 opencode 增加了一键安装。
- 只装了 ZCode 桌面版的电脑可以「从桌面版接上」命令行：用桌面版自带的运行环境生成 `~/.local/bin/zcode`，不用另装 Node.js。
- 队员卡片上可以直接打开桌面版和命令行。聊天室顶栏加了「添加队员」，频道设置里会列出还没有命令行、因此进不了聊天室的队员。

## [0.6.1] - 2026-11-22

修复派活、模型选择和任务恢复，聊天室提供可读的失败原因和解决入口。

### 相比 v0.6.0

- 派活默认走命令行。CLI 未登录时可以直接打开对应登录入口。只有 CLI 无法启动时才提供桌面端选项，并由用户确认。
- 反重力离线或退出登录时仍保留模型列表和已保存的选择。下次派活使用保存的模型，也支持从自定义安装目录刷新模型。
- WorkBuddy 通过 CodeBuddy CLI 执行，悬浮窗可查看回复、工具调用和结果。修复阶段汇报误判、打断后丢失会话，以及并发补充指令丢失。
- 豆包通过 Headless CLI 执行。Bus 保留已受理任务的会话和任务编号，等待超时、中止或服务重启后可用它们在豆包核对原任务。
- 聊天室遇到登录、并发上限、模型不可用、限流或网络错误时，会说明原因并提供登录、设置、用量或任务记录入口。悬浮窗保留未派出的草稿。
- 看门狗只读取与工单匹配的会话日志。重试会带上用户在工单里已发的补充。修复失败后误用上轮回复、终端打不开却停止后台任务，以及任务记录串项目的问题。
- Claude 及其分身永久封印。AgentBus 关闭了它们的派活、登录、探测和桌面端、CLI 启动入口。

已有用户可在「设置 → 关于 → 现在检查」查看更新，下载安装包后覆盖安装。启动新版后，App 会更新仍在运行的旧版后台服务，并继续使用原来的本机项目和任务数据。

### English

- CLI-first dispatch with sign-in actions and explicit confirmation before a desktop fallback.
- Antigravity keeps saved models and the last known catalog offline. WorkBuddy shows live progress and resumes its CLI session after interruption. Doubao uses its Headless CLI and preserves accepted job IDs.
- Chat explains common failures and links to the relevant next step. Failed submissions keep the draft in the floating island.
- The watchdog follows the assigned session and retains follow-up instructions on retry. The app replaces an older running bundled service during upgrade. Claude and its clones are permanently sealed against launch and dispatch by AgentBus.

[Compare v0.6.0 with v0.6.1](https://github.com/baitongren166-hub/agentbus/compare/v0.6.0...v0.6.1)
