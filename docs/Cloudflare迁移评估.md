# 迁移到 Cloudflare 的可行性评估

> 结论先行：**能迁，而且比预想的好。但要做 4 处改造，且有一个硬约束（CPU 时间）必须绕开。**
> 不建议「直接扔掉现有部署全搬过去」，建议「核心 API 上 Workers，重计算留在原处」。

---

## 一、这套系统的真实构成

先看清楚要搬什么：

| 模块 | 行数 | 性质 | 能否上 Workers |
|---|---|---|---|
| `datasource.py` | 591 | 抓腾讯/新浪行情（`requests` + TCP） | ⚠️ 需改造（HTTP 客户端） |
| `app.py` | 1697 | FastAPI，**56 个接口** | ✅ Python Workers 已支持 FastAPI |
| `store.py` | 1577 | SQLite / TiDB 双驱动 | ✅ 有 Hyperdrive |
| `indicator.py` + `mytt.py` | 1210 | 技术指标，numpy/pandas | ✅ Pyodide 已支持 |
| `patterns.py` | 721 | 形态识别（已定不接入） | — |
| `screener.py` | 322 | 策略选股（内存计算） | ✅ CPU 够用 |
| `newbie.py` | 623 | 打分引擎，**多线程** | 🔴 无多线程 |
| `tdx.py` / `westock.py` / `hithink.py` | 1741 | 外部 CLI/API 数据源 | 🔴 基本不可行 |
| `web/index.html` | 199 KB 单文件 | 前端 + ECharts | ✅ Static Assets |
| `data/tick.db` | 405 KB | 本地 SQLite | → 需转 D1 或继续用 TiDB |

**依赖清单**（`requirements.txt`）出奇地干净：只有 5 个包
`fastapi / uvicorn / requests / numpy / pandas`，加 `pymysql`。**没有 akshare、没有 mootdx。**

这是好消息 —— 依赖越少，迁移越容易。

---

## 二、Cloudflare 侧能力核查（2026-09 现状）

### ✅ 支持的部分

| 能力 | 状态 | 说明 |
|---|---|---|
| **Python Workers** | **已 GA** | 官方支持，不再是实验性 |
| **FastAPI** | **官方支持** | `workers.asgi` 连接器，2 分钟即可部署全套 FastAPI |
| **numpy / pandas** | **支持** | Pyodide 提供，Wasm 快照让冷启动从 10s 降到 ~1s |
| **Hyperdrive** | **支持 MySQL/PG** | 内置连接池，解决 Serverless 反复建连的问题 |
| 静态资源 | 支持 | 20,000 个文件，单文件 25 MiB（我们的 199KB 单页绰绰有余） |
| Cron Triggers | 支持 | 免费版 5 个，付费 250 个（同步任务可用） |

### 🔴 硬约束（必须绕开）

| 约束 | 免费版 | 付费版 | 对本项目的影响 |
|---|---|---|---|
| **CPU 时间** | **10 ms** | 30 s（可调到 5 min） | 🔴 **决定性约束** |
| 内存 | 128 MB | 128 MB | ⚠️ 够用但紧（见下） |
| **多线程 / 多进程** | ❌ **不支持** | ❌ **不支持** | 🔴 `newbie.py` 的 `ThreadPoolExecutor` 失效 |
| 同时出站连接 | 6 | 6 | ⚠️ 并发抓行情受限 |
| 请求数 | 10 万/天 | 不限 | ✅ 够用 |
| Cron CPU | 10 ms | 30 s（间隔>1h 可到 15 min） | ⚠️ 落库任务受限 |

> **关键点**：`等待网络请求（fetch / 数据库查询）不计入 CPU 时间`。
> 这条很重要 —— 意味着「等数据源返回」「等 TiDB 返回」都不算 CPU，
> 只有真正的计算（算指标、跑策略）才算。

---

## 三、四个必须解决的问题

### 🔴 问题 1：CPU 10ms 是硬天花板

这是**最关键的一条**。我们的重计算流程：

| 操作 | 实测 CPU 时间 | 免费版 10ms | 付费版 30s |
|---|---|---|---|
| 全市场算全套指标（3761 只） | **110 ms** | ❌ 超 11 倍 | ✅ |
| 策略选股（15 个策略扫全市场） | 预估 50~200 ms | ❌ | ✅ |
| 单只股票算指标 | ~0.03 ms | ✅ | ✅ |
| 解析 199KB HTML / JSON | ~2 ms | ✅ | ✅ |

**结论：免费版（10ms）跑不动「全市场扫描」这类操作。**

对策有三条：

