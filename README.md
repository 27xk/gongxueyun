# AutoMoGuDing SaaS（工学云打卡管理平台）

AutoMoGuDing SaaS 是一个面向多用户托管的工学云自动化平台，提供管理端与用户端两套 Web 界面，支持自动打卡、缺卡记录筛选、按类型补卡、日报 / 周报 / 月报提交、批量执行，以及 AI 生成报告内容。

Self-hosted internship attendance automation platform for MoGuDing / 工学云，适合个人、班级、小团队集中托管打卡和报告任务。

**English keywords:** MoGuDing automation, internship attendance, self-hosted SaaS, FastAPI, Vue 3, Docker Compose, report automation, attendance replacement.

### 界面截图

| 用户列表与批量任务 | 打卡设置 |
|------------------|----------|
| ![首页 - 用户列表](./img/首页-用户列表.png) | ![打卡设置页](./img/打卡设置页.png) |

| 补卡详情 | 日报 / 周报 / 月报补交 |
|----------|------------------------|
| ![补卡详细](./img/补卡详细.png) | ![补日 - 周 - 月报详细](./img/补日-周-月报详细.png) |

| 报告设置 | 推送设置 | 全局邮箱通知 |
|----------|----------|--------------|
| ![报告设置](./img/报告设置.png) | ![推送设置](./img/推送设置.png) | ![全局邮箱通知](./img/全局邮箱通知.png) |

## 适合谁使用

- 需要集中管理多个工学云账号的个人或小团队。
- 需要定时打卡、手动补卡、补交日报 / 周报 / 月报的实习用户。
- 需要通过自托管方式掌控账号、密码、通知和执行记录的管理员。
- 想要用 Docker 快速部署一个工学云自动化后台的运维人员。

## 项目亮点

- **从用户视角出发：** 用户只需要绑定账号、配置地址和时间，就可以在工作台查看执行结果、生成报告、处理缺卡。
- **从管理员视角出发：** 管理员可以批量维护用户、集中查看执行状态、统一配置通知，并为用户处理补卡和补交报告。
- **从部署视角出发：** 项目提供 Docker Compose、`.env.example`、GHCR 镜像发布流程和清晰的本地开发命令。
- **从维护视角出发：** 后端有明确的任务执行链路、运行时回写逻辑和补卡单元测试。

## 功能概览

- **管理端：** 管理用户、查看审计日志、配置通知、批量执行任务、测试 AI 与地理编码能力，支持为指定用户刷新缺卡记录、手动补卡和补交报告。
- **系统设置：** 管理员可在 Web 中统一配置 QQ 邮箱 SMTP 和工学云补卡代理；工学云补卡代理支持动态取 IP 接口和静态代理池。
- **用户端：** 通过 `/u` 入口注册 / 登录、绑定工学云账号、修改个人打卡与报告配置、手动执行任务，支持自助查看待补卡日期、手动补卡和补交日报 / 周报 / 月报。
- **自动调度：** 基于 APScheduler 为每个用户注册上下班打卡和报告任务。
- **补卡能力：** 自动拉取已打卡记录，过滤已完成日期；补卡时先选择上班或下班类型，再选择待补日期，可补选中日期，也可一键补当前类型下全部待补日期，批量补卡在遇到频繁请求时会自动延迟重试并进入冷却降速。
- **报告补交：** 日报、周报、月报分别支持按周期补交，并提供当前类型下一键补全部待补周期的入口。
- **批量执行：** 通过队列 worker 并发处理批量任务，支持暂停、取消与失败重试。
- **运行时同步：** 将工学云登录态、计划信息、执行结果等运行时数据回写到数据库 JSON 字段。

## 更新日志

### 当前版本（2026-05-23）

