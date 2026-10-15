"""#93 个股深度分析（LLM 综合解读，复用 sentiment_optimizer「context→LLM→护栏」范式）。

build_context(code) 只读聚合四类数据（全部走现成模块，不重造、不改状态）：
  · 实时快照   datasource.snapshot（价/涨跌幅/估值/市值/换手，多源降级入口已抹平单位）
  · 技术面     indicator.all_indicators（自动信号汇总 signals + 综合裁决 verdict + 多空计数）
  · 所属板块   sector_sentiment.map_sectors（从股名映射板块）
  · 近期快讯   sector_verify._load_news（与收盘对账同 24h 窗口，筛含股名/所属板块的条目）

护栏（与 sentiment_optimizer 同思路：AI 负责想，代码负责验，降级不用人管）：
  · 纯只读 + 人工点触发（低频），无"应用"步骤，绝不改任何词表/状态；
  · LLM 输出过 schema 校验（summary/strengths/risks/score 形态），
    某维度数据缺失时在 prompt 标注"暂无"，让 LLM 据此措辞、不编造；
  · LLM 未启用/未配置/网络错/结构异常 → 降级为"纯数据摘要"（data_only=True），
    不报错、不 500；
  · 不做涨跌预测（与 sector_news_lab 同边界）：score 是"信号一致性/数据完备度"的
    描述性打分，不是看涨看跌；文案明确"仅供参考，非投资建议"。
"""
from __future__ import annotations
import json
import re
import time
from typing import Any, Dict, List, Optional

_SYS = """你是A股个股分析助手。给你一只股票的真实数据（行情/技术面信号/所属板块/近期快讯），
你产出结构化解读。只输出一个 ```json 代码块，仅含：
{"summary":"≤140字综合概述","logic":"≤200字多空逻辑","strengths":["亮点1","亮点2"],
"risks":["风险点1","风险点2"],"watch":["关注信号1"],"score":0~100,"score_note":"≤30字打分依据"}

规则（违反会被代码拒绝、白费 Token）：
- summary 用 2~3 句话说清「现在这是什么状态、为什么」；≤140 字；
- logic 专门解释技术面：把多空计数与关键信号组织成一段话，讲清「信号之间一致还是矛盾、
  哪一方占上风、量价/超买类细节意味着什么」；信号数据不足就明说，≤200 字；
- strengths/risks 各 1~5 条，每条 ≤60 字，必须基于给定数据、尽量引用具体数值或信号名
  （如「CCI=199.9 已超买」而非笼统的「技术面好」），禁止编造数据里没有的信息；
- watch 列 1~3 条「后续盯什么」：条件式提示（如「若跌破 20 日线且缩量，短线转弱」），
  同样只能基于给定数据，≤50 字每条；
- 数据里标注"暂无"的维度不要强行解读，可略过或在 risks 里提"数据不全"；
- score 反映"多空信号一致性 + 数据完备度"（不是预测涨跌）：信号一致且数据全→偏高，
  信号矛盾或数据缺失→偏低，中性给 50 左右；
- 严禁给出"建议买入/卖出/目标价"等投资建议，只做客观数据解读；
- 只输出 json 块，不要额外文字。"""


