# 贡献指南

感谢你愿意改进 AutoMoGuDing SaaS。提交 Issue 或 PR 前，请先阅读本文档，确保信息完整、变更可验证。

## 提交 Issue

### Bug 反馈

请尽量提供：

- 部署方式：Docker Compose 或本地开发。
- 操作系统、Python、Node.js、MySQL 版本。
- 后端版本或 Git commit。
- 复现步骤。
- 实际结果和期望结果。
- 后端日志、浏览器控制台报错或接口响应。
- 是否涉及特定账号配置、打卡类型或报告类型。

### 功能建议

请说明：

- 你希望解决什么问题。
- 当前流程哪里不顺。
- 期望的用户操作路径。
- 是否愿意提交 PR。

## 本地开发

后端：

```bash
pip install -r server/requirements.txt
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8147
```

前端：

```bash
cd web
npm install
npm run dev
```

## 提交 PR 前的验证

后端测试：

```bash
python -m unittest discover -s tests
```

后端编译检查：

```bash
python -m compileall server
```

前端构建：

```bash
cd web
npm run build
```

空白字符检查：

```bash
git diff --check
```

当前项目没有 `npm test` 和 `npm run lint` 脚本。

## 代码风格

- 后端优先沿用现有 FastAPI、SQLModel 和工具函数风格。
- 前端优先沿用现有 Vue 3、Element Plus 和消息提示封装。
- 不要在一次 PR 中混入无关重构。
- 涉及行为变化时，优先补充或更新后端单元测试。
- 文档使用简体中文；命令、路径、接口字段和代码标识保持原文。

## 补卡相关改动注意事项

补卡一次只补一种类型：

- `START`：上班。
- `END`：下班。

修改补卡逻辑时，必须确认不会在用户只选择一种类型时自动补另一种类型。

相关测试：

- `tests/test_clockin_backfill.py`
- `tests/test_main_logic_api.py`
- `tests/test_task_runner_clockin.py`
- `tests/test_api_clockin_makeup.py`

## 文档维护

功能变更时请同步更新：

- `README.md`
- `docs/current-features.md`
- `server/README.md`
- `web/README.md`
- `CHANGELOG.md`

如果涉及界面变化，请同步更新 `img/` 下的截图，并在 README 的 Demo 区域检查展示是否仍准确。
