# free-stockdb 蒸馏与「牛来面板」可优化项对照

> 来源：`https://github.com/hello245m/free-stockdb`（抓取于 2026-10-06，2776★ / 416 fork / MIT / 2026-10-04 仍在更新）
> 许可：**MIT** → 可萃取架构与算法思路；本报告只给**可抄常量、算法与签名**，落地一律自行实现，不搬运其 C++ / LevelDB 代码。
> 蒸馏范围：README + `docs/DATA_SOURCE.md` + `stockdb.conf` + `sync_url.txt` + `pybao/zhibiao.py` + `pybao/stock_sdk.py` + `pybao/native_mcp.py` + `cpp/src/updater.cpp` + `cpp/src/server.cpp` + `调用方式/`。

---

## 一、项目概况

| 项 | 内容 |
|---|---|
| 定位 | **A 股本地量化数据引擎（数据底座）**：同步、清洗、复权、存储、批量查询、指标计算 |
| 技术栈 | **C++17 服务/同步器**（libcurl + OpenSSL + LevelDB/LGDB + Zstd）+ Python SDK（`stock_sdk.py`）+ 原生 MCP（`native_mcp.py`，**纯标准库零依赖**） |
| 存储 | LevelDB（配置段 `leveldb:` cache_size=500 / write_buffer_size=16 / block_size=32 / compression=yes）+ binlog 复制；**Zstd 压缩，宣称比 CSV/MySQL 小 3 倍** |
| 服务 | `127.0.0.1:7899`，支持 `auth`（强密码）、`readonly`、`slowlog_timeout` |
| 数据覆盖 | 日/周/月 + 1/5/15/30 分钟 K + tick（按需）；价格/量额/换手/估值/市值/ST 字段 |
| 指标 | **39 种技术指标** + 5 种指数加权算法（`ZB_*` 计算核心） |
| 调用方式 | Python SDK / HTTP API / **Excel·WPS 宏** / HTML 网页 / **AI MCP** 五种 |
| 体量 | 磁盘 5GB（仅日线）～20GB（含全量分钟线） |

**与牛来面板的关键差异**：它是**数据工程底座**（面向量化研究员，靠 Python/AI 调用），我们是**应用面板**（面向选股用户，前端交互）。**定位互补而非竞争**——我们处在它的下游。真正有吸收价值的是三块：**板块映射表、MCP 接口、复权/指数这类纯计算能力**。

---

## 二、它解决的核心痛点（README 原话提炼）

README 用一张「数据工程工作量对比表」立论：全市场 7000+ 股票分钟线回测，用远程 API 方案要 **10-15 个工作日**才能跑通第一个策略（下载 3-5 天 / 清洗 1-2 天 / 复权 1-2 天 / 存储 1-3 天 / 指标 2-3 天 / 板块映射 1-2 天 / 接口对接 1-2 天），它压缩到 **30 分钟**。

对照我们：这七项里**我们已经解决了 5 项**（数据源走 westock/eltdx、后台一键灌数、SQLite/MySQL + `bar_sync` 增量、61 项指标、多入口 Web），但**板块映射和 AI 调用入口这两项仍是缺口**——下面第四节逐条对照。

---

## 三、源码级落点（可抄常量与签名）

### 3.1 板块映射 `bk.get()`（`pybao/zhibiao.py`）

```python
CATEGORY_MAP  = {0: "概念", 1: "申万一级", 2: "申万二级", 3: "申万三级"}
BOARD_FIELDS  = ("code", "name", "source", "type", "group", "category", "symbols")
FIELD_ALIASES = {"symbol": "symbols", "symbols": "symbols",
                 "symbls": "symbols", "codelist": "symbols"}   # 容错：拼错也能查
```
- 类 `BoardIndex`：**股票 → 板块** 与 **板块 → 成分股** 的**双向索引**（毫秒级）
- 覆盖：申万一二三级 + **1200+ 概念板块**
- 调用：`bk.get(x=代码或板块名或代码列表, category=0-3, fields="code,name,symbols")`

### 3.2 复权折算 `_apply_fq_in_memory()`（`pybao/stock_sdk.py:371`）

