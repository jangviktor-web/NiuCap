"""
数据源层 —— 多源自动降级
沙箱实测：腾讯行情/K线 稳定可用；新浪全市场列表 可用；东方财富接口在本环境被屏蔽。
"""
import re
import os
import json
import time
import threading
import requests
from typing import Optional

# 本地 A 股节假日表（休市日 + 调休补班日），见文件内说明
import holidays as _hl

# 同花顺（fuyao）备源。定位是**第三级降级**：只在 eltdx / 腾讯 / 新浪全挂时
# 才起作用，所以常态零开销。顶层 import 但包了 try —— 拿不到凭证时
# _htk.API_AVAILABLE 为 False，降级链自动少一级，不影响任何现有路径。
try:
    import hithink as _htk
except Exception:                     # pragma: no cover
    _htk = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

TIMEOUT = 12

# ---------------------------------------------------------------------------
# 全局限速 + 内存缓存（防止触发数据源 WAF/限流）
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_last_call = {}

_cache_lock = threading.Lock()
_kline_cache = {}      # key -> (timestamp, data)
_quote_cache = {}
_intraday_cache = {}   # 分钟线单独一份，TTL 更短

KLINE_TTL = 300        # K线缓存 5 分钟（日线级别足够）
QUOTE_TTL = 20         # 行情缓存 20 秒
INTRADAY_TTL = 15      # 分钟线缓存 15 秒（分时要「看起来实时」，不能沿用 300 秒）


def _throttle(key, min_interval):
    """同一数据源最小调用间隔，避免高频触发限流。

    实现要点：在锁内【只做时间片预留】，把 sleep 放到锁外。

    旧写法是「持锁 sleep」——锁在 sleep 期间一直被占用，于是所有线程
    被迫串行等待，批量同步时实际退化成单线程（实测 4 并发与 8 并发耗时
    相同）。现在改为发号制：每个调用者在锁内领取自己的 `slot`（下一个
    可用时刻），锁立刻释放，再各自 sleep 到自己的 slot。请求发出的时刻
    间隔仍是 min_interval，限流语义不变，但等待可以并行。
    """
    with _rate_lock:
        now = time.time()
        last = _last_call.get(key, 0)
        slot = max(now, last + min_interval)
        _last_call[key] = slot
    wait = slot - time.time()
    if wait > 0:
        time.sleep(wait)


def _cache_get(cache, key, ttl):
    with _cache_lock:
        item = cache.get(key)
    if item and (time.time() - item[0]) < ttl:
        return item[1]
    return None


def _cache_put(cache, key, val, maxsize=4000, skip_empty=False):
    """写缓存。

    skip_empty=True 时**不缓存空结果**。这是为了修一个真实踩到的坑：
    数据源限流（实测腾讯分钟线被限流时会安静地返回 200 + 空数组，
    而不是报错）返回空后，那个空结果会被缓存 TTL 秒，导致后续
    「清了限流再重试」也一直是空的——表现为「接口明明能用却一直没数据」，
    且现象会持续到 TTL 过期，极难排查。

    取数失败的「空」与「这只票确实没有数据」在调用方无法区分，
    因此一律不缓存空值：代价是空结果会重复打数据源，但那本就是
    小概率路径，换来的是不会被假数据锁死。
    """
    if skip_empty and not val:
        return
    with _cache_lock:
        if len(cache) > maxsize:
            # 简单淘汰：清掉最旧的一半
            items = sorted(cache.items(), key=lambda kv: kv[1][0])
            for k, _ in items[:len(items) // 2]:
                cache.pop(k, None)
        cache[key] = (time.time(), val)


def _get(url, headers=None, encoding=None, timeout=TIMEOUT):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r


# ---------------------------------------------------------------------------
# 代码规范化
# ---------------------------------------------------------------------------

def normalize(code: str) -> str:
    """600519 -> sh600519 ; 000858 -> sz000858 ; 00700 -> hk00700"""
    code = str(code).strip().lower()
    if re.match(r"^(sh|sz|bj|hk|us)", code):
        return code
    if re.match(r"^\d{5}$", code):          # 港股 5 位
        return "hk" + code
    if re.match(r"^\d{6}$", code):
        # 920xxx 是北交所 2024 年起启用的新代码段（如 920002 万达轴承）。
        # 它落在「9 开头」，但**不是**沪市——按沪市走会全线取不到数据
        # （实测 sh920002 快照与日线都是 None，bj920002 正常）。
        # 沪市 9 开头只有 900xxx（B 股），所以这里单独把 920 段摘出来。
        if code[:3] == "920":
            return "bj" + code
        if code[:3] == "200":               # 深市 B 股（沪市 2 开头只有 204xxx 逆回购）
            return "sz" + code
        if code[0] == "1":
            # 1 开头横跨两个市场，按段分：
            #   11xxxx -> 沪市可转债（110/113/118）
            #   12/15/16/18xxxx -> 深市（可转债 / ETF / LOF / 分级）
            # 实测：sh159915、sh123138 取不到；更糟的是 sh160216 会返回 100.0
            # 这种占位数据（真实价 0.634），属于「取到了但取错」。
            return ("sh" if code[:2] == "11" else "sz") + code
        if code[0] in ("6", "5", "9"):      # 6/5/9 开头 -> 沪市(含ETF/科创)
            return "sh" + code
        # 这里原来是 `code[0] in "032"`，是字符串包含判断——"2" 也在 "032" 里，
        # 于是 204001（沪市国债逆回购 GC001）被判成深市，取不到数。
        # 2 开头只有 200xxx（深 B）是深市，其余（204xxx）归沪市，走默认分支。
        if code[0] in ("0", "3"):           # 0/3 开头 -> 深市(含创业板)
            return "sz" + code
        if code[0] in ("4", "8"):           # 4/8 开头 -> 北交所
            return "bj" + code
        return "sh" + code
    return code


def bare(code: str) -> str:
    return re.sub(r"^(sh|sz|bj|hk|us)", "", str(code).lower())


# ---------------------------------------------------------------------------
# 腾讯实时行情（主源，实测稳定，支持批量）
# ---------------------------------------------------------------------------

# 腾讯行情字段位置（0-based，按 ~ 分割后）—— 实测校准（sh600519, 88 字段）
TQ_FIELDS = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "volume": 6, "time": 30, "change": 31, "change_pct": 32,
    "high": 33, "low": 34,
    "amount": 37,        # 单位：万元
    "turnover": 38,      # 换手率 %
    "pe": 39,
    "amplitude": 43,     # 振幅 %
    "float_cap": 44,     # 流通市值 亿
    "total_cap": 45,     # 总市值 亿
    "pb": 46,
    "limit_up": 47, "limit_down": 48,
}


