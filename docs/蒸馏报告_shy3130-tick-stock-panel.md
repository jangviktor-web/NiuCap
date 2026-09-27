# 蒸馏报告：shy3130/tick-stock-panel → 牛来选股面板可借鉴项

> 抓取方式说明：GitHub `git clone` / `raw` / `api` 子域在沙箱内被 TLS 阻断，本报告通过 WebFetch 代理抓取 `github.com` 主域的 HTML 目录树与 `/raw/` 文档正文完成。覆盖：README、docs/features.md、docs/market-phase.md、docs/strategy.md、docs/custom-data-source.md、AGENTS.md、backend/app 目录树。未逐行阅读 300+ 源文件（受代理对超大文件摘要限制），但核心架构、功能口径、阈值已完整提取。

- 目标仓库：`https://github.com/shy3130/tick-stock-panel`（MIT，v0.2.2，v0.3 / AI 助手分支开发中）
- 蒸馏日期：2026-09-27
- 结论先行：对方在**架构工程化、AI 助手、监控/异动、市场阶段与主线、因子平台、数据源插件化**六个方向明显领先；**选股/回测/快讯/情绪**双方基本对等，我们个别点更务实。建议搬思路不搬栈。

---

## 1. 目标项目概览

| 维度 | shy3130/tick-stock-panel | 我们的牛来选股面板 |
|------|--------------------------|--------------------|
| 定位 | A 股「选股 + 监控 + 回测」量化工作台，自托管零运维 | A 股量化选股与择时分析面板 |
| 后端栈 | FastAPI + **Polars** 向量化 + **DuckDB** 列式存储 + **vectorbt** 回测 | FastAPI + **pandas** + **SQLite**（483MB 行情库） |
| 前端栈 | **Vite + React + TypeScript**，插槽化（`src/custom/`） | **单文件** `web/index.html`（原生 JS） |
| 数据源 | **TickFlow 官方 SDK** + 可插拔第三方（YAML 声明 / Python 插件） | go-stock（行情/财务/龙虎榜）+ 新浪7x24 + 同花顺快讯 |
| 数据粒度 | 日 K **+ 分钟 K（全量落盘）** + 五档盘口 | 日 K 为主（分钟级能力缺失） |
| 资产 | 股票 **+ ETF** | 仅股票 |
| AI | 对话助手（18 工具，流式，开发中） | #93 待做（需 Key） |
| 规模 | 多模块、CI/Docker/GHCR、文档体系完整 | 8 个 Tab，已上线 Gitee 公开 + WorkBuddy 云 |

**许可相容性**：双方均为 MIT，可自由借鉴思路甚至代码（建议保留原作者署名与上游链接）。

---

## 2. 功能清单与双方对照

| 功能模块 | 对方 | 我们 | 差距判定 |
|----------|------|------|----------|
| 选股引擎 | 25 内置 + AI 生成 + 自定义信号（Polars 表达式热加载） | 29 策略 + 条件选股（自定义信号） | **对等**（我们策略更多，对方扩展方式更灵活） |
| 指标体系 | 均线/震荡/量能/原子信号，enriched 列式落盘 | 61 项指标 | 对等 |
| 回测引擎 | vectorbt 三模式 + 因子归因 + 导出 CSV + 候选复测 + 蒙卡回撤 | 止损止盈/金字塔/网格（基础） | **我们偏弱**（缺导出/候选复测/归因） |
| 个股分析 | 专用日 K + **9 类关键价位**（纯函数）+ AI 四维 | 行情条 + 技术面结论 + K 线通道 | 对等（对方价位维度更系统） |
| 市场温度 | 5 档 state + 情绪雷达 | **13 维市场温度画像** + 市场宽度 | **我们更细**（但缺周期阶段/主线） |
| 市场阶段 | **6 阶段情绪周期**（冰点/启动/主升/高潮/退潮/修复）+ 主线识别 | 无 | **我们缺失** |
| 连板梯队 | 各连板层级统计 + 封单 | 无 | 缺失 |
| 概念/行业轮动 | RPS 轮动 + 概念涨幅 | 快讯关联股票 tag | 偏弱 |
| 监控中心 | **四类规则** + 冷却去重 + 严重级别 + 弹窗声效 + 外部推送 | #91 待做 | **我们缺失** |
| 异动监控 | 竞价/盘中/偏移三 Tab + 龙虎榜 + 盘后 AI 复盘 | 双源快讯（带情绪标） | **我们偏弱** |
| AI 助手 | 18 只读工具 + 流式 + 工具足迹卡 + 解耦扩展 | 无（#93 待做） | **我们缺失** |
| 因子平台 | DSL 编辑器 + IC/IR/Newey-West/BH-FDR + 因子↔策略桥 | 无 | 缺失 |
| 分钟策略 | 盘中信号 + 分钟回测（共用特征构造器） | 无 | 重基建缺失 |
| 数据源插件 | YAML 六数据集契约 + 能力路由 + Tushare/CSV 接入 | 硬编码 3 源 | **我们僵化** |
| ETF | 支持 | 无 | 缺失 |
| 第三方数据接入 | HTTP 定时拉取 / CSV 上传 / JSON 写入 + schema 发现 | 无 | 缺失 |

