# 运维与安全指南

## 启动与初始化

1. 复制 `.env.example` 为 `.env`。
2. 将 `APP_DOMAIN` 设置为已经解析到服务器公网 IP 的域名；开放 TCP 80/443。
3. 为 PostgreSQL 生成随机密码，为 `APP_SECRET_KEY` 生成至少 48 字节随机值，为 `CREDENTIAL_ENCRYPTION_KEY` 生成 Fernet 密钥。
4. 运行 `docker compose pull` 和 `docker compose up -d`，默认拉取 GHCR 的 `latest` 镜像。应用容器启动时自动执行 `alembic upgrade head`，Caddy 自动配置 HTTPS。
5. 打开 `https://你的 APP_DOMAIN/login`；系统会在没有启用管理员时自动进入初始化页，创建管理员后进入管理后台。

应用端口默认不发布到宿主机，只能通过同一 Compose 网络中的 Caddy 访问。Caddy 是唯一
可信代理，因此应用容器使用 `FORWARDED_ALLOW_IPS=*`；不要在修改 Compose、直接公开应用
端口后继续使用该值。会话 Cookie 在生产环境带 `Secure`、`HttpOnly` 与 `SameSite=Lax`。

仅限本机开发时，可通过 `compose.dev.yaml` 暴露 HTTP 端口并使用 development Cookie：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d db app
```

使用飞牛等外部 HTTPS 反向代理时，必须改用 `compose.proxy.yaml`，不要叠加
`compose.dev.yaml`：

```bash
docker compose -f compose.yaml -f compose.proxy.yaml up -d app db
```

外部代理配置保持 `APP_ENV=production`，并通过 `FORWARDED_ALLOW_IPS` 信任代理转发的协议。
代理容器或 Docker 网关地址可能在重建后变化，因此默认值为 `*`；应用端口应仅开放给反向
代理或受信网络，可通过 `APP_BIND_ADDRESS` 限制监听地址。

## 备份与恢复

使用仓库脚本创建 custom-format dump、内容清单和 SHA-256 校验文件：

```bash
./scripts/backup.sh
```

恢复脚本会先验证校验和与归档结构，再恢复到临时数据库检查 Alembic 版本；验证通过后
停止 app、清理并恢复正式数据库：

```bash
./scripts/restore.sh ./backups/你的备份.dump --confirm-destructive-restore
```

脚本读取 `POSTGRES_USER` 和 `POSTGRES_DB`，不硬编码数据库身份。恢复后必须检查
`/readyz`、登录、账号数量及凭据能否解密。备份、校验文件和
`CREDENTIAL_ENCRYPTION_KEY` 必须分别保存在异地介质；密钥丢失后，数据库中的 B 站与
通知凭据无法恢复。`backups/` 已被 Git 忽略。

## 升级与迁移

1. 先备份数据库和当前 `.env`。
2. 获取新版本代码并阅读发布说明。
3. 执行 `docker compose pull` 和 `docker compose up -d`。如需固定版本，先在 `.env` 中设置 `APP_IMAGE=ghcr.io/0verme/bilibili-charge-hub:<版本标签>`。
4. 检查 `/healthz`、`/readyz`、容器日志和最近任务状态。迁移由应用启动命令执行；发生错误时不要跳过迁移或修改版本表。

## 安全说明

- 真实 Cookie 和通知凭据使用 Fernet 加密存储，API 和页面只返回元数据或掩码；当前不保存 refresh token。
- 系统用户只能查询带自身 `user_id` 的账号、记录、任务、渠道与券领取数据；管理员也不会获得其他用户明文凭据。
- 通用 Webhook 禁止非 HTTP(S) 协议、危险方法/请求头、本机、私网、链路本地和云元数据目标，并在发送前复核 DNS 解析结果。
- 分享链接只读，Token 随机生成且数据库仅存摘要；密码通过 POST 换取短期 HttpOnly 访问 Cookie。
- Bilibili Web 接口并非稳定的官方开放 API，可能变化、限流或触发风控。默认采集周期为 5 分钟；客户端只对 429、5xx 和网络错误做有限退避重试。
- 系统只检查并领取会员 B 币券，不会自动消费或自动为 UP 主充电。

## 故障排查

- `401 Bilibili login expired`：重新扫码绑定账号。
- 任务不运行：确认任务已启用、时区为 `Asia/Shanghai`，并检查 `job_runs` 的错误摘要。
- 通知失败：查看 `notification_deliveries` 的状态和脱敏响应摘要；不要把完整 Token 打入日志。
- Docker Hub 拉取失败：检查宿主机代理、DNS 和 Docker Desktop 网络后重试构建。

## 部署限制与保留策略

- 当前只支持单 app 副本、单 Uvicorn worker；不要横向扩容。
- `RETENTION_DAYS` 默认 90 天，用于清理已完成通知、任务运行记录和过期会话；充电记录不会被自动清理。
- 每次升级前执行备份并验证 `/readyz`。建议定期进行恢复演练，而不只检查备份文件是否存在。