def _f(parts, key, default=0.0):
    idx = TQ_FIELDS.get(key)
    if idx is None or idx >= len(parts):
        return default
    v = parts[idx]
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def quote_tencent(codes, use_cache=True):
    """腾讯批量实时行情（带缓存 + 限速）。codes: list[str] -> dict[code] = {...}"""
    if not codes:
        return {}
    codes = sorted(set(normalize(c) for c in codes))
    if use_cache:
        key = ",".join(codes)
        hit = _cache_get(_quote_cache, key, QUOTE_TTL)
        if hit is not None:
            return hit

    out = {}
    # 腾讯单次不宜过多，分批
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        _throttle("tencent_quote", 0.35)
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            r = _get(url, encoding="gbk")
        except Exception:
            continue
        for line in r.text.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            head, _, payload = line.partition("=")
            payload = payload.strip().strip('"')
            parts = payload.split("~")
            if len(parts) < 40:
                continue
            code = head.strip().replace("v_", "")
            price = _f(parts, "price")
            prev = _f(parts, "prev_close")
            if price <= 0:
                continue
            out[code] = {
                "code": code,
                "symbol": bare(code),
                "name": parts[1].strip(),
                "price": round(price, 3),
                "prev_close": round(prev, 3),
                "open": round(_f(parts, "open"), 3),
                "high": round(_f(parts, "high"), 3),
                "low": round(_f(parts, "low"), 3),
                "change": round(_f(parts, "change"), 3),
                "change_pct": round(_f(parts, "change_pct"), 2),
                "volume": round(_f(parts, "volume"), 0),           # 手
                "amount": round(_f(parts, "amount"), 2),           # 万元
                "turnover": round(_f(parts, "turnover"), 2),       # 换手率%
                "pe": round(_f(parts, "pe"), 2),
                "pb": round(_f(parts, "pb"), 2),
                "amplitude": round(_f(parts, "amplitude"), 2),
                "float_cap": round(_f(parts, "float_cap"), 2),     # 亿
                "total_cap": round(_f(parts, "total_cap"), 2),     # 亿
                "limit_up": round(_f(parts, "limit_up"), 2),
                "limit_down": round(_f(parts, "limit_down"), 2),
                "time": parts[TQ_FIELDS["time"]] if len(parts) > 30 else "",
                "source": "tencent",
            }
    if use_cache and out:
        _cache_put(_quote_cache, ",".join(codes), out)
    return out


# ---------------------------------------------------------------------------
# 腾讯 K 线（日/周/月 + 分钟）
# ---------------------------------------------------------------------------

PERIOD_MAP = {
    "1d": ("day", "qfqday"),
    "1w": ("week", "qfqweek"),
    "1M": ("month", "qfqmonth"),
}

# 周期别名归一：前端可能传 day/week/month，统一成内部 1d/1w/1M，
# 否则会被误判为分钟线（走 mkline 端点）导致取不到数据。
_PERIOD_ALIAS = {
    "d": "1d", "day": "1d", "daily": "1d", "1day": "1d",
    "w": "1w", "week": "1w", "weekly": "1w",
    "m": "1M", "month": "1M", "monthly": "1M", "1m": "1M",
}


def normalize_period(period):
    """把各种周期写法统一成内部键（1d/1w/1M 或分钟线原样）。"""
    if period is None:
        return "1d"
    p = str(period).strip()
    return _PERIOD_ALIAS.get(p.lower(), p)


# 分钟线周期。本项目内部一律用「1m/5m/15m/30m/60m」这种「数字+m」写法，
# 与 eltdx 一致；腾讯源用的是同一个写法，不用转换。
INTRADAY_PERIODS = ("1m", "5m", "15m", "30m", "60m")

# 「5」「5min」「5分钟」「m5」这类写法统一成「5m」。
# 注意 eltdx 不接受 'm5'（会抛 invalid kline period），所以这里必须归一。
_MIN_ALIAS = {}
for _p in INTRADAY_PERIODS:
    _n = _p[:-1]
    for _w in (f"{_n}min", f"{_n}分钟", f"m{_n}", f"{_n}m", _n):
        _MIN_ALIAS[_w] = _p


def normalize_intraday_period(period) -> Optional[str]:
    """把分钟线周期写法归一到 '1m'/'5m'/'15m'/'30m'/'60m'。

    认不出（如 '1d'、'abc'）返回 None，调用方据此回落到日线或报错。
    """
    if period is None:
        return None
    p = str(period).strip().lower()
    if p in INTRADAY_PERIODS:
        return p
    return _MIN_ALIAS.get(p)


def _is_waf(text):
    """识别腾讯 WAF 拦截页"""
    if not text:
        return False
    head = text[:400].lower()
    return ("waf.tencent.com" in head or "<!doctype html" in head
            or "<html" in head[:60])


