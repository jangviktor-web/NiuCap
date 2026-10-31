# TradingView-API 深度对比与可移植性分析

> 分析对象：`github.com/Mathieu2301/TradingView-API`，v4.1.0（2026-10 抓取）
> 对比对象：本工程 `tick-stock-panel`（牛来选股面板）
> 抓取方式：codeload zip 归档 + 全量源码阅读（非仅 README）

---

## 一、对方到底是什么

一句话：**它是一个直连 TradingView 私有 WebSocket 协议的 Node/TypeScript 数据客户端库**，本身不画图、不做 UI，纯粹解决「怎么从 TradingView 拿到数据和指标结果」。

| 维度 | 事实 |
| --- | --- |
| 语言/运行时 | TypeScript，ESM，Node ≥ 20，**运行时依赖只有 `ws` 一个** |
| 规模 | 10,999 行 TS（含测试），约 120 个文件 |
| 协议 | TradingView 私有 WS：帧格式 `~m~<长度>~m~<载荷>`，**长度按 UTF-16 code unit 计**；`~h~<id>` 心跳必须原样回显 |
| 三层 API | ① `data` 高层（自动管连接/会话/超时/清理）② `backtest` 诚实回测协议 ③ low-level（chart / study / quote session 全手动） |
| 数据范围 | 全球股票、加密、外汇、期货；K 线、实时报价、**远程执行 TradingView 内置指标与 Pine 脚本**、选股器、账户自选股、技术评级、符号搜索 |
| 非标准周期 | Heikin Ashi、Renko、Kagi、LineBreak、PointAndFigure、Range，且由服务端计算 |
| 深历史 | `MAX_CHUNK = 5000`，靠 `series_completed` 事件驱动续拉 `fetchMore()`，直到满足或服务器无更多数据 |
| 代理 | 自建 HTTP CONNECT 隧道（Node `http.Agent` 子类 + `tls.connect`），Bun 走原生 `proxy` 选项 |
| 工程证据 | vitest 单元 + **22 个 live 测试** + V3/V4 K 线一致性 parity 探针 + 2 小时 endurance（12 次连接轮换、1721 次 K 线回调、719 次心跳、**零错误**） |

### 它最值得注意的三个设计

**1. 「诚实回测」协议（`src/backtest/`）**
下载一次 K 线 → 冻结成快照（丢弃未完成 bar、记录 gaps、算 SHA-256）→ **先跑因果性检查再模拟** → 悲观成交 → 样本内/外分割 → 四种退出模型对比 → **1000 次随机入场基线** → 输出 JSON 报告 + 警告。文档原话：「它不声称盈利，负面的或不确定的结果是本协议的有效结果，而且是最常见的那种。」

**2. 随机入场基线（`stats.ts`）**
mulberry32 种子化 PRNG + FNV-1a 派生子种子；在与策略**完全相同的合格 bar** 上均匀抽样同等笔数的入场，方向按策略的多空占比抽；同一退出、同一成本、同一「同时只持一仓」规则；跑 1000 次后给出 `percentileRank = 100 × count(随机收益 < 策略收益) / n`。抽不到的笔数会补抽并在报告里警告 `baseline_unmatched`。

**3. 因果性自动检查（`backtest.md` / `run.ts`）**
模拟之前，把用户的信号函数在**截断历史**上重跑：截至每个信号所在 bar 各跑一次（默认上限 100 个，超出按种子抽样），外加 8 个等距切点。信号必须与不截断时完全一致——
- 截断后才出现的信号 = `look_ahead`（用了未来）
- 截断后有、后来消失的 = `repaint`（重绘）

任一违反都以 `LOOK_AHEAD` 错误中止并列出每处的 `cut/bar/direction/kind`。文档明确说：「修规则，而不是关掉检查。」

---

## 二、我们的底子（已逐项核实）