---

## 3. 关键技术亮点深蒸馏

### 3.1 市场阶段（情绪周期）与主线识别 —— 最值得搬
实现：`backend/app/services/market_phase.py` + `market_mainline.py`。**只用本地已有的 `consecutive_limit_ups`（连板数）、`amount`、概念映射快照**，不依赖额外数据源，2020-08 起全历史可回算。

**6 阶段体系**（与原有 5 档 `state` 并存，不替换）：

| 阶段 | 核心判定（EMA 平滑 alpha=1/3 约 5 日 + 2 日确认后生效） |
|------|------|
| 高潮 climax | ge2（2 板以上家数）≥ 50，或首板 ≥ 220（历史占比 <2%） |
| 主升 rally | 高度 ≥7 且 ge2 ≥15 且晋级率 ≥0.23；或晋级率 ≥0.30 配合高度 ≥5、ge2 ≥12 |
| 退潮 ebb | 晋级率 <0.15 且宽度自 5 日前高位回落；或晋级率 <0.13 且封板率 <0.57 |
| 启动 ignite | 宽度/高度自低位扩张（ge2 较 5 日前 +3 且 ≥8，或高度抬升且 ≥5）且晋级率恢复 ~0.19 |
| 冰点 ice | 高度 ≤4、ge2 ≤6、首板 ≤24 同时贴地 |
| 修复 repair | 兜底（占比最高，~74% 天数） |

- 优先级：`climax > rally > ebb > ignite > ice > repair`。
- **弱档否决**：当 5 档 `state ∈ {weak, lean_weak}` 时，正向阶段（主升/高潮/启动）一律降为修复——修复了「连板强但大盘崩」的误判（如 2024-01 微盘流动性危机）。
- 平均段长 **9.7 天**（对比原 5 档 1.1–1.5 天），回填验收 1454 交易日。
- **主线分** = `0.35×涨停数 + 0.25×最高板 + 0.25×梯队档位数 + 0.15×二板宽度`；每概念当日涨停 <3 家不排名；宽基/风格标签（融资融券 ~7700 家）按成员数上限 600 过滤，避免垄断 top1；每日 top30 持久化。

> 可借鉴性：★★★★★。我们已有 13 维温度画像，叠加这套连板梯队→情绪周期阶段，能把「温度」升级成「周期位置 + 主线」。**数据可行性已核实**：我们的 `daily_bars` 表（code/date/open/close/high/low/volume/amount）虽无独立连板列，但涨停日可由 `close` 相对 `prev_close` 的涨跌幅阈值推出，连续涨停计数即连板数——**无需新增采集**。板块阈值（主板 10% / 创业板科创板 20%）若缺股票类型可用主板 10% 近似，或补一张股票基础信息表。阈值常量与 EMA+2 日确认逻辑可直接移植。

### 3.2 监控中心 + 异动监控 —— 对应我们的 #91
统一规则引擎管理**四类监控**：策略监控（扫描结果变化）、个股信号监控（如 `RSI>80`）、价格涨跌监控、全市场异动。特性：多条件 AND/OR、冷却期去重、严重级别（info/warn/critical）、右下角弹窗可配声效、`alerts.jsonl` 持久化、未读徽标。**外部推送**支持飞书/企业微信/通用 JSON Webhook（HMAC-SHA256 可选）/SMTP。

