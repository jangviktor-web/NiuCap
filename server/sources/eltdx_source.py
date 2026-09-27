"""eltdx 数据源适配器 —— 本工程中【唯一】直接 import eltdx 的模块。

## 为什么要有这一层

eltdx 走的是通达信 7709 非公开协议，其许可证（ELTDX Research-Only License）
明确**只允许个人学习 / 协议研究 / 非商业用途**，禁止商业服务、数据转售等。
把它的调用全部收拢在这一个文件里，好处是：

  1. 将来若要摘除（例如面板转为对外服务），删掉本文件 + 改一行开关即可，
     不会散落在各处；
  2. 可以做优雅降级：eltdx 没装 / 连不上时，调用方回落到腾讯源，业务无感；
  3. 便于统一处理复权口径、单位换算、异常值过滤这些容易出错的细节。

## 三个必须记住的坑（都踩过，已在此处处理）

1. **`adjust` 必须显式传 `'qfq'`**。默认是 `None`（不复权），会让除权股
   静默产生约 1.8% 的偏差（实测 sh601318 有 28 处不符）。传 `'qfq'` 后
   与腾讯源偏差为 0.000%。

2. **数据在 `.bars` 属性里**，`KlineSeries` 本身不可迭代。

3. **`volume_lots` 才是「手」**，与腾讯 `volume` 口径一致（实测比值 1.0000）；
   `volume_wire_value` 是「股」，差 100 倍，绝不能混用。

## 额外收益

腾讯源的 K 线**不提供成交额**（`amount` 恒为 `None`），导致库里 daily_bars
的 amount 列一直是空的。eltdx 提供真实成交额（单位：元），落库时一并补上——
这对「放量」类策略是刚需字段。

## 用法

    from sources.eltdx_source import available, get_bars_batch
    if available():
        data = get_bars_batch(['sh600519', 'sz000001'], count=500)
        # -> {'sh600519': [{'date','open','close','high','low','volume','amount'}, ...], ...}

批量取数是本适配器的核心价值：实测全市场 5575 只 × 250 根日线仅 15 秒，
而逐只走腾讯源受 400ms 限流拖累需要约 48 分钟。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------- 可选导入

try:
    import eltdx  # type: ignore
    _IMPORT_ERR = ""
except Exception as e:                                    # pragma: no cover
    eltdx = None                                          # type: ignore
    _IMPORT_ERR = f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 配置

# 连接池规模。实测（全市场 5575 只 × 250 根）：
#   server=3, conn/srv=4  → 23.81s
#   server=4, conn/srv=6  → 15.89s
# 再往上收益递减且更易触发对端限制，默认取实测较优的 4×6。
def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


SERVER_COUNT = _env_int("TICK_ELTDX_SERVERS", 4)
CONN_PER_SERVER = _env_int("TICK_ELTDX_CONNS", 6)

# 单次批量请求的代码数。eltdx 内部称 batch_size，200 实测吞吐最佳。
BATCH_SIZE = _env_int("TICK_ELTDX_BATCH", 200)

# 复权口径 —— 必须与前复权的腾讯源保持一致，否则除权股会出现约 1.8% 静默偏差。
ADJUST = os.environ.get("TICK_ELTDX_ADJUST", "qfq")

# 全市场批量取数的超时保护（秒）。正常 15~25s，给足余量。
BAR_TIMEOUT = float(os.environ.get("TICK_ELTDX_TIMEOUT", "120"))

# 分钟线周期白名单。eltdx 的 period 取值实测（见 docs/分钟级实时数据说明.md）：
#   'day' / 'week' / 'month' / '1m' / '5m' / '15m' / '30m' / '60m'
# 注意写法是「数字在前、m 在后」，写成 'm5' 会直接抛
# `ValueError: invalid kline period: "m5"`。
INTRADAY_PERIODS = ("1m", "5m", "15m", "30m", "60m")

#: 单次请求最多 800 根（eltdx 服务端硬限制，超出抛
#: `ValueError: page size must be between 1 and 800`）。要取更多须用 start 翻页。
INTRADAY_PAGE_MAX = 800

# 毫秒级实时快照的缓存时长（秒）。TdxClient 内部有连接池，直接复用。
INTRADAY_QUOTE_TTL = 3.0

# 是否剔除「停牌占位行」。
#
# 通达信对停牌日会返回一条 OHLC 全等于前收、且 volume=0 的占位记录。
# 实测沪深300 里 sh601059 / sh601238 / sh601995 三只在 2026-09-21 就是这种行。
#
# 这类行必须剔除，否则会污染指标计算：把 volume=0 算进「均量」会低估基准，
# 让后续的「放量」判断失真；形态识别也会把它当成一根无波动的 K 线。
# 这就是 docs/策略改造难点评估.md 里标的「难点 2 · 停牌 / 数据滞后」。
#
# 设 TICK_ELTDX_KEEP_SUSPENDED=1 可保留（默认剔除）。
DROP_SUSPENDED = os.environ.get("TICK_ELTDX_KEEP_SUSPENDED", "0") not in ("1", "true", "True")


# 最近一次批量取数剔除的停牌占位行数（供调用方打印，便于观察数据情况）
_LAST_DROPPED: List[int] = [0]


def _is_suspended_placeholder(row: Dict[str, Any]) -> bool:
    """判断是否为停牌占位行：成交量为 0（或极微量）且 OHLC 完全相同。

    只凭 volume==0 判断过于激进（理论上盘中停牌恢复等场景可能合法），
    因此叠加「OHLC 四价全等」这一特征——真实成交日不可能四价完全相同。
    """
    vol = row.get("volume")
    if vol is None or vol > 0:
        return False
    o, c, h, l = row.get("open"), row.get("close"), row.get("high"), row.get("low")
    if None in (o, c, h, l):
        return False
    return o == c == h == l


# ---------------------------------------------------------------- 连接管理

_client = None
_client_lock = threading.Lock()


class EltdxUnavailable(RuntimeError):
    """eltdx 不可用（未安装 / 连接失败）。调用方应据此回落到其他数据源。"""


def available() -> bool:
    """eltdx 是否已安装。注意这【不代表】能连上服务器，连接问题在调用时才会暴露。"""
    return eltdx is not None


def version() -> str:
    """eltdx 版本号。

    注意 `eltdx.version` 是 importlib.metadata.version 的别名（需传包名），
    真正的版本在 `eltdx.__version__`，不要调错。
    """
    if eltdx is None:
        return "-"
    v = getattr(eltdx, "__version__", None)
    if v:
        return str(v)
    try:
        import importlib.metadata as md
        return md.version("eltdx")
    except Exception:
        return "?"


def _get_client():
    """惰性建连并复用。多线程下用锁保护首次创建。"""
    global _client
    if eltdx is None:
        raise EltdxUnavailable(f"eltdx 未安装：{_IMPORT_ERR}")
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        c = eltdx.TdxClient.from_hosts(
            server_count=SERVER_COUNT,
            connections_per_server=CONN_PER_SERVER,
        )
        c.connect()
        _client = c
        return _client


def close() -> None:
    """关闭连接（进程退出 / 测试清理用）。"""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None


def reload_config() -> Dict[str, Any]:
    """从 config 层重读参数并同步到本模块全局。

    管理页改了连接池/批大小后调用。注意连接池参数的变更需重建连接才生效——
    这里顺手关掉旧 client，下次取数会按新参数重连（代价是首请求慢一次）。
    """
    global SERVER_COUNT, CONN_PER_SERVER, BATCH_SIZE, ADJUST, BAR_TIMEOUT, DROP_SUSPENDED
    try:
        import config as _cfg
    except Exception:
        return {"reloaded": False, "note": "config 模块不可用，沿用环境变量"}
    before = (SERVER_COUNT, CONN_PER_SERVER, BATCH_SIZE, BAR_TIMEOUT, DROP_SUSPENDED)
    SERVER_COUNT = _cfg.get("TICK_ELTDX_SERVERS")
    CONN_PER_SERVER = _cfg.get("TICK_ELTDX_CONNS")
    BATCH_SIZE = _cfg.get("TICK_ELTDX_BATCH")
    BAR_TIMEOUT = float(_cfg.get("TICK_ELTDX_TIMEOUT"))
    # 配置项语义是「保留停牌占位行」，本模块用的是它的反面「剔除」
    DROP_SUSPENDED = not bool(_cfg.get("TICK_ELTDX_KEEP_SUSPENDED"))
    after = (SERVER_COUNT, CONN_PER_SERVER, BATCH_SIZE, BAR_TIMEOUT, DROP_SUSPENDED)
    pool_changed = before[:3] != after[:3]
    if pool_changed:
        close()                    # 连接池变化，下次请求重建
    return {"reloaded": True, "changed": before != after, "pool_reset": pool_changed}


# ---------------------------------------------------------------- 数据转换


def _row_from_bar(bar: Any, intraday: bool = False) -> Optional[Dict[str, Any]]:
    """把 eltdx 的 KlineBar 转成本工程统一的行格式。

    统一格式（与 datasource.get_kline / store.upsert_bars 一致）：
        {'date': 'YYYY-MM-DD', 'open', 'close', 'high', 'low',
         'volume': 手, 'amount': 元}

    intraday=True 时 `date` 变成 'YYYY-MM-DD HH:MM'（分钟线必须带时刻，
    否则同一天的多根分钟线会主键冲突）。**注意不要用紧凑的
    'YYYYMMDDHHMM'**：腾讯源 `_rows_to_kline` 原样透传它，两源格式
    不一致会让上层缓存 key 和前端解析都要分叉，统一成可读格式更省心。

    单位换算注意事项见模块 docstring「三个必须记住的坑」第 3 条：
    volume 取 volume_lots（手），而不是 volume_wire_value（股）。
    """
    try:
        t = getattr(bar, "time", None)
        if t is None:
            return None
        # time 是带时区的 datetime
        if hasattr(t, "strftime"):
            d = t.strftime("%Y-%m-%d %H:%M") if intraday else t.strftime("%Y-%m-%d")
        else:
            d = str(t)[:16] if intraday else str(t)[:10]
        if not d:
            return None

        def num(name):
            v = getattr(bar, name, None)
            if v is None:
                return None
            try:
                f = float(v)
                return None if f != f else f      # 过滤 NaN
            except (TypeError, ValueError):
                return None

        return {
            "date": d,
            "open": num("open"),
            "close": num("close"),
            "high": num("high"),
            "low": num("low"),
            "volume": num("volume_lots"),         # 手，与腾讯口径一致
            "amount": num("amount"),              # 元，腾讯源缺此字段
        }
    except Exception:
        return None


# ---------------------------------------------------------------- 对外接口


def get_bars_one(code: str, count: int = 500,
                 adjust: str = None) -> List[Dict[str, Any]]:
    """取单只股票的日线（日期升序）。

    参数
        code   : 形如 'sh600519' / 'sz000001'（本工程统一带交易所前缀）
        count  : 取最近多少根
        adjust : 复权口径，默认取模块级 ADJUST（'qfq'）。除非确知用途，不要改。
    """
    res = get_bars_batch([code], count=count, adjust=adjust)
    return res.get(code, [])


def get_bars_batch(codes: Sequence[str], count: int = 500,
                   adjust: str = None,
                   period: str = "day",
                   start: int = 0,
                   timeout: float = None) -> Dict[str, List[Dict[str, Any]]]:
    """批量取多只股票的 K 线（日线 / 周月 / 分钟线）。**这是本适配器的核心接口。**

    一次调用拉全部代码，走 eltdx 内建的并发池，实测：
      · 5575 只 × 250 根日线   约 15 秒（逐只走腾讯源受 400ms 限流需约 48 分钟）
      · 3800 只 × 400 根 5分钟线 约 12 秒

    参数
        period : 'day'(默认) / 'week' / 'month' / '1m' / '5m' / '15m' / '30m' / '60m'
        start  : 往回跳过多少根。分钟线要取超过 800 根的深度必须靠它翻页。
        adjust : 复权口径。**分钟线请传 'none'**——分时图看的是真实价格，
                 前复权会把历史分钟价按除权因子整体缩放，与当日盘口对不上。

    返回：{code: [行, ...]}，时间升序。取不到的代码不会出现在结果里。
    请求期间出错会抛 EltdxUnavailable，由调用方决定是否降级。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}

    period = (period or "day").strip()
    intraday = period in INTRADAY_PERIODS

    # 分钟线不做复权：分时看到的必须是真实成交价。
    # 日线默认 qfq（必须显式传，否则除权股约 1.8% 静默偏差）。
    if adjust is None:
        adjust = "none" if intraday else ADJUST

    if intraday and count > INTRADAY_PAGE_MAX:
        count = INTRADAY_PAGE_MAX      # 服务端硬限制，上层用 pages 翻页

    try:
        client = _get_client()
    except EltdxUnavailable:
        raise
    except Exception as e:
        raise EltdxUnavailable(f"eltdx 连接失败：{type(e).__name__}: {e}") from e

    try:
        series_map = client.bars.get(
            list(codes),
            period=period,
            start=int(start or 0),
            count=int(count),
            adjust=adjust,
            batch_size=BATCH_SIZE,
        )
    except Exception as e:
        # 连接层问题标记为不可用，让调用方降级；其他异常也一并归类，
        # 因为对本工程而言结果都是「这个源这次没拿到数据」。
        raise EltdxUnavailable(f"eltdx 取数失败：{type(e).__name__}: {e}") from e

    out: Dict[str, List[Dict[str, Any]]] = {}
    dropped = 0
    for code, series in (series_map or {}).items():
        bars = getattr(series, "bars", None)
        if not bars:
            continue
        rows = []
        for b in bars:
            r = _row_from_bar(b, intraday=intraday)
            if not r:
                continue
            if DROP_SUSPENDED and _is_suspended_placeholder(r):
                dropped += 1
                continue
            rows.append(r)
        if rows:
            out[code] = rows
    _LAST_DROPPED[0] = dropped
    return out