| 维度 | 事实 |
| --- | --- |
| 规模 | 40,291 行 Python / 约 50 个模块；FastAPI + SQLite(`tick.db`) + echarts 单页前端（18 个 tab） |
| 数据源 | **eltdx 直连通达信 7709 协议**（批量 5575 只 × 250 根日线 ≈ 15 秒；腾讯逐只 ≈ 48 分钟）+ 腾讯 + 新浪 + 同花顺 fuyao，四级自动降级；带限速「发号制」和分级 TTL 缓存（K线 300s / 行情 20s / 分钟线 15s） |
| 实时性 | **纯 HTTP 轮询**（5s / 30s），前端 `WebSocket` 出现次数 = 0 |
| 因子实验室 | 横截面 Spearman IC、ICIR、t 值、正占比、五档分层单调性、regime 标签、`DEDUP_STEP=20` 去重叠 |
| 前视防护 | `lag=1`（t 日出信号、t+1 收盘成交）+ 自检「篡改未来数据不应回改历史因子值」+ 「把未来收益当因子，IC 必须 ≈ 1」 |
| 回测成本 | 佣金万 2.5 + 印花税千 1 + 滑点万 5（**比 TV 的通用 0.1%/0.05% 更贴 A 股**） |
| A 股特有 | **T+1**（`sellable_from = i+1`）、**次日开盘涨停买不进**（按板块涨停比例判定）、FIFO 批次归因、涨跌停价 |
| 走步验证 | `walkforward.py`：3 折、训练段占 0.7 |

---

## 三、差距在哪里（诚实版）

### 3.1 实质性架构差距

**实时推送 vs 轮询。** 对方是 WS 长连接 + 心跳 + 单会话多订阅复用（`QuoteSession` 用 `=JSON.stringify({session,symbol})` 做 key，同一 symbol 的多个订阅共享一份数据，后到的订阅还能从已加载的订阅 `seed()` 拿到初值）；我们是 5 秒轮询。后果：一根 1 分钟 K 线期间要重取 12 次，无法感知 tick 级变化，也无法在休市/复市时即时反应。

**非标准 K 线周期。** 对方有 6 种，且在服务端算好直接给；我们只有标准 OHLC。

**指标生态。** 对方能在 TradingView 服务器上跑它的内置指标库和用户的 Pine 脚本，直接拿 study 序列；我们的指标全部自己实现（`mytt.py` 等）。这是「生态」差距，不是算法差距。

### 3.2 方法论差距（这部分更值得关注）

| 项目 | TradingView-API | 我们 | 判断 |
| --- | --- | --- | --- |
| 随机入场基线 | 1000 次种子化随机，同退出同成本，输出百分位 | **无** | **真实缺口** |
| 因果性自动检查 | 截断历史重放信号函数，自动判 look_ahead/repaint | 只有静态自检，不对真实信号函数做重放 | **真实缺口** |
| 数据快照可复现 | 冻结 + 丢弃未完成 bar + 记录 gaps + SHA-256 | 有缓存，无校验与复现保证 | **真实缺口** |
| 悲观成交假设 | bar 内同触止损/止盈假定先止损；跳空穿过按开盘价成交（`stop_gap`） | 用次日开盘/收盘价，回避了 bar 内歧义 | 各有取舍（见下） |
| 成本模型 | 通用 0.1% 费 + 0.05% 滑点 | 万2.5+千1印花税+万5滑点 | **我们更准** |
| 交易制度 | 无 | T+1、涨停买不进、FIFO 批次 | **我们更准** |
| 测试证据 | 单元 + 22 live + parity + endurance，文档区分「证明了什么/没证明什么」 | 约 60 个 check/e2e/smoke 脚本 + 各模块 selfcheck | 相当，但对方把「证据」本身当交付物 |

> **悲观成交这条要特别说明**：我们用次日开盘价成交，本质上比「假定先止损」更保守也更干净——它根本不制造 bar 内信息。真正的差距不在假设，而在**遇到「当日触及止损」这类策略时我们会退化**（拿不到 bar 内高低点序列）。所以这条是「补能力」而非「改假设」。

