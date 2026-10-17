# Cloudflare 部署可行性设计（2026-10 更新版）

> 本文是 `Cloudflare迁移评估.md`（2026-09）的**更新补遗**。结论方向不变（能搭、但要重分配职责），
> 但 2026-09 版的一个关键假设——「数据源只是 `requests` 抓腾讯/新浪、改成 `fetch` 即可」——
> 已被过去一个月的架构演进**推翻**：数据获取层现在重度依赖**本地二进制**，
> 而本地二进制在 Cloudflare Workers 上**绝对无法运行**。
>
> 结论先行：**能搭，但不是「搬过去」，而是「拆职责」；且云上只能跑 HTTP 源，二进制源必须留在原服务器。**

---

## 〇、一句话结论

| 问题 | 回答 |
|---|---|
| 面板能部署到 Cloudflare 吗？ | **能**，但只能承载「HTTP 数据源 + 纯计算 + 前端」这一部分 |
| 能整体搬过去吗？ | **不能**。本地二进制数据源（`eltdx` / `westock`）和全市场落库批任务必须留在原服务器 |
| 最大风险 | **腾讯/同花顺是否屏蔽 Cloudflare 出口 IP**（必须实测，不能推断） |
| 成本 | **$0（免费版，限 10ms CPU）/ $5 月（付费版，30s CPU）** |

---

## 一、相比 2026-09 评估，新增的三道硬墙

2026-09 评估时，`requirements.txt` 只有 `fastapi/uvicorn/requests/numpy/pandas+pymysql`，
数据层是「`requests` 抓腾讯/新浪 + TCP」。现在架构进化了，反而**更难迁**：

| 墙 | 证据 | 对 Workers 的影响 |
|---|---|---|
| 🔴 **`eltdx` 原生二进制客户端进了主链路** | `requirements.txt` 含 `eltdx>=3.2`；`server/sources/eltdx_source.py` 是唯一 import 点；`datasource.py` 以它为 K 线/快照**主源**，全市场批量取数 15 秒（腾讯逐只 48 分钟） | Pyodide/Wasm **不能加载 `.so`、不能连本地 7709 端口**。该源**绝对无法上云** |
| 🔴 **`westock` 走 subprocess 调 CLI 二进制** | `server/westock.py:197 subprocess.run(...)`；`run_cli()` 调外部可执行文件 | Workers **无进程、无任意二进制**。该模块不可上云 |
| 🔴 **多线程仍在** | `server/newbie.py:543 ThreadPoolExecutor` | Workers 是单线程 isolate，无 `threading`/`multiprocessing` 真并发 |
| ⚠️ 体量膨胀 | `app.py` 1697 → **4978 行**（接口数远超 56 个） | 不是硬阻，但迁移面更大、回归更重 |

> **好消息（也是逃生通道）**：`datasource.py` 已内置分层降级——`eltdx` 主源 → 腾讯/新浪 HTTP 降级 →
> 同花顺 `hithink` HTTP 三级备源；`eltdx` 可经环境变量 `TICK_ELTDX=0` **一键关闭回落腾讯**。
> 因此「关闭 eltdx + westock，只跑 HTTP 源」在数据通路上是**被设计支持的**，不是临时 hack。

---

## 二、数据源分层现状（决定哪些能上云）

| 源 | 协议/形态 | 能否上 Workers | 备注 |
|---|---|---|---|
| **eltdx** | 原生 `.so` + 通达信 7709（TCP 本地客户端） | 🔴 **不能**（二进制/本地 socket） | 主源；批量取数快；提供 `amount` 成交额字段 |
| 腾讯 / 新浪 | **HTTP**（`qt.gtimg.cn` 等） | ✅ **能**（`fetch`） | 降级源；单请求快；**不提供 `amount`（成交额恒空）** |
| 同花顺 hithink | **HTTP**（X-Token / JWT） | ✅ **能**（`fetch`） | 三级备源；黄金/宏观已走此路 |
| westock | subprocess + 外部 CLI 二进制 | 🔴 **不能** | 基金/筹码等；依赖外部可执行文件 |

