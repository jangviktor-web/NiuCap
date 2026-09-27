"""ETF 筛选 —— 新浪全量清单 + 腾讯批量快照

数据源分工（都是实测过能用的）：
  - 新浪 Market_Center 接口：全量 ETF 清单（实测 1676 只），带最新价/涨跌幅/成交额
  - 腾讯 quote_tencent 批量：补规模（float_cap，亿元）/ 换手率 / 振幅

做不到的（方案 4.2）：IOPV 与折溢价率。
  唯一的免费源是东财 fund_etf_spot_em，实测本机 RemoteDisconnected。
  宁可不做，也不用净值估一个假溢价率 —— 错的数比没数更害人。

为什么引 requests 而不是 akshare：akshare 就是给下面这个 jsonp 接口套了层壳，
  顺带拖进 py_mini_racer 等一堆依赖。这里直接 requests + json，零新依赖。
"""

import json
import time

import requests

_SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php/"
    "IO.XSRV2.CallbackList['da_yPT46_Ll7K6WD']/Market_Center.getHQNodeDataSimple"
)
# ponytail: 只覆盖场内 ETF（node=etf_hq_fund）；要 LOF/封闭式再加映射,
# add when 有人要筛 LOF。
_PARAMS = {"page": "1", "num": "5000", "sort": "symbol", "asc": "0",
           "node": "etf_hq_fund", "[object HTMLDivElement]": "qvvne"}
_HEADERS = {"Referer": "https://vip.stock.finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0"}

_CACHE = {"ts": 0.0, "rows": [], "enriched": False, "snap_at": 0.0}
TTL = 600              # 清单缓存 10 分钟
# 实测：腾讯批量 100 只 0.39s / 300 只 1.75s / 600 只 3.50s —— 批量越大单只越便宜，
# 600 只是实测能稳定拿全的上限。1676 只分 3 批约 10s（只发生在缓存失效那一次）。
_SNAPSHOT_BATCH = 600


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_list(force=False, with_snapshot=True):
    """拉全量 ETF 清单（可顺带补实时规模/换手）。

    返回 (rows, source)；source ∈ {'sina','cache','fail'}
    —— 源挂了会回落到上一次成功的快照并明确标注，绝不静默给陈旧数据
    （静默回落比报错更危险，跟之前修的交易日判断是同一类坑）。

    with_snapshot=True 且缓存里还没补过快照时，会全量 enrich 一遍
    （1676 只约 21 批，实测 8s 左右），做完后跟清单一起缓存 —— 首次慢，之后秒开。
    """
    now = time.time()
    if not force and _CACHE["rows"] and now - _CACHE["ts"] < TTL:
        rows = _CACHE["rows"]
        if with_snapshot and not _CACHE["enriched"]:
            enrich_snapshots(rows)
            _CACHE["enriched"] = True
            _CACHE["snap_at"] = now
        return rows, "cache"
    try:
        r = requests.get(_SINA_URL, params=_PARAMS, timeout=20, headers=_HEADERS)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        txt = r.text
        i = txt.find("([")
        if i < 0:
            raise RuntimeError("返回体里没有 jsonp 包裹，接口可能改版了")
        body = txt[i + 1:-2]                 # 剥掉回调壳与尾部 );
        try:
            raw = json.loads(body)
        except json.JSONDecodeError:
            raw = json.loads(body[:body.rfind("]") + 1])
        rows = []
        for x in raw:
            sym = str(x.get("symbol") or "").strip().lower()
            if not sym:
                continue
            rows.append({
                "code": sym,
                "name": str(x.get("name") or "").strip(),
                "price": _f(x.get("trade")),
                "change_pct": _f(x.get("changepercent")),
                "prev_close": _f(x.get("settlement")),
                "open": _f(x.get("open")),
                "high": _f(x.get("high")),
                "low": _f(x.get("low")),
                "volume": _f(x.get("volume")),
                "amount": _f(x.get("amount")) / 1e8,      # 元 → 亿元
                "time": str(x.get("ticktime") or ""),
            })
        if not rows:
            raise RuntimeError("解析出 0 条，接口可能改版")
        if with_snapshot:
            enrich_snapshots(rows)
        _CACHE["ts"] = now
        _CACHE["rows"] = rows
        _CACHE["enriched"] = bool(with_snapshot)
        _CACHE["snap_at"] = now
        return rows, "sina"
    except Exception:
        if _CACHE["rows"]:
            return _CACHE["rows"], "cache"
        return [], "fail"