### 3.3 一个必须说清的硬事实：连通性

实测（本环境，2026-10-25）：

| 端点 | IPv4 可达性 | 延迟 |
| --- | --- | --- |
| `qt.gtimg.cn`（腾讯行情） | ✅ HTTP 200 | 40 ms |
| `push2his.eastmoney.com`（东方财富） | ✅ 可达 | 87 ms |
| `scanner.tradingview.com` | ❌ 超时（DNS 仅返回 IPv6，无 IPv4 出口） | — |
| `data.tradingview.com` | ❌ 连接失败 | — |
| `www.tradingview.com` | ❌ 超时 | — |

**结论：把 TradingView 当作我们面板的数据通道，在 A 股场景既不可行也无必要。** 不可行是因为其端点在大陆网络长期不稳定（本环境直接不通）；无必要是因为 TradingView 的 A 股数据本身是二手转发，而我们有 eltdx 直连交易所级协议（还带腾讯源缺失的 `amount` 成交额字段），数据质量更高。

**但下面所有「可移植技术」都与 TradingView 连通性无关，可以纯本地实现。**

---

## 四、可移植清单（第一轮结论，已被 §六 修正重排）

### A 级：纯本地、零外部依赖、直接提升严谨度

**A1. 随机入场基线 + 百分位排名** ⭐ 最推荐
- 移植内容：mulberry32 PRNG、FNV-1a 派生子种子、合格 bar 均匀抽样、多空占比定向、同退出同成本同单仓规则、`percentileRank`、`baseline_unmatched` 警告
- 接入点：`factor_lab.py`（因子页）和 `strategy_eval.py` / `equity_sim.py`（策略评估）
- 价值：把「IC = 0.08，t = 2.1」变成「跑赢 96% 的随机入场」。这是我们目前**完全缺失**的最有力一句话证据
- 工作量：约 120 行 + 两个接入点 + selfcheck（用合成数据验证：随机基线的均值应接近 0，种子相同结果应可复现）

**A2. 因果性自动检查（look-ahead / repaint 检测）**
- 移植内容：对信号/因子计算函数做「截断历史重放」，最多 100 个信号 + 8 个等距切点，比对一致性，违反即报错并列出位置
- 接入点：`factor_lab.build_factors` 的每个因子函数、策略信号函数
- 价值：把前视防护从「写自检证明框架有效」升级为「对每个真实因子自动执行」。我们已经有 `lag=1` 和静态自检，缺的是自动化
- 工作量：约 90 行通用框架 + 因子函数签名统一

**A3. 数据快照冻结 + SHA-256**
- 移植内容：`freezeSnapshot` —— 校验 OHLC 一致性、严格递增、丢弃未完成 bar、推断 bar 秒数、检测 gaps、算 SHA-256、深冻结；`loadSnapshot` 校验 checksum 后可精确复现
- 接入点：`factor_lab.evaluate_cached`
- 价值：因子/回测结果可复现、可追溯；「上次算的 IC 和这次不一样」有据可查
- 工作量：约 100 行

**A4. 悲观成交补能力（bar 内高低点）**
- 移植内容：`reachedStop/reachedTarget` + `gapExit` + `intrabarExit`（同触时先止损）+ 按收盘价盯市摊销回撤
- 接入点：`backtest.py`
- 注意：必须叠加我们已有的「涨停买不进」「T+1」，且 A 股涨跌停会改变「是否触得到」的判断，不能照搬
- 工作量：约 80 行 + 与现有次日开盘口径的兼容层

### B 级：中等工作量，体验/能力提升

**B1. 非标准 K 线周期（Heikin Ashi / Renko / Range）**
纯本地计算，不需要 TradingView。接到分钟线/日线卡片，与现有 `stockMinute` 通道切换机制复用。

