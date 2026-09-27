"""龙虎榜后验字段增强（东财源）端到端校验。

覆盖：
  1. /api/lhb 默认：source=eastmoney、items 非空、榜单日期非空，
     条目含上榜原因 reason 与 d1/d2/d5/d10 后验字段键
  2. 历史日期 ?date=2026-09-18：D1/D2 有数值（后验已到期）
  3. 节假日 ?date=2026-09-25：自动回退到最近有数据的交易日
  4. ?limit=5：截断生效、rank 连续
  5. 降级路径：东财抛异常时路由回退 westock 源（source=westock）
  6. 后验值语义：当日榜（最新一期）d1 可为 null（未到期），非 null 时为数值

跑法（需先启动服务）：
    python3 tests/check_lhb_em.py
"""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
BASE = "http://127.0.0.1:8899"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("✅" if ok else "❌"), name, detail)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


import json  # noqa: E402

# ---- 1. 默认调用 ----
d = get("/api/lhb?limit=30")
items = d.get("items") or []
check("默认调用 source=eastmoney", d.get("source") == "eastmoney", f"source={d.get('source')}")
check("items 非空", len(items) > 0, f"n={len(items)}")
check("榜单日期非空", bool(d.get("date")), f"date={d.get('date')}")
x0 = items[0] if items else {}
check("条目含 reason", "reason" in x0 and x0.get("reason"), f"reason={str(x0.get('reason'))[:20]}")
check("条目含 d1~d10 键", all(k in x0 for k in ("d1", "d2", "d5", "d10")))
check("条目含 buy/sell/close", all(k in x0 for k in ("buy", "sell", "close")))

# 净买额降序
nbs = [i.get("net_buy") or 0 for i in items]
check("净买额降序", all(nbs[i] >= nbs[i + 1] for i in range(len(nbs) - 1)))

# ---- 2. 历史日期有后验值 ----
d2 = get("/api/lhb?date=2026-09-18&limit=10")
it2 = d2.get("items") or []
check("09-18 榜日期正确", d2.get("date") == "2026-09-18", f"date={d2.get('date')}")
d1s = [i.get("d1") for i in it2 if i.get("d1") is not None]
check("09-18 D1 有数值（后验已到期）", len(d1s) >= 1, f"有值条数={len(d1s)}")
d2s = [i.get("d2") for i in it2 if i.get("d2") is not None]
check("09-18 D2 有数值", len(d2s) >= 1, f"有值条数={len(d2s)}")

# ---- 3. 节假日自动回退 ----
d3 = get("/api/lhb?date=2026-09-25&limit=5")
check("节假日 09-25 自动回退", d3.get("date") not in ("", "2026-09-25") and bool(d3.get("items")),
      f"回退到 {d3.get('date')}")

# ---- 4. limit 截断 ----
d4 = get("/api/lhb?limit=5")
it4 = d4.get("items") or []
check("limit=5 截断", len(it4) <= 5, f"n={len(it4)}")
check("rank 连续 1..n", [i.get("rank") for i in it4] == list(range(1, len(it4) + 1)))

# ---- 5. 降级路径（单元级：mock 东财抛异常）----
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
try:
    from unittest.mock import patch
    from server import app as amod
    # patch 路由模块实际绑定的 wst 对象（模块可能被加载两份，patch server.westock 不一定命中）
    with patch.object(amod.wst, "lhb_em", side_effect=RuntimeError("模拟东财挂了")):
        r5 = amod.api_lhb(limit=5, date="")
        check("东财异常时降级 westock", r5.get("source") == "westock", f"source={r5.get('source')}")
        check("降级后 items 仍可用", isinstance(r5.get("items"), list))
except Exception as e:  # noqa: BLE001
    check("降级路径测试执行", False, f"异常: {e}")

# ---- 6. 后验语义：最新一期 D1 可为 null，非 null 必为数值 ----
latest = d.get("date")
vals = [i.get("d1") for i in items]
ok_sem = all(v is None or isinstance(v, (int, float)) for v in vals)
check("后验字段语义（null=未到期，其余为数值）", ok_sem,
      f"最新榜 {latest}，D1 有值 {sum(1 for v in vals if v is not None)}/{len(vals)}")

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n通过 {n_pass}/{len(results)}")
sys.exit(0 if n_pass == len(results) else 1)
