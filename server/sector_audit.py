#!/usr/bin/env python3
"""板块舆情规则每日巡检（task #188）。

为什么要有这个模块：
  2026-10-13 人工回溯 24h 快讯，发现词表被地缘实体词（伊朗/乌克兰/胡塞…）
  和泛化措辞词（发布/增长/上涨…）污染——军工 288 条命中里 128 条是纯外交
  辞令。这类问题靠人定期回溯才发现，太滞后。本模块把「回溯→找疑点→出
  报告」固化成每日自动跑，人只看报告做决策。

设计取舍——巡检只报告、不自动改词表：
  词表是业务判断（"无人机"利好军工没错，但"俄军袭击"算不算？），自动改
  可能把真相关改成不相关。所以产出是「疑点清单 + 可执行建议」，改不改
  人说了算。防词表被改回归的断言已由 sector_sentiment.selfcheck 负责，
  这里双保险。

数据源优先内存滚动缓冲（newsfeed._history），standalone 跑时回落解析
data/news_cache.md（落盘文件，gitignore 覆盖，不会进版本库）。
"""
import collections
import datetime as _dt
import json
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

K_AUDIT_TRY = "sector_audit_last_try"      # 最近一次「到点检查」的日期
K_AUDIT_OK = "sector_audit_last_ok"        # 最近一次「真的跑完」的日期
K_AUDIT_RESULT = "sector_audit_last_result"  # 一句话结果，给人看
DEFAULT_AUDIT_AT = "21:00"                 # 收盘后 1.5h，24h 窗口数据齐
_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_AUDIT_DIR = os.path.join(_ROOT, "data", "sector_audit")
_HISTORY_FILE = os.path.join(_AUDIT_DIR, "history.json")   # {date: {sector: n}} 近30天
_REPORT_MD = os.path.join(_AUDIT_DIR, "audit_{date}.md")

_audit_thread: Optional[threading.Thread] = None
_audit_stop = threading.Event()

# ── 疑点规则词表（与 sector_sentiment 的词表修复保持联动）──────────────────
# R1 地缘实体：出现在军工/能源归类词里 = 灌水（见 2026-10-13 修正注释）
GEO_TERMS = {"伊朗", "俄乌", "乌克兰", "以色列", "胡塞", "普京", "基辅", "莫斯科",
             "克里姆林宫", "中东", "五角大楼", "也门", "萨那", "霍尔木兹", "北约",
             "空袭", "海湾", "俄军"}
# R2 泛化措辞/结果词：出现在任何板块情绪词里 = 会把无关新闻刷成利好/利空
VAGUE_TERMS = {"增长", "上涨", "下跌", "发布", "推出", "升级", "合作", "签约",
               "涨停", "异动", "拉升", "板块走强", "需求", "政策", "地缘",
               "利率", "AI", "突破", "回暖"}
# R3 修正率阈值：被板块词修正方向的样本占比过高 = 词表方向与词典系统性冲突
CORRECT_RATE_WARN = 0.30
# R4 命中数突增阈值：超过近 7 次巡检中位数的 N 倍 = 可能有新灌水源
SPIKE_FACTOR = 2.0
# R5 小样本极值：n<3 且净情绪=±1 = 全对但样本极少，展示置信度低
SMALL_N = 3


def audit_at() -> str:
    """每日巡检时间点。环境变量 TICK_SECTOR_AUDIT_AT 覆盖，脏值回落 21:00。"""
    v = (os.environ.get("TICK_SECTOR_AUDIT_AT") or "").strip()
    return v if _AT_RE.match(v) else DEFAULT_AUDIT_AT


def _now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ───────────────────────── 数据源 ─────────────────────────

_MD_LINE = re.compile(
    r"^- (.+?) \| (.+?) \| (.+?) \| (\w+) \| ([\d.\-]+) \| (\d+) \| (.*?) \| (.*?) \| (.*?) \| (\d+)$")


