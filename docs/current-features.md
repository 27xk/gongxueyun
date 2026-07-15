# 当前功能与接口速查

本文档记录当前代码实现中的核心功能、页面入口和接口约定。它描述的是当前实现，不是历史设计稿。

## 功能范围

| 模块 | 管理端 | 用户端 | 说明 |
|------|--------|--------|------|
| 认证 | 管理员登录、角色和权限点 | 注册、登录、绑定工学云账号 | 两套登录态独立 |
| 用户管理 | 创建、编辑、软删除、停用 | 读取和保存自身配置 | 删除用户保留历史记录 |
| 打卡设置 | 代维护时间、地点、周期、图片、备注；处理安全验证 | 自助维护个人配置；验证后显式继续打卡 | 保存后重建调度任务 |
| 缺卡查询 | 指定用户缺卡日期 | 当前用户缺卡日期 | 后端按打卡周期过滤 |
| 手动补卡 | 补选中、补全部 | 补选中、补全部 | 一次只补一种类型 |
| 报告补交 | 日报 / 周报 / 月报 | 日报 / 周报 / 月报 | 按报告类型独立处理 |
| 批量任务 | 创建、暂停、恢复、取消、重试 | 不开放 | worker 认领队列项 |
| 系统设置 | AI、SMTP、工学云代理 | 不开放全局设置 | 用户可维护个人推送 |
| 地理编码 | 地址搜索、地图核对页 | 地址搜索 | `/geocode/search` 为主 |
| 运行观测 | 审计、指标、任务事件 | 执行记录 | metrics 生产需 token |

## 页面与入口

| 路径 | 页面 | 说明 |
|------|------|------|
| `/login` | 管理端登录页 | 管理员入口 |
| `/` | 会话分流入口 | 管理员进入用户列表，用户进入 `/u`，未登录进入 `/u/login` |
| `/create` | 新增用户页 | 创建用户与基础配置 |
| `/edit/:id` | 用户编辑页 | 打卡、补卡、报告、单用户推送 |
| `/audit` | 审计日志页 | 关键操作记录 |
| `/settings` | 系统设置页 | AI、SMTP、工学云补卡代理 |
| `/settings/notifications` | 通知设置页 | 全局邮箱通知 |
| `/u/login` | 用户登录页 | 独立用户端认证状态 |
| `/u/register` | 用户注册页 | 受后端注册开关控制 |
| `/u` | 用户工作台 | 手动执行、执行记录、日报快捷入口 |
| `/u/settings` | 我的配置 | 打卡、报告、补卡、个人推送 |

## 核心代码

| 文件 | 职责 |
|------|------|
| `server/api.py` | 管理端和用户端 API |
| `server/user_runtime.py` | 数据库用户模型与任务运行配置之间的转换 |
| `server/scheduler.py` | 定时任务注册 |
| `server/queue_worker.py` | 批量任务队列 |
| `server/task_runner.py` | 打卡、补卡、报告提交等任务执行 |
| `server/clockin_backfill.py` | 打卡记录归一化和待补卡日期筛选 |
| `server/coreApi/MainLogicApi.py` | 工学云接口客户端 |
| `server/execution_locks.py` | 定时任务分布式锁 |
| `server/observability.py` | 任务事件记录与 `/metrics` 运行统计 |

## 地图与地理编码

| 能力 | 默认配置 | 说明 |
|------|----------|------|
| 地址搜索 | `GEOCODE_SEARCH_PROVIDER=mapchaxun` | 调用后端 `/geocode/search`，默认使用 `https://www.mapchaxun.cn/api/getSolidAdress` |
| 经纬度核对页 | `VITE_MAP_DISPLAY_URL=https://www.mapchaxun.cn/jingweidu` | 管理端打卡设置内嵌展示 |
| 逆地理解析 | `GEOCODE_PROVIDER=osm` | 搜索服务未返回结构化地址时才需要 |
| 百度搜索 / 逆地理 | `GEOCODE_SEARCH_PROVIDER=baidu` 或 `GEOCODE_PROVIDER=baidu` | 必须配置 `BAIDU_MAP_AK` |
| 高德兼容 | `GEOCODE_PROVIDER=amap` | 需要 `AMAP_KEY` |

