# 打卡预授权设计

## 背景

当前系统只在打卡接口返回业务码 `304` 后创建支付宝登记，并等待用户显式继续打卡。登记信息只存在于当次响应和页面内存，无法提前覆盖无人值守的定时打卡，也不能为指定日期的补卡预留凭据。

本次新增独立的预授权页面。系统根据用户添加日期、实习计划结束日期、打卡周期及上下班时间动态生成授权列表。用户逐项完成支付宝授权后，系统保存 `outRegisterNo`；普通打卡或补卡实际遇到 `304` 时，再自动取出匹配凭据重试一次。

## 目标

- 用户端和管理端均提供预授权页面。
- 日期范围从用户添加当天延伸到 `planInfo.endTime`。
- 仅为打卡周期中启用的星期生成授权项。
- 今天及未来按上班、下班分别授权；过去日期每天只需一条补卡授权。
- 提供浏览器和支付宝两种打开方式。
- 用户点击「我已完成授权」后才保存 `outRegisterNo`。
- 普通打卡、单日补卡和批量补卡只在真实 `304` 后使用匹配凭据。
- 凭据只自动使用一次，不改变现有即时支付宝验证能力。

## 非目标

- 不批量创建支付宝登记，用户必须逐项操作。
- 不在服务端判断支付宝页面是否真的验证成功；用户显式确认是唯一完成信号。
- 不保存未确认登记或 `registerUrl`。
- 不允许同一凭据自动重试多次。
- 不改变工学云上下班时间、打卡周期和补卡类型的现有配置入口。

## 已确认规则

### 日期与类型

- 起始日期为用户添加时间对应的北京时间日期。
- 结束日期为实习计划对象中的 `planInfo.endTime`。
- 只生成 `clockIn.schedule.weekdays` 启用的星期；缺失时沿用当前默认规则。
- 今天及未来每天生成 `START` 和 `END` 两项。
- 已过去日期每天生成一项 `MAKEUP`，折叠显示但允许展开。
- `MAKEUP` 可用于该日上班或下班补卡，具体类型在执行补卡时确定。
- 修改打卡周期或上下班时间后，动态列表立即按新配置计算；已保存记录不删除。

### 授权与消费

- 开始预授权时，未确认数据只保留在当前页面内存。
- 页面刷新、关闭或票据过期后，用户需要重新发起预授权。
- 点击「我已完成授权」后才持久化 `outRegisterNo`。
- `outRegisterNo` 按用户要求明文保存，不在页面、日志、审计、通知或执行历史中返回。
- `registerUrl` 永不持久化。
- 首次打卡或补卡请求始终不带预授权凭据。
- 收到 `304` 后，系统原子占用匹配凭据并自动重试一次。
- 凭据一旦实际进入重试流程，即视为已使用；网络结果不确定时也不得复用。
- 重试仍返回 `304` 时，记录改为「需重新授权」，且不再自动重试。
- 已使用或失败的过去日期允许重新预授权，但同一时刻只能存在一个有效凭据。

## 数据模型

### 用户创建时间

为 `user` 表新增非空 `created_at` 字段，统一保存 UTC 时间。展示和日期计算时转换为 `Asia/Shanghai`。

历史用户按以下优先级回填：

1. 该用户最早的 `user.create` 审计时间。
2. `clockIn.schedule.startDate` 对应日期的北京时间零点。
3. 执行迁移时的 UTC 时间。

新用户在创建时写入当前 UTC 时间。

### 预授权记录

新增 `ClockInPreauthorization` 模型及 `clockinpreauthorization` 表：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 主键 |
| `tenant_id` | string | 租户 ID |
| `user_id` | integer | 任务用户 ID |
| `target_date` | date | 目标打卡或补卡日期 |
| `target_type` | string | `START`、`END` 或 `MAKEUP` |
| `status` | string | `authorized`、`consumed` 或 `reauthorize_required` |
| `out_register_no` | string | 明文 `outRegisterNo` |
| `authorized_at` | datetime | 用户确认授权时间，UTC |
| `consumed_at` | datetime | 凭据占用时间，UTC，可空 |
| `used_target_type` | string | `MAKEUP` 实际用于 `START` 或 `END`，其余为空 |
| `created_at` | datetime | 创建时间，UTC |
| `updated_at` | datetime | 最后更新时间，UTC |

