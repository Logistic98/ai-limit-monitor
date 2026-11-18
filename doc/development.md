# 开发规范

项目使用 Python 3.12+ 和 `uv`。依赖、命令入口与 Ruff 规则在 [`pyproject.toml`](../pyproject.toml)，依赖版本由 [`uv.lock`](../uv.lock) 锁定。

## 代码入口

| 路径 | 职责 |
|---|---|
| [`cli.py`](../cli.py) | 本地命令解析与服务启动 |
| [`application/monitor_service.py`](../application/monitor_service.py) | Telegram 命令、监控检查、报告与续费提醒编排 |
| [`application/quota_refresh_service.py`](../application/quota_refresh_service.py)、[`application/reset_credit_service.py`](../application/reset_credit_service.py) | 限额恢复与重置卡自动任务 |
| [`infrastructure/providers/`](../infrastructure/providers/) | 查询额度、订阅、重置卡和更新凭据 |
| [`domain/models.py`](../domain/models.py) | 用量窗口、积分和续费等统一数据结构 |
| [`presentation/`](../presentation/) | Telegram 消息格式 |
| [`infrastructure/storage/json_state.py`](../infrastructure/storage/json_state.py) | 状态读取与保存 |

## 修改约定

- 服务接口字段变化时，在对应的 Provider 中解析并映射为领域模型。消息展示读取统一模型，日期和倒计时复用 [`shared/utils.py`](../shared/utils.py)。
- 增加 Telegram 命令时同步检查命令路由、`/help` 文案和 [Bot 菜单](../infrastructure/notifications/telegram.py)，并更新[使用文档](usage.md)。
- 配置项在 [`config/settings.py`](../config/settings.py) 定义，示例与单位放在 [`.env.example`](../.env.example)。新增持久化字段时，旧状态文件缺少该字段也应能正常加载。
- 调度相关验证可使用固定时钟和临时状态文件。HTTP 与 Telegram 交互用替身验证，避免开发测试消耗真实额度或发送通知。

## 本地验证

```bash
uv sync
uv run python -m unittest discover
uv run ruff check .
```

完成[配置](configuration.md)后，可以运行一次真实检查。带 `--notify` 的版本还会推送 Telegram 报告。

```bash
uv run ai-limit-monitor once
```
