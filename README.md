# AutoMoGuDing SaaS（工学云自动化打卡平台）

AutoMoGuDing SaaS 是一个自托管的工学云自动化平台，提供管理端和用户端两套 Web 界面，覆盖自动打卡、支付宝安全验证后继续打卡、缺卡筛选、手动补卡、日报 / 周报 / 月报提交、批量执行、AI 报告生成和运行观测。
适合个人、班级和小团队统一托管工学云打卡与报告任务。

## 项目速览

| 维度 | 当前状态 |
|------|----------|
| 产品形态 | 自托管 Web 平台 |
| 后端 | FastAPI + SQLModel + MySQL + Alembic + APScheduler |
| 前端 | Vue 3 + Vite + Pinia + Vue Router + Element Plus |
| 入口 | 管理端 `/login`；用户端 `/u/login`、`/u/register`、`/u`、`/u/settings` |
| 数据库 | 仅支持 MySQL，连接串必须使用 `mysql+pymysql://` |
| 调度模型 | `app` 提供 Web / API，`worker` 运行定时任务和批量队列 |
| 安全基线 | HttpOnly Cookie、CSRF、CSP、HSTS、权限点、限流、审计、敏感字段加密 |

## 界面截图

### 管理端

| 用户列表与首页 | 打卡设置 |
|---|---|
| ![首页 - 用户列表](./img/首页-用户列表.png) | ![打卡设置页](./img/打卡设置页.png) |

| 补卡详情 | 日报 / 周报 / 月报补交 |
|---|---|
| ![补卡详细](./img/补卡详细.png) | ![补日 - 周 - 月报详细](./img/补日-周-月报详细.png) |

| 报告设置 | 推送设置 | 全局邮箱通知 |
|---|---|---|
| ![报告设置](./img/报告设置.png) | ![推送设置](./img/推送设置.png) | ![全局邮箱通知](./img/全局邮箱通知.png) |

### 用户端

| 用户工作台 | 打卡配置 |
|---|---|
| ![用户端-首页](./img/用户端-首页.png) | ![用户端-打卡配置](./img/用户端-打卡配置.png) |

| 报告配置 | 推送配置 |
|---|---|
| ![用户端-报告配置](./img/用户端=报告配置.png) | ![用户端-推送配置](./img/用户端-推送配置.png) |

## 适用场景

| 使用者 | 适合做什么 | 不适合做什么 |
|--------|------------|--------------|
| 个人用户 | 托管自己的工学云打卡、报告和补卡 | 当作公开在线 Demo 给陌生人共用 |
| 班级 / 小团队 | 集中管理多个账号、统一查看执行结果 | 无限制堆用户，不做队列和数据库容量规划 |
| 管理员 | 批量执行、处理缺卡、配置 AI / SMTP / 代理 | 让用户端登录态和管理端登录态混用 |
| 运维人员 | Docker Compose 部署、备份恢复 | 生产环境依赖运行时自动改表 |
| 二次开发者 | 扩展接口、报告模板、推送渠道和前端页面 | 绕开现有权限、审计和测试 |

## 能力矩阵

| 模块 | 管理端 | 用户端 | 关键后端模块 |
|------|--------|--------|--------------|
| 登录认证 | 管理员登录、角色权限、权限点校验 | 注册 / 登录、绑定工学云账号 | `server/auth.py`、`server/api.py` |
| 用户管理 | 新增、编辑、软删除、重置状态 | 读取和保存自身配置 | `server/models.py`、`server/user_runtime.py` |
| 自动打卡 | 为用户配置打卡时间、地点、周期；处理安全验证 | 自助维护个人配置；验证后显式继续打卡 | `server/scheduler.py`、`server/task_runner.py` |
| 缺卡筛选 | 查询指定用户缺卡日期 | 查询当前用户缺卡日期 | `server/clockin_backfill.py` |
| 手动补卡 | 为指定用户补选中或补全部 | 自助补选中或补全部 | `server/task_runner.py`、`server/coreApi/MainLogicApi.py` |
| 报告补交 | 日报 / 周报 / 月报生成与补交 | 日报 / 周报 / 月报生成与补交 | `server/task_runner.py`、`server/coreApi/AiServiceClient.py` |
| 批量任务 | 创建、暂停、恢复、取消、失败重试 | 不开放批量管理 | `server/queue_worker.py` |
| AI 设置 | 全局 API URL / API Key / Model 和测试 | 使用全局 AI 配置生成报告 | `server/settings` 相关 API、AI 客户端 |
| 推送设置 | 全局 SMTP；代维护单用户推送 | 自助维护个人推送配置 | `server/util/notifier.py` |
| 地理编码 | 地址搜索、经纬度回填、地图核对页 | 地址搜索、经纬度回填 | `/geocode/search`、`/geocode/reverse` |
| 观测审计 | 审计日志、任务事件、`/metrics` | 查看自身执行记录 | `server/observability.py` |
| 备份恢复 | 通过 CLI 导入 / 导出数据库 JSON | 不开放 | `server/backup_cli.py` |