唯一约束为 `(tenant_id, user_id, target_date, target_type)`。增加 `(tenant_id, user_id, target_date)` 和 `(tenant_id, user_id, status, target_date)` 组合索引。

数据库不创建 `pending` 状态。重新授权时更新唯一记录；状态为 `authorized` 的记录不能被普通重复提交覆盖，必须通过明确的重新授权入口。

## 动态列表

列表服务以 `User.created_at`、`planInfo.endTime` 和当前打卡配置计算虚拟行，再按唯一键关联已保存记录。它不预先向数据库写入未来数千条空记录。

### 范围计算

1. 将 `User.created_at` 转换为北京时间日期。
2. 解析 `planInfo.endTime`；支持当前运行数据使用的日期时间字符串。
3. 起始日期晚于结束日期时返回空列表及「实习计划已结束」状态。
4. 遍历闭区间并按 `schedule.weekdays` 过滤。
5. 目标日期早于北京时间今天时生成 `MAKEUP`，否则生成 `START` 和 `END`。
6. `START`、`END` 的显示时间分别取 `schedule.startTime`、`schedule.endTime`。

列表默认使用已保存的 `planInfo.endTime`。字段缺失时自动登录并调用 `practice/plan/v3/getPlanByStu` 同步计划；用户点击「刷新计划」时强制重新同步。同步失败或结束时间无效时返回明确业务错误，不猜测结束日期。

配置变化后，虚拟行按最新配置刷新。数据库中的历史授权和消费记录继续保留；不再属于当前有效周期的记录不出现在活动列表中。

## 支付宝 URL 构建

原始登记接口仍为 `usercenter/alipay/v1/createAxdjk`。服务端只接受合法的 `alipays://` 分层 URL。

### 回跳文案

`thirdPartSchema` 改为百度翻译移动端 URL，其 `query` 文案为：

```text
你已经成功了，请返回点击我已完成授权，本次授权账号：{User.phone}，本次授权时间：{北京时间 YYYY-MM-DD HH:mm:ss}
```

授权时间指创建支付宝登记并取得 `outRegisterNo` 的服务端北京时间，不是目标打卡时间。

### 编码规则

必须使用结构化 URL API 完成以下步骤：

1. 使用 `urlsplit`、`parse_qsl` 解析原始支付宝 URL。
2. 使用 `urlencode` 构建百度翻译 URL 的 `from=zh`、`to=en` 和动态 `query`。
3. 替换支付宝 URL 中的 `thirdPartSchema`，保留其他查询参数。
4. 使用 `urlencode` 重新编码支付宝查询参数，得到直接打开 URL。
5. 使用 `urlencode({"scheme": direct_url})` 构建 `https://ds.alipay.com/?scheme=...` 浏览器 URL。

不得通过字符串替换拼接嵌套查询参数。测试必须覆盖中文、手机号、时间、`&`、`=`、百分号及重复 `thirdPartSchema`，最终结果中只保留一个有效回跳参数。

## API 设计

用户端与管理端使用同一服务层，分别暴露以下路径：

| 能力 | 用户端 | 管理端 |
| --- | --- | --- |
| 查询列表 | `GET /api/app/clock-in/preauthorizations` | `GET /api/users/{user_id}/clock-in/preauthorizations` |
| 开始授权 | `POST /api/app/clock-in/preauthorizations/start` | `POST /api/users/{user_id}/clock-in/preauthorizations/start` |
| 完成授权 | `POST /api/app/clock-in/preauthorizations/complete` | `POST /api/users/{user_id}/clock-in/preauthorizations/complete` |

用户端只能操作当前绑定任务用户。管理端要求任务执行权限，并继续应用租户隔离。

### 查询列表

查询参数包括：

- `scope=future|past`：主列表或过去补卡列表。
- `page`、`page_size`：分页参数。
- `status`：可选状态过滤。
- `target_type`：可选类型过滤。
- `refresh_plan=true`：强制同步计划。

响应包含脱敏账号、创建日期、计划结束日期、当前上下班时间、状态汇总、分页信息和列表项。列表项包含日期、时间、类型、状态、授权/消费时间、补卡实际类型和允许的操作；不包含 `outRegisterNo` 或任何打开 URL。

### 开始授权

请求体包含 `target_date` 和 `target_type`。服务端必须先验证该虚拟行在当前日期范围和周期内存在，且状态允许发起或重新授权。

成功响应包含：

