# AutoMoGuDing SaaS 后端

`server/` 是 AutoMoGuDing SaaS 的 FastAPI 后端，负责管理端 API、用户端 API、工学云接口调用、定时调度、批量任务队列、补卡、报告提交和运行时数据回写。

## 技术栈

- FastAPI
- SQLModel
- MySQL
- APScheduler
- Requests
- ONNX Runtime

## 启动命令

从项目根目录启动：

```bash
pip install -r server/requirements.txt
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147
```

启动后访问：

- API 文档：`http://localhost:8147/docs`
- OpenAPI：`http://localhost:8147/openapi.json`

## 环境变量

后端默认读取项目根目录 `.env`。

必填：

```env
DATABASE_URL=mysql+pymysql://user:password@127.0.0.1:3306/automoguding?charset=utf8mb4
```

常用可选项：

```env
APP_SECRET=please-change-me-in-production
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123456
SCHEDULER_TIMEZONE=Asia/Shanghai
SCHEDULER_JITTER_SECONDS=600
SCHEDULER_REPORT_JITTER_SECONDS=0
GEOCODE_PROVIDER=osm
AMAP_KEY=your-amap-key
```

说明：

- `DATABASE_URL` 必须使用 MySQL，且必须以 `mysql+pymysql://` 开头。
- 生产环境必须显式配置 `APP_SECRET`。
- `GEOCODE_PROVIDER=amap` 时需要提供 `AMAP_KEY`。
- Docker 容器内的 `127.0.0.1` 指向容器自身。容器连接宿主机 MySQL 时，请使用宿主机 IP 或 `host.docker.internal`。

## 启动流程

`server/main.py` 在启动时会执行以下动作：

1. 加载 `.env`。
2. 创建数据库连接。
3. 建表并补齐运行时字段。
4. 初始化管理员种子账号。
5. 检查验证码识别所需 ONNX 模型。
6. 启动 APScheduler。
7. 启动批量任务 queue worker。
8. 如果 `web/dist` 存在，则托管前端静态资源并提供 SPA fallback。

因此排查启动问题时，不要只看 API 是否启动，也要关注数据库连接、模型文件、调度线程和队列线程。

## 目录说明

```text
server/
├─ api.py                   # 管理端和用户端 API
├─ auth.py                  # Token 签发、校验和角色权限
├─ clockin_backfill.py      # 打卡记录归一化和待补卡日期筛选
├─ coreApi/
│  ├─ MainLogicApi.py       # 工学云接口客户端
│  └─ AiServiceClient.py    # AI 报告生成客户端
├─ database.py              # 数据库连接、建表和补列
├─ models.py                # SQLModel 数据模型
├─ queue_worker.py          # 批量任务队列
├─ scheduler.py             # 用户定时任务注册
├─ task_runner.py           # 打卡、补卡、报告提交等任务执行
├─ user_runtime.py          # User 模型与运行配置之间的桥接
└─ util/                    # 加密、验证码、消息推送、配置工具
```

## API 面

后端同时承载两套 API。

### 管理端

管理端接口面向 `admin`、`operator`、`viewer` 等角色，负责：

- 用户管理
- 批量执行
- 审计日志
- 通知配置
- AI 测试
- 地理编码
- 缺卡查询和补卡
- 报告生成和提交

补卡相关接口：

```http
GET /users/{user_id}/clock-in/missing-days
POST /users/{user_id}/clock-in/makeup
POST /users/{user_id}/clock-in/makeup-all
```

### 用户端

用户端接口统一挂在 `/app/*`，面向终端用户，负责：

- 注册 / 登录
- 绑定工学云账号
- 读取和保存自身配置
- 手动执行任务
- 查看执行记录
- 缺卡查询和补卡
- 日报、周报、月报生成和提交

补卡相关接口：

```http
GET /app/clock-in/missing-days
POST /app/clock-in/makeup
POST /app/clock-in/makeup-all
```

## 补卡执行链路

补卡相关代码分布：

- `server/api.py`：接收 `target_dates` 和 `target_type`，校验参数并写审计日志。
- `server/clockin_backfill.py`：归一化远端打卡记录，生成缺卡日期选项。
- `server/task_runner.py`：根据日期、类型和配置时间执行补卡。
- `server/coreApi/MainLogicApi.py`：构造工学云补卡请求并调用远端接口。

补卡请求只补一种类型：

- `target_type=START`：只补上班。
- `target_type=END`：只补下班。

即使某一天同时缺上班和下班，选择 `START` 时也只补上班；选择 `END` 时只补下班。

学生补卡使用工学云接口：

```text
attendence/attendanceReplace/v4/save
```

补卡请求关键字段：

| 字段 | 值 |
|------|----|
| `attendanceType` | `REPLACE` |
| `type` | `START` 或 `END` |
| `createTime` | 目标日期 + 上班 / 下班配置时间 |
| `attendenceTime` | `null` |
| `isReplace` | `null` |

## 任务执行链路

执行入口主要有 3 类：

- 定时任务：`scheduler.py` 注册后由 APScheduler 触发。
- 批量任务：`queue_worker.py` 从队列中取任务执行。
- 手动任务：管理端或用户端 API 直接触发。

最终都会进入 `server/task_runner.py`：

- `perform_clock_in`：普通打卡。
- `perform_clock_in_makeup`：补单个日期的一种类型。
- `perform_clock_in_makeup_many`：补多个日期的一种类型。
- `submit_daily_report`：日报提交。
- `submit_weekly_report`：周报提交。
- `submit_monthly_report`：月报提交。

执行结果会通过 `server/user_runtime.py` 回写到用户记录，包括最近运行时间、状态、日志、登录态和计划信息。

## 测试与验证

当前后端单元测试使用 Python 标准库 `unittest`。

```bash
python -m unittest discover -s tests
```

语法编译检查：

```bash
python -m compileall server
```

空白字符检查：

```bash
git diff --check
```

当前项目没有统一的 lint 脚本，也没有前端测试脚本。前端构建验证见 `web/README.md`。
