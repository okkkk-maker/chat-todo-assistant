# 待办总结助手 · 部署与迁移说明

> 版本 **v1.5.4**　适用：Windows 10 / 11 64 位
> 采集 QQ + 个人微信（4.x）本地消息 → 生成 `work\待办素材.json` → 由 WorkBuddy 提炼成带溯源的待办清单并推送。
> **采集与提炼都是每 3 小时一次。**
>
> **⚠️ 引导提示**：程序首次启动处于待命状态，**使用前必须先在控制台【⚙️ 系统设置】中确认配置模式并勾选启用平台**；此外，**钉钉与飞书通道目前尚未经过实机测试**，如需使用请务必先在【系统设置】中确认勾选、测试连通性并保存配置后再启用。

---

## 1. 它能做什么

| 环节 | 由谁完成 | 频率 | 产物 |
|---|---|---|---|
| 采集 QQ / 微信本地消息（只读解密） | 本包内的常驻调度器 `tools\scheduler.py` | **每 180 分钟** | `work\待办素材.json` |
| 提炼待办 + 溯源 + 推送企业微信 | WorkBuddy 定时任务（提示词 `automation-prompt.md`） | **每 3 小时** | `output\todo-*.md` + 企业微信消息 |
| 可视化配置 / 体检 / 立即采集 | 控制台 `Start.bat` → http://127.0.0.1:8000 | 手动 | — |

设计原则（已写进代码，不是口号）：

- **只读**：只复制本地数据库副本再解密，不写原库、不影响正在聊天的微信/QQ；
- **不臆造**：清单每条都带 `会话 / 发送人 / 时间 / 消息ID`，取不到来源的不进清单；
- **不静默**：任何通道失败都会落到 `output\异常说明-*.txt`，并冻结游标，修好后从原位置补录，不跳过。

---

## 2. 运行环境与依赖

### 2.1 系统

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11，**64 位** |
| 磁盘 | 约 250 MB（含离线 wheels 与运行环境）；微信镜像另占 50~300 MB |
| 权限 | 首次安装与取密钥需要**管理员**（读进程内存、写启动项）；日常采集用普通权限即可 |
| 网络 | **首次安装不需要联网**（依赖已在 `vendor\wheels`）；只有 WorkBuddy 提炼推送需要网络 |

### 2.2 Python

- 支持 **3.10 / 3.11 / 3.12 / 3.13**（推荐 3.13 x64）
- 不需要预装任何第三方库：`Install.bat` / `Admin_Init.bat` 会用 `vendor\wheels` **离线**建 `.venv`
- 机器上完全没有 Python 时，`Install.bat` 会尝试下载便携版 3.11（需要网络）；内网机器请提前装好 Python

### 2.3 依赖清单（已随包，无需下载）

见 `requirements.txt`：

```
sqlcipher3-wheels==0.5.7   # 解 QQ 的 SQLCipher 库
pycryptodome==4.2.0        # AES（缺失时自动降级到 Windows CNG，功能不受影响）
zstandard==0.25.0          # 解微信 zstd 压缩正文（可选，缺了会少读到正文）
frida==16.7.19             # 仅"启动瞬间抓取"兜底方案用；常规取密钥不需要
```

> `frida` 固定 16.7.19：17.x 在 Win10 上会报 `Unable to locate the ntdll loader TLS routines`。

### 2.4 数据源（按需，缺哪个就少哪个通道）

| 通道 | 前提 | 是否需要额外取密钥 |
|---|---|---|
| QQ（NT 架构） | 本机登录电脑 QQ，且消息库存在 | **需要**（`vendor\1-get-qq-key.bat` 或控制台「QQ 密钥」） |
| 个人微信 4.x | 本机登录微信（**必须你自己在桌面双击启动**） | **需要**（控制台「🔑 自动抓取」2~3 秒，无需重启微信） |
| 企业微信 | WorkBuddy 连接器，或群机器人 Webhook | 不需要（收件箱 / Webhook） |
| 飞书 | 自建应用 App ID / Secret，且应用已入群 | 不需要 |
| 钉钉 | 企业内部应用凭证 | 不需要 |

> 只用 QQ + 微信也能跑；其余通道在 `config\active_platforms.json` 里不勾选即可。

---

## 3. 目录结构