# 腾讯 K 线端点。实测：web.ifzq 常触发 WAF(501)，ifzq 与 proxy 稳定。
# 故把可靠域放前面，并对每个域做一次短重试。
_KLINE_HOSTS = [
    "https://ifzq.gtimg.cn",
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://web.ifzq.gtimg.cn",
]

_KLINE_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "Accept": "*/*",
}


def _try_kline_url(url, key, p, code, tries=2):
    """请求单个 K 线端点，返回 rows（失败/被 WAF 返回 []）。短重试避开瞬时拦截。"""
    for _ in range(tries):
        try:
            r = _get(url, headers=_KLINE_HEADERS, timeout=12)
            if _is_waf(r.text):
                time.sleep(0.3)
                continue
            j = r.json()
            data = (j.get("data") or {}).get(code) or {}
            rows = data.get(key) or data.get(p) or []
            if rows:
                return rows
        except Exception:
            time.sleep(0.3)
            continue
    return []


def kline_tencent(code, period="1d", count=250):
    """腾讯 K 线。多域降级（ifzq -> proxy -> web.ifzq），每域短重试，规避 WAF 限流。"""
    code = normalize(code)
    period = normalize_period(period)
    _throttle("tencent_kline", 0.4)

    if period in PERIOD_MAP:
        p, key = PERIOD_MAP[period]
        for host in _KLINE_HOSTS:
            if "proxy.finance" in host:
                url = f"{host}/appstock/app/newfqkline/get?param={code},{p},,,{count},qfq"
            else:
                url = f"{host}/appstock/app/fqkline/get?param={code},{p},,,{count},qfq"
            rows = _try_kline_url(url, key, p, code)
            if rows:
                return _rows_to_kline(rows)
        return []

    # 分钟线 m1/m5/m15/m30/m60
    for host in _KLINE_HOSTS:
        url = f"{host}/appstock/app/kline/mkline?param={code},{period},,{count}"
        rows = _try_kline_url(url, period, period, code)
        if rows:
            return _rows_to_kline(rows)
    return []


def _rows_to_kline(rows):
    out = []
    for row in rows:
        try:
            out.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    return out


