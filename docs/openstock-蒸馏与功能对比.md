# OpenStock 全量蒸馏 × 牛来面板功能对比

> 抓取源：`github.com/Open-Dev-Society/OpenStock`（main 分支，2026-09-26）
> 目的：全量抓取 → 蒸馏功能 → 筛出**可迁移到牛来面板**的能力
> 视角：ponytail（只搬模式，不搬代码；AGPL 下搬运思路而非源码）

---

## 1. OpenStock 项目画像

| 维度 | 内容 |
|---|---|
| 定位 | 美股行情替代品（Finnhub 数据 + TradingView 图表），13K+ 注册用户 |
| 技术栈 | Next.js 15 (App Router) · React 19 · TypeScript · Tailwind v4 · shadcn/ui + Radix · Better Auth · MongoDB/Mongoose · Finnhub API · TradingView 嵌入件 · Inngest(事件/cron/AI) · Nodemailer |
| 市场 | **纯美股**（NYSE 会话时钟、Finnhub 标的、USD 计价） |
| 许可 | **AGPL-3.0** —— 若抄源码并作为服务部署，须同协议开源并署名 |

> ⚠️ 许可红线：本报告只萃取**交互模式与算法思路**（命令面板、会话时钟数学、价格闪烁 CSS），落地时用我们自己的 vanilla JS 重写，不复制其 TSX。

---

## 2. 全量功能盘点（来自文件树 + 源码）

| 模块 | 关键文件 | 功能 |
|---|---|---|
| 鉴权 | `lib/better-auth/*` · `app/(auth)/*` | 邮箱密码 + OAuth，中间件保护路由 |
| **全局搜索 / 命令面板** | `components/SearchCommand.tsx` · `cmdk` | **Cmd/Ctrl+K** 唤起；空闲显热门股、输入防抖查 Finnhub；选股跳个股页 |
| 自选股 | `watchlist.actions.ts` · `WatchlistTable` · `WatchlistButton` | 每用户唯一 symbol，增删 |
| 个股详情 | `stocks/[symbol]/page.tsx` | TradingView 高级图 + 财务 + 技术 + 公司档案 + **社媒情绪** |
| 市场总览 | `MarketSwitcher` · TradingView heatmap/quotes/top-stories 嵌入 | 美股/加密/外汇切换 chips（带"当前开市"圆点） |
| **市场会话时钟** | `lib/market-session.ts` · `landing/MarketClock` | NYSE pre/open/after/closed 四态 + "距收盘 Xh Ym" |
| **价格闪烁** | `PriceFlash.tsx` | tick 变化时内容上浮绿/下浮红 900ms |
| **实时脉冲瓦片** | `IndexPulse` · `PulseTile` · `useLiveQuotes` | 大盘指数实时跳动瓦片 |
| 告警 | `CreateAlertModal` · `alert.actions` | 仅"涨破/跌破某价"→ 邮件；每 5 分钟查、90 天过期（Cloud 付费功能） |
| 入场问卷 | `forms/*` · `profile/*` | 国家/目标/风险/行业 |
| 自动化邮件 | `lib/inngest/*` · `nodemailer/*` | AI 欢迎邮件（Gemini）、周报（Kit 广播） |
| 社媒情绪 | `StockSentimentCard` · `adanos.actions` | Reddit / X / 新闻 / Polymarket 情绪 |
| UI 细节 | `DataFreshness`(诚实"Live·15s") · `sonner` toast · 暗色默认 | 无障碍 tab、实时标签 |

---

## 3. 逐项对照牛来面板（已覆盖 / 缺失 / 不适用）

牛来面板现状（来自历史任务 #95–#98、虚拟盘 T+1 修复）：行情、选股、策略体检、虚拟盘、监控中心、后台管理、市场情绪周期、新闻、自选股、榜单、搜索、新手页。

| OpenStock 功能 | 牛来现状 | 结论 |
|---|---|---|
| 自选股 | ✅ 已有（自选股 tab + goStock 入口） | **已覆盖** |
| 个股详情（图/指标/信号） | ✅ 更强（自绘 K 线 + MACD/RSI/KDJ 副图 + 量化策略信号，非 TradingView 外链） | **已覆盖且更优** |
| 市场总览 | ✅ 市场情绪周期（#95）覆盖"市场 mood" | **已覆盖** |
| 告警 | ✅ 监控中心（#96）：阈值 + 钉钉/邮件**外部推送**，比它强 | **已覆盖且更优** |
| 搜索 | ✅ 已有（搜索框 → openStock） | **已覆盖** |
| 新闻 | ✅ newsfeed 模块已纳管（#97） | **已覆盖** |
| **命令面板 Cmd+K** | ❌ 无（面板已 10 个 tab，靠鼠标点） | **缺失 → 高价值** |
| **交易时段时钟** | ❌ 无（只有静态情绪周期，没有"距收盘 X"） | **缺失 → 中高价值** |
| **价格闪烁** | ⚠️ 局部有实时价，无 tick 闪烁动效 | **缺失 → 中低价值** |
| 板块热力图 | ⚠️ 情绪周期是 mood，不是板块热力图 | 可选（需 A 股板块数据） |
| 实时脉冲瓦片 | ⚠️ 无大盘跳动瓦片 | 可选（中低） |
| 社媒情绪(Reddit/X/Polymarket) | — | **不适用（美股专属）跳过** |
| 入场问卷 | — | **YAGNI（个人工具跳过）** |
| 多市场切换(美股/加密/外汇) | — | 港股/美股适配是独立 pending 任务 #94，暂跳过 |
| 邮件周报 / Inngest 基建 | ✅ 监控中心已做推送 | **跳过（不引整套事件/AI 基建）** |

