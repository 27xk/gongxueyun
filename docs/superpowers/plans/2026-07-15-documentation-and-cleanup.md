# 文档更新与无用内容清理实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让正式文档与当前 `main` 的功能、接口、部署和 CI 保持一致，并删除静态确认无用的代码、完成计划和本地生成文件。

**架构：** 以运行入口、路由、导入关系、测试和 CI 为事实来源。先用 Ruff 的 `F401/F841` 结果完成无行为变更的代码清理，再按读者职责更新产品、开发和运维文档，最后删除已迁移内容和本地生成物并执行全量验证。

**技术栈：** Python 3.10、FastAPI、Ruff、unittest、Vue 3、Vite、Markdown、GitHub Actions、Docker Compose

---

## 文件职责

- 修改：`server/coreApi/AiServiceClient.py`：移除未使用的异常变量。
- 修改：`server/coreApi/MainLogicApi.py`：移除未使用的日志上下文导入。
- 修改：`server/models.py`：移除未使用的 Pydantic 导入。
- 修改：`server/rate_limit.py`：移除未使用的 SQLAlchemy 导入。
- 修改：`server/task_runner.py`：移除未使用的标准库和运行态导入。
- 修改：`server/util/FileUploader.py`：移除未使用的类型导入。
- 修改：`README.md`：面向使用者的产品、入口、核心流程、部署和验证总览。
- 修改：`server/README.md`：面向后端开发与运维的接口、任务链路和 CI 门禁说明。
- 修改：`web/README.md`：面向前端开发的页面、支付宝验证交互和构建命令。
- 修改：`docs/current-features.md`：当前功能、双端接口和业务状态的事实清单。
- 修改：`docs/ops/runbook.md`：CI、Docker Publish、支付宝验证和镜像验签排障。
- 修改：`CONTRIBUTING.md`：贡献验证矩阵、文档同步和敏感信息要求。
- 修改：`ROADMAP.md`：保留尚未完成且可验收的计划。
- 修改：`CHANGELOG.md`：补齐 2026 年 6 月、7 月交付记录。
- 删除：`docs/superpowers/specs/2026-07-15-alipay-clockin-verification-design.md`：已迁移的功能设计。
- 删除：`docs/superpowers/plans/2026-07-15-alipay-clockin-verification.md`：已完成的实现计划。
- 删除：`docs/superpowers/plans/2026-07-15-documentation-and-cleanup.md`：全部任务完成后删除本计划，避免当前树保留完成计划。

### 任务 1：清理静态确认的无用 Python 符号

**文件：**
- 修改：`server/coreApi/AiServiceClient.py:339`
- 修改：`server/coreApi/MainLogicApi.py:21`
- 修改：`server/models.py:4`
- 修改：`server/rate_limit.py:7`
- 修改：`server/task_runner.py:5,29`
- 修改：`server/util/FileUploader.py:5`

- [ ] **步骤 1：确认 Ruff 红灯只包含批准的 7 处**

运行：

```powershell
ruff check server scripts --select F401,F841 --output-format concise
```

预期：FAIL，恰好报告 `AiServiceClient.py` 1 处、`MainLogicApi.py` 1 处、`models.py` 1 处、`rate_limit.py` 1 处、`task_runner.py` 2 处、`FileUploader.py` 1 处。

- [ ] **步骤 2：实施最小清理**

执行以下明确修改：

```python
# server/coreApi/AiServiceClient.py
except Exception:
    logger.exception("解析响应发生异常")
    return None

# server/coreApi/MainLogicApi.py
# 删除：from server.util.LoggerContext import _log_ctx

# server/models.py
# 删除：from pydantic import BaseModel

# server/rate_limit.py
from sqlalchemy import delete

# server/task_runner.py
# 删除：import threading
# 从 server.user_runtime 导入列表删除 runtime_plan_required

# server/util/FileUploader.py
# 删除：from typing import Optional
```

- [ ] **步骤 3：验证 Ruff 绿灯和后端回归**

