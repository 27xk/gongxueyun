# AutoMoGuDing SaaS 后端

`server/` 是 AutoMoGuDing SaaS 的 FastAPI 后端，负责管理端 API、用户端 API、工学云接口调用、支付宝安全验证后继续打卡、定时调度、批量任务队列、补卡、报告提交、审计、指标和运行时数据回写。

## 后端速览

| 维度 | 内容 |
|------|------|
| Web 框架 | FastAPI |
| ORM / 模型 | SQLModel |
| 数据库 | MySQL + PyMySQL |
| 迁移 | Alembic |
| 调度 | APScheduler |
| 队列 | 数据库批量任务队列 + worker |
| HTTP 客户端 | Requests / HTTPX 测试客户端 |
| 验证码模型 | ONNX Runtime |
| 认证 | HttpOnly Cookie、CSRF、JWT、权限点 |
| 观测 | 审计日志、任务事件、HTTP 指标、Prometheus 文本指标 |

## 启动命令

| 场景 | 命令 | 说明 |
|------|------|------|
| 安装依赖 | `pip install -r server/requirements.txt` | 建议在虚拟环境执行 |
| 数据库迁移 | `python -m alembic upgrade head` | 生产发布前必须执行 |
| 本地后端 | `python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147` | 默认读取项目根目录 `.env` |
| API 文档 | `http://localhost:8147/docs` | 本地默认开启，生产需 `EXPOSE_API_DOCS=true` |
| OpenAPI | `http://localhost:8147/openapi.json` | 本地默认开启，生产需 `EXPOSE_API_DOCS=true` |

## 环境变量

后端默认读取项目根目录 `.env`。建议从 `.env.example` 复制后修改，启动前至少确认数据库、应用密钥、敏感字段加密密钥和管理员账号。

| 配置 | 要求 | 说明 |
|------|------|------|
| `DATABASE_URL` | 必填 | 指向 MySQL，必须以 `mysql+pymysql://` 开头 |
| `APP_SECRET` | 必填 | Token / Cookie 签名密钥，禁止默认值和短密钥 |
| `USER_PASSWORD_KEY` / `FERNET_KEY` | 生产必填 | 加密工学云密码、SMTP 授权码和代理密钥 |
| `ADMIN_USERNAME` | 必填 | 种子管理员账号 |
| `ADMIN_PASSWORD` | 必填 | 生产必须使用强密码 |

### 关键配置表

