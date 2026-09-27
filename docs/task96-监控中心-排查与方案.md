# #96 监控中心 + 外部推送 —— 排查与方案

备份：`backups/task96-20260928-031220/`（app.py / store.py / index.html）

---

## 一、现状盘点（可复用 vs 需新建）

| 能力 | 现状 | 处置 |
|------|------|------|
| 用户级告警存储 | ❌ 无（只有巡检告警 `health_alerts.log`，是运维日志不是用户告警） | 新建 `alerts` 表 |
| 规则存储 | ❌ 无 | 新建 `alert_rules` 表 |
| 实时行情 | ✅ `datasource.quote_tencent([codes])`（/api/quote 在用） | 复用 → 价格类规则 |
| 个股指标 | ✅ `HistoryEngine.metrics(code, need=[...])`（全市场内存缓存，/api/market_phase 同款） | 复用 → 信号类规则 |
| 全市场异动 | ✅ `hithink.anomaly_list(30)`、`limit_up_pool()`、`hot_rank()` | 复用 → 市场类规则 |
| 策略命中 | ✅ `screener` / `strategy_eval` | 复用 → 策略类规则 |
| 建表与迁移 | ✅ `store._SCHEMA`（SQLite）+ `_SCHEMA_MYSQL`（MySQL/TiDB 双驱动），`_migrate()` 幂等补列 | **两处都要加**，否则云上 MySQL 模式缺表 |
| 配置存储 | ✅ `meta(k,v)` 表 | 复用存 Webhook 配置，不新建表 |
| 登录态 | ✅ `_require_login(request)` / `_current_user(request)` | 复用；但监控按「单机自用」定位，**允许匿名**（与自选股一致，虚拟盘才强制登录） |
| 后台线程 | ✅ app.py 已有多个 daemon 线程（预热/调度） | 复用同款模式，新增一个轮询线程 |

---

## 二、风险点与方案

### R1 检测轮询不能拖垮服务 / 触发限流
- 风险：每轮遍历规则 × 逐只取实时行情，规则多了会打爆腾讯行情接口，且检测线程与请求线程抢 SQLite 写锁（历史上出现过 228MB WAL 僵死锁）。
- **方案**：
  - 单线程轮询，默认 **60 秒**一轮，规则全部禁用时线程空转 sleep。
  - 价格类规则**批量取行情**（`quote_tencent` 一次传全部 code，与 /api/quote 一致，单次上限 100），不逐个请求。
  - 告警落盘**批量单事务**写入，写完立即 `commit`，避免长事务持锁。
  - 轮询线程用**独立 sqlite 连接**（各线程独立连接，避免跨线程共享连接）。

### R2 冷却去重：同一规则不能刷屏
- 风险：条件持续满足时每轮都触发 → 告警洪水 + Webhook 被限流（企微每分钟 ≤20 条）。
- **方案**：规则带 `cooldown_min`（默认 60），`last_fired` 时间戳；未过冷却直接跳过。落盘前再按 `(rule_id, code, 当日)` 去重一次（同规则同标的同日只留一条），双保险。

### R3 推送失败绝不能阻断主流程（对方铁律）
- 风险：Webhook 超时/网络失败 → 检测线程卡死或异常退出，监控整体失效。
- **方案**：
  - 推送包在 `try/except` 内，**异常只记日志不影响落盘**；
  - `urllib` 请求带 `timeout=8`；
  - 瞬时 5xx 退避重试 3 次（0.5s/1s/2s）——对方注明「瞬时失败不重试会被冷却窗口压掉」，必须重试；
  - 未配置 Webhook 时静默跳过（不报错）。

### R4 签名口径（照搬源码，别自己发明）
- 飞书：`HmacSHA256(f"{timestamp}\n{secret}")` → Base64，body 里放 `timestamp` + `sign`；无 secret 时不签。
- 企微：`?key=` 直接 POST，markdown 内容按**字节**截断 4096（中文 3 字节）。
- 通用：`secret` 时对**原始 body** 做 HMAC-SHA256，头 `X-TickFlow-Timestamp` / `X-TickFlow-Signature: sha256=<hex>`；信封 `{event, timestamp, title, body, data}`。
- **方案**：`server/webhook.py` 三套适配，签名用 `hmac`+`hashlib`+`base64`（stdlib，不加依赖）。