def kline_sina(code, count=250):
    """新浪日K 兜底"""
    code = normalize(code)
    _throttle("sina_kline", 0.5)
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={count}")
    try:
        r = _get(url, headers={"Referer": "https://finance.sina.com.cn/"})
        txt = r.text.strip()
        if not txt.startswith("["):
            return []          # 被封或异常
        rows = json.loads(txt)
    except Exception:
        return []
    out = []
    for row in rows or []:
        try:
            out.append({
                "date": str(row["day"])[:10],
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row["volume"]) / 100.0,   # 股 -> 手
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def get_kline(code, period="1d", count=250, use_cache=True):
    """K线多源降级：腾讯 -> 新浪 -> 同花顺（日线），带缓存

    注：分钟/周/月线仅腾讯支持（上游同花顺只给日线）；日线三源互备。
    """
    code = normalize(code)
    period = normalize_period(period)
    ckey = f"{code}|{period}|{count}"
    if use_cache:
        hit = _cache_get(_kline_cache, ckey, KLINE_TTL)
        if hit is not None:
            return hit

    rows = kline_tencent(code, period, count)
    if not rows and period == "1d":
        rows = kline_sina(code, count)
        if not rows:
            rows = kline_hithink(code, count)

    # 不缓存空结果：数据源限流时会静默返回空，缓存了会把自己锁死 TTL。
    _cache_put(_kline_cache, ckey, rows, skip_empty=True)
    return rows


def kline_hithink(code, count=250, adjust="forward"):
    """同花顺历史日 K（第三级备源）。无凭证 / 上游不可用时返回 []。

    为什么放在最后：它是**兜底**不是**优化** —— 腾讯 0.4s 节流批量的速度
    远优于这里，本地库已有 274 万根日线也轮不到它。价值只在前两个源同时
    不可用时（eltdx 被关 / 腾讯被 WAF）让页面还有数据。
    """
    if _htk is None or not _htk.API_AVAILABLE:
        return []
    try:
        return _htk.kline_history(code, count, adjust)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 分钟线（实时分时）
#
# 两源降级：
#   1. eltdx  — 通达信 7709 协议。批量取数，实测全市场 3800 只 × 400 根
#               5 分钟线仅 12 秒，且【带真实成交额】与【精确到分钟的时间戳】。
#               注意许可证只允许个人学习/非商业用途，见 sources/eltdx_source.py。
#   2. 腾讯   — 逐只接口，受 0.4s/只 硬性节流，全市场需约 24 分钟。
#               只适合单只股票看盘，且不提供成交额。
#
# 缓存 TTL 取 15 秒：分时图必须「看起来是活的」，但也不能每次刷新都打数据源。
# ---------------------------------------------------------------------------

#: 分钟线单只默认取多少根（约 5 个交易日的 5 分钟线）
INTRADAY_DEFAULT_COUNT = 400
#: 单只最多取多少根，防止前端传个巨大值把数据源打爆（2400 ≈ 15 个交易日）
INTRADAY_MAX_COUNT = 2400

#: 各周期的「默认取多少根」。
#:
#: 为什么不能所有周期都统一 400 根：**不同周期的一根代表的时间跨度差 60 倍**。
#: 实测（sh600519，400 根）：
#:     1m  -> 只覆盖 1.6 个交易日   → MA60 是拿「1.6 天」算出来的，指标严重失真
#:     5m  -> 覆盖 8 个交易日        → 可用
#:     15m -> 覆盖 25 个交易日       → 可用
#:     60m -> 覆盖 100 个交易日      → 可用
#: 所以 1m 必须显著加大根数（2400 根 ≈ 10 个交易日），否则用户在 1 分钟
#: 周期下看到的 MA20/MA60 是「假均线」——数值算得出来，但口径完全是错的。
#: 这属于「不报错的错误」，比报错更危险。
INTRADAY_DEFAULT_COUNT_BY_PERIOD = {
    "1m": 2400,
    "5m": 800,
    "15m": 800,
    "30m": 800,
    "60m": 800,
}


def default_intraday_count(period: str) -> int:
    """取该周期的推荐根数（分时看盘用，未收录的周期回落通用默认值）。"""
    p = normalize_intraday_period(period)
    return INTRADAY_DEFAULT_COUNT_BY_PERIOD.get(p, INTRADAY_DEFAULT_COUNT)


#: 全市场扫描用多少根。
#:
#: 这个值**故意比看盘小得多**，因为扫描的指标需求是固定的：
#: 最长窗口是 MA60 + vol_ratio20，实际只需要约 61 根。
#:
#: 实测（sh600519 5m）：
#:     count=100 → ma60=1255.7376666666669  量比=1.603   chg20=0.041%
#:     count=800 → ma60=1255.7376666666669  量比=1.603   chg20=0.041%
#: 指标**逐位相同**，但批量耗时：
#:     count=100 → 10.8s    count=400 → 16.9s    count=800 → 31.4s
#: 即 800 根花了 3 倍时间换来完全一样的结果——纯浪费。
#:
#: 取 120 是留了安全边际：新股/长期停牌股的序列里可能有空洞，
#: 多给的 60 根用于兜住「有效根数不足 61」的情况。
SCAN_COUNT = 120


def scan_count(period: str) -> int:
    """全市场扫描用的根数。与 `default_intraday_count` 分开，别混用。"""
    return SCAN_COUNT


def _kline_intraday_tencent(code, period, count):
    """腾讯分钟线（已带 0.4s 节流），失败返回 []。

    ## 必须做周期写法转换（这是个真实踩到的坑）

    两个源的分钟周期写法**正好相反**，且都不会报错、只会安静返回空：
        本项目内部 / eltdx :  '5m'   （数字在前）
        腾讯 mkline 端点    :  'm5'   （m 在前）
    实测 `kline_tencent(code, '5m', 100)` 返回 0 根，
    而 `kline_tencent(code, 'm5', 100)` 正常返回 100 根。
    因此这里统一把内部写法翻成腾讯要的写法再调用。
    """
    tx = {"1m": "m1", "5m": "m5", "15m": "m15",
          "30m": "m30", "60m": "m60"}.get(period, period)
    try:
        rows = kline_tencent(code, tx, count)
    except Exception:
        return []
    return _normalize_intraday_dates(rows)


def _normalize_intraday_dates(rows):
    """把腾讯的紧凑时间戳 'YYYYMMDDHHMM' 统一成 'YYYY-MM-DD HH:MM'。

    为什么要统一：两个源格式不同（eltdx 给的是可读格式），若不抹平，
    上层的缓存 key、前端图表解析、跨源比对都要写两套分支——
    实测已经因为格式差异导致过「同一只票两条来源数据拼不齐」的问题。

    同时顺带做一次**去重**：腾讯 mkline 在翻页/重试边界可能返回重叠根。
    非法格式的行直接丢弃（宁可少一根，也不让脏数据进图表）。
    """
    out = []
    seen = set()
    for r in rows or []:
        d = str(r.get("date") or "")
        if len(d) == 12 and d.isdigit():            # 202609221500
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}"
        elif len(d) == 10 and d.isdigit():          # 2026092215（缺分钟）
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:00"
        if not d or d in seen:
            continue
        seen.add(d)
        r = dict(r)
        r["date"] = d
        out.append(r)
    out.sort(key=lambda x: x["date"])
    return out


def get_kline_intraday(code, period="5m", count=None,
                       use_cache=True):
    """取单只股票的分钟线（时间升序），eltdx 优先、腾讯降级。

    返回统一格式：
        [{'date': 'YYYY-MM-DD HH:MM', 'open','close','high','low',
          'volume': 手, 'amount': 元 或 None}, ...]

    `count=None` 时按周期取推荐根数（见 `INTRADAY_DEFAULT_COUNT_BY_PERIOD`）。
    **不要图省事统一传 400**：1m 周期下 400 根只有 1.6 天，算出来的
    MA60 是错的（详见 `default_intraday_count` 的注释）。

    `amount` 在腾讯源下为 None（该源不提供成交额）——这是可接受的降级，
    序列里其他字段齐全。调用方应能容忍 None，不要直接参与运算。

    失败时返回 []（**不抛异常**）：分时图取不到数据是常态情况之一
    （新股、停牌、代码写错），不该让上层 500。
    """
    code = normalize(code)
    p = normalize_intraday_period(period)
    if not p:
        return []                       # 非法周期（如 '1d'）直接空，由上层给 400
    if count is None:
        count = default_intraday_count(p)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = default_intraday_count(p)
    count = max(1, min(count, INTRADAY_MAX_COUNT))

    ckey = f"{code}|{p}|{count}"
    if use_cache:
        hit = _cache_get(_intraday_cache, ckey, INTRADAY_TTL)
        if hit is not None:
            return hit

    rows = _intraday_from_eltdx_one(code, p, count)
    source = "eltdx"
    if not rows:
        rows = _kline_intraday_tencent(code, p, count)
        source = "tencent" if rows else ""

    # 无论哪个源，出口格式必须完全一致（见 _normalize_intraday_dates）。
    rows = _normalize_intraday_dates(rows)
    if rows:
        # 标注来源，便于前端/日志判断是否拿了降级数据
        for r in rows:
            r["source"] = source
    # 同样不缓存空结果：分钟线被限流时也是静默返回空。
    _cache_put(_intraday_cache, ckey, rows, skip_empty=True)
    return rows