**B2. WebSocket 推送替代轮询**
不必用 TradingView——腾讯 `push2` 系列本身支持推送，或自建 server 端推流。改造点：server 加 WS 端点，前端把 `setInterval(loadX, 5000)` 换成订阅。**注意**：刚做完的主题切换依赖 `chartCache`，改造时不能破坏它。

**B3. 传输抽象 + 代理支持**
把 `datasource.py` 里硬编码的多源降级收敛成统一的 `Transport` 接口（`isOpen/send/close/terminport`），加 HTTP CONNECT 隧道支持。为我们自己的数据源加代理能力，也方便未来接任意源。

### C 级：不建议 / 有条件才做

| 项目 | 理由 |
| --- | --- |
| 远程 Pine / 内置指标执行 | 需要 TradingView 连通性 + 账号 cookie 凭证 + 服务条款风险；且与 eltdx 直连质量重叠 |
| `scanner.tradingview.com` 选股 | 同上；我们的 `screen_dsl` + `screener` 已是 A 股定制（涨跌停/龙虎榜/板块/情绪） |
| 复刻其 WS 协议层 | 只有决定接 TradingView 时才需要。但「多格式容错解压（ZIP/gzip/deflate/br 依次尝试）+ lenient 帧解析」的思路可以搬到 eltdx/腾讯源的脏数据处理上 |

---

## 五、第二轮深挖：协议客户端的工程细节

第一轮看完「它有什么」，第二轮专挑「它怎么做到的」——这些是更能直接改进我们的部分。

### 5.1 错误分类学（`src/errors.ts`，22 个机器可读错误码）

不是抛字符串 message，而是**每个错误都带 `code` + `details`（服务器原文）+ `cause`**：

```
PROTOCOL_ERROR / CONNECTION_ERROR / DISCONNECTED / AUTH_ERROR / SYMBOL_ERROR /
SERIES_ERROR / CRITICAL_ERROR / STUDY_ERROR / QUOTE_ERROR / PARSE_ERROR /
INVALID_STATE / INVALID_ARGUMENT / HTTP_ERROR / NOT_FOUND / NO_DATA /
TIMEOUT / ABORTED / CALLBACK_ERROR / LOOK_AHEAD
```

且提供 `toTradingViewError(error, fallbackCode)` 把任何抛出值归一化——包括把 `AbortError` 自动转成 `ABORTED`。

> **我们的现状**：`app.py` 有 138 处 `HTTPException`，全是字符串 detail，调用方只能靠正则匹配文字。因子实验室/回测/数据源降级这些地方，前端拿到的失败原因无法程序化区分「超时」和「符号不存在」。
> **可移植**：定义一套 `PanelError` 码表（至少覆盖 DATA_TIMEOUT / SYMBOL_NOT_FOUND / SOURCE_EXHAUSTED / RATE_LIMITED / INSUFFICIENT_DATA / LOOK_AHEAD），`app.py` 的异常统一归一化。这是**纯本地、零风险、当天可完成**的改进。

### 5.2 「不无限等待」哲学（`client.ts`，三处兜底）

| 机制 | 默认值 | 解决的问题 |
| --- | --- | --- |
| `connectTimeoutMs` | 20 s | 连接/认证建立不了就快速失败 |
| `inactivityTimeoutMs` | 60 s | 服务器每 10 s 心跳，超时无数据 = 网络静默丢失（sleep / NAT / 代理超时），**主动判失败**而不是永远挂着 |
| close 兜底 | 3 s | 关闭握手 3 秒未完成就 `terminate()`；fail 后 3 秒没等到 close 也强制收尾 |

注意 `#watchInactivity` 里还有个细节：定时器延迟用 `Math.min(delay, 2**31-1)` 截断，注释写明「更长的定时器延迟会溢出并同时触发」——**`setTimeout` 超 24.8 天会立即触发的坑**。

