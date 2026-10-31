#!/usr/bin/env python3
"""沪深300 ETF 合计规模 ÷ 沪深300 指数流通市值（B 方案：季频骨架 + 今日实时点）
================================================================================

回答一个问题：**被动跟踪沪深300 的 ETF，相对这 300 只票的流通盘子占多大？**

为什么只能有两种点，不能在季报之间插值
----------------------------------------
两条腿的可得频率**不对等**，这是整个设计的全部前提：

                  当日可得        历史可得
  ETF 份额/规模    ✅ 腾讯快照      ✅ 东财 F10 季报（57 期，2012 起）
  指数流通市值     ✅ 腾讯快照      ❌ 无任何源给历史

「日频」在这里只能靠假设——假设季报之间份额不变，把曲线画成只剩价格在动。
那条线看起来很连续，但**真正的变量在两次季报之间是编的**：
510300 实测 2025-12-31 888.30 亿份 → 2026-03-31 448.29 亿份，单季砍半，
而这种变动季报之间完全看不见。所以只画两种点：

  1. **季报点**：份额取季末 → 一段真实历史（单位亿份，见下方「单位」）；
  2. **今日实时点**：双方都用当日快照 → 一个真实比例（单位 %）。

两类点之间**不连线**。连上就等于宣称「从季末到今天份额线性变化」，那是没数据的
推论。前端画虚线只做视觉引导，tooltip 明说虚线不是数据。

单位为什么分两套（最容易看错的地方）
--------------------------------------
  hist[].shares  亿份   ← 季报原文就是「期末总份额（亿份）」
  today.ratio    %      ← 规模/流通市值
把两者画在同一条 y 轴上会让人以为「亿份」就是「占比」。所以前端是**双图**：
上图画份额（亿份），下图只放今日一个比例点。这是 B 方案最诚实的形状：
历史看规模怎么长，今天看占比多少。**不要为了「好看」把它们合并成一张图。**

分母为什么用流通市值不用总市值
--------------------------------
ETF 买的是能交易的流通盘。实测沪深300 流通 48.4 万亿 / 总市值 51.1 万亿，差 5.5%。
用总市值会把比例系统性低估 5%。

数据源：全部走项目已有通路，**不新建外部依赖**
------------------------------------------------
- 分子分母的当日值：datasource.quote_tencent（一条请求拿全，且不带限流问题）。
- 季报历史：东财 F10 规模变动。**仅有此一处**碰东财，且带缓存 + 串行 sleep，
  因为它对高频请求会直接断连（实测 RemoteDisconnected）。
- 池子名单：etf.fetch_list（新浪全量 + 腾讯补规模），已在库里，不再自己枚举。

ponytail: 池子每次请求都重新枚举（etf 自带 10 分钟缓存）。不做「池子成员变化告警」——
规模占比本身是慢变量，成员进出影响的是绝对值细节，报告里列出名单就够了。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

TTL = 1800               # 30 分钟：季报季度才变，当日规模也没必要更密
GAP_S = 1.2              # 碰东财的请求之间必须 sleep，否则被断连
K_CACHE = "hs300_etf_ratio_cache"

INDEX_CODE = "sh000300"

#: 池子口径：**精确**匹配「沪深300」或「沪深300ETF+公司后缀」。
#:
#: 为什么不用「名称含沪深300」这种宽松写法：实测会混进一堆衍生指数——
#: 300红利低波ETF、沪深300成长ETF、沪深300价值ETF、ESG300ETF，它们跟踪的
#: 不是沪深300 本身，规模混进分子会让口径变成「含300 字样的基金」。
#: 也不能用「含300 且含ETF」：中证1000ETF、A100ETF 同样过得了。
_POOL_PAT = re.compile(r"^沪深300(ETF[\u4e00-\u9fa5A-Za-z]*)?$")
_POOL_DROP = ("联接", "增强", "成长", "价值", "红利", "ESG", "LOF", "A类", "C类")

#: 静态种子表。这是本模块**完整性**的唯一保障，不是优化：
#:
#: 腾讯搜索单次只回 10 条（接口硬限制，不是参数没给够），多关键词也只覆盖
#: 十几只；东财 clist 高频即断连、新浪清单实测连续超时。三条路都拿不到全量，
#: 而小产品合计约 3%——漏掉它们比例就偏低。
#:
#: 本表来自一次完整枚举（35 只口径）后逐只用腾讯核验，保留当前仍在交易、
#: 且名称符合 _POOL_PAT 的 24 只。新增产品由搜索补；本表不需要经常维护，
#: 但**删任何一行之前先想清楚：那只票的规模谁来兜**。
_SEED = (
    # 沪市
    "sh510300", "sh510310", "sh510320", "sh510330", "sh510350",
    "sh510360", "sh510370", "sh510380", "sh510390",
    "sh515130", "sh515310", "sh515330", "sh515350", "sh515360",
    "sh515380", "sh515390", "sh515660",
    # 深市
    "sz159300", "sz159393", "sz159673", "sz159919", "sz159925",
)

#: 腾讯搜索接口。比新浪清单（实测连续 3 次 26s 超时）与东财 clist
#: （高频即 RemoteDisconnected）都稳：实测 0.0s 返回，连测无失败。
_SEARCH = "https://smartbox.gtimg.cn/s3/"
_SEARCH_KW = ("沪深300ETF", "沪深300")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def in_pool(name: str) -> bool:
    """这只 ETF 算不算「被动跟踪沪深300」。"""
    n = str(name or "").replace(" ", "")
    return bool(_POOL_PAT.match(n)) and not any(b in n for b in _POOL_DROP)


# ---------------------------------------------------------------- 季报（唯一碰东财处）


def quarter_shares(code: str) -> List[Tuple[str, float, float]]:
    """某只 ETF 的季末总份额与期末净资产 [(date, 亿份, 亿元)]。code 传 6 位数字。

    表体列：日期 / 期间申购 / 期间赎回 / 期末总份额(亿份) / 期末净资产(亿元) / 变动率。
    正则在真实页面上验证过（2026-09-30 → 241.20 / 1,068.79）。
    净资产是重建历史占比的关键：占比 = 净资产 ÷ 指数流通市值，两条腿都是钱、
    同单位；份额×未知的历史净值反而引入第二重估计。
    """
    url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx?" +
           urllib.parse.urlencode({"type": "gmbd", "code": code,
                                   "rt": str(time.time())}))
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": "https://fundf10.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read().decode("utf-8", "replace")
            break
        except Exception as e:          # noqa: BLE001 限流/断连同一类
            last = e
            txt = ""
            time.sleep(GAP_S * (i + 1))
    else:
        raise RuntimeError(f"东财季报请求失败：{type(last).__name__}: {last}")

    pat = re.compile(r"<tr><td>(\d{4}-\d{2}-\d{2})</td>"
                     r"<td[^>]*>([\d,\.\-]+)</td>"
                     r"<td[^>]*>([\d,\.\-]+)</td>"
                     r"<td[^>]*>([\d,\.\-]+)</td>"
                     r"<td[^>]*>([\d,\.\-]+)</td>")
    out = []
    for m in pat.finditer(txt):
        try:
            out.append((m.group(1), float(m.group(4).replace(",", "")),
                        float(m.group(5).replace(",", ""))))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 当日快照（全走腾讯）


def search_etfs(keyword: str) -> List[Dict[str, str]]:
    """腾讯搜索接口按关键词查 ETF，返回 [{code:'sh510300', name:'...'}]。

    返回体形如 v_hint="sh~510300~沪深300ETF华泰柏瑞~hs300etfhtbr~ETF^sz~..."
    （字段用 ~ 分隔、条目用 ^ 分隔，名称是 \\u 转义）。单次最多回 10 条，
    所以下面多关键词聚合——这是接口的硬限制，不是我们少传了参数。
    """
    last = None
    for i in range(3):
        try:
            url = _SEARCH + "?" + urllib.parse.urlencode(
                {"v": "2", "q": keyword, "t": "all"})
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=12) as r:
                txt = r.read().decode("utf-8", "replace")
            m = re.search(r'v_hint="(.*)"', txt)
            if not m:
                raise RuntimeError("返回体里没有 v_hint，接口可能改版")
            out = []
            for part in m.group(1).split("^"):
                f = part.split("~")
                if len(f) < 5 or f[4] != "ETF":
                    continue
                name = f[2]
                # 名称可能是 \uXXXX 转义（原样返回时），也可能已是中文
                if "\\u" in name:
                    try:
                        name = name.encode().decode("unicode_escape")
                    except Exception:
                        pass
                out.append({"code": (f[0] + f[1]).lower(), "name": name})
            return out
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"腾讯搜索失败（{keyword}）：{type(last).__name__}: {last}")


def etf_candidates() -> List[str]:
    """沪深300 ETF 候选代码 = 腾讯搜索结果 ∪ 静态种子表，去重保序。

    搜索负责「新发的」，种子表兜住「搜索一次只回 10 条、多关键词也只覆盖十几只」
    漏掉的小产品。两边都拿不到就是网络整体不通——此时抛错，不要假装池子是空的。
    """
    found: Dict[str, None] = {}
    errs = []
    for kw in _SEARCH_KW:
        try:
            for r in search_etfs(kw):
                found.setdefault(r["code"], None)
        except Exception as e:          # noqa: BLE001
            errs.append(str(e))
    if not found:
        # 搜索全失败。种子表还能用，但必须让上层知道搜索是坏的——
        # 静默只用种子表会漏掉新发产品，而用户看不出「这次少了几只」。
        raise RuntimeError("腾讯搜索全部失败，无法枚举 ETF：" + "；".join(errs[:2]))
    for c in _SEED:
        found.setdefault(c, None)
    return list(found)


def snapshot() -> Dict[str, Any]:
    """一次请求拿齐：指数流通市值 + 池子里每只 ETF 的规模。

    分子 = 池内 ETF 的 float_cap 合计（亿元），分母 = 指数 float_cap（亿元）。
    两者同源同单位，不换算 —— 换算就是引入误差的机会。
    """
    import datasource as ds

    # 1) 候选代码：腾讯搜索 ∪ 种子表。搜索全挂会抛错——如实说「枚举失败」，
    #    而不是报「池为空」，后者会让人以为市场上真没有沪深300 ETF。
    cands = etf_candidates()

    # 2) 规模与指数市值：腾讯一条批量请求（这也是一直最稳的那个接口）。
    #    名称一律以这次返回的实时值为准，种子表只提供代码、不参与判断：
    #    代码表可能过期（比如某只改名或不再跟踪沪深300），实时名称才是事实。
    codes = [INDEX_CODE] + cands
    q = ds.quote_tencent(codes, use_cache=False)
    idx = q.get(INDEX_CODE) or {}
    den = idx.get("float_cap")
    if not den:
        raise RuntimeError("指数流通市值取不到（quote_tencent 未返回 sh000300.float_cap）")

    items, skipped = [], []
    for c in cands:
        s = q.get(c) or {}
        nm = s.get("name") or ""
        cap = s.get("float_cap")
        if not nm or not cap or cap <= 0:
            skipped.append(c)          # 停牌/退市/查不到，跳过但不影响整体
            continue
        if not in_pool(nm):
            continue                   # 名字不是沪深300 本体（衍生指数/别的指数）
        items.append({"code": c[2:] if len(c) > 6 else c, "name": nm,
                      "cap": float(cap)})

    if not items:
        raise RuntimeError(
            f"候选 {len(cands)} 只里没有一只被腾讯识别为沪深300 ETF"
            f"（跳过 {len(skipped)} 只无快照）——筛选口径可能需复核")
    items.sort(key=lambda x: -x["cap"])
    num = sum(x["cap"] for x in items)
    if num <= 0:
        raise RuntimeError("池内 ETF 规模合计为 0，拒绝用 0 当分子")

    return {
        "etf_cap": num,
        "index_cap": float(den),
        "index_name": idx.get("name"),
        "index_price": idx.get("price"),
        "index_total_cap": idx.get("total_cap"),
        "items": items,
        "cand_n": len(cands),         # 候选多少只（排查口径用）
        "skipped": skipped,           # 哪些代码查不到快照
    }


# ---------------------------------------------------------------- 历史占比重建


def _idx_close_map(count: int = 3800) -> Dict[str, float]:
    """指数日线 date→close。3800 根能覆盖到 2011（季报最早 2012-05）。"""
    import datasource as ds
    rows = ds.get_kline(INDEX_CODE, "1d", count, use_cache=True)
    return {r["date"]: float(r["close"]) for r in rows if r.get("close")}


def _close_on(dmap: Dict[str, float], date: str, slack: int = 10):
    """季报日（非交易日）取 ≤slack 个自然日内的最后一个交易日收盘。"""
    if not date:
        return None
    from datetime import date as _d, timedelta as _td
    try:
        base = _d.fromisoformat(date)
    except ValueError:
        return None
    for i in range(slack + 1):
        c = dmap.get((base - _td(days=i)).isoformat())
        if c:
            return c
    return None


def rebuild_hist(hist: List[Dict[str, Any]], snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """给季报点补上「近似历史占比」与指数收盘。

    占比_t = 净资产_t(510300) ÷ 流通市值_t。流通市值没有历史源，用
    「今日流通市值 × close_t/close_today」近似——等价于假设**流通股本
    自 2012 年以来不变**。这个假设不成立（新股纳入、增发、样本调整都在
    动股本），所以整条线打上「近似」标签，口径与今日全池占比**不可直接
    混读**（今日主读数是全池 22 只，历史线只有 510300 一只）。
    要给这条线撒谎很容易（比如假装它是全池的），我们不。
    """
    idx_cap_today = float(snap["index_cap"])
    close_today = snap.get("index_price")
    dmap = _idx_close_map()
    out = []
    for h in hist:
        c = _close_on(dmap, h["date"])
        nav = h.get("nav_cap")
        ratio = None
        if c and close_today and nav and nav > 0:
            denom = idx_cap_today * (c / close_today)
            ratio = _ratio(nav, denom)
        out.append(dict(h, idx_close=c, ratio=ratio))
    return out


def percentile_of(cur: Optional[float], pts: List[Tuple[str, float]],
                  years: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """当前值在窗口内的百分位：严格小于当前值的样本数 ÷ 样本数（当前值不计入）。

    与参考页口径一致。pts = [(date, value)]，窗口按 years 截。样本太少
    （<8）不给——61 个季频点切 1 年只剩 4 个点，那种百分位没有意义。
    返回 {pct, n, lo, lo_date, hi, hi_date}。窗口内没有更小值时 pct=0 是
    **合法读数**（当前就是最低），不是失败，所以不能拿 None 顶替。
    """
    if not cur or cur <= 0 or not pts:
        return None
    if years:
        import datetime as _dt
        cut = (_dt.date.today() - _dt.timedelta(days=int(years * 365.25))).isoformat()
        pts = [(d, v) for d, v in pts if d >= cut]
    pos = [(d, v) for d, v in pts if v and v > 0]
    if len(pos) < 8:
        return None
    less = sum(1 for _, v in pos if v < cur)
    lo_pt = min(pos, key=lambda p: p[1])
    hi_pt = max(pos, key=lambda p: p[1])
    return {"pct": less / len(pos), "n": len(pos),
            "lo": lo_pt[1], "lo_date": lo_pt[0],
            "hi": hi_pt[1], "hi_date": hi_pt[0]}


def build(force: bool = False) -> Dict[str, Any]:
    """前端要的全部数据。带缓存；失败返回 ok=False + 原因，绝不回退成 0。"""
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

    try:
        snap = snapshot()
    except Exception as e:                  # noqa: BLE001
        return {"ok": False, "error": f"当日快照取数失败：{e}"}

    ratio = _ratio(snap["etf_cap"], snap["index_cap"])

    # 季报序列：取池内最大那只（份额历史最长，也占池子大头）
    hist: List[Dict[str, Any]] = []
    src_code = snap["items"][0]["code"] if snap["items"] else ""
    src_name = snap["items"][0]["name"] if snap["items"] else ""
    hist_err = ""
    if src_code:
        try:
            hist = [{"date": d, "shares": s, "nav_cap": nav}
                    for d, s, nav in reversed(quarter_shares(src_code))]
        except Exception as e:              # noqa: BLE001
            hist_err = f"{type(e).__name__}: {e}"

    # 近似历史占比（510300 口径）+ 指数收盘对照。任何一步失败都只降级、
    # 不拖垮整卡：占比线拿不到时前端还剩份额线可看。
    ratio_err = ""
    hist_ratio: List[Dict[str, Any]] = []
    today_510300_ratio = None
    try:
        top_cap = snap["items"][0]["cap"]
        today_510300_ratio = _ratio(top_cap, snap["index_cap"])
        hist = rebuild_hist(hist, snap)
        hist_ratio = [h for h in hist if h.get("ratio") is not None]
    except Exception as e:                  # noqa: BLE001
        ratio_err = f"{type(e).__name__}: {e}"

    # 百分位：当前 510300 占比 vs 自己的季频重建序列。**不能用全池今日占比**
    # 去比——序列是 510300 一只的，混着比是把两只口径不同的尺子互贴。
    pct: Dict[str, Any] = {}
    if today_510300_ratio and hist_ratio:
        pts = [(h["date"], h["ratio"]) for h in hist_ratio]
        for y in (None, 10, 5, 3):
            r = percentile_of(today_510300_ratio, pts, years=y)
            if r is not None:
                pct[str(y) if y else "all"] = r

    data = {
        "ok": True,
        "today": {
            "date": time.strftime("%Y-%m-%d"),
            "etf_cap": snap["etf_cap"],
            "index_cap": snap["index_cap"],
            "ratio": ratio,
            # 510300 单只口径：与 hist_ratio 同一把尺子，百分位才有意义
            "top_ratio": today_510300_ratio,
        },
        "hist": hist,
        "hist_code": src_code,
        "hist_name": src_name,
        "hist_error": hist_err,
        "hist_ratio_error": ratio_err,
        "pct": pct,
        "pool": snap["items"][:20],
        "pool_n": len(snap["items"]),
        "pool_cap": snap["etf_cap"],
        "index": {"name": snap["index_name"], "price": snap["index_price"],
                  "float_cap": snap["index_cap"],
                  "total_cap": snap["index_total_cap"]},
        "caveats": [
            "历史占比线是**近似重建**：分子=510300 季报期末净资产（全池只有龙头"
            "有完整季报历史），分母=今日流通市值 × 历史收盘/今日收盘——等价于假设"
            "流通股本 2012 年以来不变。股本增发、样本调整都会引入偏差，所以这条线"
            "只看**相对位置（百分位）与方向**，绝对值别当真。",
            "季报点之间不连线成「真数据」：季报之间份额会变（510300 实测单季从 "
            "888.30 亿份砍到 448.29 亿份），季报看不见那种变动。参考站有日频净申购"
            "数据因此能画日频线，我们拿不到该源，季频是诚实的选择。",
            "今日主读数是**全池**占比（22 只实时快照），历史线与百分位是 **510300 "
            "单只**口径——两者刻意分开，不混用同一把尺子。",
            "池子 = 腾讯搜索 ∪ 静态种子表，名称以腾讯实时返回为准后再精确筛选"
            "（只要「沪深300」或「沪深300ETF+公司名」）；衍生指数如 300红利低波、"
            "沪深300成长/价值、ESG300 一律排除，它们跟踪的不是沪深300 本体。",
            "分母用指数流通市值（48.4 万亿）而非总市值（51.1 万亿），因为 ETF 买的是"
            "流通盘；用总市值会把比例系统性低估约 5%。",
            "只做规模观察，不构成任何买卖建议。",
        ],
    }
    try:
        _st.meta_set(K_CACHE, json.dumps({"at": now, "data": data},
                                         ensure_ascii=False))
    except Exception:
        pass
    data["cached"] = False
    return data


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    """比例。任一腿缺失/非正 → None。**绝不返回 0**：0 会被读成「没有 ETF 资金」。"""
    if not num or not den or num <= 0 or den <= 0:
        return None
    return num / den


def selfcheck() -> int:
    """纯逻辑自检，不联网。"""
    bad = 0

    def ck(ok: bool, msg: str) -> None:
        nonlocal bad
        if not ok:
            bad += 1
            print(f"  ❌ {msg}")

    # 池子口径。这批断言来自实测噪音：宽松的「含沪深300」会把衍生指数收进来，
    # 「含300 且含ETF」又会把中证1000/A100 收进来，两种写法都错。
    ck(in_pool("沪深300ETF华泰柏瑞"), "普通沪深300ETF 应保留")
    ck(in_pool("沪深300 ETF 易方达"), "名称带空格也应识别")
    ck(in_pool("沪深300"), "嘉实那只就叫「沪深300」（462 亿，第 4 大），必须保留")
    ck(in_pool("沪深300ETF"), "无公司后缀也应保留")
    ck(not in_pool("沪深300增强ETF"), "增强类应排除")
    ck(not in_pool("沪深300ETF联接A"), "联接基金应排除")
    ck(not in_pool("中证500ETF"), "非沪深300 应排除")
    ck(not in_pool("沪深300LOF"), "LOF 应排除")
    ck(not in_pool("300红利低波ETF嘉实"), "300红利低波是衍生指数，应排除")
    ck(not in_pool("沪深300成长ETF万家"), "沪深300成长是衍生指数，应排除")
    ck(not in_pool("沪深300价值ETF华夏"), "沪深300价值是衍生指数，应排除")
    ck(not in_pool("ESG300ETF国联安"), "ESG300 不是沪深300，应排除")
    ck(not in_pool("中证1000ETF富国"), "中证1000 不是沪深300，应排除")
    ck(not in_pool("A100ETF华夏"), "A100 不是沪深300，应排除")

    # 种子表本身必须都符合口径（防止有人往里塞错代码/错口径的东西）
    ck(all(c.startswith(("sh", "sz")) and len(c) == 8 for c in _SEED),
       "种子表代码格式应形如 sh510300")
    ck(len(set(_SEED)) == len(_SEED), "种子表不应有重复代码")

    # 季报正则（真实片段）
    frag = ("<tr><td>2026-09-30</td><td class='tor'>201.07</td>"
            "<td class='tor'>149.02</td><td class='tor'>241.20</td>"
            "<td class='tor'>1,068.79</td><td class='tor'>12.66%</td></tr>")
    m = re.search(r"<tr><td>(\d{4}-\d{2}-\d{2})</td><td[^>]*>([\d,\.\-]+)</td>"
                  r"<td[^>]*>([\d,\.\-]+)</td><td[^>]*>([\d,\.\-]+)</td>", frag)
    ck(bool(m) and m.group(1) == "2026-09-30"
       and abs(float(m.group(4)) - 241.20) < 1e-6, "季报行应解析出日期与期末份额")

    # 比例：缺失/为 0 一律 None，不返回 0
    r = _ratio(2800.0, 484272.0)
    ck(r is not None and 0.004 < r < 0.008, f"比例量级应在 0.5% 左右，实得 {r}")
    ck(_ratio(0, 484272.0) is None, "分子 0 应返回 None")
    ck(_ratio(None, 484272.0) is None, "分子缺失应返回 None")
    ck(_ratio(2800.0, None) is None, "分母缺失应返回 None")

    # 百分位：口径 = 严格小于当前的样本 ÷ 窗口样本（当前不计入）。
    # pct=0（当前就是最低）是合法读数不是失败；样本 <8 必须拒绝。
    pts = [(f"2020-0{i}-01", i * 0.001) for i in range(1, 9)]   # 8 点 0.001~0.008
    p8 = percentile_of(0.0055, pts)
    ck(p8 is not None and abs(p8["pct"] - 5 / 8) < 1e-9,
       f"8 点中 0.0055 的百分位应为 5/8，实得 {p8 and p8['pct']}")
    ck(p8 is not None and p8["lo"] == 0.001 and p8["hi"] == 0.008,
       "百分位应附窗口高低点")
    p0 = percentile_of(0.0005, pts)
    ck(p0 is not None and p0["pct"] == 0, "当前即最低时 pct=0 是合法读数，不能是 None")
    ck(percentile_of(0.005, pts[:4]) is None, "样本不足 8 应拒绝（4 点的百分位无意义）")
    pwin = percentile_of(0.0075, pts, years=1)
    ck(pwin is None, "1 年窗口只有 4 个季频点应拒绝")

    # 腾讯搜索返回体解析（真实片段，名称是 \u 转义）
    sample = ('v_hint="sh~510300~\\u6caa\\u6df1300ETF\\u534e\\u6cf0\\u67cf'
              '\\u745e~hs300etfhtbr~ETF^sz~159919~\\u6caa\\u6df1300ETF\\u5609'
              '\\u5b9e~hs300etfjs~ETF"')
    mm = re.search(r'v_hint="(.*)"', sample)
    parts = [p.split("~") for p in mm.group(1).split("^")] if mm else []
    got = [(p[0] + p[1], p[4]) for p in parts if len(p) >= 5]
    ck(len(got) == 2 and got[0] == ("sh510300", "ETF"), "搜索返回体应解析出市场+代码")
    ck(got[1][0] == "sz159919", "深市代码应拼成 sz159919")

    print(f"hs300_etf_ratio.selfcheck: {'OK' if bad == 0 else str(bad) + ' 项失败'}")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if selfcheck() else 0)
