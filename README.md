# AI Limit Monitor

AI Limit Monitor 是一个使用 Python 编写的 Claude Code 与 OpenAI Codex 限额监控服务。项目使用 uv 管理依赖，支持 Docker Compose 部署，并通过 Telegram Bot 推送定时报告、阈值告警、错误恢复通知和容器内浏览器登录流程。

项目参考了以下开源项目和实现思路：

- [kai-denrei/claude-usage-monitor](https://github.com/kai-denrei/claude-usage-monitor)：Claude Code OAuth usage API 读取方式。
- [mokyiichek/ClaudeUsageMonitorByAPI](https://github.com/mokyiichek/ClaudeUsageMonitorByAPI)：Claude Code 配额定时 Telegram 汇报思路。
- [Keshkov/AI-Limit-Tracker-bot](https://github.com/Keshkov/AI-Limit-Tracker-bot)：Telegram Bot 形式的限额提醒体验。
- OpenAI Codex CLI 当前源码中的 `/wham/usage` 调用方式和 rate limit payload 结构。

## 功能

- 实时读取 Claude Code 限额：调用 `https://api.anthropic.com/api/oauth/usage`，解析 5 小时、7 天、Sonnet、Opus、Cowork 等窗口。
- 实时读取 Codex 限额：调用 `https://chatgpt.com/backend-api/wham/usage`，解析 Codex 主窗口、7 天窗口、附加模型窗口、credit / spend control 信息。
- Telegram Bot 通知：支持周期性报告、阈值告警、错误告警、恢复通知和登录辅助命令；阈值告警与 /monitor 使用同一种完整报告形式。
- 阈值去重：默认在 50%、70%、80%、90%、100% 用量阈值跨越时推送一次完整报告，同一个重置窗口内不会重复发送相同阈值。
- 上限识别：当 Claude usage API 返回 HTTP 429 限流时，识别为额度已达上限并在报告中以 100% 窗口呈现，而非误报为异常或要求重新登录。
- Docker 部署：镜像内置 Claude Code CLI 与 Codex CLI，通过 Telegram 生成浏览器登录链接，登录态保存在 Docker named volume 中。

## 工程结构

项目代码目录直接放在仓库根目录，并按职责分层，避免业务逻辑、外部 API、配置和展示格式混在一起。

```text
ai-limit-monitor/
├── application/                  # 应用服务层：调度、检查、通知编排
│   └── monitor_service.py
├── config/                       # 配置层：环境变量与运行配置
│   └── settings.py
├── domain/                       # 领域层：ProviderUsage、UsageWindow、登录结果等核心模型
│   ├── auth.py
│   └── models.py
├── infrastructure/               # 基础设施层：外部系统适配器
│   ├── auth/                     # 容器内 Claude / Codex CLI 登录流程
│   ├── notifications/            # Telegram Bot API 客户端
│   ├── providers/                # Claude / Codex usage API 客户端
│   └── storage/                  # JSON 状态存储
├── presentation/                 # 展示层：Telegram 消息格式化
├── shared/                       # 通用工具函数
├── cli.py                        # 命令行入口
├── __main__.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── uv.lock
```

依赖方向保持为：`cli -> application -> infrastructure / presentation -> domain`。领域模型不依赖具体外部服务，Claude、Codex、Telegram、状态文件都放在基础设施层，后续替换通知渠道或新增 provider 不需要改核心模型。

## Telegram 命令

启动服务后，可以在允许的 Telegram Chat 中发送以下命令：

```text
/start                  显示帮助
/help                   显示帮助
/check                  检查 Claude 和 Codex 登录状态
/monitor                立即监控并返回完整报告
/diagnose               输出脱敏诊断信息，用于排查 token 过期和自动刷新状态
/login_claude           生成 Claude 浏览器登录链接
/login_codex            生成 Codex 浏览器登录链接和验证码
/login_code claude CODE 提交 Claude 登录页返回的 code
/ping                   健康检查
```

注意：同一个 Telegram Bot token 同一时间只能有一个消息接收端。如果日志出现 `Conflict: terminated by other getUpdates request`，需要停止另一个 Bot 实例，或为本服务更换独立的 Bot token。

## 本地开发

安装依赖：

```bash
uv sync
```

代码检查：

```bash
uv run ruff check .
```

本地一次性检查：

```bash
uv run ai-limit-monitor once
```

发送 Telegram 测试消息：

```bash
uv run ai-limit-monitor send-test
```

启动长期服务：

```bash
uv run ai-limit-monitor run
```

## 配置

复制配置模板：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
# Telegram Bot 配置
# TELEGRAM_BOT_TOKEN：从 BotFather 获取的 Bot Token。
TELEGRAM_BOT_TOKEN=replace-with-your-telegram-bot-token
# TELEGRAM_CHAT_ID：接收通知的 Telegram Chat ID。
TELEGRAM_CHAT_ID=replace-with-your-telegram-chat-id
# TELEGRAM_ALLOWED_CHAT_IDS：允许使用 Bot 命令的 Chat ID，多个值用英文逗号分隔；留空时默认使用 TELEGRAM_CHAT_ID。
TELEGRAM_ALLOWED_CHAT_IDS=

# 监控调度配置
# CHECK_INTERVAL_SECONDS：自动检查间隔，单位秒。
CHECK_INTERVAL_SECONDS=600
# REPORT_INTERVAL_SECONDS：完整报告发送间隔，单位秒。
REPORT_INTERVAL_SECONDS=3600
# SEND_STARTUP_REPORT：服务启动时是否立即发送一次完整报告。
SEND_STARTUP_REPORT=true
# ALERT_THRESHOLDS：用量告警阈值，多个值用英文逗号分隔；跨越阈值时推送完整报告。
ALERT_THRESHOLDS=50,80,100
# TIMEZONE：时间显示使用的时区。
TIMEZONE=Asia/Shanghai
# STATE_PATH：状态文件路径，用于记录告警去重和错误恢复状态。
STATE_PATH=/data/state.json
# HTTP_TIMEOUT_SECONDS：请求 Claude、Codex、Telegram API 的超时时间，单位秒。
HTTP_TIMEOUT_SECONDS=20

# Claude Code 限额读取配置
# CLAUDE_ENABLED：是否启用 Claude 限额监控。
CLAUDE_ENABLED=true
# CLAUDE_USAGE_URL：Claude Code OAuth usage API 地址，一般不需要修改。
CLAUDE_USAGE_URL=https://api.anthropic.com/api/oauth/usage
# CLAUDE_TOKEN_URL：Claude Code OAuth token 刷新地址，一般不需要修改。
CLAUDE_TOKEN_URL=https://platform.claude.com/v1/oauth/token
# CLAUDE_OAUTH_CLIENT_ID：Claude Code OAuth 客户端 ID，一般不需要修改。
CLAUDE_OAUTH_CLIENT_ID=9d1c250a-e61b-44d9-88ed-5944d1962f5e
# CLAUDE_BETA_HEADER：Claude OAuth usage API 所需的 beta 标识，一般不需要修改。
CLAUDE_BETA_HEADER=oauth-2025-04-20
# CLAUDE_CREDENTIALS_PATH：容器内 Claude CLI 登录凭据路径，不需要手动填写 access token。
CLAUDE_CREDENTIALS_PATH=/root/.claude/.credentials.json
# CLAUDE_REFRESH_BEFORE_SECONDS：Claude access token 过期前多少秒主动刷新。
CLAUDE_REFRESH_BEFORE_SECONDS=300

# Codex 限额读取配置
# CODEX_ENABLED：是否启用 Codex 限额监控。
CODEX_ENABLED=true
# CODEX_USAGE_URL：Codex usage API 地址，一般不需要修改。
CODEX_USAGE_URL=https://chatgpt.com/backend-api/wham/usage
# CODEX_AUTH_PATH：容器内 Codex CLI 登录凭据路径，不需要手动填写 access token。
CODEX_AUTH_PATH=/root/.codex/auth.json
```

安全建议：不要把 `.env` 提交到 Git。项目已经在 `.gitignore` 中忽略 `.env` 和 `.env.*`。常规部署不需要在 `.env` 中手填 Claude 或 Codex 的 access token。Claude Code 的 access token 通常是短期 token，本服务会读取同一凭据文件中的 refresh token，在过期前自动刷新并写回凭据文件；只有 refresh token 也失效、登录被撤销或账号无权访问 usage API 时，才需要重新登录。

## 状态存储

项目没有使用数据库，也没有 SQLite、MySQL、PostgreSQL 之类的依赖。`STATE_PATH=/data/state.json` 只是一个轻量 JSON 状态文件，用来记录：

- 已经发送过哪些阈值告警，避免同一个重置窗口重复刷屏
- provider 当前是否处于错误状态，避免登录失效或请求失败时重复通知
- 上一次完整报告发送时间

Docker Compose 使用 named volume `monitor-state` 挂载到容器内 `/data`，所以仓库里不需要 `data/` 目录。

## 登录与凭据

镜像内已经安装 Claude Code CLI 和 Codex CLI，登录流程在容器内执行，凭据保存在 Docker named volume 中，不需要在宿主机手动执行 `claude auth login` 或 `codex login`，也不需要把 access token 写入 `.env`。

首次启动或登录失效时，Bot 会在 Telegram 中发送可点击的登录信息。也可以手动发送命令生成登录链接：

```text
/login_claude
/login_codex
```

Claude 登录流程：Bot 会返回 Claude 登录页面链接。用浏览器打开并完成授权后，如果页面返回 code，把 code 发回 Bot：

```text
/login_code claude YOUR_CODE
```

Codex 登录流程：Bot 会返回 Codex device 登录页面和一次性验证码。用浏览器打开页面并输入验证码即可，容器内的 Codex CLI 会自动完成登录并保存凭据。

登录态保存位置：

```text
claude-auth -> /root/.claude
codex-auth  -> /root/.codex
```

当前默认读取路径：

```text
CLAUDE_CREDENTIALS_PATH=/root/.claude/.credentials.json
CODEX_AUTH_PATH=/root/.codex/auth.json
```

如果需要清除登录态，可以删除对应 Docker volume 后重新登录：

```bash
docker compose down
docker volume rm ai-limit-monitor_claude-auth ai-limit-monitor_codex-auth
```

注意：Codex 的 device code 有时效；Claude 的 OAuth code 也有时效，生成后请尽快完成。

## Docker Compose 部署

准备 `.env` 后启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

查看状态：

```bash
docker compose ps
```

停止服务：

```bash
docker compose down
```

一次性检查：

```bash
docker compose run --rm ai-limit-monitor uv run --no-sync ai-limit-monitor once
```

发送 Telegram 测试消息：

```bash
docker compose run --rm ai-limit-monitor uv run --no-sync ai-limit-monitor send-test
```

默认 `docker-compose.yml` 使用以下 named volumes：

```text
monitor-state -> /data
claude-auth   -> /root/.claude
codex-auth    -> /root/.codex
```

这些 volume 由 Docker 管理，仓库中不需要 `data/`、`.claude/` 或 `.codex/` 目录。

## 通知策略

服务每 `CHECK_INTERVAL_SECONDS` 秒读取一次 Claude 和 Codex 用量。默认每小时发送一次完整报告，即 `REPORT_INTERVAL_SECONDS=3600`。当某个窗口的用量跨越 `ALERT_THRESHOLDS` 中的阈值时，服务会立即推送一次完整报告作为告警（与 /monitor 形式相同）。状态保存在 `STATE_PATH` 中，因此容器重启后不会重复发送同一窗口的历史阈值告警。

如果某个 provider 读取失败，会发送一次错误通知；当后续恢复成功时，会发送恢复通知。如果 Claude 反复提示需要重新登录，可以发送 `/diagnose` 查看脱敏诊断信息，重点关注 `refresh token`、`可自动刷新` 和 `token 状态`。

## 注意事项

Claude 的 usage API 和 Codex 的 `/wham/usage` 都不是稳定的公开长期兼容接口，后续 CLI 或服务端变更可能导致字段结构变化。代码已经把 provider client 和 payload parser 独立封装，后续如果接口变化，只需要调整 `infrastructure/providers/claude.py` 或 `infrastructure/providers/codex.py`。