- `registration_ticket`：短期签名票据。
- `direct_url`：修改回跳地址后的 `alipays://` URL。
- `browser_url`：`https://ds.alipay.com/?scheme=...` URL。
- `started_at`：登记发起时间。
- `expires_at`：票据过期时间。

签名票据有效期为 30 分钟，绑定租户、用户、日期、类型、`outRegisterNo`、发起时间和用途。它只存在于页面内存，不写数据库。票据过期、篡改、跨用户或用途不符时拒绝完成请求。

### 完成授权

请求体只提交 `registration_ticket`。服务端验证后按唯一键写入或更新记录：

- 同一有效票据重复提交时幂等返回当前记录。
- 已有其他 `authorized` 凭据时拒绝静默覆盖。
- `consumed` 或 `reauthorize_required` 记录允许通过新票据重新授权。
- 审计只记录用户、日期、类型和结果，不记录票据、登记编号或 URL。

开始与完成接口分别按客户端 IP 和用户 ID 限流。

## 页面设计

页面采用已确认的扁平表格方案。

### 入口

- 用户端新增 `/u/preauthorizations`，在用户端顶部导航增加「预授权」。
- 管理端新增 `/users/:id/preauthorizations`，从用户列表和用户编辑页进入指定用户的预授权页面。
- 两端复用列表、筛选和授权对话框组件，只切换 API 前缀与权限。

### 主列表

今天及未来记录默认展开，表格列为：

- 日期
- 时间
- 类型
- 状态
- 授权时间
- 操作

提供日期、类型和状态筛选及分页。过去日期放入可展开的「补卡预授权」区域，每天一行，类型显示为「补卡」，使用后显示实际采用的上班或下班类型。

桌面端使用高密度表格。窄屏改为每条授权记录一行的紧凑纵向列表，避免依赖横向滚动。

### 授权对话框

点击「开始预授权」后创建登记并打开对话框，提供以下操作：

- 「浏览器打开」：新窗口打开 `browser_url`，使用 `noopener`、`noreferrer`。
- 「支付宝打开」：跳转到 `direct_url`。
- 「我已完成授权」：提交签名票据并刷新当前列表。
- 「取消」：丢弃页面内存中的登记信息。

状态文案统一为「待授权」「已授权」「已使用」「需重新授权」。页面不渲染完整 `outRegisterNo`。

## 打卡与补卡联动

### 核心响应边界

底层工学云客户端不再在检测到 `304` 时无条件创建新登记，而是先返回明确的 `verification_required` 业务结果。任务编排层决定使用预授权或创建即时登记，避免在已有预授权时生成无用 `outRegisterNo`。

### 普通打卡

1. 按当前逻辑确定北京时间日期和 `START` 或 `END`。
2. 首次提交不带 `outRegisterNo`。
3. 非 `304` 时按原业务结果结束，不查询预授权。
4. 收到 `304` 后查询同租户、用户、日期、类型且状态为 `authorized` 的记录。
5. 通过条件更新将记录原子改为 `consumed`，写入 `consumed_at`。
6. 取得占用成功的 `outRegisterNo` 并重试一次。
7. 重试仍为 `304` 时改为 `reauthorize_required`；其他结果保留 `consumed`。
8. 没有可用记录时创建即时登记，沿用现有显式继续打卡流程。

### 补卡

补卡使用目标日期，而不是请求执行日期。首次请求不带凭据；收到 `304` 后查询该日 `MAKEUP`：

- 原子占用时写入实际 `used_target_type`。
- 上班或下班补卡均可使用，但同一 `MAKEUP` 只能占用一次。
- 单日补卡和批量补卡使用相同解析器；批量任务逐日期独立匹配。
- 重试规则、状态变化和即时验证回退与普通打卡一致。

### 并发与失败

定时任务锁只覆盖同一用户和打卡类型，不能代替凭据级并发控制。凭据使用必须通过带 `status = authorized` 条件的数据库更新完成；只有更新一行的调用方可以取得并使用凭据。

在发送前完成原子占用。进程崩溃、超时或网络结果不确定时，凭据保持 `consumed`，避免重复使用。用户可在页面重新预授权后再次尝试。

现有显式继续接口携带的是用户当次已经完成的登记编号，不再额外查询或消费预授权。

## 安全与隐私

