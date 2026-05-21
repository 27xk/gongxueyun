# AutoMoGuDing SaaS 前端

`web/` 是 AutoMoGuDing SaaS 的 Vue 3 前端工程，包含管理端和用户端两套界面。项目使用 Vite 构建，UI 组件基于 Element Plus。

## 技术栈

- Vue 3
- Vite
- Pinia
- Vue Router
- Element Plus
- Axios
- Leaflet

## 页面入口

### 管理端

- `/login`：后台登录页
- `/`：管理端首页
- 用户编辑页：维护账号、打卡、补卡、报告、AI、通知等配置
- 用户列表页：批量执行任务、查看用户状态和执行结果
- 审计日志页：查看管理端和用户端关键操作
- 通知配置页：配置全局 QQ 邮箱 SMTP 推送能力

### 用户端

- `/u/login`：用户登录页
- `/u/register`：用户注册页
- `/u`：用户工作台
- `/u/settings`：用户设置页

用户端使用独立认证状态，不与管理端登录态混用。

## 开发命令

```bash
cd web
npm install
npm run dev
npm run build
npm run preview
```

说明：

- `npm run dev` 默认监听 `0.0.0.0:5173`，并开启 strict port。
- Vite 会把 `/api` 代理到 `http://127.0.0.1:8147`。
- 当前没有前端测试脚本，也没有 `npm run lint`。

## 目录结构

```text
web/
├─ src/
│  ├─ api/              # 管理端和用户端 Axios 实例
│  ├─ router/           # 路由和认证守卫
│  ├─ stores/           # 管理端和用户端认证状态
│  ├─ utils/            # 消息提示等前端工具
│  └─ views/            # 页面组件
├─ vite.config.js       # Vite 配置和 /api 代理
└─ package.json         # 前端依赖和脚本
```

## 接口约定

前端通过 `/api` 访问后端：

- 管理端使用 `src/api/http.js`。
- 用户端使用 `src/api/userHttp.js`。
- 请求失败时统一通过 `src/utils/notify.js` 解析错误并展示提示。
- `401` 会清空对应端的登录态并跳转登录页。

## 打卡与补卡界面

管理端用户编辑页和用户端设置页都包含打卡配置和补卡操作。

| 打卡设置 | 补卡详情 |
|----------|----------|
| ![打卡设置页](../img/打卡设置页.png) | ![补卡详细](../img/补卡详细.png) |

补卡流程：

1. 点击「刷新缺卡」，读取远端打卡记录。
2. 选择补卡类型：`上班` 或 `下班`。
3. 日期下拉只展示当前类型仍缺卡的日期。
4. 点击「补选中」补多选日期。
5. 点击「全部待补」补当前类型下所有待补日期。

注意：补卡一次只补一种类型。选择「上班」时只提交 `START`；选择「下班」时只提交 `END`。即使某天同时缺上班和下班，也不会自动同时补两条。

补卡请求字段：

```json
{
  "target_dates": ["2026-05-06", "2026-05-07"],
  "target_type": "START"
}
```

管理端接口：

- `GET /users/{user_id}/clock-in/missing-days`
- `POST /users/{user_id}/clock-in/makeup`
- `POST /users/{user_id}/clock-in/makeup-all`

用户端接口：

- `GET /app/clock-in/missing-days`
- `POST /app/clock-in/makeup`
- `POST /app/clock-in/makeup-all`

## 报告界面

管理端和用户端都支持补交报告：

| 报告设置 | 日报 / 周报 / 月报补交 |
|----------|------------------------|
| ![报告设置](../img/报告设置.png) | ![补日 - 周 - 月报详细](../img/补日-周-月报详细.png) |

- 日报：选择未提交日期，AI 生成内容后可提交。
- 周报：选择未提交周周期，AI 生成内容后可提交。
- 月报：选择未提交月份，AI 生成内容后可提交。

用户端工作台保留快捷的日报生成和提交入口；完整的日报、周报、月报配置与补交入口位于 `/u/settings`。

## 管理与通知界面

| 用户列表 | 推送设置 | 全局邮箱通知 |
|----------|----------|--------------|
| ![首页 - 用户列表](../img/首页-用户列表.png) | ![推送设置](../img/推送设置.png) | ![全局邮箱通知](../img/全局邮箱通知.png) |

## 构建产物

```bash
cd web
npm run build
```

构建结果输出到 `web/dist`。后端在检测到 `web/dist` 存在时，会由 FastAPI 直接托管前端静态资源，并提供 SPA fallback。