> **我们的现状**：`datasource.py` 的 `TIMEOUT = 12` 是固定值，没有「连接建立了但数据不来」的半开检测；缓存 TTL 最长 300 s，也没有对应的强制收尾。
> **可移植**：给数据源层加「静默检测」——请求发出后若在 N 秒内既无响应也无错误，视为该源劣化并切下一级。比单纯调大超时更能应对「源返回 200 + 空数组」这类我们已经踩过的坑。

### 5.3 Emitter 的容错设计（`src/events.ts`）

- 单个 listener 抛异常**不影响其他 listener**，也不崩宿主进程（`#call` 逐个 try/catch）
- listener 返回 Promise 时，其 rejection 也被捕获（否则会成为 unhandled rejection）
- `error` 事件**没有任何 listener 时打到 `console.error`**，而不是静默丢弃
- `on()` 返回 unsubscribe 函数；另有 `once` / `onAny`（catch-all，事件名作首参）/ `listenerCount`

> **我们的现状**：`redrawChartsForTheme()` 里有 `try/catch` 回退整页刷新，是同一思路的好实践；但更广的面上，前端多处 `.catch(function(e){ die(el, ...) })` 是各写各的。
> **可移植**：把「一个失败不拖垮全部、失败必须有痕迹」固化成前端统一的事件/错误出口。优先级低于 5.1。

### 5.4 协议级测试替身（`tests/helpers/fake-server.ts`，386 行）

`FakeConnection` 完整实现了 `Transport` 接口（`isOpen` / `send` / `close` / `terminate`），在内存里驱动，**测试协议逻辑完全不需要网络**：
- `send()` 时真的走一遍 `decodeFrames` 解析，记录 `sent[]` 和 `heartbeats[]`
- `terminate()` 模拟 `ws` 的行为：直接断socket，报 1006 异常关闭码（与 `close()` 的 1000 区分）
- `FakeServer.handle(connection, packet)` 让测试方按方法名分发响应

这正是 `transport.ts` 里那个 `Transport` 抽象接口的回报——**因为注入了接口，所以能造替身**。

> **我们的现状**：`tests/` 约 60 个脚本是 HTTP 级 e2e + 各模块 `selfcheck()`，没有「源级替身」。多源降级链（eltdx→腾讯→新浪→同花顺）目前只能靠真实网络验证。
> **可移植**：给 `datasource.py` 定义统一的取数接口，再造一个可注入的假源（可控延迟/可控失败/可控空返回），就能离线测降级链。**这是让我们的 `datasource` 从「能跑」变成「可测」的关键一步**，优先级中高。

### 5.5 `chartData()`：回测结果 → 图表载荷的一次性适配（`backtest/chart.ts`）

一个把回测产出转成「价格图页面一次拿全」的函数：

```ts
{ bars,                    // 冻结的 K 线
  indicators,              // 信号函数的指标序列，逐 bar，undefined 转 null
  warmupEndTime,           // 第一个可交易 bar 的开盘时间（热身段结束）
  splitTime,               // 第一个样本外 bar 的开盘时间
  signals,                 // 信号（含 refBars）
  trades }                 // 按 exit id 分组的交易
```

并且**强制校验 `snapshot.sha256 === report.snapshot.sha256`**，防止把 A 次回测的报告配 B 次快照的图。

> **我们的现状**：因子实验室/策略评估页是前端多次请求拼数据；热身段、样本内外分割线这些「图上的关键参考线」没有明确的时间戳契约。
> **可移植**：给 `factor_lab.evaluate()` / `strategy_eval` 加一个 `chart_payload()`，把「K 线 + 指标线 + warmupEndTime + splitTime + 信号点 + 交易标注」打成单一 JSON，前端一次拿全、按 splitTime 画分割线。与 A3（快照冻结）天然配对。

### 5.6 基线不只是百分位，还有 p5/p50/p95

`randomBaseline()` 除了 `percentileRank`，还输出 `p5 / p50 / p95` 三个分位数（`quantile()` 用线性插值），外加 `matchedRuns` / `meanTrades` ——后者用于在报告里警告「1000 次里只有 812 次抽到了同等笔数，因为合格 bar 不够」。

