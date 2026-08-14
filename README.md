# Bilibili Charge Hub

可开源、自托管的多用户 Bilibili 充电记录管理平台。0.2 提供无需操作 Swagger 的完整管理后台，覆盖扫码绑定、增量采集、任务、通知、分享和用户管理。

## 本地开发

需要 Python 3.11+：

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn app.main:app --reload
```

访问 `http://localhost:8000/login`。系统会在没有启用管理员时自动跳转到初始化页，否则显示登录页。存活检查位于 `/healthz`，依赖就绪检查位于 `/readyz`，OpenAPI 文档位于 `/docs`。

首次启动后，在初始化页创建唯一的首位管理员。后续可在“用户与安全”中管理用户；登录会话保存在 HttpOnly Cookie 中，写操作同时受同源与 CSRF 校验保护。

登录后可调用 `POST /api/bili/qr-sessions` 获取二维码地址，并轮询 `GET /api/bili/qr-sessions/{id}`。浏览器只接触二维码和状态，B 站 Cookie 由后端提取并使用 Fernet 加密保存；refresh token 不持久化。`GET /api/bili/accounts` 仅返回当前用户的账号元数据，不返回任何凭据。

`POST /api/bili/accounts/{id}/collect` 会分页读取该账号的全部充电记录。服务优先使用上游订单标识生成稳定事件 ID，否则基于账号、用户、金额和时间生成摘要；数据库同时使用账号与事件 ID 唯一约束防止重复。每次执行都会保存页数、读取数、新增数、耗时和错误摘要，同一账号不会并发采集。

账号绑定后默认创建 5 分钟增量采集任务和每日 01:00 B 币券领取任务。任务配置和下次运行时间持久化，立即运行会返回可查询的运行记录。B 币券月份按 `APP_TIMEZONE` 计算；系统不包含自动消费功能。

通知中心支持飞书机器人、Telegram Bot、Server酱和通用 Webhook。渠道凭据加密保存，页面/API 只返回掩码；事件先写入 outbox，再为每个订阅渠道独立投递，失败使用指数退避重试。通用 Webhook 仅允许 HTTP(S) 的 POST/PUT/PATCH，并在发送前拒绝本机、私网、链路本地和云元数据地址。

登录后访问 `/dashboard` 管理全部功能。驾驶舱支持账号、时间、昵称/UID 和金额过滤、分页及按当前筛选流式导出。分享链接使用随机 Token、有效期、可选密码和昵称/UID 脱敏；密码通过 POST 解锁，不进入 URL 或反向代理日志。

## Docker Compose

复制 `.env.example` 为 `.env`，替换其中的域名、密码和密钥，将 `APP_DOMAIN`
解析到服务器，然后运行：

```bash
docker compose up -d
```

生产环境默认拉取 `ghcr.io/0verme/bilibili-charge-hub:latest`。如需固定版本，设置
`APP_IMAGE=ghcr.io/0verme/bilibili-charge-hub:<版本标签>` 后再启动。

访问 `https://你的 APP_DOMAIN/login`。默认 Compose 使用 Caddy 自动申请和续期 TLS
证书，应用端口只在容器网络内暴露；应用容器以非 root 用户运行，PostgreSQL 数据
保存在命名卷中。不要提交 `.env`、Cookie、数据库文件、备份或日志。

仅限本机开发且不需要 Secure Cookie 时，可运行：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d db app
```

开发覆盖文件会使用当前仓库的 `Dockerfile` 构建应用镜像。

使用飞牛等外部 HTTPS 反向代理时，不要使用开发覆盖文件；请运行：

```bash
docker compose -f compose.yaml -f compose.proxy.yaml up -d app db
```

该配置保持生产 Cookie 策略，并信任代理传入的 HTTPS 转发头，避免把同源登录请求误判为
跨域请求。可通过 `APP_BIND_ADDRESS` 和 `APP_PORT` 控制宿主机监听地址及端口。

镜像发布工作流在代码推送到 `main`、推送 `v*` 版本标签或手动触发时运行。首次发布后，
如果部署服务器需要免登录拉取，请在 GitHub 的包设置中将容器包可见性改为 Public；私有包
则需要在服务器上先登录 `ghcr.io`。

> 当前版本只支持一个 app 副本和一个 Uvicorn worker。不要使用 `--workers` 或扩展 Compose app 副本，否则内存调度器可能重复执行任务。

## 安全边界

- 所有租户业务数据必须通过当前系统用户过滤。
- B 站 Cookie 和通知凭据只允许加密持久化。
- 当前不保存或使用 B 站 refresh token；登录失效后会暂停账号任务并要求重新扫码。
- API、日志和页面不得返回完整 Cookie 或 Token。
- Cloudflare Worker/D1 仅作为可选扩展，不是主系统依赖。
- Bilibili Web 接口不是稳定的官方开放 API，使用时应控制频率并遵守平台规则。
- 系统不会自动消费 B 币券；任何未来的自动充电功能必须默认关闭并二次确认。

完整的启动初始化、备份恢复、升级迁移、安全边界和故障排查见 [运维与安全指南](docs/operations.md)。

## 许可证

[MIT](LICENSE)