def enrich_snapshots(rows, batch=_SNAPSHOT_BATCH):
    """用腾讯批量快照补 规模/换手/振幅。

    ponytail: 串行分批，1676 只约 20 批；实测单批 80 只 <0.5s，够用。
    add when 需要秒级刷新 —— 那时再上并发。
    """
    try:
        import datasource as ds
    except Exception:
        return 0
    got = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        try:
            q = ds.quote_tencent([r["code"] for r in chunk], use_cache=False)
        except Exception:
            continue
        for r in chunk:
            s = q.get(r["code"]) or {}
            if s.get("price"):
                got += 1
            r["cap"] = _f(s.get("float_cap"))        # 规模（亿元）
            r["turnover"] = _f(s.get("turnover"))    # 换手率 %
            r["amplitude"] = _f(s.get("amplitude"))  # 振幅 %
            if s.get("price"):
                r["price"] = _f(s["price"])          # 腾讯的价更新更快
                r["change_pct"] = _f(s.get("change_pct"))
    return got


SORTS = ("cap", "amount", "change_pct", "turnover", "price")


def screen(rows, *, kw="", min_cap=0.0, min_amount=0.0, min_turnover=0.0,
           chg_min=None, chg_max=None, sort_by="amount", desc=True, limit=100):
    """筛选 + 排序。rows 是 fetch_list 的产出（可先 enrich 过）。"""
    out = rows
    kw = (kw or "").strip()
    if kw:
        out = [r for r in out if kw in r["name"] or kw in r["code"]]
    if min_cap > 0:
        out = [r for r in out if (r.get("cap") or 0) >= min_cap]
    if min_amount > 0:
        out = [r for r in out if (r.get("amount") or 0) >= min_amount]
    if min_turnover > 0:
        out = [r for r in out if (r.get("turnover") or 0) >= min_turnover]
    if chg_min is not None:
        out = [r for r in out if (r.get("change_pct") or 0) >= chg_min]
    if chg_max is not None:
        out = [r for r in out if (r.get("change_pct") or 0) <= chg_max]

    key = sort_by if sort_by in SORTS else "amount"
    out = sorted(out, key=lambda r: (r.get(key) or 0), reverse=desc)
    return out[:max(1, min(int(limit), 500))]


def selfcheck() -> int:
    """联网自检：确认源还活着 + 筛选逻辑正确。"""
    fails = []

    def check(cond, good, bad):
        if cond:
            print(f"  ✅ {good}")
        else:
            print(f"  ❌ {bad}")
            fails.append(bad)

    print("[1] 数据源")
    rows, src = fetch_list(force=True)
    check(len(rows) >= 1000, f"ETF 清单 {len(rows)} 只（源={src}）",
          f"清单只拿到 {len(rows)} 只（源={src}）")
    if not rows:
        print("  ⚠️ 源不通，后续用例跳过")
        return 1 if fails else 0
    r0 = rows[0]
    check({"code", "name", "price", "amount"}.issubset(set(r0)),
          f"字段齐全（样例 {r0['code']} {r0['name']}）", "字段缺失")

    print("[2] 快照补充（规模/换手）")
    sample = rows[:160]
    got = enrich_snapshots(sample)
    check(got > 0, f"160 只里补到 {got} 只快照", "快照一个都没补到")
    with_cap = [r for r in sample if r.get("cap")]
    check(len(with_cap) > 0, f"{len(with_cap)} 只拿到规模数据", "规模字段全空")

    print("[3] 筛选逻辑")
    big = screen(sample, min_cap=100.0, sort_by="cap", limit=10)
    check(all((r.get("cap") or 0) >= 100.0 for r in big),
          f"规模≥100亿筛出 {len(big)} 只，且都满足条件", "规模筛选放过了小规模的")
    check(len(big) <= 10, f"limit 生效（{len(big)} ≤ 10）", "limit 没生效")
    desc_ok = all((big[i].get("cap") or 0) >= (big[i + 1].get("cap") or 0)
                  for i in range(len(big) - 1))
    check(desc_ok, "按规模降序正确", "排序不是降序")
    asc = screen(sample, sort_by="cap", desc=False, limit=5)
    asc_ok = all((asc[i].get("cap") or 0) <= (asc[i + 1].get("cap") or 0)
                 for i in range(len(asc) - 1))
    check(asc_ok, "按规模升序正确", "升序排序不对")
    kw_hit = screen(sample, kw="沪深300", limit=50)
    check(all(("沪深300" in r["name"]) or ("沪深300" in r["code"]) for r in kw_hit),
          f"关键词筛出 {len(kw_hit)} 只且都命中", "关键词筛选串了")
    check(screen([], min_cap=1.0) == [], "空输入不炸", "空输入炸了")

    print("=" * 50)
    if fails:
        print(f"❌ {len(fails)} 项失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(selfcheck())