def build_context(code: str, hours: int = 24) -> Dict[str, Any]:
    """只读聚合个股四维数据；任何单源失败都记 missing、不阻断其余维度。"""
    ctx: Dict[str, Any] = {"ok": False, "code": str(code or "").strip(), "name": "",
                           "quote": {}, "tech": {}, "sectors": [], "news": [],
                           "missing": [], "generated_at": int(time.time())}
    if not ctx["code"]:
        ctx["reason"] = "缺少股票代码"
        return ctx
    try:
        import datasource as ds
    except Exception as e:
        ctx["reason"] = f"数据源不可用：{e}"
        return ctx
    ncode = ds.normalize(ctx["code"])

    # 1) 实时快照
    try:
        q = (ds.snapshot([ncode]) or {}).get(ncode) or {}
        if q:
            ctx["name"] = q.get("name") or ""
            ctx["quote"] = {"name": ctx["name"], "price": q.get("price"),
                            "change_pct": q.get("change_pct"), "turnover": q.get("turnover"),
                            "pe": q.get("pe"), "pb": q.get("pb"),
                            "total_cap": q.get("total_cap"), "amount": q.get("amount")}
    except Exception:
        pass
    if not ctx["quote"]:
        ctx["missing"].append("实时快照")

    # 2) 技术面（signals + 综合裁决 + 多空计数，LLM 最需要的三样）
    try:
        import indicator as ind_mod
        ind = ind_mod.all_indicators(ds.get_kline(ncode, "1d", 120))
        if ind:
            ctx["tech"] = {
                "verdict": ind.get("verdict"), "verdict_dir": ind.get("verdict_dir"),
                "bull": ind.get("bull_count"), "bear": ind.get("bear_count"),
                "neutral": ind.get("neutral_count"),
                "signals": [{"name": s.get("name"), "dir": s.get("dir"),
                             "desc": s.get("desc", "")}
                            for s in (ind.get("signals") or [])][:8],
            }
    except Exception:
        pass
    if not ctx["tech"]:
        ctx["missing"].append("技术面(K线不足或计算失败)")

    # 3) 所属板块 + 名称兜底：ticker 精确反查优先（快照 name 常空——eltdx 单只查询
    #    不带 name/估值）；name 缺失时从板块成分股反查补上，供新闻筛选与展示。
    try:
        import stock_sector_map as ssm
        ctx["sectors"] = ssm.sectors_for_stocks([ncode])[:3]
        if not ctx["name"]:
            bare = re.sub(r"^(sh|sz|bj)", "", ncode)
            for sec in ctx["sectors"]:
                for st in ssm.sector_stocks(sec):
                    if st.get("code") in (bare, ncode) and st.get("name"):
                        ctx["name"] = st["name"]
                        break
                if ctx["name"]:
                    break
        if ctx["name"] and ctx["quote"] and not ctx["quote"].get("name"):
            ctx["quote"]["name"] = ctx["name"]
    except Exception:
        pass
    if not ctx["sectors"] and ctx["name"]:       # ticker 反查落空 → 股名映射兜底
        try:
            import sector_sentiment as ss
            ctx["sectors"] = ss.map_sectors(ctx["name"])[:3]
        except Exception:
            pass

    # 4) 近期快讯：该股名优先，所属板块补充（与收盘对账同窗口同原语）
    try:
        import sector_verify as sv
        items = sv._load_news(hours) or []
        name, secs = ctx["name"], set(ctx["sectors"])
        by_stock, by_sector = [], []
        for it in items:
            c = it.get("content") or ""
            tone = (it.get("sentiment") or {}).get("tone", "neutral")
            rec = {"text": c[:100], "tone": tone}
            if name and name in c:
                by_stock.append(dict(rec, by="stock"))
            elif secs and any(s in c for s in secs):
                by_sector.append(dict(rec, by="sector"))
        ctx["news"] = by_stock[:4] + by_sector[:3]
    except Exception:
        ctx["missing"].append("近期快讯")

    ctx["ok"] = bool(ctx["quote"] or ctx["tech"] or ctx["news"])
    if not ctx["ok"]:
        ctx["reason"] = "该股无可用数据（快照/技术面/快讯均缺失，可能代码有误或停牌）"
    return ctx


