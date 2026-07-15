# 运行手册

本文档用于排查线上运行、CI 安全扫描和批量任务异常。处理问题时优先保留日志、请求 ID、任务 ID、批量任务 ID 和后端版本，避免只凭界面现象判断。

## 告警总览

| 告警 / 现象 | 先看哪里 | 最常见原因 | 完成标准 |
|-------------|----------|------------|----------|
| 供应链审计失败 | CI 失败步骤、依赖审计、容器扫描、固定策略结果 | 依赖漏洞、外部 action 未固定、基础镜像未固定 | 审计和策略结果恢复正常 |
| 认证失败激增 | `/audit`、登录接口响应、限流指标 | 密码错误、账号停用、Cookie / Origin / Host 配置变更 | 单账号问题被定位，或全局配置恢复 |
| 5xx 告警 | `/metrics.prom`、请求 ID、后端日志 | 数据库、迁移版本、配置校验、外部依赖失败 | 5xx 回落，请求 ID 可追溯 |
| 批量任务卡住 | `/batch-jobs/{id}`、worker 日志 | running lease 未回收、远端限流、worker 停止 | queued / running / failed 状态恢复可解释 |
| AI 生成失败 | 系统设置、`/settings/ai/test`、AI 安全配置 | Key 缺失、host 白名单、内网端点被拒、模型不在白名单 | 测试接口和正式生成链路均可解释 |
| 支付宝验证异常 | 预授权状态、当次任务结果、继续接口响应、授权用户范围 | `304` 被误判、预授权已消费、登记失败、链接无效、票据过期 | 不误报成功，状态转换可解释，用户可重新授权或即时验证 |
| 权限异常 | 当前用户权限、`ROLE_PERMISSIONS_JSON`、审计日志 | 灰度配置残留、角色权限映射错误 | 权限矩阵符合预期，灰度配置清理 |

## 供应链审计失败

| 检查项 | 命令 / 文件 | 处理 |
|--------|-------------|------|
| Python 依赖 | `server/requirements.txt` | 升级受影响包并做回归确认 |
| 前端依赖 | `web/package.json`、`web/package-lock.json` | 升级依赖锁定文件，必要时调整依赖声明 |
| GitHub Actions | `.github/workflows/docker-publish.yml` | 外部 action 的 `uses:` 必须钉到完整 40 位 commit SHA |
| Docker 基础镜像 | `Dockerfile` | `FROM` 必须使用 `@sha256:` digest |
| 策略结果 | CI 输出中的固定策略检查 | 修复 action 和基础镜像固定策略 |

### Docker Publish 步骤顺序

Docker Publish 只有一个主 Job，但镜像步骤位于全部质量门之后。排障时先定位第一个失败步骤，不要把所有失败都归因于 Docker。

| 阶段 | 关键步骤 | 失败影响 |
|------|----------|----------|
| 后端准备 | 安装依赖、`pip-audit`、Alembic 迁移 | 后端测试和后续步骤不执行 |
| 后端验证 | unittest、compileall、质量门、备份恢复演练 | Node、Trivy 和 Docker 全部跳过 |
| 前端验证 | `npm ci`、审计、lint、测试、构建 | Trivy 和 Docker 全部跳过 |
| 文件系统扫描 | Trivy `CRITICAL,HIGH` | 镜像登录、构建和推送跳过 |
| 镜像发布 | Buildx、登录、metadata、build and push | 签名和验签跳过 |
| 供应链证明 | cosign 签名、GHCR 签名验证 | Job 失败，不应视为完整发布 |

GitHub Job 元数据中的 `skipped` 表示前置步骤已经失败，不代表被跳过的 Docker 步骤本身存在错误。后端测试失败时，应先获取失败断言或在与 CI 相同的 Python 和依赖版本中复现。

### OpenAPI 契约失败

`tests/test_openapi_contract.py` 比较运行时生成结果和 `docs/api/openapi-contract.json`。FastAPI 版本不同可能改变框架内置的验证错误 schema，因此不能使用全局旧环境生成快照。

```powershell
python -m pip install -r server/requirements.txt
python scripts/openapi_contract.py --write
python -m unittest discover -s tests -p "test_openapi_contract.py" -v
```

提交前检查快照差异，只应包含本次路由或模型变更，以及当前固定依赖确定生成的框架字段。不要为了让测试通过手工删除未知字段。

## 认证失败告警