运行：

```powershell
ruff check server scripts --select F401,F841
$env:APP_ENV='test'
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

预期：Ruff 退出码为 0；后端 `283` 项测试通过。

- [ ] **步骤 4：提交代码清理**

```powershell
git add server/coreApi/AiServiceClient.py server/coreApi/MainLogicApi.py server/models.py server/rate_limit.py server/task_runner.py server/util/FileUploader.py
git commit -m "refactor(维护): 清理无用 Python 符号"
```

### 任务 2：更新产品、功能和前后端正式文档

**文件：**
- 修改：`README.md`
- 修改：`docs/current-features.md`
- 修改：`server/README.md`
- 修改：`web/README.md`

- [ ] **步骤 1：记录当前文档缺口**

运行：

```powershell
rg -n "alipay/continue|msg.*304|docker-compose.image|npm run build" README.md docs/current-features.md server/README.md web/README.md
```

预期：FAIL 或命中不完整；4 份文档没有同时覆盖支付宝继续接口、`304` 业务语义、预构建镜像和完整验证命令。

- [ ] **步骤 2：更新根 README**

在 `README.md` 完成以下具体修改：

- 能力矩阵的自动打卡行增加支付宝安全验证后显式继续。
- 入口速查明确 `/` 根据会话分流：管理员进入管理端、用户进入 `/u`、未登录进入 `/u/login`。
- 在核心流程中增加“支付宝安全验证”章节，写明 `msg == "304"` 不是成功、首次响应创建登记但不自动重试、用户验证后点击继续、再次 `304` 刷新登记。
- Docker Compose 表增加 `docker compose -f docker-compose.image.yml up -d`，说明 `APP_IMAGE` 和 `scripts/check-image-update.ps1`。
- 部署说明明确两套 Compose 都只启动 `app` 和 `worker`，MySQL 8 由 `DATABASE_URL` 指向外部实例。
- 增加后端、前端、依赖审计和 OpenAPI 快照验证命令。

- [ ] **步骤 3：更新当前功能和后端说明**

在 `docs/current-features.md` 和 `server/README.md` 增加以下事实：

```text
POST /app/clock-in/alipay/continue
POST /users/{user_id}/clock-in/alipay/continue
```

同时说明：

- `out_register_no` 必填，`target_type` 为 `START` 或 `END`。
- `msg == "304"` 映射为 `verification_required`，任务结果对调用端表现为可识别的失败，不得记录为打卡成功。
- `outRegisterNo` 和 `registerUrl` 不写入审计、持久化执行结果或通知。
- 补卡不进入继续打卡流程。
- CI 后端顺序为依赖审计、迁移、测试、编译、质量门和备份恢复演练。

- [ ] **步骤 4：更新前端说明**

在 `web/README.md` 记录：

- `AlipayVerificationDialog.vue` 是管理端和用户端共享组件。
- 用户端从 `/app/run` 识别验证结果并调用 `/app/clock-in/alipay/continue`。
- 管理端从 `/users/{id}/run` 识别验证结果并调用 `/users/{id}/clock-in/alipay/continue`。
- 登记信息只保存在页面内存；链接必须为 `alipays://`。
- 开发命令补齐 `npm ci`、`npm audit --audit-level=high`、`npm run lint`、`npm test` 和 `npm run build`。

- [ ] **步骤 5：验证核心文档覆盖并提交**

运行：

```powershell
rg -n "alipay/continue|msg.*304|docker-compose.image|npm run build" README.md docs/current-features.md server/README.md web/README.md
git diff --check
```

预期：4 份文档均有与职责相符的命中，`git diff --check` 退出码为 0。

提交：

```powershell
git add README.md docs/current-features.md server/README.md web/README.md
git commit -m "docs(功能): 同步当前接口与部署说明"
```

### 任务 3：更新运维与项目治理文档

**文件：**
- 修改：`docs/ops/runbook.md`
- 修改：`CONTRIBUTING.md`
- 修改：`ROADMAP.md`
- 修改：`CHANGELOG.md`

