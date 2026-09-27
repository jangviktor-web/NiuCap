"""日线落库的每日自动调度。

为什么是这个形状（而不是 apscheduler / cron）：

  · 只要「每天到点跑一次 + 错过就补」，一个守护线程 + sleep 就是全部需求。
    引 apscheduler 是多一个依赖、多一层配置，换不来任何这里需要的东西。
  · cron 更稳，但要动服务器配置、且拿不到服务里的 config/日志。见 deploy.sh。

三个关键决定：

  1. 状态存 meta 表（last_run_date / last_ok_date / last_result）。
     存表而非存内存，服务重启后依然知道「今天跑过没」——漏跑补跑就靠它，
     不需要额外的补跑逻辑。
  2. 用「日期字符串不等」判断该不该跑，不用时间差。
     sleep 期间系统休眠 / 时钟漂移都不会漏掉或重复触发。
  3. 15:30 跑时当日 K 线可能还没结算完（upsert 幂等，次日会覆盖成正确的），
     所以这里只负责触发，不在调度层做数据完整性判断——
     完整性交给 sync_bars 自己的取数结果。
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any, Dict, Optional

# meta 表键名
K_LAST_TRY = "sync_bars_last_try"      # 最近一次尝试的日期（YYYY-MM-DD）
K_LAST_OK = "sync_bars_last_ok"        # 最近一次成功的日期
K_LAST_RESULT = "sync_bars_last_result"  # 最近一次结果摘要（给人看）
K_SKIP = "sync_bars_last_skip"         # 最近一次因非交易日跳过的日期

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _today() -> str:
    return _dt.date.today().isoformat()


def _now(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 状态读写

def state() -> Dict[str, Any]:
    """给管理页看的调度状态（不会触发任何实际任务）。"""
    import store as st
    today = _today()
    skip = st.meta_get(K_SKIP, "")
    return {
        "enabled": _enabled(),
        "at": _run_at(),
        "last_try": st.meta_get(K_LAST_TRY, ""),
        "last_ok": st.meta_get(K_LAST_OK, ""),
        "last_result": st.meta_get(K_LAST_RESULT, ""),
        "today": today,
        "today_done": st.meta_get(K_LAST_OK, "") == today,
        "skipped_today": skip == today,      # 今天判定为非交易日
        "skip_hint": skip,
        "running": _running(),
        "next": _next_run_text(),
    }


def _running() -> bool:
    """落库任务是否正在跑（复用 app 里的 _SYNC_STATE，避免两处状态打架）。"""
    try:
        import app
        return bool(app._SYNC_STATE.get("running"))
    except Exception:
        return False


def _next_run_text() -> str:
    """下一次触发时间。**只做估算**，跳过周末但不管节假日。

    真实执行与否由 _tick 里的交易日判断决定（要发网络请求，不适合放在
    这个纯展示函数里）。所以这里可能显示"周六"之外的工作日，遇到节假日
    会显示一个实际不会跑的时间——代价可接受，总比每次刷页面都发请求好。
    """
    if not _enabled():
        return "已关闭"
    hh, mm = _run_hm()
    now = _dt.datetime.now()
    tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tgt <= now:
        tgt += _dt.timedelta(days=1)
    while tgt.weekday() >= 5:        # 跳过周末；节假日不查（见 docstring）
        tgt += _dt.timedelta(days=1)
    sec = int((tgt - now).total_seconds())
    return f"{tgt.strftime('%m-%d %H:%M')}（{sec // 3600}h{sec % 3600 // 60}m 后）"


# ---------------------------------------------------------------- 配置

def _enabled() -> bool:
    try:
        import config
        return bool(config.get("TICK_SYNC_BARS_AUTO"))
    except Exception:
        return False


def _run_hm() -> tuple:
    """解析配置里的 "HH:MM"，脏值回落到 15:30。"""
    try:
        import config
        raw = str(config.get("TICK_SYNC_BARS_AT") or "15:30")
    except Exception:
        raw = "15:30"
    try:
        hh, mm = raw.split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        pass
    return 15, 30


def _run_at() -> str:
    hh, mm = _run_hm()
    return f"{hh:02d}:{mm:02d}"


# ---------------------------------------------------------------- 执行

def _do_sync(scope: str, count: int) -> Dict[str, Any]:
    """同步跑一次落库（阻塞）。返回结果摘要。

    sync_bars.sync_via_eltdx 返回 (ok, fail, total_bars, failed_list)，
    四个位置别弄错——第二个是【失败数】不是总数，曾因此把 300 只全成功
    显示成 "300/0"（看着像全军覆没）。
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    import sync_bars as sb

    codes = sb.pick_codes(scope, "")
    res = sb.sync_via_eltdx(codes, count, workers=6)
    ok = int(res[0]) if isinstance(res, tuple) and len(res) > 0 else 0
    fail = int(res[1]) if isinstance(res, tuple) and len(res) > 1 else 0
    bars = int(res[2]) if isinstance(res, tuple) and len(res) > 2 else 0
    # 键名用 synced 而不是 ok：run_now 要返回 {"ok": True/False} 表示成败，
    # 这里若也叫 ok（成功家数），**r 展开时会把成败标志覆盖成 5568 这种数字
    return {"scope": scope, "count": count, "synced": ok, "fail": fail,
            "bars": bars, "total": ok + fail}


