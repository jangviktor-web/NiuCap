# 牛来选股面板（Niucap） · A股量化选股与择时面板

> 本地自托管的 A 股量化**选股 + 择时 + 复盘**面板。25+ 选股策略、实时行情、龙虎榜后验、双源快讯、词典情绪、风险过滤、虚拟盘，**一条命令起，数据完全在自己手里**。

> 仓库名 `niucap`（牛 = capture 牛股，A 股梗）；产品中文名「牛来选股面板」。两者指同一个项目。

---

## ✨ 特性

| 模块 | 说明 |
|---|---|
| 🔍 **选股引擎** | 25+ 内置策略 + 分钟级策略 + 自定义信号，毫秒级扫描全 A 股 |
| 📡 **实时行情** | 实时 tick / 分钟 / 日 K，多数据源按能力路由 |
| 🐯 **龙虎榜后验** | 东财源，D+1 / D+2 / D+5 / D+10 复权涨幅 + 上榜原因，把名单变成绩单 |
| 📰 **双源快讯流** | 新浪 7×24 + 同花顺，30s 缓存，红条标记，关联股票可点跳个股页 |
| 🧠 **词典情绪** | 公告 / 快讯自动打「看涨 / 看跌 / 中性」标签（本地零成本） |
| 🛡 **风险过滤** | 最大阴量、J 值异常、公告风险着色，剔除劣质标的 |
| 💹 **虚拟盘** | 费率可配，盈亏跟踪，T+1 / 费用 / 滑点约束 |
| 🚀 **一键部署** | Docker / Python / WorkBuddy 云平台，任选 |

---

## 🚀 快速开始

### 方式 A：Python 本机（推荐给开发者）

```bash
git clone https://github.com/<你的用户名>/niucap.git
cd niucap
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server/run.py          # 或 ./deploy.sh（后台常驻 + 可选 Cloudflare 隧道）
# 打开 http://localhost:8899
```

> 需要 Python ≥ 3.11。无需任何 API Key，纯免费公开行情源（腾讯 / 新浪 / 同花顺 / 东财）。

### 方式 B：WorkBuddy 一键发布到云平台（推荐零运维）

