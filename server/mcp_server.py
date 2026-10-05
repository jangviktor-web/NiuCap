#!/usr/bin/env python3
"""
牛来选股面板 · MCP 服务端（stdio 传输）

让 Claude Desktop / Cursor / Windsurf 等支持 MCP 的 AI 工具直接查询本地行情、
跑 29 个选股策略、算 61 项技术指标、看连板梯队与双源快讯。

设计约束（每一条都是实测踩出来的）：
  1. **绝不 import app** —— app.py 内有 9 处后台线程（调度器 / 健康巡检 /
     缓存预热×2 / 新兵预热…），import 它会连带起一堆线程，MCP 进程不该有。
  2. **必须先加载 .env 再 import store** —— store 在模块级（import 时）就读
     TICK_DB_HOST 决定后端；晚了就回落本地 SQLite，云数据库用户会读到空库。
  3. **stdout 只能有 JSON-RPC** —— 工具执行期把 print 一类输出全部导到 stderr，
     否则协议流被污染，客户端解析直接失败。
  4. **stdout 强制 UTF-8** —— Windows 默认 GBK，中文股票名会把协议写崩。
  5. **零新增依赖** —— 只用标准库 json/sys，不引入 MCP SDK。

用法（Claude Desktop / Cursor 的 mcpServers 配置）：
    {"mcpServers": {"niucap": {"command": "python",
                               "args": ["-u", "<项目根>/server/mcp_server.py"]}}}
"""
import contextlib
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# 1) 路径与工作目录：与 run.py 保持一致，保证能 import server 下的模块
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)                      # 相对路径（data/tick.db）才对得上
sys.path.insert(0, HERE)