| 现象 | 检查 | 处理 |
|------|------|------|
| 单个管理员失败 | `/audit` 中 `admin.login.fail`、账号状态、密码重置记录 | 区分密码错误、账号停用、权限变更 |
| 单个用户失败 | `/audit` 中 `app.login.fail`、用户端账号、绑定工学云信息 | 确认是用户端密码还是工学云账号密码 |
| 大量账号同时失败 | `APP_SECRET`、Cookie 域、`FRONTEND_ORIGINS`、`TRUSTED_HOSTS` | 回滚错误配置或补齐白名单 |
| 浏览器有 Cookie 但请求 401 | CSRF token、Origin / Referer、HTTPS / `AUTH_COOKIE_SECURE` | 按部署方式修正前端地址和 Cookie 安全属性 |
| 登录接口 429 | 限流 bucket、IP、账号维度 | 判断是否撞库、代理配置错误或测试脚本过快 |

## 5xx 告警

| 排查顺序 | 证据 | 判断 |
|----------|------|------|
| 1 | `/metrics.prom` 状态码分布和最近请求 ID | 是否集中在单接口 |
| 2 | `HttpRequestMetric` | 是否集中在同一账号、路径或时间窗口 |
| 3 | `TaskExecutionEvent` | 是否由定时任务、补卡或报告任务触发 |
| 4 | `AuditLog` | 是否紧跟配置变更、权限变更或批量操作 |
| 5 | 后端启动日志 | 数据库连接、Alembic 版本、模型校验、环境变量安全校验 |

| 原因 | 处理 |
|------|------|
| 批量任务引起 | 先暂停或取消对应任务，再处理失败项，避免 worker 扩散错误 |
| 迁移版本不一致 | 执行 `python -m alembic current` 和 `python -m alembic upgrade head` |
| 数据库连接失败 | 检查 `DATABASE_URL`、MySQL 网络、账号权限和连接池配置 |
| 请求体过大 | 检查 `MAX_REQUEST_BODY_BYTES` 和客户端上传内容 |
| 外部依赖失败 | 区分工学云、AI、地图、SMTP、代理接口 |

## 批量任务卡住

| 检查项 | 看什么 | 处理 |
|--------|--------|------|
| 任务状态 | `/batch-jobs/{id}` 的 `queued`、`running`、`failed` | 判断是未认领、执行中还是失败堆积 |
| running 长时间不动 | `BATCH_RUNNING_ITEM_TIMEOUT_SECONDS`、worker 日志 | 等待 lease 回收或重启 worker 后观察 |
| failed 增多 | 失败原因、远端返回、代理切换次数 | 先 `retry-failed`，再决定继续 / 暂停 / 取消 |
| queued 不减少 | worker 是否运行、`APP_ROLE=worker`、数据库连接 | 恢复 worker 或修正角色配置 |
| 活跃任务过多 | `BATCH_JOB_MAX_USERS`、`BATCH_TENANT_MAX_ACTIVE_JOBS`、幂等键 | 调整容量或清理重复提交 |

## AI 生成失败

| 检查项 | 预期 |
|--------|------|
| 系统设置 | 管理端“系统设置 -> AI 设置”已保存 API URL、API Key、Model |
| Key 回显 | 读取接口只返回 `hasApiKey`，看不到明文 Key 是预期行为 |
| 测试接口 | `/settings/ai/test` 能连通；`/ai/test` 只是兼容入口 |
| Host 白名单 | `AI_ALLOWED_HOSTS` 覆盖目标 host |
| 内网模型 | 必须同时设置 `ALLOW_PRIVATE_AI_ENDPOINTS=true` 和明确的 `AI_ALLOWED_HOSTS` |
| 模型白名单 | `AI_ALLOWED_MODELS` 包含目标模型 |
| 输出长度 | `AI_MAX_OUTPUT_TOKENS` 足够但不过大 |
| 回放对比 | `AI_PROMPT_VERSION` 一致 |

正式 AI 生成默认拒绝本机、内网、链路本地和特殊地址，并会固定已校验 DNS 解析结果，避免校验后解析漂移。

## 支付宝预授权与即时验证异常

先区分是预授权流程还是没有可用预授权后的即时验证流程。不要从日志中寻找登记编号或完整 URL；这些字段按设计不会写入日志。

