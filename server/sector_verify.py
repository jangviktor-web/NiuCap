# server/sector_verify.py
"""板块舆情收盘对账（#189）——仅开盘日、收盘后运行。

把「当天 24h 板块舆情」的方向，与「当天收盘行业板块实际涨跌」逐板块对账：
命中/错失、准确率、错因研判、调整建议。落盘人读报告
data/sector_verify/verify_<日期>.md。**只对账、不自动改词表**（同 #188 巡检——
词表是业务判断，报告给证据，人决策后改 sector_sentiment.py）。

口径三件事（对账的生命线，改一个数字结论就变）：
  1. 交易日：scheduler.is_trading_day 一票肯定制。None（网络异常）按交易日
     处理——宁可白跑一次，也不能因一次网络抖动整天不对账（sync_bars 踩过）。
  2. 舆情窗口：近 24h 且 pub_ts ≤ 今日 15:00。15:00 后的盘后快讯影响的是
     明天，计入今天会把「明天的消息」算成「今天判错」。
  3. 实际表现用「超额」不用裸涨跌：板块涨跌 − 上证指数涨跌。大盘涨 2% 时
     板块涨 1% 其实相对弱，裸值会把贝塔当成板块 alpha，对账就失去意义。

未收盘时手动跑只出报告、不计入（K_VRF_OK 不置位、不进 history）——
否则盘前试跑一次，15:30 真正的收盘对账会被「今天已跑过」跳过。
"""
from __future__ import annotations

import collections
import datetime as _dt
import json
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.abspath(__file__))
_VERIFY_DIR = os.path.join(_ROOT, "..", "data", "sector_verify")
_HISTORY_FILE = os.path.join(_VERIFY_DIR, "history.json")
_REPORT_MD = "verify_{date}.md"

DEFAULT_VERIFY_AT = "15:30"
_AT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

K_VRF_TRY = "sector_verify_last_try"      # 最近一次「到点检查」的日期
K_VRF_OK = "sector_verify_last_ok"        # 最近一次「真的对完账」的日期
K_VRF_RESULT = "sector_verify_last_result"  # 一句话结果，给人看

# 新闻口径截止时刻：今日 15:00 前的快讯才可能影响今日收盘
CLOSE_HOUR, CLOSE_MIN = 15, 0
# 超额死区（百分点）：|板块组超额| <= 0.5 记「平」。单日板块相对波动多在
# ±2% 内，0.5 是「不算涨也不算跌」的合理带宽，再小噪声占比会过高。
EXCESS_FLAT = 0.5
# 舆情方向阈值：净情绪/样本数 >= 0.34 才给方向（5正2负=0.43→偏多；4正3负=0.14→中性）
NET_RATIO = 0.34
MIN_N = 3        # 低于此样本数不给方向（进「样本不足未对账」）
CONF_N = 10      # 高置信样本线

# 16 个舆情板块 → 通达信行业板块组（等权均值作该板块的实际表现）。
# 键集必须与 sector_sentiment.SECTOR_KEYWORDS 一致（selfcheck 有断言）。
# 取「代表板块组」而非全量：消费横跨食品饮料/家电/纺服/零售/社服，全取会把
# 「白酒批价上涨」这类舆情稀释掉；组别写在报告里，不认可可改这张表。
SECTOR_BOARDS: Dict[str, List[str]] = {
    "军工": ["航天装备Ⅱ", "航空装备Ⅱ", "地面兵装Ⅱ", "军工电子Ⅱ", "航海装备Ⅱ"],
    "能源": ["煤炭开采", "焦炭Ⅱ", "油服工程", "炼化及贸易"],
    "金融": ["国有大型银行Ⅱ", "股份制银行Ⅱ", "农商行Ⅱ", "城商行Ⅱ",
            "证券Ⅱ", "保险Ⅱ", "多元金融"],
    "科技AI": ["软件开发", "计算机设备", "IT服务Ⅱ"],
    "汽车": ["乘用车", "商用车", "汽车零部件", "汽车服务"],
    "医药": ["化学制药", "中药Ⅱ", "生物制品", "医药商业", "医疗器械", "医疗服务"],
    "半导体": ["半导体", "元件", "光学光电子", "其他电子Ⅱ"],
    "通信": ["通信设备", "通信服务"],
    "消费": ["白酒Ⅱ", "非白酒", "饮料乳品", "食品加工", "调味发酵品Ⅱ",
            "休闲食品", "一般零售", "专业连锁Ⅱ", "互联网电商",
            "旅游及景区", "酒店餐饮"],
    "新能源": ["电池", "光伏设备", "风电设备", "电网设备", "其他电源设备Ⅱ"],
    "地产": ["房地产开发", "房地产服务", "房屋建设Ⅱ"],
    "建材": ["水泥", "装修建材", "玻璃玻纤", "非金属材料Ⅱ"],
    "化工": ["化学原料", "化学制品", "化学纤维", "塑料", "橡胶", "农化制品"],
    "有色": ["工业金属", "小金属", "贵金属", "能源金属", "金属新材料"],
    "农业": ["养殖业", "种植业", "渔业", "饲料", "动物保健Ⅱ", "农产品加工"],
    # 港股行情未接入数据源，无 A 股行业基准 → 单列「无法对账」
    "中概港股": [],
}
NO_BASE_NOTE = "无 A 股行业板块基准（港股行情源未接入）"

