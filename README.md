# astrbot_plugin_cs_stats

移植自 [HakuBot](https://github.com/HakuchumuHYX/HakuBot) 的 `cs_stats` 插件，面向 **AstrBot 4.29.4 + SnowLuma 2.4.2（OneBot v11 / aiocqhttp）** 进行完整适配与重构。

支持 **完美世界竞技平台（天梯 / 官匹）** 与 **5E 对战平台（优先排位）** 的 CS2 生涯战绩卡片、单局战绩卡片、多账号绑定管理及 AI 毒舌锐评。

## 特性亮点

- **100% 保留原版 HUD 视觉与功能**：原汁原味移植 CS2 赛后暗色 HUD 卡片（段位牌、逐场 Rating 柱状图、走势折线、完美五维雷达、常打地图/武器、单局 10 人数据表、逐回合条、击杀克星、平台荣誉与趣味标签）。
- **SnowLuma 2.4.2 / OneBot v11 深度适配**：
  - 渲染图片默认以内存字节 / Base64（`Image.fromBytes`）发送，兼容 AstrBot 与 SnowLuma 分离部署 / Docker 容器环境。
  - 自动解析 `@某人` 与引用回复消息（若 `Reply.sender_id` 缺失，自动调用 OneBot v11 `get_msg` 接口补齐），自动忽略 `@机器人` 与回复机器人。
  - 完美登录态失效时，通过 `send_private_msg` 自动私聊通知 AstrBot 管理员（`admins_id`）。
- **AstrBot 4.29.4 原生体验**：
  - 使用 `StarTools.get_data_dir()` 规范存储绑定表与锐评缓存（支持跨平台原子写与损坏备份）。
  - 提供完整 `_conf_schema.json` WebUI 可视化配置。
  - AI 锐评默认直接复用 AstrBot 当前配置的 LLM Provider（无需重复填 Key），也支持切换为 `custom` 模式接入独立 OpenAI 兼容接口。
  - 交互选号使用 `@session_waiter` 并按「会话 + 用户」隔离，多人同时绑定互不干扰。
  - 新增私聊管理员命令 `完美登录 <手机号> <验证码>`，免 SSH 直接更新完美 Token。

## 安装与部署

1. 在 AstrBot 插件市场安装本插件（或将仓库放入 `data/plugins/astrbot_plugin_cs_stats/`）。
2. **渲染后端说明（开箱即用 / 本地极速模式二选一）**：
   - **默认模式（`render.backend = auto`）**：优先使用本地 Playwright Chromium；如果未安装浏览器（例如 Docker 容器首次安装插件），会**自动回退使用 AstrBot 内置文转图（`html_render`）**并内联字体，无需额外操作即可出图。
   - **本地 Playwright 模式（推荐，出图更快更稳定）**：在 AstrBot 宿主机或 Docker 容器内执行一次即可：
     ```bash
     docker exec -it <你的AstrBot容器名> playwright install chromium
     # 或非 Docker 环境直接运行：
     playwright install chromium
     ```
     > Linux / Docker 环境下如缺少系统依赖或中文字体，可执行 `playwright install-deps chromium`，并安装 `fonts-noto-cjk` / `fonts-wqy-zenhei`（或在 `render.font_dir` 中放入 `MiSans-*.ttf`）。
3. **国内网络优化**：完美头像默认从 Steam 拉取，超时已缩短至 **3 秒**（`steam_avatar_timeout`），若国内服务器直连 Steam 频繁超时，可在插件配置中将其设为 `0`（跳过 Steam 头像、使用首字母头像）或填写 `steam_proxy`。
4. 确保已在 AstrBot **平台管理** 中通过 `aiocqhttp`（反向 WebSocket）连接到 SnowLuma 2.4.2，并在 **访问控制 -> 管理员** 中配置管理员 QQ 号（字符串格式）。

## 完美世界登录配置

5E 对战平台全部接口免登录，开箱即用；完美天梯与官匹需要配置一个完美电竞 App 登录态（`pw_token` 与 `pw_steam_id`）：

- **方式一（推荐，私聊机器人）**：
  1. 在手机端「完美世界电竞」App 登录页输入手机号并点击获取验证码（**不要在 App 里点击登录**）。
  2. 用管理员 QQ 私聊机器人发送：`完美登录 <手机号> <验证码>`。
- **方式二（CLI 脚本）**：
  ```bash
  python scripts/pw_login.py --config /path/to/AstrBot/data/config/astrbot_plugin_cs_stats_config.json
  ```
  完成后向机器人发送 `完美重载` 即可热生效。

## 命令一览

默认无前缀及 `/` 前缀均可触发（可在 WebUI 修改 `command_prefix`）：

### 账号管理
- `完美绑定 <昵称或SteamID64>` / `官匹绑定 <昵称或SteamID64>`
- `5e绑定 <昵称或5E个人主页ID>`（大小写不敏感）
- `完美账号` / `官匹账号` / `5e账号`
- `完美主号 #序号` / `5e主号 #序号`
- `完美解绑 [#序号]` / `5e解绑 [#序号]`

### 战绩查询与 AI 锐评
- `完美战绩` / `官匹战绩` / `5e战绩`（支持 `[@某人|回复] [#序号]`）
- `完美单局` / `官匹单局` / `5e单局`（支持 `[1~20] [@某人|回复] [#序号]`）
- `完美锐评` / `官匹锐评` / `5e锐评`（管理员附加 `-r` 或 `刷新` 可强制重新生成）
- `完美单局锐评` / `官匹单局锐评` / `5e单局锐评`（支持 `[1~20]` 及管理员 `-r` / `刷新`）

### 管理员与帮助命令
- `cs查询 <完美|官匹|5e> <昵称或ID>`：免绑定直接查询任意玩家
- `完美登录 <手机号> <验证码>`：仅限私聊，自动写入 Token 并恢复完美查询
- `完美重载`：重载配置并清除完美失效状态
- `cs帮助`：查看命令速查表
