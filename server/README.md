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
GEOCODE_SEARCH_PROVIDER=mapchaxun
GEOCODE_PROVIDER=osm
BAIDU_MAP_AK=your-baidu-map-ak
BAIDU_MAP_COORD_TYPE=gcj02ll
BAIDU_MAP_INPUT_COORD_TYPE=
BAIDU_MAP_OUTPUT_COORD_TYPE=
AMAP_KEY=your-amap-key
```

说明：

- `DATABASE_URL` 必须使用 MySQL，且必须以 `mysql+pymysql://` 开头。
- 生产环境必须显式配置 `APP_SECRET`。
- `GEOCODE_SEARCH_PROVIDER=mapchaxun` 为默认地址搜索服务，无需地图 Key，只影响搜索框。
- `GEOCODE_PROVIDER` 控制搜索结果需要反查时的逆地理解析，默认 `osm`，可切换 `baidu` / `amap`。
- 使用百度 Web 服务时需要提供 `BAIDU_MAP_AK`；缺少 AK 时会返回明确错误。`BAIDU_MAP_COORD_TYPE` 默认 `gcj02ll`，也可用 `BAIDU_MAP_INPUT_COORD_TYPE`、`BAIDU_MAP_OUTPUT_COORD_TYPE` 分别控制百度逆地理输入坐标和返回坐标。
- `CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS` 控制一键补卡每个日期之间的默认间隔。
- `CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES` 控制补卡遇到频繁请求时的最大重试次数。
- `CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS` 控制补卡频繁请求重试的初始等待秒数，后续会递增退避。
- `CLOCKIN_MAKEUP_RATE_LIMIT_COOLDOWN_SECONDS` 控制触发 IP 频繁后的批量冷却间隔。当前日期重试成功后，后续日期会按该间隔降速；如果当前日期重试耗尽仍然频繁，会停止剩余日期。
- `MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS` 控制普通网络被工学云返回“IP非法请求过多，已限制访问”后的非代理请求暂停时间，默认 `600` 秒。暂停期间不会继续向工学云发起普通网络请求；手动补卡启用代理后仍可获取/切换代理 IP 重试。
- `MOGUDING_PROXY_API_URL` 控制动态代理获取接口。接口响应需要包含 `ip:端口`，后端会从接口 URL 查询参数读取 `accessName` 和 `accessPassword`，并拼接成 `http://accessName:accessPassword@ip:端口`。
- `MOGUDING_PROXY_TTL_SECONDS` 控制动态代理缓存时长，默认 `55` 秒。
- `MOGUDING_PROXY_API_TIMEOUT_SECONDS` 控制动态代理接口请求超时。
- `MOGUDING_PROXY_URLS` 控制静态工学云补卡代理池，多个代理用逗号、分号或换行分隔。如果同时配置动态代理接口，会优先使用动态代理接口。代理只在手动补卡执行阶段启用；正常登录、定时打卡、报告提交和缺卡查询不会使用该代理。
- 补卡代理也可以在管理端 Web 的「系统设置」中保存为全局配置。环境变量中的代理配置优先级高于 Web 全局配置。
- `REPORT_MAKEUP_BATCH_DELAY_SECONDS` 控制日报 / 周报 / 月报一键补交的批量间隔，未配置时会回退到补卡间隔。
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
- 审计日志查询和清空
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
- 日报、周报、月报生成和提交，以及当前类型下一键补全部待补周期

补卡相关接口：

```http
GET /app/clock-in/missing-days
POST /app/clock-in/makeup
POST /app/clock-in/makeup-all
```

报告补交接口：

```http
GET /app/reports/{report_key}/missing-periods
POST /app/reports/{report_key}/generate
POST /app/reports/{report_key}/submit
POST /app/reports/{report_key}/makeup-all
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

批量补卡默认按 `CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS` 间隔逐个日期执行。如果远端返回“请求过于频繁”、`429`、`rate limit` 或“IP非法请求过多，已限制访问”，后端会等待后重试当前日期，而不是直接进入下一个日期。重试次数和初始等待时间由 `CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES`、`CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS` 控制。

触发 IP 频繁后，批量任务会进入冷却策略：

- 当前日期按指数退避重试。
- 当前日期重试成功后，后续日期之间的间隔提升到 `CLOCKIN_MAKEUP_RATE_LIMIT_COOLDOWN_SECONDS`。
- 当前日期重试耗尽仍然频繁时，后续日期会被标记为跳过并停止继续请求，避免扩大远端限流。

只有手动补卡会启用工学云代理。正常登录、缺卡查询、定时打卡和报告提交会继续直连工学云，不会使用这里配置的代理。

如果配置了 `MOGUDING_PROXY_API_URL`，补卡请求会在提交前获取动态代理。动态代理默认缓存 `MOGUDING_PROXY_TTL_SECONDS` 秒；遇到 IP 频繁或代理连接失败时，会重新调用代理接口获取新 IP 后继续重试。补卡批量结果会记录 `代理切换次数`。

普通登录、定时打卡、报告提交、缺卡查询等非补卡请求如果命中“IP非法请求过多，已限制访问”，会进入普通网络熔断窗口并停止后续工学云请求，避免在已封 IP 的情况下继续扩大限制。

动态代理接口示例：

```env
MOGUDING_PROXY_API_URL=http://capi.51daili.com/traffic/getip?linePoolIndex=1&packid=12&time=2&qty=1&port=1&format=txt&dt=2&ct=1&dtc=5&usertype=17&uid=54638&accessName=your-name&accessPassword=your-pass
```

接口返回 `1.2.3.4:8080` 时，实际使用的代理会变成 `http://your-name:your-pass@1.2.3.4:8080`。也可以使用 `MOGUDING_PROXY_URLS` 配置静态代理池。

管理端 Web 提供「系统设置」→「工学云代理」入口，可配置补卡代理的启用状态、动态代理接口、缓存秒数、接口超时秒数和静态代理列表。

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

其中补卡只属于手动任务，不会被普通定时打卡任务自动触发。定时打卡只执行 `perform_clock_in`，报告定时任务只执行对应的日报、周报或月报提交。

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