_MD_LINE = re.compile(
    r"^- (.+?) \| (.+?) \| (.+?) \| (\w+) \| ([\d.\-]+) \| (\d+) \| (.*?) \| (.*?) \| (.*?) \| (\d+)$")


# ───────────────────────── 配置与状态 ─────────────────────────

def verify_at() -> str:
    """每日对账时间点。环境变量 TICK_SECTOR_VERIFY_AT 覆盖，脏值回落 15:30。"""
    v = (os.environ.get("TICK_SECTOR_VERIFY_AT") or "").strip()
    return v if _AT_RE.match(v) else DEFAULT_VERIFY_AT


def _now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_board(name: str) -> str:
    """板块名归一：去首尾空白 + 去尾部罗马数字后缀（通达信二级行业带 Ⅱ）。
    '农商行Ⅱ' 与 '农商行' 视作同一板块，防映射表与数据源口径不一致。"""
    return re.sub(r"(?:Ⅱ|II|ii)+$", "", (name or "").strip())


# ───────────────────────── 数据源 ─────────────────────────

def _load_news(hours: int = 24) -> List[Dict[str, Any]]:
    """近 N 小时、且发布于今日 15:00 前的快讯。

    服务进程内用 newsfeed._history（已加载才用，**不 import**——模块级 atexit
    保存会让 standalone 进程退出时清空缓存，见 #188），否则解析 md 落盘文件。
    """
    now = _dt.datetime.now()
    lower = int((now - _dt.timedelta(hours=hours)).timestamp())
    close_ts = int(_dt.datetime(now.year, now.month, now.day,
                                CLOSE_HOUR, CLOSE_MIN).timestamp())
    upper = min(close_ts, int(now.timestamp()))  # 盘前试跑时上限=现在
    out: List[Dict[str, Any]] = []
    _nf = sys.modules.get("newsfeed")
    if _nf is not None:
        try:
            for it in list(getattr(_nf, "_history", []) or []):
                pub = int(it.get("pub_ts") or it.get("epoch") or 0)
                if not (lower <= pub <= upper):
                    continue
                out.append({"content": it.get("content", ""),
                            "sentiment": it.get("sentiment")
                            or {"tone": "neutral", "score": 0.0},
                            "stocks": it.get("stocks") or [],
                            "pub_ts": pub})
        except Exception:
            out = []
    if not out:
        try:
            with open(os.path.join(_ROOT, "..", "data", "news_cache.md"),
                      encoding="utf-8") as f:
                for ln in f:
                    m = _MD_LINE.match(ln.rstrip("\n"))
                    if not m:
                        continue
                    _tm, _id, _src, tone, score, _ep, stocks_s, _url, content, pub = m.groups()
                    pub = int(pub)
                    if not (lower <= pub <= upper):
                        continue
                    stocks = []
                    for part in stocks_s.split(";"):
                        part = part.strip()
                        if ":" in part:
                            sym, name = part.split(":", 1)
                            stocks.append({"symbol": sym.strip(), "name": name.strip()})
                    out.append({"content": content,
                                "sentiment": {"tone": tone, "score": float(score)},
                                "stocks": stocks, "pub_ts": pub})
        except Exception:
            return []
    return out