- `outRegisterNo` 按产品要求明文保存，仅任务执行服务读取。
- ORM 对象不得直接作为预授权 API 响应。
- 日志、审计、异常、通知、执行结果和监控指标不得包含 `outRegisterNo`、`registerUrl`、两个打开 URL 或签名票据。
- 用户账号只出现在支付宝回跳文案中；普通列表响应使用现有脱敏规则。
- 管理端接口应用任务执行权限、租户约束和目标用户有效状态检查。
- 所有日期匹配以 `Asia/Shanghai` 为业务时区，持久化时间统一使用 UTC。
- 外部登记 URL 必须在服务端验证 `alipays://` Scheme，再交给前端。

## 错误处理

- 无绑定用户：用户端返回现有绑定错误。
- `planInfo.endTime` 缺失或无效：尝试同步后返回明确错误，页面显示空状态和「刷新计划」。
- 目标日期或类型不在动态列表：返回 `400`，不得创建登记。
- 工学云登记失败或 URL 非法：不生成签名票据，不改变已有状态。
- 票据过期、篡改或归属不符：返回 `400` 或 `403`，提示重新开始授权。
- 完成接口遇到并发有效凭据：返回冲突，不覆盖已有 `authorized` 记录。
- 凭据重试仍返回 `304`：标记「需重新授权」，不进入循环。

## 数据库迁移与备份

新增 Alembic 迁移 `20260715_0003`：

1. 为 `user` 新增可空 `created_at`。
2. 按回填规则补齐历史值。
3. 将字段改为非空并建立索引。
4. 创建 `clockinpreauthorization` 表、唯一约束和组合索引。

迁移必须兼容当前 MySQL 部署并提供可逆降级。新表加入备份导出和恢复顺序；恢复测试验证用户与预授权记录的关联不丢失。

## 测试方案

### 后端

- 动态日期范围、闭区间、北京时间边界和星期过滤。
- 过去日期每天一条 `MAKEUP`，今天及未来每天两条记录。
- 用户创建时间的 3 级历史回填。
- `planInfo.endTime` 缺失时同步及失败处理。
- 百度回跳文案中的真实账号和登记发起时间。
- 双层查询参数编码、特殊字符和非法 Scheme 拒绝。
- 签名票据过期、篡改、重放、跨租户和跨用户校验。
- 完成授权的幂等、冲突和重新授权状态转换。
- 普通打卡和补卡首次请求不带凭据。
- 仅在 `304` 后原子消费，最多重试一次。
- 批量补卡逐日期匹配，`MAKEUP` 记录实际类型。
- 并发占用只能成功一次。
- 敏感字段不进入执行历史、审计、日志和通知。
- Alembic、模型契约、OpenAPI 及备份恢复测试。

### 前端

- 用户端和管理端路由、权限及 API 前缀。
- 扁平表格、过去日期折叠、筛选、分页和状态文案。
- 两种打开方式使用服务端返回的完整 URL。
- 「我已完成授权」提交票据并刷新状态。
- 取消、刷新、票据过期和接口失败状态。
- 页面不展示完整 `outRegisterNo`。
- 桌面和移动视口的 Playwright 截图及无重叠检查。

### 完整验证

```powershell
python -m unittest discover -s tests
python -m compileall server
ruff check server scripts
python scripts/quality_gate.py
python scripts/verify_supply_chain_policy.py
python scripts/backup_restore_drill.py
python scripts/export_openapi.py --check

Set-Location web
npm run lint
npm test
npm run build
```

还需执行 Docker 镜像构建，确认 Alembic 迁移、前端静态资源和生产启动流程可用。

## 文档更新

实现完成后同步更新：

- `README.md`
- `server/README.md`
- `web/README.md`
- `docs/current-features.md`
- `docs/ops/runbook.md`
- `CHANGELOG.md`
- OpenAPI 快照

文档必须说明日期生成规则、双端入口、两种打开方式、显式确认、普通打卡/补卡的凭据消费语义，以及 `outRegisterNo` 明文存储但不进入可观测数据的边界。

## 完成标准

- 双端能从用户添加日期到实习计划结束日期查看授权列表。
- 未来日期按上/下班分别授权，过去日期每天一条补卡授权。
- 两种 URL 均正确修改并编码 `thirdPartSchema`。
- 未点击「我已完成授权」时数据库没有新增或变更凭据记录。
- 打卡和补卡仅在真实 `304` 后使用匹配凭据，且最多重试一次。
- 同一凭据在并发场景下只消费一次。
- 所有敏感输出边界测试通过。
- 数据库迁移、备份恢复、完整后端和前端测试、OpenAPI 校验及 Docker 构建全部通过。