### R5 双驱动 schema（SQLite + MySQL）必须同步
- 风险：只在 SQLite schema 加表 → 云上 MySQL 模式 500 报错。
- **方案**：`_SCHEMA` 与 `_SCHEMA_MYSQL` 各加一份建表 SQL；MySQL 版用 `VARCHAR`/`DOUBLE`/`BIGINT`，TEXT 列不建索引。

### R6 前端入口与「未读」语义
- 风险：告警不断产生，用户不知道有新消息。
- **方案**：新增「🔔 监控中心」tab；tab 上挂未读数量角标（沿用现有角标样式）；告警列表支持「标记已读 / 清空」；轮询 15s 拉一次新告警（不做 SSE，够用且省）。

### R7 规则表达式的安全边界
- 风险：若做成「任意表达式求值」就是远程代码执行。
- **方案**：**不做表达式引擎**。规则 = 结构化条件数组 `[{field, op, value}]` + AND/OR，`field` 走白名单映射，`op` 限定 `> >= < <= ==`，绝不 eval。

---

## 三、落地设计（最小可用）

### 数据模型（新增 2 表，Webhook 配置复用 meta）

```sql
CREATE TABLE IF NOT EXISTS alert_rules (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,        -- price|signal|market|strategy
  code         TEXT NOT NULL DEFAULT '',
  conds        TEXT NOT NULL,        -- JSON: [{"field":"pct","op":">=","value":5}]
  logic        TEXT NOT NULL DEFAULT 'AND',
  severity     TEXT NOT NULL DEFAULT 'warn',   -- info|warn|critical
  cooldown_min INTEGER NOT NULL DEFAULT 60,
  push         INTEGER NOT NULL DEFAULT 1,     -- 是否外部推送
  enabled      INTEGER NOT NULL DEFAULT 1,
  last_fired   REAL NOT NULL DEFAULT 0,
  fired_count  INTEGER NOT NULL DEFAULT 0,
  created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  ts       REAL PRIMARY KEY,
  rule_id  TEXT NOT NULL,
  rule_name TEXT NOT NULL DEFAULT '',
  kind     TEXT NOT NULL,
  code     TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'warn',
  msg      TEXT NOT NULL,
  value    REAL,
  is_read  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_read ON alerts(is_read, ts);
```
保留策略（照搬对方）：`MAX_DAYS=7`、`MAX_RECORDS=5000`，每 20 次写入滚动清理一次。

### 四类规则的取值来源

| 类型 | 取值 | 可用字段（白名单） |
|------|------|--------------------|
| price 价格 | `quote_tencent([code])` 实时 | `price`、`pct`（涨跌幅%）、`amount`、`turnover` |
| signal 信号 | `HistoryEngine.metrics(code)` | `close`、`ma5/10/20/60`、`rsi6/rsi14`、`macd`、`hhv20`、`llv20`、`ma_align` 等 |
| market 市场 | `hithink.anomaly_list/limit_up_pool` | `limit_up_cnt`、`anomaly_cnt` |
| strategy 策略 | `screener` 命中 | `hit_cnt`（策略命中数） |

### API（`/api/alerts/*`）
- `GET  /api/alerts/rules` 规则列表
- `POST /api/alerts/rules` 新建（body: name/kind/code/conds/logic/severity/cooldown_min/push/enabled）
- `PUT  /api/alerts/rules/{id}` 更新（含启停、已读）
- `DELETE /api/alerts/rules/{id}`
- `GET  /api/alerts?unread=1&limit=50` 告警流
- `POST /api/alerts/read` 标记已读（body: ts 或 all）
- `DELETE /api/alerts` 清空
- `GET/PUT /api/alerts/webhook` Webhook 配置（存 meta）
- `POST /api/alerts/test` 发送测试推送
- `POST /api/alerts/check` 手动触发一轮检测（便于演示与自检）

### 文件
- 新建 `server/alerts.py`：规则 CRUD + 检测循环 + 告警落盘 + 冷却去重
- 新建 `server/webhook.py`：飞书/企微/通用 + HMAC 签名 + 静默降级
- 改 `server/store.py`：`_SCHEMA` / `_SCHEMA_MYSQL` 各加 2 张表
- 改 `server/app.py`：注册 API + 启动检测线程
- 改 `web/index.html`：新增「🔔 监控中心」tab（规则管理 + 告警流 + Webhook 配置 + 未读角标）

### 自检
`tests/check_alerts.py`：字段白名单与条件求值、冷却去重、飞书/企微签名正确性（含已知向量）、推送失败静默降级、保留策略、API 冒烟。