- **缺卡与补卡能力完成：** 新增打卡记录归一化与缺卡日期筛选；管理端和用户端均支持刷新缺卡、选择补卡类型、选择多个待补日期补卡，以及一键补当前类型下全部待补日期；批量补卡遇到频繁请求时会自动延迟重试并进入冷却降速。
- **报告补交能力完成：** 日报、周报、月报分别支持按周期筛选未提交记录，并可对当前类型一键补全部待补周期。
- **定时任务边界修正：** 普通定时打卡只执行正常打卡，不会自动触发补卡；补卡只通过管理端或用户端手动接口触发。
- **补卡接口对齐：** 学生补卡请求使用 `attendence/attendanceReplace/v4/save`，请求体包含 `attendanceType=REPLACE`，并按补卡类型只提交一条上班或下班记录，不再自动同时补上下班。
- **测试补齐：** 新增打卡记录筛选、补卡请求构造、按类型补卡和批量补卡请求解析的单元测试；后端可通过 `python -m unittest discover -s tests` 执行当前测试集合。
- **管理端能力完成：** 支持后台登录、角色权限、用户管理、用户执行日志、审计日志查询与清空、通知配置、SMTP 测试、AI 测试、地理编码搜索与逆地理解析。
- **用户端能力完成：** 恢复 `/u/login`、`/u/register`、`/u`、`/u/settings` 四个入口，用户端拥有独立认证状态和请求入口，不再与后台管理端登录态混用。
- **用户自助流程完成：** 支持用户注册 / 登录、绑定工学云账号、读取自身配置、自动获取打卡地址、保存打卡与报告配置、手动执行任务、查看执行记录、生成日报和提交日报。
- **自动化任务完成：** 基于 APScheduler 为单个用户注册上班打卡、下班打卡、日报、周报和月报任务，并在配置变更后重建对应 job。
- **批量任务完成：** 管理端可发起批量运行，后端队列 worker 支持并发执行、失败重试、暂停、恢复、取消和进度查询。
- **运行时回写完成：** 抽取统一的执行结果回写逻辑，将执行状态、日志、最近运行时间、远端登录态和实习计划信息同步回用户数据。
- **安全与配置完成：** 支持 MySQL 数据库、JWT 登录态、角色校验、基础限流、敏感字段加密存储，并通过 `.env` 管理生产配置。
- **前端一致性完成：** 统一前端消息提示入口，修正 `createWebHistory()` 模式下的 `401` 未登录跳转，并清理 Vue 模板残留文件。
- **部署与工程维护完成：** 支持本地前后端分离开发、Docker Compose 一体化部署、FastAPI 托管 `web/dist` 静态资源，并补齐 `.gitignore` 以忽略环境变量、运行数据、模型文件、依赖目录和本地工具缓存。
- **镜像发布流程完成：** 新增 GitHub Actions 工作流 `.github/workflows/docker-publish.yml`，支持推送 `main` / `master`、发布 `v*` 标签或手动触发时自动构建 Docker 镜像，并发布到 GitHub Container Registry (GHCR)；配置 Docker Hub 密钥后可同步推送到 Docker Hub。

完整版本记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 技术栈

- **后端：** FastAPI + SQLModel + MySQL
- **前端：** Vue 3 + Vite + Pinia + Vue Router + Element Plus
- **任务调度：** APScheduler
- **部署方式：** 本地前后端分离开发，或 Docker Compose 一体化部署

## 架构说明

### 1. 后端启动流程

`server/main.py` 是后端入口。应用启动时不只会拉起 API，还会执行以下初始化动作：

- 建表并补齐运行时列
- 初始化管理员种子账号
- 检查并下载验证码识别所需的 ONNX 模型
- 启动 APScheduler 定时调度器
- 启动批量任务 queue worker
- 在 `web/dist` 存在时直接托管前端静态资源，并处理 SPA fallback

### 2. 双 API 面

`server/api.py` 同时承载两套接口：

- **管理端 API：** 面向 `admin`、`operator`、`viewer` 等角色
- **用户端 API：** 统一挂在 `/app/*`，面向终端用户

用户端当前已接回以下典型能力：

- `/app/auth/register`
- `/app/auth/login`
- `/app/me`
- `/app/bind`
- `/app/run`
- `/app/execution`
- `/app/clock-in/missing-days`
- `/app/clock-in/makeup`
- `/app/clock-in/makeup-all`
- `/app/reports/daily/generate`
- `/app/reports/daily/submit`

管理端对应提供以下补卡接口：

- `GET /users/{user_id}/clock-in/missing-days`
- `POST /users/{user_id}/clock-in/makeup`
- `POST /users/{user_id}/clock-in/makeup-all`

### 3. 任务执行链路

项目的执行链路集中在以下几个模块：