def run_now(scope: str = "all", count: int = 250, manual: bool = False) -> Dict[str, Any]:
    """跑一次落库并记状态。manual=True 时无论成功与否都更新 last_try。

    失败不更新 last_ok——这样下一次 tick 会重试（自动补跑）。
    """
    import store as st

    if not _lock.acquire(blocking=False):
        return {"ok": False, "note": "另一次落库正在进行"}
    try:
        if _running():
            return {"ok": False, "note": "已有同步任务在跑，请等待完成"}
        t0 = time.time()
        st.meta_set(K_LAST_TRY, _today())
        try:
            r = _do_sync(scope, count)
            st.meta_set(K_LAST_OK, _today())
            msg = (f"{_now(t0)} → {_now(time.time())} · "
                   f"成功 {r['synced']}/{r['total']} 只"
                   + (f"（失败 {r['fail']}）" if r['fail'] else "")
                   + f" · {r['bars']} 条 · scope={r['scope']} · "
                   f"耗时 {int(time.time() - t0)}s")
            st.meta_set(K_LAST_RESULT, msg)
            print(f"[sync-bars] 完成: {msg}")
            # 落库只写了 SQLite，内存历史引擎还停在旧数据。不重载的话，
            # 体检/选股要等到服务重启才能看到今天的数据。
            try:
                import history as _hist
                _hist.reload_engine()
            except Exception as _e:
                print(f"[sync-bars] 历史引擎重载失败：{_e}")
            # 顺手预热策略体检缓存（后台线程，失败不影响落库状态）
            try:
                import strategy_eval as _se
                _se.prewarm(why="落库完成")
            except Exception as _e:
                print(f"[sync-bars] 体检预热启动失败：{_e}")
            return {"ok": True, **r, "elapsed": round(time.time() - t0, 1)}
        except Exception as e:
            msg = f"{_now(t0)} 失败: {type(e).__name__}: {e}"
            st.meta_set(K_LAST_RESULT, msg)
            print(f"[sync-bars] {msg}")
            return {"ok": False, "error": msg}
    finally:
        _lock.release()


# ---------------------------------------------------------------- 循环

def _loop():
    """每分钟醒一次，比日期。够用且不怕休眠/漂移。"""
    while not _stop.is_set():
        try:
            _tick()
        except Exception as e:
            print(f"[sync-bars] tick 异常（忽略，下一分钟重试）：{type(e).__name__}: {e}")
        # 用 wait 而不是 sleep，能被 stop 立刻打断
        _stop.wait(60)


#: 用来判断交易日历的锚点。上证指数每个交易日必有一根日线。
#: 交易日历锚点。不用一张逐年维护的节假日表——指数数据天然带着正确答案。
#: 给两个是因为单只指数偶尔抽风会给出错误答案，多一个就多一票。
CAL_ANCHORS = ("sh000001", "sz399001")

# 兼容旧引用
CAL_ANCHOR = CAL_ANCHORS[0]


def _day_via_snapshot(code: str, target: str) -> Optional[bool]:
    """源①：实时快照的行情时间戳。

    腾讯快照给 `time` 是**行情时间**而非请求时间（凌晨请求拿到的是上一交易日
    16:14，实测已确认），所以它天然回答"数据最新到哪一刻"。

    这一路最灵敏：15:30 跑时日线往往还没结算出当日那根，但快照早在 15:00
    收盘那一刻就已经带上今天的时间戳了。**它正是为了补日线的滞后而排在第一。**
    """
    try:
        import datasource as ds
        q = ds.quote_tencent([code], use_cache=False).get(code) or {}
    except Exception:
        return None
    tm = str(q.get("time") or "")
    if len(tm) < 8 or not tm[:8].isdigit():
        return None
    return tm[:8] == target.replace("-", "")


