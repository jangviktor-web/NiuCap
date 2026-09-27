#!/usr/bin/env python3
"""#87 同步守门三重判断 + #89 词典情绪分析 自检。

A 组：sentiment 引擎（真值表 + batch/tag_notice）
B 组：gate 三重判断（注入时间/缓存/抽检，纯逻辑不碰网络）
C 组：增量预筛（注入 bars_progress 与 get_bars_batch，验证分组与跳过）
D 组：HTTP 集成（打真实 8899 服务：/api/sentiment、/api/notices）

用法：python3 tests/check_gate_sentiment.py
"""
import json
import os
import sys
import tempfile
import datetime as _dt

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "server"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import sentiment as senti          # noqa: E402
import sync_bars as sb             # noqa: E402

PASS = []
FAIL = []


def check(cond, good, bad):
    if cond:
        print(f"  ✅ {good}")
        PASS.append(good)
    else:
        print(f"  ❌ {bad}")
        FAIL.append(bad)


def eq(got, want, label):
    check(got == want, label, f"{label}: got={got!r} want={want!r}")


# ================================================================
print("═" * 60)
print("A 组：词典情绪引擎")
print("═" * 60)
rc = senti.selfcheck()
check(rc == 0, "sentiment.selfcheck() 全过", f"sentiment.selfcheck() 失败 rc={rc}")

eq(senti.analyze("公司增持股份")["tone"], "pos", "增持 → 看涨")
eq(senti.analyze("股东减持计划")["tone"], "neg", "减持 → 看跌")
eq(senti.analyze("Annual report 2026")["tone"], "neutral", "英文 → 中性")
r = senti.analyze("短期回调，但是长期依然看多")
eq((r["tone"], r["pos_n"], r["neg_n"]), ("pos", 1, 1), "转折句正负计数")

# ================================================================
print()
print("═" * 60)
print("B 组：同步守门三重判断（注入测试）")
print("═" * 60)

# ---- 判断1：时间守门 ----
MON_NOON = _dt.datetime(2026, 9, 28, 10, 0)     # 周一盘中
MON_EVE = _dt.datetime(2026, 9, 28, 17, 0)      # 周一盘后
SAT = _dt.datetime(2026, 9, 26, 12, 0)          # 周六

big = [f"sh6000{i:02d}" for i in range(1500)]   # >1000 → 算全量
_, g = sb.gate_full_sync("all", big, now=MON_NOON)
eq(g["gate1"] and g["gate1"]["hit"], True, "判断1：周一10:00 全量 → 命中")
eq(g["action"], "skip", "判断1：盘中全量 → skip")

_, g = sb.gate_full_sync("all", big, now=MON_NOON, force=True)
eq(g["action"], "run", "判断1：盘中全量 + --force → 放行")

_, g = sb.gate_full_sync("all", big, now=MON_EVE)
eq(g["gate1"], None, "判断1：周一17:00 → 不命中（已收盘）")

_, g = sb.gate_full_sync("all", big, now=SAT)
eq(g["gate1"], None, "判断1：周六 → 不命中（周末全天可跑）")

small = ["sh600519", "sz000001", "sh601318"]    # 小池子不拦
_, g = sb.gate_full_sync("hs300", small, now=MON_NOON)
eq(g["gate1"], None, "判断1：小池子(3只) 盘中 → 不拦")