# ---------------------------------------------------------------------------
# 2) .env 自读（复刻 app.py 的 _load_env_file，必须在 import store 之前）
#    不覆盖已有进程环境变量，读失败也不拖垮启动。
# ---------------------------------------------------------------------------
def _load_env_file() -> None:
    for path in (os.path.join(os.path.dirname(HERE), ".env"),
                 os.path.join(HERE, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:      # 读 .env 失败不该拖垮启动
            _log(f"[env] 读取 {path} 失败：{e}")
        break


# ---------------------------------------------------------------------------
# 3) stdout 切 UTF-8（Windows GBK 会把中文股票名写崩）
# ---------------------------------------------------------------------------
def _force_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        try:
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer,
                                          encoding="utf-8", newline="\n")
        except Exception:
            pass


_force_utf8_stdout()


def _log(msg: str) -> None:
    """所有日志走 stderr —— stdout 只允许 JSON-RPC 消息"""
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


_load_env_file()

# ---------------------------------------------------------------------------
# 4) 业务模块（独立可用，无 app 依赖）
# ---------------------------------------------------------------------------
import datasource as ds           # noqa: E402
import indicator as ind           # noqa: E402
import limitup as lu              # noqa: E402
import newsfeed as nf             # noqa: E402
import screener as scr            # noqa: E402
import store                      # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "niucap-stock-panel", "version": "1.0.0"}

# 全市场快照缓存（TTL 与面板 MARKET 一致 = 180s）。
# 实测 pool_rows 约 22s、run_strategies 约 10s，不缓存的话每次调用都要半分钟。
_MARKET = {"rows": None, "ts": 0.0}
_MARKET_TTL = 180
_MAX_SCREEN_LIMIT = 100
_MAX_KLINE_COUNT = 250


def _market_rows():
    now = time.time()
    if _MARKET["rows"] and (now - _MARKET["ts"]) < _MARKET_TTL:
        return list(_MARKET["rows"])
    rows = ds.pool_rows("all")
    if rows:
        _MARKET["rows"], _MARKET["ts"] = rows, now
    return list(rows or [])


def _f(v, nd=2):
    """数值规整：None/非数字 → None，避免 JSON 里出现 NaN"""
    try:
        if v is None:
            return None
        return round(float(v), nd)
    except Exception:
        return None


def _slim(r: dict, keys):
    return {k: r.get(k) for k in keys if k in r}


# ---------------------------------------------------------------------------
# 5) 工具实现
# ---------------------------------------------------------------------------
def tool_get_quote(a):
    """实时行情快照（腾讯源，带 3 分钟缓存）"""
    codes = [c.strip().lower() for c in str(a.get("codes", "")).split(",") if c.strip()]
    if not codes:
        raise ValueError("codes 不能为空，例如 sh600519,sz000858")
    # 注意：quote_tencent 返回 {code: row} 字典，不是列表
    raw = ds.quote_tencent(codes)
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw or [])
    keys = ("code", "name", "price", "change_pct", "change", "open", "high",
            "low", "prev_close", "volume", "amount", "turnover",
            "pe", "pb", "float_cap", "total_cap", "amplitude", "time")
    return {"count": len(rows),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": [_slim(r, keys) for r in rows]}


def tool_get_kline(a):
    """K 线：日/周/月 + 1~60 分钟"""
    code = str(a.get("code", "")).strip().lower()
    if not code:
        raise ValueError("code 不能为空")
    period = str(a.get("period", "1d")).strip()
    if period not in ("1d", "1w", "1M", "1m", "5m", "15m", "30m", "60m"):
        raise ValueError("period 仅支持 1d/1w/1M/1m/5m/15m/30m/60m")
    count = int(a.get("count", 60) or 60)
    count = max(5, min(count, _MAX_KLINE_COUNT))
    bars = ds.kline_tencent(code, period=period, count=count)
    keys = ("date", "open", "high", "low", "close", "volume", "amount")
    out = [_slim(b, keys) for b in (bars or [])]
    return {"code": code, "period": period, "count": len(out), "bars": out}


def tool_screen(a):
    """29 个内置策略选股（并集 / 交集）"""
    keys = str(a.get("keys", "")).strip()
    if not keys:
        raise ValueError("keys 不能为空，例如 ma_bull,gap_up（见 strategy_list）")
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    mode = str(a.get("mode", "union")).strip()
    if mode not in ("union", "intersect"):
        raise ValueError("mode 仅支持 union（并集）/ intersect（交集）")
    limit = int(a.get("limit", 20) or 20)
    limit = max(1, min(limit, _MAX_SCREEN_LIMIT))
    exclude_st = bool(a.get("exclude_st", True))

    # 先校验 key 再拉全市场快照：实测快照约 22s，参数错了不该让用户白等
    invalid = [k for k in key_list if k not in scr.STRATEGY_BY_KEY]
    if invalid:
        raise ValueError(f"未知策略: {','.join(invalid)}（用 strategy_list 查看全部）")

    rows = _market_rows()
    if not rows:
        raise RuntimeError("取不到全市场行情快照，请确认网络可用")
    if exclude_st:
        rows = [r for r in rows
                if "ST" not in (r.get("name") or "").upper()
                and "退" not in (r.get("name") or "")]

    hit, tags, diag = scr.run_strategies(rows, key_list, mode=mode)
    hit.sort(key=lambda r: r.get("change_pct") or 0, reverse=True)
    name_map = {d["key"]: d["name"] for d in scr.STRATEGY_DEFS}

    items = []
    for r in hit[:limit]:
        it = _slim(r, ("code", "name", "price", "change_pct", "turnover_rate",
                       "amount", "market_cap", "pe"))
        it["hit"] = [name_map.get(k, k) for k in tags.get(r["code"], [])]
        items.append(it)
    return {
        "keys": [name_map.get(k, k) for k in key_list], "mode": mode,
        "total": len(hit), "returned": len(items), "items": items,
        "diag": {"history_ready": diag.get("hist_ready", False),
                 "history_as_of": diag.get("hist_as_of", ""),
                 "skipped_no_history": diag.get("skipped", []),
                 "data_lag_days": diag.get("data_lag_days")},
    }


def tool_strategy_list(a):
    """列出全部 29 个策略的 key / 中文名 / 分类 / 说明"""
    return {"count": len(scr.STRATEGY_DEFS),
            "items": [{"key": d["key"], "name": d["name"],
                       "cat": d.get("cat", ""), "desc": d.get("desc", "")}
                      for d in scr.STRATEGY_DEFS]}


def tool_get_indicators(a):
    """61 项技术指标总览 + 自动信号（分组返回最新值）"""
    code = str(a.get("code", "")).strip().lower()
    if not code:
        raise ValueError("code 不能为空")
    period = str(a.get("period", "1d")).strip() or "1d"
    count = int(a.get("count", 250) or 250)
    klines = ds.kline_tencent(code, period=period,
                              count=max(60, min(count, _MAX_KLINE_COUNT)))
    if not klines or len(klines) < 30:
        raise RuntimeError("K 线数据不足 30 根，无法计算指标（请先同步日线数据）")
    r = ind.all_indicators(klines)
    if not r:
        raise RuntimeError("指标计算返回空")
    groups = [{"cat": g["cat"],
               "items": [{"name": it.get("name"), "value": _f(it.get("value"), 3),
                          "prev": _f(it.get("prev"), 3)} for it in g["items"]]}
              for g in r.get("groups", [])]
    return {"code": code, "period": period,
            "indicator_count": r.get("indicator_count") or sum(len(g["items"]) for g in groups),
            "verdict": r.get("verdict"), "verdict_dir": r.get("verdict_dir"),
            "bull": r.get("bull_count"), "bear": r.get("bear_count"),
            "neutral": r.get("neutral_count"),
            "signals": r.get("signals", [])[:20],
            "groups": groups}


def tool_get_limitup(a):
    """连板梯队：涨停股按连板天数分组 + 6 阶段市场情绪"""
    limit = int(a.get("limit", 60) or 60)
    limit = max(1, min(limit, 200))
    rows = _market_rows()
    if not rows:
        raise RuntimeError("取不到全市场行情快照")
    # ensure_cached 只返回 board_days/yest 中间数据，梯队要靠 build_ladder 组装
    cached = lu.ensure_cached(rows, limit)
    data = lu.build_ladder(rows, limit=limit, board_cache=cached, background=False)
    # 注入 6 阶段情绪周期（与面板「连板梯队」页一致）
    try:
        import sqlite3
        import market_phase as mp
        conn = sqlite3.connect(store.DB_PATH, timeout=15.0)
        try:
            ph = mp.get_phase_data(conn)
        finally:
            conn.close()
        data["phase"] = ph.get("phase")
        data["phase_ready"] = bool(ph.get("ready"))
    except Exception as e:
        data["phase_error"] = str(e)
    # 休市日 / 数据未同步时梯队为空，给 AI 一句人话，免得它以为接口坏了
    if not (data.get("ladder") or []):
        data["note"] = ("当前无涨停梯队数据：可能是休市、盘中尚未封板，"
                        "或日线未同步（后台管理 → 运行参数 → 开始同步）")
    return data


def tool_get_news(a):
    """双源快讯（新浪 7×24 + 同花顺，去重 + 情绪标签）"""
    limit = int(a.get("limit", 20) or 20)
    feed = nf.get_feed()
    items = (feed.get("items") or [])[:max(1, min(limit, 50))]
    keys = ("source", "time", "content", "sentiment", "red", "codes")
    return {"count": len(items),
            "updated": feed.get("updated") or feed.get("ts"),
            "items": [_slim(it, keys) for it in items]}


def tool_add_watchlist(a):
    """批量加入自选（写操作；默认加到第一个分组，可按名称指定/新建）"""
    codes = [c.strip().lower() for c in str(a.get("codes", "")).split(",") if c.strip()]
    if not codes:
        raise ValueError("codes 不能为空，例如 sh600519,sz000858")
    if len(codes) > 100:
        raise ValueError("单次最多 100 只")
    store.initialize()

    folder_name = str(a.get("folder", "") or "").strip()
    fid = None
    if folder_name:
        folders = store.list_folders()
        hit = [f for f in folders if f.get("name") == folder_name]
        if hit:
            fid = hit[0]["id"]
        else:
            fid = store.create_folder(folder_name).get("id")

    added, skipped, failed = [], [], []
    for c in codes:
        try:
            r = store.add_item(c, folder_id=fid)
            (skipped if r.get("existed") else added).append(c)
        except Exception as e:
            failed.append({"code": c, "error": str(e)})
    return {"requested": len(codes), "added": added,
            "already_exists": skipped, "failed": failed,
            "folder": folder_name or "默认分组"}


# ---------------------------------------------------------------------------
# 6) 工具注册表
# ---------------------------------------------------------------------------
TOOLS = {
    "get_quote": {
        "desc": "查询个股实时行情快照（价格/涨跌幅/换手/市值/PE）。codes 用逗号分隔，如 sh600519,sz000858。",
        "schema": {"type": "object", "properties": {
            "codes": {"type": "string", "description": "股票代码，逗号分隔"}},
            "required": ["codes"]},
        "fn": tool_get_quote,
    },
    "get_kline": {
        "desc": "取 K 线：日/周/月 与 1/5/15/30/60 分钟。用于看走势、算均线。",
        "schema": {"type": "object", "properties": {
            "code": {"type": "string", "description": "股票代码，如 sh600519"},
            "period": {"type": "string", "description": "1d/1w/1M/1m/5m/15m/30m/60m，默认 1d"},
            "count": {"type": "integer", "description": "根数，默认 60，最多 250"}},
            "required": ["code"]},
        "fn": tool_get_kline,
    },
    "strategy_list": {
        "desc": "列出全部 29 个选股策略的 key、中文名、分类与说明。选股前先看这个拿 key。",
        "schema": {"type": "object", "properties": {}},
        "fn": tool_strategy_list,
    },
    "screen": {
        "desc": "用内置策略扫描全市场选股。keys 为策略 key（逗号分隔，见 strategy_list），mode 为并集/交集。首次调用较慢（约 30 秒，需拉全市场行情），之后 3 分钟内有缓存。",
        "schema": {"type": "object", "properties": {
            "keys": {"type": "string", "description": "策略 key，逗号分隔，如 ma_bull,gap_up"},
            "mode": {"type": "string", "description": "union 并集 / intersect 交集，默认 union"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最多 100"},
            "exclude_st": {"type": "boolean", "description": "排除 ST 与退市，默认 true"}},
            "required": ["keys"]},
        "fn": tool_screen,
    },
    "get_indicators": {
        "desc": "计算 61 项技术指标（均线/趋势/震荡/通道/量能/情绪/波动率）与自动多空信号、技术面结论。",
        "schema": {"type": "object", "properties": {
            "code": {"type": "string", "description": "股票代码，如 sh600519"},
            "period": {"type": "string", "description": "周期，默认 1d"},
            "count": {"type": "integer", "description": "K 线根数，默认 250"}},
            "required": ["code"]},
        "fn": tool_get_indicators,
    },
    "get_limitup": {
        "desc": "连板梯队：当日涨停股按连板天数分组（首板/2板/3板/4板+）。",
        "schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "涨停股取样数，默认 60"}}},
        "fn": tool_get_limitup,
    },
    "get_news": {
        "desc": "双源快讯（新浪 7×24 + 同花顺），带去重与情绪标签。",
        "schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "条数，默认 20，最多 50"}}},
        "fn": tool_get_news,
    },
    "add_watchlist": {
        "desc": "批量把股票加入自选股（写操作）。可指定分组名，不存在则自动新建；已存在的跳过。",
        "schema": {"type": "object", "properties": {
            "codes": {"type": "string", "description": "股票代码，逗号分隔，最多 100 只"},
            "folder": {"type": "string", "description": "目标分组名，留空用默认分组"}},
            "required": ["codes"]},
        "fn": tool_add_watchlist,
    },
}


# ---------------------------------------------------------------------------
# 7) JSON-RPC over stdio
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _quiet_stdout():
    """工具执行期把 print 一类输出导到 stderr，保护 stdout 的协议流"""
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = real


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": str(message)}})


def handle(req):
    method, req_id = req.get("method"), req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(req_id, {"protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "initialized",
                  "notifications/cancelled"):
        return None
    if method == "tools/list":
        return _ok(req_id, {"tools": [
            {"name": n, "description": t["desc"], "inputSchema": t["schema"]}
            for n, t in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool:
            return _err(req_id, -32601, f"未知工具: {name}")
        try:
            with _quiet_stdout():
                out = tool["fn"](args)
            text = json.dumps(out, ensure_ascii=False, default=str)
        except Exception as e:
            # 业务异常也走 content 返回（isError），让 AI 能读懂并自行修正参数，
            # 而不是把整个 MCP 连接搞崩
            return _ok(req_id, {"content": [{"type": "text",
                                             "text": f"调用失败：{e}"}],
                                "isError": True})
        return _ok(req_id, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _ok(req_id, {})
    if req_id is not None:
        return _err(req_id, -32601, f"不支持的方法: {method}")
    return None


def main():
    _log(f"[mcp] {SERVER_INFO['name']} v{SERVER_INFO['version']} 就绪，"
         f"工具 {len(TOOLS)} 个，后端 store={store.BACKEND}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _err(None, -32700, f"JSON 解析失败：{e}")
            continue
        try:
            handle(req)
        except Exception as e:
            _log(f"[mcp] handle 异常：{e}")
            _err(req.get("id"), -32603, f"内部错误：{e}")


if __name__ == "__main__":
    main()