> **可移植**：A1 落地时直接把这套口径做全，别只做单一百分位。`matchedRuns < runs` 时必须警告——否则「跑赢 96%」可能建立在不可比的样本上。

### 5.7 脏数据清洗的具体手艺

`searchMarkets` 里服务器返回的 `symbol` / `description` 带 HTML 高亮标签，要 `replace(/<\/?em>/g, '')` 剥掉。加上第一轮看到的 `normaliseBase64`（URL-safe + 去空白 + 补 padding）和 `decodeCompressed` 的五级容错（ZIP→原样→zlib→raw deflate→gzip 依次试）——

> **共同模式：对接第三方时，永远假设返回是脏的，并且把「清洗」和「解析」分成可单测的纯函数。**
> 我们的 `eltdx_source.py` 已经这么做了（复权口径、单位换算、异常值过滤收拢在一处，模块 docstring 写明三个坑），这点上我们和它是同一水平。值得补的是：**把这些清洗逻辑从 docstring 里的经验，变成 selfcheck 里的断言**（例如「单位换算比值必须 ≈ 1.0000」这类可复现的检查）。

### 5.8 `llms.txt`：给 AI agent 的一页能力清单

仓库根目录一个纯文本文件，把整个 API（含 backtest 协议的全部默认值和口径）压缩成一页，明确写「No claim of profitability」「It is not a market-data query CLI. No MCP server or hosted trading API is shipped」。

> 我们有 `server/mcp_server.py`，但缺一份等价的「能力与边界」单页。对新用户和对你让我改这个项目，这类文件能少走很多弯路。**低成本高回报。**

---

## 六、修正后的可移植清单

把两轮发现合并，按「性价比 × 独立性」重排：

| # | 项目 | 工作量 | 依赖 | 前置条件 |
| --- | --- | --- | --- | --- |
| **A1** | 随机入场基线（含 p5/p50/p95 + matchedRuns 警告） | ~130 行 | 无 | 合成数据自检：同种子可复现、随机均值≈0 |
| **A2** | 因果性自动检查（截断重放） | ~90 行 | 无 | 因子函数签名统一 |
| **A3** | 快照冻结 + SHA-256 | ~100 行 | 无 | 与 A5 配对 |
| **A4** | **错误码分类学**（22 码的精简版） | ~60 行 | 无 | 先定码表，再逐处替换字符串 detail |
| **A5** | `chart_payload()`：回测→图表一次拿全 | ~70 行 | A3 | 前端约定 splitTime/warmupEndTime 画参考线 |
| **A6** | 数据源静默检测 + 强制收尾 | ~50 行 | 无 | 不动现有 TTL 语义 |
| B1 | 数据源接口化 + 假源注入（可测降级链） | ~150 行 | 无 | 先抽接口再造假源 |
| B2 | bar 内高低点 + 悲观成交 | ~80 行 | 无 | 必须叠加涨跌停/T+1 |
| B3 | 非标准周期（Heikin Ashi / Renko / Range） | ~120 行 | 无 | 复用分钟线通道 |
| B4 | WS 推送替代轮询 | ~200 行 | 无 | 走腾讯 push2，不能破坏 chartCache |
| B5 | 统一错误出口（前端） | ~40 行 | A4 | — |
| C1 | `llms.txt` 等价物（能力与边界单页） | ~80 行 | 无 | 与 README 分工 |

**仍然不建议**：远程 Pine 执行、`scanner.tradingview.com` 选股、复刻其 WS 协议层（理由见 §3.3 与 §四 C 级）。

---

## 七、一句话结论

**TradingView-API 在「数据面」上和我们不构成竞争——它拿的是全球二手行情，我们拿的是 A 股交易所级协议；但它在「回测方法论」和「协议客户端工程」两方面都明确领先我们。** 方法论上领先三招：随机入场基线（含 p5/p50/p95）、因果性自动检查、快照可复现；工程上领先三招：22 个机器可读错误码、三处「不无限等待」兜底、协议级测试替身。这六招都不依赖 TradingView，纯本地可实现，合计约 600 行，是最该移植的部分。

