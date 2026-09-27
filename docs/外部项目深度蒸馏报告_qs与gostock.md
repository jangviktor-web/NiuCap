# 外部项目深度蒸馏报告 · a-share-quant-selector 与 go-stock

> **分析对象**（本次为**全部源码级精读**，非 README 级）：
> - [Dzy-HW-XD/a-share-quant-selector](https://github.com/Dzy-HW-XD/a-share-quant-selector)（下称 **qs**，Python 单策略批处理，约 6,000 行）
> - [ArvinLovegood/go-stock](https://github.com/ArvinLovegood/go-stock)（下称 **gs**，Go+Wails 桌面盯盘软件，Go 后端约 7,900 行 + Vue3 前端约 10,900 行）
>
> **对照对象**：本项目 tick-stock-panel（29 策略 / 61 指标 7 组 / 走查回测 / 80+ 路由）
> **分析时间**：2026-09-26
> **前作**：《外部项目对照与升级方案.md》（2026-09-22，README 级分析；其中 B1 匹配已落地为 `server/similarity.py` 修正版 `pattern_score`）

---

## 〇、一句话总评

**qs 是「数据管道卫生课」，gs 是「免费数据源 + AI 卡片百科」。**

| | qs | gs |
|---|---|---|
| 本质 | 单机 Python 定时批处理：腾讯/akshare 抓日线 CSV → 每日跑单一「碗口反弹」→ 钉钉推文+K线图 | Wails v2 桌面应用：robfig/cron 秒级轮询自选股 → chromedp 爬页面 → OpenAI 兼容 SSE 出 AI 卡片 → 钉钉/系统通知 |
| 强项 | 数据工程守门逻辑、风险过滤、通知限流 | **免费接口矩阵（新浪/腾讯/东财/财联社/雪球，URL 全齐）**、AI 上下文工程、词典情绪分析、自选股体验 |
| 弱项 | 单策略、无回测、「DTW」实为插值欧氏距离、断网 mock 假数据兜底（毒化回测） | 桌面特有依赖（chromedp/托盘）、成本报警重复触发、无策略扫描与回测 |
| 与我们关系 | 已消化大头（B1→pattern_score 修正版），剩**廉价过滤器与同步守门**没拿 | **几乎全是新东西**：快讯、情绪、AI、提醒、成本线、港美股 |

---

## 一、qs 蒸馏要点（源码级）

### 1.1 碗口反弹策略的真实逻辑（与 README 不一致处以代码为准）

- **知行短期趋势线** = `EMA(EMA(close,10),10)`（双重 10 日 EMA，与 M1-M4 无关，`technical.py:162`）；
- **知行多空线** = `(MA14 + MA28 + MA57 + MA114) / 4`（CHANGELOG 记录 2024-02-10 从 5/10/20/30 改来；docstring 里的「MA5+10+20+30」是过时注释）；
- 信号只看**最新一根 K 线**：名称过滤 → 有效成交 → J 异常过滤 → 双线多头（短趋>多空）→ `J ≤ J_VAL` → **最大阴量过滤** → M 日内存在放量阳线（`量比≥N 且 阳线 且 市值≥CAP`）→ 位置分类（🥣回落碗中 > 📊靠近多空线 ±3% > 📈靠近短期线 ±2%）；
- **参数三处不一致**：代码默认 N=4/M=15/J_VAL=30，生效 YAML 是 N=2/M=30/J_VAL=20，README 又写 N=2.4/M=20——三个来源互相矛盾，以 `config/strategy_params.yaml` 为准。

### 1.2 两个廉价风险过滤器（我们没有，3 行代码级）

1. **最大阴量过滤**（`bowl_rebound.py:239-244`）：回溯 M 天内**成交量最大的那根 K 线若是阴线 → 整只剔除**。对「放量出货」的廉价防御，可做成全局风险开关并用 walk_forward 验证增量价值。
2. **J 值异常过滤**（`bowl_rebound.py:222-224`）：`head(30) 的 |J| 均值 > 80 → 剔除`，防复权缺口/数据错位导致的 KDJ 失真。通用数据卫生检查。

### 1.3 数据管道守门（值得移植到 SQLite 同步链）

- **智能更新三重判断**（`main.py:93-139`）：① 15:00 前不更新（防盘中价污染收盘库）② `.update_cache.json` 记录当日已更新则跳过 ③ **抽样 100 只检查当天数据覆盖率**，100% 有则跳网络。
- **增量预筛**（`akshare_fetcher.py:834-868`）：逐股 `read_csv(nrows=1)` 只读首行拿最新日期 → 算每股缺口天数（+2 缓冲、封顶 60）→ 零需求跳过。避免为判断是否更新而全量读库。
- **失败断点续传**：`failed_stocks.json` 记录失败代码，下次跳过。
- **市值单位启发式**：`cap < 1e10` 判为亿元乘 1e8（实用但危险，注意它还有 `hash(code)%500` 伪市值这种反面教材）。

### 1.4 B1 匹配引擎的两个可取细节（补充到 pattern_score 的思路）

- **案例窗口取「突破日之前」的数据**（`pattern_library._extract_window`：`date < breakout_date` 的前 25 天）——匹配**蓄势态**而非突破态，避免突破大阳线污染特征。我们的 pattern_score 若加「案例模式」，应沿用此设计。
- **特征缺失时给 0.5 中性分**而非 0 分（`pattern_matcher.py`）——容错约定，避免数据缺失被当作强负信号。
- 其余核实：所谓 DTW 优先用 `fastdtw` 库，**fallback 是「插值对齐 + 欧氏距离」，不是 DTW**；实际生效权重被 YAML 覆盖为 trend 0.10 / kdj 0.20 / volume 0.25 / **shape 0.45**（README 写 30/20/25/25 是默认值，不是生效值）。

### 1.5 钉钉通知工程（做推送时直接抄）

- **RateLimiter**（`dingtalk_notifier.py:30-95`）：60s 滑动窗口 20 条 + 最小间隔 2s + 收到 **errcode=660026** 时设全局锁 `_lock_time = now + min(2^retry, 30)` 指数退避。
- **18000 字节 UTF-8 安全分段**（`:560-586`）：多字节字符边界回退截断，段间 sleep(1)。
- 反面教材：`data:image/png;base64` 塞 markdown 发图（钉钉不支持，显示不出）；全项目**没有** @提醒实现；在线改参 POST /api/config 直接覆盖 YAML 无备份无校验。

### 1.6 明确不抄

腾讯 ifzq 上限 1000 条的「6 年」日线；CSV 倒序+逐股文件；**断网 mock 随机游走兜底**（静默毒化回测，绝对反面教材）；`close*2e8` 估算市值；web_server 同步全市场扫描阻塞请求线程。

---

## 二、gs 后端蒸馏要点（源码级）

### 2.1 免费数据源矩阵（最有价值的部分，URL 已核实）

| 用途 | 接口 | 要点 |
|---|---|---|
| A股实时 | `http://hq.sinajs.cn/rn={ts}&list=sh600000,sz000001` | Header `Referer: https://finance.sina.com.cn`，**GB18030 转码**；32 字段五档盘口 |
| 美股实时 | 同上，前缀 `gb_`（如 `gb_aapl`） | parts[21] 盘前价 [22] 盘前涨跌幅 [12] 市值 [14] PE [13] EPS |
| 港股实时 | 腾讯 `http://qt.gtimg.cn/?q=r_hk09660` | `~` 分隔；美股代码 `usAAPL.OQ` |
| 日K通用 | `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq` | 港/美/A 一套通吃（我们已有） |
| 分时 | `https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600000` | 美股 `.../UsMinute/query` |
| 新浪日K | `http://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol={code}&scale=240&ma=yes&datalen={days}` | scale 单位分钟，240=日线 |
| **财联社电报** | `https://www.cls.cn/telegraph`（goquery 抓 `div.telegraph-content-box`） | 2 个 span=时间+内容；红条 `span.c-de0422`=加红；个股搜索 `.../searchPage?keyword={name}&type=telegram` |
| **新浪 7x24** | `https://zhibo.sina.com.cn/api/zhibo/feed?callback=callback&page=1&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1&type=0` | JSONP 剥壳取 `result.data.feed.list`；tag 含「焦点」→ 加急 |
| 雪球热股 | `https://stock.xueqiu.com/v5/stock/hot_stock/list.json?...&_type={sha\|hk\|us}` | 需浏览器 Cookie（硬编码 token，易过期） |
| **新浪资金流（免token）** | 行业 `.../MoneyFlow.ssl_bkzj_bk?...&fenlei={1\|0}`；个股排名 `.../ssl_bkzj_ssggzj`；个股时序 `.../ssl_qsfx_zjlrqs?daima=sh600000` | 我们 fundflow 依赖 westock CLI，这三个可做**免依赖降级链** |
| **龙虎榜增强** | 东财 `datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&...` | 比我们 lhb 多 **D1/D2/D5/D10_CLOSE_ADJCHRATE 后验涨跌幅** 与 **EXPLAIN 上榜原因** |
| 港股列表 | `https://stock.gtimg.cn/data/hk_rank.php?board=main_all&metric=price&pageSize=500&reqPage={n}` | JSONP；备用新浪 `Market_Center.getHKStockData` |
| 财经日历 | `https://www.cls.cn/api/calendar/web/list?...&sign={md5}` | 签名算法在 `market_news_api.go` |

### 2.2 AI 工作流（OpenAI 兼容 SSE）

- 协议：`POST {BaseUrl}/chat/completions`，`stream:true`，逐行解析 `data:` SSE；同时解析 `delta.content` 与 `delta.reasoning_content`（兼容 DeepSeek-R1 类推理模型）。无工具调用、无 embedding。
- **上下文「伪对话注入」模式**（`NewChatStream`，7 个 goroutine 并发采集）：时间 → 三指数（爬新浪页面转 markdown 表）→ 120 天 K 线（**JSON 转 markdown 表，省 token 的主要手段**）→ 现价盘口 → 财报（雪球主指标）→ 市场电报 20 条 → 个股新闻，按 user/assistant 交替注入，绕开超长 system。
- 指数跳过财报/新闻（`checkIsIndexBasic`）；数据源失败只注入警告文本不中断。
- **每股独立 cron**（`FollowedStock.Cron` 字段，用户逐股设 AI 分析时间）→ 结果落 `ai_response_result` 表**只留最新一条**做缓存回显。
- 每日总结：时间 + 3 指数 + 最近 100 条电报 →「总结市场新闻中的投资机会」。

### 2.3 词典情绪分析（纯本地零成本，`stock_sentiment_analysis.go`）

分词 → 词典打分，规则完整可移植：
- 正/负词各 35 个，权重 1.5~3.0（涨停/跌停 3.0，利好/利空 2.5，超预期 2.5 …）；
- **否定词**（不/没/无/非/未/别/勿）看前一词直接反转符号；
- **程度副词**作乘数（非常 1.8 / 极其 2.2 / 稍微 0.6 …）；
- **转折词**（但是/然而/不过/却/可是）把文本切段，**后段得分 ×1.5**；
- `score > 1 → 看涨，< -1 → 看跌，否则中性`。每条新闻入库即打分。

### 2.4 盯盘提醒引擎

- 规则极简：每股两字段 `AlarmChangePercent`（默认 3%）+ `AlarmPrice`；成本价报警隐式启用。
- 每秒 cron 批量拉实时 → 按 A/HK/US 交易时段函数过滤 → 阈值判断在前端 → 钉钉 markdown（内嵌新浪分时图）或系统通知（win: go-toast / darwin: osascript）。
- **防重**：freecache 按 code 设 TTL（涨跌 5min、价格/成本 30min）。⚠️ 但成本报警未走 TTL，每次刷新重复推送——**抄功能时必须修这个缺陷**。

### 2.5 明确不抄

chromedp 无头浏览器池（重；仅雪球财报页需要）；otto 解 JSONP（Python `re.sub` 剥壳即可）；页面爬现价兜底（我们行情链更稳）；75 万字节敏感词表。

---

## 三、gs 前端蒸馏要点（功能级）

> 勘误：UI 框架是 **Naive UI**（非 Element Plus）；自选股是**三列卡片墙**非表格；**无右键菜单、无拖拽**（预设的这两个交互不存在）。

### 3.1 自选股卡片（stock.vue, 2131 行）

- **价格数字滚动动画**：`<n-number-animation :from :to :duration="1000">`，原生等价 = `requestAnimationFrame` 补间 1s，比闪烁高级；
- 成本行：`成本:12.50*1000 8.30% ( 1040.00 ¥ )` 按盈亏红/绿——**只在卡片文字展示，没有 K 线画成本线**（我们用 ECharts markLine 反超它）；
- 四宫格（高/低/昨收/今开）+ 盘口折叠面板（头部直接显示买一卖一，展开看五档）；
- **搜索定位闪烁**：`scrollIntoView({behavior:'smooth'})` + `.blink-border` 红边框 1s 无限闪 5s 后移除；
- 分组 n-tabs（addable/closable），排序靠每股 sort 数字值。

### 3.2 AI 体验（4 种，全部一次性 prompt 非多轮对话）

1. **单票分析**：点按钮**先查缓存秒开历史结果**（模型名 tag + 生成时间），无则弹窗 spinner + SSE 流式 append，`"DONE"` 信号后持久化；
2. 市场快讯 AI 总结（仅该页签显示悬浮按钮）；
3. 每股定时 AI（cron 字段）；
4. **提示词模板系统**：system/user 两类可增删改，user 支持 `{{stockName}}/{{stockCode}}/{{costPrice}}` 占位。
- 结果渲染：单一 Markdown 流（md-editor-v3），分块靠 prompt 让模型输出三级标题；底部固定免责声明；**导出矩阵**：存图（html2canvas）/复制/存 MD/存 Word。

### 3.3 值得抄的 UI/UX 细节

- **hover 即指即看**：龙虎榜/研报/公告/资金表全部名称单元格 hover 弹 800px popover 内嵌 K 线或资金图（列表零跳转）；
- **公告风险语义着色**（~20 行）：标题含 质押/冻结/解禁/减持/退市/停牌/破产→红，回购/重组/诉讼/收购/调研→橙，否则蓝；
- **同一数据换 9 种排序 = 9 个资金页签**（净流入/流出/净流入率/主力占比…props 复用同一渲染函数）；
- 龙虎榜**空数据自动回退前一日**（递归至多 7 次，带进度提示）；
- 全局水印免责声明；热股榜 5s 轮询带**热度/排名变化 ↑↓ 箭头列**；
- 不值得抄：弹幕、托盘/全局快捷键（桌面特有）、跳问财外链、成本报警无去重。

---

## 四、落地清单（按 价值÷成本 排序，已对照本项确认空白）

### ✅ 已消化确认（无需重做）
B1 模板匹配（实测否定 → `pattern_score` 修正版）、走查回测（`walkforward.py`）、龙虎榜基础榜（`/api/lhb`）、资金流（`/api/fundflow`，westock 链）、研报/公告列表（`/api/reports` `/api/notices`）、腾讯分时/K线（`/api/kline_intraday`）。

### P0 · 半天级，即抄即用

| # | 项 | 来源 | 落地方式 |
|---|---|---|---|
| 1 | **最大阴量过滤器** | qs | ✅ **已完成（2026-09-26）**：`_apply_risk_filters` 全局过滤层（合成后统一剔、tune 可关、diag.risk_filter 透出）；实测 pullback_ma20 397→306 剔 91 只。walk_forward 增量验证待做 |
| 2 | **J 值异常过滤** | qs | 数据卫生：`head(30) |J| 均值>80` 剔除，挂进 `_data_lag()` 旁的诊断层 |
| 3 | **龙虎榜后验字段** | gs | ✅ **已完成（2026-09-26）**：`/api/lhb` 切东财源（`wst.lhb_em`），上榜原因 EXPLAIN + D1/D2/D5/D10 后验复权涨跌幅 + 同 code 去重 + 节假日自动回退 + 失败降级 westock；前端 D 列红绿着色；`tests/check_lhb_em.py` 16/16 |
| 4 | **公告/研报风险着色** | gs | ✅ **已完成（2026-09-26）**：前端 `noticeTone()`——风险类红/事件类橙/普通默认，公告列表已应用 |
| 5 | **同步守门三重判断** | qs | ✅ **已完成（2026-09-26）**：`gate_full_sync` 三重守门（时间/当日 cache/抽检 100 只 ≥90%，--force 可绕）+ `bars_progress()` 增量预筛（缺口分档拉，参考日=max(众数,期待日) 防与判断③打架——周六补周五矛盾真实踩到并锁死用例）；CLI 与管理页手动落库均生效；`tests/check_gate_sentiment.py` 36/36 |

### P1 · 1~2 天级，面板体验升级

| # | 项 | 来源 | 落地方式 |
|---|---|---|---|
| 6 | **双源快讯流** | gs | ✅ **已完成（2026-09-26）**：**源重选**——财联社 HTML 已改版 JS 渲染（0 条）+ API 拒签，改用**新浪 7x24 + 同花顺快讯**（动手前 10 轮×5 源稳定性探测，probe 脚本可复跑）；`server/newsfeed.py` 归一化 + 两级去重（源内 id + 跨源内容指纹）+ 30s TTL + 单源失败降级；新页签「📰 快讯」红条（同花顺 color=2）/来源徽章/关联股票 tag（A股可点跳个股）/情绪点；自检 15/15 + e2e 44/44 |
| 7 | **词典情绪分析** | gs | ✅ **已完成（2026-09-26）**：`server/sentiment.py`——**免分词**词典扫描（比原方案的 jieba 更稳：长词优先 find 定位，分词会把「创新高」切碎丢词）；否定窗口扩到前 3 字（中文否定带衬词）、程度词整体优先于逐字否定（防「非常」的「非」误判）；`/api/notices` 每条带 sentiment + 前端 ▲看涨/▼看跌/─中性 徽章；`POST /api/sentiment` 通用端点留给快讯流复用；自检 25 项真值表 |
| 8 | **自选股成本线盈亏** | gs | watch 表加 `cost/volume`；自选表格加盈亏 tag + 今日盈亏；**K 线 markLine 画成本横线**（超过原实现）；阈值穿越触发提醒 |
| 9 | **盯盘提醒** | gs | watch 加 `alarm_pct/alarm_price`；浏览器 `Notification` API + 可选钉钉 webhook（markdown 内嵌自绘分时图 dataURL 前先验证）；**必须加 TTL 防重（5/30min），修 gs 的重复推送缺陷** |
| 10 | **hover 即指即看** | gs | 事件委托 + 300ms 延迟 + 单例浮层 + ECharts 实例复用；给龙虎榜/资金流/研报/公告/自选统一加 |
| 11 | **AI 个股深度分析** | gs | OpenAI 兼容 SSE；**抄「伪对话上下文注入」**但用我们的 61 指标+筹码+关键位替代原始 120 天 OHLCV 表（更省 token 更专业）；结果按 code 落库只留最新；前端 marked 流式 + 缓存秒开 + 模板管理 + 免责声明 |
| 12 | **港美股行情** | gs | 新浪 `gb_` 前缀（含盘前盘后）+ 腾讯 `r_hk`；我们 `datasource.py` 已有 hk/us normalize，只差 quote 适配与前端页签 |

### P2 · 增强，随缘

| # | 项 | 要点 |
|---|---|---|
| 13 | 每日 AI 总结 | 100 条电报 + 我们的温度/广度 → 收盘后一次 LLM 调用 |
| 14 | 新浪资金流三接口 | `ssl_bkzj_bk/_ssggzj/_zjlrqs` 免 token，作 westock 降级链 + 全市场资金排行页 |
| 15 | 价格滚动动画 + 闪烁定位 | rAF 补间 `animateNumber(el,from,to)` + `.blink-border`，~60 行 |
| 16 | 资金流 9 视角组织 | 同数据换 sort 复用渲染函数 |
| 17 | 龙虎榜空数据日期回退 | 循环前一日，toast 进度，~20 行 |
| 18 | 全局免责水印 | canvas 平铺，~30 行 |
| 19 | pattern_score 补充思路 | 案例窗口取「突破日前」（蓄势态）+ 特征缺失给 0.5 中性分 |
| 20 | 钉钉推送组件 | 做提醒时抄 RateLimiter（20条/分+2s 间隔+660026 指数退避）+ UTF-8 安全分段 |

### 🚫 明确不抄

qs：断网 mock 假数据兜底（毒化回测）、CSV 倒序架构、data-URL 发图、hash 伪市值、无校验在线改参。
gs：chromedp 无头浏览器、otto JSONP、弹幕、托盘/全局快捷键（桌面特有）、跳问财外链、成本报警无去重（抄时必修）。

---

## 五、建议分期路线

1. **第一批（P0 全部，约 1 天）**：两个风险过滤器 + lhb 后验字段 + 公告着色 + 同步守门——全是后端小改，walk_forward 可验证第 1 项的真实价值。
2. **第二批（约 1~2 天）**：快讯 + 情绪（#6/#7）——一个新页签，零 API 成本，是面板从「盘面工具」走向「信息枢纽」的第一步。
3. **第三批（约 1~2 天）**：自选股体验三件套（#8/#9/#10）——成本线 + 提醒 + 即指即看。
4. **第四批（约 1~2 天，需配置 LLM Key）**：AI 深度分析（#11，可选 #13/#14 模板与每日总结）。
5. **港美股（#12）** 视实际需求另立项。

---

## 六、复现与溯源

- 源码存档：`/root/.codebuddy/artifact/2f8a7d30-9c21-4f0b-1d2a-a38240679000/distill/{qs,go-stock-master}/`（ghproxy.net 镜像下载）
- 本文所有 URL/公式/行号均出自上述源码精读，三份原始蒸馏记录：qs 全量精读（6,006 行）、gs Go 后端（约 7,900 行）、gs Vue 前端（约 10,900 行）。
