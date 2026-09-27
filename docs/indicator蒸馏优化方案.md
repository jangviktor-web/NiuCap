# cinar/indicator 蒸馏优化方案（含各方案操作计划书）

> 调研对象：https://github.com/cinar/indicator （Go，v2.1.44，498 个源码文件）
> 结构：trend 40+ / momentum 20+ / volatility 17 / volume 12 个指标，
> strategy 组合器（AND/OR/多数/Sharpe/Sortino），backtest 框架，80 个示例策略。
> 调研日期：2026-09-24。对照对象：tick-stock-panel 现有 22 策略 + 体检/模拟净值/宽度链路。

## 〇、现状盘点（蒸馏的落点）

| 我方资产 | 已有 | 缺口（对照 indicator 库） |
|---|---|---|
| mytt.py | 通达信算子：MA/EMA/SMA/WMA/DMA、HHV/LLV、REF/STD、SLOPE/CROSS/BARSLAST… | 无成型经典振荡器/量能指标 |
| indicator.py（单股K线图） | MACD、RSI6/14、BOLL、CCI、WR | 无 ATR/SuperTrend/吊灯止损叠加 |
| screener.py（策略层） | 16 日线策略（多为形态/量价直觉规则）+ 形态评分 | 无基于经典指标的系统化策略 |
| strategy_eval.py | IC/ICIR + 分层测试（9 策略白名单） | — |
| equity_sim.py | 总收益/超额/最大回撤/胜率/年化 | **缺 Sharpe/Sortino/Calmar/盈亏比** |
| 组合层 | 无 | 缺 AND/OR/多数投票组合器 |
| 市况识别 | 宽度（涨跌家数/MA20比例/新高新低） | 缺趋势市/震荡市开关（Chop） |

**蒸馏原则**：凡新策略必须「daily_bars 可重建」→ 自动进体检白名单 → 分层测试
→ 模拟净值，全链路可验证，不接受无法回测的新策略。

---

## 方案 A：经典指标扩容——新增 6 个可体检策略（核心方案）

**蒸馏源**：volatility/super_trend.go、atr.go、momentum/connors_rsi.go、
td_sequential.go、volume/mfi.go、cmf.go

**做什么**：把 6 个经典指标做成日线策略，注册进 screener，进入体检/分层/模拟
全链路，用我们自己的 A 股数据验证它们到底灵不灵（而不是照单全收）。

| 新策略 | key | 逻辑（指标公式已核对源码） | 蒸馏源 |
|---|---|---|---|
| 超级趋势多头 | `supertrend_long` | ATR(10)×3 通道翻转翻多当日 | SuperTrend |
| ATR 通道突破 | `atr_breakout` | 收盘 > 昨收 + 2×ATR(14)（波动率自适应突破） | ATR |
| 康纳丝超卖 | `connors_rsi_dip` | ConnorsRSI < 15（RSI(3)+连跌Streak的RSI(2)+ROC(1)百分位rank 均值） | ConnorsRSI |
| TD九转抄底 | `td9_buy` | 连续 9 根收盘 < 4 日前收盘（TD setup 计满 9） | TD Sequential |
| 资金流超卖 | `mfi_oversold` | MFI(14) < 20（带量权的 RSI） | MFI |
| 资金流入突破 | `cmf_breakout` | CMF(20) > 0.1 且收盘创 20 日新高 | CMF |

**操作计划书**（预计 ~3.5 小时）：
1. 新建 `server/indicators_extra.py`：纯 numpy 实现 6 个指标序列版
   （ATR/ConnorsRSI/TD序列/MFI/CMF/SuperTrend），每个函数配合成引擎的
   「截断序列 → 当日值」口径；顺带 `selfcheck()` 用手工数据验 12+ 步。
2. `server/screener.py`：注册 6 个策略到 `STRATEGY_DEFS`（cat 归入
   「趋势形态/反转波动/量价涨停」或新增「经典指标」类），带 `hist=True`
   （需要历史序列），参数进 `PARAM_DEFAULTS`。
3. `server/strategy_eval.py`：`EVALUABLE_KEYS` 加 6 个 key（日线可重建，
   白名单机制不变）。
4. `tests/e2e_strategy_eval.py`：表格行数 9→15 的断言同步更新；
   新增 `tests/check_indicators_extra.py`（自检入口）。
5. 跑体检 120 天，把 6 个新策略的 ICIR/分层/模拟净值实测写进部署指南。

**风险**：ConnorsRSI 的 PercentRank(100) 全市场逐日算偏慢（评估时长可能
+20%）；TD 序列状态机要仔细处理前视（只用截至当日的序列）。
**验收**：体检表 15 行全部给出 ICIR 与分层；模拟净值可回放任一新策略。

