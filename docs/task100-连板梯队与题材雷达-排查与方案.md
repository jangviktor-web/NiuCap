# #100 连板梯队 + 情绪周期 + 题材雷达（融合去重）——排查与方案

> 来源蒸馏：`jundizhou/easy-stock`（A股 AI 投研工作台，PolyForm Noncommercial）。
> 目标：把 easy-stock 的「超短连板梯队 + 情绪周期」与「趋势题材雷达（融合去重）」蒸馏进牛来面板。
> 约束：**不影响现有功能**；东财被沙箱拦截，只能用腾讯/新浪/eltdx（westock）。

---

## 一、排查结论（现状盘点）

| 能力 | 牛来现状 | 缺口 |
|---|---|---|
| 今日涨停/炸板 | ✅ `moves.detect_moves(rows)` 已算；`/api/moves` 已暴露 | — |
| 涨跌分布 + 涨停/跌停计数 | ✅ `westock.changedist()`（`up_limit`/`down_limit`） | — |
| 龙虎榜 | ✅ `westock.lhb_market()` 已有 | — |
| **市场情绪周期（6 阶段）** | ✅ `market_phase.py` #95 已做（冰点/启动/主升/高潮/退潮/修复） | 已覆盖，本任务**复用并同屏展示** |
| 词典情绪 | ✅ `sentiment.py`（公告/研报） | — |
| 市场宽度 | ✅ `market_breadth.py` | — |
| **连板天数 / 梯队 / 晋级率 / 昨日反馈** | ❌ 仅有"今日涨停"，无连板天数 | **本任务补** |
| **题材/板块雷达（融合去重）** | ⚠️ 行业板块 `westock.sector_rank()` 可用；概念板块 ❌（东财被拦） | **本任务补（行业口径）** |

**数据源可达性实测**：
- 腾讯 `qt.gtimg.cn` / 新浪 `hq.sinajs.cn` ✅（牛来运行期在用）
- 东财 `push2*.eastmoney.com` ❌ 沙箱拦截（curl 空返回）
- eltdx CLI ✅ `/root/.local/bin/westock`（westock 行业板块/温度可用）
- `get_kline(code,"1d",count)` ✅ 腾讯/新浪 K 线（连板天数要用）
- `daily_bars` 表 ⚠️ 仅覆盖策略选股宇宙（非全市场）→ **连板不能靠它，需对涨停子集取 K 线**

---

## 二、实现方案（最小改动，纯增量）

### A. 连板梯队 `server/limitup.py`（新文件）
- `build_ladder(rows)`：`moves.detect_moves(rows)` 取今日涨停 → 对涨停子集调 `get_kline(code,"1d",count=12)` 算**连板天数**（复用 `moves._limit_pct` 判定 10/20/30/ST）；按天数分梯队（首板/2板/3板/4+板）。
- **情绪周期**：调用 `market_phase.get_phase_data()` 取 6 阶段 + 主线，同屏展示（不重造）。
- **晋级率 / 昨日反馈**：从涨停子集 K 线取昨日 change%（涨停=晋级、红=溢价、绿=闷杀）；晋级率 = 今日连板数 / 昨日涨停数（昨列表缓存于 meta，首跑退化成连板率）。
- **性能**：涨停子集约 30–150 只，逐只 K 线 ~0.4s → 首次约 15–60s。采用**后台线程计算 + 按交易日缓存**（对齐 #98 strategy_eval prewarm 模式）：API 立即返回今日涨停 + 缓存的连板；首跑返回 `computing=true`，前端轮询/自动刷新补满。
- **交易日历**：连板按交易日对齐（周末/节假日不误判），避免 easy-stock 踩过的坑。

### B. 题材雷达 `server/theme_radar.py`（新文件）
- `build_radar(limit=40)`：`westock.sector_rank()` 取行业板块（涨幅 + 主力净流入 + 上涨宽度`up_count` + 领涨股）→ 四维归一评分（强度/资金/宽度/持续天数，持续天数由 `main_net_5d/20d` 趋势近似）→ 排序。
- **融合去重**：实现 `fuse_boards(*lists)` 名称归一（去"证券/券商"类重叠）+ 去重合并 → 结构上预留"行业+概念"双口径融合（概念走东财，沙箱不可用，待开放）。
- **降级**：eltdx CLI 不可用 → `sector_rank` 返回 [] → 接口返回空 + `source_error`，前端提示"数据源不可用"，**不影响其他功能**。

### C. 路由 `server/app.py`
- `GET /api/limitup`（参数 `limit`）：返回 `{date, phase, mainline, 涨停, ladder, rate, max_height, computing}`。
- `GET /api/theme`（参数 `limit`）：返回 `{date, boards:[{name,score,change_pct,main_net,up_count,leader,...}]}`。
- 复用 `MARKET.ensure()` 取全市场快照（与 `/api/moves` 同源，已验证）。

### D. 前端 `web/index.html`
- 导航新增两个按钮：`🔥 连板梯队`、`🎯 题材雷达`（放在现有 tab 之间，不挪动现有）。
- 两个 `<div class="tab" id="tab-limitup/tab-theme">` 容器。
- 点击委托加 `if(want==='limitup') loadLimitup();` / `if(want==='theme') loadTheme();`（沿用现有懒加载模式）。
- 渲染：连板用梯队卡片 + 情绪周期条；题材用雷达评分表 + 热力着色（沿用现有 `.card` 样式）。

### E. 后台纳管 `server/maintain.py`
- `modules_status()` 追加 `limitup` / `theme` 两项（与 #97 纳管风格一致，仅追加，不改既有项）。

---

## 三、风险清单（R1–R10）

| # | 风险 | 应对 |
|---|---|---|
| R1 | eltdx CLI 不可用 → 题材雷达空 | 接口降级返回 `source_error`，前端提示，不 500 |
| R2 | 连板 K 线逐只慢/限流 | 后台线程 + 按日缓存；单只超时跳过；首跑 `computing` |
| R3 | `MARKET.ensure()` 偶发慢/失败 | 复用现有快照；失败返回 0 涨停 + 错误，不崩面板 |
| R4 | 周末/节假日连板误判 | 对齐交易日历 |
| R5 | 新增 tab/按钮破坏现有布局 | 仅追加按钮与容器，不改现有顺序与样式 |
| R6 | 新路由异常 → 500 | 统一 try/except 返回明确 JSON 错误 |
| R7 | 概念板块（东财）沙箱不可用 | 题材雷达基于行业口径，融合层预留概念接口，透明说明 |
| R8 | 后端改动影响其他接口 | 只新增文件+新增路由+追加纳管，不修改既有逻辑 |
| R9 | 前端脚本语法错误拖垮整页 | 内联脚本语法校验 6/6 |
| R10 | 连板/题材数据延迟显示 | 前端"计算中/数据源不可用"状态，自动刷新补满 |

---

## 四、验证（测试）
- `tests/check_limitup_theme.py`：HTTP 层验证 `/api/limitup`、`/api/theme` 返回结构（涨停数 ≥0、梯队分组、连板率、题材评分排序、降级不 500）。
- Playwright 端到端：点击两个新 tab，确认渲染、无控制台报错、现有 tab 不受影响。
- 回归：#95/#97/#98/#99 相关用例 + 现有 `check_*` 不退化。