```
待办总结助手-交付包-v1.5.0\
├─ Install.bat               ① 建 .venv + 离线装依赖 + 注册开机自启
├─ Admin_Init.bat            ② 初始化向导（生成 .env、探测 QQ/微信路径、配平台）
├─ Start.bat                 ③ 双击=打开控制台；带参数=跑一次采集
├─ Web_Console.bat           与 Start.bat 等价（保留旧入口名）
├─ WeChat_Key.bat            微信取密钥菜单（1 自动抓取 / 2 启动抓取 / 3 粘贴 / 4 体检）
├─ run.py                    采集主流程（四步）
├─ server.py                 控制台服务（127.0.0.1:8000，含采集调度线程）
├─ channel_collector.py      多通道聚合与状态判定（OK/STALE/PARTIAL/FAIL/NOT_READY）
├─ index.html                控制台前端（单文件）
├─ .env.example              配置模板（首次初始化会复制成 .env）
├─ requirements.txt          依赖清单（仅说明用，实际走 vendor\wheels）
├─ automation-prompt.md       WorkBuddy 定时任务提示词模板（可迁移，无写死路径）
├─ 说明-使用者.txt / 说明-实施员.txt
├─ BUILD-INFO.txt            版本与迭代记录
├─ config\
│   ├─ schedule.json         采集计划（**每 180 分钟**）
│   ├─ active_platforms.json 启用哪些平台 + 可选通道
│   └─ schema.json           控制台表单结构
├─ tools\                    全部实现（见 §7 关键模块）
├─ docs\                     接入指南 / 排查手册 / 功能清单
└─ vendor\
    ├─ wheels\               离线依赖（sqlcipher3 / pycryptodome / zstandard / frida）
    ├─ qq-key\                QQ 取密钥脚本
    └─ wechat-key\            微信取密钥历史脚本（4.0.x 兜底，4.1+ 已不需要）
```

运行时才会生成（**不在交付包里**，迁移时也不需要带）：

```
.venv\            运行环境          work\     素材、微信明文镜像、密钥环 wx_keys.json
output\           清单与日志        data\     游标等状态
```

---

## 4. 首次部署（新机器，约 5 分钟）

> 全程只有"登录 QQ/微信"和"点一次取密钥"需要人工。

1. **解压**到任意目录，例如 `D:\待办助手\`（路径可以有中文，但**不要放在 C:\Program Files 下**，否则写文件会被 UAC 拦）。
2. 右键 **`Install.bat` → 以管理员身份运行**。它会：
   - 找一个 3.10~3.13 的 Python；
   - 用 `vendor\wheels` 离线建 `.venv` 并装依赖；
   - 注册两个开机自启项（`ChatTodoWatcher.bat` 单次采集 + `ChatTodoScheduler.bat` 常驻调度）。
3. 右键 **`Admin_Init.bat` → 以管理员身份运行**，按向导完成：
   - 生成 `.env`（从 `.env.example` 复制）；
   - 自动探测 QQ 数据目录、微信 `xwechat_files`；
   - 选择启用平台（QQ / 微信 / 企微 / 飞书 / 钉钉）。
   向导里 QQ 会被重启一次以抓密钥，属正常现象。
4. **微信取密钥**：先确认微信已登录（窗口标题是你的昵称），双击 `Start.bat` 打开控制台 →
   参数设置 → 微信 → 点 **【🔑 自动抓取微信密钥】**。2~3 秒完成，**不用关微信、不用重启**。
   等价命令：`.venv\Scripts\python.exe tools\wxkey.py acquire`
5. **验收**：控制台点【⚡ 立即采集】，或命令行执行
   ```bat
   .venv\Scripts\python.exe run.py --hours 4 --no-open
   ```
   看到 `完成` 且 `work\待办素材.json` 里 `meta.status` 为 `OK` 即成功（退出码 0）。

---

## 5. 配置项

### 5.1 `.env`（密钥与路径，**不要外发**）

| 键 | 说明 |
|---|---|
| `QQ_TARGET_UIN` | 要采集的 QQ 号（uin） |
| `QQ_DB_PATH` | QQ 消息库目录；留空则自动探测 |
| `QQ_DBKEY` | QQ 数据库密钥（初始化时自动抓取写入） |
| `QQ_CIPHER_PROFILE` | QQ 加密参数档位（如 `ntqq_official`），由初始化自动判定 |
| `WX_DBKEY` | 微信密钥（**4.1+ 只是 `message_0.db` 那一把**，其余在密钥环里） |
| `WX_DATA_PATH` | 微信 `db_storage` 或 `xwechat_files` 根；**填了就优先用**，留空自动探测 |
| `WX_ALLOW_SCAN` | `1`=允许内存扫描取密钥；`0`=只用已有密钥 |
| `WX_SCAN_SECONDS` | 单次扫描上限（默认 25 秒） |
| `WX_MIRROR_DIR` | 明文镜像目录；默认 `work\wx_mirror`，可迁到空间大的盘 |
| `WX_KEYRING_PATH` | 密钥环路径；默认 `work\wx_keys.json`（一般不用改） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_MAX_CHATS` | 飞书通道 |
| `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET` | 钉钉通道 |
| `WECOM_WEBHOOK_URL` | 企业微信群机器人（推送出口） |
| `SUMMARY_HOURS` | 默认回看小时数（4） |

