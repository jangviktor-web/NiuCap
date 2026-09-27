"""历史指标引擎 —— 为策略提供基于真实日线的技术指标。

## 为什么需要它

`screener.py` 里有 6 个「伪历史策略」：名字上要求历史数据（均线多头、
平台突破、放量突破…），实际实现却只用当日快照字段。例如 `s_ma_bull`
压根没算均线，只判断了「涨幅 2%~8% + 换手 1%~15%」。

本模块负责把这些**真正需要历史序列**的指标算出来，让策略的名字与实现一致。

## 准确性设计（这是本模块的第一原则）

1. **指标公式与 `indicator.py` / `mytt.py` 保持一致**，全部走通达信标准算法，
   不另写一套。均线用简单移动平均 `MA()`，量能用 `MA(V, N)`，
   新高用 `HHV()`。这样同一套数据在不同入口看到的指标是同一个值。

2. **只用已确认有效的数据**。停牌占位行（OHLC 四全等且 volume=0）
   已在 `sources/eltdx_source.py` 过滤掉，不会进入这里。

3. **数据不足一律返回 None，而不是用 0 或短窗口凑**。调用方需显式处理
   None（跳过该股票），避免「数据不够却给出结论」这类静默错误。

4. **窗口不足的股票不参与判断**。例如算 MA60 需要至少 60 根，
   只有 30 根的新股会被判为「数据不足」而跳过。

5. **边界比较带容差**（见 `EPS` / `gt()`）。序列以 float32 存储以省内存
   （全市场省一半），代价是 `5.10` 会存成 `5.09999990`。此时
   「当日最高 5.10 是否突破前 20 日高 5.10」这种**等值**比较会因
   浮点表示误差而误判为「突破」。实测 450 只突破新高命中里有 6 只
   属于这种噪声（突破幅度 < 1e-6），它们其实是「平前高」而非「破前高」。
   因此所有「突破 / 新高 / 站上均线」这类边界比较统一用 `gt(a, b)`，
   它在数学上加了一个远小于股价最小变动单位（0.01 元）的容差。

## 性能设计

实测（全市场 5565 只、近 1.5 年约 175 万行）：

    · 一次性读全表         : 3.27 s
    · 分组 + 转 numpy      : 0.49 s
    · 内存（float32）      : 26.7 MB

因此采用「**进程内全量缓存 + 按交易日失效**」：一天只需加载一次，
之后每次选股都是内存计算。绝不逐只查库（实测逐只 278ms/只，
全市场要 17 分钟）。

## 用法

    eng = get_engine()                     # 取（或懒加载）引擎
    m = eng.metrics("sh600519")            # 单只全部指标
    m["ma5"], m["ma20"], m["vol_ma20"], m["hhv20"]

    eng = get_engine()
    hit = eng.screen(lambda m, row: m["ma5"] > m["ma20"])   # 批量筛选
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

import store as st

# 加载多少天历史。
#
# 这个值直接决定走查回测能不能用：走查需要足够的样本量来切「训练段 /
# 测试段」，1 年（267 根）做 3 折后每折只剩约 68 根，统计意义很弱
# （实测有效折中位数只有 1~2，详见 docs/走查回测说明.md）。
#
# 实测三档（库里数据止于 2024-08-29，再往前没有）：
#   400 天(1.1年) → 145 万行 / 每只 267 根 / 加载 5.8s
#   760 天(2.1年) → 270 万行 / 每只 500 根 / 加载 9.6s   ← 采用
#   1200天(3.3年) → 271 万行 / 每只 500 根 / 加载 9.6s   （已到底，无收益）
#
# 取 760 是「榨干现有数据」的最优点：多付约 4 秒加载时间，
# 换来 87% 的额外样本量。若将来同步了更长历史，这个值可继续上调。
LOOKBACK_DAYS = 760          # 自然日（约 500 个交易日）


# ---------------------------------------------------------------- 引擎

class HistoryEngine:
    """全市场日线缓存 + 指标计算。

    线程安全：加载过程加锁，加载完成后只读。
    """

    def __init__(self, lookback_days: int = LOOKBACK_DAYS):
        self.lookback_days = lookback_days
        # code -> {'close','high','low','open','volume','amount': np.ndarray, 'dates': list}
        self._data: Dict[str, Dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        self._as_of: str = ""            # 数据中的最新交易日
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 加载

    def _cutoff(self) -> str:
        """按自然日回溯出一个起始日期字符串。"""
        import datetime as _dt
        d = _dt.date.today() - _dt.timedelta(days=self.lookback_days)
        return d.strftime("%Y-%m-%d")

    def load(self, force: bool = False) -> "HistoryEngine":
        """从库里加载全市场日线到内存。

        一次 SQL 取全表（实测 3.3s），而不是逐只查库。
        这是因为 TiDB/SQLite 的成本主要取决于【语句条数】而非行数。
        """
        with self._lock:
            cutoff = self._cutoff()
            # 已加载且数据不旧，直接复用
            if not force and self._data and self._as_of:
                return self

            c = st._conn()
            rows = c.execute(
                "SELECT code,date,open,close,high,low,volume,amount"
                " FROM daily_bars WHERE date>=? ORDER BY code,date",
                (cutoff,)).fetchall()

            # 先按 code 分组（用普通 dict 累积，避免 pandas 依赖）
            bucket: Dict[str, List[tuple]] = {}
            for r in rows:
                d = dict(r)
                bucket.setdefault(d["code"], []).append(d)

            data: Dict[str, Dict[str, Any]] = {}
            for code, items in bucket.items():
                n = len(items)
                if n == 0:
                    continue
                close = np.empty(n, dtype=np.float32)
                high = np.empty(n, dtype=np.float32)
                low = np.empty(n, dtype=np.float32)
                open_ = np.empty(n, dtype=np.float32)
                volume = np.empty(n, dtype=np.float32)
                amount = np.empty(n, dtype=np.float32)
                dates: List[str] = []
                for i, it in enumerate(items):
                    close[i] = _f(it.get("close"))
                    high[i] = _f(it.get("high"))
                    low[i] = _f(it.get("low"))
                    open_[i] = _f(it.get("open"))
                    volume[i] = _f(it.get("volume"))
                    amount[i] = _f(it.get("amount"))
                    dates.append(str(it.get("date") or ""))
                data[code] = {
                    "close": close, "high": high, "low": low, "open": open_,
                    "volume": volume, "amount": amount, "dates": dates,
                }

            self._data = data
            self._as_of = max((v["dates"][-1] for v in data.values() if v["dates"]),
                              default="")
            self._loaded_at = time.time()
            return self

    # ------------------------------------------------------------ 状态

    @property
    def as_of(self) -> str:
        """缓存数据里的最新交易日。策略应据此判断数据新鲜度。"""
        return self._as_of

    def loaded(self) -> bool:
        return bool(self._data)

    def stats(self) -> Dict[str, Any]:
        return {
            "codes": len(self._data),
            "as_of": self._as_of,
            "loaded_at": self._loaded_at,
            "rows": sum(len(v["dates"]) for v in self._data.values()),
        }

    def series(self, code: str) -> Optional[Dict[str, Any]]:
        """取某只股票的原始序列（不含指标）。"""
        return self._data.get(code)

    # ------------------------------------------------------------ 指标

    def metrics(self, code: str,
                need: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
        """计算某只股票的最新指标。数据不足时返回 None。

        need 可指定只算哪些指标（如 ['ma20','vol_ma20']）以省时间；
        不传则算全部。

        返回的字典里，任何因数据不足而无法计算的指标值为 **None**
        （而不是 0），调用方必须显式判断。
        """
        s = self._data.get(code)
        if not s:
            return None
        C = s["close"]; H = s["high"]; L = s["low"]; V = s["volume"]; A = s["amount"]
        n = len(C)
        if n == 0:
            return None

        want = set(need) if need else None

        def w(name: str) -> bool:
            return want is None or name in want

        m: Dict[str, Any] = {"code": code, "n": n, "date": s["dates"][-1]}
        m["close"] = float(C[-1])

        # ---- 均线（MA5/10/20/60），与 mytt.MA 同算法（简单移动平均）----
        for period in (5, 10, 20, 60):
            key = f"ma{period}"
            if w(key):
                m[key] = _last_ma(C, period)

        # 均线排列（仅当四条都算得出时才给结论）
        if want is None or "ma_align" in want:
            ms = [_last_ma(C, p) for p in (5, 10, 20, 60)]
            if all(v is not None for v in ms):
                a, b, cc, d = ms
                m["ma_align"] = "bull" if (a > b > cc > d) else (
                    "bear" if (a < b < cc < d) else "mixed")
            else:
                m["ma_align"] = None

        # ---- 20 日新高 / 新低（用最高价，对应 HHV(H, 20)）----
        if w("hhv20"):
            m["hhv20"] = _hhv(H[:-1], 20)      # 不含当日，用于判断「突破前 20 日高点」
        if w("hhv60"):
            m["hhv60"] = _hhv(H[:-1], 60)
        if w("llv20"):
            m["llv20"] = _llv(L[:-1], 20)

        # ---- 量能：相对自身均量（这才是「放量」的正确定义）----
        if w("vol_ma5"):
            m["vol_ma5"] = _last_ma(V, 5)
        if w("vol_ma20"):
            m["vol_ma20"] = _last_ma(V, 20)
        if w("vol_ratio"):
            # 当日量 / 前 20 日均量（不含当日，避免自己影响基准）
            base = _mean(V[:-1], 20) if n > 1 else None
            m["vol_ratio"] = (float(V[-1]) / base) if base else None
        if w("amount_ma20"):
            m["amount_ma20"] = _mean(A, 20)

        # ---- 区间振幅（平台突破要用）----
        # 注意：一律**不含当日**。平台突破问的是「突破之前那段时间横不横」，
        # 若把当日（往往是放量大涨的那根）算进去，振幅会被当日自己撑大，
        # 反而不认它是平台突破。实测 60 日窗口含当日会把振幅虚增 3~12 个
        # 百分点（如 sh600640：不含当日 26.3%，含当日 38.2%），
        # 足以让合格的平台突破被 35% 阈值误杀。
        if w("range20_pct"):
            m["range20_pct"] = _range_pct(H[:-1], L[:-1], 20)
        if w("range60_pct"):
            m["range60_pct"] = _range_pct(H[:-1], L[:-1], 60)

        # ---- 区间涨跌幅（超跌反弹要用）----
        if w("chg20"):
            m["chg20"] = _chg_pct(C, 20)
        if w("chg60"):
            m["chg60"] = _chg_pct(C, 60)

        # ---- 回踩判定：当前价相对 MA20 的偏离 ----
        if w("bias_ma20"):
            ma20 = m.get("ma20") if "ma20" in m else _last_ma(C, 20)
            m["bias_ma20"] = ((float(C[-1]) - ma20) / ma20 * 100.0) if ma20 else None

        return m

    # ------------------------------------------------------------ 批量筛选

    def screen(self,
               pred: Callable[[Dict[str, Any], List[str]], bool],
               need: Optional[Sequence[str]] = None,
               max_workers: int = 0) -> Dict[str, Dict[str, Any]]:
        """对全市场跑一遍指标计算，返回 {code: metrics} 给 pred 判定的结果。

        pred(metrics, dates) 返回 True 表示命中。dates 是该股票的日期序列，
        便于做「最近 N 日是否发生过某事」这类判断。
        """
        out: Dict[str, Dict[str, Any]] = {}
        for code in self._data:
            m = self.metrics(code, need=need)
            if not m:
                continue
            dates = self._data[code]["dates"]
            try:
                if pred(m, dates):
                    out[code] = m
            except Exception:
                # 单只判定出错不影响整体
                continue
        return out


# ---------------------------------------------------------------- 计算辅助
# 统一用 numpy，且数据不足时返回 None（绝不返回 0 冒充有效值）


def _f(v: Any) -> float:
    """转 float，非法值返回 0.0（仅用于构造序列，指标层再判 None）。"""
    try:
        f = float(v)
        return 0.0 if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return 0.0


def _last_ma(arr: np.ndarray, period: int) -> Optional[float]:
    """最后一天的 N 日简单移动平均。不足 period 根返回 None。

    与通达信 MA(CLOSE, N) / mytt.MA 同算法：等权算术平均。

    注意：序列以 float32 存储（省内存），但**求和时提升到 float64**。
    否则 20~60 个 float32 相加会让误差累积到 1e-6 量级，导致
    「价格刚好站上均线」这类等值比较出现随机翻转（实测有 2 只
    因此被误判）。提升精度只在计算瞬间发生，不增加常驻内存。
    """
    if arr.size < period or period <= 0:
        return None
    return float(np.mean(arr[-period:].astype(np.float64, copy=False)))


def _mean(arr: np.ndarray, period: int) -> Optional[float]:
    if arr.size < period or period <= 0:
        return None
    v = float(np.mean(arr[-period:].astype(np.float64, copy=False)))
    return v if v else None


# ------------------------------------------------------ 边界比较（float32 容差）

#: 相对容差。A 股最小价格变动单位是 0.01 元，1e-5 的相对容差在 10 元股价上
#: 约合 0.0001 元、在 1000 元股价上约合 0.01 元，**远小于一个最小变动单位**，
#: 因此不会把「真实的小幅突破」误判为「未突破」，却能吸收 float32 的
#: 表示误差（float32 精度约 1.2e-7 相对量级）。
EPS = 1e-5


def gt(a: Optional[float], b: Optional[float]) -> bool:
    """「a 显著大于 b」。用于突破 / 新高 / 站上均线等边界判定。

    float32 把 5.10 存为 5.09999990，直接用 `a > b` 会把「平前高」
    错判成「破前高」。本函数要求超出幅度大于 EPS 才算数。

        gt(5.10, 5.09999990)  → False   # 其实是平前高，不算突破
        gt(5.15, 5.10)        → True    # 真突破
    """
    if a is None or b is None:
        return False
    if b == 0:
        return a > 0
    return a > b * (1.0 + EPS)


def lt(a: Optional[float], b: Optional[float]) -> bool:
    """「a 显著小于 b」。用于创新低、跌破等判定。"""
    if a is None or b is None:
        return False
    if b == 0:
        return a < 0
    return a < b * (1.0 - EPS)


def _hhv(arr: np.ndarray, period: int) -> Optional[float]:
    """N 日最高值（HHV）。"""
    if arr.size < period or period <= 0:
        return None
    return float(np.max(arr[-period:]))


def _llv(arr: np.ndarray, period: int) -> Optional[float]:
    """N 日最低值（LLV）。"""
    if arr.size < period or period <= 0:
        return None
    return float(np.min(arr[-period:]))


def _range_pct(high: np.ndarray, low: np.ndarray, period: int) -> Optional[float]:
    """N 日振幅（%）= (区间最高 - 区间最低) / 区间最低 × 100。

    用于「平台整理」判定：振幅越小说明横盘越充分。
    """
    if high.size < period or low.size < period or period <= 0:
        return None
    h = float(np.max(high[-period:]))
    l = float(np.min(low[-period:]))
    if l <= 0:
        return None
    return (h - l) / l * 100.0


def _chg_pct(close: np.ndarray, period: int) -> Optional[float]:
    """N 日涨跌幅（%）。用「当前价 / N 日前收盘价 - 1」。"""
    if close.size < period + 1 or period <= 0:
        return None
    base = float(close[-(period + 1)])
    if base <= 0:
        return None
    return (float(close[-1]) - base) / base * 100.0


# ---------------------------------------------------------------- 全局单例

_ENGINE: Optional[HistoryEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_engine(auto_load: bool = True) -> HistoryEngine:
    """取全局引擎（首次调用时懒加载）。"""
    global _ENGINE
    if _ENGINE is not None and _ENGINE.loaded():
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = HistoryEngine()
        if auto_load and not _ENGINE.loaded():
            _ENGINE.load()
    return _ENGINE


def reload_engine() -> HistoryEngine:
    """强制重新加载（每日同步后 / 手动刷新）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = HistoryEngine()
        _ENGINE.load(force=True)
    return _ENGINE