本项目已满足云发布要求，在 WorkBuddy 里对仓库说「发布为应用」即可生成可访问的分享链接，无需自己买服务器。完整步骤见下方 **[📘 部署教程](#-部署教程一键发布到-workbuddy-云平台)**。

### 方式 C：Docker 自托管（数据常驻）

仓库未内置 Dockerfile；如需容器化，按 `server/run.py` 暴露的 `8899` 端口自行封装即可，数据卷挂到 `./data`。

---

## 📘 部署教程：一键发布到 WorkBuddy 云平台

本面板无需自己买服务器，直接在 WorkBuddy 里一键发布为在线应用，生成形如 `https://ab1dde4ffb5e275c5.app.workbuddy.host/` 的分享链接——把链接发给朋友，浏览器打开即用。当前线上示例（已验证可达，HTTP 200）即这一流程的产物。

### 为什么能直接发布

项目已满足 WorkBuddy 云平台的发布要求，发布脚本（`publish.js`）可自动完成「探测运行时 → 装依赖 → 起服务 → 就绪校验 → 出链接」：

| 平台要求 | 本项目实际情况 |
|---|---|
| 单 HTTP 端口 | `server/run.py` 用 FastAPI + uvicorn 起单端口服务 |
| 读取 `$PORT` | `resolve_port()` 优先读 `os.environ["PORT"]`，默认 `8899` |
| 绑定 `0.0.0.0` | uvicorn `host="0.0.0.0"`，网关可访问 |
| 健康检查 | `GET /api/health` 供就绪探测 |
| 声明依赖 | `requirements.txt`（轻量，无 akshare / mootdx） |

### 发布步骤

1. **打开项目**：在 WorkBuddy 中打开本仓库（已 `git clone` 或导入 `niucap`）。
2. **发起发布**：对助手说「发布为应用」或「publish」。助手会执行发布脚本，等价于：
   ```bash
   node <skill-dir>/scripts/publish.js \
     --dir /workspace/tick-stock-panel \
     --language python \
     --install-cmd "pip install -r requirements.txt" \
     --start-cmd "python server/run.py"
   ```
   > 脚本会注入 `PORT` 环境变量，`server/run.py` 自动监听该端口；`--start-cmd` 不写死端口，交给 `$PORT`。
3. **拿到链接**：脚本输出 JSON，形如
   ```json
   { "shareLink": "https://ab1dde4ffb5e275c5.app.workbuddy.host/", "verified": true }
   ```
   把 `shareLink` **整条**（若带 `?sharecode=...` 也要一并保留）发给对方。**不要删掉 `?sharecode=`**——它是访问凭据，缺了对方打开会看到「链接不完整」。
4. **访问**：浏览器打开链接，页面即「牛来选股面板」。首次打开是空库，数据初始化见下方说明。

### 重复发布 / 更新线上

- **同一项目重复发布，保持同一链接**——链接背后的内容会被覆盖，且对所有已拿到链接的人立即生效。
- 本地改完代码后，再对助手说「发布为应用」即可把新版本同步上线；建议先在本地预览确认，避免直接覆盖别人正在看的页面。
- 若 `verified` 为 `false`，稍等几秒再刷新链接（平台就绪有短暂延迟）。

### 下线

对助手说「取消发布 / 下线 / unpublish」，脚本会取消发布并使分享链接失效（破坏性操作，需你明确授权）。

### 云平台注意事项

- **数据临时**：云实例存储通常是临时的，重启 / 重新发布会清空本地 SQLite，需重新同步一次（后台管理 → 运行参数 → 开始同步）。
- 想要**数据常驻、多人共享**，用上方「首次部署必读」的云端 MySQL 协议库方案（填 `TICK_DB_HOST` 等，重启不丢数据）。
- 无需任何 API Key，行情来自腾讯 / 新浪 / 同花顺 / 东财公开接口。

---

## ⚠️ 首次部署必读：数据初始化

为控制仓库体积，**行情数据库（约 270 万行日线）不进 Git**。首次启动服务会**自动建一个空库**——此时页面能打开，但选股 / K 线 / 榜单是空的。两种灌数据方式：

- **本地 SQLite（最简单）**：进入「后台管理 → 运行参数 → 开始同步」，全市场日线约十几秒（走 eltdx 批量源）补齐。
  - 注意：云平台存储通常是临时的，重启会清空，需重新同步一次。
- **云端 MySQL 协议库（数据常驻、多人共享）**：复制 `.env.example` 为 `.env`，填好 `TICK_DB_HOST` 等 5 个变量指向你的 TiDB Cloud / MySQL，重启即自动切换。重启不丢数据，朋友也能直接访问。

---

## ⚙️ 配置（`.env`，可选）

复制 `.env.example` 为 `.env` 按需填写：

- **数据库**：`TICK_DB_HOST` 等 —— 留空用本地 SQLite，填了切云端 MySQL 协议库。
- **每日自动同步**：`TICK_SYNC_BARS_AUTO=1` + `TICK_SYNC_BARS_AT=15:30` + `TICK_SYNC_BARS_COUNT=250` + `TICK_SYNC_BARS_SCOPE=all`。
- **虚拟盘费率**：`TICK_FEE_RATE` / `TICK_STAMP_RATE` 等（仅新用户开户默认值）。
- **管理员**：`TICK_ADMIN_USERS=用户名1,用户名2` —— 不设则后台管理页整体关闭。

> `.env` 含数据库密码，**已被 `.gitignore` 排除，切勿提交**。

---

## 📁 目录结构

```
server/         FastAPI 后端：选股引擎 / 行情 / 龙虎榜 / 快讯 / 情绪 / 虚拟盘
web/            前端单页（原生 HTML/JS，无构建步骤）
scripts/        数据同步、健康检查等运维脚本
docs/           部署指南与各功能说明（中文）
tests/          自检与端到端测试
deploy.sh       一键启动 / 隧道 / 停止
requirements.txt 运行时依赖（轻量，无需 akshare/mootdx）
```

---

## 📜 许可证

本项目以 **MIT 许可证**开源，仅供量化学习与研究，**不构成任何投资建议**。

依赖 `eltdx` 为 **研究 / 学习专用许可证（禁止商业使用）**；运行期已做容错（缺失时自动回落腾讯源），个人学习用途无碍，商用请替换为其他数据源。

---

## 🏷️ Topics / 关键词

`a-share` · `china-stock` · `quant` · `stock-screener` · `selfhosted` · `real-time` ·
`龙虎榜` · `快讯` · `情绪分析` · `选股` · `量化` · `trading-dashboard` · `python` · `fastapi`
