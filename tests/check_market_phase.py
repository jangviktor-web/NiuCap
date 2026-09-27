#!/usr/bin/env python3
"""#95 市场情绪周期自检：板块阈值 / 阶段判定 / 连板推导 / API 冒烟。"""
import collections
import json
import sqlite3
import sys
import urllib.request

sys.path.insert(0, "server")
import market_phase as mp  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name} {detail}")


print("== A. 板块阈值 ==")
for code, want in [("sh600519", 10.0), ("sz300750", 20.0), ("sh688981", 20.0),
                   ("bj830799", 30.0), ("sz000001", 10.0)]:
    check(f"{code}={want}%", mp.board_pct(code) == want, f"got {mp.board_pct(code)}")

print("== B. 阶段判定（合成场景）==")


def mk(h, fb, g2, pr, sr):
    return {"date": "x", "height": h, "first_board": fb, "ge2": g2,
            "promo_rate": pr, "seal_rate": sr}


rows = [mk(10, 30, 60, 0.3, 0.8)]
mp.classify_phase_series(rows)
check("高潮(ge2>=50)", rows[0]["phase"] == "climax", rows[0]["phase"])

rows = [mk(8, 20, 20, 0.3, 0.8)]
mp.classify_phase_series(rows)
check("主升(h>=7,g2>=15,pr>=0.23)", rows[0]["phase"] == "rally", rows[0]["phase"])

rows = [mk(3, 5, 4, 0.1, 0.5)]
mp.classify_phase_series(rows)
check("冰点(h<=4,g2<=6,fb<=24)", rows[0]["phase"] == "ice", rows[0]["phase"])

rows = [mk(5, 10, 8, 0.18, 0.6)]
mp.classify_phase_series(rows)
check("修复兜底", rows[0]["phase"] == "repair", rows[0]["phase"])

# 退潮需连续 2 日确认：EMA 有惯性，用极端退潮值(pr=0.05,sr=0.3)数日后确认切换
rows = [mk(10, 30, 50, 0.3, 0.8)] + [mk(8, 20, 30, 0.05, 0.3) for _ in range(5)]
mp.classify_phase_series(rows)
check("退潮连续2日确认", rows[-1]["phase"] == "ebb", rows[-1]["phase"])
check("过渡期不立即切换(EMA+防抖)", rows[1]["phase"] == "climax", rows[1]["phase"])

print("== C. 连板推导（真实库近 2 月窗口）==")
conn = sqlite3.connect("data/tick.db", timeout=30.0)
maxd = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
check("daily_bars 有数据", bool(maxd), maxd)
t0 = __import__("time").time()
data = mp.get_phase_data(conn, history_days=25)
dt = __import__("time").time() - t0
check(f"get_phase_data ready ({dt:.1f}s)", data.get("ready") is True)
check("首算 <30s（窗口剪枝生效）", dt < 30, f"{dt:.1f}s")
if data.get("ready"):
    L = data["ladder"]
    check("梯队数值合理(高度>=1)", L["height"] >= 1, L["height"])
    check("首板>=二板+", L["first_board"] >= L["ge2"], f"{L['first_board']} vs {L['ge2']}")
    check("阶段在6种之内", data["phase"] in mp.PHASE_LABELS, data["phase"])
    check("history 25 日", len(data["history"]) == 25, len(data["history"]))
    check("history 有阶段分布",
          len(set(h["phase"] for h in data["history"])) >= 1)
    print(f"    当前: {data['label']} as_of={data['as_of']} ladder={json.dumps(L, ensure_ascii=False)}")
    print(f"    分布: {dict(collections.Counter(h['label'] for h in data['history']))}")

print("== D. 主线（同花顺涨停池聚合）==")
import hithink as htk  # noqa: E402
ml = mp.get_mainline(htk)
check("主线返回结构", isinstance(ml, dict) and "items" in ml)
if ml.get("items"):
    top = ml["items"][0]
    check("score 降序", ml["items"][0]["score"] >= ml["items"][-1]["score"])
    check("概念已拆分(单一reason不再长串)", "+" not in top["reason"], top["reason"])
    check("领涨股存在", bool(top.get("leaders")))
    print(f"    TOP: {top['reason']} 涨停{top['count']} 最高{top['max_boards']}板 "
          f"score={top['score']} 领涨{'、'.join(l['name'] for l in top['leaders'])}")
else:
    print("    （数据源暂不可用，跳过内容断言——非交易时段属正常降级）")

print("== E. API 冒烟 ==")
try:
    with urllib.request.urlopen("http://localhost:8899/api/market_phase", timeout=30) as r:
        d = json.load(r)
    check("HTTP ready", d.get("ready") is True)
    check("含 mainline 字段", "mainline" in d)
except Exception as e:
    check("HTTP 冒烟", False, f"服务未启动？{e}")

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
