# MCP 接口使用说明（让 AI 直接查你的行情）

> MCP（Model Context Protocol）是 AI 工具连接外部数据的标准协议。
> 装好后，Claude Desktop / Cursor / Windsurf 等客户端可以直接**用自然语言**查询本面板的行情、
> 跑 29 个选股策略、算 61 项技术指标、看连板梯队与快讯——**不用打开网页**。

- 实现：`server/mcp_server.py`（**纯标准库，零新增依赖**，不装 MCP SDK）
- 传输：stdio（子进程方式），与 Web 服务（8899）**互不影响**，面板不用开着也能用
- 权限：默认**只读**；`add_watchlist` 是唯一的写操作（加自选股）

---

## 一、配置（2 步）

### 1. 找到你的客户端配置文件

| 客户端 | 配置位置 |
|---|---|
| **Claude Desktop** | Windows：`C:\Users\<你>\AppData\Roaming\Claude\claude_desktop_config.json`<br>macOS：`~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Cursor** | 项目根 `.cursor/mcp.json` 或全局 `~/.cursor/mcp.json` |
| **Windsurf / 其他** | 见各自文档的 `mcpServers` 配置段 |

### 2. 写入配置

把 `<项目根>` 换成你实际的路径（**用绝对路径**）：

```json
{
  "mcpServers": {
    "niucap": {
      "command": "python",
      "args": ["-u", "<项目根>/server/mcp_server.py"]
    }
  }
}
```

> Windows 若 `python` 不在 PATH，换成完整路径，例如 `C:/Users/you/miniconda3/python.exe`
> （路径里的斜杠用 `/` 或 `\\` 均可）。

**重启客户端**，聊天窗口的工具图标里出现 `niucap-stock-panel` 即成功。

---

## 二、8 个工具

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `get_quote` | 实时行情快照（价格/涨跌/换手/市值/PE） | `codes`（逗号分隔） |
| `get_kline` | K 线：日/周/月 + 1/5/15/30/60 分钟 | `code`、`period`、`count`（≤250） |
| `strategy_list` | 列出 29 个策略的 key / 中文名 / 分类 | 无 |
| `screen` | **29 策略选股**（并集 / 交集，全市场扫描） | `keys`、`mode`、`limit`（≤100） |
| `get_indicators` | 61 项技术指标 + 多空信号 + 技术面结论 | `code`、`period` |
| `get_limitup` | 连板梯队（首板/2板/3板/4板+）+ 6 阶段情绪 | `limit` |
| `get_news` | 双源快讯（新浪 7×24 + 同花顺，带情绪标签） | `limit`（≤50） |
| `add_watchlist` | 批量加入自选（可指定/新建分组，已存在跳过） | `codes`、`folder` |

---

## 三、可以这样问（自然语言）

```text
"帮我用均线多头策略选股，看看今天哪些票符合，列出前 10 只"
"查一下贵州茅台最近的走势，把 60 项指标算一遍，技术面是偏多还是偏空？"
"今天连板梯队怎么样？最高几板？现在市场情绪处在哪个阶段？"
"帮我看看最近有什么重要快讯"
"把这 5 只票加到我的自选，分组叫'AI 选的'"
```

AI 会自动挑合适的工具、填参数，再把结果整理成人话回答你。

---

## 四、注意事项（都是实测过的）

| 现象 | 原因 / 处理 |
|---|---|
| **第一次调用选股要等 30 秒左右** | 首次要拉全市场行情（实测约 22s）+ 跑策略（约 10s）。之后 3 分钟内有缓存，二次调用约 10s |
| 选股/指标返回空或「历史未就绪」 | 日线没同步。打开面板 → 后台管理 → 运行参数 → **开始同步**，或用面板灌一次数据 |
| 连板梯队为空 | 休市、盘中尚未封板，或日线未同步（工具会返回 `note` 说明，不是故障） |
| 想切云数据库 | MCP 会自动读项目根的 `.env`（`TICK_DB_HOST` 等），与面板共用同一份配置 |
| 加自选加到了「本地用户」 | MCP 无登录态，写操作落在默认本地用户，和未登录打开面板是同一个账户 |
| **部署在云平台时用不了 MCP** | stdio 传输需要 AI 客户端在本机起子进程；云平台上请用面板网页。本地 / 自托管部署才支持 MCP |
| 客户端连不上 | 用绝对路径；确认 `python` 可执行；Windows 路径用 `/` |

---

## 五、给开发者的实现要点

改这个文件时请保留以下约束，每一条都是实测踩出来的坑：

1. **不能 `import app`** —— `app.py` 内有 9 处后台线程（调度器、健康巡检、缓存预热…），
   import 它会连带起一堆线程。只 import `store / datasource / screener / indicator` 等独立模块。
2. **先加载 `.env` 再 `import store`** —— `store` 在模块级（import 时）就读 `TICK_DB_HOST` 决定后端，
   晚了会回落本地 SQLite，云数据库用户会读到空库。
3. **stdout 只能有 JSON-RPC** —— 工具执行期用 `_quiet_stdout()` 把 `print` 导到 stderr，
   否则协议流被污染，客户端直接解析失败。
4. **stdout 强制 UTF-8** —— Windows 默认 GBK，中文股票名会把协议写崩。
5. **日志一律走 `_log()`（stderr）**，不要用 `print`。
6. **返回体要限量** —— AI 上下文有限，选股默认 20 条（上限 100）、快讯默认 20（上限 50）、K 线默认 60（上限 250）。

调试：直接跑 `python server/mcp_server.py`，然后逐行粘 JSON-RPC 请求（如
`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`）回车即可看到响应。