def _load_items(hours: int = 24) -> List[Dict[str, Any]]:
    """近 N 小时快讯。服务进程内用 newsfeed._history（已加载则用，不主动 import），
    否则解析 md 落盘文件（standalone CLI 唯一数据源）。

    为什么不能 import newsfeed：模块级 atexit.register(_save_history_md) 会让
    standalone 进程退出时保存内存 _history——本进程从未 ensure_history_loaded()，
    _history 为空，会把服务维护的 24h 缓存清空（已由 _save_history_md 内置空缓冲
    保护兜底，但这里从源头规避：不 import 即无副作用）。
    """
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=hours)
    items: List[Dict[str, Any]] = []
    _nf = sys.modules.get("newsfeed")  # 仅服务进程内已加载时才非空
    if _nf is not None:
        try:
            for it in list(getattr(_nf, "_history", []) or []):
                pub = it.get("pub_ts") or it.get("epoch") or 0
                if pub and _dt.datetime.fromtimestamp(int(pub)) >= cutoff:
                    items.append({"content": it.get("content", ""),
                                  "tone": (it.get("sentiment") or {}).get("tone", "neutral"),
                                  "stocks": it.get("stocks") or []})
        except Exception:
            items = []
    if not items:
        try:
            with open(os.path.join(_ROOT, "data", "news_cache.md"), encoding="utf-8") as f:
                for ln in f:
                    m = _MD_LINE.match(ln.rstrip("\n"))
                    if not m:
                        continue
                    tm, _id, _src, tone, _sc, _ep, stocks_s, _url, content, pub = m.groups()
                    try:
                        if _dt.datetime.fromtimestamp(int(pub)) < cutoff:
                            continue
                    except Exception:
                        pass
                    stocks = []
                    for part in stocks_s.split(";"):
                        part = part.strip()
                        if ":" in part:
                            sym, name = part.split(":", 1)
                            stocks.append({"symbol": sym.strip(), "name": name.strip()})
                    items.append({"content": content, "tone": tone, "stocks": stocks})
        except Exception:
            return []
    return items


# ───────────────────────── 巡检规则 ─────────────────────────

def _hit_terms(content: str, terms: List[str]) -> List[str]:
    """terms 命中 content 的具体词（中文子串 / 英文词边界，与 map_sectors 一致）。"""
    low = (content or "").lower()
    out = []
    for t in terms:
        if t.isascii():
            if re.search(r"(?<![a-z])" + re.escape(t.lower()) + r"(?![a-z])", low):
                out.append(t)
        elif t in (content or ""):
            out.append(t)
    return out


def _history_load() -> Dict[str, Dict[str, int]]:
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _history_save(today: str, counts: Dict[str, int]) -> None:
    """只留近 30 天。写失败不致命（R4 会退化为跳过），别让巡检崩在落盘上。"""
    try:
        os.makedirs(_AUDIT_DIR, exist_ok=True)
        hist = _history_load()
        hist[today] = counts
        for k in sorted(hist)[:-30]:
            hist.pop(k, None)
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
        os.replace(tmp, _HISTORY_FILE)
    except Exception as e:
        print(f"[sector-audit] 历史落盘失败（不影响本次巡检）：{e}")