> `.env` 含密钥，**不要提交到代码库、不要发群里**。换机器必须重新取密钥（密钥与账号+机器绑定）。

### 5.2 `config\schedule.json`（采集计划 = 每 3 小时）

```json
{
  "enabled": true,
  "mode": "interval",
  "interval_minutes": 180,
  "daily_times": ["08:30", "12:30", "17:30"],
  "hours": 4,
  "max_runtime_seconds": 900,
  "skip_if_last_unfinished": true,
  "quiet_window": []
}
```

| 字段 | 含义 |
|---|---|
| `enabled` | 是否启用常驻调度（`false` 时只手动采集） |
| `mode` | `interval`（固定间隔）或 `daily`（每天固定时刻，用 `daily_times`） |
| `interval_minutes` | **180 = 每 3 小时**；改成 60 就是每小时 |
| `hours` | 每次回看多少小时；建议 **间隔 + 1 小时**，这里 4 |
| `max_runtime_seconds` | 单轮最长运行时间，超时判失败并释放锁 |
| `skip_if_last_unfinished` | 上一轮没跑完就跳过本轮，避免两个采集撞车 |
| `quiet_window` | 静默时段（如 `["23:00-06:00"]`），期间不采集 |

改完**不需要重启**：调度线程按文件修改时间热加载。命令行也能改：

```bat
.venv\Scripts\python.exe tools\scheduler.py status
.venv\Scripts\python.exe tools\scheduler.py --interval 180 config
.venv\Scripts\python.exe tools\scheduler.py run-now
.venv\Scripts\python.exe tools\scheduler.py enable|disable
```

### 5.3 `config\active_platforms.json`

```json
{
  "config_mode": "base",
  "enabled_platforms": ["QQ", "WeCom", "WeChat"],
  "optional_platforms": ["WeCom"]
}
```

- `enabled_platforms`：勾了就必须有数据，失败会判 `PARTIAL/FAIL` 并冻结游标；
- `optional_platforms`：**数据源还没接上的通道放这里**。未接入时只标注
  `meta.optional_missing_channels`、在清单末尾写"本次未覆盖"，**不影响整轮出清单**。
  企微没授权连接器时建议放这里。

---

## 6. 定时任务（每 3 小时）

### 6.1 程序侧（采集）——已内置，无需额外配置

- `config\schedule.json` → `interval_minutes: 180`、`hours: 4`；
- 常驻进程：登录后由启动项 `ChatTodoScheduler.bat` 拉起 `tools\scheduler.py daemon`；
- 也可由控制台拉起（`server.py` 启动时会读 `enabled` 并起调度线程）；
- 日志：`output\scheduler.log`；状态：`tools\scheduler.py status`。

### 6.2 WorkBuddy 侧（提炼 + 推送）

在 WorkBuddy「自动化」里新建一个**每 3 小时**的任务：

- 调度：`FREQ=HOURLY;INTERVAL=3`（不要写 `BYHOUR/BYMINUTE`，否则会变成每天固定时刻）
- 工作目录：本包所在目录
- 提示词：用 `automation-prompt.md`（可迁移模板），或初始化后生成的
  `automation-prompt.local.md`（已把路径换成本机绝对路径）
- 若你已有"每天 08:30"的旧任务，**请删掉或改成上面的规则**，避免一天只跑一次。

> 两层分工：程序每 3 小时**采**，WorkBuddy 每 3 小时**提炼**。两个频率一致，
> 素材新鲜度阈值设为 200 分钟，正常情况下 WorkBuddy 直接读素材、不重复爬数据。

---

## 7. 启动方式

| 场景 | 操作 |
|---|---|
| 打开控制台（配置/体检/立即采集） | 双击 `Start.bat` → 浏览器打开 http://127.0.0.1:8000 |
| 跑一次采集（不弹界面） | `Start.bat --no-open --hours 4` 或 `.venv\Scripts\python.exe run.py --hours 4 --no-open` |
| 只跑采集服务、不起调度 | `.venv\Scripts\python.exe server.py --no-open --no-scheduler` |
| 只跑调度（前台观察） | `.venv\Scripts\python.exe tools\scheduler.py daemon` |
| 开机自启 | `Install.bat` 已注册；`tools\setup_autostart.py --remove` 可移除，`--status` 查看 |
| 微信体检 | `.venv\Scripts\python.exe tools\wxkey.py status` |

