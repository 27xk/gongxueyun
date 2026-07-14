# 支付宝打卡安全验证实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 正确处理工学云打卡 `msg == "304"`，获取支付宝验证登记信息，并让用户端和管理端在用户完成验证后显式继续打卡。

**架构：** `ApiClient` 把打卡响应归一化为成功或待验证业务结果；`perform_clock_in()` 把待验证映射为标准失败结果；`server/api.py` 通过共享业务函数提供两套权限隔离的继续接口。Vue 共享对话框仅展示后端校验过的 `alipays://` 深链，登记数据只保存在页面内存中。

**技术栈：** Python 3.10、FastAPI、Pydantic、SQLModel、unittest、Vue 3 Composition API、Element Plus、Axios、Vite

---

## 文件职责

- 修改：`server/coreApi/MainLogicApi.py`：登记支付宝验证、归一化打卡业务结果、携带 `outRegisterNo` 继续提交，并移除敏感调试输出。
- 修改：`server/task_runner.py`：识别待验证业务结果，普通打卡返回验证详情，补卡仅返回失败。
- 修改：`server/api.py`：校验继续请求、共享继续业务、用户端和管理端路由、限流与脱敏审计。
- 修改：`server/user_runtime.py`：持久化任务结果时剔除支付宝登记编号和深链，响应对象保持不变。
- 修改：`server/util/MessagePush.py`：生成 Server 酱和邮件内容前复用任务结果脱敏，禁止验证资料流出授权响应。
- 创建：`tests/test_alipay_clockin_verification.py`：覆盖 ApiClient、任务映射、继续业务和接口接线。
- 创建：`tests/test_frontend_alipay_verification.py`：静态验证共享组件和两端页面接线。
- 创建：`web/src/components/AlipayVerificationDialog.vue`：共享支付宝验证交互，不持久化登记信息。
- 修改：`web/src/views/user/UserHome.vue`：识别用户端运行结果并调用用户端继续接口。
- 修改：`web/src/views/UserList.vue`：识别管理端运行结果并调用目标用户的继续接口。

### 任务 1：ApiClient 支付宝登记与打卡业务结果

**文件：**
- 修改：`server/coreApi/MainLogicApi.py:488-600,932-1004`
- 创建：`tests/test_alipay_clockin_verification.py`

- [x] **步骤 1：编写登记接口和首次 `304` 的失败测试**

```python
def test_create_alipay_verification_extracts_safe_registration(self):
    client = build_api_client()
    client._post_request = Mock(return_value={
        "code": 200,
        "msg": "success",
        "data": {
            "outRegisterNo": "test-123-AbCd",
            "registerUrl": "alipays://platformapi/startapp?appId=test",
        },
    })

    result = client.create_alipay_clockin_verification()

    self.assertEqual(result["outRegisterNo"], "test-123-AbCd")
    self.assertTrue(result["registerUrl"].startswith("alipays://"))
    path, headers, payload = client._post_request.call_args.args
    self.assertEqual(path, "usercenter/alipay/v1/createAxdjk")
    self.assertIn("authorization", headers)
    self.assertEqual(set(payload), {"t"})

def test_initial_304_creates_registration_without_retrying_clockin(self):
    client = build_api_client()
    client._post_request = Mock(return_value={"code": 200, "msg": "304", "data": "verify"})
    client.create_alipay_clockin_verification = Mock(return_value=SAFE_REGISTRATION)

    result = client.submit_clock_in(build_checkin_info())

    self.assertEqual(result["status"], "verification_required")
    self.assertEqual(client._post_request.call_count, 1)
    client.create_alipay_clockin_verification.assert_called_once_with()
```

- [x] **步骤 2：运行专项测试并确认因方法缺失或返回值为 `None` 而失败**

运行：`python -m unittest discover -s tests -p "test_alipay_clockin_verification.py" -v`

预期：FAIL，`create_alipay_clockin_verification` 不存在，且提交方法尚未返回结构化结果。

- [x] **步骤 3：实现登记校验和结构化打卡结果**