def get_intraday(codes: Sequence[str], period: str = "5m",
                 count: int = 400, pages: int = 1,
                 adjust: str = "none") -> Dict[str, List[Dict[str, Any]]]:
    """批量取分钟线，支持翻页取更深的窗口。

    单次请求上限 800 根（服务端硬限制），要更长历史就多翻几页——
    每页都是一次全市场批量请求（3800 只约 12 秒），成本随页数线性增长。

    实测各周期的「可取深度」（400 根/页，sh600519，见 docs/分钟级实时数据说明.md）：
        周期   可取页数    覆盖跨度
        1m      约 40 页    约 1.3 个月
        5m      约 40 页    约 3.5 个月
        15m     约 30 页    约 10 个月
        30m     约 18 页    约 1.8 年
        60m     约 5 页     约 2 年
    超过深度上限的页返回空，不报错（自动停）。

    返回按时间升序合并去重后的 {code: [行, ...]}。
    """
    pages = max(1, int(pages or 1))
    page_size = min(int(count), INTRADAY_PAGE_MAX)
    merged: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for i in range(pages):
        chunk = get_bars_batch(
            codes, count=page_size, period=period,
            start=i * page_size, adjust=adjust)
        if not chunk:
            break                       # 已到深度上限
        for code, rows in chunk.items():
            slot = merged.setdefault(code, {})
            for r in rows:
                slot[r["date"]] = r     # dict 去重（翻页边界可能重叠一根）

    return {code: [bydate[d] for d in sorted(bydate)]
            for code, bydate in merged.items() if bydate}