- `server/user_runtime.py`：负责在数据库模型和任务运行配置之间做桥接
- `server/scheduler.py`：为单用户注册定时打卡 / 报告任务
- `server/queue_worker.py`：执行批量任务队列
- `server/task_runner.py`：真正协调工学云接口、AI 报告生成、图片上传和消息推送
- `server/clockin_backfill.py`：归一化打卡记录，按日期和 `START` / `END` 类型生成待补卡选项

无论是**定时执行**、**批量执行**还是**用户手动执行**，最终都会汇总到 `server/task_runner.py`。

### 4. 前端结构

前端位于 `web/`，主要由两套入口构成：

- **管理端：** `/login` 进入后台，使用 `web/src/stores/auth.js` 和 `web/src/api/http.js`
- **用户端：** `/u/login`、`/u/register`、`/u`、`/u/settings`，使用 `web/src/stores/userAuth.js` 和 `web/src/api/userHttp.js`

`web/src/router/index.js` 负责两套认证流和路由守卫的分流。

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 20+（建议）
- MySQL 8+

## 一键部署

适合直接在服务器或本机启动完整应用。当前 `docker-compose.yml` 默认连接外部 MySQL，因此需要先准备一个可访问的 MySQL 数据库。

### Linux / macOS

```bash
cp .env.example .env

# 修改 .env 中的 DATABASE_URL、APP_SECRET 和管理员密码后启动
docker compose up -d --build
```

### Windows PowerShell

```powershell
Copy-Item .env.example .env

# 修改 .env 中的 DATABASE_URL、APP_SECRET 和管理员密码后启动
docker compose up -d --build
```

启动后访问：

- 应用首页：`http://localhost:8147`
- API 文档：`http://localhost:8147/docs`
- 默认后台账号：由 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 决定

注意：在 Docker 容器内，`127.0.0.1` 指向容器自身。如果 MySQL 运行在宿主机上，`DATABASE_URL` 中的主机名通常应改为宿主机 IP 或 Docker Desktop 的 `host.docker.internal`。

### 真正的一条命令部署

如果你已经有可用的 MySQL，可以直接用环境变量启动：

```bash
DATABASE_URL='mysql+pymysql://user:password@mysql-host:3306/automoguding?charset=utf8mb4' \
APP_SECRET='replace-with-a-long-random-secret' \
ADMIN_USERNAME='admin' \
ADMIN_PASSWORD='change-me' \
docker compose up -d --build
```

Windows PowerShell：

```powershell
$env:DATABASE_URL = 'mysql+pymysql://user:password@mysql-host:3306/automoguding?charset=utf8mb4'
$env:APP_SECRET = 'replace-with-a-long-random-secret'
$env:ADMIN_USERNAME = 'admin'
$env:ADMIN_PASSWORD = 'change-me'
docker compose up -d --build
```

生产环境不要使用默认管理员密码，也不要使用示例 `APP_SECRET`。

## 本地开发

### 1. 准备 `.env`

后端默认从项目根目录读取 `.env`。当前数据库**只支持 MySQL**，必须配置 `DATABASE_URL`。可以从示例文件复制：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

最小示例：

```env
DATABASE_URL=mysql+pymysql://automoguding:automoguding123@127.0.0.1:3306/automoguding?charset=utf8mb4
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123456
SCHEDULER_TIMEZONE=Asia/Shanghai
GEOCODE_SEARCH_PROVIDER=mapchaxun
GEOCODE_PROVIDER=osm
```

可选配置：

```env
APP_SECRET=please-change-me-in-production
SCHEDULER_JITTER_SECONDS=600
SCHEDULER_REPORT_JITTER_SECONDS=0
CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS=2
CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES=3
CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS=10
CLOCKIN_MAKEUP_RATE_LIMIT_COOLDOWN_SECONDS=60
MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS=600
MOGUDING_PROXY_API_URL=
MOGUDING_PROXY_TTL_SECONDS=55
MOGUDING_PROXY_API_TIMEOUT_SECONDS=10
MOGUDING_PROXY_URLS=
REPORT_MAKEUP_BATCH_DELAY_SECONDS=2
BAIDU_MAP_AK=your-baidu-map-ak
BAIDU_MAP_COORD_TYPE=gcj02ll
BAIDU_MAP_INPUT_COORD_TYPE=
BAIDU_MAP_OUTPUT_COORD_TYPE=
AMAP_KEY=your-amap-key
```

说明：