- [ ] **步骤 1：更新运行手册**

在 `docs/ops/runbook.md` 增加：

- Docker Publish 的实际步骤顺序和“后端测试失败时 Docker 尚未执行”的判断方法。
- OpenAPI 快照失败时使用 `python scripts/openapi_contract.py --write`，且必须使用 `server/requirements.txt` 对应 FastAPI 版本。
- 支付宝验证排障表：`304` 误报成功、登记接口失败、非法深链、继续后再次验证、敏感字段泄露。
- GHCR 镜像使用 cosign 的验证命令和证书身份约束。

- [ ] **步骤 2：更新贡献指南**

在 `CONTRIBUTING.md` 增加完整提交前验证矩阵，并明确：

- 不提交一次性调试脚本、真实 Token、Cookie、API Key、代理密码或外部响应原文。
- 修改 FastAPI 路由或 Pydantic 模型后运行 `python scripts/openapi_contract.py --write` 并提交快照。
- 前端依赖安装使用 `npm ci`，审计使用官方 registry 可用的 `npm audit` 端点。

- [ ] **步骤 3：整理 Roadmap 和 Changelog**

在 `ROADMAP.md` 保留外部 MySQL 一键部署、配置导入导出、执行日志、截图、通知渠道、模板、看板、权限、测试和数据生命周期等未完成项目；不得把已完成的支付宝验证、镜像签名或双端认证继续列为计划。

在 `CHANGELOG.md`：

- 清空已交付的 Unreleased 项，只记录本次文档和清理。
- 新增 `2026-07-15`：支付宝验证继续打卡、OpenAPI CI 修复、镜像构建签名验签。
- 新增 `2026-07-14`：工学云 5.32.6 打卡协议适配和依赖审计修复。
- 新增 `2026-06-18`：文档、全局 AI 设置、个人推送和根路由分流。

- [ ] **步骤 4：验证治理文档并提交**

运行：

```powershell
rg -n "Docker Publish|OpenAPI|支付宝|cosign" docs/ops/runbook.md
rg -n "敏感|openapi_contract|npm ci" CONTRIBUTING.md
rg -n "2026-07-15|2026-07-14|2026-06-18" CHANGELOG.md
git diff --check
```

预期：所有主题均命中，格式检查退出码为 0。

提交：

```powershell
git add docs/ops/runbook.md CONTRIBUTING.md ROADMAP.md CHANGELOG.md
git commit -m "docs(维护): 更新运维与项目记录"
```

### 任务 4：删除已迁移文档和本地无用文件

**文件：**
- 删除：`docs/superpowers/specs/2026-07-15-alipay-clockin-verification-design.md`
- 删除：`docs/superpowers/plans/2026-07-15-alipay-clockin-verification.md`
- 本地删除：`gxyzj.py`

- [ ] **步骤 1：确认旧文档内容已经迁移**

运行：

```powershell
rg -n "alipay/continue|outRegisterNo|registerUrl|补卡不进入" README.md docs/current-features.md server/README.md web/README.md docs/ops/runbook.md
```

预期：正式文档覆盖接口、敏感字段和补卡边界。

- [ ] **步骤 2：删除跟踪的完成文档**

使用 `apply_patch` 删除两份支付宝规格和计划，随后运行：

```powershell
rg -n "2026-07-15-alipay-clockin-verification" README.md server/README.md web/README.md docs CHANGELOG.md CONTRIBUTING.md ROADMAP.md
```

预期：当前正式文档不再引用已删除路径。

- [ ] **步骤 3：删除主工作区的敏感调试文件和生成物**

以 `F:\code\szxm\automoguding-saas` 为主工作区根目录。删除前逐个解析并确认目标位于该根目录内，再删除 `gxyzj.py`、`.pytest_cache/`、`.ruff_cache/`、Python `__pycache__/`、`web/dist/` 和 `web/*.log`。保留 `.env`、`web/node_modules`、`.codex-runtime` 和 `.superpowers`。

