# 麦麦要换头像

`github.cateye.mai.set_avatar` · v1.0.2 · MIT

让 MaiBot 机器人维护一个本地头像库，按每日计划或按需更换 QQ 头像，免去每次手动换图的麻烦。适合自建 MaiBot、已接入 SnowLuma 或 NapCat 适配器的 bot 维护者。

- **每日随机换头像**：默认 08:30 触发，叠加随机漂移，按概率决定当天是否跳过。
- **头像库管理**：`/msa add` 收集图片、`/msa bot` 捕获当前头像、`/msa list` 按序号浏览、`/msa remove` 删除。
- **首次自动入库**：插件首次启用时自动捕获 bot 当前头像。
- **LLM 工具**：`change_avatar` 在用户明确要求换头像时调用，带每日上限和冷却；不可用时对模型隐藏。
- **更换提醒**：成功更换后可在 planner / replyer 上下文注入带时间的提醒。
- **配置面板**：宿主 WebUI 中提供分组配置，保存后热更新。

## 安装与启用

### 环境要求

| 项 | 要求 |
|---|---|
| 宿主 MaiBot | `1.0.0`–`1.99.99` |
| 插件 SDK | `2.5.0`–`2.99.99` |
| Python 包 | `Pillow>=9.0.0`、`aiohttp>=3.8.0`、`yarl>=1.0` |
| QQ 适配器 | `maibot-team.snowluma-adapter`（SnowLuma 合并版）或 `maibot-team.napcat-adapter`（NapCat）之一 |

插件清单没有声明适配器依赖，换头像接口在运行时探测；适配器需要已安装并在宿主中启用。

插件声明的能力：`send.text`、`send.forward`、`message.get_by_id`、`api.call`、`api.get`、`config.get`、`component.enable`、`component.disable`。

### 安装步骤

1. 把插件目录放入 MaiBot 的 `plugins/` 下，重启 MaiBot。
2. 在宿主 WebUI 的插件配置中启用 `plugin.enabled`。
3. 确认 SnowLuma 或 NapCat 适配器已配置并连接。
4. 把头像图片放进插件数据目录的 `image/`，或使用 `/msa add`；管理员可用 `/msa debug random` 立即验证。

插件数据目录由宿主分配，通常为 `data/plugins/github.cateye.mai.set_avatar/`。插件会在其中创建 `image/`，并读写：

- `state.json`：每日计划、工具用量、更换提醒时间戳、首次捕获标记。
- `avatar_index.json`：头像序号表。
- `thumb_cache/`：列表缩略图缓存，生成列表时创建，插件启动时清空。

### 适配器探测

换头像接口按以下顺序探测：

1. `adapter.napcat.account.set_qq_avatar`
2. `maibot-team.snowluma-adapter.adapter.napcat.account.set_qq_avatar`
3. `maibot-team.napcat-adapter.adapter.napcat.account.set_qq_avatar`

捕获当前头像用的 `get_login_info` 同样按“短名 → SnowLuma 全名 → NapCat 全名”的顺序探测。两个适配器都安装时，短名可用就直接使用；短名不可用时按上述全名顺序回退。找不到接口时，换头像或捕获会失败，日志提示安装两者之一。

换头像时通过 `set_qq_avatar` 的 `file` 参数传入 `base64://` 内联 PNG。只换头像不依赖 `get_login_info`；`/msa bot` 和首次自动捕获才需要它。

### 首次运行

`plugin.enabled=true` 且 `state.json` 中没有 `initial_avatar_captured` 标记时，插件加载后自动捕获 bot 当前头像：最多 10 次，每次间隔 30 秒。成功后写入标记，之后不再自动捕获；手动 `/msa bot` 不会写这个标记。10 次都失败后停止，重启插件会再次尝试，也可以手动 `/msa bot`。插件未启用时跳过自动捕获，之后启用需要重启插件才会再次尝试。

## 头像库

### 目录

- 默认目录：插件数据目录下的 `image/`。
- 额外目录：`core.extra_dir`，必须是绝对路径；首尾双引号会被去掉，相对路径记 warning 并忽略。
- 两个目录都只读取第一层，不递归子目录；同一路径只计一次。
- 额外目录中的图片参与随机抽取和 `/msa list`，也可以通过 `/msa remove` 按序号删除。
- 手动放入的图片不需要先登记就能被随机抽中；序号会在下次对账（启动、`/msa list`、添加或删除）时补登记。