- `DATABASE_URL` 必填，且必须以 `mysql+pymysql://` 开头。
- 生产环境应显式配置 `APP_SECRET`。
- 默认使用 `mapchaxun` 内部接口做地址搜索，无需地图 Key。接口返回经纬度和省市区后，前端会自动回填打卡位置。
- `GEOCODE_SEARCH_PROVIDER` 只控制搜索框，默认 `mapchaxun`；`GEOCODE_PROVIDER` 控制搜索结果需要反查时的逆地理解析，默认 `osm`，也可切换 `baidu` / `amap`。
- 如果逆地理解析或搜索切换到百度 Web 服务，请提供 `BAIDU_MAP_AK`；缺少 AK 时会返回明确错误。`BAIDU_MAP_COORD_TYPE` 默认 `gcj02ll`，也可用 `BAIDU_MAP_INPUT_COORD_TYPE`、`BAIDU_MAP_OUTPUT_COORD_TYPE` 分别控制百度逆地理输入坐标和返回坐标。
- 管理端打卡设置默认内嵌 `https://www.mapchaxun.cn/jingweidu` 作为经纬度核对页；搜索地址仍会通过后端自动回填经纬度、省市区和地址。如需自定义核对页，需要在前端构建前设置 `VITE_MAP_DISPLAY_URL`。
- `CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS` 控制一键补卡的默认间隔。
- `CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES` 和 `CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS` 控制遇到频繁请求时的重试次数与初始等待时间。
- `CLOCKIN_MAKEUP_RATE_LIMIT_COOLDOWN_SECONDS` 控制触发 IP 频繁后的批量冷却间隔。当前日期重试成功后，后续日期会按该间隔降速；如果当前日期重试耗尽仍然频繁，会停止剩余日期，避免继续触发远端风控。
- `MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS` 控制普通网络被工学云返回“IP非法请求过多，已限制访问”后的非代理请求暂停时间，默认 `600` 秒。暂停期间不会继续向工学云发普通网络请求；手动补卡启用代理后仍可获取/切换代理 IP 重试。
- `MOGUDING_PROXY_API_URL` 控制动态代理获取接口。接口返回内容需要包含 `ip:端口`，例如 `1.2.3.4:8080`。如果接口 URL 中包含 `accessName` 和 `accessPassword`，后端会自动拼成 `http://accessName:accessPassword@ip:端口` 使用。
- `MOGUDING_PROXY_TTL_SECONDS` 控制动态代理缓存时长，默认 `55` 秒，适配常见 1 分钟代理有效期。
- `MOGUDING_PROXY_API_TIMEOUT_SECONDS` 控制动态代理接口请求超时。
- `MOGUDING_PROXY_URLS` 控制静态工学云补卡代理池，多个代理用逗号、分号或换行分隔，例如 `http://user:pass@1.2.3.4:8080,http://5.6.7.8:8080`。如果同时配置动态代理接口，会优先使用动态代理接口。代理只在手动补卡执行阶段启用；正常登录、定时打卡、报告提交、缺卡查询、AI、地理编码和通知请求都不使用该代理。
- 工学云补卡代理也可以在管理端 Web 的「系统设置」中全局配置。环境变量中的 `MOGUDING_PROXY_API_URL` / `MOGUDING_PROXY_URLS` 优先级高于 Web 全局配置。
- `REPORT_MAKEUP_BATCH_DELAY_SECONDS` 控制日报 / 周报 / 月报一键补交的批量间隔，未配置时会回退到补卡间隔。

### 2. 启动后端

```bash
pip install -r server/requirements.txt
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147
```

启动后可访问：

- API 文档：`http://localhost:8147/docs`
- OpenAPI：`http://localhost:8147/openapi.json`

### 3. 启动前端

```bash
cd web
npm install
npm run dev
```

启动后访问：

- 前端页面：`http://localhost:5173`

Vite 开发服务器会将 `/api` 代理到本地后端 `http://127.0.0.1:8147`。

### 4. 构建前端

```bash
cd web
npm run build
npm run preview
```

## Docker 部署

项目支持通过 Docker Compose 一体化启动。默认 `docker-compose.yml` 用于本地构建：

```bash
docker compose up -d --build
```

当前 `docker-compose.yml` 会：

- 构建应用镜像
- 暴露 `8147` 端口
- 将 `./data/images` 挂载到容器内图片目录
- 通过环境变量把 `DATABASE_URL`、调度时区、管理员账号等配置注入应用

