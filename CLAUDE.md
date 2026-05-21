# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoMoGuDing SaaS（工学云打卡管理平台）是一个面向多用户托管的 Web 系统，支持自动打卡、缺卡记录筛选、按类型补卡、日报 / 周报 / 月报提交，以及 AI 生成报告内容。代码与注释以中文为主。

核心技术栈：

- 后端：FastAPI + SQLModel + MySQL
- 前端：Vue 3 + Vite + Element Plus
- 运行形态：本地前后端分离开发，Docker 下由 FastAPI 直接提供构建后的前端静态文件

## Development Commands

### Backend

后端从项目根目录 `.env` 读取配置，核心数据库配置是 `DATABASE_URL`，且当前只支持 MySQL。

```bash
pip install -r server/requirements.txt
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147
```

API 文档：`http://localhost:8147/docs`

### Frontend

```bash
cd web
npm install
npm run dev
npm run build
npm run preview
```

Vite 开发服务器默认在 `5173`，并将 `/api` 代理到本地后端 `8147`。

### Docker

```bash
docker compose up -d --build
```

### Lint / Test

当前仓库已有后端单元测试，但没有标准化 lint 脚本，也没有前端测试脚本。

```bash
python -m unittest discover -s tests
python -m compileall server

cd web
npm run build
```

更新仓库说明时不要补写不存在的 `npm test`、`npm run lint` 等命令。

## Architecture

### Backend startup and execution flow

- `server/main.py` 在启动时负责建表、补运行时列、种子管理员账号、确保验证码 ONNX 模型存在、启动 APScheduler 和批处理 queue worker。
- 同一入口还负责 CORS、安全头，以及 `web/dist` 存在时的静态前端与 SPA fallback。
- `server/database.py` 强制使用 `DATABASE_URL` 创建 MySQL 连接，并在启动时补齐 `userInfo` / `planInfo` 等运行时列。
- `server/auth.py` 实现基于 HMAC 的 token 签发与校验，以及 `admin` / `operator` / `viewer` / `user` 角色检查。

### Dual API surfaces

- `server/api.py` 同时承载管理端 API 与 `/app/*` 用户端 API。
- 管理端 API 面向管理员、操作员、只读角色，负责用户管理、批量执行、审计日志、通知设置、AI 测试、地理编码、缺卡查询和补卡等能力。
- `/app/*` 用户端 API 面向终端用户，负责注册 / 登录、绑定工学云账号、修改自身配置、手动执行任务、缺卡查询、补卡，以及生成和提交日报。

补卡相关接口：

- `GET /app/clock-in/missing-days`
- `POST /app/clock-in/makeup`
- `POST /app/clock-in/makeup-all`
- `GET /users/{user_id}/clock-in/missing-days`
- `POST /users/{user_id}/clock-in/makeup`
- `POST /users/{user_id}/clock-in/makeup-all`

### Runtime bridge and task execution

- `server/models.py` 将用户配置、报告设置、AI 设置、通知设置、执行结果等大量数据存储在 JSON 字段中。
- `server/user_runtime.py` 是持久化模型与执行配置之间的桥接层：它把 `User` 转成任务运行所需的配置，同时把远端登录态、实习计划、通知配置等运行时信息同步回数据库。
- `server/scheduler.py` 为单用户注册定时打卡 / 报告任务；`server/queue_worker.py` 负责批量任务队列，支持并发、暂停、取消和重试回退。
- 定时执行、批量执行和手动执行最终都会进入 `server/task_runner.py`，由它协调工学云接口、AI 报告生成、图片上传和消息推送。
- `server/clockin_backfill.py` 负责打卡记录归一化和待补卡日期筛选。补卡以 `target_type` 区分 `START` / `END`，一次只补一种类型；即使某天同时缺上班和下班，也不会在选择一种类型时自动补另一种。

### Frontend structure

- 前端使用 Vue 3、Pinia、Vue Router、Element Plus。
- `web/src/stores/auth.js` 负责在 `localStorage` 中持久化 token、用户名和角色。
- `web/src/router/index.js` 负责登录跳转与角色路由守卫。
- `web/src/api/http.js` 统一注入 Bearer Token，并在 `401` 时清空登录态。
- `web/vite.config.js` 将 `/api` 代理到本地后端 `8147`，构建时按 `vue` / `leaflet` / `axios` 拆分 chunk。

## Configuration and Runtime Behavior

- 后端默认从项目根目录 `.env` 读取配置；`DATABASE_URL` 必须是 `mysql+pymysql://...`。
- 生产环境必须显式配置 `APP_SECRET`；开发环境未配置时会临时生成。
- 常用运行时开关包括 `ADMIN_USERNAME`、`ADMIN_PASSWORD`、`SCHEDULER_TIMEZONE`、`SCHEDULER_JITTER_SECONDS`、`SCHEDULER_REPORT_JITTER_SECONDS`、`GEOCODE_PROVIDER` 和 `AMAP_KEY`。
- 启动应用不只是“起一个 API”：还会自动建表、补列、种子管理员、检查模型文件、启动调度器和队列线程。排查启动问题时要把这些副作用一起考虑。
- 用户创建、更新、绑定后，接口层通常会先移除旧 job，再按新配置重新注册 job。修改排班、报告或启停逻辑时，要把调度重建视为功能的一部分。
- 工学云账号密码、SMTP 密码等敏感信息是加密存储的；`/ai/test` 默认只允许公网 HTTPS 地址，不允许本机或内网地址。
- 本地开发通常是前端 `5173` + 后端 `8147`；容器部署时由 FastAPI 直接提供构建后的 `web/dist`。