| 分组 | 变量 | 默认 / 示例 | 说明 |
|------|------|-------------|------|
| 数据库 | `DATABASE_URL` | 必填 | 必须以 `mysql+pymysql://` 开头 |
| 数据库 | `DATABASE_POOL_SIZE` | `10` | MySQL 连接池基础连接数 |
| 数据库 | `DATABASE_MAX_OVERFLOW` | `20` | 连接池溢出连接数 |
| 数据库 | `DATABASE_POOL_RECYCLE_SECONDS` | `1800` | 回收 MySQL 空闲连接 |
| 数据库 | `DATABASE_POOL_TIMEOUT_SECONDS` | `30` | 获取连接超时 |
| 迁移 | `ALLOW_RUNTIME_SCHEMA_MIGRATIONS` | 生产 `false` | 是否允许应用启动时自动建表、补列、建索引 |
| 进程 | `APP_ROLE` | 本地 `all` | `api` 只提供 Web / API；`worker` 只跑调度和队列 |
| 管理员 | `ADMIN_USERNAME` | `admin` | 种子管理员账号 |
| 管理员 | `ADMIN_PASSWORD` | 必填 | 生产不允许默认弱密码 |
| 认证 | `APP_SECRET` | 必填 | Token / Cookie 签名密钥 |
| 认证 | `AUTH_COOKIE_SECURE` | 生产 `true` | HTTPS 环境保持开启 |
| 认证 | `RETURN_AUTH_TOKEN` | `false` | 仅外部脚本兼容需要开启 |
| 认证 | `ALLOW_LEGACY_TOKENS` | `false` | 旧版无版本 token 短期迁移开关 |
| 用户端 | `APP_REGISTRATION_ENABLED` | 生产 `false` | 是否开放自助注册 |
| 用户端 | `USER_PASSWORD_MIN_LENGTH` | `10` | 用户端密码最小长度 |
| 密钥存储 | `USER_PASSWORD_KEY` / `FERNET_KEY` | 生产必填 | 加密工学云密码、SMTP 授权码和代理密钥 |
| CORS | `FRONTEND_ORIGINS` | 空 | 跨域前端 Origin 白名单 |
| CORS | `ALLOW_WILDCARD_CORS` | `false` | 生产不应使用 `*` |
| Host | `TRUSTED_HOSTS` | 空 | 生产 Host 白名单 |
| Host | `ALLOW_MISSING_TRUSTED_HOSTS` | `false` | 缺失 Host 白名单时是否允许启动 |
| 请求体 | `MAX_REQUEST_BODY_BYTES` | `8388608` | 非 GET 请求体上限 |
| 安全头 | `ENABLE_HSTS` | 生产 `true` | HSTS 开关 |
| 安全头 | `DISABLE_CSP` | `false` | 仅前端资源策略冲突时临时关闭 |
| 安全头 | `CONTENT_SECURITY_POLICY` | 空 | 自定义 CSP |
| 权限 | `ROLE_PERMISSIONS_JSON` | 空 | 短期权限灰度覆盖 |
| 审计 | `ALLOW_AUDIT_LOG_PURGE` | `false` | 是否允许清空审计日志 |
| 转发头 | `TRUST_PROXY_HEADERS` | `false` | 单独开启在生产无效 |
| 转发头 | `TRUSTED_PROXY_IPS` | 空 | 可信代理 IP / CIDR |

### AI、地图、代理和任务配置

