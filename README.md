# AI Limit Monitor

长任务开跑前，想知道 Claude Code 和 Codex 哪边还有额度、多久会重置，通常要分别去查。AI Limit Monitor 在服务器后台持续监控，把两边的用量和重置倒计时汇总到 Telegram。用量跨过设定的阈值时，机器人会主动发来报告，方便你及时安排后续任务。

## 功能特性

- **用量与重置倒计时**。查看会员的额度窗口已用多少、何时自然重置、还要等多久，达到阈值时通知。
- **额度恢复后自动开启新窗口**。额度重置后，服务可自动发起一次轻量请求，让下一轮窗口开始计时，在有5h限额时，方便使用连续两个时间窗口的配额。
- **重置卡到期前自动使用**。查看可用重置卡片和到期时间。开启自动使用后，服务会在到期前安排用卡，并在执行前提醒，手动使用需二次确认。
- **会员续费提醒**。查看下次续费时间和倒计时，并按在续费日期前收到续费通知，以防止会员中断。

## 快速开始

在装有 Docker 和 Compose 的服务器上进入项目目录。首次安装时复制配置模板。

```bash
cp .env.example .env
```

在 `.env` 中填写 BotFather 提供的 `TELEGRAM_BOT_TOKEN` 和接收通知的 `TELEGRAM_CHAT_ID`。新建机器人时，可在服务启动前向机器人发送 `/start`，再调用 `getUpdates`，将响应中的 `message.chat.id` 填入 `TELEGRAM_CHAT_ID`。[Telegram Bot API](https://core.telegram.org/bots/api#getupdates)

如需在多个会话使用命令，将所有允许的会话 ID 用英文逗号分隔后填入 `TELEGRAM_ALLOWED_CHAT_IDS`。

```bash
docker compose up -d --build
docker compose ps
```

容器显示 `healthy` 后，在 Telegram 打开机器人私聊并发送 `/ping` 确认可以收到回复。容器启动异常时运行 `docker compose logs --tail=100 ai-limit-monitor`。

部署细节与排障详见[部署文档](doc/deployment.md)。

## 登录与 Telegram 命令

- **Claude**。发送 `/login_claude` 并打开授权链接。页面如返回授权码，发送 `/login_code claude YOUR_CODE` 完成登录。
- **Codex**。发送 `/login_codex`，打开登录链接并输入机器人给出的一次性验证码。

| 命令 | 用途 |
|---|---|
| `/check` | 检查 Claude 和 Codex 的登录状态 |
| `/monitor` | 立即查询用量、重置倒计时、Codex 续费日期与倒计时，以及可用重置卡 |
| `/diagnose`、`/debug` | 查看脱敏诊断，包含凭据有效期、Codex 续费日期与倒计时、自动任务状态和最近错误 |
| `/login_claude`、`/login claude` | 获取 Claude 授权链接 |
| `/login_codex`、`/login codex` | 获取 Codex 设备登录链接和一次性验证码 |
| `/login_code claude 授权码`、`/login-code claude 授权码` | 提交 Claude 授权页返回的授权码 |
| `/quota_refresh`、`/quota_refresh status` | 查看限额恢复任务状态 |
| `/quota_refresh on`、`/quota_refresh off` | 开启或关闭额度恢复后的自动请求 |
| `/quota_refresh claude`、`/quota_refresh codex` | 立即通过指定 CLI 发起一次轻量请求 |
| `/reset_codex_cards` | 查询 Codex 可用重置卡、到期时间和计划使用时间 |
| `/reset_codex_tasks` | 查看重置卡使用计划、提醒和失败重试状态 |
| `/reset_codex_auto on`、`/reset_codex_auto off` | 开启或关闭重置卡自动使用 |
| `/reset_codex_use` | 选出最早到期的可用卡片并生成确认码 |
| `/reset_codex_use confirm 确认码` | 在同一会话确认用卡，确认码有效期为 5 分钟 |
| `/ping` | 检查机器人是否在线 |
| `/start`、`/help` | 显示命令帮助 |

操作示例与注意事项详见[使用文档](doc/usage.md)。

## 自动提醒和任务

- **用量告警**。用量跨过配置阈值时，机器人会发送一份监控报告。
- **Codex 续费提醒**。默认在续费前 14、7、3、1 天提醒。用 `CODEX_RENEWAL_REMINDER_DAYS` 调整天数，留空可停用提醒。
- **限额恢复**。首次部署默认开启。额度窗口重置后会通过对应 CLI 发起一次轻量请求，用 `/quota_refresh off` 可关闭。
- **Codex 重置卡**。首次部署默认在卡片到期前自动使用，用 `/reset_codex_auto off` 可关闭。手动使用时发送 `/reset_codex_use`，再按机器人回复二次确认。

阈值、提醒时间和其他环境变量详见[配置说明](doc/configuration.md)。

## 更新和配置

更新源码后重新运行 `docker compose up -d --build`。仅修改 `.env` 时运行 `docker compose up -d --no-build --force-recreate`。Docker 数据卷保存登录凭据、调度任务和提醒记录，更新时沿用现有数据卷。

本地开发与测试详见[开发规范](doc/development.md)。