def _load_eltdx_source():
    """惰性加载 eltdx 适配器，并**尊重 sources 包的总开关**。

    为什么要绕到这里而不是直接 `import sources.eltdx_source`：
    总开关（TICK_ELTDX=0）在 `sources/__init__.py` 里定义，用于
    「许可证合规排查」和「怀疑 eltdx 导致数据异常」时一键整体退回腾讯源。
    若这里直接 import 具体模块，就会绕过那个开关，让开关失效。

    返回模块对象，或 None（不可用 / 被开关关闭）。
    """
    try:
        import sys as _sys
        import os as _os
        _base = _os.path.dirname(_os.path.abspath(__file__))
        if _base not in _sys.path:
            _sys.path.insert(0, _base)
        import sources                      # noqa: F401  （触发总开关求值）
        if not sources.eltdx_enabled():
            return None
        return sources.eltdx_source
    except Exception:
        return None


def _intraday_from_eltdx_one(code, period, count):
    """走 eltdx 取单只分钟线，需要超过 800 根时自动翻页。"""
    _e = _load_eltdx_source()
    if _e is None:
        return []
    try:
        pages = max(1, (count + 799) // 800)
        return _e.get_intraday_one(code, period=period, count=min(count, 800),
                                   pages=pages)
    except Exception:
        # EltdxUnavailable 或其它异常，统一视为「这个源这次没拿到」
        return []


def get_intraday_batch(codes, period="5m", count=INTRADAY_DEFAULT_COUNT):
    """批量取多只股票的分钟线，返回 {code: [行, ...]}。

    只在 eltdx 可用时才有意义（腾讯源逐只需 0.4s/只）。eltdx 不可用时
    返回 {}，调用方应回落到「逐只拉取」或直接放弃全市场扫描。

    实测：3800 只 × 400 根 5 分钟线约 12 秒。
    """
    codes = [normalize(c) for c in (codes or []) if c]
    if not codes:
        return {}
    p = normalize_intraday_period(period)
    if not p:
        return {}
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = INTRADAY_DEFAULT_COUNT
    count = max(1, min(count, INTRADAY_MAX_COUNT))

    _e = _load_eltdx_source()
    if _e is None:
        return {}
    try:
        pages = max(1, (count + 799) // 800)
        res = _e.get_intraday(codes, period=p, count=min(count, 800),
                              pages=pages)
    except Exception:
        return {}

    out = {}
    for code, rows in (res or {}).items():
        if not rows:
            continue
        for r in rows:
            r["source"] = "eltdx"
        out[code] = rows
    return out


def intraday_source_status():
    """当前分钟线数据源可用性（供 /api/health 与前端提示用）。"""
    _e = _load_eltdx_source()
    if _e is None:
        return {"primary": "tencent", "eltdx": False,
                "note": "eltdx 不可用（未安装或被总开关 TICK_ELTDX=0 关闭），"
                        "分钟线走腾讯逐只；全市场分钟线扫描不可用"}
    try:
        ping = _e.ping()
    except Exception as ex:
        return {"primary": "tencent", "eltdx": False,
                "note": f"eltdx 连不上（{type(ex).__name__}），走腾讯逐只"}
    return {"primary": "eltdx", "eltdx": True, "ping": ping,
            "version": _e.version(),
            "note": "eltdx 可用，支持全市场分钟线批量扫描"}


# ---------------------------------------------------------------------------
# 实时快照（elta 优先 / 腾讯降级）
#
# 为什么要换掉腾讯快照：腾讯是**逐只**接口且带 0.4s 硬性节流，
# 取 50 只要 20 秒。分时看盘每次刷新都要快照，这个延迟不可接受。
# eltdx 快照按 80 只分片批量取，4000 只只要 3.6 秒（实测）。
#
# 更重要的是一致性：分钟 K 线已经来自 eltdx，如果快照还走腾讯，
# 会出现「K 线最后一根收在 A、快照显示 B」的时差观感问题。
# 两处用同一个源，时间戳口径才是自洽的。
# ---------------------------------------------------------------------------

SNAPSHOT_TTL = 3       # 快照缓存 3 秒（分时要「活」，又不能每次刷新都打源）


# ---------------------------------------------------------------------------
# 代码有效性守卫
#
# 为什么需要它：通达信协议**不校验代码合法性**，对不存在的代码会安静地
# 返回「某个别的标的」的数据。实测：
#     sh999999 -> 3952.13（与上证指数 sh000001 完全一致）
#     sh999998 -> 4143.88
# 也就是说，用户把代码敲错一位，不会得到报错，而会得到一份**看起来正常
# 但属于另一个标的**的行情——这是最危险的一类错误，因为没有任何迹象提示
# 「这不是你要的股票」。
#
# 判据：A 股代码表（5575 只）+ 指数/板块白名单。
# 不能只用代码表——指数不在 A 股代码表里（sh000001 / sz399001 都不在），
# 只用代码表会把指数全部误杀。
# ---------------------------------------------------------------------------

#: 指数与板块代码（前缀匹配）。分时看盘允许看指数，所以必须放行。
_INDEX_PREFIXES = (
    "sh000", "sh950", "sz399", "bj899",
    "sh880", "sh881", "sh882", "sh883",   # 通达信板块指数
)

#: 北交所代码段。43/83/87/88 是老三板沿用，920 是 2024 年起的新代码段。
#: 为什么单独列：eltdx 的 all_a_shares() 里 bj 只覆盖了 920 段（实测 349 只），
#: 430/83/87/88 段的北交所股票（如 bj430047 诺思兰德）**在代码表里查不到**，
#: 但它们行情完全正常。只按代码表判会误杀真实股票，所以这些段改用探测兜底。
_BJ_SEGMENTS = ("bj43", "bj83", "bj87", "bj88", "bj92")

#: 代码表查不到、但确实存在的代码段。
#: all_a_shares() 只有 A 股，下面这些都不在里面，全靠探测兜底：
#:   - 北交所老段（eltdx 的 bj 只覆盖 920 段）
#:   - B 股 sh900xxx / sz200xxx
#:   - ETF / LOF / 场内基金（沪 5 开头、深 15/16/18）——分钟线实测可用
#:   - 可转债（沪 11、深 12）
#:   - 国债逆回购（沪 204、深 131）
#: 范围必须收窄：见 is_valid_code 里 sh999999 的教训，探测不能对通用段开放。
_PROBE_SEGMENTS = (
    _BJ_SEGMENTS
    + ("sh900", "sz200")
    + ("sh5", "sz15", "sz16", "sz18")
    + ("sh11", "sz12")
    + ("sh204", "sz131")
)

_codes_cache = {"at": 0.0, "set": None}
_probe_cache: dict = {}      # 探测结果缓存（避免同一无效代码反复打数据源）


def _a_share_codes():
    """A 股代码集合（缓存 1 小时）。取不到时返回 None 表示「无法判定」。"""
    now = time.time()
    if _codes_cache["set"] is not None and (now - _codes_cache["at"]) < 3600:
        return _codes_cache["set"]
    _e = _load_eltdx_source()
    if _e is None:
        return None
    try:
        s = set(_e.all_a_shares())
    except Exception:
        return None
    if not s:
        return None
    _codes_cache["set"] = s
    _codes_cache["at"] = now
    return s


def is_valid_code(code) -> bool:
    """判断代码是否可能是真实标的（A 股 or 指数）。

    返回 True 表示「无法证伪」——包括代码表取不到的情况（宁可放过，
    也不要因为校验器自己挂了就把正常请求拦掉）。

    注意：不能反过来用「快照能不能取到」当判据。实测 sh999999 也能取到
    （返回的是上证指数），会把冒名顶替的代码判成有效。
    """
    c = normalize(code)
    if not c:
        return False
    if any(c.startswith(p) for p in _INDEX_PREFIXES):
        return True
    if len(c) != 8:                     # 形如 sh600519，必须是 2+6
        return False
    codes = _a_share_codes()
    if codes is None:
        return True                     # 无法判定时不拦
    if c in codes:
        return True

    # 代码表里没有，但落在「已知表外段」（北交所老段 / B 股）→ 探测一次。
    # 探测走统一快照（eltdx 优先 + 腾讯降级）：实测 bj430047 只有腾讯有。
    # 放行一个不存在的 bj 代码代价很小（北交所分钟线本就落到 404 专项提示），
    # 反过来误杀真实股票代价很大。
    if not any(c.startswith(p) for p in _PROBE_SEGMENTS):
        return False
    if c in _probe_cache:
        return _probe_cache[c]
    ok = True                           # 探测自己挂了 → 不拦
    try:
        ok = bool(snapshot([c]).get(c))
    except Exception:
        pass
    _probe_cache[c] = ok
    return ok


def snapshot_eltdx(codes):
    """走 eltdx 批量快照，返回 {code: 行情行}；不可用返回 {}。"""
    _e = _load_eltdx_source()
    if _e is None:
        return {}
    try:
        return _e.get_quotes(codes)
    except Exception:
        return {}


def quote_hithink(codes):
    """同花顺行情快照（第三级备源），返回 {内部代码: 行情行}；不可用返回 {}。

    ⚠ 实测两条硬限制（详见 server/hithink.py 注释）：
      - 上游**不返回 name**，只有价格/量额。调用方目前只用 price，无影响；
      - 只支持沪深 A 股个股，ETF / 指数 / 北交所一律取不到。
    """
    if _htk is None or not _htk.API_AVAILABLE:
        return {}
    try:
        return _htk.quote_snapshot(codes)
    except Exception:
        return {}


def snapshot(codes, use_cache=True, write_cache=True):
    """统一实时快照入口：eltdx 优先、腾讯次之、同花顺兜底。

    返回格式与 `quote_tencent` **完全兼容**（字段名一致），上层无需分叉。
    三个源的单位差异已在各自适配层抹平（volume 手 / amount 万元 / 市值 亿）。

    为什么要单独一个入口而不是让调用方自己判断：快照是这个工程里
    被调用最频繁的接口（自选、榜单、分时、扫描都要），源选择逻辑
    必须只有一处，否则迟早出现「有的页面用 eltdx、有的用腾讯」。

    write_cache=False：只读不写，给「探测当前实际在用哪个源」用
    （后台概览的降级链高亮）。**探测时必须显式关掉**——实测
    use_cache=False 只跳过「读」，「写」照样发生，一次探测就会把
    样本股行情灌进用户真正看到的缓存。
    """
    codes = [normalize(c) for c in (codes or []) if c]
    if not codes:
        return {}

    ckey = "snap|" + ",".join(sorted(set(codes)))
    if use_cache:
        hit = _cache_get(_quote_cache, ckey, SNAPSHOT_TTL)
        if hit is not None:
            return hit

    out = snapshot_eltdx(codes)
    if out:
        if write_cache:
            _cache_put(_quote_cache, ckey, out, skip_empty=True)
        return out

    # eltdx 不可用（未装/被开关关闭/连接失败）→ 腾讯降级
    out = quote_tencent(codes, use_cache=False)
    if not out:
        # 腾讯也没数据（WAF 拦截 / 网络故障）→ 同花顺兜底
        out = quote_hithink(codes)
    if out and write_cache:
        _cache_put(_quote_cache, ckey, out, skip_empty=True)
    return out


def snapshot_status(probe: bool = False):
    """快照源状态（供 /api/health 与后台概览）。

    chain    : 降级链顺序（前端照此画点，高亮当前生效的那一级）
    primary  : 首选源（eltdx 装不上时为 tencent）
    live     : **实测**当前真正在用哪个源 —— 静默降级不透明是这里要解决的事：
               前面几级全挂时会自动退到下一级，界面上看不出来，数据已经换了源。
               所以后台概览传 probe=True 真取一次快照读它的 source 字段。
               ⚠ 探测用流动性最好的单只（sh600519）走网络，不写快照缓存
               （避免把探测结果灌进用户实际看到的行情缓存里）。
    """
    st = {"chain": ["eltdx", "tencent", "hithink"],
          "hithink_fallback": bool(_htk is not None and _htk.API_AVAILABLE)}
    _e = _load_eltdx_source()
    if _e is None:
        st.update({"primary": "tencent", "eltdx": False})
    else:
        st.update({"primary": "eltdx", "eltdx": True, "batch_max": 80})

    if probe:
        # 为什么绕过缓存：缓存里存的 source 是「上一次写缓存时」用的那一级，
        # 可能几小时前腾讯兜过一次，早过期了。这里 use_cache=False 强制走真实
        # 链路，问的是「此刻请求会走哪一级」——这才是降级链要展示的东西。
        # write_cache=False 则保证这次探测不污染真实缓存（只读不写）。
        try:
            rows = snapshot(["sh600519"], use_cache=False, write_cache=False)
            got = {r.get("source") for r in rows.values() if r.get("source")}
            st["live"] = (sorted(got)[0] if len(got) == 1 else
                          (sorted(got) if got else "none"))   # 混源=列表，全挂=none
        except Exception as e:
            st["live"] = "error"
            st["live_error"] = str(e)[:120]
    return st


# ---------------------------------------------------------------------------
# 市场状态（分时数据的「现在」到底是什么时刻）
#
# 这个问题在收盘后特别容易误导：15:00 之后请求分时，拿到的其实是
# 「当日完整的收盘走势」，但 day_progress=1.0 会让人以为「实时」。
# 所以必须显式告诉前端当前处于哪个时段，前端才能把文案说准。
# ---------------------------------------------------------------------------

#: A 股交易时段（本地时间，不含节假日判断——节假日靠「数据日期是否为今天」二次判定）
_MORNING = ((9, 30), (11, 30))
_AFTERNOON = ((13, 0), (15, 0))


def market_state(now=None):
    """判断当前市场时段。

    返回 {'state', 'label', 'is_trading', 'is_closed_today'}：
        state : 'pre_open'(未开盘) / 'auction'(集合竞价) /
                'trading'(交易中) / 'lunch'(午间休市) /
                'closed'(已收盘) / 'holiday'(休市)
        label : 中文文案，前端直接用

    会查本地交易日历（holidays.py）：工作日里的法定休市日会被判成 'holiday'
    并禁止交易。这样虚拟盘不会在节假日按上一交易日收盘价"成交"（见 #103/#104）。

    ⚠ 周末一律休市，不看调休补班表：国务院的「周末补班」是工作日上班安排，
      **证券交易所周末不开市**。早期实现把补班日当交易日，导致 2026 年有
      5 个周日/周六（01-04、02-14、02-28、05-09、09-20）被判成可交易 ——
      由同花顺权威交易日历对账发现（见 maintain.calendar_audit）。
    """
    t = now or time.localtime()
    hm = (t.tm_hour, t.tm_min)
    wd = t.tm_wday                     # 0=周一 ... 6=周日
    ymd = "%04d-%02d-%02d" % (t.tm_year, t.tm_mon, t.tm_mday)

    # ponytail: 本地节假日表优先于周末判断——工作日法定休市日必须拦住，
    # 否则会被误判成交易中、可按上一交易日收盘价成交。
    if _hl.is_market_holiday(ymd):
        return {"state": "holiday", "label": "休市（节假日）",
                "is_trading": False, "is_closed_today": True}
    # 周末一律休市：A 股不存在「周末补班交易日」（详见本函数 docstring）
    if wd >= 5:
        return {"state": "holiday", "label": "周末休市",
                "is_trading": False, "is_closed_today": True}

    if hm < (9, 15):
        return {"state": "pre_open", "label": "未开盘",
                "is_trading": False, "is_closed_today": False}
    if (9, 15) <= hm < (9, 30):
        return {"state": "auction", "label": "集合竞价",
                "is_trading": False, "is_closed_today": False}
    if _MORNING[0] <= hm <= _MORNING[1]:
        return {"state": "trading", "label": "交易中（上午）",
                "is_trading": True, "is_closed_today": False}
    if _MORNING[1] < hm < _AFTERNOON[0]:
        return {"state": "lunch", "label": "午间休市",
                "is_trading": False, "is_closed_today": False}
    if _AFTERNOON[0] <= hm <= _AFTERNOON[1]:
        return {"state": "trading", "label": "交易中（下午）",
                "is_trading": True, "is_closed_today": False}
    return {"state": "closed", "label": "已收盘",
            "is_trading": False, "is_closed_today": True}


# ---------------------------------------------------------------------------
# 新浪全市场快照（选股池来源，字段齐全）
# 注意：新浪对高频访问会封 IP（HTTP 456，封禁 5-60 分钟），务必低速调用
# ---------------------------------------------------------------------------

SINA_NODE = "hs_a"   # 沪深A股(含北交所)


def sina_available():
    """探测新浪列表接口是否可用（被封返回 False）"""
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"Market_Center.getHQNodeData?page=1&num=2&node={SINA_NODE}")
    try:
        r = _get(url, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=15)
        return r.status_code == 200 and r.text.strip().startswith("[")
    except Exception:
        return False


def market_snapshot(pages=55, num=100, delay=1.2):
    """
    新浪全市场列表分页拉取（低速，避免触发限流）。
    返回 list[dict]
    """
    base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
    headers = {"Referer": "https://finance.sina.com.cn/"}
    out = []
    seen = set()
    for page in range(1, pages + 1):
        params = (f"?page={page}&num={num}&sort=symbol&asc=1&node={SINA_NODE}")
        try:
            r = _get(base + params, headers=headers, timeout=20)
            if r.status_code != 200 or not r.text.strip().startswith("["):
                break               # 被封或异常，立即停止（不要继续加重）
            rows = json.loads(r.text)
        except Exception:
            break
        if not rows:
            break
        for row in rows:
            sym = row.get("symbol", "")
            if not sym or sym in seen:
                continue
            seen.add(sym)

            def num_of(k, default=0.0):
                try:
                    return float(row.get(k) or 0)
                except (TypeError, ValueError):
                    return default

            price = num_of("trade")
            if price <= 0:
                continue
            out.append(_sina_row(row, sym, num_of))
        time.sleep(delay)          # 限流保护
    return out


def _sina_row(row, sym, num_of):
    return {
        "code": sym,
        "symbol": row.get("code", ""),
        "name": row.get("name", "").replace(" ", ""),
        "price": round(num_of("trade"), 3),
        "change_pct": round(num_of("changepercent"), 2),
        "pe": round(num_of("per"), 2),
        "pb": round(num_of("pb"), 2),
        "total_cap": round(num_of("mktcap") / 10000, 2),   # 万元 -> 亿
        "float_cap": round(num_of("nmc") / 10000, 2),
        "turnover": round(num_of("turnoverratio"), 2),
        "amount": round(num_of("amount") / 10000, 2),      # 元 -> 万
        "volume": round(num_of("volume") / 100, 0),
        "open": round(num_of("open"), 3),
        "high": round(num_of("high"), 3),
        "low": round(num_of("low"), 3),
        "prev_close": round(num_of("settlement"), 3),
        "source": "sina",
    }


# ---------------------------------------------------------------------------
# 股票搜索（腾讯 smartbox）
# ---------------------------------------------------------------------------

def search_stock(keyword, limit=12):
    url = f"https://smartbox.gtimg.cn/s3/?q={requests.utils.quote(str(keyword))}&t=all"
    try:
        r = _get(url, encoding="gbk")
        m = re.search(r'"(.*)"', r.text, re.S)
        body = m.group(1) if m else ""
    except Exception:
        return []
    out = []
    for item in body.split("^"):
        parts = item.split("~")
        if len(parts) < 3:
            continue
        market, code, name = parts[0], parts[1], parts[2]
        if market not in ("sh", "sz", "bj", "hk"):
            continue
        kind = parts[4] if len(parts) > 4 else ""
        if kind and kind not in ("GP-A", "GP", "GP-B", "ETF", "KJ"):
            continue
        # smartbox 的名称可能是 \uXXXX 转义，统一解码
        try:
            name = name.encode().decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        out.append({
            "code": f"{market}{code}",
            "symbol": code,
            "name": name.replace(" ", ""),
            "market": market,
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 指数行情（大盘）
# ---------------------------------------------------------------------------

INDEX_LIST = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sz399905", "中证500"),
    ("bj899050", "北证50"),
]


def index_quotes():
    codes = [c for c, _ in INDEX_LIST]
    q = quote_tencent(codes)
    out = []
    for code, label in INDEX_LIST:
        d = q.get(code)
        if d:
            d["label"] = label
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# 股票池（本地内置，来自中证指数官网成分股，稳定不限流）
# ---------------------------------------------------------------------------

POOL_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "index_pools.json")


def load_pool():
    """加载内置指数成分股池，返回 {code: meta}"""
    try:
        with open(POOL_FILE, "r", encoding="utf-8") as f:
            pools = json.load(f)
    except Exception:
        return {}, {}
    members = {}
    meta = {}
    for idx_code, info in pools.items():
        meta[idx_code] = info.get("name", idx_code)
        for c in info.get("codes", []):
            members.setdefault(c, []).append(idx_code)
    return members, meta


def pool_rows(pool="all", max_workers=8):
    """
    用内置股票池 + 腾讯批量行情，生成与新浪同结构的数据（字段略少：无 PE/PB 时留 0）
    返回 list[dict]
    """
    members, meta = load_pool()
    if not members:
        return []

    sel = set()
    if pool and pool != "all":
        for key in pool.split(","):
            key = key.strip()
            for c, idxs in members.items():
                if key in idxs:
                    sel.add(c)
    else:
        sel = set(members.keys())

    codes = []
    for c in sel:
        codes.append(normalize(c))
    codes = sorted(set(codes))

    quotes = quote_tencent(codes)     # 内部已分批
    out = []
    for code, d in quotes.items():
        sym = d["symbol"]
        # 指数归属标签
        belong = meta_belong = members.get(sym, [])
        out.append({
            "code": code,
            "symbol": sym,
            "name": d["name"].replace(" ", ""),
            "price": d["price"],
            "change_pct": d["change_pct"],
            "pe": d["pe"],
            "pb": d["pb"],
            "total_cap": d["total_cap"],
            "float_cap": d["float_cap"],
            "turnover": d["turnover"],
            "amount": d["amount"],
            "volume": d["volume"],
            "open": d["open"],
            "high": d["high"],
            "low": d["low"],
            "prev_close": d["prev_close"],
            "pools": belong,
            "source": "tencent",
        })
    return out
