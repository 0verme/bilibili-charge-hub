# 运维与安全指南

## 启动与初始化

1. 复制 `.env.example` 为 `.env`。
2. 将 `APP_DOMAIN` 设置为已经解析到服务器公网 IP 的域名；开放 TCP 80/443。
3. 为 PostgreSQL 生成随机密码，为 `APP_SECRET_KEY` 生成至少 48 字节随机值，为 `CREDENTIAL_ENCRYPTION_KEY` 生成 Fernet 密钥；建议同时生成并保存 `ADMIN_RECOVERY_TOKEN`，用于管理员忘记密码时恢复。
4. 运行 `docker compose pull` 和 `docker compose up -d`，默认拉取 GHCR 的 `latest` 镜像。应用容器启动时自动执行 `alembic upgrade head`，Caddy 自动配置 HTTPS。
5. 打开 `https://你的 APP_DOMAIN/login`；系统会在没有启用管理员时自动进入初始化页，创建管理员后进入管理后台。

应用端口默认不发布到宿主机，只能通过同一 Compose 网络中的 Caddy 访问。Caddy 是唯一
可信代理，因此应用容器使用 `FORWARDED_ALLOW_IPS=*`；不要在修改 Compose、直接公开应用
端口后继续使用该值。会话 Cookie 在生产环境带 `Secure`、`HttpOnly` 与 `SameSite=Lax`。

### 管理员密码恢复

在 `.env` 中配置长度至少 32 个字符的 `ADMIN_RECOVERY_TOKEN` 后，打开 `/reset`，输入管理员用户名、恢复令牌和新密码即可重置。恢复令牌只从环境变量读取，不写入数据库或日志；重置成功后，该管理员的所有已有会话都会失效。恢复入口按来源地址限流，且只允许重置启用状态的管理员。

如果未配置 `ADMIN_RECOVERY_TOKEN`，恢复接口保持关闭；已有管理员仍可在后台“用户管理”中重置密码。

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

1. 先备份数据库和当前 `.env`；包含充电记录去重迁移的版本必须确认备份可恢复。
2. 获取新版本代码并阅读发布说明。
3. 执行 `docker compose pull` 和 `docker compose up -d`。如需固定版本，先在 `.env` 中设置 `APP_IMAGE=ghcr.io/0verme/bilibili-charge-hub:<版本标签>`。
4. 检查 `/healthz`、`/readyz`、容器日志和最近任务状态。迁移由应用启动命令执行；发生错误时不要跳过迁移或修改版本表。

`0006_canonical_charge_keys` 会为历史充电记录生成稳定业务键，并将同一账号下的重复行合并为最早入库的主记录。主记录会尽量保留完整昵称、头像和备注；重复 outbox 标记为 `merged`，已有 `notification_deliveries` 发送审计保留，已发送的飞书消息不会撤回。迁移完成后应检查充电记录数量、`/readyz` 和通知日志；合并后的 outbox 不会进入自动重试。

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

## 通知对账（静默遗漏恢复）

充电记录入库与通知投递是不同事务：进程、Docker 或数据库短暂中断后，可能出现充电记录已入库、但 `new_charge` 通知未被投递的静默缺口。系统每小时自动执行一次通知对账（系统任务 `notification-reconciliation`），管理员也可在通知中心手动触发 `POST /api/notifications/reconcile`（admin only，受 CSRF / 同源 / 频率限制保护）。

### 什么时候会触发

- 应用启动后由内存调度器每小时自动执行一次；
- 管理员手动触发（并发时自动跳过，避免与定时任务重叠）。

### 扫描范围与限制

- 默认只扫描最近 24 小时入库的充电记录：`NOTIFICATION_RECONCILIATION_LOOKBACK_HOURS`（1–168，默认 24）；
- 单次扫描上限：`NOTIFICATION_RECONCILIATION_MAX_RECORDS`（默认 2000），防止一次扫描无限数据；
- 时间窗口按 UTC 计算，与 `APP_TIMEZONE` 展示无关。

### 对账修复了什么

1. 记录存在但 `new_charge` outbox 缺失 → 用与正常采集完全一致的 payload 和去重键补建；
2. outbox 存在但订阅渠道缺投递记录 → 补建投递记录并交回现有投递链路；已投递的 outbox 因新增渠道被重入队（仅投递缺失渠道，成功记录永不重发）；
3. 失败投递 → 尊重原有指数退避与 `MAX_ATTEMPTS` 预算，只审计不强制重发；预算耗尽的 outbox 同样只审计。
4. `notification_eligible=false` 的历史补录记录不会被对账任务补建 `new_charge` 通知；只有正常新充电记录参与对账。

### 查看结果

每次执行的审计摘要以结构化日志输出，事件为 `notification_reconciliation_started` / `notification_reconciliation_completed` / `notification_reconciliation_failed`，字段包括 `scanned`、`missing_outbox`、`outbox_rebuilt`、`missing_deliveries`、`deliveries_created`、`requeued`、`already_complete`、`skipped`、`errors`、`duration_ms`：

```bash
docker compose logs app | grep notification_reconciliation_completed
```

手动触发接口直接返回同样的摘要 JSON。

### 失败排查

- `errors` 非零：检查对应日志中的 `reconciliation record failed`，通常是并发竞态（唯一约束冲突，已安全跳过）或数据库异常；
- `skipped` 非零：多为投递预算耗尽的 outbox，可在通知中心对对应投递手动重试；
- 长时间中断后缺口仍在：中断超过 lookback 窗口的部分不会被追溯，属预期行为；
- 对账不会调用 Bilibili API，也不会修改充电记录、金额或用户归属。

## 部署限制与保留策略

- 当前只支持单 app 副本、单 Uvicorn worker；不要横向扩容。
- `RETENTION_DAYS` 默认 90 天，用于清理已完成通知、任务运行记录和过期会话；充电记录不会被自动清理。
- 每次升级前执行备份并验证 `/readyz`。建议定期进行恢复演练，而不只检查备份文件是否存在。