| 分组 | 变量 | 默认 / 示例 | 说明 |
|------|------|-------------|------|
| AI 安全 | `AI_ALLOWED_HOSTS` | 空 | 限制 AI 生成链路 host |
| AI 安全 | `ALLOW_PRIVATE_AI_ENDPOINTS` | `false` | 内网模型必须同时配置 host 白名单 |
| AI 模型 | `AI_ALLOWED_MODELS` | 空 | 模型白名单 |
| AI 模型 | `AI_MAX_OUTPUT_TOKENS` | `1200` | 输出长度上限 |
| AI 请求 | `AI_REQUEST_MAX_TIMEOUT_SECONDS` | `60` | 单次请求超时 |
| AI 请求 | `AI_REQUEST_MAX_RETRIES` | `2` | 请求失败重试次数 |
| AI 提示词 | `AI_PROMPT_VERSION` | `2026-05-29.1` | 回放和审计用版本号 |
| AI 配额 | `AI_TENANT_DAILY_LIMIT` | `1000` | 全局每日配额 |
| AI 配额 | `AI_USER_DAILY_LIMIT` | `50` | 单用户窗口配额 |
| AI 配额 | `AI_RATE_LIMIT_WINDOW_SECONDS` | `86400` | 配额窗口 |
| 地理编码 | `GEOCODE_SEARCH_PROVIDER` | `mapchaxun` | 地址搜索 provider |
| 地理编码 | `GEOCODE_PROVIDER` | `osm` | 逆地理 provider |
| 地图密钥 | `BAIDU_MAP_AK` / `AMAP_KEY` | 空 | 使用百度 / 高德时配置 |
| 百度坐标 | `BAIDU_MAP_COORD_TYPE` | `gcj02ll` | 百度坐标类型 |
| 补卡节流 | `CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS` | `2` | 多日期补卡间隔 |
| 补卡节流 | `CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES` | `3` | 频繁请求最大重试 |
| 补卡节流 | `CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS` | `10` | 频繁请求初始等待 |
| 补卡节流 | `CLOCKIN_MAKEUP_RATE_LIMIT_COOLDOWN_SECONDS` | `60` | 触发 IP 频繁后的冷却间隔 |
| 工学云熔断 | `MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS` | `600` | 普通网络被限 IP 后暂停时长 |
| 补卡代理 | `MOGUDING_PROXY_API_URL` | 空 | 动态代理获取接口 |
| 补卡代理 | `MOGUDING_PROXY_URLS` | 空 | 静态代理池 |
| 补卡代理 | `MOGUDING_PROXY_ALLOWED_HOSTS` | 空 | 动态代理接口 host 白名单 |
| 补卡代理 | `ALLOW_PRIVATE_MOGUDING_PROXY_ENDPOINTS` | `false` | 内网代理接口开关 |
| 补卡代理 | `MOGUDING_PROXY_TTL_SECONDS` | `55` | 动态代理缓存时长 |
| 补卡代理 | `MOGUDING_PROXY_API_TIMEOUT_SECONDS` | `10` | 动态代理接口超时 |
| 报告补交 | `REPORT_MAKEUP_BATCH_DELAY_SECONDS` | `2` | 报告一键补交间隔 |
| 调度 | `SCHEDULER_TIMEZONE` | `Asia/Shanghai` | 调度时区 |
| 调度 | `SCHEDULER_LOAD_PAGE_SIZE` | `500` | 启动时分页加载用户 |
| 调度 | `SCHEDULER_JITTER_SECONDS` | `600` | 打卡调度抖动 |
| 调度 | `SCHEDULER_REPORT_JITTER_SECONDS` | `0` | 报告调度抖动 |
| 分布式锁 | `TASK_LOCK_TTL_SECONDS` | `1800` | 定时任务锁 TTL |
| 批量任务 | `BATCH_RUNNING_ITEM_TIMEOUT_SECONDS` | `1800` | running 项 lease 超时 |
| 批量任务 | `BATCH_JOB_MAX_USERS` | `200` | 单批任务用户数上限 |
| 批量任务 | `BATCH_TENANT_MAX_ACTIVE_JOBS` | `5` | 活动批量任务数量限制 |
| 幂等 | `IDEMPOTENCY_TTL_SECONDS` | `604800` | 幂等记录保留窗口 |

### 观测、备份和模型配置

| 分组 | 变量 | 默认 / 示例 | 说明 |
|------|------|-------------|------|
| 指标 | `METRICS_AUTH_TOKEN` | 空 | 生产访问 `/metrics` / `/metrics.prom` 所需 token |
| 指标 | `METRICS_CACHE_TTL_SECONDS` | `5` | 指标快照缓存 |
| 指标 | `HTTP_METRIC_RETENTION_DAYS` | `14` | HTTP 明细保留天数 |
| 指标 | `HTTP_METRIC_PURGE_INTERVAL_SECONDS` | `3600` | 指标清理节流 |
| 指标 | `HTTP_METRIC_SAMPLE_RATE` | `1` | HTTP 明细采样率 |
| 指标 | `HTTP_METRIC_EXCLUDE_PATH_PREFIXES` | 空 | 排除的路径前缀 |
| 指标 | `RECENT_METRIC_WINDOW_SECONDS` | `300` | 最近请求统计窗口 |
| 备份 | `BACKUP_ENCRYPTION_KEY` | 空 | 生产导出建议配置 |
| 备份 | `ALLOW_PLAINTEXT_BACKUP` | `false` | 是否允许生产明文导出 |
| 验证码 | `CAPTCHA_MODEL_AUTO_DOWNLOAD` | 生产 `false` | 是否启动时自动下载 ONNX |
| 验证码 | `CAPTCHA_MODEL_REQUIRE_CHECKSUM` | 生产 `true` | 下载时是否强制校验 SHA256 |
| 验证码 | `CAPTCHA_MODEL_SHA256_OCR_ONNX` | 空 | `ocr.onnx` 校验值 |
| 验证码 | `CAPTCHA_MODEL_SHA256_YOLOV5N_ONNX` | 空 | `yolov5n.onnx` 校验值 |