def _ctx_prompt(ctx: Dict[str, Any]) -> str:
    """把 build_context 的结构化数据渲染成 LLM user prompt；缺维度标"暂无"。"""
    L = ["【股票】" + (ctx.get("name") or ctx.get("code") or "")]
    q = ctx.get("quote") or {}
    if q:
        def f(k, unit=""):
            v = q.get(k)
            return f"{v}{unit}" if v not in (None, "") else "暂无"
        L.append(f"【行情】价格 {f('price')} 元，涨跌幅 {f('change_pct', '%')}，"
                 f"换手率 {f('turnover', '%')}，PE {f('pe')}，PB {f('pb')}，"
                 f"总市值 {f('total_cap')} 亿")
    else:
        L.append("【行情】暂无")
    t = ctx.get("tech") or {}
    if t:
        L.append(f"【技术面】综合裁决：{t.get('verdict')}（{t.get('verdict_dir')}），"
                 f"多空计数 多{t.get('bull')}/空{t.get('bear')}/中{t.get('neutral')}")
        sigs = t.get("signals") or []
        if sigs:
            L.append("  信号：" + "；".join(f"{s.get('name')}({s.get('dir')})" for s in sigs))
    else:
        L.append("【技术面】暂无")
    L.append("【所属板块】" + ("、".join(ctx.get("sectors") or []) or "暂无"))
    news = ctx.get("news") or []
    if news:
        L.append("【近期快讯】")
        for n in news:
            tag = {"pos": "偏多", "neg": "偏空"}.get(n.get("tone"), "中性")
            L.append(f"  - [{tag}] {n.get('text')}")
    else:
        L.append("【近期快讯】暂无")
    return "\n".join(L)