def get_intraday_one(code: str, period: str = "5m", count: int = 400,
                     pages: int = 1) -> List[Dict[str, Any]]:
    """取单只股票的分钟线（时间升序）。"""
    res = get_intraday([code], period=period, count=count, pages=pages)
    return res.get(code, [])


def last_dropped() -> int:
    """最近一次 get_bars_batch 剔除的停牌占位行数。"""
    return _LAST_DROPPED[0]


def all_a_shares() -> List[str]:
    """全部 A 股代码（形如 ['sh600519', ...]）。实测 5575 只约 2.3 秒。

    注意：这是【交易所全量代码表】，包含当日停牌股，与行情快照的口径不同。
    """
    try:
        client = _get_client()
        return list(client.codes.all_a_shares())
    except EltdxUnavailable:
        raise
    except Exception as e:
        raise EltdxUnavailable(f"eltdx 取代码表失败：{type(e).__name__}: {e}") from e


def ping() -> str:
    """连通性探测。返回服务器标识字符串；不可用则抛 EltdxUnavailable。"""
    try:
        return _get_client().ping()
    except EltdxUnavailable:
        raise
    except Exception as e:
        raise EltdxUnavailable(f"eltdx ping 失败：{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------- 实时快照

#: 快照接口**单次硬上限 80 只**——这是个必须在代码里显式处理的坑。
#:
#: 实测：传 500 只只返回 80 只，传 100 只也只返回 80 只，且**不报错、不告警**，
#: 安静地截断。如果调用方以为「传多少就拿多少」，会得到一份缺了 90% 的
#: 「全市场行情」，而且看起来一切正常——这类静默截断比抛异常危险得多。
#:
#: 注意它和「批量 K 线」的 BATCH_SIZE=200 不是一回事：K 线接口能接受 200+，
#: 快照接口只吃 80。两个接口的限制不能互相套用。
QUOTE_BATCH_MAX = 80


def _row_from_snapshot(q: Any) -> Optional[Dict[str, Any]]:
    """把 eltdx QuoteSnapshot 转成本工程统一的行情行格式。

    字段名与 `datasource.quote_tencent` 的产出**故意保持一致**，这样上层
    换源时不需要改任何消费代码：
        code / symbol / name(缺) / price / prev_close / open / high / low /
        change / change_pct / volume(手) / amount(元) / 五档

    两个源的单位差异（这里是真实的坑）：
        · 腾讯 amount 单位是【万元】，eltdx 是【元】——这里统一转成【元】，
          与 K 线接口的口径一致（K 线的 amount 也是元）。
        · 腾讯 volume 是【手】，eltdx `total_hand` 也是【手】，一致。
    """
    try:
        full = getattr(q, "full_code", None)
        if not full:
            return None
        price = getattr(q, "last_price", None)
        prev = getattr(q, "pre_close_price", None)
        if price is None:
            return None
        try:
            price = float(price)
            prev = float(prev) if prev is not None else 0.0
        except (TypeError, ValueError):
            return None

        change = price - prev
        change_pct = (change / prev * 100.0) if prev > 0 else 0.0

        def levels(name):
            out = []
            for lv in (getattr(q, name, None) or ()):
                try:
                    out.append({"price": float(lv.price), "volume": float(lv.volume)})
                except (TypeError, ValueError, AttributeError):
                    continue
            return out

        amount = getattr(q, "amount", None)          # 元
        return {
            "code": full,
            "symbol": full[2:],
            "name": "",                              # 快照不带名称，由上层补
            "price": round(price, 3),
            "prev_close": round(prev, 3),
            "open": round(float(getattr(q, "open_price", 0) or 0), 3),
            "high": round(float(getattr(q, "high_price", 0) or 0), 3),
            "low": round(float(getattr(q, "low_price", 0) or 0), 3),
            "change": round(change, 3),
            "change_pct": round(change_pct, 2),
            "volume": float(getattr(q, "total_hand", 0) or 0),      # 手
            "amount": (round(float(amount), 2) if amount is not None else None),  # 元
            "turnover": None,                        # 快照不含换手率（需流通股本）
            "pe": None, "pb": None, "amplitude": None,
            "float_cap": None, "total_cap": None,
            "bid_levels": levels("buy_levels"),
            "ask_levels": levels("sell_levels"),
            "inside": float(getattr(q, "inside_dish", 0) or 0),      # 内盘（手）
            "outer": float(getattr(q, "outer_disc", 0) or 0),        # 外盘（手）
            "source": "eltdx",
        }
    except Exception:
        return None


def get_quotes(codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """批量实时快照，返回 {code: 行情行}。

    **自动按 80 只分片**（见 `QUOTE_BATCH_MAX`）——调用方传多少都不用管上限。
    实测 4000 只分片后约 3.6 秒，覆盖 100%。

    取不到的代码不会出现在结果里（停牌 / 已退市 / 代码不存在）。
    整体连接失败会抛 EltdxUnavailable，由调用方决定降级。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}

    try:
        client = _get_client()
    except EltdxUnavailable:
        raise
    except Exception as e:
        raise EltdxUnavailable(f"eltdx 连接失败：{type(e).__name__}: {e}") from e

    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(codes), QUOTE_BATCH_MAX):
        chunk = codes[i:i + QUOTE_BATCH_MAX]
        try:
            snapshots = client.quotes.get_snapshots(chunk)
        except Exception:
            # 单个分片失败不拖垮整批：跳过这一片，剩下的继续。
            # 全市场取数里偶发 TCP 断连是已知现象，不该让整次请求失败。
            continue
        for q in (snapshots or []):
            row = _row_from_snapshot(q)
            if row:
                out[row["code"]] = row
    return out


# ---------------------------------------------------------------- 自检

def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    """连通性 + 数据正确性自检。

    校验三件事：连得上、拿得到、单位对（volume 与 amount 是否合理）。
    这是切换数据源前必跑的一步——单位错了不会报错，只会静默落库错误数据。
    """
    out: Dict[str, Any] = {"ok": False, "steps": []}

    def step(name, ok, detail=""):
        out["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        if verbose:
            print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
                  + (f"  {detail}" if detail else ""))
        return ok

    if not step("import eltdx", available(), _IMPORT_ERR or f"v{version()}"):
        return out

    try:
        t0 = time.time()
        srv = ping()
        step("connect + ping", True, f"{srv}  {time.time()-t0:.2f}s")
    except Exception as e:
        step("connect + ping", False, str(e)[:120])
        return out

    try:
        t0 = time.time()
        data = get_bars_batch(["sh600519", "sz000001"], count=10)
        dt = time.time() - t0
        ok = len(data) == 2 and all(len(v) > 0 for v in data.values())
        step("batch fetch 2 codes", ok, f"{dt:.2f}s")
    except Exception as e:
        step("batch fetch 2 codes", False, str(e)[:120])
        return out

    # 单位校验：volume 应为「手」（万级），amount 应为「元」（十亿级），
    # 且 amount ≈ close × volume × 100 量级。这是最容易出错的地方。
    try:
        row = data["sh600519"][-1]
        vol, amt, close = row["volume"], row["amount"], row["close"]
        vol_ok = vol is not None and 1e2 < vol < 1e7
        amt_ok = amt is not None and amt > 1e6
        ratio = (amt / (close * vol * 100)) if (amt and close and vol) else 0
        ratio_ok = 0.8 < ratio < 1.2
        step("volume 单位=手", vol_ok, f"volume={vol}")
        step("amount 单位=元", amt_ok, f"amount={amt:,.0f}" if amt else "None")
        step("amount≈close×vol×100", ratio_ok, f"比值={ratio:.4f}")
    except Exception as e:
        step("单位校验", False, str(e)[:120])
        return out

    out["ok"] = all(s["ok"] for s in out["steps"])
    out["sample"] = data.get("sh600519", [])[-1:] or []
    return out


if __name__ == "__main__":                                # pragma: no cover
    print("eltdx 适配器自检")
    print("=" * 56)
    r = selfcheck()
    print("=" * 56)
    print("结论：", "全部通过 ✓" if r["ok"] else "存在问题 ✗")
