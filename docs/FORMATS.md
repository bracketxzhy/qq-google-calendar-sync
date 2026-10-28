# 配置和消息格式

`examples/` 全是虚构模板。真实配置只写到 `.private`，真实 review、来源核验、Google 读回和报告只写到 `state`。不能把真实数据改个文件名存进 examples。

| 文件 | 来源和用途 |
| --- | --- |
| `.private/config.json` | 向导生成，qq_account、calendar_id、qq_path、两个范围确认字段 |
| `.private/account-selection.json` | group_allowlist=`group:<群号>`、private_allowlist=`user:<对方QQ号>` |
| `.private/snowluma-secrets.json` | 安装时随机生成，不提供预设值、不打印 |
| `.private/message-source.json` | 固定 source、真实启用时间、历史回补禁用 |
| `.private/snowluma-startup.json` | 登录单次接入默认 disabled，明确 opt in 后启用 |
| `state/pipeline/review-input.json` | 实际新/变化来源；key 和 fingerprint 用于防止过期分析 |
| `state/review.json` | Codex 分析，reviewed、tasks、必要时真实 user_confirmation |
| `state/snowluma/acceptance.json` | group/user 独立真实自然消息验收 |
| `state/pipeline/pipeline.sqlite3` | 来源、任务版本、Google Event ID、操作状态和去重 |

每个任务的 id 为 24 位小写十六进制，表示稳定语义事项；status 可取 pending/active/cancelled/dismissed/completed。sources 是实际已处理来源 key 列表，不能丢失。event 支持 title、description、location、start_date/end_date 或 start_time/end_time、timezone_str；两种时间表示互斥，end_date 为不含当天的结束日。时区固定 Asia/Shanghai。

active 要求 date_explicit=true、personally_required=true 以及有效 event；cancelled 要 cancellation_explicit=true 且任务已存在。completed/dismissed 和暂停/恢复涉及真实用户确认，需 review.user_confirmation.source_question_id；对应标志由 pipeline.py 校验。文件格式通过不代表内容已获本人确认。

calendar-plan 的操作 payload 以及 Google 读回形状详见 WORKFLOW.md。真实连接器结果优先，不能用示例构造成功结果。