def _day_via_daily(code: str, target: str) -> Optional[bool]:
    """源②：日线最后一根的日期。最权威，但滞后（要等结算）。"""
    try:
        import datasource as ds
        kl = ds.get_kline(code, "1d", 3, use_cache=False)
    except Exception:
        return None
    if not kl:
        return None
    return str(kl[-1].get("date") or "")[:10] == target


def _day_via_intraday(code: str, target: str) -> Optional[bool]:
    """源③：分钟线最后一根。同样实时，用 eltdx/腾讯自己的降级链。"""
    try:
        import datasource as ds
        rows = ds.get_kline_intraday(code, "5m", 10, use_cache=False)
    except Exception:
        return None
    if not rows:
        return None
    return str(rows[-1].get("date") or "").startswith(target)


#: 判定链，按「最灵敏 → 最权威 → 兜底」排。
#: 快照排第一不是因为它最准，而是因为在 15:30 这个时间点它是**唯一必然
#: 已经更新**的那一个——日线此刻大概率还停在昨天。
_DAY_CHECKS = (
    (_day_via_snapshot, "快照"),
    (_day_via_daily, "日线"),
    (_day_via_intraday, "分钟线"),
)


def is_trading_day(now=None) -> Optional[bool]:
    """今天是不是交易日。True/False，判不出来时返回 None。

    **不维护节假日表**——问交易所自己的数据。节假日表要逐年更新（调休安排
    每年不同），而指数数据天然带着正确答案，零维护。

    ## 为什么必须多源交叉（这是一个真实踩到的坑）

    最早只用日线，逻辑是「最新日线 != 今天 → 非交易日」。这句话把三种
    完全不同的情况混为一谈：

        周末          → 该跳过   ✅
        节假日        → 该跳过   ✅
        盘后数据尚未出 → 该跳过   ❌ ← 那天明明是交易日

    每天 15:30 跑正好落在第三种：日线还没结算出当日那根，于是整天被静默
    跳过——**这是沉默失败，比报错更危险**。

    所以改成**一票肯定制**：任一数据源证明"今天有行情"，就是交易日；
    只有所有源都明确说"今天没数据"，才判非交易日。数据源不可能在非交易日
    凭空造出今天的数据，因此肯定票是可信的。

    None 的含义：所有源都取不到（网络异常）。此时**不要拦**——宁可白跑一次，
    也不能因为一次网络抖动就整天不落库。

    ## 已知天花板（ponytail）

    若某天快照接口恰好挂了，**且**日线与分钟线同时因滞后停在昨天，仍会误判
    成非交易日。要凑齐这个条件得三个源一起出问题（分钟线是实时的，交易日的
    15:30 必然有当天数据），概率已很低；真发生了也只是漏跑一天，下次跑会补。
    要彻底消除得引入一张节假日表，那是年年要更新的维护负担，暂不做：
    `[skip] → use trading_calendar, add when 有人愿意每年更新一次假期表。`
    """
    t = now or _dt.datetime.now()
    if t.weekday() >= 5:            # 周末不必发请求
        return False
    target = t.date().isoformat()

    answered = False               # 是否有任一源给出了明确答复
    for fn, name in _DAY_CHECKS:
        for code in CAL_ANCHORS:
            r = fn(code, target)
            if r is True:
                return True        # 一票肯定就够，立刻收工
            if r is False:
                answered = True
                break              # 这个源已经明确否定，换下一个源
            # None = 该源在这个锚点上答不上来，试试下一个锚点
        if answered:
            # 已有明确否定票，但先别急着判——后面的源可能给出肯定票吗？
            # 不会：不同源对同一天不可能一个有数据一个没有。
            # 但继续问没坏处（最多多一次请求），且能防止某源异常返回值。
            continue
    return False if answered else None


