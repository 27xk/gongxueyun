# 文档更新与无用内容清理设计

## 背景

当前实现已经包含支付宝安全验证后继续打卡、双端独立认证、批量任务、依赖审计、镜像签名等能力，但正式文档仍停留在此前版本。仓库还存在已完成的一次性实现计划、静态检查确认的无用符号，以及本地调试脚本和生成文件。

本次工作以当前 `main` 的运行入口、路由、导入关系、测试、CI 和构建配置为事实来源。只删除能够证明没有运行价值的内容，不根据文件名或主观判断删除历史迁移、模型、测试或运维工具。

## 目标

- 让根 README、前后端说明、功能速查、运行手册、贡献指南、Roadmap 和 Changelog 与当前实现一致。
- 完整记录支付宝安全验证的触发条件、显式继续流程、接口边界和敏感信息处理。
- 补齐源码构建、预构建镜像部署、镜像更新、CI 审计、构建、签名和验签说明。
- 删除一次性调试文件、已完成且内容已迁移的计划文档和本地生成文件。
- 清理 Ruff 已确认的无用导入和局部变量，不改变运行行为或公共接口。

## 非目标

- 不重写 Git 历史，不执行强制推送。
- 不删除 Alembic 迁移、ONNX 模型、测试、Docker Compose、镜像更新脚本或监控配置。
- 不重构大文件，不调整 API、数据库结构、任务状态或前端交互。
- 不把实现计划当作长期产品文档保留；有效规则迁移到正式文档后删除旧计划。
- 不删除 `.env`、`node_modules`、`.codex-runtime` 或 `.superpowers` 等仍可能被本地环境使用的内容。

## 文档更新范围

| 文件 | 更新内容 |
|------|----------|
| `README.md` | 当前能力、根路由行为、支付宝验证流程、两种 Docker Compose 部署、验证命令和文档索引 |
| `server/README.md` | 支付宝验证业务响应、继续接口、敏感信息边界、CI 后端门禁和任务链路 |
| `web/README.md` | 共享验证对话框、管理端与用户端触发路径、前端验证命令 |
| `docs/current-features.md` | 双端继续接口、`304` 业务语义、用户显式继续流程和失败状态 |
| `docs/ops/runbook.md` | Docker Publish 排障顺序、OpenAPI 快照、支付宝验证排障和镜像验签 |
| `CONTRIBUTING.md` | 本地完整验证矩阵、文档同步要求和敏感调试文件禁令 |
| `ROADMAP.md` | 移除已经完成的条目，保留尚未实现且可验收的计划 |
| `CHANGELOG.md` | 补齐 2026 年 6 月和 7 月已交付能力，整理 Unreleased |

文档只引用仓库当前存在的路径、命令和接口。根路径 `/` 的说明必须反映真实路由守卫：管理员会话进入管理端，用户会话进入 `/u`，未登录访问跳转 `/u/login`。

## 删除范围

### 当前树删除

- `docs/superpowers/specs/2026-07-15-alipay-clockin-verification-design.md`
- `docs/superpowers/plans/2026-07-15-alipay-clockin-verification.md`

上述文档描述的安全规则、接口和用户流程迁移到正式文档后再删除。

### 本地文件清理

- `gxyzj.py`：被 `.gitignore` 排除的一次性调试脚本，无入口、导入、测试或文档引用，并包含硬编码访问凭据。
- `.pytest_cache/`、`.ruff_cache/` 和 Python `__pycache__/`：测试和静态检查缓存。
- `web/dist/`：前端构建产物；验证构建后删除。
- `web/*.log`、`web/.vite-*.log`、`web/dev-server*.log` 和 `web/static-*.log`：本地开发日志。

保留 `.gitignore` 中的 `gxyzj.py` 规则，防止同名敏感调试脚本被意外提交。

## 无用代码清理

以 `ruff check server scripts --select F401,F841` 的结果为准，清理以下 7 处：

| 文件 | 清理内容 |
|------|----------|
| `server/coreApi/AiServiceClient.py` | 异常分支中未使用的局部变量 `e` |
| `server/coreApi/MainLogicApi.py` | 未使用的 `_log_ctx` 导入；`task_runner.py` 仍使用该上下文，因此保留 `LoggerContext.py` |
| `server/models.py` | 未使用的 `BaseModel` 导入 |
| `server/rate_limit.py` | 未使用的 `func` 导入 |
| `server/task_runner.py` | 未使用的 `threading` 和 `runtime_plan_required` 导入 |
| `server/util/FileUploader.py` | 未使用的 `Optional` 导入 |

不根据静态引用次数删除可执行入口或独立运维脚本。`server/backup_cli.py` 和 `scripts/check-image-update.ps1` 虽不被应用导入，但分别由 CLI 和运维人员直接执行，必须保留并写入文档。

## 安全边界

- 文档示例不得包含真实 Token、Cookie、API Key、登记编号、用户 ID 或代理凭据。
- `outRegisterNo` 和 `registerUrl` 只存在于当次授权响应和页面内存，不写入审计、持久化任务结果或通知内容。
- `gxyzj.py` 删除前不复制其凭据或请求数据到任何文档、测试、提交信息或日志。
- 本次不重写 Git 历史；如果后续确认历史提交包含仍有效的凭据，应先轮换凭据，再单独设计历史清理。

## 验证方案

### 文档与清理验证

- 使用 `ruff check server scripts --select F401,F841` 确认 7 处无用符号清零。
- 使用 `rg` 检查正式文档包含支付宝继续接口和两种镜像部署方式。
- 检查 Markdown 相对链接和引用文件均存在。
- 检查已删除文件不再被当前树引用。
- 使用 `git diff --check` 检查空白和格式问题。

### 后端验证

```powershell
python -m unittest discover -s tests
python -m compileall server
python scripts/quality_gate.py
python scripts/verify_supply_chain_policy.py
python scripts/backup_restore_drill.py
pip-audit -r server/requirements.txt
```

### 前端验证

```powershell
Set-Location web
npm audit --registry=https://registry.npmjs.org --audit-level=high
npm run lint
npm test
npm run build
Set-Location ..
```

前端构建验证完成后删除忽略的 `web/dist/`，确保工作区只保留源码和必要的本地依赖。

## 完成标准

- 8 份正式文档与当前代码和 CI 一致，没有过时接口、入口或部署描述。
- 两份已完成的支付宝计划文档从当前树删除，其有效内容已迁移。
- 7 处静态确认的无用符号全部清理，Ruff 对应规则无报错。
- 一次性敏感调试脚本和明确的本地生成文件已删除。
- 保留项均有运行、测试、部署或运维依据。
- 后端、前端、质量门、依赖审计和构建验证全部通过。