def _sector_stats(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """复刻 sector_sentiment.aggregate 的归因逻辑（ticker 反查优先、关键词回落、
    板块词修正词典方向、时效衰减加权），但按板块收集对账需要的明细。

    为什么不用 aggregate 直接出数：它的 samples 每板块只留 3 条且不带 pub_ts，
    对账要算「旧闻占比」「被修正占比」，得自己走一遍同样的原语——
    与 #188 巡检同做法，口径与面板严格一致（衰减权重也复用 ss.age_weight）。
    """
    import sector_sentiment as ss
    import stock_sector_map as ssm

    per: Dict[str, Dict[str, Any]] = collections.defaultdict(lambda: {
        "n": 0, "pos": 0, "neg": 0, "neu": 0, "corrected": 0, "old": 0,
        "wsum": 0.0, "pos_w": 0.0, "neg_w": 0.0,
        "samples": [], "pubs": []})
    now = _dt.datetime.now()
    prev_close = int(_dt.datetime(now.year, now.month, now.day,
                                  CLOSE_HOUR, CLOSE_MIN).timestamp()) - 24 * 3600
    # 时效参考时刻与面板一致：窗口内最新快讯的 pub_ts（纯函数，不读墙钟）
    ref = max((int(it.get("pub_ts") or 0) for it in items), default=0)
    for it in items:
        content = it.get("content", "") or ""
        tone = (it.get("sentiment") or {}).get("tone", "neutral")
        pub = int(it.get("pub_ts") or 0)
        w = ss.age_weight(pub, ref)
        secs = ssm.sectors_for_stocks(it.get("stocks")) or ss.map_sectors(content)
        if not secs:
            continue
        cats = ss.detect_catalysts(content, secs)
        sec_net = cats["sec_net"]
        eff = tone
        if sec_net > 0:
            eff = "pos"
        elif sec_net < 0:
            eff = "neg"
        for s in secs:
            a = per[s]
            a["n"] += 1
            a["wsum"] += w
            if eff == "pos":
                a["pos"] += 1
                a["pos_w"] += w
            elif eff == "neg":
                a["neg"] += 1
                a["neg_w"] += w
            else:
                a["neu"] += 1
            if sec_net and eff != tone:
                a["corrected"] += 1
            if pub and pub < prev_close:
                a["old"] += 1
            a["pubs"].append(pub)
            if len(a["samples"]) < 3 and (sec_net or tone != "neutral"):
                a["samples"].append({
                    "text": content[:60], "tone": eff, "origin": tone,
                    "corrected": bool(sec_net) and eff != tone})
    return per


def _board_changes() -> Tuple[Dict[str, Dict[str, Any]], float]:
    """全市场行业板块行情 → {规范名: {name, change_pct, main_net}} 与等权均值。"""
    import westock as ws
    rows = ws.sector_rank(limit=200)
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = _norm_board(r.get("name"))
        if key:
            out[key] = {"name": r.get("name"), "change_pct": float(r.get("change_pct", 0.0)),
                        "main_net": float(r.get("main_net", 0.0) or 0.0)}
    eq = sum(v["change_pct"] for v in out.values()) / len(out) if out else 0.0
    return out, eq


def _benchmark(fallback_eq: float) -> Tuple[float, str]:
    """大盘基准优先上证指数，取不到回落「全行业板块等权均值」。"""
    try:
        import datasource as ds
        q = ds.quote_tencent(["sh000001"], use_cache=False)
        v = q.get("sh000001") or {}
        if v.get("change_pct") is not None:
            return float(v["change_pct"]), "上证指数"
    except Exception:
        pass
    return fallback_eq, "全行业板块等权均值"


# ───────────────────────── 判定逻辑 ─────────────────────────

def _dir_of(a: Dict[str, Any]) -> str:
    """样本数够 + 净情绪占比过阈值才给方向，否则中性（样本不足单列）。

    ponytail: 时效衰减口径（#189 对账驱动）——带 wsum 时按衰减加权净情绪判定
    （旧闻降权，与面板 heat_table 严格一致）；wsum 缺失（自检探针/旧调用）
    回落原始计数口径，两套口径阈值同为 NET_RATIO。
    """
    n = a["n"]
    if n < MIN_N:
        return "样本不足"
    wsum = a.get("wsum") or 0.0
    if wsum > 0:
        net_w = (a.get("pos_w", 0.0) - a.get("neg_w", 0.0))
        if net_w > 0 and net_w / wsum >= NET_RATIO:
            return "偏多"
        if net_w < 0 and -net_w / wsum >= NET_RATIO:
            return "偏空"
        return "中性"
    net = a["pos"] - a["neg"]
    if net > 0 and net / n >= NET_RATIO:
        return "偏多"
    if net < 0 and -net / n >= NET_RATIO:
        return "偏空"
    return "中性"


def _actual_dir(excess: Optional[float]) -> Optional[str]:
    if excess is None:
        return None
    if excess > EXCESS_FLAT:
        return "涨"
    if excess < -EXCESS_FLAT:
        return "跌"
    return "平"


def _diagnose(r: Dict[str, Any]) -> Tuple[str, str]:
    """错失归因。优先级：舆情时效 > 样本不足 > 方向矛盾 > 相对强弱。
    每条都带回可核查的证据字段，不让报告只给结论不给据。"""
    if r["old_ratio"] >= 0.5:
        return ("舆情时效",
                f"主导新闻 {r['old_ratio']:.0%} 发布于前一日收盘前，市场已提前反映，"
                "当日无异动信息")
    if r["n"] < MIN_N:
        return ("样本不足", f"仅 {r['n']} 条命中撑起方向，噪声大")
    if r["sent_dir"] == "偏多" and r["raw"] < 0:
        return ("利好未兑现",
                "舆情偏多但板块组收跌：消息面未扭转资金面，或利好高开兑现")
    if r["sent_dir"] == "偏多":
        # ponytail: excess 用 .get() 兜底——_diagnose 也被 selfcheck 的
        # 探针 dict 直接调用，字段不全时不该 KeyError 崩整轮对账。
        return ("跑输大盘",
                f"板块组涨 {r['raw']:+.2f}% 但跑输大盘（超额 {r.get('excess') or 0.0:+.2f}%），"
                "资金未向该板块集中")
    if r["sent_dir"] == "偏空" and r["raw"] > 0:
        return ("利空未发酵",
                "舆情偏空但板块组收涨：利空被市场消化，或超跌反弹")
    return ("冲击有限",
            f"板块组跌 {r['raw']:+.2f}% 但抗跌（超额 {r.get('excess') or 0.0:+.2f}%），"
            "利空冲击小于预期")


# ───────────────────────── 主流程 ─────────────────────────

def run_verify(hours: int = 24, why: str = "手动") -> Dict[str, Any]:
    """执行一次收盘对账。返回结构化结果 + 落盘 markdown 报告。

    计入当日（置 K_VRF_OK、进 history）的条件：是交易日且已过 15:00。
    盘前/盘中/非交易日手动跑只出报告供查看格式，不算数。
    """
    now = _dt.datetime.now()
    today = now.date().isoformat()
    past_close = (now.hour, now.minute) >= (CLOSE_HOUR, CLOSE_MIN)
    try:
        import scheduler as _sch
        is_td = _sch.is_trading_day(now)
    except Exception:
        is_td = None      # 判不出来按交易日处理（项目惯例：宁可白跑）
    counts = bool(is_td) and past_close

    items = _load_news(hours)
    if not items:
        return {"ok": False, "reason": "近 24h 无快讯（新闻缓存为空？）"}
    per = _sector_stats(items)
    try:
        boards, eq_mean = _board_changes()
    except Exception as e:
        return {"ok": False, "reason": f"行业板块行情获取失败：{type(e).__name__}: {e}"}
    if not boards:
        return {"ok": False, "reason": "行业板块行情为空（westock 数据源异常？）"}
    bench, bench_name = _benchmark(eq_mean)

    rows: List[Dict[str, Any]] = []
    for sec, board_names in SECTOR_BOARDS.items():
        a = per.get(sec) or {"n": 0, "pos": 0, "neg": 0, "neu": 0,
                             "corrected": 0, "old": 0,
                             "wsum": 0.0, "pos_w": 0.0, "neg_w": 0.0,
                             "samples": [], "pubs": []}
        net = a["pos"] - a["neg"]
        sdir = _dir_of(a)
        conf = "高" if a["n"] >= CONF_N else ("中" if a["n"] >= MIN_N else "低")
        hit_boards = [b for b in board_names if _norm_board(b) in boards]
        raw = excess = main_net = None
        if hit_boards:
            raw = sum(boards[_norm_board(b)]["change_pct"] for b in hit_boards) / len(hit_boards)
            main_net = sum(boards[_norm_board(b)]["main_net"] for b in hit_boards) / 1e8
            excess = raw - bench
        adir = _actual_dir(excess)
        if sdir == "样本不足":
            result = "样本不足未对账"
        elif not hit_boards:
            result = "无基准未对账"
        elif sdir == "中性":
            result = "中性命中" if adir == "平" else "中性偏离"
        elif adir == ("涨" if sdir == "偏多" else "跌"):
            result = "命中"
        else:
            result = "错失"
        r = {"sector": sec, "n": a["n"], "pos": a["pos"], "neg": a["neg"],
             "net": net, "sent_dir": sdir, "conf": conf,
             "boards": hit_boards or board_names, "raw": raw, "excess": excess,
             "actual_dir": adir, "result": result,
             "corrected_ratio": (a["corrected"] / a["n"]) if a["n"] else 0.0,
             "old_ratio": (a["old"] / a["n"]) if a["n"] else 0.0,
             "main_net": main_net, "samples": a["samples"]}
        if result == "错失":
            r["cause"], r["cause_detail"] = _diagnose(r)
        rows.append(r)

    denom = [r for r in rows if r["result"] in ("命中", "错失")]
    hits = [r for r in denom if r["result"] == "命中"]
    acc = (len(hits) / len(denom)) if denom else None
    hi = [r for r in denom if r["conf"] == "高"]
    acc_hi = (sum(1 for r in hi if r["result"] == "命中") / len(hi)) if hi else None

    summary = {"date": today, "at": _now_text(), "why": why, "hours": hours,
               "items": len(items), "bench": bench, "bench_name": bench_name,
               "counted": counts, "is_trading_day": is_td,
               "n_dir": len(denom), "hits": len(hits),
               "acc": acc, "acc_hi": acc_hi, "n_hi": len(hi)}
    # 调整建议：与 md 报告同源（_suggestions），随 API 返回让前端直给结论，
    # 不改词表——人看建议后自己回 sector_sentiment.py 改（#188/#189 原则）。
    wrong = [r for r in rows if r["result"] == "错失"]
    suggestions = _suggestions(summary, rows, wrong)
    report_path = _write_report(today, summary, rows)

    import store as _st
    if counts:
        _st.meta_set(K_VRF_RESULT,
                     f"{_now_text()} 对账 {len(denom)} 个方向板块，"
                     f"命中 {len(hits)}（准确率 "
                     f"{('%.0f%%' % (acc * 100)) if acc is not None else '—'}）")
        _st.meta_set(K_VRF_OK, today)
        _history_save(today, summary)
    else:
        note = "非交易日" if is_td is False else ("未收盘" if not past_close else "")
        _st.meta_set(K_VRF_RESULT,
                     f"{_now_text()} 试跑（{note or '不计入'}）：{len(items)} 条 / "
                     f"{len(denom)} 个方向板块，命中 {len(hits)}")
    return {"ok": True, "summary": summary, "rows": rows,
            "suggestions": suggestions, "report": report_path}


def _write_report(today: str, summary: Dict[str, Any],
                  rows: List[Dict[str, Any]]) -> str:
    """人读报告：准确率总览 + 逐板块对账表 + 错因研判 + 调整建议。"""
    path = os.path.join(_VERIFY_DIR, _REPORT_MD.format(date=today))
    L: List[str] = []
    L.append(f"# 板块舆情收盘对账 {today}")
    L.append("")
    flag = "✅ 计入当日" if summary["counted"] else "⚠️ 试跑（不计入当日对账）"
    L.append(f"- 对账时间：{summary['at']}（{summary['why']}）· {flag}")
    L.append(f"- 大盘基准：{summary['bench_name']} {summary['bench']:+.2f}%"
             f" · 舆情窗口：近 {summary['hours']}h 且 ≤ 今日 15:00，{summary['items']} 条快讯")
    if summary["acc"] is not None:
        hi = (f"，高置信子集 {summary['acc_hi']:.0%}（{summary['n_hi']} 个）"
              if summary["acc_hi"] is not None else "")
        L.append(f"- 准确率：**{summary['acc']:.0%}**"
                 f"（{summary['hits']}/{summary['n_dir']} 个方向板块{hi}）")
    else:
        L.append("- 准确率：— （今日无足够方向板块）")
    L.append("")
    L.append("> 口径：舆情方向 = 净情绪(板块词修正后)/样本数 ≥ 34%；"
             "实际方向 = 板块组等权涨跌 − 大盘基准，超额 ±0.5% 内记「平」。"
             "只对账、不自动改词表（同 #188）。")
    L.append("")

    L.append("## 逐板块对账")
    L.append("")
    L.append("| 板块 | 舆情(正/负/净) | 方向·置信 | 板块组涨跌 | 超额 | 实际 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    mark = {"命中": "✅", "错失": "❌", "中性命中": "➖", "中性偏离": "⚠️",
            "样本不足未对账": "·", "无基准未对账": "·"}
    for r in rows:
        raw = f"{r['raw']:+.2f}%" if r["raw"] is not None else "—"
        exc = f"{r['excess']:+.2f}%" if r["excess"] is not None else "—"
        L.append(f"| {r['sector']} | {r['pos']}/{r['neg']}/{r['net']:+d}"
                 f"（n={r['n']}） | {r['sent_dir']}·{r['conf']} | {raw} | {exc} "
                 f"| {r['actual_dir'] or '—'} | {mark.get(r['result'], '')}{r['result']} |")
    L.append("")

    wrong = [r for r in rows if r["result"] == "错失"]
    if wrong:
        L.append(f"## 错因研判（{len(wrong)} 个）")
        L.append("")
        for r in wrong:
            L.append(f"### ❌ {r['sector']}：舆情{r['sent_dir']}，实际{r['actual_dir']}"
                     f"（超额 {r['excess']:+.2f}%）")
            L.append(f"- 归因：**{r['cause']}** —— {r['cause_detail']}")
            extra = []
            if r["corrected_ratio"] >= 0.5:
                extra.append(f"{r['corrected_ratio']:.0%} 样本方向被板块词修正（修正机制可能过度）")
            if r["main_net"] is not None and abs(r["main_net"]) >= 1:
                extra.append(f"板块组主力净流入 {r['main_net']:+.1f} 亿")
            if extra:
                L.append(f"- 佐证：{'；'.join(extra)}")
            for s in r["samples"][:2]:
                tag = "〔修正〕" if s["corrected"] else ""
                L.append(f"- 当日主导舆情{tag}：[{s['origin']}→{s['tone']}] {s['text']}")
            L.append("")

    causes = collections.Counter(r["cause"] for r in wrong)
    if causes:
        L.append("## 错因统计")
        L.append("")
        for c, n in causes.most_common():
            L.append(f"- {c}：{n} 个")
        L.append("")

    L.append("## 调整建议")
    L.append("")
    for s in _suggestions(summary, rows, wrong):
        L.append(f"- {s}")
    L.append("")

    skip = [r for r in rows if r["result"] in ("样本不足未对账", "无基准未对账")]
    if skip:
        L.append("## 未对账板块")
        L.append("")
        for r in skip:
            why_ = NO_BASE_NOTE if r["result"] == "无基准未对账" else f"仅 {r['n']} 条命中"
            L.append(f"- {r['sector']}：{why_}")
        L.append("")

    L.append("## 板块组映射（可调整）")
    L.append("")
    for r in rows:
        if r["boards"]:
            L.append(f"- {r['sector']} ← {'、'.join(r['boards'])}")
    L.append("")

    try:
        os.makedirs(_VERIFY_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
        os.replace(tmp, path)
    except Exception as e:
        print(f"[sector-verify] 报告落盘失败：{e}")
    return path


def _suggestions(summary: Dict[str, Any], rows: List[Dict[str, Any]],
                 wrong: List[Dict[str, Any]]) -> List[str]:
    """基于本次对账数据给调整方向。只建议，不动手——同 #188 原则。"""
    out: List[str] = []
    n = summary["n_dir"]
    acc = summary["acc"]
    if n >= 3 and acc is not None and acc < 0.5:
        out.append("**整体准确率低于随机**：先核对板块组映射是否失真"
                   "（如消费横跨 11 个板块被单一新闻稀释），再查新闻窗口口径。")
    if wrong:
        stale = sum(1 for r in wrong if r["cause"] == "舆情时效")
        small = sum(1 for r in wrong if r["cause"] == "样本不足")
        corr = sum(1 for r in wrong if r["corrected_ratio"] >= 0.5)
        if stale >= max(1, len(wrong) * 0.5):
            out.append(f"**{stale}/{len(wrong)} 个错失由『舆情时效』驱动**：主导新闻非当日发布。"
                       "可考虑①面板 24h 窗口按 pub_ts 加时效衰减（越新权重越高）；"
                       "②或把对账对照窗口收紧到 12h，只看隔夜+当日消息。")
        if small >= max(1, len(wrong) * 0.5):
            out.append(f"**{small}/{len(wrong)} 个错失来自小样本板块**：面板对 n<3 板块"
                       "标注「低置信」（与 #188 R5 联动），别让 1-2 条噪声撑起方向。")
        if corr >= max(1, len(wrong) * 0.5):
            out.append(f"**{corr}/{len(wrong)} 个错失样本的方向被板块词大幅修正**："
                       "板块词修正机制可能过度（联动 #188 R3），抽查这几个板块的"
                       "SECTOR_BULLISH/BEARISH 是否收了品类词。")
        bull_w = sum(1 for r in wrong if r["sent_dir"] == "偏多")
        bear_w = sum(1 for r in wrong if r["sent_dir"] == "偏空")
        if bull_w != bear_w and (bull_w == 0 or bear_w == 0):
            out.append(f"**方向偏置**：错失全部来自「舆情{bull_w and '偏多' or '偏空'}」侧，"
                       "检查该侧词表是否系统性过灵（利好词多于利空词？）。")
    if acc is not None and acc >= 0.7 and n >= 5:
        out.append("方向机制整体可信，继续积累对账历史（history.json 存 90 天），"
                   "样本多了再按板块细分准确率。")
    if not out:
        out.append("样本量不足以下结论，先积累几日对账历史。")
    return out


# ───────────────────────── 历史与调度 ─────────────────────────

def _history_load() -> Dict[str, Any]:
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _history_save(today: str, summary: Dict[str, Any]) -> None:
    """只留近 90 天。写失败不致命（趋势展示退化为空），别让对账崩在落盘上。"""
    try:
        os.makedirs(_VERIFY_DIR, exist_ok=True)
        hist = _history_load()
        hist[today] = {"acc": summary["acc"], "hits": summary["hits"],
                       "n_dir": summary["n_dir"], "items": summary["items"]}
        for k in sorted(hist)[:-90]:
            hist.pop(k, None)
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
        os.replace(tmp, _HISTORY_FILE)
    except Exception as e:
        print(f"[sector-verify] 历史落盘失败（不影响本次对账）：{e}")


_verify_thread: Optional[threading.Thread] = None
_verify_stop = threading.Event()


def verify_state() -> Dict[str, Any]:
    """给后台面板看的调度状态 + 近期准确率趋势。只读，不触发对账。"""
    try:
        import store as _st
        try_ = _st.meta_get(K_VRF_TRY, "") or ""
        ok = _st.meta_get(K_VRF_OK, "") or ""
        result = _st.meta_get(K_VRF_RESULT, "") or ""
    except Exception as e:
        try_, ok, result = "", "", f"读取调度状态失败：{type(e).__name__}: {e}"
    hist = _history_load()
    recent = [{"date": d, **hist[d]} for d in sorted(hist)[-10:]]
    return {
        "enabled": _verify_thread is not None and _verify_thread.is_alive(),
        "at": verify_at(),
        "today": _dt.date.today().isoformat(),
        "last_try": try_,
        "last_ok": ok,
        "last_result": result,
        "done_today": ok == _dt.date.today().isoformat(),
        "recent": recent,
    }


def start_daily_verify(hours: int = 24) -> bool:
    """拉起每日对账调度线程（daemon）。重复调用只启动一次。

    三原则照抄 #98/#188：① 状态存 meta 表（重启知「今天对过没」不补跑）；
    ② 日期字符串比对（休眠/时钟漂移不漏不重）；③ 非开盘日跳过，
    判不出交易日（None）按开盘日处理——宁可白跑，不能因网络抖动整天不对账。
    """
    global _verify_thread
    if _verify_thread is not None and _verify_thread.is_alive():
        return False
    _verify_stop.clear()

    def _loop():
        last_checked = ""
        while not _verify_stop.is_set():
            try:
                import store as _st
                today = _dt.date.today().isoformat()
                if last_checked != today and _st.meta_get(K_VRF_TRY, "") == today:
                    last_checked = today      # 重启后从 meta 恢复，不重复检查
                now = _dt.datetime.now()
                hh, mm = (int(x) for x in verify_at().split(":"))
                if (now.hour, now.minute) >= (hh, mm) and last_checked != today:
                    last_checked = today
                    _st.meta_set(K_VRF_TRY, today)
                    if _st.meta_get(K_VRF_OK, "") == today:
                        print("[sector-verify] 今天已对账过，跳过")
                    else:
                        import scheduler as _sch
                        td = _sch.is_trading_day(now)
                        if td is False:
                            print("[sector-verify] 今天非交易日，跳过对账")
                        else:
                            r = run_verify(hours=hours, why="每日自动")
                            print(f"[sector-verify] 完成："
                                  f"{r.get('summary') or r.get('reason')}")
            except Exception as e:
                print(f"[sector-verify] 对账异常（不影响其他功能）：{e}")
            _verify_stop.wait(60)

    _verify_thread = threading.Thread(target=_loop, name="sector-verify", daemon=True)
    _verify_thread.start()
    return True


# ───────────────────────── 自检 ─────────────────────────

def selfcheck() -> int:
    """模块自检：配置、映射覆盖、判定逻辑探针、报告渲染（写临时文件）。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    import sector_sentiment as ss

    ok("默认对账时间 15:30 且可被环境变量覆盖",
       verify_at() == DEFAULT_VERIFY_AT or os.environ.get("TICK_SECTOR_VERIFY_AT"))
    # 映射必须覆盖全部舆情板块——漏一个该板块就永远不进对账表
    ok("板块组映射覆盖全部舆情板块",
       set(SECTOR_BOARDS) == set(ss.SECTOR_KEYWORDS))
    ok("板块组映射无空名/重复名",
       all(b and len(set(bs)) == len(bs) for bs in SECTOR_BOARDS.values() for b in bs))
    ok("板块名归一去 Ⅱ 后缀", _norm_board("农商行Ⅱ") == "农商行"
       and _norm_board(" 软件开发 ") == "软件开发")

    # 方向阈值探针
    ok("偏多需净情绪占比过 34%",
       _dir_of({"n": 7, "pos": 5, "neg": 2}) == "偏多"
       and _dir_of({"n": 7, "pos": 4, "neg": 3}) == "中性")
    # ponytail: 偏空探针须真的过 NET_RATIO=0.34——n=6/pos=2/neg=4 时
    # -net/n=0.333 恰好卡在阈值下会被判中性（第一版就踩了这个坑）。
    ok("偏空对称判定", _dir_of({"n": 7, "pos": 2, "neg": 5}) == "偏空")
    ok("样本不足单列（n<3 不给方向）",
       _dir_of({"n": 2, "pos": 2, "neg": 0}) == "样本不足")
    # ponytail: 时效衰减口径（wsum 存在走加权；探针无 wsum 仍走原始计数口径）
    ok("衰减口径：旧闻降权后净情绪不过阈 → 中性",
       _dir_of({"n": 4, "pos": 3, "neg": 1, "wsum": 1.75, "pos_w": 0.75, "neg_w": 1.0}) == "中性")
    ok("衰减口径：新鲜利好主导 → 偏多",
       _dir_of({"n": 4, "pos": 3, "neg": 1, "wsum": 1.75, "pos_w": 1.0, "neg_w": 0.25}) == "偏多")
    ok("衰减口径：样本不足仍优先（n<3 即使带权重）",
       _dir_of({"n": 2, "pos": 2, "neg": 0, "wsum": 2.0, "pos_w": 2.0, "neg_w": 0.0}) == "样本不足")
    # 超额死区探针
    ok("超额 ±0.5% 内记平",
       _actual_dir(0.4) == "平" and _actual_dir(-0.5) == "平"
       and _actual_dir(0.6) == "涨" and _actual_dir(-0.9) == "跌")
    # 归因优先级探针：旧闻占比过半 → 舆情时效，盖过其他原因
    r1 = {"old_ratio": 0.6, "n": 8, "sent_dir": "偏多", "raw": -1.0}
    ok("归因：旧闻过半优先判舆情时效", _diagnose(r1)[0] == "舆情时效")
    r2 = {"old_ratio": 0.1, "n": 2, "sent_dir": "偏多", "raw": -1.0}
    ok("归因：样本不足次之", _diagnose(r2)[0] == "样本不足")
    r3 = {"old_ratio": 0.1, "n": 8, "sent_dir": "偏多", "raw": -1.0}
    ok("归因：偏多却收跌=利好未兑现", _diagnose(r3)[0] == "利好未兑现")
    r4 = {"old_ratio": 0.1, "n": 8, "sent_dir": "偏多", "raw": 0.8, "excess": -0.9}
    ok("归因：偏多跑输大盘", _diagnose(r4)[0] == "跑输大盘")
    r5 = {"old_ratio": 0.1, "n": 8, "sent_dir": "偏空", "raw": 0.8}
    ok("归因：偏空却收涨=利空未发酵", _diagnose(r5)[0] == "利空未发酵")

    # 报告渲染冒烟（写临时目录，不碰正式产物）
    try:
        global _VERIFY_DIR
        real = _VERIFY_DIR
        _VERIFY_DIR = os.path.join(_ROOT, "..", "data", "sector_verify", "_selfcheck")
        p = _write_report("2099-01-01", {
            "at": "2099-01-01 00:00:00", "why": "自检", "hours": 24, "items": 10,
            "bench": -0.43, "bench_name": "上证指数", "counted": True,
            "n_dir": 2, "hits": 1, "acc": 0.5, "acc_hi": None, "n_hi": 0}, [
            {"sector": "军工", "n": 8, "pos": 6, "neg": 2, "net": 4, "sent_dir": "偏多",
             "conf": "高", "boards": ["航天装备Ⅱ"], "raw": 1.2, "excess": 1.63,
             "actual_dir": "涨", "result": "命中", "corrected_ratio": 0.1,
             "old_ratio": 0.1, "main_net": 3.2, "samples": []},
            {"sector": "汽车", "n": 5, "pos": 2, "neg": 3, "net": -1, "sent_dir": "偏空",
             "conf": "中", "boards": ["乘用车"], "raw": 0.4, "excess": 0.83,
             "actual_dir": "涨", "result": "错失", "corrected_ratio": 0.6,
             "old_ratio": 0.1, "main_net": -1.0,
             "cause": "利空未发酵", "cause_detail": "x",
             "samples": [{"text": "样本", "tone": "neg", "origin": "pos",
                          "corrected": True}]}])
        ok("报告渲染冒烟通过", os.path.exists(p) and os.path.getsize(p) > 200)
        try:
            os.remove(p)
        except OSError:
            pass
        _VERIFY_DIR = real
    except Exception as e:
        ok(f"报告渲染冒烟通过（异常：{e}）", False)

    # 调度状态可读（不触发对账）
    try:
        st = verify_state()
        ok("调度状态可读（含 at/next/recent）",
           st.get("at") == DEFAULT_VERIFY_AT and "recent" in st)
    except Exception as e:
        ok(f"调度状态可读（异常：{e}）", False)

    print(f"\n{'✅ 全部通过' if not fails else '❌ %d 项失败' % fails}")
    return fails


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="板块舆情收盘对账")
    ap.add_argument("--run", action="store_true", help="立即执行一次对账")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--why", default="命令行")
    ap.add_argument("--state", action="store_true", help="只看调度状态")
    args = ap.parse_args()
    if args.state:
        import json as _json
        print(_json.dumps(verify_state(), ensure_ascii=False, indent=2))
        return 0
    if args.run:
        import json as _json
        r = run_verify(hours=args.hours, why=args.why)
        print(_json.dumps(r.get("summary") or {"ok": False, "reason": r.get("reason")},
                          ensure_ascii=False, indent=2))
        if r.get("ok"):
            print(f"报告: {r['report']}")
        return 0 if r.get("ok") else 1
    return selfcheck()


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