访问入口：

- 应用首页：`http://localhost:8147`
- API 文档：`http://localhost:8147/docs`

如果要使用 GitHub Actions 已发布的远端镜像，请使用 `docker-compose.image.yml`。这种方式会从镜像仓库拉取新镜像，而不是用服务器本地源码重新构建。建议在 `.env` 中指定镜像和数据库配置：

```env
APP_IMAGE=ghcr.io/<owner>/<repo>:20260522
DATABASE_URL=mysql+pymysql://user:password@mysql-host:3306/automoguding?charset=utf8mb4
APP_SECRET=replace-with-a-long-random-secret
```

启动或更新：

```bash
docker compose -f docker-compose.image.yml up -d
```

`docker-compose.image.yml` 已配置 `pull_policy: always`，使用 `20260522` 这类日期标签时，重新执行 `up -d` 会主动检查并拉取远端同标签的新镜像。也可以把 `APP_IMAGE` 指定为 `20260522-153045` 或 `sha-abcdef0` 来锁定某一次构建。

Docker 本地已经拉取的镜像不会主动显示远端同名标签是否更新；如果镜像仓库里的 `20260522` 被新构建覆盖，本地仍需要重新执行 `up -d` 或 `docker compose -f docker-compose.image.yml pull` 才会对比远端 digest。想在镜像列表中直接看到新增版本，请使用 `YYYYMMDD-HHMMSS` 标签。

Windows PowerShell 可以用仓库内脚本检查并拉取同标签更新：

```powershell
.\scripts\check-image-update.ps1 -Image ghcr.io/<owner>/<repo>:20260522
```

### GitHub 自动构建镜像

仓库已提供 `.github/workflows/docker-publish.yml`，用于在 GitHub Actions 中自动构建并发布镜像到 GHCR，也可以同步推送到 Docker Hub。

触发方式：

- 推送到 `main` 或 `master` 分支：构建并推送分支镜像，同时默认分支会更新 `latest` 标签。
- 推送 `v*` 标签：构建并推送对应版本镜像，例如 `v1.0.0`。
- 每次非 PR 构建都会额外推送日期标签，例如 `20260522` 和 `20260522-153045`。`YYYYMMDD` 表示当天最新构建，`YYYYMMDD-HHMMSS` 表示一次唯一构建，便于在镜像仓库页面直接看到更新时间和新增版本。
- 每次构建都会推送 `sha-<commit>` 标签，便于精确回滚到某次提交。
- Pull Request：只执行构建验证，不推送镜像。
- 手动触发：可在 GitHub Actions 页面运行 `Docker Publish` 工作流。

镜像地址格式：

```text
ghcr.io/<owner>/<repo>:latest
ghcr.io/<owner>/<repo>:20260522
ghcr.io/<owner>/<repo>:20260522-153045
ghcr.io/<owner>/<repo>:sha-abcdef0
```

注意：`latest`、分支标签和纯日期标签会被后续构建覆盖，已运行的容器不会自动更新。服务器需要重新执行 `docker compose -f docker-compose.image.yml up -d`，或者直接指定新的 `YYYYMMDD-HHMMSS` / `sha-*` 标签。

Docker Hub 同步发布需要在 GitHub 仓库的 `Settings` → `Secrets and variables` → `Actions` 中配置以下 Secrets：

```text
DOCKERHUB_USERNAME=你的 Docker Hub 用户名
DOCKERHUB_TOKEN=你的 Docker Hub Access Token
DOCKERHUB_REPOSITORY=你的 Docker Hub 镜像名，可选，例如 username/automoguding-saas
```

如果不配置 `DOCKERHUB_REPOSITORY`，工作流会默认使用 `DOCKERHUB_USERNAME/<repo>` 作为 Docker Hub 镜像名。

如果仓库是私有仓库，需要在 GitHub 的 Package 设置中确认 GHCR 镜像可见性和拉取权限。GHCR 发布默认使用 GitHub 自动注入的 `GITHUB_TOKEN`。

## Release

当前推荐发布方式：

- 使用 Git tag 管理版本，例如 `v0.1.0`。
- 推送 `v*` 标签后，GitHub Actions 会构建并发布 Docker 镜像。
- 版本说明维护在 [CHANGELOG.md](./CHANGELOG.md)。