| 现象 | 判断 | 处理 |
|------|------|------|
| 打卡响应 `msg == "304"` 却显示成功 | 业务响应被错误归类 | 检查 `ApiClient` 是否返回 `verification_required`，任务层不得把它映射为成功 |
| 预授权列表返回 400 | `planInfo.endTime`、打卡星期或上下班时间无效 | 先同步实习计划，再检查用户打卡配置；不要手工插入日期 |
| 开始授权返回「已有有效预授权」 | 同日期、同类型已是 `authorized` | 直接使用现有记录；如需重新登记，先确认当前状态不是有效授权 |
| 连续授权返回 429 | 同一 IP 和用户 1 分钟内开始或完成超过 30 次 | 等待当前 60 秒窗口结束，不要切换账号或绕过用户范围 |
| 两种打开按钮均禁用 | 开始响应 URL 不符合允许的协议或域名 | 检查上游登记响应；后端只接受 `alipays://`，前端不得绕过校验 |
| 浏览器方式没有拉起支付宝 | 浏览器拦截跳转或设备未安装支付宝 | 尝试直接「支付宝打开」；仍失败时重新发起登记 |
| 点击「我已完成授权」返回 400 | 票据超过 30 分钟、被篡改或用户 / 日期不匹配 | 关闭弹窗后重新开始授权，不复用旧票据 |
| 状态为 `consumed` | 凭据已被一次定时或手动任务原子占用 | 查看脱敏任务状态；需要再次使用时重新授权 |
| 状态为 `reauthorize_required` | 携带预授权凭据重试后仍返回 `304` | 重新授权；系统不会自动发起第 3 次打卡请求 |
| 授权仍为 `authorized`，但某次打卡成功 | 首次打卡没有触发 `304` | 这是预期行为；正常成功不会读取或消费预授权 |
| 首次 `304` 后出现即时验证弹窗 | 当天、类型没有 `authorized` 凭据，或凭据已被并发任务取得 | 完成即时验证，或为后续日期重新预授权 |
| 即时继续后再次要求验证 | 即时登记过期或远端要求重新登记 | 使用后端返回的新登记信息，不自动循环重试 |
| 用户端返回 401 / 404 | 登录态失效、未绑定用户或租户不匹配 | 重新登录并检查绑定关系，不改用管理端接口绕过 |
| 管理端返回 403 / 404 | 缺少 `tasks:run` 权限或目标用户不在当前租户 | 修正权限或用户范围 |

只查看不含凭据的状态汇总：

```sql
SELECT status, COUNT(*)
FROM clockinpreauthorization
WHERE tenant_id = '<tenant>' AND user_id = <user_id>
GROUP BY status;
```

预授权的 `outRegisterNo` 在用户确认后明文保存在 `ClockInPreauthorization`；即时验证登记编号只存在于当次 HTTP 响应和页面内存。两类登记编号以及 `registerUrl`、签名票据均不得进入日志、审计详情、最近执行结果、Server 酱或邮件通知。

## 预授权迁移与备份

发布前确认数据库已升级到 `20260715_0003_clockin_preauthorization`：

```powershell
python -m alembic current
python -m alembic upgrade head
```

| 现象 | 检查 | 处理 |
|------|------|------|
| 启动提示 Alembic 非 head | `python -m alembic current` | 停止流量后执行 `upgrade head`，不要依赖生产运行时改表 |
| 历史用户预授权起始日异常 | `User.created_at`、最早 `user.create` 审计、`schedule.startDate` | 按迁移回填优先级核对，不直接改预授权日期行 |
| 恢复后预授权缺失 | 备份 manifest、表校验和、恢复顺序 | 确认备份包含 `clockinpreauthorization`，并先恢复用户表 |
| 明文备份被拒绝 | `BACKUP_ENCRYPTION_KEY`、`ALLOW_PLAINTEXT_BACKUP` | 生产优先配置备份加密密钥，不因登记编号明文存储而关闭备份保护 |

降级到 `20260530_0002` 会删除预授权表和 `User.created_at`。只在临时验证库或明确接受数据丢失的回滚中执行。生产回滚前必须先导出备份。

## GHCR 镜像验签

Docker Publish 使用 GitHub OIDC 为 GHCR digest 执行无密钥签名。部署指定 digest 前可运行：

```bash
cosign verify \
  --certificate-identity-regexp "https://github.com/27xk/gongxueyun/.github/workflows/docker-publish.yml@refs/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "ghcr.io/27xk/gongxueyun@sha256:<digest>"
```

验签必须同时满足仓库工作流身份和 GitHub Actions OIDC issuer。只有镜像能拉取但验签失败时，应检查 digest、工作流身份、签名步骤和发布 Run，不要改为跳过验签。

## 权限灰度

| 操作 | 要求 |
|------|------|
| 临时灰度 | 只使用 `ROLE_PERMISSIONS_JSON` 做短期覆盖 |
| 验证范围 | 管理员、用户、批量任务、审计日志、系统设置、用户端接口 |
| 结束灰度 | 清空 `ROLE_PERMISSIONS_JSON`，回到内置权限策略 |
| 事故回滚 | 保留审计日志和变更记录，不要只改前端隐藏菜单 |
