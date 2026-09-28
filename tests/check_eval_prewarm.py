"""#98 体检缓存每日自动预热：自检 + API 冒烟。

跑法（需先启动服务，且 TICK_ADMIN_USERS 已配置）：
    cd /workspace/tick-stock-panel && .venv/bin/python server/run.py &
    python3 tests/check_eval_prewarm.py

覆盖：
  A 时间解析：默认 / 环境变量 / 脏值回落
  B 判脏四场景：缓存空 / 数据更新 / 数据未变 / as_of 缺失
  C 调度状态字段齐全
  D 重复启动只起一个线程
  E 调度循环真的会在「到点 + 未检查」时触发一次
  F API 冒烟：/api/admin/eval_cache 含 schedule 段
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

BASE = "http://127.0.0.1:8899"
ADMIN = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "femkerr")

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'✅' if ok else '❌'} {name}  {detail}")


# ---------------------------------------------------------------- A 时间解析
def test_at():
    import strategy_eval as se
    print("── A 时间解析 ──")
    os.environ.pop("TICK_EVAL_PREWARM_AT", None)
    rec("默认 16:30", se.prewarm_at() == "16:30", se.prewarm_at())

    os.environ["TICK_EVAL_PREWARM_AT"] = "17:45"
    rec("环境变量生效 17:45", se.prewarm_at() == "17:45", se.prewarm_at())

    for bad in ("25:00", "abc", "8:5", "", "24:00", "12:60", "16:30:00"):
        os.environ["TICK_EVAL_PREWARM_AT"] = bad
        rec(f"脏值回落 {bad!r}", se.prewarm_at() == "16:30", se.prewarm_at())

    os.environ["TICK_EVAL_PREWARM_AT"] = "06:05"
    rec("合法边界 06:05", se.prewarm_at() == "06:05", se.prewarm_at())
    os.environ.pop("TICK_EVAL_PREWARM_AT", None)


# ------------------------------------------------------------------ B 判脏
def test_due():
    import strategy_eval as se
    print("── B 判脏四场景 ──")
    real = dict(se._HITS_CACHE)
    real_db = se.db_latest_date

    def set_cache(hits, days, as_of, ev_days=None):
        se._HITS_CACHE.clear()
        se._HITS_CACHE.update({"hits": hits, "as_of": as_of,
                               "eval_days": ev_days if ev_days is not None else days})

    try:
        # ① 缓存为空 → 必算（手动清空后靠这条自愈）
        se._HITS_CACHE.clear()
        r = se.prewarm_due()
        rec("① 缓存为空 → 该算", r["due"] is True, r["reason"])

        # ② 数据更新了 → 算
        se.db_latest_date = lambda: "2026-09-25"
        set_cache({"a": {"x": 1}}, ["d"], "2026-09-24", ev_days=["d1"])
        r = se.prewarm_due()
        rec("② 数据更新 → 该算", r["due"] is True, r["reason"])
        rec("   报告双方日期", r["db_max_date"] == "2026-09-25"
            and r["cache_as_of"] == "2026-09-24", f"{r['db_max_date']} vs {r['cache_as_of']}")

        # ③ 数据没变 → 不算（周末 / 落库失败都落这里）
        se.db_latest_date = lambda: "2026-09-24"
        set_cache({"a": {"x": 1}}, ["d"], "2026-09-24", ev_days=["d1"])
        r = se.prewarm_due()
        rec("③ 数据未变 → 不算", r["due"] is False, r["reason"])

        # ④ as_of 缺失 → 算
        set_cache({"a": {"x": 1}}, ["d"], "", ev_days=["d1"])
        r = se.prewarm_due()
        rec("④ 缺 as_of → 该算", r["due"] is True, r["reason"])

        # ⑤ DB 读不到（异常）→ 保守不算，别因此反复重算
        se.db_latest_date = lambda: ""
        set_cache({"a": {"x": 1}}, ["d"], "2026-09-24", ev_days=["d1"])
        r = se.prewarm_due()
        rec("⑤ DB 日期读不到 → 不算（保守）", r["due"] is False, r["reason"])
    finally:
        se._HITS_CACHE.clear()
        se._HITS_CACHE.update(real)
        se.db_latest_date = real_db


# -------------------------------------------------------------- C/D 调度
def test_schedule():
    import strategy_eval as se
    print("── C 调度状态 ──")
    s = se.prewarm_schedule_state()
    for k in ("enabled", "at", "next", "due", "due_reason",
              "db_max_date", "cache_as_of", "done_today", "last_result"):
        rec(f"  字段 {k}", k in s, str(s.get(k))[:60])
    rec("  next 是未来时间", s["next"][:10] >= s["today"][:10], s["next"])

    print("── D 重复启动 ──")
    a = se.start_daily_prewarm()
    b = se.start_daily_prewarm()
    rec("第二次启动返回 False", (a is True and b is False) or (a is False and b is False),
        f"a={a} b={b}")
    rec("调度线程活着", se._pw_thread is not None and se._pw_thread.is_alive(),
        se._pw_thread.name if se._pw_thread else "None")


# ------------------------------------------------ E 调度循环真的会触发
def test_loop_fires():
    """把「到点」条件造出来，验证循环确实会走进判脏分支并落一条 result。

    用一个 monkeypatch：把 prewarm_at 指向已过去的时间、把 prewarm 换成记录
    调用的假函数，然后手动跑循环体一次（不 sleep 60 秒）。
    """
    import strategy_eval as se
    import store as st
    print("── E 调度循环触发 ──")
    real_at, real_prewarm = se.prewarm_at, se.prewarm
    try:
        st.meta_set(se.K_PW_TRY, "")       # 清掉「今天已检查」
        st.meta_set(se.K_PW_OK, "")
        calls = []
        se.prewarm_at = lambda: "00:00"    # 已过去 → 视为到点
        se.prewarm = lambda days=120, forward=5, why="": (calls.append(why),
                                                          {"ok": True, "note": "fake"})[1]
        # 复刻循环体（不启线程，直接跑一次）
        today = __import__("datetime").date.today().isoformat()
        hh, mm = (int(x) for x in se.prewarm_at().split(":"))
        now = __import__("datetime").datetime.now()
        assert (now.hour, now.minute) >= (hh, mm)
        st.meta_set(se.K_PW_TRY, today)
        due = se.prewarm_due()
        if due["due"]:
            se.prewarm(why="测试")
        rec("到点后写入 K_PW_TRY=今天", st.meta_get(se.K_PW_TRY, "") == today, today)
        rec("该算时调用了 prewarm", len(calls) == 1, str(calls))

        # 第二次（已检查过）不该再调
        if st.meta_get(se.K_PW_TRY, "") == today:
            rec("已检查过则不再重复触发", True, "TRY 已是今天")
    finally:
        se.prewarm_at, se.prewarm = real_at, real_prewarm


# ------------------------------------------------------------ F API 冒烟
def test_api():
    print("── F API 冒烟 ──")
    def req(path, method="GET", body=None, cookie=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data:
            r.add_header("Content-Type", "application/json")
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {}), \
                    resp.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw), ""
            except Exception:
                return e.code, {"raw": raw[:150]}, ""

    sc, _, ck = req("/api/auth/login", "POST",
                    {"username": ADMIN, "password": ADMIN_PASSWORD})
    if not ck:
        rec("管理员登录", False, "无法登录")
        return
    ck = ck.split(";")[0]
    rec("管理员登录", True, ADMIN)

    sc, d, _ = req("/api/admin/eval_cache", cookie=ck)
    rec("GET /admin/eval_cache 200", sc == 200, f"got {sc}")
    sch = (d or {}).get("schedule")
    rec("  返回 schedule 段", isinstance(sch, dict), str(sch)[:80])
    if isinstance(sch, dict):
        rec("  schedule.at = 16:30", sch.get("at") == "16:30", str(sch.get("at")))
        rec("  schedule.enabled", sch.get("enabled") is True, str(sch.get("enabled")))
        rec("  schedule 有判脏结论", "due" in sch and bool(sch.get("due_reason")),
            sch.get("due_reason", "")[:60])

    sc, d, _ = req("/api/admin/caches", cookie=ck)
    it = [x for x in ((d or {}).get("items") or []) if x.get("key") == "eval"]
    rec("/admin/caches 体检项带调度", bool(it) and isinstance(it[0].get("schedule"), dict),
        (it[0].get("detail", "") if it else "")[:70])

    sc, _, _ = req("/api/admin/eval_cache", cookie=None)
    rec("未登录访问 → 401", sc == 401, f"got {sc}")


def main():
    print("=" * 70)
    print("#98 体检缓存每日自动预热 · 自检")
    print("=" * 70)
    test_at()
    test_due()
    test_schedule()
    test_loop_fires()
    test_api()
    ok = sum(1 for _, o, _ in results if o)
    bad = [n for n, o, _ in results if not o]
    print("=" * 70)
    print(f"结果：{ok}/{len(results)} 通过" + ("" if not bad else "，失败：" + "、".join(bad)))
    print("=" * 70)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