## 入口速查

| 场景 | 地址 / 命令 | 说明 |
|------|-------------|------|
| 根地址 | `/` | 管理员会话进入管理端，用户会话进入 `/u`，未登录跳转 `/u/login` |
| 管理端登录 | `/login` | 后台管理员入口 |
| 用户端登录 | `/u/login` | 用户独立登录态，不复用管理端登录态 |
| 用户端注册 | `/u/register` | 受 `APP_REGISTRATION_ENABLED` 控制，生产默认关闭 |
| 用户工作台 | `/u` | 手动执行、执行记录、日报快捷入口 |
| 用户设置 | `/u/settings` | 打卡、报告、补卡、推送配置 |
| API 文档 | `/docs` | 生产默认关闭，需 `EXPOSE_API_DOCS=true` |
| OpenAPI | `/openapi.json` | 生产默认关闭，契约快照见 `docs/api/openapi-contract.json` |
| Prometheus 指标 | `/metrics.prom` | 生产需要 `METRICS_AUTH_TOKEN` |

## 核心流程

### 打卡与补卡

| 阶段 | 行为 | 关键规则 |
|------|------|----------|
| 自动打卡 | 根据用户配置的上下班时间、周期、地址和图片执行 | 定时任务只做正常打卡，不自动补卡 |
| 缺卡查询 | 拉取工学云记录并归一化为 `START` / `END` | 同一天只缺一种类型时，只展示该类型 |
| 选择补卡 | 先选 `上班` 或 `下班`，再选日期 | 一次只补一种类型 |
| 补选中 | 对 `target_dates` 中的日期逐个补卡 | 支持多日期 |
| 全部待补 | 重新查询当前类型仍缺的日期后执行 | 不跨类型补卡 |
| 频繁请求 | 命中 `429`、`rate limit` 或 IP 限制时等待重试 | 当前日期成功后进入冷却降速；持续失败则停止剩余日期 |
| 代理 | 只在手动补卡执行阶段启用 | 登录、缺卡查询、定时打卡、报告提交不使用补卡代理 |

### 支付宝安全验证

工学云打卡接口可能返回 HTTP 200、业务 `code == 200`，同时 `msg == "304"`。该响应表示需要支付宝安全验证，不代表打卡成功。

| 阶段 | 系统行为 | 用户行为 |
|------|----------|----------|
| 首次触发 `304` | 创建支付宝验证登记，返回 `outRegisterNo` 和经过协议校验的 `registerUrl` | 在当前页面打开支付宝完成验证 |
| 等待验证 | 不自动重试打卡，不把任务记录为成功 | 完成验证后点击「验证完成，继续打卡」 |
| 显式继续 | 携带 `outRegisterNo` 只提交 1 次打卡 | 等待成功或新的验证提示 |
| 再次触发 `304` | 创建新登记并替换页面内存中的验证信息 | 使用新链接再次验证 |

用户端调用 `POST /api/app/clock-in/alipay/continue`，管理端调用 `POST /api/users/{user_id}/clock-in/alipay/continue`。登记编号和深链不写入审计日志、持久化执行结果或消息通知。补卡流程不会进入该继续接口。

### 报告补交

| 类型 | 待补周期 | 操作 | 限制 |
|------|----------|------|------|
| 日报 | 日期 | 生成、提交、补全部日报 | 未开启日报时接口直接返回空列表 |
| 周报 | 自然周 | 生成、提交、补全部周报 | 不会混入日报或月报 |
| 月报 | 月份 | 生成、提交、补全部月报 | 不会混入日报或周报 |

报告类“立即执行”不触发普通 `/run` 接口的 429 限流；打卡和默认手动运行仍保留内部限流。

## 快速开始

### 环境要求

| 组件 | 版本建议 | 说明 |
|------|----------|------|
| Python | 3.10+ | 后端运行和测试 |
| Node.js | 20+ | 前端开发和构建 |
| MySQL | 8+ | 唯一支持的数据库 |
| Docker Compose | v2+ | 一体化部署 |

### 本地开发

| 步骤 | 命令 | 说明 |
|------|------|------|
| 安装后端依赖 | `pip install -r server/requirements.txt` | 建议使用虚拟环境 |
| 升级数据库 | `python -m alembic upgrade head` | 生产和联调优先使用迁移 |
| 启动后端 | `python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147` | API 默认在 `8147` |
| 安装前端依赖 | `cd web && npm install` | 首次开发执行 |
| 启动前端 | `cd web && npm run dev` | Vite 默认 `5173`，`/api` 代理到后端 |
| 修改代理目标 | `VITE_API_PROXY_TARGET=http://127.0.0.1:8147 npm run dev` | 后端不在默认地址时使用 |