> **同一个 8000 端口只允许一个控制台。** 重复双击 `Start.bat` 时，第二个会提示
> 「控制台已经在运行」并退出——这是防止两个进程争抢端口导致"界面时好时坏"的保护。
> 自查：`netstat -ano | findstr ":8000"` 应该只有一行 `LISTENING`。

---

## 8. 迁移到另一台机器

### 8.1 需要带走的

✅ 整个 `待办总结助手-交付包-v1.5.0` 目录（含 `vendor\wheels`，这是离线安装的关键）

### 8.2 不需要带、也不该带的

❌ `.venv\`（换机重建）　❌ `.env`（含本机密钥）　❌ `work\`（含明文镜像与密钥环）
❌ `output\`（历史清单）　❌ `automation-prompt.local.md`（含本机绝对路径）

> 本交付包已经默认排除了以上内容。

### 8.3 目标机器上的步骤

1. 解压到 `D:\待办助手\`（不要在 `C:\Program Files` 下）；
2. 管理员运行 `Install.bat`（离线建环境）；
3. 管理员运行 `Admin_Init.bat`（生成新 `.env`、探测本机 QQ/微信路径）；
4. 登录 QQ 与微信 → 控制台点【🔑 自动抓取微信密钥】（QQ 密钥在向导里已抓）；
5. 新建 WorkBuddy 每 3 小时自动化（提示词用 `automation-prompt.md`）；
6. 跑一次 `Start.bat --no-open --hours 4` 验收。

### 8.4 验收清单

- [ ] `tools\scheduler.py status` 显示「启用: 是，间隔: 每 180 分钟」
- [ ] `tools\wxkey.py status` 显示密钥环覆盖 N 个库（微信 4.1+ 通常 20 个）
- [ ] `run.py --hours 4 --no-open` 退出码 0，`work\待办素材.json` 的 `meta.status=OK`
- [ ] `output\` 下没有新的「异常说明-多平台采集失败.txt」
- [ ] `netstat -ano | findstr ":8000"` 最多一行 `LISTENING`
- [ ] WorkBuddy 自动化下次运行时间 ≈ 3 小时后

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 控制台取密钥报「没找到」，命令行却能取到 | 8000 端口有两个实例，请求落到了旧进程。`netstat -ano \| findstr ":8000"` 查，全部结束后只启一个 |
| 提示「控制台已经在运行」 | 正常保护，不是错误；直接用已打开的那个页面 |
| 微信报「密钥与当前数据库不匹配」 | 换过微信号 / 微信重装升级 → 重新点【🔑 自动抓取】 |
| 微信 note 出现「没有可用的 zstd 解压库」 | 缺可选依赖；跑一轮 `run.py` 会自动从 `vendor\wheels` 补装，或执行 `tools\bootstrap_deps.py` |
| 微信 note「密钥环覆盖 N 个库」少于总数 | 有分片没取到钥匙；再抓一次 `tools\wxkey.py acquire` 会自动补齐 |
| 清单里企微一直显示「本次未覆盖」 | 没接连接器。属预期；接好后从 `optional_platforms` 移除即可 |
| `meta.status=PARTIAL` 且不出清单 | 有**必选**通道没采到。看 `broken_channels` / `not_ready_channels`；确属暂不接入的，加进 `optional_platforms` |
| QQ 报 `NO_KEY` | 没跑过初始化，或 QQ 升级后密钥失效；重跑 `Admin_Init.bat` |
| 换机器后一台能跑一台不能 | 密钥与账号+机器绑定，必须在新机器重新取一次 |
| 提示「另一个待办汇总实例正在运行中」 | 上一轮还没结束，属保护机制，等下一轮 |

更多细节：

- `docs\专题指引\全平台接入指南.md` — 各平台接入步骤
- `docs\专题指引\微信密钥抓取-超时与卡死排查.md` — 微信取密钥的全部坑与判据
- `docs\专题指引\功能清单与本次补全说明.md` — 架构、缺漏审计、每轮修复的根因

---

## 10. 数据与安全

- 所有解密都在本地完成，明文镜像写在 `work\wx_mirror\`，**不会上传**；
- 原始加密库只读，程序不执行任何 `INSERT/UPDATE`；
- `.env`、`work\wx_keys.json`、`work\wx_mirror\` 都属敏感数据，迁移/备份时请单独处理；
- 仅采集**本人有权访问**的本机聊天数据；对外推送只发给你指定的接收人。