### 支持格式与处理

- 识别后缀：`.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.bmp`，大小写不敏感。
- 动图（GIF、WebP 等）取第一帧。
- 换头像时按 `core.square_mode` 处理成 1:1：`居中切割`（默认）裁掉多余部分保留中间，`拉伸` 变形铺满正方形。
- 处理后最长边超过 1024 px 会等比缩到 1024，输出 PNG；会按 EXIF 方向摆正。
- 随机抽取不排除当前头像，可能连续抽到同一张。
- 添加时会在解码前检查像素数（上限 6400 万），无法解码的图片会被拒绝；通过链接下载的图片最多 32MB。

### 添加、命名与去重

- `/msa add` 不带名称：按图片内容 MD5 命名，后缀按识别到的格式补全（未识别的格式回退 `.png`）；如果该 MD5 文件已存在，提示已在库中，不再写入。
- `/msa add 名称`：按清洗后的名称主体 + 图片实际格式后缀保存（未识别的格式回退 `.png`）；目标路径已存在时覆盖。名称只写主体，不要带后缀，否则会得到双重后缀。
- 名称中的路径分隔符、冒号、星号、问号、引号、尖括号、竖线及控制字符会替换为 `_`；首尾空白和点会去掉，最长 64 字符；Windows 保留名（`CON`、`PRN`、`AUX`、`NUL`、`COM1`–`COM9`、`LPT1`–`LPT9`）会加 `_` 前缀。
- 哈希去重只比较 MD5 文件名，不扫描库内其他文件的内容；同一张图如果此前用自定义名称保存，仍可能再生成一份哈希命名文件。

### 序号与列表

- `avatar_index.json` 记录每个文件的 `seq`、`path`、`added`。
- 序号按登记时间紧凑排列为 1..N；新发现但未登记的文件按文件修改时间补登记。
- 添加、删除、启动对账、列出前对账都会触发重排：删除序号 3，原序号 4 变为 3。
- `/msa list` 每组 20 张，按当前序号升序；省略组号 = 第 1 组，`/msa list 2` 从序号 21 继续。
- 列表用合并转发返回，每张附序号和文件名；合并转发不可用时回退为纯文本名单。
- 每张缩略图目标 ≤500KB，最长边超过 720 时先缩到 720；缩到最小边长仍超限时使用最后一次压缩结果。缓存按图片内容 MD5 命名，有效期 5 小时，插件启动时清空整个 `thumb_cache/`。
- 删除或损坏 `avatar_index.json` 后会按目录中的文件重新登记，序号可能变化。

## 命令

命令名 `msa`；插件命令正则同时接受 `/msa` 和不带斜杠的 `msa`。除裸 `/msa` 外，所有子命令仅管理员可用；非管理员会收到「权限不足：/msa 仅管理员可用。」

### 命令列表

| 命令 | 参数 | 说明 |
|---|---|---|
| `/msa` | 无 | 只回复用法，不执行动作，不校验管理员 |
| `/msa add [名称]` | 可选名称 | 把本条消息或被引用消息中的第一张图片加入头像库 |
| `/msa bot` | 无 | 捕获 bot 当前头像入库 |
| `/msa bot <组号>` | 正整数 | 等同 `/msa list <组号>` |
| `/msa list [组号]` | 正整数，默认 1 | 合并转发浏览头像，每组 20 张 |
| `/msa remove <序号>` | 正整数 | 删除该序号图片并紧凑重排 |
| `/msa debug random` | 无 | 立即随机换一次头像 |

### 示例

```
/msa add
/msa add 头像1
/msa list 2
/msa remove 3
/msa bot
/msa bot 2
/msa debug random
```

### 各命令说明

**`/msa`**

只发送用法文本，不校验管理员，也不触发任何动作。

**`/msa add [名称]`**

- 优先取本条消息中的第一张图片；本条没有图片时，读取被引用消息中的第一张图片。
- 支持图片段携带的二进制数据或图片链接；链接下载走后面的安全校验。
- 不带名称按 MD5 命名并去重；带名称按名称保存，目标路径已存在时覆盖。
- 成功回报文件名和序号；没有图片、引用消息没有图片、图片无法解码或超出像素上限时返回失败原因。
- 写入位置固定为数据目录 `image/`，不会写到 `extra_dir`。

