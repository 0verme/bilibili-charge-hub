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