Windows PowerShell：

```powershell
Copy-Item .env.example .env
python -m alembic upgrade head
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147

cd web
npm install
npm run dev
```

### Docker Compose

| 方式 | 命令 | 适用场景 |
|------|------|----------|
| 源码构建 | `docker compose up -d --build` | 本机或服务器直接从源码构建 |
| 预构建镜像 | `docker compose -f docker-compose.image.yml up -d` | 使用 `APP_IMAGE` 指定的 GHCR 或 Docker Hub 镜像 |

两套 Compose 均启动两个应用服务；它们不内置 MySQL，必须通过 `DATABASE_URL` 连接可用的 MySQL 8 实例：

| 服务 | `APP_ROLE` | 职责 |
|------|------------|------|
| `app` | `api` | Web / API，不运行定时任务和批量队列 |
| `worker` | `worker` | APScheduler 定时任务和批量任务队列 |

预构建镜像默认使用 `ghcr.io/27xk/gongxueyun:latest`。如需检查并拉取指定镜像更新，可在 Windows PowerShell 中运行：

```powershell
$env:APP_IMAGE = 'ghcr.io/27xk/gongxueyun:latest'
./scripts/check-image-update.ps1
```

脚本退出码 `10` 表示检测到新镜像并已拉取，退出码 `0` 表示当前镜像没有变化。

## 验证命令

| 范围 | 命令 |
|------|------|
| 后端测试 | `python -m unittest discover -s tests` |
| 后端编译 | `python -m compileall server` |
| OpenAPI 契约 | `python scripts/openapi_contract.py` |
| 后端质量门 | `python scripts/quality_gate.py` |
| 供应链策略 | `python scripts/verify_supply_chain_policy.py` |
| 备份恢复演练 | `python scripts/backup_restore_drill.py` |
| Python 依赖审计 | `pip-audit -r server/requirements.txt` |
| 前端安装 | `cd web && npm ci` |
| 前端审计 | `cd web && npm audit --audit-level=high` |
| 前端检查 | `cd web && npm run lint && npm test` |
| 前端构建 | `cd web && npm run build` |

## 目录结构

| 路径 | 说明 |
|------|------|
| `server/` | FastAPI 后端、调度器、任务执行、数据模型和迁移 |
| `web/` | Vue 3 前端 |
| `docs/current-features.md` | 当前功能、接口和运行行为速查 |
| `docs/ops/runbook.md` | 线上排障、CI 失败和批量任务处理手册 |
| `monitoring/prometheus/alerts.yml` | Prometheus 告警规则 |
| `tests/` | 后端 unittest 测试 |
| `docker-compose.yml` | 本地源码构建部署 |
| `img/` | README 和文档截图 |

## 文档索引

| 文档 | 适合读者 | 内容 |
|------|----------|------|
| [当前功能与接口速查](./docs/current-features.md) | 产品、测试、二次开发 | 功能范围、页面入口、接口、补卡和报告规则 |
| [后端说明](./server/README.md) | 后端开发、运维 | 启动、环境变量、API、任务链路和备份 |
| [前端说明](./web/README.md) | 前端开发、联调 | 页面入口、路由、截图、接口约定、用户端页面 |
| [运行手册](./docs/ops/runbook.md) | 运维、值班 | 供应链、认证、5xx、批量任务、AI、权限排障 |
| [Roadmap](./ROADMAP.md) | 产品、维护者 | 后续计划和优先级 |
| [贡献指南](./CONTRIBUTING.md) | 贡献者 | Issue / PR 要求、代码风格 |
| [更新日志](./CHANGELOG.md) | 所有人 | 版本发布记录 |
| `docs/superpowers/` | 维护者 | 历史规格和实现计划，不一定代表最新实现 |

## 反馈信息模板

| 问题类型 | 请提供 |
|----------|--------|
| 登录失败 | 登录入口、账号类型、接口响应、后端日志、是否触发限流 |
| 前后端联调失败 | 前端地址、后端地址、`VITE_API_PROXY_TARGET`、请求路径、状态码 |
| 数据库错误 | `DATABASE_URL` 脱敏后主机信息、Alembic 版本、完整 SQL 错误 |
| 补卡失败 | 用户配置、目标日期、`target_type`、工学云返回、代理切换次数 |
| 报告失败 | 报告类型、周期、AI 设置、`AI_ALLOWED_HOSTS`、后端任务日志 |