| 百度变量 | 说明 |
|----------|------|
| `BAIDU_MAP_AK` | 百度地图 Web 服务 AK |
| `BAIDU_MAP_COORD_TYPE` | 地理编码返回坐标类型，默认 `gcj02ll` |
| `BAIDU_MAP_INPUT_COORD_TYPE` | 百度逆地理输入坐标类型，默认跟随 `BAIDU_MAP_COORD_TYPE` |
| `BAIDU_MAP_OUTPUT_COORD_TYPE` | 百度地理编码返回坐标类型，默认跟随 `BAIDU_MAP_COORD_TYPE` |

使用百度但未配置 AK 时会直接返回明确错误，不会静默切换到其他服务。

## 打卡记录与缺卡筛选

打卡记录会先归一化为以下结构：

| 字段 | 说明 |
|------|------|
| `date` | 日期，格式为 `YYYY-MM-DD` |
| `time` | 打卡时间，格式为 `YYYY-MM-DD HH:mm:ss` |
| `type` | 打卡类型，`START` 或 `END` |
| `type_label` | 中文类型，上班或下班 |
| `address` | 记录中的地址 |

缺卡筛选规则：

| 条件 | 结果 |
|------|------|
| 同一天存在 `START` 和 `END` | 不是待补日期 |
| 同一天只存在 `START` | 只在下班待补列表出现 |
| 同一天只存在 `END` | 只在上班待补列表出现 |
| 同一天两种都不存在 | 分别在上班和下班待补列表出现 |
| 日期不在用户打卡周期内 | 不展示 |

## 补卡规则

| 规则 | 当前实现 |
|------|----------|
| 补卡类型 | `target_type=START` 只补上班；`target_type=END` 只补下班 |
| 触发方式 | 只通过管理端或用户端手动接口触发 |
| 定时任务 | 普通定时打卡不会自动触发补卡 |
| 多日期补卡 | 按日期间隔逐个执行 |
| 频繁请求 | 遇到“请求过于频繁”、`429`、`rate limit` 或 IP 限制时重试当前日期 |
| 冷却策略 | 当前日期恢复后后续日期降速；当前日期耗尽重试仍失败则停止剩余日期 |
| 代理作用域 | 工学云代理只在手动补卡执行阶段启用 |

补卡时间来源：

| 类型 | 时间来源 | 默认 |
|------|----------|------|
| 上班补卡 | `config.clockIn.schedule.startTime` | `07:30` |
| 下班补卡 | `config.clockIn.schedule.endTime` | `18:00` |

## 支付宝安全验证

打卡接口返回 HTTP 200 且业务字段为 `code == 200`、`msg == "304"` 时，状态是 `verification_required`，不能记录为打卡成功。系统会获取支付宝登记信息并等待用户完成验证，不会自动重试。

| 阶段 | 当前实现 |
|------|----------|
| 首次 `304` | 打卡只提交 1 次，随后创建验证登记 |
| 页面提示 | 只接受 `alipays://` 深链，登记信息保存在页面内存 |
| 用户确认 | 用户点击「验证完成，继续打卡」后显式调用继续接口 |
| 继续成功 | 关闭对话框并刷新任务结果 |
| 继续后再次 `304` | 创建新登记并显示新的验证链接，不自动循环请求 |
| 结果存储 | `outRegisterNo` 和 `registerUrl` 不进入审计、持久化执行结果或消息通知 |
| 补卡 | 待验证响应按失败处理，不提供继续补卡入口 |

继续请求体：

```json
{
  "out_register_no": "example-register-no",
  "target_type": "START"
}
```

`out_register_no` 必填，`target_type` 只允许 `START` 或 `END`。

| 调用端 | 方法 | 路径 |
|--------|------|------|
| 用户端 | `POST` | `/app/clock-in/alipay/continue` |
| 管理端 | `POST` | `/users/{user_id}/clock-in/alipay/continue` |

## 补卡接口

### 用户端

| 方法 | 路径 | 请求体 | 说明 |
|------|------|--------|------|
| `GET` | `/app/clock-in/missing-days` | 无 | 获取当前用户缺卡日期 |
| `POST` | `/app/clock-in/makeup` | `target_dates` 或 `target_date` + `target_type` | 补选中的日期 |
| `POST` | `/app/clock-in/makeup-all` | `target_type` | 补当前类型下全部待补日期 |

### 管理端

| 方法 | 路径 | 请求体 | 说明 |
|------|------|--------|------|
| `GET` | `/users/{user_id}/clock-in/missing-days` | 无 | 获取指定用户缺卡日期 |
| `POST` | `/users/{user_id}/clock-in/makeup` | `target_dates` 或 `target_date` + `target_type` | 为指定用户补选中的日期 |
| `POST` | `/users/{user_id}/clock-in/makeup-all` | `target_type` | 为指定用户补当前类型下全部待补日期 |

