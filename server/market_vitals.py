#!/usr/bin/env python3
"""市场体检：破净率 + 风格罗盘 + 宽基指数 PE
==================================================

参考 baifenwei.com 搬的三个「市场位置」读数，放牛熊页第四张卡。
三件事的数据可得性完全不同，如实分开对待：

  1. 破净率（全 A PB<1 占比）—— 腾讯批量快照一次给全（实测 32s/5579 只），
     但**历史要自己攒**：每天收盘后落一行，自建样本不足 30 天不给分位。
  2. 风格罗盘（三对指数比值分位）—— K 线就有 10 年历史，**今天就能给真分位**，
     不用攒。这是三件事里唯一立等可取的。
  3. 宽基指数 PE —— 腾讯指数快照带当日 PE（12.89，与百分位网 12.86 吻合），
     PB 不给。**历史 PE 无稳定免费源**（乐咕 WAF、中证接口 500、理杏仁收费），
     同破净率一起攒。

为什么用懒触发落库而不是再加一个定时线程：bullbear 已有一条 16:45 线程，
再开一条就是两套「盘后任务」要各自防重、各自排错。这里在 API 被访问时
顺手补当天的一行（过了 15:30 且今天没落过），单机自用够了。
ponytail: 懒触发——没人看页面就不落库；要定时改成像 bullbear 那样挂线程。

分母口径（破净率）：全 A = _a_share_codes() 的 5579 只（含北交所），
PB<=0（净资产为负，实测 58 只）单独计数、不算进破净也不算进「健康」，
单列一行如实展示。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

# 2 小时：破净率/PE/总市值都是收盘级指标，盘中只随价格小幅漂移，
# 高频刷新没有信息增益；全A快照一次 33 秒 + 5585 只，打太密还伤腾讯限速。
# 日频落库另有 15:30 懒触发防重（K_LAST_DAY），与此缓存无关。
TTL = 7200
K_CACHE = "market_vitals_cache"
K_LAST_DAY = "market_vitals_last_day"
K_GDP = "gdp_ttm_cache"
K_BUFFETT_DAILY = "buffett_daily_json"

#: 指数代码 → 展示名。腾讯快照带当日 PE（协议 39 位），PB 对指数恒为 0 不取。
INDEX_PE = (
    ("sh000300", "沪深300"), ("sh000905", "中证500"), ("sh000852", "中证1000"),
    ("sh000016", "上证50"), ("sz399006", "创业板指"), ("sh000922", "中证红利"),
)

#: 风格罗盘：三对指数比值。ratio = a/b，比值涨 = 左边风格相对变强。
#: 选对的原则：两条腿都是常见宽基/风格指数，腾讯 K 线 400 根内全覆盖。
STYLE_PAIRS = (
    ("sh000905", "sh000016", "中证500/上证50", "小盘 vs 大盘"),
    ("sz399006", "sh000016", "创业板指/上证50", "成长 vs 价值"),
    ("sh000922", "sz399006", "中证红利/创业板指", "红利 vs 成长"),
)

#: 15:30 之后才落库——之前落的是盘中读数，收盘后可能变
_CAPTURE_AFTER = (15, 30)


# ---------------------------------------------------------------- 风格罗盘


def _ratio_series(a: str, b: str, count: int = 400) -> List[Dict[str, Any]]:
    """两指数逐日比值（按共同交易日对齐，任一缺当天就跳过）。"""
    import datasource as ds
    ra = ds.get_kline(a, "1d", count)
    rb = ds.get_kline(b, "1d", count)
    bm = {r["date"]: r["close"] for r in rb}
    out = []
    for r in ra:
        bc = bm.get(r["date"])
        if bc and bc > 0 and r["close"] > 0:
            out.append({"date": r["date"], "ratio": r["close"] / bc})
    return out


def _pctile(cur: float, vals: List[float]) -> Optional[float]:
    """当前值在样本里的分位（严格小于 / 样本数）。样本太少不给。"""
    vals = [v for v in vals if v and v > 0]
    if cur is None or cur <= 0 or len(vals) < 30:
        return None
    return sum(1 for v in vals if v < cur) / len(vals)


def style_compass() -> Dict[str, Any]:
    """三对风格比值：当前值 + 250 日分位 + 60 日分位 + 近 20 日涨跌。

    分位窗口选 250/60 而不是 10 年：比值长期均值漂移（风格牛熊可持续数年），
    10 年分位会常年钉在一个位置失去参考意义；一年/一季窗口才是「近期谁强」。
    """
    import datasource as ds
    out = []
    for a, b, label, desc in STYLE_PAIRS:
        try:
            s = _ratio_series(a, b)
        except Exception as e:                      # noqa: BLE001
            out.append({"label": label, "desc": desc, "error": f"{type(e).__name__}: {e}"})
            continue
        if len(s) < 60:
            out.append({"label": label, "desc": desc, "error": "K 线样本不足"})
            continue
        cur = s[-1]["ratio"]
        last250 = [x["ratio"] for x in s[-250:]]
        last60 = [x["ratio"] for x in s[-60:]]
        p250 = _pctile(cur, last250)
        p60 = _pctile(cur, last60)
        chg20 = None
        if len(s) > 20 and s[-21]["ratio"] > 0:
            chg20 = cur / s[-21]["ratio"] - 1
        out.append({
            "label": label, "desc": desc, "a": a, "b": b,
            "ratio": cur, "p250": p250, "p60": p60, "chg20": chg20,
            "n250": len(last250),
            # 比值方向必须跟读数一起给：0.62 是「左边=右边×0.62」，
            # 不说明的话读者不知道 >1 才代表左边强
            "hint": f"{label}（>1 左边强，<1 右边强）",
        })
    return out


# ---------------------------------------------------------------- 破净率 + 指数 PE


def _snap_all(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    import datasource as ds
    return ds.quote_tencent(codes, use_cache=False)


def broken_net(snap: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """全 A 破净率。PB<=0（净资产为负）单列，不算健康也不算破净。"""
    rows = list(snap.values())
    total = len(rows)
    below = [r for r in rows if (r.get("pb") or 0) > 0 and r["pb"] < 1]
    neg = [r for r in rows if (r.get("pb") or 0) < 0]
    no_pb = [r for r in rows if not r.get("pb")]
    return {
        "total": total,
        "below_n": len(below),
        "ratio": len(below) / total if total else None,
        "neg_n": len(neg),
        "no_pb_n": len(no_pb),
        "samples": [{"code": r["code"], "name": r["name"], "pb": r["pb"]}
                    for r in sorted(below, key=lambda x: x["pb"])[:30]],
    }


def index_pe(snap: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for code, name in INDEX_PE:
        r = snap.get(code) or {}
        pe = r.get("pe")
        out.append({"code": code, "name": name, "pe": pe if pe and pe > 0 else None,
                    "price": r.get("price")})
    return out


# ---------------------------------------------------------------- 巴菲特指标


def gdp_ttm() -> Dict[str, Any]:
    """GDP TTM（滚动四季，亿元）。东财数据中心 RPT_ECONOMY_GDP，季度更新。

    TTM 口径：最新行是 Q4（12-01）→ 直接用全年累计；否则
    TTM = 最新累计 + 上一年全年 − 上一年同期累计。结果缓存 30 天。
    """
    import json as _json
    import store as _st

    now = time.time()
    raw = _st.meta_get(K_GDP, "")
    if raw:
        try:
            c = _json.loads(raw)
            if now - float(c.get("at") or 0) < 30 * 86400 and c.get("data"):
                return c["data"]
        except Exception:
            pass

    import requests
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_ECONOMY_GDP&columns=ALL&pageSize=12"
           "&sortColumns=REPORT_DATE&sortTypes=-1")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    rows = ((r.json() or {}).get("result") or {}).get("data") or []
    series = []                       # (yyyy, q_cum 或全年累计, 标签)
    for d in rows:
        dt = (d.get("REPORT_DATE") or "")[:10]
        v = d.get("DOMESTICL_PRODUCT_BASE")
        if dt and v:
            series.append((int(dt[:4]), int(dt[5:7]), float(v)))
    series.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not series:
        return {"value": None, "label": "", "error": "GDP 行为空"}
    yy, mm, cum = series[0]
    if mm == 12:
        ttm, label = cum, f"{yy} 年"
    else:
        prev_same = next((v for y, m, v in series if y == yy - 1 and m == mm), None)
        prev_full = next((v for y, m, v in series if y == yy - 1 and m == 12), None)
        if prev_same is None or prev_full is None:
            # 上年数据不齐，退化用最新累计（口径偏小，如实标注）
            ttm, label = cum, f"{yy} 前{'一二三四'[mm // 3 - 1]}季累计（TTM 缺上年数据，口径偏小）"
        else:
            ttm = cum + prev_full - prev_same
            label = f"{yy - 1}Q4-{yy}Q{'一二三四'[mm // 3 - 1]} 滚动四季"
    out = {"value": ttm, "label": label, "source": "国家统计局（东财数据中心）"}
    try:
        _st.meta_set(K_GDP, _json.dumps({"at": now, "data": out},
                                        ensure_ascii=False))
    except Exception:
        pass
    return out


def buffett(snap: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """巴菲特指标 = 全 A 总市值 / GDP TTM。总市值来自已拉好的腾讯快照（零加请求）。

    只统计**个股**：snap 里混着 6 个指数代码，指数的 total_cap 字段不是市值
    （实测会被算出 248 万亿的荒谬值），必须剔除。
    """
    idx_codes = {c for c, _ in INDEX_PE}
    mv = sum(r["total_cap"] for c, r in snap.items()
             if c not in idx_codes and r.get("total_cap") and r["total_cap"] > 0)
    g = gdp_ttm()
    # 两边都是「亿」，直接相除；写成 /100 会得出 78.8 倍的荒谬值
    ratio = (mv / g["value"]) if (g.get("value") and mv) else None
    return {"total_mv": mv, "gdp_ttm": g.get("value"),
            "gdp_label": g.get("label"), "ratio": ratio}


def _buffett_capture(today: str, b: Dict[str, Any]) -> None:
    """巴菲特指标每日一行，存 meta JSON 列表（不建新表，上限 750 行）。"""
    import json as _json
    import store as _st
    lst = _json.loads(_st.meta_get(K_BUFFETT_DAILY, "[]") or "[]")
    lst = [x for x in lst if x.get("date") != today]
    lst.append({"date": today, "mv": b.get("total_mv"), "ratio": b.get("ratio")})
    _st.meta_set(K_BUFFETT_DAILY, _json.dumps(lst[-750:], ensure_ascii=False))


def buffett_pct(ratio: Optional[float]) -> tuple:
    """自建历史分位：从攒的行里取。返回 (分位, 样本天数)。"""
    import json as _json
    import store as _st
    lst = _json.loads(_st.meta_get(K_BUFFETT_DAILY, "[]") or "[]")
    vals = [x["ratio"] for x in lst if x.get("ratio")]
    if ratio is None or len(vals) < 30:
        return None, len(vals)
    return sum(1 for v in vals if v < ratio) / len(vals), len(vals)


# ---------------------------------------------------------------- 每日落库


def _capture(today: str, bn: Dict[str, Any], pes: List[Dict[str, Any]]) -> None:
    """当天一行进 market_vitals_daily。重复调用幂等（PRIMARY KEY 覆盖也行，
    但只在缺行时写，避免盘中读数覆盖收盘读数）。"""
    import store as _st
    _st.market_vitals_daily_save({
        "date": today,
        "broken_ratio": bn.get("ratio"),
        "broken_n": bn.get("below_n"),
        "total_n": bn.get("total"),
        "index_pe_json": json.dumps({p["code"]: p["pe"] for p in pes},
                                    ensure_ascii=False),
        "updated_at": time.time(),
    })


def maybe_capture(bn: Dict[str, Any], pes: List[Dict[str, Any]],
                  bf: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """过了 15:30 且今天没落过 → 落一行。返回动作说明（排查用）。"""
    import store as _st
    import datetime as _dt
    now = _dt.datetime.now()
    if (now.hour, now.minute) < _CAPTURE_AFTER:
        return {"captured": False, "reason": "未到 15:30"}
    today = now.strftime("%Y-%m-%d")
    if _st.meta_get(K_LAST_DAY, "") == today:
        return {"captured": False, "reason": "今天已落"}
    try:
        _capture(today, bn, pes)
        if bf and bf.get("ratio"):
            _buffett_capture(today, bf)
        _st.meta_set(K_LAST_DAY, today)
        return {"captured": True, "date": today}
    except Exception as e:                          # noqa: BLE001
        return {"captured": False, "reason": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 组合


def build(force: bool = False) -> Dict[str, Any]:
    import store as _st

    now = time.time()
    if not force:
        raw = _st.meta_get(K_CACHE, "")
        if raw:
            try:
                c = json.loads(raw)
                if now - float(c.get("at") or 0) < TTL and c.get("data"):
                    return dict(c["data"], cached=True)
            except Exception:
                pass

    import datasource as ds
    try:
        codes = sorted(ds._a_share_codes()) + [c for c, _ in INDEX_PE]
        snap = _snap_all(codes)
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "error": f"全市场快照取数失败：{type(e).__name__}: {e}"}

    bn = broken_net(snap)
    pes = index_pe(snap)
    styles = style_compass()
    bf = buffett(snap)
    cap = maybe_capture(bn, pes, bf)

    # 自建历史分位：样本 <30 天不给，不硬算
    hist = _st.market_vitals_daily_list(750)
    br_vals = [(h["date"], h["broken_ratio"]) for h in hist if h.get("broken_ratio")]
    br_pct = None
    if bn.get("ratio") and len(br_vals) >= 30:
        vals = [v for _, v in br_vals if v and v > 0]
        br_pct = sum(1 for v in vals if v < bn["ratio"]) / len(vals)
    # 各指数 PE 的自建分位（同规则）
    pe_pct: Dict[str, float] = {}
    if len(hist) >= 30:
        pe_hist: Dict[str, List[float]] = {}
        for h in hist:
            try:
                for code, pe in (json.loads(h.get("index_pe_json") or "{}")).items():
                    if pe and pe > 0:
                        pe_hist.setdefault(code, []).append(pe)
            except Exception:
                continue
        for p in pes:
            if p["pe"] and len(pe_hist.get(p["code"], [])) >= 30:
                vs = pe_hist[p["code"]]
                pe_pct[p["code"]] = sum(1 for v in vs if v < p["pe"]) / len(vs)

    data = {
        "ok": True,
        "date": time.strftime("%Y-%m-%d"),
        "broken": bn,
        "broken_pct": br_pct,
        "hist_days": len(br_vals),
        "index_pe": pes,
        "pe_pct": pe_pct,
        "styles": styles,
        "buffett": bf,
        "buffett_pct": buffett_pct(bf.get("ratio"))[0],
        "buffett_hist_days": buffett_pct(bf.get("ratio"))[1],
        "capture": cap,
        "caveats": [
            "破净率与指数 PE 的**历史分位是自建样本**（本机每天落一行），样本不足 "
            "30 天时只显示当日读数、不硬给分位——分位需要足够的历史才有意义。",
            "指数 PB 腾讯不提供、历史 PE 无稳定免费源（乐咕有 WAF、中证接口不稳），"
            "所以只有 PE 没有指数 PB。这不是省事，是拿不到就不装作拿到了。",
            "风格罗盘分位用 250/60 日窗口而非 10 年：比值长期均值会漂移，"
            "10 年分位常年钉死在一个位置就失去参考意义了。",
            "破净率的行业分布参考站有，我们没有行业字段，砍掉——比例与极值样本已够判断市场位置。",
            "巴菲特指标（全A总市值/GDP TTM）的历史分位是自建样本（每天一行），"
            "不足 30 天只给当日读数；GDP 用国家统计局季度数据滚动四季，每季更新。",
            "全部读数只描述位置，不构成买卖建议。",
        ],
    }
    try:
        _st.meta_set(K_CACHE, json.dumps({"at": now, "data": data},
                                         ensure_ascii=False))
    except Exception:
        pass
    data["cached"] = False
    return data


def selfcheck() -> int:
    """纯逻辑自检，不联网。"""
    bad = 0

    def ck(ok: bool, msg: str) -> None:
        nonlocal bad
        if not ok:
            bad += 1
            print(f"  ❌ {msg}")

    # 破净统计口径：PB<=0 不算破净也不算健康
    fake = {
        "a": {"pb": 0.8, "name": "破净", "code": "sh600000"},
        "b": {"pb": 2.0, "name": "正常", "code": "sh600001"},
        "c": {"pb": -1.0, "name": "净资产为负", "code": "sz000001"},
        "d": {"pb": None, "name": "缺PB", "code": "sz000002"},
    }
    bn = broken_net(fake)
    ck(bn["total"] == 4 and bn["below_n"] == 1, "破净只数应只含 0<PB<1")
    ck(abs(bn["ratio"] - 0.25) < 1e-9, "破净率应=1/4（分母含全部 4 只）")
    ck(bn["neg_n"] == 1 and bn["no_pb_n"] == 1, "负PB与缺PB应单列")

    # 分位：样本 <30 拒绝
    ck(_pctile(1.0, [0.5] * 10) is None, "样本不足 30 应拒绝给分位")
    ck(_pctile(1.0, [0.5] * 30) == 1.0, "全小于时分位应为 1.0")
    ck(_pctile(None, [0.5] * 30) is None, "当前值缺失应返回 None")

    # 指数 PE：0/负值要过滤成 None（0 会被读成「没有盈利」）
    pes = index_pe({"sh000300": {"pe": 0, "price": 4000},
                    "sz399006": {"pe": 42.9, "price": 2400}})
    by = {p["code"]: p["pe"] for p in pes}
    ck(by["sh000300"] is None and by["sz399006"] == 42.9, "PE<=0 应过滤为 None")

    print(f"market_vitals.selfcheck: {'OK' if bad == 0 else str(bad) + ' 项失败'}")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if selfcheck() else 0)
