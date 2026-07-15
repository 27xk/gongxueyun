# 打卡预授权实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在用户端和管理端提供按实习计划动态生成的打卡预授权列表，并在普通打卡或补卡真实返回 `304` 时安全消费匹配的 `outRegisterNo` 重试一次。

**架构：** 新增独立预授权模型和领域服务，虚拟列表按用户创建日、`planInfo.endTime`、打卡星期及时间实时计算，只持久化用户确认后的凭据。任务层通过凭据 Hook 原子占用记录，底层工学云客户端只报告 `304`，由编排层选择预授权或现有即时验证流程。前端使用双端共享的扁平表格页面与授权对话框。

**技术栈：** Python 3.12、FastAPI、SQLModel、SQLAlchemy、Alembic、MySQL、Vue 3、Vue Router、Element Plus、Axios、unittest、Vite、Docker。

---

## 文件结构

### 新建文件

- `server/clockin_preauthorization.py`：日期列表、支付宝 URL、签名票据、数据库状态转换和任务 Hook。
- `server/migrations/versions/20260715_0003_clockin_preauthorization.py`：用户创建时间与预授权表迁移。
- `tests/test_clockin_preauthorization.py`：领域规则、URL、票据、数据库生命周期和并发测试。
- `tests/test_clockin_preauthorization_api.py`：用户端和管理端接口、权限、限流、同步计划和敏感字段测试。
- `tests/test_frontend_clockin_preauthorization.py`：前端路由、共享组件、双端 API 前缀和敏感字段静态契约。
- `web/src/components/ClockInPreauthorizationPage.vue`：双端共享的列表、筛选、分页和授权状态管理。
- `web/src/components/ClockInPreauthorizationDialog.vue`：两种打开方式及显式完成确认。
- `web/src/views/user/UserPreauthorizations.vue`：用户端页面包装器。
- `web/src/views/UserPreauthorizations.vue`：管理端页面包装器。

### 修改文件

- `server/models.py`：新增 `User.created_at` 和 `ClockInPreauthorization`。
- `server/backup.py`：将预授权表加入备份恢复顺序。
- `server/auth.py`：允许内部短期票据携带受控附加声明。
- `server/coreApi/MainLogicApi.py`：将 `304` 检测与创建支付宝登记解耦。
- `server/task_runner.py`：首次请求、凭据 Hook、单次重试和补卡透传。
- `server/scheduler.py`：定时任务注入用户级预授权 Hook。
- `server/api.py`：Pydantic 模型、双端预授权接口及手动任务 Hook。
- `server/user_runtime.py`：补充预授权相关敏感字段剔除规则。
- `tests/test_migrations_contract.py`：验证新迁移及历史用户回填。
- `tests/test_platform_foundations.py`：验证预授权记录备份恢复。
- `tests/test_alipay_clockin_verification.py`：更新 `304` 编排测试并覆盖预授权重试。
- `tests/test_runtime_and_report_force.py`：覆盖运行入口 Hook 透传。
- `web/src/router/index.js`：新增用户端和管理端页面路由。
- `web/src/views/user/UserLayout.vue`：新增用户端「预授权」导航。
- `web/src/views/UserList.vue`：新增管理端用户预授权入口。
- `web/src/views/UserEdit.vue`：编辑用户时新增预授权入口。
- `docs/api/openapi-contract.json`：更新稳定 OpenAPI 契约。
- `README.md`、`server/README.md`、`web/README.md`、`docs/current-features.md`、`docs/ops/runbook.md`、`CHANGELOG.md`：同步功能、接口、状态及运维说明。

## 任务 1：数据库模型、迁移与备份

**文件：**

- 修改：`server/models.py`
- 创建：`server/migrations/versions/20260715_0003_clockin_preauthorization.py`
- 修改：`server/backup.py`
- 修改：`tests/test_migrations_contract.py`
- 修改：`tests/test_platform_foundations.py`
- 创建：`tests/test_clockin_preauthorization.py`

- [x] **步骤 1：编写模型和迁移失败测试**

在 `tests/test_clockin_preauthorization.py` 中断言模型默认值和唯一约束：