```python
def create_alipay_clockin_verification(self) -> Dict[str, str]:
    response = self._post_request(
        "usercenter/alipay/v1/createAxdjk",
        self._get_authenticated_headers(),
        {"t": aes_encrypt(str(int(time.time() * 1000)))},
    )
    data = response.get("data") if isinstance(response, dict) else None
    if response.get("code") != 200 or response.get("msg") != "success" or not isinstance(data, dict):
        raise ValueError("创建支付宝安全验证失败")
    out_register_no = data.get("outRegisterNo")
    register_url = data.get("registerUrl")
    if not isinstance(out_register_no, str) or not out_register_no.strip() or not isinstance(register_url, str):
        raise ValueError("支付宝安全验证响应不完整")
    if not register_url.startswith("alipays://"):
        raise ValueError("支付宝安全验证链接无效")
    return {"outRegisterNo": out_register_no.strip(), "registerUrl": register_url}
```

同时执行以下最小调整：

- `_post_request()` 对所有 `code == 200` 响应直接返回，让业务方法处理 `302` 和 `304`。
- `_submit_clock_in_payload()` 返回 `{"status": "success"}` 或 `verification_required`；`302` 仍只在验证码成功后提交一次重试。
- 初次 `304` 只调用一次打卡接口和一次登记接口，不自动把新登记号重试到打卡接口。
- `checkin_info["outRegisterNo"]` 非空时写入 payload，显式继续仍只提交一次。
- 保留当前 `Dart/3.7`、新增 payload 键和省略 `lastDetailAddress` 的协议适配，删除 URL、headers、payload、response 的 `print()`。

- [x] **步骤 4：补充非法深链、缺字段、正常成功和继续 payload 测试并运行**

运行：`python -m unittest discover -s tests -p "test_alipay_clockin_verification.py" -v`

预期：PASS；测试确认 `https://` 深链被拒绝、异常文本不包含完整深链、继续 payload 含 `outRegisterNo` 且打卡只调用一次。

### 任务 2：任务执行层待验证映射

**文件：**
- 修改：`server/task_runner.py:291-437`
- 测试：`tests/test_alipay_clockin_verification.py`

- [x] **步骤 1：编写普通打卡、补卡和显式继续的失败测试**

```python
def test_perform_clockin_maps_verification_to_fail_details(self):
    api_client = build_task_api_client()
    api_client.submit_clock_in.return_value = {
        "status": "verification_required",
        **SAFE_REGISTRATION,
    }

    result = perform_clock_in(api_client, build_task_config(), forced_checkin_type="START")

    self.assertEqual(result["status"], "fail")
    self.assertEqual(result["details"]["outRegisterNo"], SAFE_REGISTRATION["outRegisterNo"])
    self.assertEqual(result["details"]["target_type"], "START")

def test_replace_304_is_not_reported_as_success(self):
    # replace=True 时状态必须为 fail，且 details 不提供登记号和深链。
```

- [x] **步骤 2：运行任务层测试并确认当前仍误报成功**

运行：`python -m unittest discover -s tests -p "test_alipay_clockin_verification.py" -v`

预期：FAIL，实际状态仍为 `success`。

- [x] **步骤 3：实现待验证映射和继续参数**

```python
def perform_clock_in(..., replace: bool = False, out_register_no: Optional[str] = None):
    checkin_info = {...}
    if out_register_no:
        checkin_info["outRegisterNo"] = out_register_no
    submit_result = (
        api_client.submit_clock_in_replace(checkin_info)
        if replace else api_client.submit_clock_in(checkin_info)
    )
    if submit_result.get("status") == "verification_required":
        details = {"target_type": checkin_type}
        if not replace:
            details.update({
                "outRegisterNo": submit_result["outRegisterNo"],
                "registerUrl": submit_result["registerUrl"],
            })
        return {
            "status": "fail",
            "message": "需要完成支付宝安全验证，请验证后继续打卡",
            "task_type": "打卡",
            "details": details,
        }
```

- [x] **步骤 4：运行任务层和既有补卡测试**

运行：`python -m unittest discover -s tests -p "test_alipay_clockin_verification.py" -v`，随后运行 `python -m unittest discover -s tests -p "test_task_runner_makeup_delay.py" -v`

预期：PASS，普通待验证携带登记信息，补卡待验证不携带登记信息且不会成功。

### 任务 3：两套显式继续 HTTP 接口

**文件：**
- 修改：`server/api.py:1-84,478-496,809-844,1486-1520,3176-3263`
- 修改：`server/user_runtime.py:214-241`
- 修改：`server/util/MessagePush.py:85-150`
- 测试：`tests/test_alipay_clockin_verification.py`