## 启动流程

| 顺序 | 动作 | 失败时优先排查 |
|------|------|----------------|
| 1 | 加载 `.env` | 文件路径、编码、变量名 |
| 2 | 创建数据库连接 | `DATABASE_URL`、网络、MySQL 权限 |
| 3 | 检查 / 初始化 schema | Alembic 版本、运行时迁移开关 |
| 4 | 初始化管理员种子账号 | 管理员密码强度、已有账号状态 |
| 5 | 检查验证码 ONNX 模型 | 自动下载开关、SHA256、模型文件 |
| 6 | 根据 `APP_ROLE` 启动 APScheduler | 时区、用户配置、任务锁 |
| 7 | 根据 `APP_ROLE` 启动 queue worker | 批量任务表、lease、线程池 |
| 8 | 挂载 `web/dist` | 前端是否已构建、静态资源路径 |

生产发布时以 Alembic 作为 schema 入口：

```bash
python -m alembic upgrade head
```

## 目录说明

| 路径 | 职责 |
|------|------|
| `server/api.py` | 管理端和用户端 API |
| `server/auth.py` | Token、Cookie、CSRF、角色和权限点 |
| `server/clockin_backfill.py` | 打卡记录归一化和待补卡日期筛选 |
| `server/coreApi/MainLogicApi.py` | 工学云接口客户端 |
| `server/coreApi/AiServiceClient.py` | AI 报告生成客户端 |
| `server/database.py` | 数据库连接、建表和补列兼容逻辑 |
| `server/migrations/` | Alembic 数据库迁移 |
| `server/models.py` | SQLModel 数据模型 |
| `server/observability.py` | 任务事件、HTTP 指标和 metrics 输出 |
| `server/queue_worker.py` | 批量任务队列 |
| `server/scheduler.py` | 用户定时任务注册 |
| `server/task_runner.py` | 打卡、补卡、报告提交等任务执行 |
| `server/user_runtime.py` | User 模型与运行配置之间的桥接 |
| `server/backup_cli.py` | 数据库 JSON 备份 / 恢复 |
| `server/util/` | 加密、验证码、消息推送、配置工具 |

## API 面

### 管理端

| 能力 | 路径示例 | 说明 |
|------|----------|------|
| 登录 | `/auth/login` | 管理端独立认证流 |
| 当前管理员 | `/auth/me` | 读取登录用户和权限 |
| 用户管理 | `/users`、`/users/{user_id}` | 新增、编辑、软删除、配置用户 |
| 批量任务 | `/batch-jobs`、`/batch-jobs/{id}` | 创建、查询、暂停、恢复、取消、重试 |
| 审计日志 | `/audit` | 查询关键操作 |
| 系统设置 | `/settings/*` | AI、SMTP、工学云代理等全局设置 |
| 地理编码 | `/geocode/search`、`/geocode/reverse` | 地址搜索和逆地理 |
| 补卡 | `/users/{user_id}/clock-in/*` | 缺卡查询、补选中、补全部 |
| 验证后继续打卡 | `/users/{user_id}/clock-in/alipay/continue` | 携带登记编号显式继续 1 次打卡 |
| 报告 | `/users/{user_id}/reports/*` | 生成、提交、补全部 |
| 指标 | `/metrics`、`/metrics.prom` | 运行统计和 Prometheus 文本 |

### 用户端

