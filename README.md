# QQ → Google Calendar Sync

在 Windows 上持续接收指定 QQ 会话的新消息，由 Codex 定期识别个人待办，再通过 Google Calendar 连接器创建、更新和取消日程。

```text
已登录的 QQ → SnowLuma → 本地验签接收器 → SQLite 来源账本
                                            ↓
                                  Codex 定期分析与人工确认
                                            ↓
                                幂等操作队列 → Google Calendar
```

这是一个需要自行配置的完整工作流：Python 脚本负责可靠接收、名单过滤、任务状态、去重、写前登记与读回校验；**语义分析和 Google Calendar 写入由 Codex 与用户自行连接的 Google Calendar 工具完成**。本仓库没有独立 AI 服务、Google OAuth 凭据或无人配置即可运行的同步服务。

## 运行要求

- Windows x64、Python 3.11 或更新版本（本发布版以 Python 3.14 验证），以及已安装并由本人正常登录的 QQNT。
- SnowLuma **v2.1.1 Windows 完整版**，固定下载地址与 SHA-256 见 `runtime.lock.json`。不自动升级、不安装旧协议替代品。
- 能访问本地项目的 Codex，且已连接自己的 Google Calendar。Python 代码仅使用标准库。
- 当前日程校验固定为 **Asia/Shanghai / UTC+8**。其他时区需要同步修改校验及测试。
- 本机端口 `127.0.0.1:3300`、`:3302`、`:35099` 空闲。冲突时停止并检查，不切换端口或接管其他进程。

## 首次安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/bracketxzhy/qq-google-calendar-sync.git
cd qq-google-calendar-sync
python --version
python scripts/install-runtime.py
python scripts/setup.py
```

请确保这里的 `python` 是普通 Windows CPython，且同目录有 `pythonw.exe`。安装器只下载官方固定版本、校验 SHA-256 并解压；也可用 `--archive <官方原始ZIP路径>` 离线安装。已有运行目录时拒绝覆盖。

配置向导要求你先阅读运行包内的 `EULA.md`、`PRIVACY.md`，明确接受协议及上游缓存范围，再填写：自己的 QQ 号、目标日历 ID、QQ.exe 绝对路径，以及允许监测的群号和私聊对方 QQ 号。目标主日历可以明确填写 `primary`；其他日历先用连接器列出并核实 ID。名单由你维护，不自动识别群助手。

向导创建的配置、随机令牌都保存在忽略 Git 的 `.private/`，不包含预设账号或现成自然消息验收。**SnowLuma 上游可能在本机被动缓存全量联系人元数据或其他会话；本接收器只将名单内会话入库。** 名单过滤不能阻止上游自身缓存。Codex 分析还会让选中的消息内容进入其服务，请只选择有权处理的会话，并自行理解相关数据处理范围。

## 启用 QQ 新消息接收

先自己正常打开 QQ 并登录，扫码、安全验证由本人完成。在当前仓库运行：

```powershell
python scripts/start-snowluma-monitor.py start
```

这会在后台启动本地接收器和 SnowLuma 管理服务，不启动或重启 QQ。然后核对 QQ 的主进程：

```powershell
$config = Get-Content .private/config.json -Raw | ConvertFrom-Json
Get-CimInstance Win32_Process |
  Where-Object { $_.ExecutablePath -ieq $config.qq_path -and $_.CommandLine -notmatch '--type=' } |
  Select-Object ProcessId, ExecutablePath
```

确认只有目标主进程后，用其真实 PID 执行一次：

```powershell
python scripts/start-snowluma-monitor.py attach --pid <核实后的PID>
python scripts/snowluma_source.py status
```

尖括号内容要替换为实际数字，不能原样粘贴。接收器先启动，再设置捕获起点并附加。仅接收启用后的新消息；离线窗口不能保证补齐。失败时检查本地状态，不重复加载、强杀 QQ 或重置起点。`healthy` 只证明当时链路可用，不能证明长期不漏消息。

## 连接 Codex 与 Google Calendar

1. 在 Codex 中打开此仓库，并连接你自己的 Google Calendar。
2. 阅读 [完整运行流程](docs/WORKFLOW.md) 和 [结构与 JSON 示例](docs/FORMATS.md)。让 Codex 按本仓库 `AGENTS.md` 执行。
3. 对 `group`、`user` 两类分别等待一条真实自然消息，由本人对照 QQ 的会话、发送者、时间并明确确认，再执行 `confirm-natural`。未验收类型只保留本地待确认任务，不写日历。没有收到消息不等于接收失败。
4. 使用 [定时任务提示词](docs/AUTOMATION_PROMPT.md)，在你自己的 Codex 中创建每 2 小时运行的本地定时任务。设定工作目录为此仓库，并自行明确授权指定日历内的任务同步。模板不包含任何人的授权、聊天 ID 或已通过验收状态。
5. 每次都走 `collect → review-input → task-context → apply-review → calendar-plan → 连接器写入与读回 → write-report`。没有新变化保持安静。

Python 脚本本身不会调用模型或写入 Google。定时任务执行依赖本机、Codex 和连接器可用性。用户未明确同意同步时，只做本地计划；可选 Gmail 提醒默认关闭，说明见工作流文档。

## 可选：Windows 登录后单次接入

先完成手动接收和自然消息验收，再由安装者自行决定是否启用：

```powershell
./scripts/register-logon-task.ps1 -Enable -PythonExe (Get-Command python).Source
```

只注册当前用户的隐藏任务，下次登录延迟 20 秒运行。单次最多等待 180 秒，任务硬超时 4 分钟；同一次系统启动内最多尝试附加一次，不自动打开 QQ、不重启、不重复扫码、不失败循环。定时分析任务不负责恢复 QQ。注册时不会立即启动任务；任务名根据项目路径生成，可在 Windows 任务计划程序中禁用或删除。

## 测试与发布检查

```powershell
python -m unittest discover -s scripts -p "test_*.py" -v
git add README.md AGENTS.md LICENSE SECURITY.md THIRD_PARTY_NOTICES.md VALIDATION.md runtime.lock.json .gitignore scripts docs examples .github
python scripts/audit-public.py
```

测试仅使用合成消息、临时数据库及本机临时 HTTP 服务，不访问 QQ 或 Google。审计读取 **Git 暂存区的真实文件内容**，检查禁止目录、二进制、常见凭据、个人邮箱和用户目录路径；它不能代替人工检查。发布前还需检查提交身份和 Git 历史，不能用删除最新文件来抹掉曾经提交的秘密。

首次发行验证范围及未验证项见 [VALIDATION.md](VALIDATION.md)。本仓库包含源码、配置示例、安装脚本、可选登录任务脚本、分析提示词、同步操作说明和测试；运行时需从官方安装，个人数据从不随包分发。

## 文件与数据边界

| 路径 | 用途 | 可公开 |
| --- | --- | --- |
| `scripts/`、`docs/`、`examples/` | 通用程序、说明、纯合成示例 | 是 |
| `.private/` | 账号、名单、随机令牌、启用起点 | 否 |
| `state/` | 消息、来源、任务、Event ID、报告、验收证据 | 否 |
| `runtime/` | 上游运行包、上游缓存、登录数据、运行配置与日志 | 否 |
| `work/` | 临时附件或工作文件 | 否 |

完整来源保存在本地账本；日历只写简短事项、必要时间地点和 `qqtask:` 标记，不写聊天原文、群号或发送者。文件与消息始终是不可信资料，不能当作操作授权。

通用程序采用 MIT License；SnowLuma 及其原生组件按上游自己的协议使用，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