异动页独立三 Tab：竞价异动（盘前风向标 + 追高风险标记，来自 60 日回测）、盘中异动（涨停/炸板/翘板/跌停/新高/新低/放量聚合）、偏移异动（交易所偏离值口径，按板块分档实时算接近度）。

> 可借鉴性：★★★★☆。正是我们 #91 盯盘提醒的目标形态。规则引擎 + 外部推送（尤其企业微信/飞书 Webhook）可直接设计。

### 3.3 AI 对话助手 —— 旗舰缺口（对方也在开发中）
- **18 个只读工具**覆盖全站：个股实时/日线/财务五表、大盘看板/指数/regime/异动、板块轮动、自选/持仓/信号库、策略与因子目录/回测。
- **逐字流式输出** + **工具足迹卡**（每次调用工具名/参数/耗时/结果摘要可展开核对，取数可核对是设计铁律）+ 非模态右滑面板（⌘K 呼出）。
- **完全解耦扩展**：后端 `app/custom/assistant/`（启动自动发现注册独立路由）、前端 `src/custom/assistant/`（构建挂载插槽），删除即卸载；未配 Key 或不支持工具调用的供应商时 **fail-closed**。

> 可借鉴性：★★★★☆（价值高，但重）。我们 #93 可直接参考其「工具化取数 + 流式 + 足迹卡 + fail-closed」设计范式，不必重造轮子。**注意：对方此功能分支仍「本分支开发中」，不是成品，别照搬半成品**。

### 3.4 数据源插件化 / 能力路由 —— 解我们硬编码之痛
YAML 声明契约，支持六数据集（`daily`/`adj_factor`/`realtime`/`minute`/`full_minute`/`financial`）：`field_map` 字段映射 + `transforms`（如 `parse_date`）+ 三种鉴权（bearer/header/query）+ `pct_unit` 单位契约（百分制必须显式声明，绝不数值猜测）。支持 Tushare HTTP 定时拉取、CSV/Excel 上传、JSON 写入，自动 schema 发现 + 符号归一。

> 可借鉴性：★★★☆☆。我们现硬编码 go-stock/新浪/同花顺，抽一层 YAML 数据源契约能立刻提升可维护性，也为「用户接自己 Tushare」铺路。但要把我们现有 3 源改造成「标准字段映射」需重构 store/抓取层。

### 3.5 因子平台 / 分钟策略 —— 重基建，谨慎
因子平台（DSL 编辑器、25 算子、79 字段、IC/IR/Newey-West/BH-FDR、因子↔策略四座桥）和分钟策略（共用 `intraday_features.py` 特征构造器、条件上升沿防未来函数）是工程重镇，依赖 Polars/DuckDB + 全量分钟落盘基建。

> 可借鉴性：★★☆☆☆。思路（因子→策略一键生成、因子归因）可记，但基建成本极高，不建议近期做。

---

## 4. 可优化到牛来选股面板的建议（按优先级）

### P0｜低成本高价值，可直接搬（建议 #95–#97）
1. **市场情绪周期阶段 + 主线识别**（来自 3.1）
   - 在现有「市场温度」页追加 6 阶段标签（冰点/启动/主升/高潮/退潮/修复）+ 主线排行。
   - 数据已具备：`daily_bars` 可由 `close/prev_close` 推导涨停与连板数，零新增采集；阈值常量与 EMA+2 日确认直接移植。
   - 价值：把我们「温度」升级为「周期位置 + 主线」，是差异化卖点。
2. **监控中心 + 外部推送**（对应 #91，来自 3.2）
   - 先做四类规则引擎 + 右下角弹窗 + `alerts.jsonl`；外部推送优先做企业微信/飞书 Webhook（HMAC 可选）。
   - 价值：补齐实时盯盘，用户留存显著提升。
3. **个股分析 9 类关键价位**（来自 features：压力支撑/成交密集/枢轴点/前高前低/Keltner/ATR 通道/缺口/斐波那契/整数关口）
   - 我们已有 K 线通道，补其余 8 类纯函数价位，毫秒级、零新增采集。
   - 价值：个股页专业度直接对齐对方。