---

## 方案 B：回测质量指标扩容（最小最快，建议先做）

**蒸馏源**：strategy/sharpe_ratio.go、sortino_ratio.go、outcome.go

**做什么**：模拟净值（equity_sim）的指标行从 8 项扩到 12 项：
- **Sharpe**（年化）＝ mean(日收益) / std(日收益) × √250（源码公式
  `Sharpe = Mean(periodReturns) / StdDev(periodReturns) * Sqrt(periodsPerYear)`，
  无风险利率取 0）
- **Sortino**（年化）＝ mean / 下行std × √250（只罚亏损波动）
- **Calmar** ＝ 年化收益 / |最大回撤|
- **盈亏比** ＝ 平均单笔盈利 / 平均单笔亏损（逐笔）

**操作计划书**（预计 ~1.5 小时）：
1. `server/equity_sim.py`：`_compose()` 的 stats 增加 sharpe/sortino/calmar/
   profit_loss_ratio；逐笔收益列表已在 trade["ret"] 里，直接算；
   selfcheck 加 4 步合成数据断言（恒定日收益的 Sharpe→∞ 处理为 None 等）。
2. `web/index.html` `renderSim()`：chips 数组加 4 项（Sharpe≥1 红、<0 绿）。
3. `tests/e2e_sim_breadth.py`：模拟指标断言加 4 个字段名。
4. 部署指南补指标释义（Sharpe>1 良好、>2 优秀；Sortino 适合看重回撤的
   小白；Calmar 越高回撤换收益越划算；盈亏比 × 胜率 = 期望）。

**风险**：几乎无。40 天窗口 Sharpe 样本少（40 个日收益），标注「窗口短仅供参考」。
**验收**：模拟任一策略，4 个新指标有值且 Sharpe/Sortino 与手算一致。

---

## 方案 C：策略组合器——策略实验室（工程量最大）

**蒸馏源**：strategy/majority_strategy.go、and_strategy.go、or_strategy.go、split_strategy.go

**做什么**：让用户把 2~4 个基础策略拼成组合策略，三种模式：
- **AND 全票**：所选策略当天全部命中才买（高精度低频）
- **OR 任一**：任一命中就买（广撒网）
- **多数投票**：≥N/总数 命中（源码 MajorityStrategy：组内多数派）

组合策略自动获得 key（如 `vote:ma_bull+oversold_rebound+volume_surge`），
直接进体检 → 分层 → 模拟净值链路，回答「组合是否比单打强」。

**操作计划书**（预计 ~3 小时）：
1. `server/screener.py`：加 `run_combo(spec, rows, hist)` 通用执行器，
   spec 解析 `mode:keys` 字符串；组合策略不在 STRATEGY_DEFS 里静态注册，
   由 `STRATEGY_BY_KEY.get` 旁路处理（保持静态注册干净）。
2. `server/strategy_eval.py`：`evaluate()` 接受 `combo_spec` 参数——命中
   集合用 run_combo 现算（组合不进预跑循环，按需单算，避免 22×组合爆炸）；
   `_cache_key` 加 combo 维度。
3. `server/app.py`：`GET /api/strategy_eval?combo=vote:...`、
   `GET /api/equity_sim?combo=...`（模拟直接复用 _HITS_CACHE 旁路：组合的
   hits 由体检 job 算完单独存 `_COMBO_HITS`）。
4. `web/index.html`：策略页签加「🧪 策略实验室」按钮 → 弹层选策略（多选
   上限 4）+ 模式单选 → 提交后台 job → 结果复用体检面板渲染。
5. e2e：建一个固定组合（如 `vote:ma_bull+oversold_rebound`）验证体检+模拟
   全链路 + 页面交互。

**风险**：体检循环改动面大（需单测防未来函数回归）；组合数爆炸——**限制
最多 4 个因子**；AND 模式可能整天零命中（需空组合日的处理，已有先例）。
**验收**：多数投票组合的 ICIR、分层、模拟曲线全部产出；与成员策略单打对比
显示在同屏。

---

## 方案 D：市况开关——趋势市/震荡市识别（差异化强）

**蒸馏源**：volatility/chop.go（Choppiness Index）

**做什么**：
1. 每日算全市场等权指数的 Chop(14)：`100 × log10(ΣTR14 / (HH14-LL14)) / log10(14)`；
   **Chop < 38.2 = 趋势市（适合趋势策略）、> 61.8 = 震荡市（适合超跌反弹类）、
   之间 = 过渡市**。
