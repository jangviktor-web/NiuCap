# 任务 #95：市场情绪周期 + 主线识别 —— 排查与方案

> 流程：备份 → 排查问题 → 方案 → 实现 → 测试 → 确认。
> 备份目录：`backups/task95-20260928-002429/`（app.py / index.html 各一份）。
> 蒸馏来源：`docs/蒸馏报告_shy3130-tick-stock-panel.md` 第 8.1 / 8.2 / 8.3 节源码级常量。

## 一、现状盘点（探查结论）

| 项 | 现状 | 结论 |
|----|------|------|
| `daily_bars` | code/date/open/close/high/low/volume/amount/updated_at，无 prev_close 列；覆盖 2024-06-07~2026-09-24，272 万行，5569 只 | 连板数可由 `LAG(close)` 推导；覆盖 ≈2.3 年，够 EMA 加热 |
| 板块判定 | `server/newbie.py:_board(code)` / `_limit_pct(code)` 已按代码前缀给主板10%/创业科创20%/北交所30% | **直接复用**，无需新建基础表 |
| 实时连板天梯 | `server/hithink.py:limit_up_pool()/limit_up_ladder()` 同花顺源，每项含 `continue_day_cnt` 与 `reason`（板块原因） | 主线走「实时」口径，规避缺历史概念映射表 |
| 涨停/跌停近似 | `server/market_breadth.py` 已有按板块阈值口径 | 封板率可从 high/close 推，有先例 |
| 5 档 state（弱档否决） | 对方 regime_builder 有 `state` 列；我方无 | 首版**跳过否决**，用 13 维温度画像做等价弱市判断 |

## 二、可能出现的问题与解决方案

### 问题 1：连板数推导的板块阈值与 ST
- **现象**：daily_bars 无板块类型、无 ST 标记。ST 股 5% 涨停在 10% 阈值下不会被识别为涨停 → 不计入连板梯队（属合理近似，与对方"近似家数看趋势"口径一致）。
- **方案**：复用 `newbie._board` 的代码前缀判定（bj/30x/68x），阈值 10/20/30%。ST 不计入连板（可接受）。容差用 `(1+pct/100-0.005)` 吸收四舍五入。

### 问题 2：历史数据不足（仅 2024-06 起，对方标定 2020-08 起）
- **现象**：阈值常量是对方用 1454 交易日 p10/p60/p90 标定的。我们用 2.3 年分布会与其略有偏移。
- **方案**：**阈值常量原样照搬**（不重标定），阶段逻辑对阈值绝对水平不敏感；EMA(alpha=1/3)+2 日确认只依赖序列平滑，不依赖全历史分位。在报告里已标注「我们分布偏短，阶段占比可能与对方有偏差，属正常」。

### 问题 3：连板数全量计算性能（272 万行窗口函数）
- **现象**：对 5569 只 × 560 交易日做 `LAG` + 连续计数窗口，首算可能 10~30s，且每次请求不能重算。
- **方案**：① 一次性计算全序列；② 结果持久化到 `data/market_phase_daily.json`（约 560 行 × 小字段）；③ 模块级缓存，按 `daily_bars` 最大日期判脏：脏才重算并回写；④ 端点首次调用若未就绪返回 `{ready:false}`，前端显示「预热中」，后台线程预热，避免阻塞请求。

### 问题 4：封板率 seal_rate / 晋级率 promo_rate 所需字段缺失
- **现象**：对方 seal_rate 来自 regime 列、promo 来自连板池；我方无。
- **方案**：均从 daily_bars 现算——seal_rate = 封板数/(封板数+炸板数)，炸板=high 触及涨停价但 close 未封；promo_rate = 昨日连板池今日续板比例（consec==prev+1）。二者仅用于「退潮」分支判定，缺失也不影响主框架。

### 问题 5：主线所需历史概念映射表缺失
- **现象**：对方主线依赖 `ext_gn_ths` 概念快照（我方无）。
- **方案**：主线分两路——① **实时主线**：用 `hithink.limit_up_pool()` 按 `reason`（板块/概念原因）聚合当日涨停，加权分排序，标「实时」；② 历史主线暂不依赖概念表，仅作为延展预留。这样主线立即可用且真实。

### 问题 6：弱档否决（state 列）缺失
- **现象**：对方用 5 档 state 否决「连板强但大盘崩」的误标。
- **方案**：首版去掉该否决（注释保留说明）。等价弱市判断可由我们 13 维温度综合评分分档近似，作为后续增强。不阻塞 #95。

### 问题 7：前端注入与样式一致
- **现象**：温度卡片已有「盘后/实时」标签体系，新增情绪周期需对齐。
- **方案**：情绪周期阶段标「盘后」（来自 daily_bars 最新交易日），主线标「实时」（来自同花顺）；阶段用专属配色（冰点蓝/启动绿/主升红/高潮深红/退潮橙/修复灰），避免与涨跌红绿混淆。

## 三、实现方案（落地口径）

### 后端 `server/market_phase.py`（新增）
- `board_pct(code)`：复用 newbie 口径。
- `compute_daily_series(conn)`：SQL CTE 算 per-date 聚合（height/first_board/ge2/ge3/ge5/promo_rate/seal_rate），全序列缓存 + JSON 落盘。
- `classify(daily)`：照搬蒸馏 8.1 的常量与 `raw_label` 顺序（冰点优先于退潮、连续 2 日确认、跳过 state 否决），EMA 平滑。
- `current_phase()`：最新阶段 + 近 20 日阶段史。
- `mainline_today()`：hithink 实时涨停按 reason 聚合。
- `ensure_ready()`：后台预热 + 脏检查。

### 后端 `server/app.py`
- 新增 `GET /api/market_phase`：返回 `{ready, phase, label, color, history, height, first_board, ge2, promo_rate, seal_rate, as_of, mainline, mainline_source}`。

### 前端 `web/index.html` `loadMarketTemp()`
- 并行 `api('/api/market_phase')`，在 KPI 网格下方注入「情绪周期」块：阶段大标签 + 近 20 日阶段色条 + 连板高度/首板/二板家数 + 实时主线 Top 列表。

## 四、测试口径
1. 单元自检：连板数推导（取已知连板股验证 consec 正确）；6 阶段在近期数据不抛错、分布合理（非全 repair）。
2. API：`/api/market_phase` 结构正确、`ready=true`、mainline 非空（交易时段）。
3. 前端：Playwright 截图温度卡片含情绪周期块，标签/配色正常。