| 方案 | 说明 | 代价 |
|---|---|---|
| **A. 上付费版** | $5/月起，CPU 提到 30s（默认）→ 5 min | 每月 $5，最省事 |
| B. 结果预计算 | 用 Cron 定时算好存起来，请求只读结果 | ⌛ 时效性差，Cron 也有 CPU 限制 |
| C. 拆分计算 | 把大计算切成多请求，或用 Durable Objects | 🛠 改造成本高 |

> **注意**：付费版 Cron 若间隔 >1 小时，CPU 可到 **15 分钟** —— 这对「全市场落库」
> 这类批任务很友好（我们实测 48 分钟是**墙钟时间**，其中绝大部分是等待数据源限流，
> CPU 占用极低）。

---

### 🔴 问题 2：没有多线程

`newbie.py:543` 用了 `ThreadPoolExecutor`，`app.py` 有多个 `threading.Thread` 做后台预热。

Cloudflare Workers 是**单线程 isolate**，这些代码会直接失效。而且：

- ❌ 无 `threading` 真并发
- ❌ 无 `multiprocessing`
- ❌ 无 `uvloop` / `uvicorn`（工作线程模型不同）
- ⚠️ `asyncio` 可用，但要重写为异步风格

**对策**：把 `ThreadPoolExecutor` 改为 `asyncio.gather`（并发抓行情、并发算指标）。
好在我们已经验证过：全市场**串行**算指标只要 110ms，本来就不太需要多线程。

---

### ⚠️ 问题 3：出站请求方式要换

现在的数据源用 `requests` 抓腾讯/新浪：

```python
requests.get("https://qt.gtimg.cn/q=sh600519")
```

Python Workers **不支持 `requests`**（Pyodide 无原始 socket HTTP 客户端），
必须改成 JavaScript 的 `fetch`：

```python
from js import fetch
resp = await fetch(url)          # 只能走 fetch
txt = await resp.text()
```

**影响范围**：`datasource.py` 里所有 `_get()` 调用点。好消息是我们在
`datasource.py` 里已经做了**统一封装的 `_get()`**，改一处即可。

**但要注意两个衍生问题**：

1. **同时出站连接上限 6 个** —— 我们现在的并发抓取可能超限，需要加信号量
2. **数据源可能屏蔽 Cloudflare 的 IP** —— 腾讯/新浪对海外 IP + 数据中心 IP
   的态度未经验证，**这是最大的不确定风险，必须实测**

---

### ⚠️ 问题 4：128 MB 内存够，但要选对格式

全市场 3761 只 × 500 根日线：

| 存储格式 | 大小 | 能放进 128MB？ |
|---|---|---|
| **numpy float32 二进制** | **39.4 MB** | ✅ 有余量 |
| JSON | 82.5 MB | ⚠️ 勉强，风险高 |
| `list[dict]` | 500 MB+ | ❌ |

**结论**：必须用二进制格式。如果走 KV/R2 做缓存，要先序列化成
`Uint8Array`（用 `arrayBuffer`），**不能存 JSON**。

---

## 四、数据库：三条路可选

| 方案 | 说明 | 推荐度 |
|---|---|---|
| **A. 继续用现有 TiDB + Hyperdrive** | 数据不用迁，Hyperdrive 做连接池 | ⭐⭐⭐ 最省事 |
| B. 迁到 D1 | Cloudflare 原生 SQLite，单库 10GB | ⭐⭐ 但每库 10GB，188 万行约 100MB，够用 |
| C. 保留 TiDB，但只读 | 写操作（自选股/虚拟盘）也留在 TiDB | ⭐⭐⭐ 与 A 类似 |

**关于 Hyperdrive 的一个已知限制**：
官方文档明确说「Hyperdrive **currently supports PostgreSQL and PostgreSQL-compatible**」，
**MySQL/TiDB 不在推荐列表里**。但同时又提供了 MySQL 教程和 Python Workers 的
`pymysql` 驱动示例 —— 说明可用但非最优路径。

> ⚠️ 这一点存在文档矛盾，**建议实测验证**（用 `aiomysql` 或 `pymysql` 连 TiDB 试一次）。

另外 Cloudflare 有官方的 **`@tidbcloud/serverless`** 驱动（HTTP 协议，走 fetch），
虽然不在 Workers 的「数据库集成」表里，但值得一试 —— HTTP 方式比 TCP 更适合 Workers。

---

## 五、迁移路径建议

### 不推荐：一次性全搬

理由：
1. `tdx.py`（590行）依赖本地通达信客户端 —— 根本无法上云
2. `newbie.py` 多线程要重写
3. CPU 限制要重新设计计算流程
4. 数据源 IP 屏蔽风险未验证

