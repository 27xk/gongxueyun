# 当前功能与接口速查

本文档记录当前代码实现中的核心功能、补卡规则、接口入口和验证命令。历史设计与计划文档位于 `docs/superpowers/`，不一定代表最新实现。

## 功能范围

AutoMoGuDing SaaS 当前包含两套 Web 界面：

- **管理端：** 用户管理、批量任务、审计日志、通知配置、AI 测试、地理编码、缺卡查询和补卡。
- **用户端：** 注册 / 登录、绑定工学云账号、个人配置、手动执行、执行记录、日报生成 / 提交、缺卡查询和补卡。

后端任务执行链路集中在：

- `server/api.py`：管理端和用户端 API。
- `server/user_runtime.py`：数据库用户模型与任务运行配置之间的转换。
- `server/scheduler.py`：定时任务注册。
- `server/queue_worker.py`：批量任务队列。
- `server/task_runner.py`：打卡、补卡、报告提交等任务执行。
- `server/clockin_backfill.py`：打卡记录归一化和待补卡日期筛选。
- `server/coreApi/MainLogicApi.py`：工学云接口客户端。

更多后端维护细节见 `server/README.md`，前端交互说明见 `web/README.md`。

## 打卡记录与缺卡筛选

打卡记录会先归一化为以下结构：

| 字段 | 说明 |
|------|------|
| `date` | 日期，格式为 `YYYY-MM-DD` |
| `time` | 打卡时间，格式为 `YYYY-MM-DD HH:mm:ss` |
| `type` | 打卡类型，`START` 或 `END` |
| `type_label` | 中文类型，上班或下班 |
| `address` | 记录中的地址 |

缺卡筛选规则：

- `START` 表示上班打卡。
- `END` 表示下班打卡。
- 同一天如果 `START` 和 `END` 都存在，则该日期不是待补日期。
- 同一天如果只缺一种类型，则只在该类型下展示。
- 待补日期会按用户配置的打卡周期过滤。

## 补卡规则

补卡一次只补一种类型：

- `target_type=START`：只补上班。
- `target_type=END`：只补下班。

即使某一天同时缺上班和下班，只要用户选择 `START`，系统也只提交上班补卡；选择 `END` 时也只提交下班补卡。

补卡时间来自用户配置：

- 上班补卡使用 `config.clockIn.schedule.startTime`，默认 `07:30`。
- 下班补卡使用 `config.clockIn.schedule.endTime`，默认 `18:00`。

学生补卡请求使用：

```text
attendence/attendanceReplace/v4/save
```

关键请求字段：

| 字段 | 值 |
|------|----|
| `attendanceType` | `REPLACE` |
| `type` | `START` 或 `END` |
| `createTime` | 目标日期 + 对应上班 / 下班时间 |
| `attendenceTime` | `null` |
| `isReplace` | `null` |

## 用户端补卡接口

### 获取缺卡日期

```http
GET /app/clock-in/missing-days
```

返回中的 `options` 用于前端日期下拉。每个选项包含：

```json
{
  "value": "2026-05-06",
  "label": "2026-05-06（缺上班）",
  "missing_types": ["START"],
  "existing_types": ["END"]
}
```

### 补选中日期

```http
POST /app/clock-in/makeup
Content-Type: application/json
```

```json
{
  "target_dates": ["2026-05-06", "2026-05-07"],
  "target_type": "START"
}
```

兼容单日期请求：

```json
{
  "target_date": "2026-05-06",
  "target_type": "END"
}
```

### 补全部待补日期

```http
POST /app/clock-in/makeup-all
Content-Type: application/json
```

```json
{
  "target_type": "END"
}
```

该接口会先重新获取缺卡列表，再只补当前 `target_type` 下仍缺卡的日期。

## 管理端补卡接口

管理端接口与用户端语义一致，只是路径包含用户 ID：

```http
GET /users/{user_id}/clock-in/missing-days
POST /users/{user_id}/clock-in/makeup
POST /users/{user_id}/clock-in/makeup-all
```

## 前端补卡交互

管理端用户编辑页和用户端设置页采用同一交互模型：

1. 点击「刷新缺卡」。
2. 选择补卡类型：上班或下班。
3. 日期下拉只展示当前类型仍缺的日期。
4. 点击「补选中」补多选日期。
5. 点击「全部待补」补当前类型下全部待补日期。

页面会显示：

- 已获取打卡记录数
- 当前类型待补天数
- 已选择天数

## 报告补交

日报、周报、月报都支持按周期筛选未提交记录：

- 日报按日期筛选。
- 周报按自然周筛选。
- 月报按月份筛选。

管理端和用户端设置页均支持选择未提交周期、AI 生成内容和立即提交。

## 验证命令

后端测试：

```bash
python -m unittest discover -s tests
```

后端语法编译：

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