**`/msa bot`**

- 通过适配器 `get_login_info` 取 bot 自己的 QQ 号，再从 qlogo（`q1`/`q0` 两个域名，640 px）下载当前头像。
- 按内容 MD5 命名写入 `image/`；该哈希文件已存在时提示「已在库中」及序号。
- 不刷新更换提醒，不占工具配额。
- `/msa bot <组号>` 只接受正整数，否则回用法提示。

**`/msa list [组号]`**

- 省略组号查看第 1 组；`/msa list 2` 查看第 2 组（序号 21 起）。
- 组号必须是正整数；越界时提示总张数和组数。
- 头像库为空时提示先用 `/msa add` 或 `/msa bot` 添加。
- 合并转发失败时发送纯文本名单；个别缩略图生成失败时会单独提示序号。

**`/msa remove <序号>`**

- 删除该序号对应的图片文件，删除后其余序号自动前移。
- 删除前会先对账：文件已不存在的条目会被移除并紧凑重排，所以按原序号继续操作可能删除另一张图片，也可能提示没有该序号的头像；删除瞬间文件消失则不报错。
- 删除前解析真实路径，必须位于数据目录 `image/` 或 `extra_dir` 内；否则拒绝删除并记日志；路径无法解析时同样拒绝。
- 额外目录中的文件也会被删除。

**`/msa debug random`**

- 立即随机换一次头像，不写每日计划、不增加工具调用次数、不触发工具冷却。
- 成功更换会刷新 `notice_last_ts`，因此会重新开始更换提醒的保留计时。
- 未知 debug 子命令回用法提示。

### 命令权限

命令鉴权由插件自己完成。判定顺序：

1. 宿主传入 `is_local_operator=true` 的本地控制台触发直接放行。
2. 其他触发者把 `user_id` 归一为纯数字（去掉 `平台:` 前缀），与管理员名单比对。
3. 管理员名单 = 宿主 `plugin.permission` ∪ 配置 `core.admins`，按纯数字 ID 去重。

具体规则：

- 插件读取的宿主名单是 `plugin.permission`；`command_permissions` 不参与本插件的命令鉴权。
- 只有能归一为纯数字的 ID 会参与比对；非数字条目被丢弃。
- 插件不区分平台：`qq:10001` 和 `10001` 都归一为 `10001`，纯 ID 相同即视为同一人。
- `core.admins` 支持 `"qq:10001"` 和 `"10001"` 两种写法。
- 宿主名单读取失败时只使用 `core.admins`。
- 两处名单都为空时，非本地操作员一律拒绝（fail-closed）。
- 管理员可以删除头像库和 `extra_dir` 中的文件，只把可信账号加入名单。

## LLM 工具

工具名 `change_avatar`，组件全名 `github.cateye.mai.set_avatar.change_avatar`，无参数。用途是让 bot 在用户明确要求「换头像 / 更换头像 / 随机换个头像」时，从头像库随机抽一张换成自己的 QQ 头像；不能指定图片。

可用条件：

- `plugin.enabled=true`；
- `tool.allow_bot=true`；
- 当天成功调用次数 < `tool.daily_limit`；
- 距上次成功调用 ≥ `tool.cooldown_hours` 小时（`0` 表示不冷却）。

不满足时，插件通过 `component.disable_component(..., scope="global")` 把工具从模型侧隐藏；每 30 秒的调度检查、配置热更新后、工具调用后都会重新计算可见性。条件恢复后自动重新启用。

调用时还会再次检查条件：

- 不可用：`{"success": false, "content": "当前无法更换头像：<原因>"}`
- 成功：`{"success": true, "content": "已从头像库随机抽取并换上新头像「<文件名>」。"}`
- 头像库为空或适配器失败：`{"success": false, "content": "换头像失败：<原因>"}`

计数规则：

- 只有成功的工具调用才增加当天次数并刷新冷却时间。
- 次数按日期重置；冷却时间戳跨天保留，前一天晚间的调用可能让第二天仍处于冷却。
- 每日定时更换、`/msa debug random`、`/msa bot` 都不占工具次数，也不触发工具冷却。
- `tool.daily_limit=0` 时工具始终不可用。

## 每日定时与状态

调度器每 30 秒检查一次；只有 `plugin.enabled` 和 `daily.enabled` 同时为 true 时才执行每日更换。

### 计划与触发

