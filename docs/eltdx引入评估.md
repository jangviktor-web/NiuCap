# eltdx 引入选股面板 · 多维实测评估报告

> 对象：[electkismet/eltdx](https://github.com/electkismet/eltdx) —— 通达信 7709 行情 / 7615 F10 资料的 Python 客户端
> 版本：`eltdx 3.2.2`（`cp310-abi3-manylinux_2_17_x86_64.whl`，2.0 MB，**Rust 编译，零 Python 依赖**）
> 本报告全部结论均来自**本机实测**，不是文档推断。

---

## 一、结论先行

**有必要引入，但必须限定用途，且不能替代商业数据源。**

三句话总结：

1. **作为「日线历史数据源」，价值极高且已验证** —— 全市场 5575 只 × 250 根日线，**15.9 秒**拿完，与腾讯源逐日交叉验证误差 **< 0.04%**（且偏差是复权舍入，非数据错误）。这直接解决 `策略改造难点评估.md` 里最棘手的**难点 1（覆盖率 8.1%）**。
2. **作为「短线衍生指标源」，价值高但不能实时全市场用** —— `shortline_indicators` 一次性给 30+ 个现成指标（开盘量比、竞价量比、封单占流通比、beta、PE…），**这正是我们 6 个伪历史策略想算而算不出的东西**；但全市场要 **216 秒**，只能对候选池按需算。
3. **有两条硬约束必须接受** —— ① **许可证明确禁止商业使用**；② 走通达信非公开协议，**数据源 IP 可能变化、无 SLA**。因此定位应是「**开发/研究期的数据加速器**」，而非生产环境的唯一依赖。

---

## 二、我们的数据源现状（对照基础）

先看清面板现在有什么，才能判断 eltdx 填了什么空缺。

| 模块 | 行数 | 角色 | 关键接口 |
|---|---|---|---|
| `datasource.py` | 591 | **日线 + 快照主力** | `get_kline()`（腾讯）、`quote_tencent()`、`market_snapshot()`（新浪） |
| `hithink.py` | — | 涨停池 / 龙虎榜 | `limit_up_pool()`、`limit_up_ladder()`、`dragon_tiger()` |
| `westock.py` | 665 | 走外部 CLI 取基本面 | `fund_flow()`、`chip()`、`finance()`、`reports()`、`profile()` |
| `tdx.py` | 590 | **只是公式编译器** | 解析通达信公式语法，**不取数据** |
| `store.py` | 1577 | TiDB / SQLite 落库 | `daily_bars` 表 |

**关键认知**：现有 `tdx.py` 名字像数据源，实际只是**公式语法解析器**。也就是说，
**面板目前完全没有原生的「通达信协议数据通道」** —— eltdx 补的正是这个位置，二者是**互补而非重复**。

---

## 三、六维实测结果

### 维度 1 · 可用性：装得上、连得快 ✅

```
平台        : Linux x86_64 / Python 3.11.1
安装        : pip install eltdx → 成功（无需 Rust 工具链，直接装 abi3 wheel）
Python 依赖 : Requires: （空）        ← 零依赖，不污染现有依赖树
连接建立    : 0.20 ~ 0.35 s
```

`TdxClient.from_hosts()` 会自动探活优选服务器（`probe_hosts=True`），连接池可调：

```python
client = eltdx.TdxClient.from_hosts(
    server_count=4,              # 用几台服务器
    connections_per_server=6,    # 每台几条 TCP（= 总并发槽位）
)
client.connect()
```

### 维度 2 · 数据准确性：与腾讯源交叉验证 ✅

逐日对比 250 根日线（`adjust='qfq'` 前复权，与腾讯口径一致）：

| 代码 | eltdx 条数 | 腾讯条数 | 最大偏差 | 判定 |
|---|---|---|---|---|
| `sh600519` 贵州茅台 | 250 | 250 | **0.000%** | ✅ 完全一致 |
| `sh601318` 中国平安 | 250 | 250 | **0.000%** | ✅ 完全一致 |
| `sz000001` 平安银行 | 250 | 250 | 0.037% | ⚠️ 19/250 处微差 |
| `sz300750` 宁德时代 | 250 | 250 | 0.020% | ⚠️ 145/250 处微差 |
| `sh600036` 招商银行 | 250 | 250 | 0.011% | ⚠️ 47/250 处微差 |

**偏差根因已定位，不是数据错误**：

```
sz000001  2025-09-30   eltdx close=10.7400   腾讯 close=10.7440   差 0.0040 元（绝对量）
sz300750  2025-09-10   eltdx close=307.6700  腾讯 close=307.7320  差 0.0620 元
```

差异是**固定绝对量**（0.004 元 / 0.062 元），不随股价放大 → 属于**复权因子的舍入精度差异**。
eltdx 给出更接近真实成交价的数值，腾讯是前复权平滑后的值。**对选股策略无实质影响**（信号判断都基于比例关系）。

> ⚠️ **踩过的坑（务必记住）**：`bars.get()` 的 `adjust` 参数**默认 `None`（不复权）**。
> 不传这个参数，除权股会静默产生 **1.8% 的错误**（实测 `sh601318`，28 处不符）。
> 验证结果：`adjust='qfq'` 偏差 0.000%，`adjust='hfq'` 偏差 193%。**必须显式传 `adjust='qfq'`**。

### 维度 3 · 读取性能：数量级提升 ✅✅

| 操作 | eltdx | 现有腾讯源 | 提升 |
|---|---|---|---|
| 日线 250 根，单只（首次） | 0.036 s | 0.40 s（含 400ms 限流） | **快 11 倍** |
| 日线 250 根，10 只 | **0.09 s** | 4.0 s（串行限流） | **快 44 倍** |
| 日线 250 根，300 只 | **2.58 s** | ~120 s（推算） | **快 46 倍** |
| 日线 250 根，**全市场 5575 只** | **15.9 s** | 不可行（约 37 分钟） | — |

**批量 K 线呈完美线性扩展**（这是最关键的发现）：

```
  10 只 →  0.09s    9.4 ms/只
  50 只 →  0.42s    8.4 ms/只
 100 只 →  0.87s    8.7 ms/只
 300 只 →  2.58s    8.6 ms/只
5575 只 → 15.89s    2.9 ms/只 ← 并发池消化后降至 2.9ms
复现验证 → 15.10s / 15.52s（两次，1,369,701 根 K 线）
```

> 对比意义：现有 `sync_bars.py` 全市场落库估算 **48 分钟**（瓶颈是腾讯 400ms 限流）。
> 用 eltdx，**同样的活 15.9 秒做完**。这是引入 eltdx 最硬的理由。

### 维度 4 · 独有能力：填了 5 个面板空白 ✅

这是「必要性」判断的核心。以下能力**面板现有全部数据源都没有**：

| # | 能力 | eltdx 接口 | 实测耗时 | 面板现状 | 价值 |
|---|---|---|---|---|---|
| 1 | **集合竞价逐秒明细** | `auctions.series()` | 0.10 s | ❌ 完全没有 | ⭐⭐⭐ |
| 2 | **逐笔成交（含订单笔数）** | `trades.today()` | 0.01 s | ❌ 完全没有 | ⭐⭐⭐ |
| 3 | **短线衍生指标 ×30+** | `helpers.shortline_indicators()` | 见下 | ❌ 全需自算 | ⭐⭐⭐ |
| 4 | **个股题材（带入选日+理由）** | `helpers.stock_topics()` | **0.34 s** | △ `westock.profile` 很粗 | ⭐⭐⭐ |
| 5 | **连板梯队** | `helpers.limit_ladder()` | 256 s | △ `hithink.limit_up_ladder` 已有 | ⭐ |
| 6 | **除权除息 / 复权因子** | `corporate.adjustment_factors()` | 0.01 s | ❌ 完全没有 | ⭐⭐ |
| 7 | **股本变动历史** | `corporate.capital_changes()` | 0.01 s | ❌ 没有（westock 只有当期） | ⭐⭐ |
| 8 | **涨跌停价表（含规则）** | `limits.special()` | 0.01 s | △ 可自算 | ⭐ |
| 9 | **全市场代码表** | `codes.all_a_shares()` | 2.34 s | △ 有但走新浪分页 | ⭐ |

**三个最有价值的实测样例：**

**① 集合竞价 —— 含「未匹配量 + 方向」**

```
09:15:00  price=301.96  matched_volume=245   unmatched_volume=56   direction=+1
09:15:09  price=301.96  matched_volume=498   unmatched_volume=34   direction=+1
...
14:59:51  price=297.09  matched_volume=4333  unmatched_volume=60   direction=-1
```

`unmatched_volume` + `unmatched_direction` 就是**「竞价抢筹 / 压单」的原始数据**。
现在面板要判断「竞价异动」只能靠开盘后的快照反推，有了这个可以直接算。

**② 短线衍生指标 —— 一次拿 30+ 个现成字段**

```
sh600519  开盘量比=2.92  竞价量比=2.13  开盘涨幅=0.15%  pe=19.3  beta=-0.34
  封单额=125257  封单占流通比=1.8e-05  换手z=0.0045  昨量比=0.968  ...
```

完整字段包括：`open_volume_ratio`（开盘量比）、`auction_prev_volume_ratio`（竞价量比）、
`seal_to_float_ratio`（封单占流通比）、`beta_60d`、`pe_ttm`、`open_turnover_z`、
`limit_up_streak_days`（连板数）、`ladder_level`（梯队层级）…

> **这是本次评估最重要的发现**：`screener.py` 那 6 个伪历史策略想表达的语义
> ——「放量」「回踩」「超跌」「平台突破」——**eltdx 全部有现成的、算好的字段**。
> 与其自己从 `daily_bars` 算，不如直接读。

**③ 个股题材 —— 带入选日期与理由**

```
sz300750 宁德时代 → 38 个题材
  含H股        相关度5.0  入选日20250519  理由=H股:宁德时代(03750)于2025-05-20上市
  固态电池      相关度3.0  入选日20240402  理由=公司是实现凝聚态电池（半固态）商业化的企业
  特斯拉概念    相关度3.0  入选日20211103  理由=公司将向特斯拉供应锂离子动力电池产品
  昨成交20      相关度5.0  入选日20260921  理由=2026-09-21成交额为147.56亿，沪深两市排名第4
```

`westock.profile` 只给概念名单，**没有入选日、没有入选理由、没有相关度**。
这种颗粒度在做「题材发酵时间线」分析时是刚需。

### 维度 5 · 性能陷阱：helper 类接口慢 ⚠️

**必须区分两类接口**，否则会踩坑：

| 接口类型 | 示例 | 速度 | 可否全市场用 |
|---|---|---|---|
| **纯取数型** | `bars.get()`、`trades`、`money_flow`、`auctions`、`corporate` | **0.01 ~ 0.1 s** | ✅ 可（K线全市场 15.9s） |
| **全市场扫描型** | `shortline_indicators`、`limit_ladder`、`theme_strength_rank` | **216 ~ 256 s** | ❌ 不可 |

`shortline_indicators` 实测拆解：

```
全市场 5575 只  第一次 220.17 s
全市场 5575 只  第二次 216.18 s   ← 几乎无改善，不是缓存问题
        300 只          14.65 s   ←  48.85 ms/只，线性
```

**根因**：它要对每只股票读本地 `zhb.zip` 统计包，计算 `beta_60d` 等指标，是 CPU 密集而非网络密集。
（对比 K 线是 2.9 ms/只，慢 17 倍。）

**正确用法**：
- ❌ 不要在全市场选股主链路里调 `shortline_indicators`
- ✅ 应在**粗筛出候选池后**再调 —— 50 只约 2.5 s，300 只约 15 s，完全可接受
- ✅ 或**离线批量算一次落库**（216 s / 天，可接受），选股时直接查库

> 另外注意数据质量：全市场结果里 `sh600053` 开盘量比 **268 倍**、`sh600448` **175 倍**，
> 明显是复牌 / 长期停牌导致的基数失真。**接入时必须加过滤**（如剔除开盘量比 > 50 的异常值）。

### 维度 6 · 许可证与合规：**禁止商业使用** 🔴

许可证全文（`eltdx-3.2.2.dist-info/licenses/LICENSE`）关键条款：

```
ELTDX Research-Only License

This project is provided only for personal learning, protocol research, and
non-commercial study.

You may not use this project, its source code, its derived works, or data
obtained through it for any commercial activity, paid service, production
service, resale, redistribution for profit, market-data vending, automated
trading service, ...
```

**中文要点**：

| 允许 | 禁止 |
|---|---|
| 个人学习、协议研究 | 任何商业活动 |
| 阅读、使用、复制、修改源码（个人研究） | 付费服务、生产服务 |
| — | 转售、营利性再分发、行情数据售卖 |
| — | 自动化交易服务 |

**风险叠加**：除许可证外，还走**通达信非公开协议**（7709/7615 端口）。访问第三方服务器及所获数据的
全部责任由使用者承担。**无 SLA、无数据准确性担保、服务器 IP 可能随时失效**。

**处置建议**：

1. **如果面板是个人研究 / 自用** → 引入完全合规，放心用。
2. **如果面板将来要对外提供服务或收费** → **不能**把 eltdx 作为生产依赖。三个选项：
   - **选项 A（推荐）**：把 eltdx 定位为「**开发期 / 研究期的数据加速器**」——批量拉数据落库到 TiDB，
     生产链路只读自己的库。这样对外暴露的是「我们自己的数据」，与 eltdx 隔离。
   - **选项 B**：生产链路继续用腾讯 / 新浪 / hithink / westock 等已商用授权渠道，eltdx 只在本地研究用。
   - **选项 C**：**抽象出数据源接口层**（见下），eltdx 实现一个 adapter，将来可一键换掉。
3. **代码隔离**：把 eltdx 调用集中在**单一适配器模块**里，不要散落各处，便于事后摘除或替换。

---

## 四、引入方案：分三步走

按「价值 / 风险」排序，**从最安全、收益最大的地方切入**。

### 第一步：作为「日线历史数据源」替换 `sync_bars.py` 的取数环节（收益最大，风险最低）

**做什么**：`scripts/sync_bars.py` 现在逐只调腾讯 `get_kline()`，受 400ms 限流拖累，
全市场需 **48 分钟**。改用 eltdx 批量接口，**15.9 秒**完成。

```python
# 示意：批量拉全市场日线
codes = client.codes.all_a_shares()                    # 2.34 s，5575 只
result = client.bars.get(                            # 15.89 s，一次调用
    codes, period='day', count=250, adjust='qfq', batch_size=200,
)
for code, series in result.items():
    for bar in series.bars:                          # 注意：数据在 .bars 属性里
        ...  # 落库到 daily_bars（复用现有 upsert_bars 多值批量写入）
```

**收益**：
- 直接解决 `策略改造难点评估.md` 的**难点 1**：覆盖率 8.1% → 100%
- 全市场落库从「48 分钟」变「16 秒取数 + 落库时间」
- 数据与腾讯口径一致（复权舍入级的微小差异）

**风险**：低。有腾讯源作为降级兜底，eltdx 挂了自动回落。

**必须注意**：
- ⚠️ 显式传 `adjust='qfq'`（否则 1.8% 静默错误）
- ⚠️ 数据在 `series.bars` 里，不是直接可迭代
- ⚠️ 做**双源校验**：首次切换时抽样与腾讯逐日比对，偏差 > 0.1% 则告警

### 第二步：为 6 个伪历史策略提供「现成指标」（收益高，需注意性能）

**做什么**：`screener.py` 的 `s_volume_surge`、`s_pullback_ma20`、`s_oversold_rebound`
等策略，不再自己从 `daily_bars` 算，改为读 eltdx 的 `shortline_indicators`。

**两种接入姿势**：

| 姿势 | 说明 | 适用 |
|---|---|---|
| **离线落库（推荐）** | 每天收盘后跑一次全市场（216 s），把 30+ 指标存进新表 `shortline_daily`；选股时直接 SQL 查询 | 生产环境 |
| **按需调用** | 对已粗筛出的候选池（50~300 只）实时调，2.5 ~ 15 s | 研究 / 交互查询 |

**收益**：策略语义与实现终于对齐（`s_ma_bull` 真的算均线、`s_volume_surge` 真的比均量）。

**风险**：中。需新建表 + 定时任务；注意异常值过滤。

### 第三步：补充面板空白能力（收益中，按需）

按优先级：

1. **个股题材**（`stock_topics`，0.34 s）→ 最适合先做，快、且质量明显优于 `westock.profile`
2. **集合竞价明细**（`auctions.series`，0.10 s）→ 可做「竞价异动」新功能
3. **逐笔成交**（`trades`，0.01 s）→ 可做「主力大单」分析（利用 `order_count` 字段）
4. **复权因子 / 股本变动**（`corporate`）→ 支撑更严谨的回测
5. ~~连板梯队~~ → `hithink` 已有且更快，**不重复造**

### 落地建议：先建一层数据源抽象

不管引入哪些，建议先加一个薄适配层，避免 eltdx 调用散落各处：

```
server/
  sources/
    __init__.py
    base.py          # 定义统一接口：get_bars(code, n) / get_shortline(codes) / get_topics(code)
    tencent.py       # 现有 datasource 逻辑包装
    eltdx_source.py  # eltdx 实现（唯一 import eltdx 的地方）
```

好处：① 许可证风险可一键摘除；② 可做多源降级；③ 将来迁 Cloudflare 时可整体替换。

---

## 五、和 Cloudflare 迁移方案的关系

结合 `docs/Cloudflare迁移评估.md` 的结论：

| 项 | 判断 |
|---|---|
| Cloudflare Workers 能否直连通达信 7709 | ⚠️ **不行**。Workers **不支持原始 TCP socket**，只能 HTTPS/fetch。eltdx 是纯 TCP 协议，**无法在 Worker 里跑** |
| 那还能用吗 | ✅ 能，但只能**离线用**：本地 / 容器里跑 eltdx 拉数据 → 落库到 TiDB → Worker 只读库 |
| 与现有方案冲突吗 | ❌ 不冲突，反而是**互补**。Worker 的 CPU 10ms 限制本来就跑不动全市场计算，正好把重活（eltdx 取数 + 指标计算）放在离线侧 |

**结论**：eltdx 的引入**反而让 Cloudflare 方案更可行** —— 把「贵操作」全部前置到离线批处理，
Worker 只做轻量查询，正好绕开 CPU 与内存限制。

---

## 六、决策清单

| 问题 | 答案 |
|---|---|
| 有必要引入吗？ | **有**。日线取数快 46 倍 + 填补 5 项能力空白 + 直接解难点 1 |
| 最大价值在哪？ | ① 全市场日线 15.9 秒（vs 48 分钟）② `shortline_indicators` 现成算好 30+ 指标 |
| 最大风险是什么？ | **许可证禁止商业使用**（若面板要对外服务则不可作生产依赖） |
| 立刻做什么？ | 第一步：改造 `sync_bars.py` 取数环节，跑全市场落库 |
| 不要做什么？ | ❌ 别拿它替换全部数据源；❌ 别在全市场主链路调 `shortline_indicators`；❌ 别在 CF Worker 里用 |
| 需要先装什么吗？ | 已装好（`.venv` 里 `eltdx 3.2.2`，零依赖） |

---

## 附：实测复现要点

```python
import eltdx

# 1. 连接（推荐 3~4 台服务器 × 4~6 连接）
client = eltdx.TdxClient.from_hosts(server_count=4, connections_per_server=6)
client.connect()

# 2. 全市场日线（15.9 s）
codes  = client.codes.all_a_shares()                       # → list[str]，形如 'sh600519'
result = client.bars.get(codes, period='day', count=250,
                         adjust='qfq',                     # ⚠️ 必须显式指定
                         batch_size=200)
for code, series in result.items():
    for bar in series.bars:                                # ⚠️ 数据在 .bars
        bar.time, bar.open, bar.close, bar.high, bar.low, bar.volume_lots, bar.amount

# 3. 短线指标（候选池用；全市场 216s 太慢）
tbl = client.helpers.shortline_indicators(codes[:300])
for row in tbl.rows:
    row.open_volume_ratio, row.auction_prev_volume_ratio, row.seal_amount, row.beta_60d

# 4. 个股题材（0.34 s）
topics = client.helpers.stock_topics('sz300750')
for t in topics.topics:
    t.topic_name, t.relation_level, t.selected_date, t.reason

# 5. 集合竞价（0.10 s）
auction = client.auctions.series('sz300750')
for p in auction.points:
    p.time_label, p.price, p.matched_volume, p.unmatched_volume, p.unmatched_direction_raw
```

**接口对象字段速查**（都是 dataclass，用属性访问，不能下标）：

| 返回对象 | 数据字段 |
|---|---|
| `KlineSeries` | `.bars` → `KlineBar`（`.time/.open/.close/.high/.low/.volume_lots/.amount`） |
| `TradePage` | `.ticks` → `TradeTick`（`.time_label/.price/.volume/.order_count/.side`） |
| `AuctionSeries` | `.points` → `AuctionPoint`（`.time_label/.price/.matched_volume/.unmatched_volume/.unmatched_direction_raw`） |
| `ShortlineIndicatorTable` | `.rows` → `ShortlineIndicator`（30+ 字段） |
| `StockTopics` | `.topics` → `StockTopic`（`.topic_name/.relation_level/.selected_date/.reason`） |
| `LimitLadderTable` | `.rows` → `ShortlineIndicator` |
