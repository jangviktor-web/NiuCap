# Task #99 命令面板(Cmd+K) + 交易时段时钟 —— 排查与方案

> 来源：OpenStock 蒸馏（见 `docs/openstock-蒸馏与功能对比.md`）①、② 两项
> 改动文件：仅 `web/index.html`（纯前端，零后端改动）
> 流程：备份 → 排查 → 方案 → 实现 → 测试 → 确认
> 备份：`backups/cmdpanel-clock-20260928-123454/index.html`

---

## 1. 现状盘点（已读源码确认）

| 点 | 结论 |
|---|---|
| 主 tab | `<nav>.tabs button[data-tab]` 共 18 个（766–784）；`hot`/`moves` 隐藏，`admin` 未登录隐藏 |
| tab 切换 | `syncTabs()` 只切高亮；真正入口是 `b.onclick`（1902–1938）含各页懒加载 `load` |
| 个股跳转 | `openStock(code)`（2272）归一代码 + 切 stock tab + `loadStock`；`goStock` 已委托它 |
| 搜索 | `/api/search?q=` 返回 `{items:[{code,name}]}`，`resolveStock` 已封装 |
| 头部 | `header` 是 `position:sticky;top:0`；`.hd`(flex-wrap) 与 `<nav>` 的 `navright`（自动刷新区） |
| 快捷键 | grep 全局**无** `ctrlKey/metaKey` 监听；仅各输入框 Enter/Esc。**Ctrl/Cmd+K 空闲** |

---

## 2. 风险排查（R1–R10）与解决方案

- **R1 快捷键冲突** → 已 grep 确认全局无 `Ctrl/Cmd+K` 占用，直接在 `document` 级监听即可；面板打开时 `preventDefault()` 阻止浏览器焦点地址栏。
- **R2 焦点打架**：面板输入框是独立元素，`#q` 的 keydown 只在自身聚焦触发，不冲突；但面板内 Esc 要 `stopPropagation`，避免冒泡到 `#q` 的 Escape 关 sug（无害，仍加一道）。
- **R3 tab 列表分叉**：**不写死**，运行时从 `.tabs button[data-tab]` 动态读 `textContent`+`data-tab`，用 `offsetParent===null` 排除隐藏项（hot/moves/admin 未登录）。零维护。
- **R4 选 tab 漏懒加载**：命令面板选 tab 走 `button.click()`（复用 1902 委托，含 `loadAlerts/loadWatch/loadAdmin` 等），不自己调 `syncTabs`，避免漏 load。
- **R5 时钟刷新开销**：`setInterval` 1s 一次，纯函数计算 + 一次 DOM 写入，极轻；不引入 `requestAnimationFrame`/可见性暂停（过度优化）。
- **R6 时区坑**：一律用 `Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai'})` 取北京时间，浏览器/沙箱时区不影响结果。
- **R7 法定节假日**：**忽略**（同 OpenStock 简化），只按周几+时段判断；注释标明 `# ponytail: 忽略法定节假日，加 holiday 表当需精确到休市日`。
- **R8 CSS 破坏布局**：面板用 `position:fixed; inset:0` overlay + 高 `z-index`(>9999 用 10000)，不挤占文档流；时钟加在 `navright`（与自动刷新并排），不改其他元素。
- **R9 点击外部关闭**：overlay 点击（非 box 内）关闭面板；Esc 关闭。
- **R10 搜索结果跳转**：选中股票走 `openStock(code)`（已归一）；`/api/search` 超时/空结果降级为空列表，不崩溃。

---

## 3. 实现方案（最小 diff，只在 index.html）

### 3.1 HTML
1. `navright`（787 前）插入 `<span class="mktclock" id="mktClock" title="沪深交易时段（忽略法定节假日）">—</span>`。
2. `</body>` 前插入命令面板 overlay：
   ```html
   <div id="cmdPalette" class="cmdp" hidden>
     <div class="cmdp-box">
       <input id="cmdInput" class="cmdp-input" placeholder="跳转到页面，或搜股票代码/名称  (↑↓ 选择 · Enter 打开 · Esc 关闭)">
       <div id="cmdList" class="cmdp-list"></div>
     </div>
   </div>
   ```

### 3.2 CSS（就近加在 `<style>` 末尾）
`.cmdp`（overlay）、`.cmdp-box`、`.cmdp-input`、`.cmdp-list`、`.cmdp-item`（hover/active 高亮）、`.cmdp-empty`、`.mktclock`（四态配色：open 红/active、call 橙、lunch 灰、closed 暗）。

### 3.3 JS（新增 `<script>`，放在 7677 初始化区附近）
- `mktSession(now)` 纯函数：北京时间 → `{phase, line}`（规则见 §4）。
- `renderMktClock()` 写 `#mktClock` + `setInterval(…,1000)`。
- 命令面板：`toggleCmd/openCmd/closeCmd`、`buildCmdItems()`（动态 tab + 股票搜索）、防抖 `onCmdInput`、`keydown` 上下选择/Enter/Esc、`selectCmd(item)`（tab→`.click()`，stock→`openStock`）。
- 全局快捷键：`document.addEventListener('keydown', e => (e.ctrlKey||e.metaKey)&&e.key==='k' && (e.preventDefault(),toggleCmd()))`。

---

## 4. A 股交易时段规则（北京时间，忽略节假日）

| 时段 | 时间 | phase | 展示 |
|---|---|---|---|
| 早集合竞价 | 09:15–09:30 | call | 集合竞价 · 9:30 开盘 |
| 早盘连续 | 09:30–11:30 | open | 交易中 · 距午休 Xh Ym |
| 午休 | 11:30–13:00 | lunch | 午休 · 13:00 开盘 |
| 午盘连续 | 13:00–14:57 | open | 交易中 · 距收盘 Xh Ym |
| 尾盘集合 | 14:57–15:00 | call | 尾盘集合 · 15:00 收盘 |
| 盘后 | 15:00–次日09:15 | closed | 已收盘 · 明日 9:15 集合竞价 |
| 周末 | 全天 | closed | 休市 · 下交易日 9:15 |

倒计时按分钟差格式化 `Xh Ym`（<1h 显示 `Ym`）。

---

## 5. 测试计划

新增 `tests/check_cmd_clock.py`（playwright）：
1. **Cmd+K 打开**：按 `Control+k` → `#cmdPalette` 不再 `hidden`，输入框聚焦。
2. **跳页面**：输入"个股" → 候选含"📈 个股分析" → Enter → `.tabs button[data-tab=stock]` 带 `on` 且 `#tab-stock.on`。
3. **搜股票**：输入"600519" → 防抖后候选含搜索结果 → 选中 → `state.stockCode==='sh600519'` 且跳到个股页。
4. **Esc 关闭**：面板 `hidden`。
5. **交易时钟**：`#mktClock` 文本非空，匹配状态词 `(交易中|午休|集合竞价|已收盘|休市|盘后)`。
6. **控制台 0 报错**。

---

## 6. 跳过项（ponytail）

- 不引 `cmdk`/任何依赖，纯原生 JS + CSS。
- 不做多市场（港股/美股）时段区分，单一 A 股时钟（#94 适配时再扩）。
- 不抄 OpenStock 的 AGPL 源码，仅复用交互模式，自行 vanilla 实现。