def _parse_json(text: str) -> Optional[dict]:
    """提取 ```json 块并解析；无机读块/坏 JSON/非 dict 都返回 None。"""
    m = re.search(r"```json\s*(.*?)```", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _validate(obj: Dict[str, Any]) -> Dict[str, Any]:
    """schema 校验：summary/logic/strengths/risks/watch/score/score_note 形态，返回采用子集。"""
    out: Dict[str, Any] = {}
    s = obj.get("summary")
    if isinstance(s, str) and s.strip() and len(s) <= 160:
        out["summary"] = s.strip()

    lg = obj.get("logic")
    if isinstance(lg, str) and lg.strip() and len(lg) <= 240:
        out["logic"] = lg.strip()

    def _strlist(key, lo, hi, maxlen):
        v = obj.get(key)
        if isinstance(v, list):
            keep = [x.strip() for x in v if isinstance(x, str) and x.strip() and len(x) <= maxlen]
            if lo <= len(keep) <= hi:
                return keep
        return None

    st = _strlist("strengths", 1, 5, 60)
    if st:
        out["strengths"] = st
    rk = _strlist("risks", 1, 5, 60)
    if rk:
        out["risks"] = rk
    wt = _strlist("watch", 1, 3, 60)
    if wt:
        out["watch"] = wt
    try:
        sc = round(float(obj.get("score")))
        if 0 <= sc <= 100:
            out["score"] = int(sc)
    except (TypeError, ValueError):
        pass
    sn = obj.get("score_note")
    if isinstance(sn, str) and sn.strip() and len(sn) <= 60:
        out["score_note"] = sn.strip()
    # 至少要有一个实质字段才采用（否则回落纯数据摘要）
    if not (out.get("summary") or "score" in out or out.get("strengths")):
        return {}
    return out


def analyze(ctx: Dict[str, Any], override: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """调 LLM 产解读 JSON。LLM 异常上抛（由 analyze_stock 转成纯数据摘要降级）。"""
    import llm_client
    raw = llm_client.chat([{"role": "system", "content": _SYS},
                           {"role": "user", "content": _ctx_prompt(ctx)}],
                          max_tokens=700, temperature=0.3, override=override)
    return _parse_json(raw)


def analyze_stock(code: str, hours: int = 24,
                  override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """主入口：build_context → LLM 解读 → schema 校验 → 降级为纯数据摘要。

    返回 {"ok", "data_only", "ctx", "llm"}：
      data_only=True  = LLM 不可用/schema 不过，只有原始数据摘要（不报错）；
      data_only=False = 附上通过校验的 llm 解读。
    """
    ctx = build_context(code, hours=hours)
    if not ctx.get("ok"):
        return {"ok": False, "reason": ctx.get("reason", "无可用数据"),
                "data_only": True, "ctx": ctx, "llm": None}
    base = {"ok": True, "data_only": True, "ctx": ctx, "llm": None}
    try:
        import llm_client                          # 放 try 内：import 失败也降级
        obj = analyze(ctx, override=override)
    except Exception:
        return base                                # LLM 不可用 → 纯数据摘要
    if obj is None:
        return base
    v = _validate(obj)
    if not v:
        return base                                # schema 不过 → 纯数据摘要
    return {"ok": True, "data_only": False, "ctx": ctx, "llm": v}


def selfcheck() -> int:
    """离线自检：prompt 渲染 + schema 正/负例 + 增强/降级路径（假 chat，不调 LLM）。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    ctx = {"ok": True, "name": "贵州茅台", "code": "sh600519",
           "quote": {"name": "贵州茅台", "price": 1500.0, "change_pct": 1.2,
                     "turnover": 0.5, "pe": 30, "pb": 10, "total_cap": 18800},
           "tech": {"verdict": "偏多", "verdict_dir": "bull", "bull": 5, "bear": 2,
                    "neutral": 3, "signals": [{"name": "均线多头排列", "dir": "bull", "desc": "x"}]},
           "sectors": ["白酒", "消费"], "news": [{"text": "白酒板块走高", "tone": "pos", "by": "sector"}],
           "missing": []}
    p = _ctx_prompt(ctx)
    ok("_ctx_prompt：含股票名/行情/技术面/板块/快讯",
       "贵州茅台" in p and "1500" in p and "均线多头排列" in p and "白酒" in p and "白酒板块走高" in p)
    miss = {"name": "", "code": "sh000001", "quote": {}, "tech": {}, "sectors": [], "news": [], "missing": []}
    ok("_ctx_prompt：缺维度标注暂无(≥3)", _ctx_prompt(miss).count("暂无") >= 3)

    good = {"summary": "茅台技术面信号多空交织，短线偏中性。", "logic": "多空各4个信号："
             "DMI/TRIX/站上BBI偏多，但KDJ与CCI双双超买，短线有回踩风险，整体中性。",
             "strengths": ["均线多头", "资金流入"],
             "risks": ["估值不低", "短线超买"], "watch": ["若跌破20日线且缩量，短线转弱"],
             "score": 68, "score_note": "信号一致但估值高"}
    v = _validate(good)
    ok("_validate：合法解读提取全部字段",
       v.get("score") == 68 and len(v.get("strengths", [])) == 2 and v.get("summary")
       and v.get("logic") and v.get("watch") == ["若跌破20日线且缩量，短线转弱"])
    ok("_validate：summary 超长丢弃", "summary" not in _validate({"summary": "x" * 200, "score": 50}))
    ok("_validate：logic 超长丢弃", "logic" not in _validate({"summary": "ok", "logic": "x" * 300}))
    ok("_validate：strengths 条数超限丢弃", "strengths" not in _validate({"summary": "ok", "strengths": ["a"] * 6}))
    ok("_validate：watch 条数超限丢弃", "watch" not in _validate({"summary": "ok", "watch": ["a"] * 4}))
    ok("_validate：score 超范围丢弃", "score" not in _validate({"score": 150}))
    ok("_validate：空 dict → 无采用", _validate({}) == {})
    ok("_parse_json：坏 JSON → None", _parse_json("```json\n{bad}\n```") is None)

    import llm_client
    _orig_chat, _orig_bc = llm_client.chat, globals()["build_context"]
    try:
        globals()["build_context"] = lambda code, hours=24: ctx   # 注入已知 ctx，免真实数据/网络
        def _boom(*a, **k):
            raise RuntimeError("boom")
        llm_client.chat = _boom
        r = analyze_stock("sh600519")
        ok("analyze_stock：LLM 异常 → 纯数据摘要(data_only)",
           r["ok"] and r["data_only"] and r.get("llm") is None)
        llm_client.chat = lambda *a, **k: "解读\n```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
        r2 = analyze_stock("sh600519")
        ok("analyze_stock：LLM 正常 → 附加解读", r2["ok"] and not r2["data_only"] and r2["llm"]["score"] == 68)
        llm_client.chat = lambda *a, **k: '```json\n{"foo":"bar"}\n```'
        r3 = analyze_stock("sh600519")
        ok("analyze_stock：schema 不过 → 纯数据摘要", r3["ok"] and r3["data_only"])
    finally:
        llm_client.chat, globals()["build_context"] = _orig_chat, _orig_bc
    ok("build_context：空代码 → ok False", build_context("").get("ok") is False)
    print(f"stock_analyzer selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