示例：

```bash
git tag v0.1.0
git push origin v0.1.0
```

发布前建议至少运行：

```bash
python -m unittest discover -s tests
python -m compileall server
cd web
npm run build
```

## 文档索引

- [当前功能与接口速查](./docs/current-features.md)：当前实现中的功能范围、补卡规则、补卡 API 和验证命令。
- [后端说明](./server/README.md)：后端启动流程、模块职责、API 面、补卡执行链路和验证命令。
- [前端说明](./web/README.md)：前端入口、目录结构、补卡界面和构建说明。
- [Roadmap](./ROADMAP.md)：后续计划和优先级。
- [贡献指南](./CONTRIBUTING.md)：提交 Issue、PR 和本地验证方式。
- [更新日志](./CHANGELOG.md)：版本发布记录。
- `docs/superpowers/`：历史规格和实现计划，用于追溯设计过程，不一定代表最新实现。

## 常用入口

### 管理端

- 登录页：`/login`
- 默认首页：`/`

### 用户端

- 登录页：`/u/login`
- 注册页：`/u/register`
- 用户工作台：`/u`
- 用户设置页：`/u/settings`

## 打卡与补卡

### 自动打卡

用户的自动打卡配置位于管理端用户编辑页或用户端设置页。核心配置包括：

- 上班时间和下班时间
- 打卡周期
- 打卡天数
- 打卡地址、经纬度、省市区
- 打卡图片数量和备注

保存配置后，后端会重建该用户的调度任务。定时任务、管理端批量执行和用户端手动执行都会复用 `server/task_runner.py` 中的同一套打卡逻辑。

### 缺卡记录

系统通过工学云打卡记录接口读取指定周期内的打卡数据，并在本地做归一化处理：

- `START` 表示上班打卡。
- `END` 表示下班打卡。
- 同一天如果上班和下班都存在，则不会出现在待补列表中。
- 同一天如果只缺一种类型，则只会在对应类型的待补列表中出现。
- 待补日期会按当前配置的打卡周期过滤，避免展示非打卡日。

管理端和用户端都会展示「已获取记录数」「当前类型待补天数」「已选择天数」。

### 手动补卡

补卡流程分为两步：

1. 选择补卡类型：`上班` 或 `下班`。
2. 选择一个或多个待补日期，点击「补选中」；或点击「全部待补」补当前类型下所有待补日期。

补卡只提交用户当前选择的类型。例如选择「上班」时，即使某天同时缺上班和下班，也只补上班，不会自动补下班。

批量补卡在遇到远端“请求过于频繁”、`429`、`rate limit` 或“IP非法请求过多，已限制访问”时，会等待后重试当前日期。当前日期重试成功后，后续日期会进入冷却间隔；如果当前日期重试耗尽仍然频繁，系统会停止剩余日期并把它们标记为跳过，避免继续触发远端风控。

手动补卡时，如果配置了 `MOGUDING_PROXY_API_URL`，补卡请求会先调用该接口获取代理。接口返回 `ip:端口` 后，后端会读取接口 URL 查询参数中的 `accessName` 和 `accessPassword`，并拼成 `http://accessName:accessPassword@ip:端口` 使用。例如：

```env
MOGUDING_PROXY_API_URL=http://capi.51daili.com/traffic/getip?linePoolIndex=1&packid=12&time=2&qty=1&port=1&format=txt&dt=2&ct=1&dtc=5&usertype=17&uid=54638&accessName=your-name&accessPassword=your-pass
MOGUDING_PROXY_TTL_SECONDS=55
```

动态代理默认缓存 `55` 秒。补卡请求遇到 IP 频繁、IP 被限制访问或当前代理连接失败时，后端会重新调用代理接口获取新 IP 再重试；补卡结果会记录 `代理切换次数`，便于判断是否确实发生了代理切换。也可以继续使用 `MOGUDING_PROXY_URLS` 配置静态代理池。该代理不会用于正常定时打卡、报告提交、缺卡查询或登录。

普通登录、定时打卡、报告提交、缺卡查询等非补卡请求如果命中“IP非法请求过多，已限制访问”，会进入普通网络熔断窗口并停止后续工学云请求，避免在已封 IP 的情况下继续扩大限制。

管理员也可以在管理端进入「系统设置」→「工学云代理」保存全局补卡代理配置。环境变量里显式配置的代理优先于 Web 全局配置。