def run_audit(hours: int = 24, why: str = "手动") -> Dict[str, Any]:
    """执行一次巡检。返回结构化结果 + 落盘 markdown 报告。只报告，不改词表。"""
    import sector_sentiment as ss

    items = _load_items(hours)
    # 复刻 aggregate 的归因逻辑（ticker 反查优先，关键词回落），但按板块收集明细
    per_sec: Dict[str, Dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "corrected": 0, "geo_only": 0, "vague_hits": collections.Counter(),
                 "samples": [], "corr_samples": []})
    counts: Dict[str, int] = {}
    for it in items:
        content = it.get("content", "")
        tone = it.get("tone", "neutral")
        secs = ss.map_sectors(content)
        if not secs:
            continue
        cats = ss.detect_catalysts(content, secs)
        sec_net = cats.get("sec_net", 0)
        eff = tone
        if sec_net > 0:
            eff = "pos"
        elif sec_net < 0:
            eff = "neg"
        for s in secs:
            a = per_sec[s]
            a["n"] += 1
            counts[s] = a["n"]
            if sec_net and eff != tone:
                a["corrected"] += 1
                if len(a["corr_samples"]) < 3:
                    a["corr_samples"].append(f"[{tone}→{eff}] {content[:60]}")
            # R1：仅由地缘实体词触发的归类（词表若被改回归，这里立刻现形）
            kw_hits = _hit_terms(content, ss.SECTOR_KEYWORDS.get(s, []))
            if kw_hits and all(k in GEO_TERMS for k in kw_hits):
                a["geo_only"] += 1
                if len(a["samples"]) < 3:
                    a["samples"].append(f"命中{kw_hits}：{content[:60]}")
            # R2：泛化措辞词触发情绪
            for w in cats.get("bull", []) + cats.get("bear", []):
                if w in VAGUE_TERMS:
                    a["vague_hits"][w] += 1
                    if len(a["samples"]) < 3:
                        a["samples"].append(f"情绪词'{w}'：{content[:60]}")

    # ── 逐规则产出疑点 ──
    findings: List[Dict[str, Any]] = []
    hist = _history_load()
    past = [v for d, v in sorted(hist.items())[-7:] if d != _dt.date.today().isoformat()]

    def add(rule: str, sector: str, detail: str, samples: List[str], advice: str, level: str = "warn"):
        findings.append({"rule": rule, "sector": sector, "detail": detail,
                         "samples": samples[:3], "advice": advice, "level": level})

    for s, a in sorted(per_sec.items(), key=lambda kv: -kv[1]["n"]):
        # R1 地缘实体灌水
        if a["geo_only"]:
            add("R1-地缘实体灌水", s, f"{a['geo_only']} 条仅由地缘实体词触发（共 {a['n']} 条）",
                a["samples"], f"检查 {s} 归类词是否混入国家/地名/领导人词；已在 selfcheck 加断言防回归", "high")
        # R2 泛化措辞误判
        if a["vague_hits"]:
            top = "、".join(f"'{w}'×{n}" for w, n in a["vague_hits"].most_common(3))
            add("R2-泛化措辞误判", s, f"泛化情绪词命中：{top}", a["samples"],
                "确认这些词是否会把无关新闻刷成利好/利空；如是，从板块情绪词删除（参考 2026-10-13 修正清单）", "high")
        # R3 修正率异常
        if a["n"] >= 5 and a["corrected"] / a["n"] > CORRECT_RATE_WARN:
            add("R3-修正率异常", s,
                f"{a['corrected']}/{a['n']}（{a['corrected']/a['n']:.0%}）样本方向被板块词修正",
                a["corr_samples"], "板块词方向与词典系统性冲突；逐条确认修正是对还是反向误判", "mid")
        # R4 命中数突增
        if past:
            base = [v.get(s, 0) for v in past if v.get(s)]
            if base:
                med = sorted(base)[len(base) // 2]
                if med > 0 and a["n"] > med * SPIKE_FACTOR:
                    add("R4-命中数突增", s, f"本次 {a['n']} 条，近 7 次中位数 {med} 条（>{SPIKE_FACTOR:.0f}倍）",
                        a["samples"] or a["corr_samples"], "有新灌水源（词表被加宽/突发新闻潮）；确认增长来自真新闻还是误命中", "mid")
        # R5 小样本极值
        if a["n"] < SMALL_N and a["n"] > 0:
            net_dir = "偏多" if a["corrected"] > 0 else "中性"
            add("R5-小样本低置信", s, f"仅 {a['n']} 条命中，净情绪 {net_dir}",
                a["samples"], "样本太少，面板展示时应标注低置信；确认不是 1-2 条噪声撑起一个板块", "low")

    today = _dt.date.today().isoformat()
    _history_save(today, counts)

    summary = {"sectors": len(per_sec), "items": len(items),
               "findings": len(findings),
               "high": sum(1 for f in findings if f["level"] == "high"),
               "at": _now_text(), "hours": hours, "why": why}
    report_path = _write_report(today, summary, findings, per_sec)

    import store as _st
    _st.meta_set(K_AUDIT_RESULT,
                 f"{_now_text()} 巡检 {len(items)} 条/ {len(per_sec)} 板块，"
                 f"{len(findings)} 个疑点（高危 {summary['high']}）")
    # ponytail: 手动跑（CLI/后台按钮）也记 OK——「今天覆盖过」不该分是谁跑的，
    # 否则 21 点调度发现 OK 为空会再跑一遍，报告被同一天覆盖两次。
    _st.meta_set(K_AUDIT_OK, today)
    return {"ok": True, "summary": summary, "findings": findings,
            "report": report_path}


def _write_report(today: str, summary: Dict[str, Any],
                  findings: List[Dict[str, Any]],
                  per_sec: Dict[str, Dict[str, Any]]) -> str:
    """人读报告：疑点清单（含样例与建议动作）+ 各板块命中概览。"""
    path = _REPORT_MD.format(date=today)
    lines = [
        f"# 板块舆情规则巡检报告 {today}",
        "",
        f"- 巡检时间：{summary['at']}（{summary['why']}）",
        f"- 窗口：近 {summary['hours']} 小时，{summary['items']} 条快讯，{summary['sectors']} 个板块",
        f"- 疑点：**{summary['findings']} 个**（高危 {summary['high']} 个）",
        "",
        "> 本报告只列疑点与建议，不自动改词表。确认后改 server/sector_sentiment.py，",
        "> 并跑 selfcheck（内含防回归断言）。",
        "",
    ]
    if not findings:
        lines.append("## ✅ 未发现疑点")
    for lv, name in (("high", "高危"), ("mid", "关注"), ("low", "提示")):
        rows = [f for f in findings if f["level"] == lv]
        if not rows:
            continue
        lines.append(f"## {name}（{len(rows)}）")
        for f in rows:
            lines.append(f"### [{f['rule']}] {f['sector']}")
            lines.append(f"- 现象：{f['detail']}")
            for s in f["samples"]:
                lines.append(f"- 样例：{s}")
            lines.append(f"- 建议：{f['advice']}")
            lines.append("")
    lines.append("## 板块命中概览")
    lines.append("")
    lines.append("| 板块 | 命中 | 被修正 | 仅地缘词触发 | 泛词命中 |")
    lines.append("|---|---|---|---|---|")
    for s, a in sorted(per_sec.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"| {s} | {a['n']} | {a['corrected']} | {a['geo_only']} | "
                     f"{sum(a['vague_hits'].values())} |")
    try:
        os.makedirs(_AUDIT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines))
    except Exception as e:
        print(f"[sector-audit] 报告落盘失败：{e}")
        return ""
    return path