- [ ] **步骤 4：提交跟踪文件删除**

```powershell
git add docs/superpowers/specs/2026-07-15-alipay-clockin-verification-design.md docs/superpowers/plans/2026-07-15-alipay-clockin-verification.md
git commit -m "chore(文档): 删除已完成的支付宝计划"
```

### 任务 5：全量验证并完成计划清理

**文件：**
- 删除：`docs/superpowers/plans/2026-07-15-documentation-and-cleanup.md`

- [ ] **步骤 1：运行代码、文档和敏感信息检查**

```powershell
ruff check server scripts --select F401,F841
git diff --check
rg -n "alipay/continue|docker-compose.image|OpenAPI|cosign" README.md server/README.md web/README.md docs/current-features.md docs/ops/runbook.md CONTRIBUTING.md CHANGELOG.md
```

预期：Ruff 和格式检查退出码为 0；正式文档主题完整。

- [ ] **步骤 2：运行后端全量验证**

```powershell
$env:APP_ENV='test'
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

预期：`283` 项测试通过。

- [ ] **步骤 3：运行前端全量验证**

```powershell
Set-Location web
npm audit --registry=https://registry.npmjs.org --audit-level=high
npm run lint
npm test
npm run build
Set-Location ..
```

预期：审计为 `0 vulnerabilities`，lint、测试和 Vite 构建退出码均为 0。

- [ ] **步骤 4：删除临时环境和生成物后运行后端门禁**

先验证 `.venv`、缓存、构建产物和日志的解析路径均位于当前工作树，再删除 `.venv`、`.pytest_cache`、`.ruff_cache`、Python `__pycache__`、`web/dist` 和 `web/*.log`。保留 `.env` 和 `web/node_modules`，随后运行：

```powershell
python -m compileall server
python scripts/quality_gate.py
python scripts/verify_supply_chain_policy.py
python scripts/backup_restore_drill.py
pip-audit -r server/requirements.txt
```

预期：编译、质量门、供应链策略、备份恢复和依赖审计退出码均为 0；依赖审计无已知漏洞。

- [ ] **步骤 5：验证 Markdown 相对链接**

运行：

```powershell
$docs = @(
    'README.md',
    'server/README.md',
    'web/README.md',
    'docs/current-features.md',
    'docs/ops/runbook.md',
    'CONTRIBUTING.md',
    'ROADMAP.md',
    'CHANGELOG.md'
)
$missing = foreach ($doc in $docs) {
    $text = Get-Content -Raw -Encoding UTF8 -LiteralPath $doc
    foreach ($match in [regex]::Matches($text, '!?' + '\[[^\]]*\]\(([^)]+)\)')) {
        $target = $match.Groups[1].Value.Trim()
        if ($target -match '^(https?://|mailto:|#)') { continue }
        $pathPart = [uri]::UnescapeDataString(($target -split '#', 2)[0])
        $base = Split-Path -Parent (Resolve-Path -LiteralPath $doc)
        $resolved = Join-Path $base $pathPart
        if (-not (Test-Path -LiteralPath $resolved)) { "$doc -> $target" }
    }
}
if ($missing) { $missing; exit 1 }
'Markdown relative links: OK'
```

预期：缺失链接数为 0。

- [ ] **步骤 6：删除已完成的本计划并提交**

使用 `apply_patch` 删除 `docs/superpowers/plans/2026-07-15-documentation-and-cleanup.md`，然后运行：

```powershell
git status --short
git diff --check
git log --oneline 99fadf1..HEAD
```

提交：

```powershell
git add docs/superpowers/plans/2026-07-15-documentation-and-cleanup.md
git commit -m "chore(维护): 完成文档与仓库清理"
```

- [ ] **步骤 7：审查分支差异**

```powershell
git diff --check 99fadf1..HEAD
git diff --stat 99fadf1..HEAD
git status --short --branch
```

预期：工作树无未提交变更，差异只包含批准范围。
