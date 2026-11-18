# 部署文档

## 首次部署

在装有 Docker 和 Compose 的服务器上进入项目目录。首次部署时复制模板，按[配置说明](configuration.md)填写 Telegram Bot Token 和通知会话。

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
docker compose ps
```

等待容器状态为 `healthy`。在 Telegram 向机器人发送 `/ping` 确认能收到回复，再用 `/check` 检查 Claude 和 Codex 的登录状态。登录成功后可以用 `/monitor` 验证额度、重置时间和 Codex 续费信息。 [登录步骤](usage.md#登录与检查)

## 更新与停止

更新源码后重新构建并启动容器。仅修改 `.env` 时，直接按[配置说明](configuration.md#凭据与生效方式)重新创建容器即可。已有部署沿用原来的 `.env`。

```bash
docker compose up -d --build
```

暂停服务时运行 `docker compose stop`，恢复时运行 `docker compose up -d`。

## 数据卷

| 数据卷 | 容器目录 | 用途 |
|---|---|---|
| `monitor-state` | `/data` | 告警和续费提醒的去重记录、调度任务与开关 |
| `claude-auth` | `/root/.claude` | Claude CLI 登录凭据 |
| `codex-auth` | `/root/.codex` | Codex CLI 登录凭据 |

更新时沿用现有数据卷。迁移服务器时需要带上这三个卷和部署配置，其中含登录凭据和 Bot 令牌。

## 排查

```bash
docker compose ps
docker compose logs --tail=100 ai-limit-monitor
```

- 容器启动失败时，先看日志中的配置校验错误，核对 `.env` 和数据卷挂载。
- 容器健康但机器人没有回复时，核对当前会话是否在允许列表中。日志出现轮询冲突时，检查是否有其他实例使用同一个 Bot 令牌。
- 机器人有回复但用量或续费查询失败时，用 `/diagnose` 查看对应服务的凭据状态和最近错误。

单独运行一次检查。

```bash
docker compose exec -T ai-limit-monitor /app/.venv/bin/ai-limit-monitor once
```

验证 Telegram 投递时运行测试消息命令，消息会发往配置的通知会话。

```bash
docker compose exec -T ai-limit-monitor /app/.venv/bin/ai-limit-monitor send-test
```