# ───────────────────────── 每日调度（照抄 strategy_eval 的预热模式）──────────
# 三个做法不重新发明：
#   1. 状态存 meta 表 —— 重启后知道「今天跑过没」，不补跑；
#   2. 日期字符串比对 —— 休眠/时钟漂移不漏不重；
#   3. 时间配置脏值回落默认。

def _audit_next_text() -> str:
    hh, mm = (int(x) for x in audit_at().split(":"))
    now = _dt.datetime.now()
    tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tgt <= now:
        tgt += _dt.timedelta(days=1)
    return tgt.strftime("%Y-%m-%d %H:%M")


def audit_state() -> Dict[str, Any]:
    """给后台面板看的调度状态。只读，不触发巡检。"""
    try:
        import store as _st
        try_ = _st.meta_get(K_AUDIT_TRY, "") or ""
        ok = _st.meta_get(K_AUDIT_OK, "") or ""
        result = _st.meta_get(K_AUDIT_RESULT, "") or ""
    except Exception as e:
        try_, ok, result = "", "", f"读取调度状态失败：{type(e).__name__}: {e}"
    today = _dt.date.today().isoformat()
    return {
        "enabled": _audit_thread is not None and _audit_thread.is_alive(),
        "at": audit_at(),
        "today": today,
        "last_try": try_,
        "last_ok": ok,
        "last_result": result,
        "done_today": ok == today,
        "next": _audit_next_text(),
    }