- [x] **步骤 1：编写请求校验、共享业务和路由失败测试**

```python
def test_continue_request_rejects_invalid_registration_number(self):
    with self.assertRaises(HTTPException) as ctx:
        api._alipay_continue_values(api.AlipayClockInContinueRequest(
            out_register_no="bad value/?",
            target_type="START",
        ))
    self.assertEqual(ctx.exception.status_code, 400)

def test_continue_business_passes_registration_to_clockin_once(self):
    with patch.object(api, "perform_clock_in", return_value=SUCCESS_RESULT) as perform:
        result, _ = api._continue_alipay_clockin_for_user(user, "safe-123", "START")
    self.assertEqual(result, SUCCESS_RESULT)
    perform.assert_called_once()
    self.assertEqual(perform.call_args.kwargs["out_register_no"], "safe-123")
```

路由测试同时验证：

- 路由集合包含 `/app/clock-in/alipay/continue` 和 `/users/{user_id}/clock-in/alipay/continue`。
- 用户端通过 `_get_bound_task_user()` 绑定关系取用户。
- 管理端通过 `_get_active_user_for_payload()` 保持 `tasks:run` 权限与租户隔离。
- 两端复用现有 `app_run` / `run` 限流桶。
- 审计详情只有 `status` 和 `target_type`，不含登记编号或深链。
- `apply_execution_results_to_user()` 存储任务结果副本，并从副本中移除 `outRegisterNo` 和 `registerUrl`；当次 HTTP 响应对象保持完整。
- 消息推送的 Markdown 和 HTML 生成器使用同一脱敏副本，推送内容不包含登记编号或深链。

- [x] **步骤 2：运行 API 测试并确认接口和共享函数尚不存在**

运行：`python -m unittest discover -s tests -p "test_alipay_clockin_verification.py" -v`

预期：FAIL，继续请求模型、共享函数和路由尚不存在。

- [x] **步骤 3：实现校验、共享业务与两个路由**

```python
ALIPAY_OUT_REGISTER_NO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")

class AlipayClockInContinueRequest(BaseModel):
    out_register_no: str
    target_type: str = "START"

def _alipay_continue_values(req):
    out_register_no = str(req.out_register_no or "").strip()
    target_type = str(req.target_type or "").strip().upper()
    if not ALIPAY_OUT_REGISTER_NO_PATTERN.fullmatch(out_register_no):
        raise HTTPException(status_code=400, detail="支付宝验证登记编号格式错误")
    if target_type not in ("START", "END"):
        raise HTTPException(status_code=400, detail="打卡类型错误")
    return out_register_no, target_type
```

共享业务从 `user_to_config()` 重建配置，执行 `_ensure_remote_runtime()`，然后调用一次 `perform_clock_in(..., out_register_no=...)` 并同步用户运行状态。路由返回 `{"result": result}`，写入脱敏审计并提交事务。

- [x] **步骤 4：运行继续 API 测试和 OpenAPI 合同测试**

运行：`python -m unittest discover -s tests -p "test_alipay_clockin_verification.py" -v`，随后运行 `python -m unittest discover -s tests -p "test_openapi_contract.py" -v`

预期：PASS。

### 任务 4：共享 Vue 对话框与两端接入

**文件：**
- 创建：`web/src/components/AlipayVerificationDialog.vue`
- 修改：`web/src/views/user/UserHome.vue`
- 修改：`web/src/views/UserList.vue`
- 创建：`tests/test_frontend_alipay_verification.py`

- [x] **步骤 1：编写前端接线失败测试**

```python
def test_user_and_admin_pages_use_shared_verification_dialog(self):
    component = read("web/src/components/AlipayVerificationDialog.vue")
    user_home = read("web/src/views/user/UserHome.vue")
    user_list = read("web/src/views/UserList.vue")
    self.assertIn("alipays://", component)
    self.assertIn("/app/clock-in/alipay/continue", user_home)
    self.assertIn("/users/${verificationUserId.value}/clock-in/alipay/continue", user_list)
    self.assertNotIn("localStorage", component + user_home + user_list)
```

- [x] **步骤 2：运行静态接线测试并确认组件和接口调用缺失**

运行：`python -m unittest tests.test_frontend_alipay_verification -v`

预期：FAIL，组件文件不存在。

- [x] **步骤 3：创建共享对话框**

组件接口：

