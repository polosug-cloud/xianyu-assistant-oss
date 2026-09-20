# 闲鱼助手（Xianyu Assistant）

> 闲鱼（Goofish）**虚拟商品自动发货 / 自动答复 / 多账号管理**的轻量自动化助手。
> 单机运行、本地管理页、多账号一进程一端口，支持托盘管理。**仅限学习研究，请遵守平台规则。**

技术栈：`Python` + `FastAPI/uvicorn` + `Playwright(Chromium)` + `SQLite` + 单文件 Web 管理页（原生 JS）+ `PowerShell/WinForms` 托盘。

---

## ✨ 功能特性

### 消息与自动答复
- **WebSocket 实时监听**（`/im` 页 `wss-goofish` 通道）+ DOM 兜底轮询，秒级响应
- **规则引擎**：包含关键词 / 正则 / 全部包含；支持 `{卡密}` `{商品名}` `{买家}` 变量
- **规则多商品**：一条规则可同时应用到多个商品；支持 **CSV 导入/导出**、表格内直接改模板
- **消息去重与回显识别**：同一消息不连续重复入库；自动识别"自己发出的消息被平台回显"，不误判为买家消息（防收发倒置、防问候语自问自答）
- **悬浮聊天窗口**：多会话查看、人工回复、发送图片、系统截图后 `Ctrl+V` 直接粘贴发送

### 自动发货
- 卡密库存按**内部 SKU** 分组，Excel 导入/导出/模板下载
- 支持**循环卡**（同 SKU 复用、次数上限自动停用）、已售/停用状态机
- 买家拍下/付款通知自动识别并自动发货（可与自动重新发布联动）

### 多账号
- 一个账号 = 一个进程 = 一个端口（主账号 8080，新增 8081/8082…）+ 独立数据目录与会话
- **托盘统一管理**：新增账号、启动/暂停（勾选即运行）、删除账号（二次确认、数据清理无残留）
- 启动全部 / 暂停全部 / 打开管理页（全部或按账号）/ 提示音开关（按账号记忆）/ 运行状态 / 营收状态

### 运维与观测
- 定时自检（账号 → 消息通道 → 商品同步 → 规则引擎 → 发货就绪），异常时系统提示音
- 状态口令 / 运营汇报口令、定时状态与营收汇报到管理员账号
- 商品自动同步、自动重新发布巡检、营收统计、操作日志
- 网页标题带账号名，便于多账号识别；登录窗口最长等待 30 分钟且被收起时自动重弹

---

## 🚀 快速开始

### 1. 环境要求
- **Windows 10/11**（托盘、系统截图、提示音依赖 Windows）
- **Python 3.10+**（开发环境为 3.14）

### 2. 安装
```bat
git clone https://github.com/polosug-cloud/xianyu-assistant-oss.git
cd xianyu-assistant-oss
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. 启动
```bat
:: 方式一：命令行（默认 http://127.0.0.1:8080）
python -m app.main

:: 方式二：托盘方式（Windows，推荐）
start_xianyu.bat
```

浏览器打开 `http://127.0.0.1:8080` → 点「登录/扫码」→ 用**闲鱼 App** 扫码登录。

### 4. 多账号
- 托盘右键 → **账号管理 → ＋新增账号**：自动分配端口（8081 起）与独立数据目录，启动后扫码即可；
- 手动方式：为第二个实例设置不同的 `XY_PORT` 与 `XY_DATA_DIR` 后启动。

---

## ⚙️ 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XY_PORT` | `8080` | 本实例监听端口 |
| `XY_HOST` | `127.0.0.1` | 监听地址（仅本机） |
| `XY_DATA_DIR` | `./data` | 数据目录（数据库/会话/临时文件） |
| `XY_LOG_DIR` | `./logs` | 日志目录 |
| `XY_BROWSERS_DIR` | `./.browsers` | Playwright 浏览器目录 |
| `XY_WEB_USER` / `XY_WEB_PASS` | `admin` / 随机 | 管理页账号；密码为空时自动生成令牌 |
| `XY_SOUND_SYS` / `XY_SOUND_MSG` | `./sounds/Spring.ogg` / `Bubble.ogg` | 提示音文件（需自备，缺失仅静音） |
| `XY_LOGIN_WAIT` | `1800` | 扫码登录窗口最长等待秒数 |
| `XY_POLL_SEC` | `1` | 主循环间隔（秒） |
| `XY_REPLY_INTERVAL` / `XY_DELIVER_INTERVAL` | `4` / `10` | 回复/发货最小间隔（秒，风控节流） |
| `XY_ACTION_HOURLY_CAP` | `200` | 每小时动作上限 |
| `XY_RULE_COOLDOWN` | `10` | 同一会话规则冷却（秒） |
| `XY_AI_*` | 关闭 | 可选 AI 回复（OpenAI 兼容接口） |

---

## 📁 目录结构

```
app/                    后端
  main.py               启动入口（uvicorn）
  api.py                FastAPI 路由 / 鉴权中间件
  browser.py            Playwright 会话与扫码登录
  listener.py           消息监听（WS + DOM）、规则分发、口令、报告
  deliverer.py          自动发货 / 发送文本与图片
  rules.py              规则引擎
  sync.py               商品同步 / 重新发布
  selfcheck.py          自检任务
  db.py                 SQLite（WAL）与业务数据访问
  screenshot_util.py    系统级截屏（GDI，纯标准库）
web/index.html          单文件管理页（原生 JS）
tools/                  辅助脚本
sounds/                 提示音目录（自备 .ogg）
xianyu_tray.ps1         托盘脚本（Windows / PowerShell + WinForms）
xianyu_tray.vbs         无窗口启动托盘
start_xianyu.bat        托盘方式启动
data/                   运行期数据（自动生成，不入库）
```

---

## ❓ 常见问题

| 现象 | 处理 |
|---|---|
| 管理页打不开 | 确认进程在运行、端口未被占用（托盘检测到占用会提示自动改端口） |
| 提示未登录 / 会话过期 | 点「登录/扫码」重新扫码（登录态会随平台过期） |
| 收不到新消息 | 检查 `/im` 页面是否正常、日志是否有 `心跳: ws_conns=1` |
| 提示音不响 | 在 `sounds/` 放入 `Spring.ogg` 与 `Bubble.ogg`，或在设置中开启 |
| 端口冲突 | 托盘会弹窗让你自动重分配或指定端口 |

---

## ⚠️ 已知限制

- 平台页面结构与内部接口可能随时调整，可能导致登录/监听/发货失效，需要相应更新；
- 平台存在风控，请合理控制动作频率（内置最小间隔与每小时上限）；
- 仅支持 Windows（托盘、系统截图与路径相关功能）。

---

## 📜 免责声明

本项目仅供**学习与研究**使用。请遵守闲鱼/淘宝平台规则及相关法律法规，不得用于刷单、欺诈、虚假交易等违规用途。
使用本工具产生的一切后果由使用者自行承担，作者不承担任何责任。

## 📄 许可证

[MIT](LICENSE)

## ⭐ Star

如果这个项目对你有帮助，欢迎点个 Star 支持一下，谢谢！