def _tick():
    import store as st

    if not _enabled():
        return
    today = _today()
    if st.meta_get(K_LAST_OK, "") == today:
        return                      # 今天已成功，什么都不做
    if st.meta_get(K_SKIP, "") == today:
        return                      # 今天已判定为非交易日，不再重复问指数

    now = _dt.datetime.now()
    hh, mm = _run_hm()
    if (now.hour, now.minute) < (hh, mm):
        return                      # 还没到点

    # 非交易日不跑（周末/节假日）。判不出来（None）时按交易日处理——
    # 宁可白跑一次，也不要因一次网络抖动整天不落库。
    td = is_trading_day(now)
    if td is False:
        # 用独立的 skip 键记录，不写 last_ok——避免"最近成功"显示成非交易日，
        # 也免得混淆「跑成功」与「不需要跑」两种语义。
        # 记下来是为了之后每分钟不再重复去问指数。
        st.meta_set(K_SKIP, today)
        print("[sync-bars] 今天非交易日（周末/节假日），跳过")
        return

    # 到点了，且今天没成功过 → 跑（含服务重启后的补跑）
    print(f"[sync-bars] 到点 {_run_at()}，开始全市场落库（今日未成功）")
    try:
        import config
        scope = str(config.get("TICK_SYNC_BARS_SCOPE") or "all")
        count = int(config.get("TICK_SYNC_BARS_COUNT") or 250)
    except Exception:
        scope, count = "all", 250
    run_now(scope, count)


def start():
    """幂等启动。已启动则直接返回。"""
    global _thread
    if _thread and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="sync-bars-daily")
    _thread.start()
    print(f"[sync-bars] 每日调度已启动：{'开启' if _enabled() else '关闭'}"
          f" · 时间 {_run_at()}")
    return True


def stop():
    _stop.set()


# ---------------------------------------------------------------- 自检