**推论**：Workers 上能跑的，只有「腾讯/新浪 + 同花顺」这两条 **HTTP 源**；
`eltdx` 与 `westock` 必须整体留在原服务器，或通过反向代理回源到原服务器。

**代价（必须接受）**：剥离 `eltdx` 后，K 线/快照的 `amount`（成交额）字段会空，
影响「放量/缩量」类策略；且失去了 15 秒全市场批量扫描能力——但后者本来就不该放在每次请求里。

---

## 三、2026-10 Cloudflare 能力核实（更新）

| 能力 | 状态 | 对本项目的意义 |
|---|---|---|
| **Python Workers + FastAPI** | 已 GA，`workers.asgi` | `app.py` 可整体搬过去跑（代码层面） |
| **`cloudflare:sockets` 出站 TCP** | ✅ 已支持（Free/Paid） | TiDB 经 TCP 也能连（不再强依赖 HTTP 驱动） |
| **Hyperdrive 连接池** | 支持 PG；MySQL/TiDB 可用非最优 | 继续用现有 TiDB 最省事 |
| 静态资源 | 20,000 文件 / 单文件 25MiB | 前端 `index.html` 单文件直接放 Static Assets |
| CPU 时间 | Free **10ms** / Paid **30s**（可到 5min） | 🔴 免费版跑不动全市场扫描；网络 I/O 不计入 |
| 内存 | 128MB/isolate | 够用但紧，必须二进制格式（非 JSON） |
| 同时出站连接 | ≤6/次调用 | 并发抓行情要加信号量 |
| 多线程 | ❌ 不支持 | `newbie`/`app` 后台线程要改 `asyncio` |
| **出站 IP 不在公开段** | 官方特性 | 🔴 腾讯/同花顺可能按异常来源风控屏蔽 |

---

## 四、推荐架构：职责重分配（更新版）

```
┌──────────────────────────────────────────────────────────────┐
│  用户浏览器                                                        │
│  web/index.html（Cloudflare Static Assets，免费、全球 CDN）        │
└───────────────┬──────────────────────────────────────────────┘
                │  HTTPS
                ▼
┌──────────────────────────────────────────────────────────────┐
│  Cloudflare Workers（Python + FastAPI，付费 $5/月，CPU 30s）       │
│  ── 上云部分（无状态 / 纯计算 / HTTP 源）──                          │
│  · /api/quote  /api/kline  /api/indicators_full  （腾讯/新浪 fetch）│
│  · /api/screener  /api/strategy_scan  （内存计算）                  │
│  · 同花顺 hithink 备源（黄金/宏观，fetch JWT）                       │
│  · 指标计算 numpy/pandas（Pyodide）                                 │
│  · TiDB 只读（Hyperdrive 或 sockets）                               │
└───────────────┬──────────────────────────────────────────────┘
                │  以下经反向代理 / Tunnel 回源
                ▼
┌──────────────────────────────────────────────────────────────┐
│  原服务器（保留，跑有状态 / 二进制 / 重批）                           │
│  · eltdx 全市场落库（15 秒批量，写入 TiDB/SQLite）                  │
│  · westock 基金/筹码（subprocess CLI）                              │
│  · newbie 多线程打分 + app 后台预热线程                             │
│  · 自选股 / 虚拟盘 事务写（TiDB）                                   │
│  · 长时间批任务（墙钟 48 分钟那种）                                 │
└──────────────────────────────────────────────────────────────┘
```

**一句话**：云上只做「查」和「算」，原服务器继续做「采」和「写」。

---

## 五、分步迁移（重排）

### 第 1 步 · 验证可行性（最关键，先做，约半天）

用一个最小 Worker 实测 **4 件事**（比 2026-09 多一件）：

