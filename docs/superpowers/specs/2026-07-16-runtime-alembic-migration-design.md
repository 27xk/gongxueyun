# 运行时 Alembic 自动迁移设计

## 背景

应用使用 `ALLOW_RUNTIME_SCHEMA_MIGRATIONS` 控制启动时是否允许修改数据库结构。
当前开启该配置后只执行 `SQLModel.metadata.create_all()` 和兼容性补列、补索引，
不会执行 Alembic。已有数据库因此可能停留在旧修订，同时应用代码已经读取新字段和新表。

预授权功能要求迁移 `20260715_0003` 提供 `user.created_at` 和
`clockinpreauthorization`。数据库仍为 `20260530_0002` 时，预授权列表会因缺列或缺表
返回 HTTP 500。

## 目标

- `ALLOW_RUNTIME_SCHEMA_MIGRATIONS=true` 时，应用启动自动执行 `alembic upgrade head`。
- Alembic 成功后再执行现有兼容性建表、补列和补索引逻辑。
- 自动迁移关闭时保持只读校验，数据库不是 `head` 则拒绝启动。
- 迁移失败时终止启动，不初始化管理员、调度器或队列 worker。
- 保留生产环境默认关闭自动迁移的现有策略。

## 非目标

- 不自动执行 Alembic 降级。
- 不绕过或伪造 `alembic_version`。
- 不在本次改动中解决多副本同时执行 DDL 的协调问题。生产多副本部署应只允许一个
  实例开启自动迁移，或在发布阶段单独执行迁移。
- 不修改预授权业务模型和 API 响应结构。

## 设计

### 数据库迁移边界

在 `server/database.py` 新增 `upgrade_database_schema_to_head()`：

1. 使用项目根目录的 `alembic.ini` 创建 Alembic `Config`。
2. 显式设置 `script_location` 为 `server/migrations`。
3. 调用 Alembic Python API 执行 `upgrade(config, "head")`。
4. 执行完成后通过现有 `require_database_schema_current()` 验证修订状态。

该函数不捕获迁移异常。连接失败、DDL 失败或修订不一致都必须向上传播，使应用启动失败。

### 应用启动顺序

`server.main.on_startup()` 使用以下顺序：

```text
允许运行时迁移
  -> Alembic upgrade head
  -> 兼容性建表、补列和补索引
  -> 校验数据库位于 head
  -> 初始化管理员
  -> 启动后台服务

禁止运行时迁移
  -> 校验数据库位于 head
  -> 初始化管理员
  -> 启动后台服务
```

Alembic 必须早于所有读取 `User` 或 `ClockInPreauthorization` 的代码，避免启动同步和
预授权接口因缺少 `user.created_at` 或预授权表而返回 500。

### 配置语义

- `APP_ENV=production` 且未配置开关：自动迁移关闭，数据库过期时拒绝启动。
- `ALLOW_RUNTIME_SCHEMA_MIGRATIONS=true`：所有环境允许自动升级。
- 非生产环境且未配置开关：保持现有默认值，允许自动升级。
- `ALLOW_RUNTIME_SCHEMA_MIGRATIONS=false`：所有环境禁止自动升级，只执行修订校验。

## 测试

- 自动迁移开启时，断言先调用 Alembic 升级，再执行兼容性建表和修订校验。
- 自动迁移关闭时，断言不调用 Alembic，只校验修订状态。
- Alembic 升级失败时，断言管理员初始化和后台服务均不会执行。
- 使用临时数据库从 `20260530_0002` 升级到 `head`，断言新增字段、预授权表和
  `alembic_version` 均正确。
- 运行预授权 API 与完整后端测试，确认迁移改动没有改变接口契约。

## 文档

更新根 README、后端 README 和 CHANGELOG，明确自动迁移开关会运行 Alembic，
生产默认关闭，并记录多副本部署限制。
