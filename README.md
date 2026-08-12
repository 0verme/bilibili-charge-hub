# Bilibili Charge Hub

可开源、自托管的多用户 Bilibili 充电记录管理平台。当前已具备安全配置、核心多租户数据模型、Alembic 迁移、管理员初始化和会话认证。

## 本地开发

需要 Python 3.11+：

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn app.main:app --reload
```

访问 `http://localhost:8000`，健康检查位于 `/healthz`，OpenAPI 文档位于 `/docs`。

首次启动后，通过 `POST /api/auth/setup` 提交至少 12 位、同时包含字母和数字的密码来创建唯一的初始管理员。后续管理员可通过 `/api/users` 创建其他系统用户；登录会话保存在 HttpOnly Cookie 中，数据库只保存会话 Token 的 SHA-256 摘要。

登录后可调用 `POST /api/bili/qr-sessions` 获取二维码地址，并轮询 `GET /api/bili/qr-sessions/{id}`。浏览器只接触二维码和状态，B 站 Cookie 与 refresh token 由后端提取并使用 Fernet 加密保存。`GET /api/bili/accounts` 仅返回当前用户的账号元数据，不返回任何凭据。

`POST /api/bili/accounts/{id}/collect` 会分页读取该账号的全部充电记录。服务优先使用上游订单标识生成稳定事件 ID，否则基于账号、用户、金额和时间生成摘要；数据库同时使用账号与事件 ID 唯一约束防止重复。每次执行都会保存页数、读取数、新增数、耗时和错误摘要，同一账号不会并发采集。

账号绑定后默认创建 60 秒采集任务和每日 01:00 B 币券领取任务。`/api/jobs` 支持 interval（最低 10 秒）与 cron、启停、修改周期和立即运行；配置持久化在数据库中并在进程启动时恢复。低于常规周期可能触发限流或风控，请谨慎使用。B 币券结果按账号和月份幂等保存；系统不包含自动消费功能。

通知中心支持飞书机器人、Telegram Bot、Server酱和通用 Webhook。渠道凭据加密保存，页面/API 只返回掩码；事件先写入 outbox，再为每个订阅渠道独立投递，失败使用指数退避重试。通用 Webhook 仅允许 HTTP(S) 的 POST/PUT/PATCH，并在发送前拒绝本机、私网、链路本地和云元数据地址。

登录后访问 `/dashboard` 查看今日、本月、累计、实际到账、平台差额、贡献者、趋势、最近记录与任务状态。`/api/dashboard` 支持账号、时间、昵称/UID 和金额过滤及分页，`/api/dashboard/export.csv` 导出当前租户记录。分享链接使用随机 Token、有效期、可选密码和昵称/UID 脱敏，数据库只保存 Token 摘要。

## Docker Compose

复制 `.env.example` 为 `.env`，替换其中所有占位密码和密钥，然后运行：

```bash
docker compose up -d
```

应用容器以非 root 用户运行，PostgreSQL 数据保存在命名卷中。不要提交 `.env`、Cookie、数据库文件或日志。

## 安全边界

- 所有租户业务数据必须通过当前系统用户过滤。
- B 站 Cookie、refresh token 和通知凭据只允许加密持久化。
- API、日志和页面不得返回完整 Cookie 或 Token。
- Cloudflare Worker/D1 仅作为可选扩展，不是主系统依赖。
- Bilibili Web 接口不是稳定的官方开放 API，使用时应控制频率并遵守平台规则。
- 系统不会自动消费 B 币券；任何未来的自动充电功能必须默认关闭并二次确认。

## 许可证

[MIT](LICENSE)