def start_daily_audit(hours: int = 24) -> bool:
    """拉起每日巡检调度线程（daemon）。重复调用只启动一次。"""
    global _audit_thread
    if _audit_thread is not None and _audit_thread.is_alive():
        return False
    _audit_stop.clear()

    def _loop():
        last_checked = ""
        while not _audit_stop.is_set():
            try:
                import store as _st
                today = _dt.date.today().isoformat()
                if last_checked != today and _st.meta_get(K_AUDIT_TRY, "") == today:
                    last_checked = today      # 重启后从 meta 恢复，不重复检查
                now = _dt.datetime.now()
                hh, mm = (int(x) for x in audit_at().split(":"))
                if (now.hour, now.minute) >= (hh, mm) and last_checked != today:
                    last_checked = today
                    _st.meta_set(K_AUDIT_TRY, today)
                    if _st.meta_get(K_AUDIT_OK, "") == today:
                        print("[sector-audit] 今天已巡检过，跳过检查")
                    else:
                        r = run_audit(hours=hours, why="每日自动")
                        if r.get("ok"):
                            _st.meta_set(K_AUDIT_OK, today)
                        print(f"[sector-audit] 完成：{r.get('summary', {})}")
                        # P2：巡检落听后顺手跑 AI 优化提议——只落待审文件，人工勾选才应用。
                        # 独立 try：LLM 未启用/网络错都不能让巡检状态陪葬。
                        try:
                            import sentiment_optimizer as _so
                            pr = _so.run_daily(hours=hours)
                            if pr.get("empty"):
                                print(f"[sentiment-opt] 每日提议：{pr.get('msg')}")
                            else:
                                print(f"[sentiment-opt] 每日提议完成：{pr.get('n_pass', 0)} 条通过护栏 / "
                                      f"{pr.get('n_fail', 0)} 条被拒（待人工勾选，见后台「AI 模型」卡片）")
                        except Exception as e:
                            print(f"[sentiment-opt] 每日提议失败（不影响巡检）：{e}")
            except Exception as e:
                print(f"[sector-audit] 巡检异常（不影响其他功能）：{e}")
            _audit_stop.wait(60)

    _audit_thread = threading.Thread(target=_loop, name="sector-audit", daemon=True)
    _audit_thread.start()
    return True


def selfcheck() -> int:
    """模块自检：调度配置、数据源解析、一次空跑（不落历史）。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    ok("默认巡检时间 21:00 且可被环境变量覆盖",
       audit_at() == DEFAULT_AUDIT_AT or os.environ.get("TICK_SECTOR_AUDIT_AT"))
    ok("GEO_TERMS 与 VAGUE_TERMS 非空", len(GEO_TERMS) > 10 and len(VAGUE_TERMS) > 10)
    items = _load_items(24)
    ok(f"数据源可解析（{len(items)} 条）", len(items) > 0)
    st = audit_state()
    ok("调度状态可读（含 next 下次时间）", "next" in st and "done_today" in st)
    # 规则自检：地缘词条必须被 R1 抓到（用假数据喂 _hit_terms 逻辑）
    geo_probe = _hit_terms("伊朗外长与沙特外长通电话", list(GEO_TERMS))
    ok("R1 探针：伊朗=地缘实体词", "伊朗" in geo_probe)
    vague_probe = _hit_terms("某公司发布新一代产品，同比增长", list(VAGUE_TERMS))
    ok("R2 探针：发布/增长=泛化词", "发布" in vague_probe and "增长" in vague_probe)
    print(f"\n{'❌ %d 项失败' % fails if fails else '✅ 全部通过'}")
    return fails


if __name__ == "__main__":
    import sys
    if "--run" in sys.argv:
        hours = 24
        for i, a in enumerate(sys.argv):
            if a == "--hours" and i + 1 < len(sys.argv):
                hours = int(sys.argv[i + 1])
        r = run_audit(hours=hours, why="命令行")
        print(json.dumps(r.get("summary"), ensure_ascii=False, indent=2))
        for f in r.get("findings", []):
            print(f"  [{f['level']}] {f['rule']} {f['sector']}: {f['detail']}")
        print("报告:", r.get("report"))
        sys.exit(0)
    sys.exit(1 if selfcheck() else 0)
