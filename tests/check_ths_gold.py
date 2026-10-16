#!/usr/bin/env python3
"""宏观风向 + 黄金行情自检（server/ths_gold.py）。

A. 宏观解析（合成响应，不依赖网络）：
   - 降息概率取「距今最近一场会议」，概率降序、小数转百分比
   - 指标序列倒序取最新/前值，XAUUSD 对照价带回
   - data 信封缺失 / 空序列时返回 None 不炸
B. 黄金行情解析（合成响应）：
   - quote_data → business_row 归一化（最新价/昨收/涨跌额/涨跌幅）
   - 部分品种失败时其余品种仍返回
C. 缓存：TTL 内不重复请求
D. API 冒烟（需要服务在 8899 跑着）
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "server")
import ths_gold  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name} {detail}")


print("== A. 宏观解析（合成） ==")

# A1. 降息概率：过去会议 + 未来会议，应选未来（2026-10-29），top=87.3%
fed_resp = {"status_code": 0, "data": {"list": [
    {"meeting_date": "2026-09-17 02:00", "items": [
        {"rate_range": "400-425", "probability": "0.60000000", "is_in_range": True},
        {"rate_range": "375-400", "probability": "0.40000000"}]},
    {"meeting_date": "2026-10-29 03:00", "items": [
        {"rate_range": "400-425", "probability": "0.12700000"},
        {"rate_range": "375-400", "probability": "0.87300000", "is_in_range": True}]},
]}}
fr = ths_gold._parse_fed_rate(fed_resp["data"])
check("选未来会议", fr and fr["meeting"] == "2026-10-29", str(fr))
check("top 概率降序取一", fr and fr["top_range"] == "375-400" and fr["top_prob"] == 87.3, str(fr))
check("次选区间", fr and fr.get("second_range") == "400-425" and fr.get("second_prob") == 12.7, str(fr))
check("is_in_range 带回", fr and fr["is_in_range"] is True, str(fr))

# A2. 只剩过去会议时取最后一场（不炸）
fr2 = ths_gold._parse_fed_rate({"list": [
    {"meeting_date": "2020-01-01 02:00", "items": [{"rate_range": "1-2", "probability": "1.0"}]}]})
check("无未来会议回落最后一场", fr2 and fr2["meeting"] == "2020-01-01", str(fr2))

# A3. 实际利率：倒序序列取最新/前值，XAUUSD 对照
ry_resp = {"data": {"metrics": [
    {"metrics_name": "tenYearUsRealYieldCurve",
     "metrics_data": {"time": ["2026-10-15", "2026-10-14"], "value": ["2.78", "2.84"]}},
    {"metrics_name": "XAUUSD",
     "metrics_data": {"time": ["2026-10-15", "2026-10-14"], "value": ["4170.68", "4105.20"]}},
]}}
ry = ths_gold._parse_metric(ry_resp["data"], "tenYearUsRealYieldCurve")
check("最新值/日期", ry and ry["value"] == 2.78 and ry["date"] == "2026-10-15", str(ry))
check("前值", ry and ry["prev"] == 2.84, str(ry))
check("XAUUSD 对照", ry and ry.get("gold") == 4170.68, str(ry))

# A4. 空数据 / 结构异常不炸
check("空 metrics → None", ths_gold._parse_metric({"metrics": []}, "x") is None)
check("空 list → None", ths_gold._parse_fed_rate({"list": []}) is None)
check("time/value 长度不齐取共同", ths_gold._parse_metric(
    {"metrics": [{"metrics_name": "termSpread", "metrics_data": {"time": ["d1"], "value": ["0.3", "9"]}}]},
    "termSpread")["value"] == 0.3)

# A5. macro_snapshot 全失败返回 {}（monkeypatch 掉网络）
orig_get = ths_gold._macro_get
ths_gold._macro_get = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
try:
    check("全失败 → {}", ths_gold.macro_snapshot(force=True) == {})
finally:
    ths_gold._macro_get = orig_get

print("== B. 黄金行情解析（合成） ==")

# B1. 单品种响应：value 为 [时间, 最新价, 量, 额] 对齐 data_fields
row = {
    "market": "81", "code": "AU9999", "base_price": 901.57,
    "data_fields": ["1", "10", "13", "19"],
    "value": [[1792108800000, None, None, None], [1792108860000, 896.5, 3802, 34169972]],
}
br = ths_gold._gold_business_row(row)
check("最新价取最后非空点", br and br.get("最新价") == 896.5, str(br))
check("行情时间归一", br and br.get("行情时间", "").startswith("20"), str(br))
check("昨收来自 base_price", br and br.get("昨收价") == 901.57, str(br))
check("涨跌额计算", br and br.get("涨跌额") == -5.07, str(br))
check("涨跌幅计算", br and br.get("涨跌幅") == -0.56, str(br))

# B2. 直接给 dict 值（部分市场形态）
row2 = {"market": "218", "code": "AUUSDO",
        "data": {"10": 4160.33, "1": "2026-10-16 21:20:00", "6": 4155.81}}
br2 = ths_gold._gold_business_row(row2)
check("dict 值直接取字段", br2 and br2.get("最新价") == 4160.33, str(br2))
check("dict 行情时间原样", br2 and br2.get("行情时间") == "2026-10-16 21:20:00", str(br2))

# B3. 无最新价 → 状态行
br3 = ths_gold._gold_business_row({"market": "81", "code": "AUTD"})
check("无价格 → 状态行", br3 and br3.get("状态") == "当前未返回行情", str(br3))

# B4. 非本模块品种 → None
check("陌生品种 → None", ths_gold._gold_business_row({"market": "99", "code": "XX"}) is None)

# B5. gold_quotes 部分失败：一个品种挂掉，其余照常返回
def fake_fetch(payload):
    codes = payload["code_list"][0]["codes"]
    if "AUUSDO" in codes:
        raise OSError("simulated failure")
    return {"status_code": 0, "data": {"quote_data": [{
        "market": payload["code_list"][0]["market"], "code": codes[0],
        "base_price": 900.0, "data_fields": ["1", "10"],
        "value": [[1792108800000, 895.0]]}]}}
orig_fetch = ths_gold._gold_fetch
ths_gold._gold_fetch = fake_fetch
try:
    g = ths_gold.gold_quotes(force=True)
    names = [q["品种"] for q in g.get("quotes", []) if "最新价" in q]
    check("部分失败仍有 5 品种", len(names) == 5, str(names))
    check("失败品种标记状态", any(
        q.get("状态") for q in g.get("quotes", []) if q["品种"] == "伦敦金现"))
finally:
    ths_gold._gold_fetch = orig_fetch

# B6. 全失败 → {}
ths_gold._gold_fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
try:
    check("全失败 → {}", ths_gold.gold_quotes(force=True) == {})
finally:
    ths_gold._gold_fetch = orig_fetch

print("== C. 缓存 ==")
calls = {"n": 0}
def counting_fetch(payload):
    calls["n"] += 1
    return {"status_code": 0, "data": {"quote_data": [{
        "market": "218", "code": "AUUSDO", "base_price": 1.0,
        "data_fields": ["1", "10"], "value": [[1792108800000, 1.0]]}]}}
ths_gold._gold_fetch = counting_fetch
try:
    ths_gold.gold_quotes(force=True)
    n_after_force = calls["n"]          # 一次全量 = 6 品种 6 次请求
    ths_gold.gold_quotes()              # TTL 内应命中缓存，不再发请求
    check("TTL 内不重复请求", calls["n"] == n_after_force and n_after_force == 6,
          f"force 后 {n_after_force} 次，再取后 {calls['n']} 次")
    time.sleep(0.1)
finally:
    ths_gold._gold_fetch = orig_fetch

print("== D. API 冒烟 ==")
for path, keys in [("/api/macro", ["fed_rate", "real_yield"]),
                   ("/api/gold_quotes", ["quotes"])]:
    try:
        with urllib.request.urlopen(f"http://localhost:8899{path}", timeout=60) as r:
            d = json.load(r)
        check(f"{path} ok", d.get("ok") is True, str(d)[:120])
        check(f"{path} 含 {keys}", all(k in d for k in keys), str(d)[:120])
    except Exception as e:
        check(f"{path} 冒烟", False, f"服务未启动或上游暂不可用：{e}")

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
