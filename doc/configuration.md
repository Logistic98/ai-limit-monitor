# 配置说明

项目从环境变量和根目录的 `.env` 加载配置，同名环境变量优先。Docker Compose 部署时，`.env` 通过服务的 `env_file` 注入容器。配置项及默认值见 [`.env.example`](../.env.example)。

## Telegram

| 配置项 | 作用 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 提供的机器人令牌 |
| `TELEGRAM_CHAT_ID` | 接收定时通知和告警的会话 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 可发送命令的会话，多个会话用逗号分隔。留空时仅允许通知会话 |

命令回复发回发起命令的会话，自动通知发往 `TELEGRAM_CHAT_ID`。首次配置至少填写令牌和通知会话，部署步骤见[部署文档](deployment.md)。

## 监控与续费

- `CLAUDE_ENABLED` 和 `CODEX_ENABLED` 分别控制 Claude 与 Codex 的监控。
- `CHECK_INTERVAL_SECONDS` 控制用量检查间隔，`SEND_STARTUP_REPORT` 控制启动时是否推送报告。用量跨过 `ALERT_THRESHOLDS` 配置的阈值时会推送告警。
- `TIMEZONE` 决定日期和时间的显示时区，使用 `Asia/Shanghai` 这类时区名称。
- `CODEX_RENEWAL_REMINDER_DAYS` 接收逗号分隔的正整数。默认在续费前 14、7、3、1 天提醒，留空只停用自动提醒，`/monitor` 和 `/diagnose` 仍可查询续费日期。

## 重置卡

`CODEX_RESET_CHECK_INTERVAL_SECONDS` 和 `CODEX_RESET_RETRY_SECONDS` 分别控制查询和失败重试间隔。使用时间由 `CODEX_RESET_USE_BEFORE_SECONDS` 相对卡片到期时间计算，提醒时间由 `CODEX_RESET_NOTIFY_BEFORE_SECONDS` 相对计划使用时间计算，两项都以秒为单位。

Telegram 的 `/reset_codex_auto` 可即时切换自动使用，开关会随状态保存。修改 `CODEX_RESET_AUTO_ENABLED` 的配置值并重新创建容器后，新的配置值会覆盖运行中的开关。

## 凭据与生效方式

常规部署通过 Telegram 完成 CLI 登录。容器从 `CLAUDE_CREDENTIALS_PATH` 和 `CODEX_AUTH_PATH` 指定的位置读取凭据。自定义 `STATE_PATH` 时，应放在已挂载的数据卷中，以保留提醒去重和任务状态。目录映射见[部署文档](deployment.md#数据卷)。

修改服务器的 `.env` 后重新创建容器，让配置生效。

```bash
docker compose up -d --no-build --force-recreate
```
