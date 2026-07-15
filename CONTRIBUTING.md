# 贡献指南

感谢你愿意改进 AutoMoGuDing SaaS。提交 Issue 或 PR 前，请先确认问题可复现、变更可验证、文档可同步。

## 提交 Issue

### Bug 反馈

| 信息 | 说明 |
|------|------|
| 部署方式 | Docker Compose 或本地开发 |
| 环境版本 | 操作系统、Python、Node.js、MySQL |
| 后端版本 | Git commit 或发布版本 |
| 复现步骤 | 从入口到报错的完整操作路径 |
| 实际结果 | 页面报错、接口响应、后端日志 |
| 预期结果 | 你认为应该发生什么 |
| 影响范围 | 管理端 / 用户端、单账号 / 多账号、打卡 / 补卡 / 报告 |
| 相关配置 | 脱敏后的关键环境变量、用户配置或代理 / AI 设置 |

### 功能建议

| 问题 | 请说明 |
|------|--------|
| 要解决什么 | 当前流程的痛点或缺口 |
| 影响谁 | 管理员、用户、运维还是二次开发者 |
| 期望路径 | 用户应该如何进入、操作和退出 |
| 边界 | 不希望功能做什么 |
| PR 意愿 | 是否愿意提交实现 |

## 本地开发

| 目标 | 命令 |
|------|------|
| 安装后端依赖 | `pip install -r server/requirements.txt` |
| 升级数据库 | `python -m alembic upgrade head` |
| 启动后端 | `python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147` |
| 安装前端依赖 | `cd web && npm ci` |
| 启动前端 | `cd web && npm run dev` |

## 提交前验证

| 范围 | 命令 | 通过标准 |
|------|------|----------|
| 后端测试 | `python -m unittest discover -s tests` | 0 个失败和错误 |
| Python 编译 | `python -m compileall server` | 退出码为 0 |
| OpenAPI 契约 | `python scripts/openapi_contract.py` | 快照与运行时一致 |
| 后端质量门 | `python scripts/quality_gate.py` | 输出 `Quality gate passed` |
| 供应链策略 | `python scripts/verify_supply_chain_policy.py` | action 和基础镜像固定策略通过 |
| 备份恢复 | `python scripts/backup_restore_drill.py` | 输出 `"ok": true` |
| Python 审计 | `pip-audit -r server/requirements.txt` | 无已知漏洞 |
| 前端审计 | `cd web && npm audit --audit-level=high` | 无高危或严重漏洞 |
| 前端检查 | `cd web && npm run lint && npm test` | 两个命令均退出 0 |
| 前端构建 | `cd web && npm run build` | Vite 构建成功 |

如果本机 npm 镜像不实现 audit API，可仅对审计命令显式使用官方端点：

```powershell
Set-Location web
npm audit --registry=https://registry.npmjs.org --audit-level=high
```

## 代码风格

| 范围 | 要求 |
|------|------|
| 后端 | 优先沿用现有 FastAPI、SQLModel 和工具函数风格 |
| 前端 | 优先沿用 Vue 3、Element Plus、Pinia 和现有消息提示封装 |
| 数据库 | schema 变更必须补 Alembic 迁移，不依赖生产运行时自动补列 |
| 权限 | 后端权限点是边界，前端隐藏菜单不是安全控制 |
| 安全 | 不回显敏感信息，不绕开 Cookie / CSRF / CORS / Host 校验 |
| PR 范围 | 不在同一个 PR 混入无关重构 |
| 测试 | 行为变更优先补充或更新对应测试 |
| 文档 | 使用简体中文；命令、路径、接口和代码标识保持原文 |

### 敏感信息与调试文件

- 不提交一次性请求脚本、抓包导出、浏览器存储转储或临时响应文件。
- 不在源码、测试、文档、Issue 或提交信息中写入真实 Token、Cookie、API Key、代理密码、用户 ID、支付宝登记编号或完整外部响应。
- 调试输出必须脱敏；认证头、请求体和 `registerUrl` 不得进入日志。
- 已经暴露的凭据先轮换，再单独评估是否需要清理 Git 历史；不要在普通功能提交中强制改写历史。

## 补卡相关改动

| 规则 | 要求 |
|------|------|
| 类型边界 | `START` 只补上班，`END` 只补下班 |
| 自动补卡 | 普通定时打卡不能自动触发补卡 |
| 多日期 | 一次请求可以补多个日期，但仍只补一种类型 |
| 频繁请求 | 必须重试当前日期，不能直接跳过继续打下一天 |
| 代理 | 代理只用于手动补卡，不影响登录、缺卡查询、定时打卡和报告提交 |

## 文档维护

| 变更类型 | 同步文档 |
|----------|----------|
| 用户可见功能 | `README.md`、`docs/current-features.md` |
| 后端启动 / 配置 / API | `server/README.md`、`docs/current-features.md` |
| 前端入口 / 页面 / 联调 | `web/README.md`、截图 |
| 运维 / CI / 安全策略 | `docs/ops/runbook.md`、`README.md` |
| FastAPI 路由 / Pydantic 模型 | 使用固定依赖运行 `python scripts/openapi_contract.py --write`，提交 `docs/api/openapi-contract.json` |
| 发布记录 | `CHANGELOG.md` |
| 后续计划 | `ROADMAP.md` |

涉及界面变更时，请同步更新 `img/` 下的截图，并检查 README 中的 Demo 区域是否仍然准确。