---

## 附 A：第三轮自检——我们自己的使用数据推翻了我上面的优先级

上轮报告通篇在讲「往因子实验室 / 策略评估里移植 A1–A5」。但从未看过**用户实际在用什么**。查 `data/tick.db`（2026-10-26，只读）：

| 指标 | 数值 |
| --- | --- |
| `screen_history` 按模块 | **newbie 679** ＞ strategy 288 ＞ screen 151 |
| 近 7 天筛选次数 | 486（占历史总量 1118 的 43%，仍在活跃） |
| `watch_items` / `trades` / `positions` | 787 / 45 / 13 |
| `ai_usage` | 每人每天 **1 次** |

**结论：使用量第一的路径是「小白选股」（预设式筛选），而我在 §六 里排的 A1–A5 全都在使用量第二、第三的因子/回测路径上。优先级排反了。**

修正后的排序（同一批技术，换接入点）：

| 项 | 原接入点 | **修正后优先接入点** |
| --- | --- | --- |
| A1 随机入场基线 | factor_lab / strategy_eval | newbie 预设策略的"跑赢随机"标注（使用量 679 的路径） |
| A4 错误码 | app.py 全局 | 不变（全局收益） |
| A3 快照 + SHA-256 | factor_lab | newbie + strategy 共用（可复现是信任前提） |
| A2 因果性检查 | 因子函数 | strategy 信号函数 |
| A5 chart_payload | 因子实验室 | newbie 结果页（一次拿全，少 N 次请求） |

**方法论教训（比结论更重要）**：我在没有查看自己产品使用数据的情况下，就替用户决定了"什么值得改进"。专家的做法是先看数据再定优先级——这也是本报告 §五 那条「从最原始素材入手」的反面例证。

---

## 附：证据出处

| 结论 | 出处 |
| --- | --- |
| 协议帧格式与 UTF-16 计长 | `src/protocol/framing.ts` |
| 深历史分批 5000 + 事件驱动续拉 | `src/data/history.ts` |
| 超时/中止/清理保证 | `src/data/operation.ts` |
| 会话复用与 seed | `src/quote/quote-session.ts` |
| 四种退出模型 | `src/backtest/exits.ts` |
| 悲观成交与跳空 | `src/backtest/simulate.ts` L58-85 |
| 随机基线与百分位 | `src/backtest/stats.ts` L95-152 |
| 快照冻结与 SHA-256 | `src/backtest/snapshot.ts` |
| 因果性检查规则 | `docs/backtest.md`「The look-ahead check」 |
| 2 小时零错误实测 | `docs/v4-reliability.md` |
| 连通性实测 | 本报告 §3.3（2026-10-25 沙箱 IPv4 实测） |
| 我方数据源与降级 | `server/datasource.py`、`server/sources/eltdx_source.py` |
| 我方前视防护 | `server/factor_lab.py` L448-460、L743-830 |
| 我方成本与 T+1 | `server/backtest.py`、`server/equity_sim.py` L14-15 |
| 22 个错误码与归一化 | `src/errors.ts` |
| 三处不无限等待兜底 | `src/client/client.ts`（`connectTimeoutMs`/`inactivityTimeoutMs`/`#forceClose`） |
| Emitter 容错 | `src/events.ts` |
| 协议级测试替身 | `tests/helpers/fake-server.ts`（386 行） |
| chartData 图表载荷适配 | `src/backtest/chart.ts` |
| 基线 p5/p50/p95 与 matchedRuns | `src/backtest/stats.ts` L120-152 |
| 能力与边界单页 | `llms.txt` |
| 我方技术评级（对照） | `server/indicator_rating.py` |
| 我方错误处理（对照） | `server/app.py` 138 处 `HTTPException` 全为字符串 detail |