```javascript
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  registerUrl: { type: String, default: '' },
  continuing: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'continue'])
const safeRegisterUrl = computed(() => props.registerUrl.startsWith('alipays://') ? props.registerUrl : '')
```

组件展示“需要支付宝安全验证”，提供“前往支付宝验证”和“验证完成，继续打卡”。继续期间禁用关闭和重复提交；组件不接触 Token、用户 ID、登记号或存储 API。

- [x] **步骤 4：接入用户端和管理端页面**

两端新增页面内存状态，使用同一解析规则从任务结果读取验证信息：

```javascript
const verificationFromResults = (results) => (results || []).find((item) =>
  item?.status === 'fail' &&
  item?.details?.outRegisterNo &&
  String(item?.details?.registerUrl || '').startsWith('alipays://')
)
```

用户端从 `/app/run` 的响应中立即打开对话框，继续时调用 `/app/clock-in/alipay/continue`。管理端保存当前用户 ID，从 `/users/{id}/run` 的响应打开对话框，继续时调用对应管理接口。成功关闭并刷新；再次待验证替换内存中的登记信息；普通失败保留对话框并显示后端错误。

- [x] **步骤 5：运行静态测试、前端质量门和构建**

运行：

```powershell
python -m unittest discover -s tests -p "test_frontend_alipay_verification.py" -v
Set-Location web
npm run lint
npm test
npm run build
```

预期：全部退出码为 0，Vite 构建成功。

### 任务 5：安全回归和全量验证

**文件：**
- 检查：`server/coreApi/MainLogicApi.py`
- 检查：`server/api.py`
- 检查：`web/src/components/AlipayVerificationDialog.vue`
- 检查：`web/src/views/user/UserHome.vue`
- 检查：`web/src/views/UserList.vue`

- [x] **步骤 1：运行敏感信息和占位符扫描**

运行：

```powershell
rg -n 'print\((headers|data|response|url)\)|authorization.*print|registerUrl.*AuditLog|out_register_no.*detail' server tests web/src
$placeholderPattern = 'TO' + 'DO|待' + '定|后续' + '实现'
rg -n $placeholderPattern docs/superpowers/plans/2026-07-15-alipay-clockin-verification.md
```

预期：第一条不命中敏感日志，第二条不命中占位符。

- [x] **步骤 2：运行后端专项和全量验证**

运行：

```powershell
python -m unittest discover -s tests -p "test_*alipay*verification.py" -v
python -m unittest discover -s tests
python -m compileall server
python scripts/quality_gate.py
python scripts/verify_supply_chain_policy.py
python scripts/backup_restore_drill.py
pip-audit -r server/requirements.txt
```

预期：所有命令退出码为 0，无失败测试。

- [x] **步骤 3：运行前端验证**

运行：

```powershell
Set-Location web
npm audit --audit-level=high
npm run lint
npm test
npm run build
Set-Location ..
```

结果：`npm ci`、官方 registry 审计、lint、test 和 build 退出码均为 0，审计为 `0 vulnerabilities`。

- [ ] **步骤 3b：运行 Docker 镜像验证**

运行：`docker build --build-arg DOWNLOAD_MODELS=0 -t automoguding-saas:alipay-verification-test .`

环境结果：当前机器未安装 `docker`、`podman` 或 `nerdctl`，命令无法启动；不得将本项报告为已通过。

- [x] **步骤 4：审查最终差异并提交**

运行：

```powershell
git diff --check
git status --short
git diff --stat
git diff -- server/coreApi/MainLogicApi.py server/task_runner.py server/api.py server/user_runtime.py server/util/MessagePush.py web/src/components/AlipayVerificationDialog.vue web/src/views/user/UserHome.vue web/src/views/UserList.vue tests/test_alipay_clockin_verification.py tests/test_frontend_alipay_verification.py
git add docs/superpowers/plans/2026-07-15-alipay-clockin-verification.md server/coreApi/MainLogicApi.py server/task_runner.py server/api.py server/user_runtime.py server/util/MessagePush.py web/src/components/AlipayVerificationDialog.vue web/src/views/user/UserHome.vue web/src/views/UserList.vue tests/test_alipay_clockin_verification.py tests/test_frontend_alipay_verification.py
git commit -m "feat(打卡): 支持支付宝验证后继续打卡"
```

预期：差异只包含本需求及已确认需要保留的协议适配，提交成功，不自动推送。
