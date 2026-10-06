<div align="center">

<img src="docs/logo.webp" width="180" alt="牛来选股面板 Logo" />

# 🐂 牛来选股面板 · Niucap

**本地自托管的 A 股量化「选股 + 择时 + 复盘」一体化面板**

*29 个选股策略 · 实时行情 · 连板梯队 · 题材雷达 · 龙虎榜后验 · 双源快讯 · 策略回测 · 虚拟盘 · 选股复盘闭环 · 命令面板*

<br>

<a href="https://ab1dde4ffb5e275c5.app.workbuddy.host/" target="_blank"><img src="https://img.shields.io/badge/%E2%9A%A1%20%E5%9C%A8%E7%BA%BF%E4%BD%93%E9%AA%8C-%E6%89%93%E5%BC%80%E6%BC%94%E7%A4%BA%E7%AB%99-FF6B35?style=for-the-badge&labelColor=2D2D2D" alt="在线体验 · 打开演示站"></a>

<sub>免安装 · 打开即用 · 演示环境数据仅供预览</sub>

<br>

[![Gitee 仓库](https://img.shields.io/badge/Gitee-jangviktor%2Fniucap-C71D23.svg)](https://gitee.com/jangviktor/niucap)
![GitHub Stars](https://img.shields.io/github/stars/jangviktor-web/niucap?style=social)
![GitHub last commit](https://img.shields.io/github/last-commit/jangviktor-web/niucap)
![Repo size](https://img.shields.io/github/repo-size/jangviktor-web/niucap)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com/)
[![No API Key](https://img.shields.io/badge/API%20Key-不需要-success.svg)](#-5-分钟跑起来)
[![Self-hosted](https://img.shields.io/badge/部署-本机-%7C-云端-%7C-Docker-orange.svg)](#-5-分钟跑起来)
[![Mobile](https://img.shields.io/badge/移动端-响应式-9cf.svg)](#-5-分钟跑起来)

</div>

<br>

<div align="center">

<table><tr>
<td align="center" width="25%">
<b>⚡ 在线体验</b><br>
<sub>免安装，打开就用</sub><br><br>
<a href="https://ab1dde4ffb5e275c5.app.workbuddy.host/" target="_blank"><b>立即体验 →</b></a>
</td>
<td align="center" width="25%">
<b>🚀 5 分钟跑起来</b><br>
<sub>本机 Python / Docker 任选</sub><br><br>
<a href="#-5-分钟跑起来"><b>看步骤 →</b></a>
</td>
<td align="center" width="25%">
<b>☁️ 不想运维？云平台一键发布</b><br>
<sub>零服务器，出分享链接</sub><br><br>
<a href="#-部署教程一键发布到-workbuddy-云平台"><b>看教程 →</b></a>
</td>
<td align="center" width="25%">
<b>🎯 先看它能干什么</b><br>
<sub>16 张实机截图 + 功能说明</sub><br><br>
<a href="#-页面预览"><b>看预览 →</b></a>
</td>
</tr></table>

</div>

<br>

![市场榜单](docs/screenshots/01-dashboard.webp)

<div align="center">

> 仓库名 `niucap`（牛 = capture 牛股，A 股梗）；产品中文名「牛来选股面板」。两者指同一个项目。
>
> **A self-hosted A-share quant dashboard** — screening, limit-up ladder, sector radar, backtesting & paper trading. **No API key. 100% local.**

</div>

---

## 🎯 为什么用它

<div class="fgrid">

<div class="fcard">
<h4>🔑 零 API Key，一条命令起</h4>
<p>行情全部走腾讯 / 新浪 / 同花顺 / 东财公开接口，情绪分析是本地词典引擎。不注册、不付费、不限流，克隆下来跑起来就能用。</p>
</div>

<div class="fcard">
<h4>⚡ 秒级全市场扫描</h4>
<p>29 个内置策略 + 六类分组（趋势形态 / 量价涨停 / 反转波动 / 形态相似度 / 经典指标 / 分钟级），并集交集自由组合，全市场扫描稳定在秒级。</p>
</div>

<div class="fcard">
<h4>🔒 数据在自己手里</h4>
<p>SQLite 本地库或云端 MySQL 协议库自适应，日线数据落盘自己掌控。不是把隐私交出去的网页版。</p>
</div>

<div class="fcard">
<h4>🧠 选完还能复盘</h4>
<p>每次选股自动存档，事后可回看<b>窗口胜率</b>（模拟 09:30–09:50 买入当日收盘结算）、<b>入选后表现</b>、批量沉淀自选——不靠记性判断策略好坏。</p>
</div>

</div>

---

## 📑 目录

<details open>
<summary><b>展开 / 收起目录</b></summary>

- [🎯 为什么用它](#-为什么用它)
- [✨ 功能总览](#-功能总览)
- [🖼 页面预览](#-页面预览)
- [🧪 选股复盘闭环（#103~#105 新增）](#-选股复盘闭环103105-新增)
- [🚀 5 分钟跑起来](#-5-分钟跑起来)
- [💡 快速上手示例](#-快速上手示例)
- [🧱 技术栈](#-技术栈)
- [📘 部署教程：一键发布到 WorkBuddy 云平台](#-部署教程一键发布到-workbuddy-云平台)
- [⚠️ 首次部署必读：数据初始化](#-首次部署必读数据初始化)
- [⚙️ 配置（.env，可选）](#-配置env可选)
- [📁 目录结构](#-目录结构)
- [🤝 贡献方式](#-贡献方式)
- [🗺️ 路线图](#-路线图)
- [❓ 常见问题](#-常见问题)
- [📜 许可证](#-许可证)

</details>

---

## ✨ 功能总览

| 模块 | 说明 |
|---|---|
| 🌟 **小白选股** | 完全不懂 K 线也能用：四套方案（稳健白马 / 超跌反弹 / 成长活跃 / 打板热点）一键出结果，每只票给 0~100 友好度评分 + 大白话逐条解释「为什么选它」 |
| 🔍 **策略选股** | 29 个内置策略（趋势形态 / 量价涨停 / 反转波动 / 形态相似度 / 经典指标 / 分钟级），支持并集 / 交集组合，秒级扫描全市场 |
| 🎯 **条件选股** | PE / PB / 市值 / 涨跌幅 / 换手率 / 成交额自由组合，内置四套预设；另有分钟级全市场实时扫描 |
| 📈 **个股分析** | 日/周/月 K 线叠加 MA + 布林 / 唐安奇 / 肯特纳 / 吊灯止损通道；MACD / RSI / KDJ / 成交量；60+ 项技术指标与自动信号；关键价位；资金流向 / 筹码分布 / 基本面 / 研报公告 |
| 🔥 **连板梯队 + 情绪周期** | 涨停子集取近 12 日 K 线算连板天数（交易日历对齐），按首板 / 2 板 / 3 板 / 4+ 板分组；融合 6 阶段市场情绪（冰点→启动→主升→高潮→退潮→修复） |
| 🎯 **题材雷达** | 行业板块四维评分（强度 / 资金 / 宽度 / 持续）+ 名称归一融合去重，一眼锁定当日最强主线 |
| 🧪 **策略回测** | T+1 / 手续费 / 印花税 / 滑点真实约束，止损止盈、金字塔分批加仓；净值曲线、年化、最大回撤、夏普、卡玛、胜率、盈亏比；网格回测与走查（防过拟合） |
| 📊 **市场榜单** | 涨幅 / 跌幅 / 成交额 / 换手率 / 市值 / 低估值六大榜单 + 市场温度 13 维画像 + 市场宽度（涨跌家数、站上 20 日线比例、新高新低） |
| 🐯 **龙虎榜后验** | 上榜原因 + D+1 / D+2 / D+5 / D+10 复权涨幅，把名单变成绩单 |
| 📰 **双源快讯** | 新浪 7×24 + 同花顺双源互备，30s 缓存，重大红条标记，词典情绪自动打「看涨 / 看跌 / 中性」，关联股票可点跳个股页 |
| ⚖️ **多股对比** | 最多 8 只同屏对比价格 / RSI / MACD / KDJ / 量比 / 估值，自动标注组内最优 / 最差 |
| ⭐ **我的自选** | 自选股分组、成本线、批量盯盘 |
| 🧪 **选股历史存档** | 每次选股自动存档，事后回看：窗口胜率回测（09:30–09:50 买入当日收盘结算）、入选后表现、批量加自选 |
| 💰 **虚拟盘** | 费率可配，盈亏跟踪，T+1 / 费用 / 滑点约束；收盘后自动禁止交易（#103） |
| 📅 **本地交易日历** | 内置 A 股节假日表，休市/补班自动识别，连板天数与选股按交易日对齐（#104）；另与同花顺权威日历**自动对账**，本地判定有偏差会在自检里报警（#107） |
| 🌅 **盘前竞价** | 竞价日（9:15–9:25）展示**我的自选集合竞价**涨幅 / 量比 / 占昨量，以及**短线风向标**全市场竞价基准 + 概念归因标签；休市日自动回落到上一交易日终态并明确标注日期（#106） |
| 💎 **估值五口径** | 个股页并排展示 **PE(TTM) / PE(MRQ) / PB(MRQ) / PS(TTM) / PCF(TTM)**，看清「静态贵还是动态贵」；ETF / 指数自动跳过不显示（#108） |
| 🏦 **ETF 筛选** | 宽基 / 行业 / 主题 ETF 多维筛选 |
| 🔔 **监控中心** | 价格 / 涨跌幅 / 自选异动规则与告警 |
| ⌨️ **命令面板** | `Cmd/Ctrl + K` 全局直达任意模块与股票，自带沪深交易时段时钟 |
| 🤖 **AI 直连（MCP）** | 内置 MCP 服务端（纯标准库、零新增依赖），Claude Desktop / Cursor 等 AI 工具可直接查行情、跑 29 策略选股、算 61 项指标、看连板梯队与快讯，还能把结果加进自选；[配置教程 →](docs/MCP-使用说明.md) |
| 🚀 **一键部署** | Python 本机 / WorkBuddy 云平台 / Docker 自托管，任选 |

---

## 🖼 页面预览

> 实机截图（1440×900）双列排布，**点击图片放大**看细节。

<table>
<tr>
<td width="50%" valign="top"><b>🌟 小白选股</b> —— 不填条件一键选股<br><sub>四种风格对应四套方案（盾牌稳健 / 折线超跌 / 上升成长 / 火焰打板），逐条给理由与友好度评分，一键跳个股详情</sub><br><img src="docs/screenshots/02-newbie.webp" width="100%"/></td>
<td width="50%" valign="top"><b>🔍 策略选股</b> —— 29 个策略自由组合<br><sub>按趋势形态 / 量价涨停 / 反转波动 / 形态相似度 / 经典指标 / 分钟级六类组织，点击即选、并集交集、秒级扫全市场</sub><br><img src="docs/screenshots/03-strategy.webp" width="100%"/></td>
</tr>
<tr>
<td width="50%" valign="top"><b>🎯 条件选股</b> —— 像填表一样选股<br><sub>PE / PB / 市值 / 涨跌幅 / 换手率 / 成交额自由组合，内置「低估值蓝筹」「成长活跃」「小市值活跃」「深度价值」四套预设</sub><br><img src="docs/screenshots/04-screen.webp" width="100%"/></td>
<td width="50%" valign="top"><b>📊 市场榜单</b> —— 六大榜单 + 温度画像<br><sub>涨幅 / 跌幅 / 成交额 / 换手率 / 市值 / 低估值，叠加市场温度 13 维画像与涨跌分布</sub><br><img src="docs/screenshots/01-dashboard.webp" width="100%"/></td>
</tr>
<tr>
<td width="50%" valign="top"><b>📰 双源快讯</b> —— 新浪 7×24 + 同花顺<br><sub>双源互备去重，重大消息红条高亮；词典情绪引擎自动打「看涨 / 看跌 / 中性」标签，关联股票可点直达</sub><br><img src="docs/screenshots/05-news.webp" width="100%"/></td>
<td width="50%" valign="top"><b>🧪 策略回测</b> —— 真实约束下的成绩单<br><sub>T+1、手续费、印花税、滑点全模拟；支持止损止盈与金字塔加仓；网格回测与走查回测防过拟合</sub><br><img src="docs/screenshots/06-backtest.webp" width="100%"/></td>
</tr>
<tr>
<td width="50%" valign="top"><b>📈 个股分析</b> —— K 线 + 60+ 项指标<br><sub>日 / 周 / 月 + 1~60 分钟 K 线，四种通道叠加，技术面结论卡片；资金流向 / 筹码分布 / 基本面 / 研报公告</sub><br><img src="docs/screenshots/08-stock.webp" width="100%"/></td>
<td width="50%" valign="top"><b>🔥 连板梯队</b> —— 梯队分组 + 情绪周期<br><sub>近 12 日 K 线算连板天数，首板 / 2 板 / 3 板 / 4+ 板分组；融合 6 阶段市场情绪（冰点→启动→主升→高潮→退潮→修复）</sub><br><img src="docs/screenshots/09-limitup.webp" width="100%"/></td>
</tr>
<tr>
<td width="50%" valign="top"><b>🎯 题材雷达</b> —— 板块四维评分<br><sub>行业板块融合去重 + 实时主线排行，热点一目了然</sub><br><img src="docs/screenshots/10-theme.webp" width="100%"/></td>
<td width="50%" valign="top"><b>ℹ️ 关于</b> —— 模块构成实时统计<br><sub>指标数 / 策略数由后端实时统计，新增即自动同步；附技术实现、数据源与免责声明</sub><br><img src="docs/screenshots/07-about.webp" width="100%"/></td>
</tr>
</table>

---
## 🧪 选股复盘闭环（#103~#105 新增）

这一组功能解决一个老问题：**选完股就扔了，好不好用、涨没涨，全凭记性。** 现在每次在【小白选股】【策略选股】【条件选股】跑完，结果都会**自动存档**到「📜 历史选股」页面，事后可回看胜率、算清收益、批量沉淀到自选。

<table>
<tr>
<td width="50%" valign="top"><b>🧭 分组导航</b> —— 17 个入口不再平铺<br><sub>按「📊 看盘 / 🔍 选股 / 📈 分析 / ⭐ 我的」4 组归位，点左上角「☰ 菜单」展开抽屉（桌面 / 手机一致），顶部指标卡不受影响</sub><br><img src="docs/screenshots/11-nav.webp" width="100%"/></td>
<td width="50%" valign="top"><b>📜 历史选股</b> —— 每次选股自动存档<br><sub>三个选股页每执行一次自动落库一条（含条件与个股列表）；顶部按「全部 / 小白 / 策略 / 条件」筛选；标题中文化、按账户隔离，只看到自己的记录</sub><br><img src="docs/screenshots/12-history.webp" width="100%"/></td>
</tr>
<tr>
<td width="50%" valign="top"><b>🎯 看胜率</b> —— 这套策略到底灵不灵<br><sub>对入选股模拟「次日 09:30–09:50 VWAP 买入、当天收盘卖出」，给出涨停率 / 胜率 / 平均涨幅 / 累计盈亏（每只 1 万元本金折算）；弹窗内可调整金额与回看天数</sub><br><img src="docs/screenshots/13-winrate.webp" width="100%"/></td>
<td width="50%" valign="top"><b>📈 看表现</b> —— 选完到现在涨了还是跌了<br><sub>以入选价（缺失时取入选日收盘兜底）对比最新价：平均涨跌幅 / 上涨胜率 / 最佳最差 / 累计盈亏，逐只列出入选价 / 最新价 / 涨跌幅</sub><br><img src="docs/screenshots/14-performance.webp" width="100%"/></td>
</tr>
<tr>
<td width="50%" valign="top"><b>⭐ 批量加自选</b> —— 一键沉淀带分组<br><sub>弹窗里选已有分组或输入新分组名（留空不新建），整批按代码加入、已存在自动跳过；配合自选分组按策略 / 行业 / 观察池整理</sub><br><img src="docs/screenshots/15-batch-watch.webp" width="100%"/></td>
<td width="50%" valign="top"><b>☑️ 批量操作栏</b> —— 结果页直接勾选<br><sub>三选股页结果表每行复选框 + 表头批量栏：全选、已选 N 只计数、看胜率、加自选；不勾选默认对当前全部结果操作</sub><br><img src="docs/screenshots/16-batch-bar.webp" width="100%"/></td>
</tr>
</table>

> 📌 **口径与细节**：胜率回测买入价 = 09:30–09:50 分钟成交额 ÷ 成交量（真实 VWAP）；涨停按前收 ×(1+涨停幅度)（主板 10% / 创业板·科创 20% / 北交所 30%，ST 5%）；回测为纯历史统计，与虚拟盘资金完全隔离。看表现基准为入选时价格，**休市日入选因无新价格涨跌显示 0，属正常**。

### 📅 本地交易日历（#104）

内置 A 股节假日表（2026 年依据国办明电，2027 年为预测值），**休市 / 补班自动识别**。影响两处：顶栏交易时段时钟显示「休市 · 节假日」；**连板天数、选股、胜率回测按交易日对齐**，不会把周末误算成交易日。

> 周末一律休市，不看调休补班表——补班是给「上班」用的，不是给「A股开盘」用的。

### 🌅 盘前竞价 + 估值五口径 + 权威日历对账（#106~#108 新增）

这一组走的是同花顺（hithink-finance）公开 API，**属于选填增强**：不配 Key 也能正常跑，只是这三项静默隐藏。

<table>
<tr>
<td width="50%" valign="top"><b>🌅 盘前竞价（#106）</b><br><sub>竞价时段展示「我的自选 · 集合竞价」：涨幅 / 量比 / 占昨量，未匹配个股单独标出；下半屏是「短线风向标 · 竞价基准」全市场竞价涨跌幅 + 概念归因标签（如「住宅开发」「租售同权」）。休市日自动回落到上一交易日终态，并在卡片上明确标注「休市 · 展示 YYYY-MM-DD 竞价终态」，不会让你误以为是今天的实时数据</sub></td>
<td width="50%" valign="top"><b>💎 估值五口径（#108）</b><br><sub>个股页头部并排展示 PE(TTM) / PE(MRQ) / PB(MRQ) / PS(TTM) / PCF(TTM)。TTM 是滚动 12 个月、MRQ 是最近一季——两个 PE 背离往往说明盈利正在变化，只看单一 PE 容易误判。ETF 与指数不适用，自动跳过不显示</sub></td>
</tr>
<tr>
<td width="50%" valign="top"><b>🗓 权威日历对账（#107）</b><br><sub>拿同花顺返回的近一年权威交易日序列（实测 241 条）与本地节假日表逐日对账，窗口内有任何一天判定不一致就在自检里报警并列出差异日期。这套对账真的抓到过 bug：本地曾把 5 个「周末补班日」误判成交易日，会让虚拟盘按上周五收盘价成交</sub></td>
<td width="50%" valign="top"><b>🔁 行情多源互备（备源加固）</b><br><sub>快照降级链 <code>eltdx → 腾讯 → 同花顺</code>，日线降级链 <code>腾讯 → 新浪 → 同花顺</code>（同花顺仅支持日线）。实测茅台 250 个交易日收盘价与本地主源<b>偏差 0.0000%</b>（同为前复权口径），可安全当兜底，不会在主源故障时让回测结果漂移。坏代码会被逐只重试隔离，不会拖垮整批请求</sub></td>
</tr>
</table>

> 📌 **三道降级装甲**：① 无 Key 立即返回、不排队；② 全部网络调用包在 try 里，失败只影响这一项；③ 结果缓存 6 小时，对账一天最多跑一次。所以即使同花顺挂了或没配 Key，这三项也只是不显示，**不会拖慢也不会搞崩面板**。

### 🛡️ 虚拟盘收盘禁交易（#103）

虚拟盘的买卖现在受**交易时段守卫**约束：仅在**连续竞价 + 集合竞价**时段允许下单，**收盘 / 休市后自动拒绝**（返回明确提示）。防止误在非交易时间操作。

---
## 🚀 5 分钟跑起来

> 💡 不想动手装？先玩 [**在线体验版**](https://ab1dde4ffb5e275c5.app.workbuddy.host/)，效果满意再回来自建。

### 方式 A：Python 本机（推荐给开发者）

```bash
# Gitee（主仓库，国内更快）
git clone https://gitee.com/jangviktor/niucap.git
# 或 GitHub（双端同步；仓库名 NiuCap，指定目录名保持一致）
git clone https://github.com/jangviktor-web/NiuCap.git niucap
cd niucap
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server/run.py          # 或 ./deploy.sh（后台常驻 + 可选 Cloudflare 隧道）
# 打开 http://localhost:8899
```

> 需要 Python ≥ 3.11。无需任何 API Key，纯免费公开行情源（腾讯 / 新浪 / 同花顺 / 东财）。

### 方式 B：WorkBuddy 一键发布到云平台（推荐零运维）

本项目已满足云发布要求，在 WorkBuddy 里对仓库说「发布为应用」即可生成可访问的分享链接，无需自己买服务器。完整步骤见下方 **[📘 部署教程](#-部署教程一键发布到-workbuddy-云平台)**。

### 方式 C：Docker 自托管（数据常驻）

仓库未内置 Dockerfile；如需容器化，按 `server/run.py` 暴露的 `8899` 端口自行封装即可，数据卷挂到 `./data`。

---

## 💡 快速上手示例

**① 纯小白，只想看「现在哪些票值得关注」**

打开「🌟 小白选股」→ 选「稳健白马」或「打板热点」→ 一键出结果，每只票附 0~100 友好度评分与大白话理由。

**② 看盘中情绪：连板梯队 + 题材雷达**

打开「🔥 连板梯队」看最高板高度与连板率；再开「🎯 题材雷达」看当日最强主线。标题栏徽标会标明数据是 **盘中实时** 还是 **收盘数据**。

**③ 用命令行 / 程序直接取数（FastAPI 后端）**

```bash
# 连板梯队 + 情绪周期
curl "http://localhost:8899/api/limitup?limit=200"

# 题材雷达（评分降序、融合去重）
curl "http://localhost:8899/api/theme?limit=40"
```

返回示例（节选）：

```json
{
  "date": "2026-09-28",
  "涨停": [ { "name": "新华传媒", "code": "600825", "change_pct": 10.04, "board_days": 2 } ],
  "ladder": { "首板": 17, "2连板": 2, "3连板": 2, "4+连板": 1 },
  "rate": 0.227,
  "max_height": 5,
  "phase": "ignite"
}
```

**④ 键盘流：`Cmd/Ctrl + K`** 呼出命令面板，输入股票代码或模块名直达，无需鼠标点导航。

---

## 🧱 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 后端 | Python 3.11 · FastAPI · uvicorn | 异步 API，单端口服务，读 `$PORT` 自适应云平台 |
| 数据 | pandas · numpy | 向量化选股 / 回测计算 |
| 存储 | SQLite（默认） / MySQL 协议库（云端常驻） | `store.py` 自适应切换 |
| 前端 | 原生 HTML / CSS / JS · ECharts | 单页应用，**零构建步骤**，开箱即用 |
| 行情源 | 腾讯 / 新浪 / 同花顺 / 东财 公开接口 | 快照与日线**多源互备**：`eltdx` → 腾讯 → 新浪 → 同花顺，前面的源全挂时自动降级 |
| 部署 | 本机 · WorkBuddy 云 · Docker | 一条命令起 |

---

## 📘 部署教程：一键发布到 WorkBuddy 云平台

本面板无需自己买服务器，直接在 WorkBuddy 里一键发布为在线应用，生成形如 `https://ab1dde4ffb5e275c5.app.workbuddy.host/` 的分享链接——把链接发给朋友，浏览器打开即用。当前线上示例（已验证可达，HTTP 200）即这一流程的产物。

### 为什么能直接发布

项目已满足 WorkBuddy 云平台的发布要求，发布脚本（`publish.js`）可自动完成「探测运行时 → 装依赖 → 起服务 → 就绪校验 → 出链接」：

| 平台要求 | 本项目实际情况 |
|---|---|
| 单 HTTP 端口 | `server/run.py` 用 FastAPI + uvicorn 起单端口服务 |
| 读取 `$PORT` | `resolve_port()` 优先读 `os.environ["PORT"]`，默认 `8899` |
| 绑定 `0.0.0.0` | uvicorn `host="0.0.0.0"`，网关可访问 |
| 健康检查 | `GET /api/health` 供就绪探测 |
| 声明依赖 | `requirements.txt`（轻量，无 akshare / mootdx） |

### 发布步骤

1. **打开项目**：在 WorkBuddy 中打开本仓库（已 `git clone` 或导入 `niucap`）。
2. **发起发布**：对助手说「发布为应用」或「publish」。助手会执行发布脚本，等价于：
   ```bash
   node <skill-dir>/scripts/publish.js \
     --dir /workspace/tick-stock-panel \
     --language python \
     --install-cmd "pip install -r requirements.txt" \
     --start-cmd "python server/run.py"
   ```
   > 脚本会注入 `PORT` 环境变量，`server/run.py` 自动监听该端口；`--start-cmd` 不写死端口，交给 `$PORT`。
3. **拿到链接**：脚本输出 JSON，形如
   ```json
   { "shareLink": "https://ab1dde4ffb5e275c5.app.workbuddy.host/", "verified": true }
   ```
   把 `shareLink` **整条**（若带 `?sharecode=...` 也要一并保留）发给对方。**不要删掉 `?sharecode=`**——它是访问凭据，缺了对方打开会看到「链接不完整」。
4. **访问**：浏览器打开链接，页面即「牛来选股面板」。首次打开是空库，数据初始化见下方说明。

### 重复发布 / 更新线上

- **同一项目重复发布，保持同一链接**——链接背后的内容会被覆盖，且对所有已拿到链接的人立即生效。
- 本地改完代码后，再对助手说「发布为应用」即可把新版本同步上线；建议先在本地预览确认，避免直接覆盖别人正在看的页面。
- 若 `verified` 为 `false`，稍等几秒再刷新链接（平台就绪有短暂延迟）。

### 下线

对助手说「取消发布 / 下线 / unpublish」，脚本会取消发布并使分享链接失效（破坏性操作，需你明确授权）。

### 云平台注意事项

- **数据临时**：云实例存储通常是临时的，重启 / 重新发布会清空本地 SQLite，需重新同步一次（后台管理 → 运行参数 → 开始同步）。
- 想要**数据常驻、多人共享**，用下方「首次部署必读」的云端 MySQL 协议库方案（填 `TICK_DB_HOST` 等，重启不丢数据）。
- 无需任何 API Key，行情来自腾讯 / 新浪 / 同花顺 / 东财公开接口。

---

## ⚠️ 首次部署必读：数据初始化

为控制仓库体积，**行情数据库（约 270 万行日线）不进 Git**。首次启动服务会**自动建一个空库**——此时页面能打开，但选股 / K 线 / 榜单是空的。两种灌数据方式：

- **本地 SQLite（最简单）**：进入「后台管理 → 运行参数 → 开始同步」，全市场日线约十几秒（走 eltdx 批量源）补齐。
  - 注意：云平台存储通常是临时的，重启会清空，需重新同步一次。
- **云端 MySQL 协议库（数据常驻、多人共享）**：复制 `.env.example` 为 `.env`，填好 `TICK_DB_HOST` 等 6 个变量指向你的 TiDB Cloud / MySQL，重启即自动切换。重启不丢数据，朋友也能直接访问。

---

## ⚙️ 配置（`.env`，可选）

复制 `.env.example` 为 `.env` 按需填写：

- **数据库**：`TICK_DB_HOST` 等 —— 留空用本地 SQLite，填了切云端 MySQL 协议库。
- **每日自动同步**：`TICK_SYNC_BARS_AUTO=1` + `TICK_SYNC_BARS_AT=15:30` + `TICK_SYNC_BARS_COUNT=250` + `TICK_SYNC_BARS_SCOPE=all`。
  - 内置**同步守门三重判断**：工作日 15:00 前不跑全量 / 当日已同步标记 / 抽检覆盖率 ≥90% 跳过，避免重复劳动与误触发。
- **虚拟盘费率**：`TICK_FEE_RATE` / `TICK_STAMP_RATE` 等（仅新用户开户默认值）。
- **管理员**：`TICK_ADMIN_USERS=用户名1,用户名2` —— 不设则后台管理页整体关闭。

> `.env` 含数据库密码，**已被 `.gitignore` 排除，切勿提交**。

---

## 📁 目录结构

```
server/         FastAPI 后端：选股引擎 / 行情 / 龙虎榜 / 快讯 / 情绪 / 连板 / 题材雷达 / 虚拟盘
  app.py          主应用（142 个路由，含 /api/limitup /api/theme）
  run.py          启动器（读 $PORT，绑 0.0.0.0）
  newsfeed.py     双源快讯（新浪7x24 + 同花顺，去重 + TTL）
  hithink.py      同花顺特色数据：盘前竞价 / 权威日历 / 估值五口径 / 行情备源（#106~#108，含 46 项离线自检）
  sentiment.py    词典情绪（正/负词 + 否定反转 + 程度乘数 + 转折）
  limitup.py      连板梯队 + 6 阶段情绪周期（#100）
  theme_radar.py  题材雷达：四维评分 + 融合去重（#100）
  windowsim.py    窗口胜率回测 / 入选后表现（#105）
  holidays.py     本地 A 股节假日表（#104）
  store.py        数据访问层（SQLite / MySQL 协议库自适应）
web/            前端单页（原生 HTML/JS + ECharts，无构建步骤）
  index.html      全站单文件应用
docs/           部署指南与各功能说明（中文）+ screenshots/ 页面截图
tests/          自检与端到端测试（Playwright 双视口）
deploy.sh       一键启动 / 隧道 / 停止
requirements.txt 运行时依赖（轻量，无需 akshare/mootdx）
```

---

## 🤝 贡献方式

欢迎 Issue、PR 与建议！本项目以 **MIT** 开源，适合量化爱好者共同打磨。

- **报告 Bug / 提需求**：开 [Issue（Gitee）](https://gitee.com/jangviktor/niucap/issues) 或 [GitHub Issue](https://github.com/jangviktor-web/NiuCap/issues)，请尽量附上复现步骤、浏览器/系统、报错截图或日志（`server.log`）。
- **提交代码**：
  1. `fork` 本仓库并基于 `master` 切出特性分支（`feat/xxx` / `fix/xxx`）。
  2. 保持提交小而聚焦，提交信息建议带前缀：`feat(#号)` / `fix(#号)` / `docs` / `test` / `chore`。
  3. 前端改动请顺手跑 `tests/` 下相关自检（或说明手测结论）；新增功能不建议破坏现有模块（本项目遵循「纯增量、零回归」原则）。
  4. 发起 Pull Request，描述「改了什么 / 为什么 / 如何验证」。
- **数据源与许可**：核心行情依赖 `eltdx` 为**研究 / 学习专用许可（禁止商业使用）**；商用前请替换为其他合规数据源，并保持 `requirements.txt` 轻量。
- **代码风格**：后端 Python（PEP8 倾向），前端原生 JS（无打包），优先复用现有 helper，删除优于新增。

---

## 🗺️ 路线图

- [x] 选股复盘闭环：历史存档 + 胜率回测 + 入选后表现 + 批量加自选（#105）
- [x] 导航按「看盘/选股/分析/我的」四组分组（#106）
- [x] 虚拟盘收盘后禁止交易（#103）
- [x] 本地 A 股节假日表（#104）
- [x] 连板梯队 + 情绪周期 + 题材雷达（#100）
- [x] 移动端响应式导航（汉堡抽屉）+ 命令面板 + 交易时钟（#99）
- [x] 前端设计令牌根因修复与无障碍（#101）
- [ ] 形态识别 / 形态相似度可视化增强（#37 / #43）
- [ ] 自选股成本线可视化（#90）
- [ ] 个股页 hover 弹图（#92）
- [ ] AI 个股卡片（需 LLM Key）（#93）
- [ ] 港美股适配（#94）

---

## ❓ 常见问题

**Q：页面能打开，但榜单 / 选股是空的？**
A：空库还没灌数据。看上面「[首次部署必读：数据初始化](#-首次部署必读数据初始化)」，进后台管理点一次「开始同步」即可。

**Q：需要 API Key 吗？收费吗？**
A：不需要，完全免费。行情来自腾讯 / 新浪 / 同花顺 / 东财公开接口，情绪分析为本地词典引擎。选填：同花顺的免费 API Key 配了之后，可在其他源全部故障时作为额外兜底；不配也完全能跑。

**Q：数据会更新吗？**
A：盘中行情缓存约 3 分钟自动刷新；日线支持每日定时自动同步（见 `.env` 配置）。

**Q：能多人同时用吗？**
A：能。配云端 MySQL 协议库（如 TiDB Cloud）后数据常驻，把部署链接或局域网地址分享给朋友即可。

**Q：休市 / 节假日打开，选股结果显示全 0 或涨跌为 0 正常吗？**
A：正常。当地没有新行情可比，入选价≈最新价，涨跌幅即 0。节假日表会让顶栏显示「休市 · 节假日」。

**Q：能用于实盘吗？**
A：本面板仅供量化学习与研究，不构成任何投资建议；虚拟盘可模拟交易，实盘决策风险自负。

---

## 📜 许可证

本项目以 **MIT 许可证**开源，仅供量化学习与研究，**不构成任何投资建议**。

依赖 `eltdx` 为 **研究 / 学习专用许可证（禁止商业使用）**；运行期已做容错（缺失时自动回落腾讯源），个人学习用途无碍，商用请替换为其他数据源。

---

<div align="center">

**用得上就点个 ⭐，有问题开 Issue～**

[![RepoStars](https://repostars.dev/api/embed?repo=jangviktor-web%2FNiuCap&theme=grape)](https://repostars.dev/?repos=jangviktor-web%2FNiuCap&theme=grape)

*仅供量化学习与研究，不构成投资建议。*

</div>

<style>
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:16px 0}
.fcard{background:#f6f8fa;border:1px solid #d0d7de;border-radius:10px;padding:14px 16px;text-align:left}
.fcard h4{margin:0 0 6px;font-size:14.5px;color:#1f2328}
.fcard p{margin:0;font-size:12.8px;line-height:1.65;color:#57606a}
</style>