### P1｜中等成本，需扩展（建议 #98–#100）
4. **AI 助手范式移植**（对应 #93，来自 3.3）
   - 采用「工具化取数 + 流式 + 工具足迹卡 + fail-closed」而非对方半成品；先做 6–8 个只读工具（实时/日线/财务/大盘/策略/回测），⌘K 呼出。需用户 Key（deepseek/openai_compat）。
5. **回测增强**：导出 CSV（概要/净值/交易明细/分标的）、保存候选 + 一键载入复测、因子归因 tab。我们已有基础回测，补这几项性价比高。
6. **数据源 YAML 契约**（来自 3.4）：把 go-stock/新浪/同花顺抽成标准 field_map，预留 Tushare/CSV 接入。先不动抓取逻辑，只做配置层抽象。

### P2｜重基建，谨慎评估（建议远期）
7. 因子平台 / DSL 编辑器（依赖 Polars/DuckDB 迁移，风险高）。
8. 分钟级策略与回测（需全量分钟落盘基建，存储与计算成本陡增）。
9. ETF 支持、第三方数据接入 UI 上传（依赖资产类型与 schema 发现体系）。

---

## 5. 直接可复用清单（思路/契约级）

- 情绪周期 6 阶段阈值常量表（3.1）—— 直接抄常量，改 EMA/2 日确认即可。
- 主线分加权公式（3.1）—— 一行加权，立即可用。
- 监控规则四类型 + AND/OR + 冷却去重 + 严重级别（3.2）—— 规则 Schema 可复用。
- Webhook JSON 信封 + HMAC-SHA256 签名（3.2）—— 推送安全性直接借鉴。
- 数据源 `field_map`/`transforms`/`pct_unit` 契约（3.4）—— 作为我们配置层目标形态。
- AI 助手「工具足迹卡 + fail-closed + 解耦扩展」范式（3.3）—— 架构设计范本。
- 关键价位 9 类纯函数口径（3.5）—— 个股页补强清单。

---

## 6. 风险与注意

- **栈不匹配**：对方 Polars/DuckDB/React，我们 SQLite/pandas/单文件 HTML。搬思路不搬代码；涉及分钟/因子/列式存储的要先评估迁移成本。
- **AI 助手未完工**：对方明确「本分支开发中」，仅参考范式，勿抄半成品。
- **数据前提**：情绪周期/连板梯队依赖连板数计算，需先确认我们库是否具备；不具备则 P0-1 降级为先补数据。
- **许可**：MIT，借鉴可保留署名；若直接引用代码片段，建议在文件头注明 upstream 链接。
- **不要过度工程**：我们的单文件轻量栈是优势（部署简单、WorkBuddy 云一行起），引入 React 构建链会抵消该优势——优先在现有 `web/index.html` + FastAPI 内扩展。

---

## 7. 建议落地顺序（供排期）

1. **#95 市场情绪周期 + 主线**（P0，先验证连板数据源）
2. **#96 监控中心 + 外部推送**（P0，补 #91）
3. **#97 个股 9 类关键价位**（P0）
4. **#98 AI 助手范式移植**（P1，需 Key）
5. **#99 回测增强（导出/候选/归因）**（P1）
6. **#100 数据源 YAML 契约抽象**（P1）
7. 因子/分钟/ETF（P2，远期）

> 备注：以上编号（#95–#100）为建议新任务，待你确认后入队。当前待办队列中 #90 自选股成本线盈亏 / #91 盯盘提醒 / #92 hover 弹图 / #93 AI 深度分析 / #94 港美股 仍可保留，#91 可由 #96 吸收。

---

## 8. 源码级移植规格（二次深抓，本次新增）

> 二次抓取方式：同样经 WebFetch 代理，`/raw/` 直取对方 `backend/app/services/` 下关键源文件，已拿到 **market_phase.py / market_mainline.py / alert_store.py / webhook_adapter.py / tool_catalog.py** 完整源码。下方给出可直接照搬的常量、公式与接口；标注「〔栈差异〕」处需按我们 SQLite/pandas 栈改写。

### 8.1 市场情绪周期（来自 market_phase.py，可直接搬常量）