| 能力 | 路径 | 说明 |
|------|------|------|
| 注册 | `/app/auth/register` | 受 `APP_REGISTRATION_ENABLED` 控制 |
| 登录 | `/app/auth/login` | 用户端独立认证流 |
| 当前用户 | `/app/me` | 读取 / 保存自身配置 |
| 绑定账号 | `/app/bind` | 绑定工学云账号 |
| 手动执行 | `/app/run` | 触发当前用户任务 |
| 执行记录 | `/app/execution` | 查看当前用户执行记录 |
| 缺卡查询 | `/app/clock-in/missing-days` | 当前用户缺卡日期 |
| 补选中 | `/app/clock-in/makeup` | 当前用户选中日期补卡 |
| 补全部 | `/app/clock-in/makeup-all` | 当前类型全部待补日期 |
| 验证后继续打卡 | `/app/clock-in/alipay/continue` | 当前绑定用户显式继续 1 次打卡 |
| 报告周期 | `/app/reports/{report_key}/missing-periods` | 查询日报 / 周报 / 月报未提交周期 |
| 报告生成 | `/app/reports/{report_key}/generate` | AI 生成内容 |
| 报告提交 | `/app/reports/{report_key}/submit` | 提交报告 |
| 报告补全部 | `/app/reports/{report_key}/makeup-all` | 补当前报告类型的全部待补周期 |

管理端创建的用户如果没有单独写入 `app_password_hash`，用户端登录会使用该用户保存的工学云账号密码作为默认登录凭据，并在首次登录时自动生成绑定的 `AppUser`。自助注册用户仍使用独立的用户端账号，绑定工学云账号后才能执行打卡和报告任务。

## 支付宝安全验证链路

工学云打卡响应中的 `msg == "304"` 是业务验证状态。`ApiClient` 将其归一化为 `verification_required`，任务层返回可识别的失败结果，禁止把该响应记为成功。

| 阶段 | 模块 | 行为 |
|------|------|------|
| 识别 `304` | `server/coreApi/MainLogicApi.py` | 创建支付宝登记，校验 `outRegisterNo` 和 `alipays://` 深链，不自动重试打卡 |
| 任务映射 | `server/task_runner.py` | 普通打卡返回验证详情；补卡只返回失败，不提供继续入口 |
| 用户端继续 | `server/api.py` | 校验绑定用户、限流、登记编号和 `START` / `END` 后提交 1 次 |
| 管理端继续 | `server/api.py` | 校验 `tasks:run` 权限、租户、限流和请求参数后提交 1 次 |
| 再次验证 | `server/coreApi/MainLogicApi.py` | 如果继续响应仍为 `304`，创建新登记并返回 |
| 脱敏 | `server/user_runtime.py`、`server/util/MessagePush.py` | 不持久化、不审计、不推送登记编号和深链 |

继续请求模型包含必填 `out_register_no` 和默认值为 `START` 的 `target_type`。登记编号只允许字母、数字和连字符，长度不超过 128；打卡类型只允许 `START` 或 `END`。

## 补卡执行链路

| 阶段 | 模块 | 行为 |
|------|------|------|
| 接收请求 | `server/api.py` | 校验 `target_dates` / `target_type`，写审计日志 |
| 生成待补 | `server/clockin_backfill.py` | 归一化远端打卡记录，按 `START` / `END` 生成选项 |
| 执行补卡 | `server/task_runner.py` | 按日期、类型和配置时间执行 |
| 远端请求 | `server/coreApi/MainLogicApi.py` | 调用工学云补卡接口 |
| 结果回写 | `server/user_runtime.py` | 写最近状态、日志和运行态 |

补卡请求只补一种类型：

| `target_type` | 行为 |
|---------------|------|
| `START` | 只补上班 |
| `END` | 只补下班 |

即使某一天同时缺上班和下班，选择 `START` 时也只补上班；选择 `END` 时只补下班。

### 补卡限流与代理

