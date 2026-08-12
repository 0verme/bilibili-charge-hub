# 运维与安全指南

## 启动与初始化

1. 复制 `.env.example` 为 `.env`。
2. 为 PostgreSQL 生成随机密码，为 `APP_SECRET_KEY` 生成至少 48 字节随机值，为 `CREDENTIAL_ENCRYPTION_KEY` 生成 Fernet 密钥。
3. 运行 `docker compose up -d`。应用容器启动时自动执行 `alembic upgrade head`。
4. 打开 `http://服务器地址:8000`，通过 `POST /api/auth/setup` 初始化唯一的首位管理员，再登录管理后台。

生产环境应在反向代理后启用 HTTPS；会话 Cookie 在 `APP_ENV=production` 时带 `Secure`、`HttpOnly` 与 `SameSite=Lax`。

## 备份与恢复

备份 PostgreSQL：

```bash
docker compose exec -T db pg_dump -U bilibili -Fc bilibili_charge_hub > backup.dump
```

恢复前停止应用写入并创建空数据库，然后执行：

```bash
docker compose exec -T db pg_restore -U bilibili -d bilibili_charge_hub --clean --if-exists < backup.dump
```

备份必须和 `CREDENTIAL_ENCRYPTION_KEY` 一起安全保管；密钥丢失后，数据库中的 B 站与通知凭据无法恢复。不要把备份或密钥提交到 Git。

## 升级与迁移

1. 先备份数据库和当前 `.env`。
2. 获取新版本代码并阅读发布说明。
3. 执行 `docker compose build --pull` 和 `docker compose up -d`。
4. 检查 `/healthz`、容器日志和最近任务状态。迁移由应用启动命令执行；发生错误时不要跳过迁移或修改版本表。

## 安全说明

- 真实 Cookie、refresh token 和通知凭据均使用 Fernet 加密存储，API 和页面只返回元数据或掩码。
- 系统用户只能查询带自身 `user_id` 的账号、记录、任务、渠道与券领取数据；管理员也不会获得其他用户明文凭据。
- 通用 Webhook 禁止非 HTTP(S) 协议、危险方法/请求头、本机、私网、链路本地和云元数据目标，并在发送前复核 DNS 解析结果。
- 分享链接只读，Token 随机生成且数据库仅存摘要；可设置有效期、密码和昵称/UID 脱敏。
- Bilibili Web 接口并非稳定的官方开放 API，可能变化、限流或触发风控。默认采集周期为 60 秒，最低 10 秒；不要无限重试。
- 系统只检查并领取会员 B 币券，不会自动消费或自动为 UP 主充电。

## 故障排查

- `401 Bilibili login expired`：重新扫码绑定账号。
- 任务不运行：确认任务已启用、时区为 `Asia/Shanghai`，并检查 `job_runs` 的错误摘要。
- 通知失败：查看 `notification_deliveries` 的状态和脱敏响应摘要；不要把完整 Token 打入日志。
- Docker Hub 拉取失败：检查宿主机代理、DNS 和 Docker Desktop 网络后重试构建。