**阶段词汇与优先级**（一字不改可复用）：
```python
PHASE_ICE, PHASE_IGNITE, PHASE_RALLY, PHASE_CLIMAX, PHASE_EBB, PHASE_REPAIR = (
    "ice", "ignite", "rally", "climax", "ebb", "repair")
PHASE_LABELS = {"ice":"冰点","ignite":"启动","rally":"主升","climax":"高潮","ebb":"退潮","repair":"修复"}
_PHASE_PRIORITY = (PHASE_CLIMAX, PHASE_RALLY, PHASE_EBB, PHASE_IGNITE, PHASE_ICE)  # 修复兜底
```

**阈值常量（标定自 2020-08~2026-08 分位数，原样可用）**：
```python
CLIMAX_GE2 = 50; CLIMAX_FIRST_BOARD = 220
RALLY_HEIGHT = 7; RALLY_GE2 = 15; RALLY_PROMO = 0.23; RALLY_PROMO_ALT = 0.30; RALLY_GE2_ALT = 12; RALLY_HEIGHT_ALT = 5
EBB_PROMO = 0.15; EBB_PROMO_STRICT = 0.13; EBB_SEAL = 0.57; EBB_RECENT_GE2 = 12; EBB_RECENT_HEIGHT = 6
IGNITE_GE2_DELTA = 3; IGNITE_GE2 = 8; IGNITE_PROMO = 0.20; IGNITE_HEIGHT_DELTA = 1; IGNITE_HEIGHT = 5; IGNITE_PROMO_SOFT = 0.19
ICE_HEIGHT = 4; ICE_GE2 = 6; ICE_FIRST_BOARD = 24
PROMO_MIN_POOL = 10          # 晋级率最小池，低于此记 null（小样本噪声）
_EMA_ALPHA = 1.0/3.0         # EMA 约 5 日
_CONFIRM_DAYS = 2            # 连续 2 日同标签才切换
_VETO_STATES = {"weak","lean_weak"}   # 大盘弱档否决（见下）
```

**判定顺序要点（踩坑已记在源码注释里，照搬即可避坑）**：
1. 高潮：`ge2>=50` 或 `首板>=220`。
2. 主升：高度/宽度/晋级率同时过 p60，或晋级率≥0.30 配合高度≥5、ge2≥12。
3. **冰点优先于退潮**（否则长期死寂市场会被误标退潮）：高度≤4 且 ge2≤6 且首板≤24。
4. 退潮：自 5 日前高位回落 且 晋级率≤0.15；或晋级率≤0.13 且封板率≤0.57。
5. 启动：ge2 较 5 日前 +3 且≥8，或高度抬升且≥5，晋级率恢复 ~0.19/0.20。
6. 兜底 repair（占比最高 ~74% 天数）。
7. **弱档否决**：正向阶段（主升/高潮/启动）出现在 5 档 `state∈{weak,lean_weak}` 的日子，一律降为 repair——修复「连板强但大盘崩」误判（2024-01 微盘流动性危机）。
8. **持续性**：每个新标签需连续 `_CONFIRM_DAYS=2` 日才生效，否则沿用旧标签（防一日游抖动）。

〔栈差异〕对方的 `classify_phase_series` 是 Polars DataFrame，我们的 `daily_bars` 是 SQLite/pandas。判定函数 `raw_label(i)` 是纯 Python（依赖 EMA 平滑后的 height/first_board/ge2/promo/seal 序列），**可直接照抄**，只需把 Polars 聚合换成我们的 SQL/pandas 聚合（见 8.3）。`state` 列（5 档弱档否决）我们目前没有——**首版可先去掉否决**（依赖我们 13 维温度画像做等价弱市判断即可），或先用 `market_overview` 的综合评分分档近似。

### 8.2 主线分（来自 market_mainline.py，一行加权）

```python
_SCORE_WEIGHTS = {"limit_up_count":0.35, "max_boards":0.25, "rungs_filled":0.25, "ge2_count":0.15}
_MIN_LIMIT_UP = 3            # 单概念当日涨停 <3 家不排名
_TOP_PER_DAY = 30            # 每日持久化 top30
# 截面 rank 归一(0-1) → 加权主线分(0-100)
score = 100 * sum(_SCORE_WEIGHTS[c] * rank_norm(c) for c in _SCORE_WEIGHTS)
```