2. 体检升级：每个策略按「当日 Chop 分组」分别统计超额 → 输出
   `regime_fit`（趋势市超额 / 震荡市超额 / 差值）→ 表格加一列
   **「市况敏感度」标注**（如 ma_bull「趋势市+2.1% / 震荡市-1.3% → 只做趋势市」）。
3. 市场宽度卡片加一行今日市况读数 + 近 120 日 Chop 迷你折线。

**操作计划书**（预计 ~2 小时）：
1. `server/market_breadth.py`：加全市场等权指数序列（日收益合成价格）→
   Chop(14) 逐日序列 + 当日读数与分组标签；自检 +4 步（手工 Chop 验证）。
2. `server/strategy_eval.py`：evaluate() 聚合阶段按当日 Chop 标签把
   hit/rest 收益分桶，items 增加 `regime: {trend_excess, range_excess, tag}`。
3. `web/index.html`：体检表加「市况敏感度」列（悬停显示两组数值）；
   宽度卡片加市况读数行。
4. e2e：体检表格列断言 + 宽度卡片市况行断言。

**风险**：全市场等权指数是合成指数（无真实指数数据），口径在界面注明。
**验收**：任一策略能看到趋势/震荡两组超额，宽度卡片显示今日市况。

---

## 方案 E：波动率止损参考线（K线图 + 虚拟盘）

**蒸馏源**：volatility/chandelier_exit.go、super_trend.go

**做什么**：
1. 单股 K 线图叠加两条参考线：**吊灯止损**（22日最高 − 3×ATR22）与
   **SuperTrend**（10日 HMA-ATR×3 翻转线），前端图例可开关。
2. 虚拟盘持仓列表加「建议止损」列：多头持仓用吊灯公式实时算出当下止损价，
   跌破即标红提醒。

**操作计划书**（预计 ~2 小时）：
1. `server/indicator.py`（或 mytt.py）：加 `atr_series(H,L,C,14)`、
   `chandelier_series(H,L,C,22,3)`、`supertrend_series(...)` 序列版。
2. K线图 series 数据（`all_indicators`/`channel_series`）加两列；
   `web/index.html` K线图渲染两线 + 图例。
3. 虚拟盘：`server/app.py` 持仓 API 补 `stop_ref` 字段；前端表格加列。
4. 自检 +6 步（手工 ATR/吊灯值验证）；e2e 补 K线图两线断言。

**风险**：SuperTrend 翻转逻辑有状态（趋势方向记忆），序列版要仔细对齐；
K线图布局空间有限（两线挤）。**验收**：K线图两线随时间连续无断点异常，
虚拟盘止损价与手算一致。

---

## 方案 F：指标历史分位——跨股票可比（可选轻量）

**蒸馏源**：helper/percent_rank.go

**做什么**：给 `history.metrics()` 加通用「当前值在近 250 日里的百分位」：
`rsi14_pct`、`vol_ratio_pct`、`bias_ma20_pct`。展示为「RSI 处于自身一年
85% 高位」，让不同价格的股票可以横向比较「贵不贵/热不热」。

**操作计划书**（预计 ~1 小时）：
1. `server/history.py`：加 `_pct_rank_now(arr, period=250)`；metrics() 对
   rsi14（需先在 metrics 算 rsi14——当前 metrics 无 RSI，需补）与 vol_ratio、
   bias_ma20 输出分位。
2. 单股指标面板显示三行分位；`tests` 自检 +3 步。
**风险**：metrics() 是全市场高频调用，250 日分位要 O(1)（用排序切片即可，
勿逐日循环）。**验收**：任一股票返回分位且 0~100。

---

## 实施顺序建议

| 顺序 | 方案 | 工作量 | 理由 |
|---|---|---|---|
| 1 | B 回测质量指标 | ~1.5h | 最小最快，直接增强已有交付 |
| 2 | A 经典指标 6 策略 | ~3.5h | 核心价值：新策略全部经体检验证 |
| 3 | D 市况开关 | ~2h | 差异化强，与 A 的策略天然联动 |
| 4 | C 策略组合器 | ~3h | 工程量最大，依赖体检框架稳定 |
| 5 | E 止损参考线 | ~2h | 独立，随时可插 |
| 6 | F 指标分位 | ~1h | 轻量可选 |

> 未采纳（说明理由）：valuation（NPV/PV 估值，与短线面板无关）、
> asset/tiingo（数据源层，我方已有 4 套源）、backtest/html_report（已有
> Web 前端，不需要 Go CLI 报表）、ichimoku（参数对 A 股适配存疑，进备选池）。
