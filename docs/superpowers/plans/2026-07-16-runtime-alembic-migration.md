# 运行时 Alembic 自动迁移实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 `ALLOW_RUNTIME_SCHEMA_MIGRATIONS=true` 真正执行 Alembic `upgrade head`，避免旧数据库导致预授权接口因缺列或缺表返回 500。

**架构：** 在数据库模块提供单一 Alembic 升级入口，由 FastAPI 启动钩子在兼容性建表前调用。自动迁移关闭时继续只读校验；迁移异常直接中止启动，保证管理员、调度器和队列不会在不完整结构上运行。

**技术栈：** Python 3.10、FastAPI、SQLModel、SQLAlchemy、Alembic、MySQL、unittest。

---

## 文件结构

- 修改 `server/database.py`：提供项目级 Alembic `upgrade head` 调用入口。
- 修改 `server/main.py`：按“升级、兼容处理、校验、业务初始化”顺序启动。
- 修改 `tests/test_platform_foundations.py`：覆盖启动顺序、关闭分支和迁移失败短路。
- 修改 `README.md`：说明本地启动和生产环境的自动迁移语义。
- 修改 `server/README.md`：说明配置开关、执行顺序和多副本限制。
- 修改 `CHANGELOG.md`：记录自动迁移与预授权 500 修复。

## 任务 1：用失败测试固定启动迁移语义

**文件：**

- 修改：`tests/test_platform_foundations.py`

- [ ] **步骤 1：编写自动迁移启动顺序失败测试**

在 `PlatformFoundationsTest` 中新增：

```python
def test_startup_runs_alembic_before_runtime_compatibility(self):
    from server import main

    calls = []
    with (
        patch("server.main.should_run_runtime_schema_migrations", return_value=True),
        patch(
            "server.main.upgrade_database_schema_to_head",
            side_effect=lambda: calls.append("alembic"),
        ),
        patch(
            "server.main.create_db_and_tables",
            side_effect=lambda: calls.append("compatibility"),
        ),
        patch(
            "server.main.require_database_schema_current",
            side_effect=lambda *_: calls.append("verify"),
        ),
        patch(
            "server.main.ensure_seed_admin_users",
            side_effect=lambda: calls.append("seed"),
        ),
        patch("server.main._should_auto_download_captcha_models", return_value=False),
        patch("server.main.should_start_background_services", return_value=False),
    ):
        main.on_startup()

    self.assertEqual(calls, ["alembic", "compatibility", "verify", "seed"])
```

- [ ] **步骤 2：编写迁移失败短路测试**

```python
def test_startup_stops_when_alembic_upgrade_fails(self):
    from server import main

    with (
        patch("server.main.should_run_runtime_schema_migrations", return_value=True),
        patch(
            "server.main.upgrade_database_schema_to_head",
            side_effect=RuntimeError("migration failed"),
        ),
        patch("server.main.create_db_and_tables") as create_tables,
        patch("server.main.ensure_seed_admin_users") as seed_admins,
        patch("server.main.start_scheduler") as start_scheduler,
        patch("server.main.start_queue_worker") as start_worker,
    ):
        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            main.on_startup()

    create_tables.assert_not_called()
    seed_admins.assert_not_called()
    start_scheduler.assert_not_called()
    start_worker.assert_not_called()
```

- [ ] **步骤 3：扩展关闭分支测试**

在现有 `test_startup_checks_schema_when_runtime_migrations_are_disabled` 中补充：

```python
patch("server.main.upgrade_database_schema_to_head") as upgrade_schema
```

并断言：

```python
upgrade_schema.assert_not_called()
```

- [ ] **步骤 4：运行测试确认失败**

运行：

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_platform_foundations.py"
```

预期：新增测试因 `server.main.upgrade_database_schema_to_head` 不存在而失败，原有测试保持通过。

## 任务 2：实现 Alembic 升级入口和启动编排

**文件：**

- 修改：`server/database.py`
- 修改：`server/main.py`
- 修改：`tests/test_platform_foundations.py`

- [ ] **步骤 1：为 Alembic 配置增加单元测试**

在 `tests/test_platform_foundations.py` 中新增：

```python
def test_upgrade_database_schema_uses_project_alembic_head(self):
    from server import database

    with patch("alembic.command.upgrade") as upgrade:
        database.upgrade_database_schema_to_head()

    config, revision = upgrade.call_args.args
    self.assertEqual(revision, "head")
    self.assertEqual(
        Path(config.config_file_name).resolve(),
        (database.PROJECT_ROOT / "alembic.ini").resolve(),
    )
    self.assertEqual(
        Path(config.get_main_option("script_location")).resolve(),
        (database.PROJECT_ROOT / "server" / "migrations").resolve(),
    )
