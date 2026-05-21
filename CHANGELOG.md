# 更新日志

本文档记录 AutoMoGuDing SaaS 的重要变更。版本号建议遵循 `v主版本.次版本.修订号` 格式。

## Unreleased

- 文档：补充 Demo 截图、部署说明、Release 流程、Roadmap 和贡献入口。
- 文档：新增后端说明、前端说明和当前功能速查。

## 2026-05-22

### 新增

- 新增缺卡记录筛选能力，支持从已打卡记录中识别待补日期。
- 新增按类型补卡能力：补卡前选择 `上班` 或 `下班`，一次只补一种类型。
- 新增「补选中」和「全部待补」两种补卡操作。
- 新增管理端补卡接口：
  - `GET /users/{user_id}/clock-in/missing-days`
  - `POST /users/{user_id}/clock-in/makeup`
  - `POST /users/{user_id}/clock-in/makeup-all`
- 新增用户端补卡接口：
  - `GET /app/clock-in/missing-days`
  - `POST /app/clock-in/makeup`
  - `POST /app/clock-in/makeup-all`
- 新增后端单元测试，覆盖打卡记录归一化、补卡请求构造、按类型补卡和批量补卡请求解析。

### 变更

- 学生补卡请求改为调用 `attendence/attendanceReplace/v4/save`。
- 补卡请求体使用 `attendanceType=REPLACE`，`attendenceTime=null`，`isReplace=null`。
- 管理端用户编辑页和用户端设置页的补卡日期改为多选，并按当前补卡类型过滤。
- README 更新到当前实现，补充补卡规则和验证命令。

### 验证

- `python -m unittest discover -s tests`
- `python -m compileall server`
- `npm run build`（在 `web/` 目录）
- `git diff --check`

## 2026-05-15

### 新增

- 管理端能力：后台登录、角色权限、用户管理、用户执行日志、审计日志、通知配置、SMTP 测试、AI 测试、地理编码搜索与逆地理解析。
- 用户端能力：`/u/login`、`/u/register`、`/u`、`/u/settings`。
- 用户自助流程：注册 / 登录、绑定工学云账号、读取自身配置、自动获取打卡地址、保存打卡与报告配置、手动执行任务、查看执行记录、生成日报和提交日报。
- 自动化任务：基于 APScheduler 注册上班打卡、下班打卡、日报、周报和月报任务。
- 批量任务：支持并发执行、失败重试、暂停、恢复、取消和进度查询。
- GitHub Actions 镜像构建流程：支持发布到 GHCR，可选同步到 Docker Hub。

### 变更

- 统一执行结果回写逻辑，将执行状态、日志、最近运行时间、远端登录态和实习计划信息同步回用户数据。
- 前端统一消息提示入口，修正 `createWebHistory()` 模式下的 `401` 未登录跳转。
- 支持本地前后端分离开发和 Docker Compose 一体化部署。