```python
def test_preauthorization_model_defaults_to_authorized(self):
    row = ClockInPreauthorization(
        user_id=7,
        target_date=datetime.date(2026, 7, 16),
        target_type="START",
        out_register_no="register-1",
        authorized_at=utc_now(),
    )
    self.assertEqual(row.status, "authorized")
    self.assertIsNone(row.consumed_at)
```

在 `tests/test_migrations_contract.py` 中执行 `alembic upgrade head` 后断言：

```python
self.assertIn("created_at", user_columns)
self.assertIn("clockinpreauthorization", tables)
self.assertIn("out_register_no", preauthorization_columns)
```

在 `tests/test_platform_foundations.py` 的备份恢复场景中插入一条预授权记录，恢复后断言 `out_register_no`、日期和状态保持一致。

- [x] **步骤 2：运行测试确认失败**

运行：

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_clockin_preauthorization.py"
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_migrations_contract.py"
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_platform_foundations.py"
```

预期：导入 `ClockInPreauthorization` 失败，迁移表不存在。

- [x] **步骤 3：实现模型和迁移**

在 `server/models.py` 中增加：

```python
class ClockInPreauthorization(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "target_date", "target_type",
            name="uq_clockinpreauthorization_target",
        ),
        Index(
            "ix_clockinpreauthorization_user_date",
            "tenant_id", "user_id", "target_date",
        ),
        Index(
            "ix_clockinpreauthorization_user_status_date",
            "tenant_id", "user_id", "status", "target_date",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    user_id: int = Field(index=True)
    target_date: datetime.date = Field(index=True)
    target_type: str = Field(index=True)
    status: str = Field(default="authorized", index=True)
    out_register_no: str
    authorized_at: datetime.datetime
    consumed_at: Optional[datetime.datetime] = None
    used_target_type: Optional[str] = None
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime.datetime = Field(default_factory=utc_now, index=True)
```

同时为 `User` 增加 `created_at`。迁移先以可空列创建，按「最早 `user.create` 审计时间 → `schedule.startDate` → 当前 UTC」逐行回填，再改为非空。迁移根据 `op.get_bind().dialect.name` 使用兼容 SQLite 测试和 MySQL 生产的批量改表。

将 `ClockInPreauthorization` 放在 `User` 之后、`AuditLog` 之前加入 `BACKUP_MODELS`，确保恢复时用户先存在。

- [x] **步骤 4：运行聚焦测试确认通过**

运行上一步测试命令。

预期：所有模型、迁移和备份测试通过。

- [x] **步骤 5：提交数据库变更**

```powershell
git add server/models.py server/migrations/versions/20260715_0003_clockin_preauthorization.py server/backup.py tests/test_clockin_preauthorization.py tests/test_migrations_contract.py tests/test_platform_foundations.py
git commit -m "feat(打卡): 添加预授权数据模型"
```

## 任务 2：动态列表、URL 与签名票据

**文件：**

- 创建：`server/clockin_preauthorization.py`
- 修改：`server/auth.py`
- 修改：`tests/test_clockin_preauthorization.py`

- [x] **步骤 1：编写纯领域失败测试**

覆盖以下输入输出：

```python
def test_build_rows_splits_past_and_future(self):
    rows = build_preauthorization_rows(
        added_date=datetime.date(2026, 7, 13),
        plan_end_date=datetime.date(2026, 7, 16),
        today=datetime.date(2026, 7, 15),
        weekdays=[1, 2, 3, 4, 5],
        start_time="08:30",
        end_time="18:30",
    )
    self.assertEqual(
        [(row.target_date.isoformat(), row.target_type) for row in rows],
        [
            ("2026-07-13", "MAKEUP"),
            ("2026-07-14", "MAKEUP"),
            ("2026-07-15", "START"),
            ("2026-07-15", "END"),
            ("2026-07-16", "START"),
            ("2026-07-16", "END"),
        ],
    )
```

```python
def test_build_open_urls_replaces_nested_callback(self):
    direct_url, browser_url = build_alipay_open_urls(
        RAW_ALIPAY_URL,
        account="13800000000",
        started_at=BEIJING_TIME,
    )
    direct_query = dict(parse_qsl(urlsplit(direct_url).query))
    callback = urlsplit(direct_query["thirdPartSchema"])
    callback_query = dict(parse_qsl(callback.query))
    self.assertIn("13800000000", callback_query["query"])
    self.assertIn("2026-07-15 18:30:00", callback_query["query"])
    self.assertEqual(dict(parse_qsl(urlsplit(browser_url).query))["scheme"], direct_url)
```

票据测试使用固定 `APP_SECRET`，验证 30 分钟过期、篡改、用途、租户、用户、日期和类型。

- [x] **步骤 2：运行测试确认失败**

运行：

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_clockin_preauthorization.py"
```

预期：领域函数尚不存在。

- [x] **步骤 3：实现最少领域代码**

在 `server/auth.py` 为内部调用增加受控附加声明：

```python
def issue_token(..., extra_claims: dict | None = None) -> str:
    payload = {...}
    for key, value in (extra_claims or {}).items():
        if key not in {"sub", "role", "tenant_id", "exp", "ver"}:
            payload[key] = value
```

在 `server/clockin_preauthorization.py` 实现：

```python
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
VALID_TARGET_TYPES = {"START", "END", "MAKEUP"}

def parse_plan_end_date(plan_info: dict) -> datetime.date: ...
def build_preauthorization_rows(...) -> list[PreauthorizationRow]: ...
def build_alipay_open_urls(register_url: str, account: str, started_at: datetime.datetime) -> tuple[str, str]: ...
def issue_registration_ticket(..., ttl_seconds: int = 1800) -> str: ...
def verify_registration_ticket(ticket: str, *, tenant_id: str, user_id: int) -> dict: ...
```

URL 实现必须使用 `urlsplit`、`parse_qsl`、`urlencode`、`urlunsplit`，拒绝非 `alipays://` URL，并保证最终只有一个 `thirdPartSchema`。

- [x] **步骤 4：运行领域测试确认通过**

运行聚焦测试，预期全部通过。

- [x] **步骤 5：提交领域规则**

```powershell
git add server/auth.py server/clockin_preauthorization.py tests/test_clockin_preauthorization.py
git commit -m "feat(打卡): 实现预授权领域规则"
```

## 任务 3：凭据持久化、列表查询与原子消费

**文件：**

- 修改：`server/clockin_preauthorization.py`
- 修改：`tests/test_clockin_preauthorization.py`

- [x] **步骤 1：编写数据库生命周期失败测试**

使用 SQLite 内存数据库覆盖：

```python
def test_complete_ticket_persists_only_after_confirmation(self):
    self.assertEqual(self.session.exec(select(ClockInPreauthorization)).all(), [])
    item = complete_preauthorization(self.session, user=self.user, ticket=self.ticket)
    self.assertEqual(item.status, "authorized")
    self.assertEqual(item.out_register_no, "register-1")
```

```python
def test_claim_is_atomic_and_makeup_records_used_type(self):
    claim = claim_preauthorization(
        self.engine,
        tenant_id="default",
        user_id=self.user.id,
        target_date=datetime.date(2026, 7, 14),
        target_type="MAKEUP",
        used_target_type="END",
    )
    second = claim_preauthorization(...)
    self.assertEqual(claim.out_register_no, "register-1")
    self.assertIsNone(second)
    self.assertEqual(self.session.get(ClockInPreauthorization, claim.id).used_target_type, "END")
```

同时覆盖授权记录冲突、相同票据幂等、`consumed`/`reauthorize_required` 重新授权、状态汇总、分页和敏感字段不进入列表字典。

- [x] **步骤 2：运行测试确认失败**

运行任务 2 的聚焦测试命令。

预期：持久化和 claim 函数不存在。

- [x] **步骤 3：实现服务函数和 Hook**

实现以下公开边界：

```python
def list_preauthorizations(session: Session, user: User, *, scope: str, page: int, page_size: int, ...) -> dict: ...
def complete_preauthorization(session: Session, *, user: User, ticket: str) -> ClockInPreauthorization: ...
def claim_preauthorization(db_engine, *, tenant_id: str, user_id: int, target_date: date, target_type: str, used_target_type: str | None = None) -> PreauthorizationClaim | None: ...
def mark_preauthorization_reauthorization_required(db_engine, claim_id: int) -> None: ...
def build_preauthorization_hooks(user: User, db_engine=engine) -> ClockInPreauthorizationHooks: ...
```

claim 使用单条带 `status = 'authorized'` 条件的 `UPDATE` 或 `SELECT ... FOR UPDATE` 加条件更新；只有影响 1 行时返回凭据。返回对象不得写日志或进入普通执行结果。

- [x] **步骤 4：运行测试确认通过**

运行领域测试，预期所有状态和并发测试通过。

- [x] **步骤 5：提交凭据生命周期**

```powershell
git add server/clockin_preauthorization.py tests/test_clockin_preauthorization.py
git commit -m "feat(打卡): 实现预授权凭据生命周期"
```

## 任务 4：重构 `304` 并接入普通打卡与补卡

**文件：**

- 修改：`server/coreApi/MainLogicApi.py`
- 修改：`server/task_runner.py`
- 修改：`server/user_runtime.py`
- 修改：`tests/test_alipay_clockin_verification.py`
- 修改：`tests/test_runtime_and_report_force.py`
- 修改：`tests/test_task_runner_makeup_delay.py`

- [ ] **步骤 1：编写首次无凭据、`304` 后单次重试失败测试**

在 `tests/test_alipay_clockin_verification.py` 增加：

```python
def test_preauthorized_clockin_sends_token_only_after_initial_304(self):
    client._post_request.side_effect = [
        {"code": 200, "msg": "304", "data": "verify"},
        {"code": 200, "msg": "success", "data": None},
    ]
    result = perform_clock_in(client, config, "START", preauthorization_hooks=hooks)
    self.assertNotIn("outRegisterNo", first_payload)
    self.assertEqual(second_payload["outRegisterNo"], "register-1")
    self.assertEqual(result["status"], "success")
```

再覆盖：无预授权时创建即时登记；二次 `304` 标记重新授权且无第三次请求；显式继续不查询预授权；补卡用 `MAKEUP` 和目标日期；批量补卡逐日透传 Hook；执行结果不含凭据。

- [ ] **步骤 2：运行测试确认失败**

运行：

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest tests.test_alipay_clockin_verification tests.test_runtime_and_report_force tests.test_task_runner_makeup_delay
```

预期：首次请求仍由底层客户端直接创建即时登记，Hook 参数不存在。

- [ ] **步骤 3：解耦底层 `304`**

将 `_submit_clock_in_payload` 的 `304` 分支改为：

```python
if response.get("msg") == "304":
    return {"status": "verification_required"}
```

`perform_clock_in` 新增 `preauthorization_hooks`。首次请求使用不含凭据的 payload；收到 `verification_required` 后：

1. 显式 `out_register_no` 存在时按现有继续流程处理。
2. 其余情况调用 Hook claim。
3. claim 成功后仅重试一次。
4. 第二次仍需验证时调用 `mark_reauthorization_required`。
5. claim 不存在时，普通打卡调用 `create_alipay_clockin_verification` 生成现有即时登记。

为 `perform_clock_in_makeup`、`perform_clock_in_makeup_many`、限流重试辅助函数和 `run_task_by_config` 逐层增加同名可选 Hook 参数。

把 `registration_ticket`、`direct_url`、`browser_url` 加入 `SENSITIVE_EXECUTION_FIELDS`。

- [ ] **步骤 4：运行打卡聚焦测试确认通过**

运行步骤 2 的命令，预期全部通过，并确认每个测试最多调用 2 次提交接口。

- [ ] **步骤 5：提交任务联动**

```powershell
git add server/coreApi/MainLogicApi.py server/task_runner.py server/user_runtime.py tests/test_alipay_clockin_verification.py tests/test_runtime_and_report_force.py tests/test_task_runner_makeup_delay.py
git commit -m "feat(打卡): 在 304 后消费预授权"
```

## 任务 5：双端 API、计划同步与所有运行入口

**文件：**

- 修改：`server/api.py`
- 修改：`server/scheduler.py`
- 创建：`tests/test_clockin_preauthorization_api.py`
- 修改：`tests/test_scheduler_hardening.py`
- 修改：`tests/test_alipay_clockin_verification.py`

- [ ] **步骤 1：编写双端 API 失败测试**

使用 FastAPI `TestClient` 和依赖覆盖验证 6 个路由：

```python
def test_app_start_returns_two_urls_without_persisting(self):
    response = client.post(
        "/api/app/clock-in/preauthorizations/start",
        json={"target_date": "2026-07-16", "target_type": "START"},
    )
    self.assertEqual(response.status_code, 200)
    self.assertTrue(response.json()["direct_url"].startswith("alipays://"))
    self.assertTrue(response.json()["browser_url"].startswith("https://ds.alipay.com/?"))
    self.assertEqual(session.exec(select(ClockInPreauthorization)).all(), [])
```

覆盖用户只能操作绑定账号、管理端租户隔离与 `tasks:run`、开始/完成限流、强制同步计划、无效结束日期、审计脱敏、列表响应脱敏。

为调度器增加测试，断言 `run_job` 调用 `run_task_by_config(..., preauthorization_hooks=...)`。为 `/app/run`、管理端立即运行、普通补卡和批量补卡增加 Hook 透传断言。

- [ ] **步骤 2：运行 API 测试确认失败**

运行：

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest tests.test_clockin_preauthorization_api tests.test_scheduler_hardening tests.test_alipay_clockin_verification
```

预期：路由返回 `404`，运行入口没有 Hook。

- [ ] **步骤 3：实现请求模型和共享处理函数**

在 `server/api.py` 增加：

```python
class ClockInPreauthorizationStartRequest(BaseModel):
    target_date: str
    target_type: str

class ClockInPreauthorizationCompleteRequest(BaseModel):
    registration_ticket: str
```

实现 `_list_clockin_preauthorizations_for_user`、`_start_clockin_preauthorization_for_user`、`_complete_clockin_preauthorization_for_user` 和 `_refresh_preauthorization_plan_if_needed`。开始接口复用 `_ensure_remote_runtime`，调用 `create_alipay_clockin_verification`，然后构建两个 URL 和签名票据；完成接口只消费票据并提交数据库事务。

添加 `/api/app/...` 与 `/api/users/{user_id}/...` 对称路由。开始和完成审计只写 `target_date`、`target_type`、状态。

在调度器和所有手动运行/补卡入口用 `build_preauthorization_hooks(user)` 注入 Hook；显式支付宝继续接口不注入。

- [ ] **步骤 4：运行 API 和调度器测试确认通过**

运行步骤 2 的命令，预期所有路由、权限、同步和 Hook 测试通过。

- [ ] **步骤 5：提交 API**

```powershell
git add server/api.py server/scheduler.py tests/test_clockin_preauthorization_api.py tests/test_scheduler_hardening.py tests/test_alipay_clockin_verification.py
git commit -m "feat(接口): 提供双端预授权 API"
```

## 任务 6：共享前端页面与授权对话框

**文件：**

- 创建：`web/src/components/ClockInPreauthorizationDialog.vue`
- 创建：`web/src/components/ClockInPreauthorizationPage.vue`
- 创建：`web/src/views/user/UserPreauthorizations.vue`
- 创建：`web/src/views/UserPreauthorizations.vue`
- 创建：`tests/test_frontend_clockin_preauthorization.py`

- [ ] **步骤 1：编写前端静态失败测试**

断言共享页面和对话框的关键契约：

```python
def test_shared_page_supports_two_api_prefixes_and_past_scope(self):
    source = read("web/src/components/ClockInPreauthorizationPage.vue")
    self.assertIn("/app/clock-in/preauthorizations", source)
    self.assertIn("/users/${props.userId}/clock-in/preauthorizations", source)
    self.assertIn("scope: 'past'", source)
    self.assertIn("ClockInPreauthorizationDialog", source)
    self.assertNotIn("localStorage", source)
```

```python
def test_dialog_has_two_open_methods_and_explicit_completion(self):
    source = read("web/src/components/ClockInPreauthorizationDialog.vue")
    self.assertIn("浏览器打开", source)
    self.assertIn("支付宝打开", source)
    self.assertIn("我已完成授权", source)
    self.assertIn("noopener,noreferrer", source)
    self.assertNotIn("outRegisterNo", source)
```

- [ ] **步骤 2：运行前端测试确认失败**

运行：

```powershell
python -m unittest tests.test_frontend_clockin_preauthorization
```

预期：组件文件不存在。

- [ ] **步骤 3：实现扁平表格页面**

`ClockInPreauthorizationPage.vue` 接收：

```javascript
const props = defineProps({
  mode: { type: String, required: true },
  userId: { type: Number, default: null },
})
```

根据 `mode` 选择 `userHttp` 或 `http`，并计算 API 基础路径。主列表默认 `scope=future`，过去区首次展开时请求 `scope=past`。桌面使用 `el-table`，移动端使用同数据的紧凑列表；两者均显示日期、时间、类型、状态、授权时间和操作。

开始接口返回的数据只存入 `ref`，关闭对话框即清除。完成接口只提交 `registration_ticket`，成功后刷新当前 scope。直接支付宝 URL 必须以 `alipays://` 开头，浏览器 URL 必须以 `https://ds.alipay.com/` 开头，否则禁用对应按钮。

- [ ] **步骤 4：运行前端测试、lint 和构建**

```powershell
python -m unittest tests.test_frontend_clockin_preauthorization
Set-Location web
npm run lint
npm test
npm run build
```

预期：静态契约、质量门和 Vite 构建全部通过。

- [ ] **步骤 5：提交共享页面**

```powershell
git add web/src/components/ClockInPreauthorizationDialog.vue web/src/components/ClockInPreauthorizationPage.vue web/src/views/user/UserPreauthorizations.vue web/src/views/UserPreauthorizations.vue tests/test_frontend_clockin_preauthorization.py
git commit -m "feat(前端): 添加预授权列表页面"
```

## 任务 7：双端路由、导航与用户入口

**文件：**

- 修改：`web/src/router/index.js`
- 修改：`web/src/views/user/UserLayout.vue`
- 修改：`web/src/views/UserList.vue`
- 修改：`web/src/views/UserEdit.vue`
- 修改：`tests/test_frontend_clockin_preauthorization.py`

- [ ] **步骤 1：扩展失败测试**

断言：

```python
self.assertIn("/u/preauthorizations", router_source)
self.assertIn("/users/:id/preauthorizations", router_source)
self.assertIn("预授权", user_layout_source)
self.assertIn("goPreauthorization", user_list_source)
self.assertIn("goPreauthorization", user_edit_source)
```

同时断言管理端路由要求 `tasks:run`，用户列表移动端下拉菜单有预授权命令。

- [ ] **步骤 2：运行测试确认失败**

运行前端静态测试，预期路由和入口断言失败。

- [ ] **步骤 3：实现路由和入口**

新增路由：

```javascript
{
  path: '/u/preauthorizations',
  component: () => import('../views/user/UserLayout.vue'),
  meta: { area: 'user' },
  children: [
    { path: '', component: () => import('../views/user/UserPreauthorizations.vue'), meta: { area: 'user' } },
  ],
},
{
  path: '/users/:id/preauthorizations',
  component: () => import('../views/UserPreauthorizations.vue'),
  meta: { permissions: ['tasks:run'] },
},
```

用户端导航增加「预授权」。用户列表桌面操作和移动端「更多」增加入口；用户编辑页仅在 `isEdit` 时显示入口。

- [ ] **步骤 4：运行前端完整验证**

运行：

```powershell
python -m unittest tests.test_frontend_clockin_preauthorization tests.test_frontend_alipay_verification
Set-Location web
npm run lint
npm test
npm run build
```

预期：路由、旧即时验证和构建全部通过。

- [ ] **步骤 5：提交双端入口**

```powershell
git add web/src/router/index.js web/src/views/user/UserLayout.vue web/src/views/UserList.vue web/src/views/UserEdit.vue tests/test_frontend_clockin_preauthorization.py
git commit -m "feat(前端): 接入双端预授权入口"
```

## 任务 8：OpenAPI 与正式文档

**文件：**

- 修改：`docs/api/openapi-contract.json`
- 修改：`README.md`
- 修改：`server/README.md`
- 修改：`web/README.md`
- 修改：`docs/current-features.md`
- 修改：`docs/ops/runbook.md`
- 修改：`CHANGELOG.md`
- 修改：`tests/test_openapi_contract.py`

- [ ] **步骤 1：运行 OpenAPI 检查确认快照过期**

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/openapi_contract.py
```

预期：失败并提示运行 `python scripts/openapi_contract.py --write`。

- [ ] **步骤 2：生成契约快照**

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/openapi_contract.py --write
```

检查快照包含 6 个预授权路由、开始/完成请求模型和分页字段，且不包含 `out_register_no` 响应字段。

- [ ] **步骤 3：更新正式文档**

文档明确记录：

- 用户创建日到 `planInfo.endTime` 的日期范围。
- 未来上/下班双项与过去单日补卡项。
- 浏览器与支付宝两种 URL。
- 「我已完成授权」前不持久化。
- `outRegisterNo` 明文保存，但不进入日志、审计、通知、执行历史或列表响应。
- 首次不带凭据、真实 `304` 后原子消费并只重试一次。
- 迁移、备份、常见错误和「需重新授权」排障。

- [ ] **步骤 4：验证文档和契约**

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest tests.test_openapi_contract
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/openapi_contract.py
git diff --check
```

预期：OpenAPI 与快照一致，Git 空白检查通过。

- [ ] **步骤 5：提交文档契约**

```powershell
git add docs/api/openapi-contract.json README.md server/README.md web/README.md docs/current-features.md docs/ops/runbook.md CHANGELOG.md tests/test_openapi_contract.py
git commit -m "docs(打卡): 补充预授权使用与运维说明"
```

## 任务 9：完整验证与业务逻辑缺陷审查

**文件：**

- 可能修改：任务 1-8 涉及的代码、测试和文档文件

- [ ] **步骤 1：运行完整后端验证**

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m compileall server
ruff check server scripts
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/quality_gate.py
python scripts/verify_supply_chain_policy.py
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/backup_restore_drill.py
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/openapi_contract.py
pip-audit -r server/requirements.txt
```

预期：所有命令退出码为 `0`。

- [ ] **步骤 2：运行完整前端验证**

```powershell
Set-Location web
npm audit --registry=https://registry.npmjs.org --audit-level=high
npm run lint
npm test
npm run build
```

预期：审计无高危漏洞，质量门和构建通过。

- [ ] **步骤 3：执行数据库与 Docker 验证**

在临时 MySQL 测试库执行：

```powershell
python -m alembic upgrade head
python -m alembic downgrade 20260530_0002
python -m alembic upgrade head
```

然后执行：

```powershell
docker build -t automoguding-saas:preauthorization-test .
```

预期：迁移可升级、降级、再升级，Docker 镜像构建成功。

- [ ] **步骤 4：启动应用并检查桌面/移动页面**

使用后端和 Vite 开发服务器启动功能分支。以管理员和用户身份分别检查：主列表、过去折叠区、开始授权对话框、两种 URL、取消、完成、刷新计划、空状态和错误状态。桌面宽度 `1440 × 900`、移动宽度 `390 × 844` 均不得出现文字遮挡、按钮溢出或横向页面滚动。

- [ ] **步骤 5：执行端到端业务逻辑审查**

逐项用测试或受控 Mock 重放以下链路：

1. 未确认授权时数据库不变。
2. 同一票据重复完成保持幂等。
3. 普通成功打卡完全不读取预授权。
4. 首次 `304`、有凭据、重试成功。
5. 首次 `304`、有凭据、重试仍 `304`。
6. 首次 `304`、无凭据、进入现有即时验证。
7. 过去日期补上班和补下班均使用 `MAKEUP`，但同一凭据只能成功占用一次。
8. 批量补卡每个日期独立取凭据，某日失败不误消费其他日期。
9. 定时任务与手动任务并发时只有一方取得凭据。
10. 修改星期、时间或计划结束日后，活动列表更新但历史记录保留。
11. 所有日志、审计、通知、执行历史和 API 列表不含登记编号或 URL。

发现缺陷时先增加最小复现测试，确认失败，再修复并重跑相关聚焦测试与完整套件。

- [ ] **步骤 6：提交审查修复和最终验证记录**

若审查产生代码修复：

```powershell
git add <对应代码和测试文件>
git commit -m "fix(打卡): 修复预授权业务边界缺陷"
```

最终确认：

```powershell
git status --short
git log --oneline --decorate -12
git diff main...HEAD --check
```

预期：工作区干净，提交历史按任务分组，分支差异无空白错误。