```

- [ ] **步骤 2：运行新测试确认失败**

运行任务 1 的聚焦测试命令。

预期：`upgrade_database_schema_to_head` 尚不存在，测试失败。

- [ ] **步骤 3：实现 Alembic 升级函数**

在 `server/database.py` 中新增：

```python
def upgrade_database_schema_to_head() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "server" / "migrations"),
    )
    command.upgrade(config, "head")
```

不捕获 `command.upgrade` 的异常。

- [ ] **步骤 4：调整 FastAPI 启动顺序**

在 `server/main.py` 导入新函数，并将启动分支改为：

```python
if should_run_runtime_schema_migrations():
    upgrade_database_schema_to_head()
    create_db_and_tables()
    require_database_schema_current(engine)
else:
    require_database_schema_current(engine)
    logger.info("runtime schema migrations are disabled; run alembic before startup")
```

- [ ] **步骤 5：运行聚焦测试确认通过**

运行任务 1 的聚焦测试命令。

预期：全部通过，自动迁移顺序为 Alembic、兼容处理、修订校验、业务初始化。

- [ ] **步骤 6：补充旧修订到 head 的迁移回归**

在 `tests/test_migrations_contract.py` 新增测试，使用临时 SQLite 数据库依次执行：

```python
self._run_alembic(root, db_path, "upgrade", "20260530_0002")
self._run_alembic(root, db_path, "upgrade", "head")
```

然后断言：

```python
inspector = inspect(engine)
self.assertIn("created_at", {item["name"] for item in inspector.get_columns("user")})
self.assertIn("clockinpreauthorization", set(inspector.get_table_names()))
with engine.connect() as conn:
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
self.assertEqual(revision, "20260715_0003")
```

为避免重复子进程代码，在测试类中提取：

```python
def _run_alembic(self, root: Path, db_path: Path, *args: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
```

- [ ] **步骤 7：运行迁移和预授权回归测试**

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_migrations_contract.py"
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests -p "test_clockin_preauthorization_api.py"
```

预期：旧表升级契约、预授权双端 API 和敏感字段测试全部通过。

- [ ] **步骤 8：提交实现**

```powershell
git add server/database.py server/main.py tests/test_platform_foundations.py tests/test_migrations_contract.py
git commit -m "fix(数据库): 启动时执行 Alembic 自动迁移"
```

## 任务 3：更新文档并完成验证

**文件：**

- 修改：`README.md`
- 修改：`server/README.md`
- 修改：`CHANGELOG.md`

- [ ] **步骤 1：更新正式文档**

文档明确记录：

- 开关开启时执行 `alembic upgrade head`，随后运行兼容性结构处理。
- 生产环境默认关闭，数据库不是 `head` 时拒绝启动。
- 自动迁移失败时应用不会继续启动。
- 多副本生产部署只允许一个实例开启自动迁移，或在发布阶段单独执行迁移。
- 未升级到 `20260715_0003` 会导致预授权接口因缺列或缺表失败。

- [ ] **步骤 2：运行完整验证**

```powershell
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m unittest discover -s tests
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python -m compileall server
ruff check server scripts
python -m dotenv -f "F:\code\szxm\automoguding-saas\.env" run -- python scripts/quality_gate.py
python scripts/verify_supply_chain_policy.py
git diff --check
```

预期：所有命令退出码为 `0`，完整测试无失败。

- [ ] **步骤 3：提交文档**

```powershell
git add README.md server/README.md CHANGELOG.md
git commit -m "docs(数据库): 说明启动自动迁移行为"
```

- [ ] **步骤 4：检查最终差异**

```powershell
git status --short
git log --oneline --decorate -6
git diff main...HEAD --check
```

预期：工作树干净，提交按规格、测试、实现和文档分组，分支差异无空白错误。