〔栈差异〕聚合依赖 `ext_gn_ths` 概念映射快照（我们仓无历史概念成分表）。**首版可用我们已有关联股票映射（快讯 ext / go-stock 概念）近似**，或先只做「涨停数 / 最高板 / 梯队」的纯价维度主线，概念映射后续补。注意对方 `MEMBERSHIP_NOTE` 明确：概念成分是当前快照回看历史，早年有归属漂移——我们若做也需同样的口径提示。

### 8.3 连板数推导（我们 `daily_bars` 的迁移实现参考）〔关键前置〕

对方存 `consecutive_limit_ups` 列；我们 `daily_bars(code/date/open/close/high/low/volume/amount)` 无此列，但可由 `close/prev_close` 推。参考实现（pandas）：
```python
def is_limit_up(close, prev_close, board="main"):
    # 主板 10% / 创业板·科创板 20% / 北交所 30%（ST 5%）；用 0.5% 容差吸收四舍五入
    thr = {"main":0.10, "cyb":0.20, "kj":0.20, "bse":0.30}.get(board, 0.10)
    return (close - prev_close) / prev_close >= thr - 0.005

def consecutive_limit_ups(df):
    # df 已按 symbol,date 排序；返回每只每个交易日截至当日的连续涨停天数
    df = df.sort_values(["code","date"])
    df["is_lu"] = df.apply(lambda r: is_limit_up(r.close, r.prev_close, board_of(r.code)), axis=1)
    # 连续计数：遇非涨停归零，否则 +1
    grp = df.groupby("code")["is_lu"]
    df["consec"] = grp.apply(lambda s: s.groupby((~s).cumsum()).cumsum())
    return df
```
派生量：`height=当日最大consec`、`first_board=consec==1 求和`、`ge2=consec>=2 求和`、`promo=昨日连板池今日继续封板比例`、`rungs_filled=consec>=2 的档位数`。

〔数据可行性〕已核实：我们的 `daily_bars` 有 `close` 与按 code+date 排序可取的 `prev_close`（`prev_close` 可用 `lag(close) over(partition by code order by date)` 算，无需新增采集）。**前提是每只股票要知道板块类型**（主板/创业板/科创板/北交所）才能选对涨停阈值——可补一张 `instruments` 基础表（code→板块），或用主板 10% 近似（误差仅创业板科创板，影响 ge2/高度统计，不影响阶段逻辑主框架）。

### 8.4 监控中心（来自 alert_store.py + webhook_adapter.py）

**告警落盘 schema（alerts.jsonl，每行一个 JSON）**——照搬 `append()` 字段约定：
```json
{"ts": 1717000000000, "rule_id": "r1", "source": "price|signal|strategy|market",
 "type": "limit_up|rsi>80|...", "symbol": "600519", "severity": "info|warn|critical",
 "msg": "...", "value": 88.5}
```
- 保留策略：`MAX_DAYS=7` + `MAX_RECORDS=5000`，每 `PRUNE_EVERY=20` 次写入触发一次滚动清理（线程锁 `_lock` 保护）。
- 未读徽标：`count()` 返回总数；点击标记已读 = `delete_one(ts)` 或 `clear()`。
- 规则引擎四类型：**策略监控（扫描结果变化）/ 个股信号（如 RSI>80）/ 价格涨跌 / 全市场异动**；多条件 AND/OR + 冷却期去重 + 严重级别。〔栈差异〕规则 Schema 用我们 `server/` 新增 `alerts.py` 实现，前端加「监控中心」tab + 右下角 toast。

**外部推送（webhook_adapter.py，签名可直接抄）**：
- 飞书：`open.feishu.cn/open-apis/bot/v2/hook/`，签名 `HmacSHA256(timestamp+"\n"+secret)` 后 Base64，放 `timestamp`+`sign` 字段；瞬时失败退避重试 3 次（冷却在事件生成时打戳，瞬时 5xx 不重试会被冷却窗口压掉）。
- 企业微信：`qyapi.weixin.qq.com/cgi-bin/webhook/send?key=`，markdown 原生支持，按**字节**截断 4096（中文 3 字节），每分钟≤20 条靠 cooldown 兜底。
- 通用第三方：`secret` 时 HMAC-SHA256 签原始 body，头 `X-TickFlow-Timestamp` + `X-TickFlow-Signature: sha256=<hex>`；信封 `{event, timestamp, title, body, data}`。
- 铁律：**推送失败静默降级，绝不阻断告警主流程**（落盘/SSE 优先）。