```python
# 预加载：self._fq_dates[code] = 除权日有序列表；self._fq_cums[code] = 累计因子
idx = bisect.bisect_right(dates, r_date_str) - 1      # 找 <= 当前日的最大除权日
f_current = cums[idx] if idx >= 0 else 1.0
ratio = (cums[-1] / f_current) if fq_type == 'qfq' else (1.0 / f_current)
# 只折算价格字段，不动 volume/amount
for field in ['open', 'high', 'low', 'close', 'pre_close']: r_copy[field] /= ratio
# 精度：代码以 1/5 开头（ETF/基金）3 位小数，其余 2 位
decimals = 3 if code.startswith(('1', '5')) else 2
# 关键点：r.copy() 后再改，避免污染底层缓存对象
```
设计要点：**复权因子不落历史价**，查询时按需折算，所以一份数据能同时出 qfq/hfq/不复权。

### 3.3 指标与指数计算 `zb.get()` / `jisuan()`

```python
METHOD_NAMES    = {1: "equal", 2: "float_mv", 3: "amount", 4: "volume", 5: "total_mv"}
DATA_FIELD_INDEX= {"open":2,"high":3,"low":4,"close":5,"volume":6,
                   "amount":7,"float_mv":8,"total_mv":9}       # 列式索引
BASIC_INDICATORS= {"ma","ema","sma","wma","dma","std","sum","hhv","llv","ref"}
BASIC_DEFAULT_N = {"ma":5,"ema":5,"sma":5,"wma":5,"dma":10,
                   "std":5,"sum":5,"hhv":5,"llv":5,"ref":1}

def jisuan(..., method: int = 1, base: float = 1000.0, cross: Any = False):
    # 分钟 K 无 float_mv/total_mv → 只允许 method=1/3/4
    if _freq(frequency) != "1d" and method in (2, 5): raise ValueError(...)
    # cross: False | True | "with_value"（金叉信号，with_value = 值与信号都返回）
    if cross not in (False, True, "with_value"): raise ValueError(...)
```
- **列式数组传给 native 核心**（`BATCH` / `BATCH_CROSS` / `ZHISHU`），全市场批量计算，宣称比 pandas 快 3 倍
- 指标参数 `n` 支持**每个指标独立传**（`n=["5,10,20", None, "12,26,9"]`）

### 3.4 同步协议（`docs/DATA_SOURCE.md` + `cpp/src/updater.cpp`）

```text
manifest.txt 每行：<sha256> <size-in-bytes> <relative-path>
```
- 同步流程：读清单 → 下载到 `*.part` → **校验大小 + SHA-256** → 替换目标文件；已存在且校验一致的**跳过**（天然断点续传）
- 路径**禁止 `..`**（防目录穿越）；公网镜像强制 HTTPS；发布新快照**先传数据文件、最后更新 manifest**（避免半完成清单）
- 源可换：`sync_url.txt` 第一条有效行 / `--source` / `file://` 本地目录 → **数据源解耦，离线可同步**

### 3.5 原生 MCP（`pybao/native_mcp.py`）

```python
class NativeMCP:            # 纯标准库（asyncio + json），零第三方依赖，不装 MCP SDK
    # JSON-RPC 2.0 over stdio
    "initialize"    → 返回协议版本与能力
    "tools/list"    → [{"name","description","inputSchema"}]   # 自注册工具表
    "tools/call"    → 调用 get_data()/zb.get()/bk.get()
    "resources/list" / "resources/templates/list"
```
对接方式：Claude Desktop / Cursor 的 `mcpServers` 里配 `"command": "python", "args": ["-u", "stock_mcp_server.py"]` 即可。

---

## 四、对我们项目的可借鉴项（按价值排序）