- 每天以当天日期和 `daily.trigger_time` 为基准，加上在 `daily.drift_spec` 范围内均匀抽取的漂移分钟数，得到计划时刻。
- 漂移负数为提前、正数为延后；下限大于上限时自动交换。
- 到达计划时刻后掷一次 `daily.skip_probability`：
  - 命中：当天记为 `skipped_random`，不换头像，不再重试。
  - 未命中：随机抽一张更换；成功记为 `changed`；失败按 30 秒调度周期重试，当前进程内最多 3 次，仍失败记为 `failed`，当天不再尝试。
- 每天只产生一个结果；`changed`、`skipped_random`、`failed` 之后都不再重复。
- 当天还没有结果且当前时间已过计划时刻时，插件在计划时刻之后启动会在第一次调度检查中补做当天判定（同样可能换或跳过）。

### 状态文件

`state.json` 位于插件数据目录，读写以下字段：

| 字段 | 内容 |
|---|---|
| `daily` | `date`、`planned_ts`、`result`（未出结果时 `null`，否则 `changed` / `skipped_random` / `failed`） |
| `tool` | `date`、`count`、`last_ts` |
| `notice_last_ts` | 上次成功更换头像的时间戳 |
| `initial_avatar_captured` | 首次自动捕获是否已完成 |

- 状态跨重启保留；删除 `state.json` 相当于重置当天计划、工具用量、提醒和首次捕获标记。
- `state.json` 缺失或无法解析时按空状态处理，重新生成计划。
- 失败尝试计数只在内存中：如果进程在当天结果落定前重启，计数会清零，可能再获得最多 3 次尝试，跨多次重启的总尝试次数可能超过 3 次；结果落定后重启不会重试。
- 配置热更新只响应本插件（`scope=self`）：如果当天还没有结果，保存配置会重新抽取漂移、重排计划并清零尝试次数；已有结果则保持不动。

### 与工具的关系

- 每日定时更换不写 `tool` 计数，也不刷新工具冷却。
- `daily.enabled=false` 只关闭每日更换；`/msa` 命令和 `change_avatar` 工具不受影响。

## 更换提醒

每次成功更换头像后（每日定时、`change_avatar` 工具、`/msa debug random`），插件记录时间并写回 `state.json` 的 `notice_last_ts`；`/msa add` 和 `/msa bot` 不记录。

在保留期内，插件向模型上下文注入一条提醒：

```
你在2026-11-17 08:31:05更换了一次头像
```

时间为 bot 所在机器的本地时间。默认保留 24 小时，每次成功更换会刷新计时；超过保留时长或 `core.change_notice_hours=0` 后不再注入。

两个 Hook：

- `maisaka.planner.before_request`：`core.change_notice_to_planner=true`（默认）时，在第一条 system 条目之后插入一条结构完整的提醒条目；找不到 system 条目时插到 `items` 开头。
- `maisaka.replyer.before_request`：`core.change_notice_to_replyer=true` 时，把提醒追加到 `extra_prompt`，原有内容在前，换行分隔；默认 `false`，即不注入 replyer。

`core.change_notice_enabled=false` 时两个 Hook 都不注入。提醒时间戳持久化，重启后仍按剩余时长生效。

## 配置

配置由宿主 WebUI 管理，保存后热更新。字段、默认值和作用如下；不需要手工维护完整配置文件。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 插件总开关；关闭时停止每日调度、隐藏工具；插件加载时若关闭，跳过首次自动捕获 |
| `plugin.config_version` | `1.0.2` | 配置版本，与插件版本同步；WebUI 中隐藏且不可编辑 |
| `core.square_mode` | `居中切割` | 非 1:1 图片处理方式：`居中切割` / `拉伸` |
| `core.extra_dir` | 空 | 额外头像目录，绝对路径；首尾引号会去掉，相对路径忽略 |
| `core.admins` | `[]` | 插件管理员列表，与 `plugin.permission` 合并去重；支持 `"qq:10001"` 或 `"10001"` |
| `core.change_notice_enabled` | `true` | 是否注入更换提醒 |
| `core.change_notice_hours` | `24.0` | 提醒保留时长（小时）；`0` 表示不注入 |
| `core.change_notice_to_planner` | `true` | 是否注入 planner |
| `core.change_notice_to_replyer` | `false` | 是否注入 replyer |
| `daily.enabled` | `true` | 是否启用每日随机换头像 |
| `daily.trigger_time` | `08:30` | 触发时间，按 bot 所在机器的本地时间，24 小时制 `HH:MM`（也接受 `H:MM`）；非法时回退 `08:30` 并记 warning |
| `daily.drift_spec` | `"-45"-"30"` | 漂移范围（分钟），格式固定 `"a"-"b"`；负数提前，上下限写反会自动交换；格式错误回退默认 |
| `daily.skip_probability` | `0.5` | 跳过当天换头像的概率，范围 `0`–`1` |
| `tool.allow_bot` | `true` | 是否允许 bot 调用工具换头像 |
| `tool.daily_limit` | `2` | 工具每天调用上限；`0` 表示当天不允许调用 |
| `tool.cooldown_hours` | `5.0` | 工具调用冷却（小时）；`0` 表示不冷却 |