def selfcheck() -> int:
    """跨日 / 未到点 / 已跑过 / 关闭 四种判断，纯逻辑不碰网络。

    ponytail: 用独立前缀 cfgselftest.* 做读写验证，绝不碰真实的 cfg.* ——
    自检跑一次就把生产配置清了（真实踩过：管理页刚设的开关被自检 reset 掉）。
    """
    import store as st

    fails = []
    T = "cfgselftest."

    def eq(got, want, label):
        if got == want:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}: got={got!r} want={want!r}")
            fails.append(label)

    def check(cond, good, bad):
        if cond:
            print(f"  ✅ {good}")
        else:
            print(f"  ❌ {bad}")
            fails.append(bad)

    # 借用真实 key 的校验器
    import config as _c

    def _validator(key):
        return _c._validator(key)

    # 自检要验证「env 层」的行为，但真实 meta 里的值优先级更高会盖住它。
    # 先把生产 meta 值摘下来存着，跑完原样放回去——绝不留痕。
    saved_meta = {k: st.meta_get("cfg." + k) for k in
                  ("TICK_SYNC_BARS_AT", "TICK_SYNC_BARS_AUTO")}
    for k in saved_meta:
        st.meta_del("cfg." + k)
    _c.invalidate()

    def _restore():
        for k, v in saved_meta.items():
            if v is None:
                st.meta_del("cfg." + k)
            else:
                st.meta_set("cfg." + k, v)
        _c.invalidate()

    print("[1] 时间解析")
    st.meta_del(T + "TICK_SYNC_BARS_AT")
    import os as _os
    _os.environ.pop("TICK_SYNC_BARS_AT", None)
    _c.invalidate()
    eq(_run_at(), "15:30", "默认 15:30")
    _os.environ["TICK_SYNC_BARS_AT"] = "17:45"
    _c.invalidate()
    eq(_run_at(), "17:45", "环境变量生效 17:45")
    _os.environ["TICK_SYNC_BARS_AT"] = "99:99"
    _c.invalidate()
    eq(_run_at(), "15:30", "脏值回落 15:30")
    _os.environ.pop("TICK_SYNC_BARS_AT", None)
    _c.invalidate()
    eq(_run_at(), "15:30", "清理后回默认")

    print("\n[2] 开关")
    _os.environ.pop("TICK_SYNC_BARS_AUTO", None)
    _c.invalidate()
    eq(_enabled(), False, "默认关闭")
    _os.environ["TICK_SYNC_BARS_AUTO"] = "1"
    _c.invalidate()
    eq(_enabled(), True, "开启")
    _os.environ.pop("TICK_SYNC_BARS_AUTO", None)
    _c.invalidate()
    eq(_enabled(), False, "复位回关闭")

    print("\n[3] 跨日判断（核心：漏跑补跑）")
    st.meta_del(T + K_LAST_OK)
    eq(st.meta_get(T + K_LAST_OK, "") == _today(), False, "无记录 → 需要跑")
    st.meta_set(T + K_LAST_OK, _today())
    eq(st.meta_get(T + K_LAST_OK, "") == _today(), True, "今天已成功 → 跳过")
    st.meta_set(T + K_LAST_OK, "2000-01-01")
    eq(st.meta_get(T + K_LAST_OK, "") == _today(), False, "隔天旧记录 → 需要跑")
    st.meta_del(T + K_LAST_OK)

    print("\n[4] 到点判断")
    hh, mm = _run_hm()
    early = _dt.datetime.now().replace(hour=(hh - 1) % 24, minute=mm)
    eq((early.hour, early.minute) < (hh, mm), True, "早于设定时间 → 不跑")

    print("\n[4.1] 交易日判断")
    # 周末必须 False，且**不发网络请求**（短路）
    sat = _dt.datetime(2026, 9, 26, 15, 30)     # 周六
    sun = _dt.datetime(2026, 9, 27, 15, 30)     # 周日
    eq(is_trading_day(sat), False, "周六 → 非交易日")
    eq(is_trading_day(sun), False, "周日 → 非交易日")
    # 工作日要看指数数据，取不到时返回 None（调用方按"跑"处理，不拦）
    wed = _dt.datetime(2026, 9, 23, 15, 30)
    r = is_trading_day(wed)
    check(r in (True, False, None),
          f"工作日判断返回 {r}（True/False/None 均合法）",
          f"工作日判断返回了非法值: {r!r}")

    # ---------------------------------------------------------------
    # 多源交叉：**锁死那个被真实踩到的 bug**
    #
    # 每天 15:30 跑时日线常常还没结算出当日那根，单看日线会被误判成
    # "非交易日"而整天跳过——那天明明是交易日。这组用例用注入数据源的
    # 方式复刻该场景，因为真实数据要等到明天 15:30 才测得出来，
    # 而回归测试必须今天就能跑。
    # ---------------------------------------------------------------
    print("\n[4.1b] 多源交叉判定（注入数据源，覆盖 15:30 那个边界）")
    import datasource as _ds
    NOON = _dt.datetime(2026, 9, 23, 15, 30)      # 一个工作日
    TGT = "2026-09-23"

    orig = {"quote": _ds.quote_tencent, "kline": _ds.get_kline,
            "intra": _ds.get_kline_intraday}

    def _fake(snap=None, daily=None, intra=None):
        """造三个假的返回：snap/daily/intra 各自为 True=今天有数据,
        False=今天没数据, None=该源挂了（抛异常）。"""
        def q(codes, use_cache=True):
            if snap is None:
                raise RuntimeError("snapshot down")
            t = (TGT if snap else "20260922").replace("-", "") + "150000"
            c = (codes or ["sh000001"])[0]
            return {c: {"code": c, "price": 1.0, "time": t}}
        def k(code, period="1d", count=250, use_cache=True):
            if daily is None:
                raise RuntimeError("daily down")
            d = TGT if daily else "2026-09-22"
            return [{"date": d, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0,
                     "volume": 1.0}]
        def i(code, period="5m", count=None, use_cache=True):
            if intra is None:
                raise RuntimeError("intraday down")
            d = TGT if intra else "2026-09-22"
            return [{"date": d + " 15:00", "open": 1.0, "close": 1.0,
                     "high": 1.0, "low": 1.0, "volume": 1.0}]
        return q, k, i

    def with_sources(snap, daily, intra):
        q, k, i = _fake(snap, daily, intra)
        _ds.quote_tencent, _ds.get_kline, _ds.get_kline_intraday = q, k, i
        try:
            return is_trading_day(NOON)
        finally:
            _ds.quote_tencent = orig["quote"]
            _ds.get_kline = orig["kline"]
            _ds.get_kline_intraday = orig["intra"]

    # ★ 核心场景：15:30，日线还没出（停在昨天），但快照已经带上今天 → 交易日
    eq(with_sources(True, False, True), True,
       "日线未出但快照已更新 → 交易日（★被修掉的那个 bug）")
    # 三个源都说今天没数据 → 才是真的非交易日
    eq(with_sources(False, False, False), False, "三源一致说没数据 → 非交易日")
    # 任一源挂了但剩下的能给出肯定 → 仍是交易日
    eq(with_sources(True, None, None), True, "只剩快照可用且说到今天 → 交易日")
    eq(with_sources(None, True, None), True, "只剩日线可用且说到今天 → 交易日")
    eq(with_sources(None, None, True), True, "只剩分钟线可用且说到今天 → 交易日")
    # 全部挂掉 → None（照跑，不拦）
    eq(with_sources(None, None, None), None, "全部源挂掉 → None（照跑，不拦）")
    # 部分源明确否定、部分挂掉 → 按已有否定票判 False
    eq(with_sources(None, False, False), False, "部分源否定+部分源挂 → 非交易日")
    print("     确认：数据源恢复原状 →",
          _ds.quote_tencent is orig["quote"] and _ds.get_kline is orig["kline"]
          and _ds.get_kline_intraday is orig["intra"])

    print("\n[4.2] 非交易日跳过：短路条件与语义")
    # 复刻 _tick 里那两个短路判断（不真调 _tick——今天是交易日时它会真跑落库）
    store = {"last_ok": "", "skip": ""}

    def would_return_early():
        today = _today()
        if store["last_ok"] == today:
            return "already_ok"
        if store["skip"] == today:
            return "skipped"
        return ""

    eq(would_return_early(), "", "两个键都空 → 继续走判断")
    store["skip"] = _today()
    eq(would_return_early(), "skipped", "skip 命中 → 直接返回，不再问指数")
    store["skip"] = ""
    store["last_ok"] = _today()
    eq(would_return_early(), "already_ok", "last_ok 命中 → 直接返回")
    eq(K_SKIP != K_LAST_OK, True, "skip 与 last_ok 是两把独立的钥匙（语义不混）")

    print("\n[5] 校验器（新类型）")
    for bad in ("25:00", "15-30", "abc", ""):
        try:
            _validator("TICK_SYNC_BARS_AT")(bad)
            fails.append(f"TICK_SYNC_BARS_AT={bad} 应被拒")
            print(f"  ❌ 应拒绝 {bad!r}")
        except ValueError:
            print(f"  ✅ 拒绝 {bad!r}")
    eq(_validator("TICK_SYNC_BARS_AT")("9:05"), "09:05", "时间补零归一")
    eq(_validator("TICK_SYNC_BARS_SCOPE")("ALL"), "all", "股票池小写归一")
    for bad in ("../etc/passwd", "all; drop", ""):
        try:
            _validator("TICK_SYNC_BARS_SCOPE")(bad)
            fails.append(f"SCOPE={bad} 应被拒")
            print(f"  ❌ 应拒绝 {bad!r}")
        except ValueError:
            print(f"  ✅ 拒绝 {bad!r}")

    print("\n[6] state() 可读")
    s = state()
    for k in ("enabled", "at", "last_ok", "today", "next"):
        if k not in s:
            fails.append(f"state() 缺 {k}")
            print(f"  ❌ state() 缺 {k}")
    if not fails:
        print(f"  ✅ state() 字段齐全 · at={s['at']} next={s['next']}")

    print("\n[7] sync_via_eltdx 返回值位置（曾把 fail 当 total，显示成 300/0）")
    import inspect, sys as _s, os as _o
    _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(
        _o.path.abspath(__file__))), "scripts"))
    import sync_bars as _sb
    doc = inspect.getdoc(_sb.sync_via_eltdx) or ""
    if "返回 (ok, fail, total_bars, failed_list)" in doc:
        print("  ✅ 上游文档声明 (ok, fail, total_bars, failed_list)")
    else:
        print(f"  ⚠ 上游返回值文档已变，请核对 scheduler._do_sync：{doc[:60]}")
    src_sync = inspect.getsource(_do_sync)
    if '"fail": fail' in src_sync and "res[1]" in src_sync:
        print("  ✅ _do_sync 正确取 res[1] 作为 fail")
    else:
        fails.append("_do_sync 未把 res[1] 当 fail")
        print("  ❌ _do_sync 返回值解析有误")

    print("\n[8] 自检不污染生产配置")
    leftover = [k for k in (st.meta_all(T) or {})]
    eq(leftover, [], "无残留临时键")
    _restore()
    after = {k: st.meta_get("cfg." + k) for k in saved_meta}
    eq(after, saved_meta, "生产配置已原样还原")

    print("\n" + "=" * 50)
    if fails:
        print(f"❌ {len(fails)} 项未通过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/server")
    raise SystemExit(selfcheck())