# ---- 判断2：当日标记（临时 cache 文件，不碰生产）----
tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
tmp.close()
_orig_cache = sb.CACHE_PATH
sb.CACHE_PATH = tmp.name
try:
    json.dump({"date": "2026-09-28", "scope": "all", "ok": 5569,
               "fail": 0, "ts": MON_EVE.timestamp()}, open(tmp.name, "w"))
    _, g = sb.gate_full_sync("all", big, now=MON_EVE)
    eq(g["gate2"] and g["gate2"]["hit"], True, "判断2：当日已跑标记 → 命中")
    eq(g["action"], "skip", "判断2：当日已跑 → skip")

    # 不同 scope 不拦
    _, g = sb.gate_full_sync("hs300", small, now=MON_EVE)
    eq(g["gate2"], None, "判断2：scope 不同 → 不命中")

    # 判断1 命中时判断2 不再走（短路）
    _, g = sb.gate_full_sync("all", big, now=MON_NOON)
    eq(g["gate2"], None, "判断1 命中后短路，判断2 不评估")

    json.dump({"date": "2026-09-25", "scope": "all", "ok": 5569,
               "fail": 0, "ts": 0}, open(tmp.name, "w"))
    _, g = sb.gate_full_sync("all", big, now=MON_EVE)
    eq(g["gate2"], None, "判断2：标记是昨天的 → 不命中")

    # ---- 判断3：覆盖率抽检（注入 bars_progress）----
    orig_prog = sb.st.bars_progress

    def fresh_prog(codes):
        return {c: {"last": "2026-09-28", "n": 503} for c in codes}

    def stale_prog(codes):
        return {c: {"last": "2026-09-18", "n": 503} for c in codes}

    def half_prog(codes):
        # 95% 已最新 / 5% 落后 → 达标
        return {c: {"last": "2026-09-28" if i % 20 else "2026-09-10",
                    "n": 503} for i, c in enumerate(codes)}

    try:
        sb.st.bars_progress = fresh_prog
        _, g = sb.gate_full_sync("all", big, now=MON_EVE)
        eq(g["gate3"] and g["gate3"]["cover"] == 1.0, True,
           "判断3：抽检 100% 已到期待日 → cover=1.0")
        eq(g["action"], "skip", "判断3：数据已最新 → skip")

        sb.st.bars_progress = stale_prog
        _, g = sb.gate_full_sync("all", big, now=MON_EVE)
        eq(g["gate3"]["cover"] == 0.0 and g["action"] == "run", True,
           "判断3：全部落后 → 放行同步")

        sb.st.bars_progress = half_prog
        _, g = sb.gate_full_sync("all", big, now=MON_EVE)
        eq(g["gate3"]["cover"] == 0.95 and g["action"] == "skip", True,
           "判断3：95% 达标 ≥ 90% 阈值 → skip")

        # 库空（无样本数据）→ 不拦
        sb.st.bars_progress = lambda codes: {}
        _, g = sb.gate_full_sync("all", big, now=MON_EVE)
        eq(g["action"], "run", "判断3：库空 → 放行（首次同步）")
    finally:
        sb.st.bars_progress = orig_prog
finally:
    sb.CACHE_PATH = _orig_cache
    os.unlink(tmp.name)

# ---- force 贯穿三重 ----
tmp2 = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
tmp2.close()
sb.CACHE_PATH = tmp2.name
_orig_prog = sb.st.bars_progress
try:
    json.dump({"date": "2026-09-28", "scope": "all", "ok": 1, "fail": 0,
               "ts": MON_EVE.timestamp()}, open(tmp2.name, "w"))
    sb.st.bars_progress = fresh_prog
    _, g = sb.gate_full_sync("all", big, now=MON_NOON, force=True)
    eq(g["action"], "run", "--force：判断1+2+3 全命中仍放行")
finally:
    sb.st.bars_progress = _orig_prog
    sb.CACHE_PATH = _orig_cache
    os.unlink(tmp2.name)

# ================================================================
print()
print("═" * 60)
print("C 组：增量预筛（注入注入，不碰网络与真库）")
print("═" * 60)

from sources import eltdx_source  # noqa: E402

_orig_bp = sb.st.bars_progress
_orig_batch = eltdx_source.get_bars_batch
_orig_store_one = sb.store_one
_orig_exp = sb._expected_last_date
_calls = []