生效方式：

- 保存配置后，宿主触发热更新；插件重新排定当天计划（仅当天尚未出结果时）并刷新工具可见性。
- `plugin.enabled` 变为 false 时停止调度并隐藏工具；变为 true 时重新启动调度。
- `daily.enabled` 在每次调度检查时读取；`tool.*` 在每次可见性刷新时读取；`core.*` 在对应操作发生时读取。
- `core.extra_dir` 只影响后续的列表、随机抽取和删除，不会移动已有文件。

## 安全说明

- **命令权限**：除裸 `/msa` 外所有子命令仅管理员；非本地操作员且名单为空时拒绝；只有纯数字 ID 参与比对。管理员可以删除 `image/` 和 `extra_dir` 中的文件，只把可信账号加入名单。
- **下载 SSRF 防护**：只接受 `http`/`https`；目标解析地址不得落在环回、私网、CGNAT `100.64.0.0/10`、链路本地（含 `169.254.169.254`）、保留、组播、未指定网段；多 A/AAAA 记录中任一不安全即拒绝；`localhost` 等名称和解析失败的目标拒绝；重定向最多 3 跳，每跳重新校验；自定义解析器在连接时再次过滤内网地址，防 DNS rebinding；总超时 30 秒。
- **内容与体积**：响应 `Content-Type` 须为 `image/*`；为空或 `application/octet-stream` 时按 PNG/JPEG/GIF/BMP/WEBP 文件头嗅探；其他类型拒绝。下载最多 32MB；解码前像素数不得超过 6400 万。
- **错误信息**：非 200、重定向、内容类型、体积等校验失败时返回固定中文提示，不回显 HTTP 状态码。
- **文件名**：`/msa add` 的名称会替换路径分隔符、冒号、星号、问号、引号、尖括号、竖线及控制字符，剥离首尾空白和点，最长 64 字符，避开 Windows 保留名。
- **删除边界**：`/msa remove` 删除前解析真实路径，必须位于 `image/` 或 `extra_dir` 内；注册表被篡改指向目录外时拒绝删除并记日志。
- **读取边界**：随机抽取、每日更换和工具只从 `image/` 与 `extra_dir` 的第一层读取图片。`/msa list` 则按 `avatar_index.json` 中的路径读取；列表读取没有和删除相同的目录边界校验，若序号表被改写为指向库外图片，管理员执行 `/msa list` 时会读取并转发它。请限制插件数据目录的写权限。
- **写入范围**：`/msa add`、`/msa bot`、首次自动捕获只写插件数据目录（`image/` 与序号表）；`extra_dir` 只用于读取和按序号删除。换头像本身不写图片文件，只更新 `state.json`；`/msa list` 写 `thumb_cache/`。
- **额外目录风险**：`core.extra_dir` 中的图片会进入序号表，管理员可用 `/msa remove` 删除该目录中的文件；不要把含重要文件的目录配进来。

## 许可证与仓库

- 插件 ID：`github.cateye.mai.set_avatar`，类型 `extension`，版本 `1.0.2`。
- 仓库：<https://github.com/cateyemizuki/cateye_mai_set_avatar>
- 问题反馈：<https://github.com/cateyemizuki/cateye_mai_set_avatar/issues>
- 更新日志：[CHANGELOG.md](CHANGELOG.md)
- 许可证：MIT，Copyright (c) 2026 cateye，见 [LICENSE](LICENSE)。
- 作者：cateye（<https://github.com/cateyemizuki>）