学生补卡请求会走工学云补卡接口：

```text
attendence/attendanceReplace/v4/save
```

补卡请求的关键字段：

| 字段 | 说明 |
|------|------|
| `type` | 补卡类型，`START` 或 `END` |
| `createTime` | 目标补卡日期和对应上班 / 下班时间 |
| `attendanceType` | 固定为 `REPLACE` |
| `attendenceTime` | 补卡时保持为空 |
| `isReplace` | 补卡时保持为空 |
| `planId` | 当前实习计划 ID |
| `userId` | 当前工学云用户 ID |

### 补卡 API

用户端接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/app/clock-in/missing-days` | 获取当前用户缺卡日期和缺卡类型 |
| `POST` | `/app/clock-in/makeup` | 补选中的日期 |
| `POST` | `/app/clock-in/makeup-all` | 补当前类型下全部待补日期 |

管理端接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/users/{user_id}/clock-in/missing-days` | 获取指定用户缺卡日期和缺卡类型 |
| `POST` | `/users/{user_id}/clock-in/makeup` | 为指定用户补选中的日期 |
| `POST` | `/users/{user_id}/clock-in/makeup-all` | 为指定用户补当前类型下全部待补日期 |

`/makeup` 请求示例：

```json
{
  "target_dates": ["2026-05-06", "2026-05-07"],
  "target_type": "START"
}
```

`target_type` 只能是：

- `START`：上班
- `END`：下班

为了兼容旧调用，`/makeup` 仍支持单日期字段：

```json
{
  "target_date": "2026-05-06",
  "target_type": "END"
}
```

## 目录结构

```text
automoguding-saas/
├─ server/                  # FastAPI 后端、调度器、任务执行与数据模型
│  ├─ clockin_backfill.py   # 打卡记录归一化与缺卡日期筛选
│  ├─ task_runner.py        # 打卡、补卡、报告提交等任务执行入口
│  └─ coreApi/              # 工学云接口客户端
├─ web/                     # Vue 3 前端
├─ docs/                    # 当前功能说明、历史设计和实现计划
├─ tests/                   # 后端 unittest 测试
├─ docker-compose.yml       # 容器编排配置
├─ Dockerfile               # 前端构建 + 后端运行镜像
└─ CLAUDE.md                # 面向 Claude Code 的仓库工作说明
```

## 关键配置与运行行为

- 应用启动时会自动建表、补列、种子管理员、检查模型、启动调度器和队列线程，因此排查启动问题时要把这些副作用一起考虑。
- 用户配置变更后，接口层通常会先移除旧 job，再按新配置重新注册 job。
- 容器部署时由 FastAPI 直接提供构建后的 `web/dist`；本地开发则通常使用 `5173` 前端 + `8147` 后端。
- 工学云账号密码、SMTP 密码等敏感信息为加密存储。
- `/ai/test` 默认只允许公网 HTTPS 地址，不允许本机或内网地址。

## 社区与反馈

如果你在使用中遇到问题，优先提供以下信息，便于复现：

- 部署方式：Docker Compose 或本地开发。
- 后端版本或 Git commit。
- Python、Node.js、MySQL 版本。
- 相关接口路径、错误提示和后端日志。
- 是否可以稳定复现，以及复现步骤。

建议入口：

- Bug 反馈：使用 GitHub Issue 的 Bug 模板。
- 功能建议：使用 Feature Request 模板。
- 后续计划：查看 [ROADMAP.md](./ROADMAP.md)。
- 参与开发：阅读 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 开发说明

### 当前可用命令

后端：

```bash
pip install -r server/requirements.txt
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147
```

前端：

```bash
cd web
npm install
npm run dev
npm run build
npm run preview
```

### 关于 lint / test

当前仓库已经有后端单元测试，但还没有统一的 lint 脚本，也没有前端测试脚本。常用验证命令如下：

```bash
python -m unittest discover -s tests
python -m compileall server

cd web
npm run build
```

说明：

- `web/package.json` 仅提供 `dev`、`build`、`preview` 三个脚本。
- 不要假设仓库内已经存在 `npm test`、`npm run lint` 等命令。
- `git diff --check` 可用于检查空白字符问题；在 Windows 环境下可能出现 LF / CRLF 换行提示，通常不是语法错误。