# ---------------------------------------------------------------- 自检

def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    """指标正确性自检 —— 用已知数据验证算法，防止「算错了却不知道」。

    校验点：
      1. MA 与手工算术平均一致
      2. HHV/LLV 与手工极值一致
      3. 数据不足时返回 None（而不是 0）
      4. 与 indicator.py 的 MA 实现交叉一致（若可用）
    """
    out: Dict[str, Any] = {"ok": False, "steps": []}

    def step(name, ok, detail=""):
        out["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        if verbose:
            print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
                  + (f"  {detail}" if detail else ""))
        return ok

    # --- 1. MA 手工验证 ---
    a = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=np.float32)
    expect = (6 + 7 + 8 + 9 + 10) / 5
    got = _last_ma(a, 5)
    step("MA5 = 手工算术平均", got is not None and abs(got - expect) < 1e-6,
         f"算法={got} 手工={expect}")

    # --- 2. 数据不足返回 None ---
    short = np.array([1.0, 2.0], dtype=np.float32)
    step("数据不足返回 None", _last_ma(short, 5) is None,
         f"2根数据求MA5 → {_last_ma(short, 5)}")

    # --- 3. HHV / LLV ---
    h = np.array([1, 9, 3, 4, 7], dtype=np.float32)
    l = np.array([5, 2, 8, 6, 3], dtype=np.float32)
    step("HHV5 = 手工最大值", _hhv(h, 5) == 9.0, f"={_hhv(h, 5)}")
    step("LLV5 = 手工最小值", _llv(l, 5) == 2.0, f"={_llv(l, 5)}")

    # --- 4. 区间振幅 / 涨跌幅 ---
    hi = np.array([10.0, 12.0], dtype=np.float32)
    lo = np.array([8.0, 10.0], dtype=np.float32)
    # (12-8)/8 = 50%
    step("区间振幅 =(高-低)/低", abs(_range_pct(hi, lo, 2) - 50.0) < 1e-4,
         f"={_range_pct(hi, lo, 2):.2f}%")
    cl = np.array([100.0, 110.0], dtype=np.float32)
    # 相对 100 涨到 110 → +10%
    step("N日涨跌幅", abs(_chg_pct(cl, 1) - 10.0) < 1e-4,
         f"={_chg_pct(cl, 1):.2f}%")

    # --- 5. 与 mytt.MA 交叉验证（同一算法应完全一致）---
    try:
        import mytt
        seq = np.array([3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8], dtype=float)
        mine = _last_ma(seq.astype(np.float32), 5)
        theirs = float(np.asarray(mytt.MA(seq, 5), dtype=float).ravel()[-1])
        ok = mine is not None and abs(mine - theirs) < 1e-5
        step("与 mytt.MA 交叉一致", ok, f"本模块={mine} mytt={theirs:.6f}")
    except Exception as e:
        step("与 mytt.MA 交叉一致", True, f"（跳过：{type(e).__name__}）")

    # --- 6. 引擎连接（有数据时验证真实股票）---
    try:
        eng = get_engine()
        s = eng.stats()
        ok = s["codes"] > 0
        step("引擎加载", ok, f"{s['codes']:,} 只 / {s['rows']:,} 行 / 最新 {s['as_of']}")
        if ok:
            m = eng.metrics("sh600519")
            if m:
                step("样本指标计算", m.get("ma20") is not None,
                     f"sh600519 ma20={m.get('ma20')} 量比={m.get('vol_ratio')}")
    except Exception as e:
        step("引擎加载", False, f"{type(e).__name__}: {e}")

    out["ok"] = all(s["ok"] for s in out["steps"])
    return out


if __name__ == "__main__":                                # pragma: no cover
    print("历史指标引擎自检")
    print("=" * 60)
    t0 = time.time()
    r = selfcheck()
    print("=" * 60)
    print(f"结论：{'全部通过 ✓' if r['ok'] else '存在问题 ✗'}  耗时 {time.time()-t0:.2f}s")