1. **Workers `fetch` 能否抓到腾讯行情**（`qt.gtimg.cn`）—— 验证 IP 是否被屏蔽
2. **TiDB 能否连上**（Hyperdrive 或 `cloudflare:sockets` TCP 或 `@tidbcloud/serverless` HTTP）
3. **全市场算指标的真实 CPU 耗时**（看是否超 10ms / 30s）
4. **关掉 `eltdx`（`TICK_ELTDX=0`）后，HTTP 源路径功能是否不塌**（amount 字段缺失的影响面）

> 1~3 任意一件不成立，方案就要调整；第 4 件决定剥离 eltdx 的代价。

### 第 2 步 · 迁移「只读查询类」接口

把接口里**无状态、纯计算、走 HTTP 源**的部分搬上 Workers：
- 行情/K线/指标查询（腾讯/新浪 fetch）
- 策略选股、扫描（内存计算）
- 黄金/宏观（同花顺 hithink fetch）
- 前端静态页

### 第 3 步 · 保留有状态/二进制在原处 + 串联

- 自选股/虚拟盘事务写、eltdx 落库、westock、多线程预热、长批任务 → 继续原服务器
- 用 Cloudflare Tunnel 或 Worker 反向代理把两部分串起来（二进制相关接口回源）

---

## 六、成本

| 项 | 现状 | 全上 Cloudflare |
|---|---|---|
| 计算 | 自备服务器（已投入） | 免费版 $0（限 10ms）/ 付费 $5 月（30s CPU） |
| 数据库 | TiDB Serverless（已有） | 继续用，Hyperdrive 或 sockets 连 |
| 域名 + HTTPS | Tunnel（免费） | 内置（免费） |
| **合计** | 已有成本 | **$0 ~ $5/月** |

成本不是瓶颈；**可行性（数据源 IP 可达性）才是**。

---

## 七、风险清单（更新）

| 风险 | 严重度 | 应对 |
|---|---|---|
| **腾讯/同花顺屏蔽 Cloudflare 出口 IP** | 🔴 高 | 第 1 步必须实测；不行则相关接口回源原服务器 |
| **剥离 eltdx 后 `amount` 字段缺失** | 🔴 中-高 | 影响放量类策略；可接受则忽略，或在原服务器算好经 TiDB 读 |
| 免费版 10ms CPU 不够 | 🔴 高 | 上付费版（$5/月） |
| 本地二进制（eltdx/westock）无法上云 | 🔴 高 | 设计上就留在原服务器，不硬搬 |
| Hyperdrive 对 MySQL/TiDB 支持非最优 | ⚠️ 中 | 实测；备选 sockets TCP 或 `@tidbcloud/serverless` HTTP |
| 无多线程改造量 | ⚠️ 中 | 改 `asyncio.gather`；实测串行已够 |
| 出站连接 ≤6 | ⚠️ 中 | 加信号量 |
| 冷启动 ~1s（Python Wasm） | 🟢 低 | 快照可接受 |

---

## 八、结论与建议

1. **能搭，但必须「拆职责」**：云上只跑 HTTP 源查询 + 纯计算 + 前端；eltdx/westock/多线程/长批留在原服务器。
2. **2026-09 评估已过时**：当年「改 `requests`→`fetch` 即可」的判断对**主数据源**不成立，因为主源现在是本地二进制 `eltdx`。
3. **最大未决风险是数据源 IP 屏蔽**：必须先花半天实测，不能靠推断（腾讯接口 `access-control-allow-origin: *` 是积极信号，但 Cloudflare 出口 IP 不在公开段，仍可能被风控）。
4. **建议下一步**：先写最小验证 Worker（我可以现在就产出可部署代码），实测上面 4 件事；通过后再进入第 2 步。

> 如果你倾向动手，我可以直接给出第 1 步的验证 Worker 代码（含 fetch 腾讯行情 / 连 TiDB / 测 CPU 三段 handler），你部署一次即可拿到实测结论。