---

## 4. 可迁移功能排序（ponytail 视角）

### 🥇 ① 全局命令面板（Cmd/Ctrl+K）—— 性价比最高，强烈建议搬
- **它做了什么**：全局挂载一个 `CommandDialog`，`Cmd/Ctrl+K` 切换；空闲列热门、输入做 300ms 防抖搜索；选股跳个股页。任何按钮可调 `openSearch()` 事件唤起。
- **为什么值得**：牛来现在 10 个 tab + 多个入口，全靠鼠标。键盘直达 tab/个股是**纯前端、零后端改动、低风险**的体验跃升。
- **落地方式**：在我们 `index.html` 里加一个 `<dialog>` + 输入框；候选项 = 静态 tab 列表（行情/选股/策略体检/虚拟盘/监控/后台/情绪/新闻/自选/榜单）∪ 实时股票搜索（复用现有搜索接口或 `goStock`）。不引 `cmdk`，几十行原生 JS 足够。
- **成本**：小（1 个函数 + 1 段 CSS）｜**价值**：高

### 🥈 ② A 股交易时段时钟 + "距收盘 X" 指示器 —— 中高价值
- **它做了什么**：`sessionOf(weekday, minutes)` 把一天切成 pre/open/after/closed，算"距收盘/开盘还有多久"。
- **为什么值得（需改写）**：它的时钟是 NYSE 的，但**切分 + 倒计时**的模式直接复用。换成 A 股规则：集合竞价 9:15–9:25、早盘 9:30–11:30、午休、午盘 13:00–14:57、尾盘集合 14:57–15:00、盘后。顶部状态条显示"交易中 · 距午休 23m"或"已收盘 · 距明日开盘 14h"。
- **成本**：小（一个纯函数 + 一个展示位）｜**价值**：中高

### 🥉 ③ 价格闪烁（PriceFlash）—— 中低价值，顺手做
- **它做了什么**：`value` 变化时给内容加 `flash-up`/`flash-down` 类 900ms，绿涨红跌（A 股配色，正好契合我们红涨绿跌约定）。
- **为什么值得**：实时行情/自选股报价 tick 时给个视觉反馈，成本低、观感好。
- **成本**：极小（一个 span + 两段 CSS keyframes）｜**价值**：中低

### ④ （可选）板块热力图 / 实时脉冲瓦片 —— 中，数据依赖
- 它的热力图是 TradingView 美股 widget，不能直接用；要做 A 股板块热力图得自己接板块数据。情绪周期已覆盖"市场 mood"，边际收益一般。**建议暂缓**，除非你要的是"行业涨跌热力"而非"情绪"。

### 明确跳过（不搬）
- 社媒情绪（Reddit/X/Polymarket）—— 美股专属，A 股无对应源
- 入场问卷（国家/目标/风险/行业）—— 个人工具 YAGNI
- 多市场切换 chips —— 等 #94 港股/美股适配再统一设计
- 邮件周报 + Inngest 事件基建 —— 监控中心已做外部推送，不引整套后台任务/AI 管线

---

## 5. 落地建议（ponytail：先搬最值的，别一口气全做）

1. **先做 ① 命令面板**——零后端、纯前端、覆盖面最广，今天就能上。
2. **顺带做 ② 交易时段时钟**——和 ① 同属"顶部/全局 UX"，一次函数复用模式，性价比高。
3. **③ 价格闪烁**作为 ① 之后的点缀，挑自选股报价处试点。
4. ④ 及以后：等 ①–③ 上线、你确认手感后再说。

> 不主动实现 ④ 及跳过项；需要"行业板块热力图"这种大功能时再说。

---

## 附：蒸馏依据（已读源码）
- `SearchCommand.tsx`（命令面板）、`lib/market-session.ts`（会话时钟）、`MarketSwitcher.tsx`（市场切换）、`stocks/[symbol]/page.tsx`（个股页组合）、`PriceFlash.tsx`（价格闪烁）、`CreateAlertModal.tsx`（告警 UI）、`package.json`（依赖）、完整文件树（≈230 文件）
