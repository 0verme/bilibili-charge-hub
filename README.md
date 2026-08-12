# Bilibili Charge Hub

可开源、自托管的多用户 Bilibili 充电记录管理平台。项目正在按可运行里程碑建设，当前完成 M0：FastAPI、安全配置、测试和 Docker Compose 基线。

## 本地开发

需要 Python 3.11+：

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn app.main:app --reload
```

访问 `http://localhost:8000`，健康检查位于 `/healthz`，OpenAPI 文档位于 `/docs`。

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