| 情况 | 处理 |
|------|------|
| 正常多日期补卡 | 按 `CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS` 间隔逐个日期执行 |
| 远端频繁请求 | 等待后重试当前日期，不直接跳到下一个日期 |
| 当前日期恢复成功 | 后续日期按 `CLOCKIN_MAKEUP_RATE_LIMIT_COOLDOWN_SECONDS` 降速 |
| 当前日期重试耗尽 | 停止剩余日期并标记跳过 |
| 配置动态代理 | 补卡前调用 `MOGUDING_PROXY_API_URL` 获取 `ip:端口` |
| 代理失败或 IP 频繁 | 重新获取 / 切换代理后重试 |
| 非补卡请求 | 登录、缺卡查询、定时打卡、报告提交不使用补卡代理 |

动态代理接口示例：

```env
MOGUDING_PROXY_API_URL=http://capi.51daili.com/traffic/getip?linePoolIndex=1&packid=12&time=2&qty=1&port=1&format=txt&dt=2&ct=1&dtc=5&usertype=17&uid=54638&accessName=your-name&accessPassword=your-pass
```

接口返回 `1.2.3.4:8080` 时，实际代理会拼成 `http://your-name:your-pass@1.2.3.4:8080`。也可以使用 `MOGUDING_PROXY_URLS` 配置静态代理池。

## 任务执行链路

| 入口 | 触发者 | 最终函数 |
|------|--------|----------|
| 定时打卡 | APScheduler | `perform_clock_in` |
| 手动打卡 | 管理端 / 用户端 API | `perform_clock_in` |
| 单日补卡 | 管理端 / 用户端 API | `perform_clock_in_makeup` |
| 多日补卡 | 管理端 / 用户端 API | `perform_clock_in_makeup_many` |
| 日报提交 | 定时 / 手动 / 批量 | `submit_daily_report` |
| 周报提交 | 定时 / 手动 / 批量 | `submit_weekly_report` |
| 月报提交 | 定时 / 手动 / 批量 | `submit_monthly_report` |
| 批量执行 | `queue_worker.py` | 按任务类型分发到 `task_runner.py` |

| 机制 | 作用 |
|------|------|
| `TaskExecutionLock` | 防止多个 worker 重复执行同一用户、同一种定时任务 |
| 批量项 lease | 防止同一队列项被多个 worker / 线程重复处理 |
| 幂等键 | 相同操作者、用户列表和参数回放同一任务 |
| `TaskExecutionEvent` | 记录任务事件 |
| `HttpRequestMetric` | 记录 HTTP 状态、延迟和请求 ID |
| `/metrics` / `/metrics.prom` | 输出任务、批量、锁、请求状态码和延迟统计 |

## 备份恢复

| 操作 | 命令 |
|------|------|
| 导出 | `python -m server.backup_cli export backup.json` |
| 导入覆盖 | `python -m server.backup_cli import backup.json --replace-existing` |
| 加密导出 | `python -m server.backup_cli export backup.json --encryption-key <key>` |
| 加密导入 | `python -m server.backup_cli import backup.json --encryption-key <key>` |

导出包包含 manifest 和表校验和，导入前会校验完整性。生产环境导出默认要求 `BACKUP_ENCRYPTION_KEY` 或 `--encryption-key`；只有显式设置 `ALLOW_PLAINTEXT_BACKUP=true` 才允许明文导出。

## CI 后端门禁

Docker Publish 在构建镜像前执行以下后端步骤：

1. `python scripts/verify_supply_chain_policy.py`
2. `pip-audit -r server/requirements.txt`
3. `python -m alembic upgrade head`
4. `python -m unittest discover -s tests`
5. `python -m compileall server`
6. `python scripts/quality_gate.py`
7. `python scripts/backup_restore_drill.py`

修改 FastAPI 路由、请求模型或响应模型后，使用与 `server/requirements.txt` 一致的依赖运行 `python scripts/openapi_contract.py --write`，并提交 `docs/api/openapi-contract.json`。直接使用不同 FastAPI 版本生成快照可能造成 CI 契约测试失败。
