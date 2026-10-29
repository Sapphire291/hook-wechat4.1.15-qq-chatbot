# 聊天机器人 · 微信 / QQ 双平台对话 Agent（内置 DSH 内核，含微信 Hook 源码）

> 一个跑在你自己 Windows 电脑上的私人聊天 Agent：读你**已登录**的微信和 QQ，在白名单会话里替你跟人聊天——
> 会联网查资料并给出来源、会按人记忆、会自己挑表情包、长回复会分几条发、聊不懂会追问。
> 本仓库包含：**Python 主程序源码 + 微信 Hook DLL 源码 + 第三方依赖清单 + 开发规范 + 文档**。

> ★ **只想要能直接跑的版本** → 看 [`聊天机器人源码/README.md`](聊天机器人源码/README.md) 的“快速开始”，或下载 Release。
> ★ **想看功能、架构与设计取舍** → 同目录 `README.md`（完整版）；本文件只做仓库导航。
> ★ **想自己编译 Hook / 主程序** → 看 [`说明.md`](说明.md) 的构建步骤。
> ★ **想看使用手册** → [`聊天机器人_使用说明.docx`](聊天机器人_使用说明.docx)。

---

## 核心能力（含 DSH 内核）

- **工作文档浏览**：微信 / QQ 收到的文档会落到当前会话的记忆目录；聊天中可调用 `read_file`，由文档智能体并行阅读并生成摘要，不靠记忆猜内容。
- **内置 DSH 内核**：主程序内置 `NativeHarness`（DSH 兼容内核），统一负责会话、记忆、模型调用、思考分档与工具调度，不需要启动外部 Node / Electron / DSH 进程。
- **跨会话记忆打通**：不同单聊 / 群聊可以合并成一个**记忆组**共享长期记忆，也可以随时**拆回独立记忆**；合并时保留共同约定与人格，拆分时只复制该成员相关部分，避免泄露其他成员隐私。
- **DSH 思考分档**：由内置内核完成思考区分——简单对话关闭思考、优先速度；兴趣话题使用轻思考；重要复杂内容保留完整思考。
- **内置联网搜索**：DSH 内核内置 `web_search` / `web_fetch`，命中时引用来源 URL；检索失败会明确说明，不编造数字、日期或链接。

---

## 致谢与来源声明（必读）

> 本项目**记忆系统部分**使用了来自 **cyanwell 的 dsh-agent-soul** 的源代码。
> 仓库地址：https://github.com/cyanwell/dsh-agent-soul
>
> 主要使用了其中 **ECHO（回声记忆）、墨（账本）、宪章** 三项功能，并做了以下改造：
> ① **ECHO**：增加“进入记忆前预压缩”；超出字数后删除原文并做“记忆的二次压缩”；若压缩后失真则直接剔除该段记忆。
> ② **宪章**：改造为“功能提示”，稳定注入 AI 底层，避免 AI 遗忘自己的能力渠道。
> ③ **墨**：用于限制 AI 的思考，分三档 —— 简单对话禁用思考以提升回复速度；复杂对话通过墨限制思考长度；任务型对话保留完整思考能力。
>
> 关于 QQ 接入：项目内置 **NapCat**（文档：https://napneko.github.io/guide/napcat），可直接在 exe 内扫码登录。

---

## 仓库结构

```
.
├─ 聊天机器人源码/            # Python 主程序（GUI、pipeline、记忆、联网、表情、测试）
│  ├─ src/                    # core / ai / integration / memory / emoji / web / ui …
│  ├─ tests/                  # 离线测试（联网测试加 --online）
│  ├─ assets/                 # 功能说明 + 默认表情库说明（素材不入库）
│  ├─ hook/                   # 当前 wxhook415.dll（构建打包需要）
│  ├─ mod/                    # 插件（napcat / wechat_hook）
│  ├─ README.md               # ★ 完整功能说明与设计取舍（建议先读这个）
│  └─ 代码规范.md / 接入规范.md
├─ 微信Hook源码/              # 微信 4.1.15.56 Hook DLL（C++ / MSVC）
│  ├─ src/ include/ xdb/      # 钩子实现、偏移表、数据库模块
│  ├─ 3rdparty/ minhook/      # 第三方头文件与 hook 库
│  ├─ postman/                # Hook HTTP API 的 Postman 集合
│  ├─ 防止微信自动更新/        # 辅助 bat 脚本
│  └─ x64_Version_dll.sln     # VS 解决方案（构建产物不入库）
├─ 依赖项/                    # wechatauto（第三方微信 UI 自动化库）+ requirements
├─ 代码规范.md / 接入规范.md   # 开发规范
├─ 说明.md                    # 源码/依赖/构建方式说明
├─ 聊天机器人_项目说明.docx     # 项目说明（Word，含致谢与架构）
├─ 聊天机器人_使用说明.docx     # 使用手册（Word）
└─ .gitignore
```

## 仓库里没有、需要单独获取的东西

为了仓库体积与你的账号素材安全，以下内容**没有入库**：

| 内容 | 原因 | 获取方式 |
|---|---|---|
| `聊天机器人源码/assets/emoji/wechat_fav/` | 约 147MB，来自个人微信收藏的表情素材 | 程序首次运行会从你的微信账号自动扫描生成；或从 Release 素材包获取 |
| `依赖项/napcat_bundle.zip` | 与主程序目录内的同名文件重复（约 34MB） | 保留在 `聊天机器人源码/napcat_bundle.zip`（已入库，便于构建） |
| `微信Hook源码/运行库/` | VC++ 运行库安装包（约 31MB，微软官方可下载） | 安装 Microsoft Visual C++ Redistributable 即可 |
| `微信Hook源码/x64/`、`x64_Version/` | MSVC 构建产物与日志 | 用 `x64_Version_dll.sln` 自行编译 |

## 快速开始

**用发布版（推荐）**：下载 Release 中的便携包 → 双击 `启动.bat` → 设置里填 API Key/白名单 → QQ 扫码。
**跑源码**：

```powershell
cd 聊天机器人源码
pip install -r requirements.txt
python main.py
# 打包：python -m PyInstaller chatbot_full.spec --noconfirm --clean
```

**编译 Hook**：见 [`说明.md`](说明.md) 的 “微信 Hook DLL” 一节（需要 MSVC / VS2022+）。

## 文档

- 功能说明、架构、思考分档、更新记录：[`聊天机器人源码/README.md`](聊天机器人源码/README.md)
- 项目说明（Word，含致谢/记忆系统详解）：`聊天机器人_项目说明.docx`
- 使用说明（Word，安装/设置/维护/FAQ）：`聊天机器人_使用说明.docx`
- 开发规范：[`代码规范.md`](代码规范.md) / [`接入规范.md`](接入规范.md)
- 源码与构建：[`说明.md`](说明.md)

## 安全与合规

- 本程序只按**白名单**回复，不群发、不加好友、不做营销；
- 微信 Hook 与 QQ NapCat 均为第三方接入方式，可能违反平台协议或带来账号风险，请自行评估；
- 仅供个人学习与自用；请勿用于骚扰、冒充或任何违法用途；
- `config.json`（含 API Key）与运行数据均已加入 `.gitignore`，请勿提交。

## GitHub 设置建议

- 仓库名：`wechat-qq-agent`（或 `wechat-qq-chatbot`）
- About：`让 AI 替你聊微信和 QQ：会联网查资料并给出来源、按人记住细节、自己挑表情包，长回复分几条发。Windows 本地运行，便携 exe。`
- Topics：`wechat qq napcat onebot deepseek chatbot llm-agent windows memory`