| # | 借鉴项 | 我们现状（实测） | 落地方式 | 价值 |
|---|---|---|---|---|
| 1 | **概念/行业板块映射表（双向）** | ⚠️ **我们自己代码里承认缺**：`market_phase.py:15/400` 注释「我方无历史概念映射表」，主线只能靠同花顺涨停池 `reason` 聚合；`maintain.py:432`「概念口径待东财开放」。题材雷达目前只有 westock 行业板块四维评分 | 建 `concept_map(code, board_code, board_name, category, source)` 表 + 双向索引；先落申万一/二级 + 东财概念，补齐三级 | **高**（直接补自认缺口：涨停归因、题材雷达加概念维度、「某概念内选股」） |
| 2 | **MCP 接口（AI 直连本地数据）** | ❌ 完全没有（全仓 grep `mcp` 无命中）；只有 140 个 HTTP 路由 | `server/mcp_server.py`：JSON-RPC 2.0 over stdio（或 FastAPI 加 SSE 端点），暴露 5 个 tool：`get_kline` / `screen`（跑 29 策略）/ `get_indicators` / `get_limitup` / `get_news` | **高**（AI 时代入口：让 Claude/Cursor 直接查我们的行情与选股结果） |
| 3 | **本地复权因子 + 按需折算** | 直接取上游 `qfqday/qfqweek/qfqmonth`（`datasource.py:246-248`），分时按不复权（`intraday.py:26`）；**无本地因子、无法切 hfq/不复权** | 抄 §3.2 算法（bisect + ratio），落 `fq_factor` 表；查询时折算 | 中（当前上游 qfq 够用；要做「后复权回测/因子校验」再引） |
| 4 | **5 种加权自定义指数** | 无自定义指数能力 | 抄 `METHOD_NAMES` 与 `base=1000.0`；做「自选股等权指数」「概念板块等权指数」 | **中高**（低成本功能亮点：复盘看「我的组合 vs 大盘」） |
| 5 | **同步清单 + SHA-256 + 断点续传** | 有 `bar_sync` 增量同步（eltdx 批量源），无校验清单/断点 | 大批量同步加 manifest 校验与 `.part` 续传 | 中（失败重跑成本） |
| 6 | **cross 金叉独立输出** | `indicator.py` 有 signals 汇总（多空），非 per-指标金叉 | `zb.get(cross="with_value")` 模式：值与金叉同时返回 | 中低 |
| 7 | **本地周期合成（日→周/月）** | 直接取上游 `qfqweek/qfqmonth` | 参考 `_merge_to_period` / `_merge_minutes_to_period` | 低（上游已有，仅离线场景有意义） |
| 8 | **列式数组 + 批量计算核心** | `indicator.py`/`mytt.py` 逐股 numpy；实测全市场扫描秒级 | 除非上分钟级全市场，否则不必改 | 低（当前够快） |

---

## 五、不建议照抄的部分

| 项 | 原因 |
|---|---|
| **C++ / LevelDB 存储引擎** | 我们 SQLite（270 万行）+ 可选 MySQL 跑得动，Zstd 压缩的磁盘收益远小于迁移与维护成本 |
| **Excel / WPS 宏调用** | 用户群体不同（我们是 Web 面板，不是桌面表格工具） |
| **它的 39 指标** | 我们已有 **61 项**（实测 `all_indicators()` 6+9+15+10+7+8+6），指标数量我们领先，不必回抄 |
| **把它当数据源接入** | 它是本地二进制服务（7899 端口 + 自有协议），集成成本 > 收益；我们 westock/eltdx 双源已够 |
| **tick 级数据** | 我们是日/分钟级选股面板，tick 不在需求范围 |

---

## 六、结论

free-stockdb 是**数据底座**，我们是**应用层**，两者互补。它真正比我们强的不是指标数量，而是**数据工程的两块短板**——板块映射表与 AI 调用入口，外加几个纯计算能力（复权、自定义指数）。

**建议优先级**：
1. 🔥 **概念板块映射表** —— 补我们自己注释里承认的缺口，题材雷达/涨停归因/概念内选股三处同时受益
2. 🔥 **MCP 接口** —— 让 AI 工具直接调用我们的 29 策略与行情，是「AI 时代选股面板」的关键入口
3. ⚡ **自选股等权指数** —— 纯计算、低成本，复盘场景的加分功能
4. ⏳ 复权因子、同步校验、金叉输出 —— 等实际需要再引，不急

---

*本报告为功能对照与方案评估，落地需自行实现，不复制其源码。*
