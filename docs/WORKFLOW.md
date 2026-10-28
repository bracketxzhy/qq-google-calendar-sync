# 从捕获到日历写入

以下命令均在仓库根目录，使用安装时同一个 `python`。本文件描述供 Codex 执行的操作协议，Google Calendar 工具由安装者自己的连接器提供。

## 1. 本次来源状态

```powershell
python scripts/snowluma_source.py status
python scripts/pipeline.py collect
python scripts/pipeline.py review-input
python scripts/pipeline.py task-context
```

检查退出码及 `state/pipeline/last-attempt.json`，读取其 `id` 对应的 `state/pipeline/runs/<id>/result.json`。`failed` 是失败，不能引用旧成功。`collected_push_unverified` 表示至少一种消息类型未完成自然对照，不是任务可以直接写云端。

来源为 SQLite 推送队列，导入游标与来源写入在同一事务提交。重复采集不重复生成来源，重复推送去重，实质内容变化保存新版本。账号不符或名单之外的推送不会在本接收器存储原文；选中来源格式缺失则 quarantine，保留待查。

## 2. 每种会话类型独立验收

等待真实群消息/私聊消息，向安装者核对会话、发送者、时间及必要简短要点，不要求发送 QQ 测试消息。提问与未答状态保存到私有 `state/pipeline/review-questions.json`。获得真实回答后创建私有 JSON，结构见 `examples/natural-confirmation.example.json`，再执行：

```powershell
python scripts/snowluma_source.py confirm-natural --file state/natural-confirmation.json
```

示例中的占位符与 `user_confirmed:false` 不能直接用于验收。命令会核对本地实际捕获的 event_key；群与私聊分别核验，证据保存于 `state/snowluma/acceptance.json`。没有新消息保持等待，不虚构通过。

## 3. 分析与任务生命周期

分析 `state/pipeline/review-input.json` 中的新/变化来源，结合 task-context 既有任务生成本地 review。`reviewed` 的 key 与 fingerprint 必须来自本轮真实来源；每个任务保留来源 keys。第一次创建的任务 ID 使用语义稳定 key 的 SHA-256 前 24 位，此后延期/修改沿用该 ID，不按新日期生成另一个任务。

明确个人义务且日期确定才 active；需要用户判断的事项保持 pending，并在 Codex 对话提问。completed/dismissed/calendar_sync_paused 需要真实用户确认，不替用户回答。明确取消需 cancellation_explicit，来源消失不等于取消。日期只有一天使用全天及次日 exclusive end_date；不猜具体时长。

```powershell
python scripts/pipeline.py apply-review --file state/review.json
python scripts/pipeline.py calendar-plan
```

review 结构见 `examples/review.example.json`。示例全是虚构占位内容，没有真实来源，不能直接导入。代码校验状态、名单、日期、指纹以及用户暂停/完成保护。

## 4. 操作队列与 Google Calendar

calendar-plan 返回 operation_id、kind、calendar_id、event、event_id、marker、previous_payload。连接器调用必须按其实际工具 schema 传参；不能把整个 operation 对象原封传给 create_event。

创建：

1. 在目标日历搜索 `qqtask:<task_id>`，再核对同名同日事件，避免重复。存在同名事件但归属不明则询问，不认领他人事件。
2. 若发现此前不确定写入留下的同标记事件，先依据真实读回核对现有 inflight 操作；不再创建一次。
3. `python scripts/pipeline.py operation-start --operation <operation_id>`。
4. 调用 Google Calendar create_event，传 event 的 title、description、location、时间/日期字段、timezone_str、calendar_id、visibility、transparency、reminders，另显式 `attendees:[]`；不添加 Meet、不发邀请。
5. 使用返回的真实 event ID 再 read_event，将其 **structuredContent 中的实际事件对象** 保存到 `state/calendar-readback.json`，然后 operation-finish。

更新/取消：

1. read_event 读取已有 ID，保存真实对象至 `state/calendar-before.json`。
2. `python scripts/pipeline.py verify-owned --operation <operation_id> --file state/calendar-before.json`。代码核验标记、前次快照、参与者及用户改动；失败立即停止，不覆盖云端。
3. `operation-start`，再调用 update_event 或 delete_event（目标日历及已有 event_id）。
4. read_event 再读回，保存实际对象并 operation-finish。删除后的 404 或工具未返回可读 cancelled 对象时，保持 inflight 并记录需核对，**不能合成取消对象骗过校验**，也不能盲重发 delete。

```powershell
python scripts/pipeline.py operation-finish --operation <operation_id> --file state/calendar-readback.json
```

当前校验器要求连接器读回形状：`id`、`status`、`summary`、`description`、`start`、`end`、`visibility`、`transparency`、`reminders`、`location`、`attendees`。其中 start/end 是连接器的扁平时间字符串，全天为 `YYYY-MM-DDT00:00:00`，定时为带 +08:00 的 ISO 字符串。它不是 Google REST API 的嵌套 start/end 对象；如工具输出形状不一致，暂停并明确适配/测试，不猜测字段或静默兼容。read_event 原样内容同时保留在私有本地证据。

已有 inflight/uncertain 时禁止继续规划；在目标日历搜索标记、读回并核对。能证实成功则用对应 inflight 操作完成登记；无法证实保持待处理，并向安装者说明。当前没有自动强制重试、自动重置状态或冲突合并命令。

## 5. 每次及每日报告

根据本次真实结果创建 `state/pipeline/runs/<run_id>/report-input.json`，至少含 collection_run；即使无变更也填空数组。结构见 `examples/report.example.json`。

```powershell
python scripts/pipeline.py summary
python scripts/pipeline.py write-report --file state/pipeline/runs/<run_id>/report-input.json
```

`run_id` 必须是本轮 last-attempt 的实际 ID。之后读回 `state/pipeline/reports/<run_id>.json` 及 `state/pipeline/daily/<上海日期>.json`。记录新增/修改/取消、待确认、日历结果、失败和不确定窗口。不能把未执行步骤写成成功；不能为补报告重复采集或写云端；既有报告不可覆盖。

## 可选 Gmail 提醒（默认关闭）

这是供 Codex 使用 Gmail 连接器执行的可选规则，没有 Python 邮件发送器。必须由安装者另行明确授权，并连接自己的 Gmail；未授权时跳过即可，不影响日历同步。

仅当当前在线、来源类型已独立验收、消息新增或实质变化、明确需要本人回复且没有本人已回复证据时考虑提醒。普通通知、日程、可选报名和不确定事项不发；不代替本人在 QQ 回复。

私有 `state/pipeline/email-notifications.json` 按 `source key + fingerprint` 去重。先保存 pending 状态与稳定邮件标记，再用 Gmail 搜索已发送邮件。已发送则记录真实邮件 ID；结果不明先查，不能重发。符合的新事项合并为一封，发送给已认证本人 `to="me"`，只写必要简短摘要，不附聊天原文、消息链接、文件或凭据。发送后读回核实，并将真实 ID 保存为 sent。无法确认时保留待处理，报告原因。

## 停用与保留

取消 Codex 定时任务可以停止分析及云端同步，但持续接收器仍可能运行。先在任务计划程序禁用本项目的登录任务；再仅识别并停止本项目 receiver/runtime PID，不能按进程名批量结束 Python、Node 或 QQ。QQ 本身由本人正常退出。保留 `.private` 和 `state` 可保留起点及去重；若要彻底删除个人数据，先停止本项目组件，核实项目绝对路径后由本人决定清理范围，不重置正在运行的项目。
