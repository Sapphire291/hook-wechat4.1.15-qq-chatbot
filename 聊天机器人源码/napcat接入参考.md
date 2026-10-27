# NapCat 接入参考

> 适用版本：聊天机器人 v1.1（OneBot 接口版）
> 当前程序**不内置 NapCat**，也不再启动、注入、扫码或管理 NapCat。
> `napcat_bundle.zip` 不再随源码、`_internal` 或发布包分发，也不参与构建。

---

## 1. 现在保留的接口

QQ 平台只通过 **OneBot v11 正向 WebSocket** 接入：

| 接口 | 位置 |
|---|---|
| QQ 适配器 | `src/integration/qq.py` |
| WebSocket 地址配置 | `config.json → qq_onebot_ws_url`（默认 `ws://127.0.0.1:3001`） |
| 设置界面地址 | 设置 → QQ AI → QQ OneBot WS |
| 插件上下文参数 | `src/plugins/api.py` 的 `napcat` 参数（仅接口占位，不再启动/管理 NapCat） |

适配器使用的事件与 API：

- 事件：`message`（私聊 / 群聊）
- 发送私聊：`send_private_msg`
- 发送群聊：`send_group_msg`
- 获取自身 QQ：`get_login_info`
- 图片/表情：`[CQ:image,file=本地路径]`

---

## 2. 已经移除的 NapCat 部分

以下内容已经从源码、`_internal` 和发布包中移除：

| 已移除 | 说明 |
|---|---|
| `src/napcat_runtime.py` | 解压、启动、注入、WebUI、二维码、自动配 OneBot |
| `src/ui/login.py` | NapCat 扫码登录窗口 |
| 设置页 QQ 登录状态 / 二维码 / 刷新按钮 | 不再显示 |
| `mod/napcat` 插件 | 不再加载 |
| `napcat_bundle.zip` | 不再放在源码根、`_internal`、发布包 |
| `chatbot_full.spec` / `聊天机器人.spec` 里的 NapCat 数据 | 不再打包 |
| `启动.bat` 里拉起 NapCat 的逻辑 | 现在只启动 `聊天机器人.exe` |
| 文档/使用说明里的“内置 NapCat”描述 | 已改成 OneBot v11 外部接入说明 |

---

## 3. 如何连接外部 NapCat / OneBot v11

程序不负责安装 NapCat，需要你自行准备一个兼容 OneBot v11 的实现。

### 步骤

1. **单独安装并登录 NapCat（或兼容实现）**
   - 用目标 QQ 号登录；
   - 确保 NapCat 正常运行。

2. **开启 OneBot v11 正向 WebSocket**
   - 默认地址：`ws://127.0.0.1:3001`；
   - 如果改了端口，后面配置要一致。

3. **配置本程序**
   - 打开程序 → **设置 → QQ AI**；
   - 找到 **QQ OneBot WS**，填：
     ```text
     ws://127.0.0.1:3001
     ```
   - 或者直接改 `config.json`：
     ```json
     "qq_onebot_ws_url": "ws://127.0.0.1:3001"
     ```

4. **启动机器人**
   - 双击 `启动.bat` 或 `聊天机器人.exe`；
   - 程序会连接该 WebSocket 地址；
   - 连接成功后，QQ 消息会进入正常回复流程。

### 连接失败排查

- NapCat/OneBot 是否真的在运行；
- 端口是否与 `qq_onebot_ws_url` 一致；
- 是否启用了「正向 WebSocket」（不是反向 WS）；
- 防火墙是否拦截本机端口；
- NapCat 日志里是否有 QQ 登录失败或 token 校验失败。

---

## 4. `napcat_bundle.zip` 怎么处理

- 当前程序**不需要**它，也不会读取它；
- 源码根目录、`_internal`、发布包里都不应再出现它；
- 如果你自己要在外部部署 NapCat，请把 bundle 解压到 NapCat 自己的目录，不要放回聊天机器人的源码根或 `_internal`；
- 当前构建脚本（两个 spec）已经不包含 `napcat_bundle.zip`。

---

## 5. 给开发者的替换建议

如果你想换一个 OneBot 实现：

1. 保证它兼容 OneBot v11 正向 WebSocket；
2. 实现/确认以下 action：
   - `send_private_msg`
   - `send_group_msg`
   - `get_login_info`
3. 事件至少发送 `message`；
4. 修改 `src/integration/qq.py` 的地址读取和消息解析；
5. 不需要恢复任何 NapCat 运行时、二维码或启动逻辑。

---

## 6. 风险提示

- 本项目不是腾讯/QQ 官方产品；
- NapCat、OneBot 及其他第三方实现均可能违反平台协议、带来账号风控；
- 请自行评估风险，不要用于营销、群控、养号等违规用途；
- 不要把 `config.json`（含 API Key）发给别人。