try:
    # 锁定期待日，防止自检随真实日期漂移（今天跑和下个月跑都必须同样通过）
    sb._expected_last_date = lambda: "2026-09-25"

    # 场景：300 只中 250 只已到参考日(09-25)且根数够 → 跳过；
    # 40 只落后 1 天 → 拉 5 根（gap=1+3，下限 5）；10 只新股无数据 → 拉 500 根
    def fake_prog(codes):
        out = {}
        for i, c in enumerate(codes):
            if i < 250:
                out[c] = {"last": "2026-09-25", "n": 500}
            elif i < 290:
                out[c] = {"last": "2026-09-24", "n": 500}
            # 后 10 只无记录（新股）
        return out

    def fake_batch(cs, count=500):
        _calls.append((tuple(cs), count))
        return {c: [{"date": "2026-09-25", "open": 1, "close": 1,
                     "high": 1, "low": 1, "volume": 1}] for c in cs}

    def fake_store_one(code, rows):
        return {"code": code, "ok": True, "bars": len(rows)}

    sb.st.bars_progress = fake_prog
    eltdx_source.get_bars_batch = fake_batch
    sb.store_one = fake_store_one
    codes = [f"sh600{i:03d}" for i in range(300)]
    ok, fail, bars, failed = sb.sync_via_eltdx(codes, count=500, workers=2)
    eq(len(_calls), 2, f"按缺口分 2 档批量拉（实际 {len(_calls)} 档）")
    counts = sorted(c for _, c in _calls)
    eq(counts, [5, 500], "档位 = 缺口档(落后1天 gap=4→下限5根) + 整段500根(新股)")
    total_req = sum(len(cs) for cs, _ in _calls)
    eq(total_req, 50, f"只请求 50 只（250 只已最新被跳过；实际 {total_req}）")
    eq((ok, fail), (50, 0), f"落库 50 只全成功（ok={ok} fail={fail}）")

    # --no-incremental 路径：全量整段
    _calls.clear()
    ok, fail, bars, failed = sb.sync_via_eltdx(codes, count=500, workers=2,
                                               incremental=False)
    eq(len(_calls), 1 and _calls[0][1] == 500, "关闭增量 → 单档 500 根全量")
    eq(len(_calls[0][0]) == 300, True, "关闭增量 → 300 只全请求")

    # ★ 锁死真实踩到的矛盾：全库众数(09-24)落后期待日(09-25) →
    #   参考日必须抬到期待日，不能整体跳过
    _calls.clear()
    sb.st.bars_progress = lambda cs: {c: {"last": "2026-09-24", "n": 500}
                                      for c in cs}
    codes2 = [f"sz000{i:03d}" for i in range(20)]
    ok, fail, bars, failed = sb.sync_via_eltdx(codes2, count=500, workers=2)
    eq(len(_calls) >= 1, True, "众数落后期待日 → 不得整体跳过（★周六补周五矛盾）")
    req_all = [c for cs, _ in _calls for c in cs]
    eq(len(req_all), 20, f"20 只全部进入拉取名单（实际 {len(req_all)}）")
finally:
    sb.st.bars_progress = _orig_bp
    eltdx_source.get_bars_batch = _orig_batch
    sb.store_one = _orig_store_one
    sb._expected_last_date = _orig_exp

# ================================================================
print()
print("═" * 60)
print("D 组：HTTP 集成（真实服务 8899）")
print("═" * 60)
import requests  # noqa: E402

BASE = "http://127.0.0.1:8899"
try:
    r = requests.post(f"{BASE}/api/sentiment",
                      json={"texts": ["公司业绩超预期", "股价崩盘", "日常经营"]},
                      timeout=10)
    eq(r.status_code, 200, "/api/sentiment 200")
    its = r.json().get("items", [])
    eq([x["tone"] for x in its], ["pos", "neg", "neutral"],
       "/api/sentiment 三条 tone 正确")

    r = requests.post(f"{BASE}/api/sentiment", json={"texts": []}, timeout=10)
    eq(r.status_code == 200 and r.json().get("items") == [],
       True, "/api/sentiment 空列表 → 200 空结果")

    r = requests.post(f"{BASE}/api/sentiment", json={"texts": ["x"] * 201},
                      timeout=10)
    eq(r.status_code, 400, "/api/sentiment 超 200 条 → 400")

    r = requests.get(f"{BASE}/api/notices?code=600519&limit=8", timeout=30)
    eq(r.status_code, 200, "/api/notices 200")
    items = r.json().get("items", [])
    if items:
        tagged = sum(1 for x in items if "sentiment" in x)
        check(tagged == len(items),
              f"/api/notices {tagged}/{len(items)} 条带情绪标签",
              f"/api/notices 情绪标签缺失（{tagged}/{len(items)}）")
        tones = {x["sentiment"]["tone_text"] for x in items}
        print(f"     标签分布：{tones or '（全部中性）'}")
    else:
        print("  ⚠ 该股暂无公告数据，跳过标签断言")
except Exception as e:
    FAIL.append(f"HTTP 集成异常：{type(e).__name__}: {e}")
    print(f"  ❌ HTTP 集成异常：{type(e).__name__}: {e}")

# ================================================================
print()
print("═" * 60)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  ✗ {f}")
    sys.exit(1)
print("✅ 全部通过")
