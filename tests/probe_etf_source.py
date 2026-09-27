# -*- coding: utf-8 -*-
"""ETF 数据源探针 —— 落地 ETF 功能前先跑这个，确认源还活着。

    cd /workspace/tick-stock-panel && python3.11 tests/probe_etf_source.py

任一项 FAIL 就先别写业务代码，先修数据源（见 docs/ETF网格与筛选功能方案.md 4.1）。
退出码 0 = 全通，1 = 有失败。
"""
import json
import os
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.join(os.path.dirname(_HERE), "server")
sys.path.insert(0, _SERVER)

SINA_ETF_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php/"
    "IO.XSRV2.CallbackList['da_yPT46_Ll7K6WD']/Market_Center.getHQNodeDataSimple"
)
# ponytail: 写死 node=etf_hq_fund，只覆盖场内 ETF；要 LOF/封闭式再加映射,
# add when 需要筛 LOF。
SINA_PARAMS = {
    "page": "1", "num": "5000", "sort": "symbol", "asc": "0",
    "node": "etf_hq_fund", "[object HTMLDivElement]": "qvvne",
}

PROBE_CODES = ["sh510300", "sz159915", "sh588000", "sz159941",
               "sh512880", "sh510500", "sz159901", "sh515790"]

_results = []


def _rec(name, ok, detail):
    _results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}  {detail}")
    return ok


def probe_sina_list(min_count=1500):
    """新浪 ETF 全量清单：不引 akshare，requests + json 直连。"""
    try:
        r = requests.get(SINA_ETF_URL, params=SINA_PARAMS, timeout=20,
                         headers={"Referer": "https://vip.stock.finance.sina.com.cn/",
                                  "User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return _rec("新浪ETF清单", False, f"HTTP {r.status_code}")
        txt = r.text
        i = txt.find("([")
        if i < 0:
            return _rec("新浪ETF清单", False, "返回体里没有 jsonp 包裹，接口可能改版")
        body = txt[i + 1:-2]          # 剥掉回调壳与尾部 );
        try:
            rows = json.loads(body)
        except json.JSONDecodeError:
            rows = json.loads(body[:body.rfind("]") + 1])
        need = {"symbol", "name", "trade", "amount"}
        if rows and not need.issubset(set(rows[0].keys())):
            return _rec("新浪ETF清单", False, f"字段缺失，需要 {need}，实到 {set(rows[0])}")
        ok = len(rows) >= min_count
        return _rec("新浪ETF清单", ok, f"{len(rows)} 只（要求 ≥{min_count}）")
    except Exception as e:
        return _rec("新浪ETF清单", False, f"{type(e).__name__}: {str(e)[:120]}")


def probe_quote():
    """腾讯批量快照：ETF 要有价格、规模、换手。"""
    try:
        import datasource as ds
        t0 = time.time()
        q = ds.quote_tencent(PROBE_CODES, use_cache=False)
        dt = time.time() - t0
        got = [c for c in PROBE_CODES if (q.get(c) or {}).get("price")]
        ok = len(got) == len(PROBE_CODES)
        detail = f"{len(got)}/{len(PROBE_CODES)} 有价格，耗时 {dt:.2f}s"
        if got:
            one = q[got[0]]
            detail += f" · 样例 {one.get('name')} 价{one.get('price')} 规模{one.get('float_cap')}亿"
        return _rec("腾讯ETF快照", ok, detail)
    except Exception as e:
        return _rec("腾讯ETF快照", False, f"{type(e).__name__}: {str(e)[:120]}")


def probe_kline(min_bars=200):
    """日线：ETF 的 OHLCV 要能拿到足够历史。"""
    try:
        import datasource as ds
        t0 = time.time()
        k = ds.get_kline("sh510300", period="1d", count=250, use_cache=False)
        dt = time.time() - t0
        n = len(k) if k else 0
        ok = n >= min_bars
        detail = f"{n} 根（要求 ≥{min_bars}），耗时 {dt:.2f}s"
        if n:
            detail += f" · 末根 {k[-1]['date']} 收 {k[-1]['close']}"
        return _rec("ETF日线", ok, detail)
    except Exception as e:
        return _rec("ETF日线", False, f"{type(e).__name__}: {str(e)[:120]}")


def probe_iopv():
    """IOPV/溢价率：东财是唯一源，本机大概率不通 —— 不通是正常的，按方案 4.2 不做。"""
    try:
        r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
                         params={"pn": "1", "pz": "5", "fs": "b:MK0021",
                                 "fields": "f12,f14,f2"},
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        ok = r.status_code == 200 and "f12" in r.text
        return _rec("东财IOPV源", ok, "可用" if ok else f"不可达（HTTP {r.status_code}）→ 溢价率功能按方案不做")
    except Exception as e:
        return _rec("东财IOPV源", False, f"{type(e).__name__} → 溢价率功能按方案不做")


def main():
    print("ETF 数据源探针")
    print("=" * 60)
    probe_sina_list()
    probe_quote()
    probe_kline()
    probe_iopv()          # 这项失败不算阻断，只是确认"不做溢价率"的前提
    print("=" * 60)
    hard = [r for r in _results if r[0] != "东财IOPV源"]
    bad = [r for r in hard if not r[1]]
    if bad:
        print(f"❌ {len(bad)}/{len(hard)} 项数据源不可用，先修源再写业务代码")
        return 1
    print(f"✅ {len(hard)}/{len(hard)} 项核心数据源可用（东财 IOPV 见上，按方案不做）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