### 8.5 AI 助手工具范式（来自 tool_catalog.py）

范式（非半成品，值直接抄）：
1. **工具目录 = OpenAI tools JSON Schema 列表**，`build_tool_schemas()` 纯静态序列化（因子/策略/数据源能力/回测 4 类目录工具），让 LLM 按需检索，**不把全量塞进 system prompt**。
2. **`execute_tool(name, args)` 统一分发**，返回 `{"ok":bool,"result"|"error"}`——工具异常统一回填 LLM，不打断循环。
3. **回测工具白名单**：`run_backtest` 只回传精简 stats 键（total_return/annual_return/max_drawdown/sharpe/sortino/calmar/win_rate/profit_factor/n_trades/avg_pnl），**绝不把 equity_curve/成交明细喂给 LLM**（控 token）。
4. **fail-closed**：无 Key / 不支持 tool-calling 的供应商 → 入口直接关闭，不降级裸返回。
5. **流式 + 工具足迹卡**：每次工具调用显示「工具名/参数/耗时/结果摘要可展开」，取数可核对是设计铁律。

〔栈差异〕对方「18 工具」在 features.md 描述，但 `tool_catalog.py` 只注册了 4 个目录/回测工具（其余在 assistant 路由层按需注入）。我们 #93 可直接按此范式：先实现 `list_strategies`/`run_backtest`/`list_factors`(我们有 61 指标可作「因子」)/`list_data_capabilities` 四个只读工具 + 逐字流式 + 足迹卡即可，不必重造。**注意对方明确「本分支开发中」，仅参考范式，勿抄半成品代码**。

### 8.6 个股 9 类关键价位（来自 features，标准公式清单）

我们已有 K 线通道（压力/支撑），补其余 8 类纯函数即可：① 成交密集区（N 日成交量加权均价附近密集成交带）② 枢轴点 Pivot（(H+L+C)/3，R1/S1=2P-H/L，R2/S2=P±(H-L)）③ 前高前低（N 日极值）④ Keltner 通道（EMA20±2·ATR）⑤ ATR 通道（均值±k·ATR）⑥ 缺口（跳空高/低开未补缺口）⑦ 斐波那契（0/23.6/38.2/50/61.8/100 回撤位）⑧ 整数关口（心理价位）。均为毫秒级纯函数，零新增采集。

---

## 9. 二次蒸馏结论（与首次对照）

| 维度 | 首次（文档层） | 本次（源码层）新增价值 |
|------|----------------|------------------------|
| 情绪周期 | 阈值表 + 思路 | **完整常量 + `raw_label` 判定顺序 + 冰点优先/弱档否决/持续性 3 个踩坑** → 可直接落代码 |
| 主线 | 权重公式 | **截面 rank 归一 + top30 + 概念快照回看限制** → 实现路径明确 |
| 连板前置 | 「需确认库能否算」 | **pandas 推导参考实现 + 板块阈值表 + 数据可行性已闭环** |
| 监控 | 规则 Schema 思路 | **alerts.jsonl 字段 + 飞书/企微 HMAC 签名代码 + 静默降级铁律** → 可照抄 |
| AI 助手 | 18 工具描述 | **tool_catalog 分发范式 + 回测 stats 白名单 + fail-closed** → 架构范本 |
| 关键价位 | 9 类清单 | 标准公式清单（纯函数，零采集） |

**落地清晰度提升**：#95（情绪周期）与 #96（监控）已从「建议」升级为「带常量/签名的移植规格」，开工即可写。#93（AI）范式确定。建议排期不变（#95→#96→#97→#98→#99→#100）。

> 备注：以上编号（#95–#100）为建议新任务，待你确认后入队。当前待办队列中 #90 自选股成本线盈亏 / #91 盯盘提醒 / #92 hover 弹图 / #93 AI 深度分析 / #94 港美股 仍可保留，#91 可由 #96 吸收。