### 推荐：分三步走

**第 1 步 · 验证可行性（最关键，先做）**

用一个最小 Worker 验证三件事：
1. Python Workers 里 `fetch` 能不能抓到腾讯行情（**验证 IP 是否被屏蔽**）
2. `pymysql` 或 `@tidbcloud/serverless` 能不能连上现有 TiDB
3. 全市场算指标的真实 CPU 时间是多少（用 `cpu_ms` 配置测）

> 这三件事任何一件不成立，方案都要调整。**建议先花半天做这个验证。**

**第 2 步 · 迁移「只读查询类」接口**

把 56 个接口里**无状态、纯计算**的部分搬上去：
- `/api/quote`、`/api/kline`、`/api/indicators_full`（算完即返回）
- `/api/screener`、`/api/strategy_scan`（内存计算）
- 前端静态资源（`web/index.html`）

这些**不依赖后台线程、不依赖持久状态**，最适合 Serverless。

**第 3 步 · 保留有状态部分在原处**

以下继续跑在现在的服务器上：
- 自选股 / 虚拟盘（要事务保证）
- `tdx.py`（依赖本地客户端）
- `newbie.py` 的后台预热线程
- 全市场落库任务（墙钟 48 分钟，不适合 Worker）

最后用 Cloudflare Tunnel 或 Worker 反向代理把两部分串起来。

---

## 六、成本对比

| 项 | 现状 | 全上 Cloudflare |
|---|---|---|
| 服务器 | 自备（已投入） | 免费版 $0 / 付费版 $5/月 |
| 数据库 | TiDB Serverless（已有） | 可继续用 / D1 另算 |
| 域名 + HTTPS | Cloudflare Tunnel（免费） | 内置（免费） |
| **合计** | 已有成本 | **$0 ~ $5/月** |

**成本不是问题**，$5/月 能拿到 30s CPU，足够跑全市场扫描。

---

## 七、风险清单

| 风险 | 严重度 | 应对 |
|---|---|---|
| **数据源屏蔽 Cloudflare IP** | 🔴 高 | 第 1 步必须验证（见下方实测线索）；不行则用该数据源时回源到自建服务器 |
| **CPU 10ms 免费版不够** | 🔴 高 | 上付费版（$5/月） |
| Hyperdrive 对 MySQL 支持不明确 | ⚠️ 中 | 实测；备选 `@tidbcloud/serverless`（HTTP） |
| 无多线程导致改造量 | ⚠️ 中 | 改 `asyncio`；实测串行性能已够 |
| 出站连接上限 6 | ⚠️ 中 | 加信号量限流 |
| 冷启动 ~1s（Python） | 🟢 低 | Wasm 快照，可接受 |
| 前端 199KB 单文件 | 🟢 低 | 直接放 Static Assets |

### 关于「数据源是否屏蔽 Cloudflare IP」的实测线索

本机验证结果（2026-09-21）：

```
GET https://qt.gtimg.cn/q=sh600519
  → HTTP 200，40 ms 响应，无 UA 要求
  → 响应头: access-control-allow-origin: *
  → server: openresty/1.11.2.1
```

**`access-control-allow-origin: *` 是个积极信号** —— 说明该接口设计上就是
面向公开跨域调用的（很多第三方网页直接在前端调它）。这类接口一般不做
严格的 IP 地域封锁，否则大量合法调用方会失效。

**但这是推断而非证明**。Cloudflare Workers 的出口 IP 与国内出口不同，
且 Cloudflare 有「不使用公开 IP 段发起出站连接」的特性
（官方文档：*TCP Workers outbound connections are sourced from a prefix
that is not part of the list of IP ranges*），这可能导致某些风控系统
把它识别为异常来源。

> ✅ **必须在 Cloudflare 上实测一次**，不能靠推断。这是整个迁移方案的前置条件。

---

## 八、结论

**能搭，但不是「搬过去」而是「重新分配职责」。**

- ✅ **可以上**：行情查询、指标计算、策略选股、前端页面（占系统价值的大头）
- 🔴 **不能上**：通达信本地客户端、多线程后台任务、长时间批处理
- ⚠️ **必须先验证**：数据源 IP 可达性、TiDB 连接、真实 CPU 耗时

**最大风险是「腾讯/新浪是否屏蔽 Cloudflare IP」** —— 这个不验证，
后面所有设计都是空中楼阁。**建议先花半天做第 1 步的验证。**

如果你倾向动手，我可以直接把第 1 步的验证 Worker 写出来跑一遍。