缺卡选项示例：

```json
{
  "value": "2026-05-06",
  "label": "2026-05-06（缺上班）",
  "missing_types": ["START"],
  "existing_types": ["END"]
}
```

补选中请求：

```json
{
  "target_dates": ["2026-05-06", "2026-05-07"],
  "target_type": "START"
}
```

兼容单日期请求：

```json
{
  "target_date": "2026-05-06",
  "target_type": "END"
}
```

补全部请求：

```json
{
  "target_type": "END"
}
```

## 前端补卡交互

| 步骤 | 页面行为 | 后端行为 |
|------|----------|----------|
| 1 | 点击「刷新缺卡」 | 读取远端打卡记录并生成待补选项 |
| 2 | 选择补卡类型：上班 / 下班 | 对应 `START` / `END` |
| 3 | 日期下拉过滤 | 只展示当前类型仍缺的日期 |
| 4 | 点击「补选中」 | 提交 `target_dates` |
| 5 | 点击「全部待补」 | 后端重新查询当前类型仍缺日期后执行 |

页面会显示：

| 指标 | 含义 |
|------|------|
| 已获取打卡记录数 | 本次远端记录数量 |
| 当前类型待补天数 | 当前 `START` / `END` 过滤后的待补数量 |
| 已选择天数 | 用户多选的日期数量 |

## 报告补交

| 报告类型 | 待补周期 | 支持操作 | 未开启时行为 |
|----------|----------|----------|--------------|
| 日报 | 日期 | 刷新、AI 生成、提交、补全部日报 | 前端禁用入口，后端返回空列表 |
| 周报 | 自然周 | 刷新、AI 生成、提交、补全部周报 | 前端禁用入口，后端返回空列表 |
| 月报 | 月份 | 刷新、AI 生成、提交、补全部月报 | 前端禁用入口，后端返回空列表 |

「立即执行日报 / 周报 / 月报」属于报告类手动任务，不触发普通 `/run` 接口的 429 限流。打卡和默认手动运行仍保留内部限流。

用户端工作台保留快捷日报生成和提交入口；完整日报、周报、月报配置与补交入口位于 `/u/settings`。

## 认证、AI 与运行观测

| 主题 | 当前行为 |
|------|----------|
| 登录态 | 默认写入 HttpOnly Cookie，前端不再把 token 放进 `localStorage` |
| CSRF | 非安全方法校验 `Origin` / `Referer` 和双提交 token |
| 跨域 | 跨域前端需要 `FRONTEND_ORIGINS` 或 `CORS_ORIGINS` |
| 响应安全头 | 默认启用安全头和 CSP；生产默认启用 HSTS |
| 审计 | 审计日志默认不可清空；清空需显式开启并留下审计记录 |
| 删除用户 | 软删除，停用打卡和用户端绑定账号，保留历史记录 |
| 敏感字段 | 生产必须配置 `USER_PASSWORD_KEY` 或 `FERNET_KEY` 后才能保存新的敏感字段 |
| 权限 | 管理端按权限点校验，不只依赖粗粒度角色 |
| AI 设置 | 全局系统设置统一管理 `/settings/ai` 和 `/settings/ai/test` |
| AI Key | 读取全局 AI 设置只返回 `hasApiKey`，不回显 API Key |
| AI 出站 | 默认拒绝本机、内网、链路本地和特殊地址；内网模型必须配白名单 |
| AI 配额 | 受全局每日、用户每日和窗口限流控制 |
| 定时任务 | 使用数据库锁避免重复执行 |
| 批量队列 | queued 原子认领为 running，写入 owner、lease token 和到期时间 |
| 数据库限流 | 登录和注册同时按 IP 与账号维度限流 |
| metrics | `/metrics` JSON，`/metrics.prom` Prometheus 文本；生产需要 token |
| 模型下载 | 生产默认不在启动时自动下载验证码 ONNX 模型 |
| schema | 生产默认不做运行时 schema 写操作，启动校验 Alembic head |
| 备份 | 导出包包含 manifest 和表校验和；生产默认要求加密 |

## CI 验证顺序

Docker Publish 在镜像构建前依次运行 action 固定策略、Python 依赖审计、数据库迁移、后端测试、编译、质量门、备份恢复、前端审计与构建，以及 Trivy 文件系统扫描。任一前置步骤失败时，Docker Buildx、镜像推送、签名和验签不会执行。
