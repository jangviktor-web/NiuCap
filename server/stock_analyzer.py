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
import os
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
                           "quote": {}, "tech": {}, "sectors": [], "news": [], "flow": {},
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

    # 4) 资金面 + 筹码（#B 扩展）：主力/超大单净额 + 5/10/20 日主力净额 + 获利盘。
    #    westock CLI 不可用时整维记 missing，不阻断其余维度。
    try:
        import westock as wst
        ff = (wst.fund_flow([ncode]) or {}).get(ncode) or {}
        chip = wst.chip(ncode) or {}
        flow: Dict[str, Any] = {}
        if ff:
            flow.update({
                "main_net": ff.get("main_net"), "main_circ_rate": ff.get("main_circ_rate"),
                "jumbo_net": ff.get("jumbo_net"), "block_net": ff.get("block_net"),
                "main_rank": ff.get("main_rank"),
                "main_net_5d": ff.get("main_net_5d"), "main_net_10d": ff.get("main_net_10d"),
                "main_net_20d": ff.get("main_net_20d"),
            })
        if chip:
            flow.update({
                "avg_cost": chip.get("avg_cost"),
                "concentration70": chip.get("concentration70"),
                "profit_rate": chip.get("profit_rate"),
            })
        if flow:
            ctx["flow"] = flow
    except Exception:
        pass
    if not ctx.get("flow"):
        ctx["missing"].append("资金/筹码")

    # 5) 近期快讯：该股名优先，所属板块补充（与收盘对账同窗口同原语）
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
    fl = ctx.get("flow") or {}
    if fl:
        def w(k):
            v = fl.get(k)
            return "暂无" if v in (None, "") else v
        L.append(f"【资金面】主力净额 {w('main_net')} 万元（占流通 {w('main_circ_rate')}%），"
                 f"超大单 {w('jumbo_net')} 万，大单 {w('block_net')} 万；"
                 f"5/10/20 日主力净额 {w('main_net_5d')}/{w('main_net_10d')}/{w('main_net_20d')} 万元")
        if fl.get("avg_cost") is not None:
            L.append(f"【筹码】平均成本 {w('avg_cost')} 元，70% 集中度 {w('concentration70')}，"
                     f"获利盘比例 {w('profit_rate')}%")
    else:
        L.append("【资金面】暂无")
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
    """提取 JSON：优先 ```json 代码块，回退裸 JSON 对象；坏 JSON/非 dict 返回 None。

    兼容裸 JSON 是必要的——LLM 常直接输出 {"...": ...} 而不包代码块，
    只认代码块会把这类合理解读误判为失败、白费 Token（plain 视角实测踩到）。
    """
    t = text or ""
    m = re.search(r"```json\s*(.*?)```", t, re.S)
    cand = m.group(1) if m else t
    try:
        obj = json.loads(cand.strip())
    except Exception:
        m2 = re.search(r"\{.*\}", cand, re.S)      # 代码块/正文里掐出第一个对象
        if not m2:
            return None
        try:
            obj = json.loads(m2.group(0))
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


# ---------------------------------------------------------------------------
# C（小白视角）：把同一份 ctx 翻成大白话，输出 {explain, watch}。
# 与主解读共用 llm_client + schema 护栏；独立 prompt，避免与 #93 长文互相挤占。
# ---------------------------------------------------------------------------
_PLAIN_SYS = """你是 A 股小白的行情翻译官。给你一只股票的行情/技术面/资金筹码/板块/快讯数据，
请用大白话讲清楚「现在是什么状况」和「要注意什么」。仅输出 JSON：
{"explain":"≤120字大白话","watch":["注意点1"]}

规则（违反会被代码拒绝、白费 Token）：
- explain 用 2~3 句大白话：先说价格和资金在做什么（有主力流入就说、没有别编），
  再说技术面偏多还是偏空；把术语（换手率/主力净额/均线/获利盘）换成生活化说法，
  引用具体数字；≤120 字；
- watch 列 1~2 条「现在要留意的事」：基于给定数据的风险或条件式提示
  （如「主力在流出，反弹可能是诱多」「获利盘 90%，抛压重」），≤40 字每条；
- 只能基于给定数据，禁止编造；不是投资建议。
用 ```json 代码块包裹输出，例如：
```json
{"explain":"…","watch":["…"]}
```"""


def _plain_prompt(ctx: Dict[str, Any]) -> str:
    """复用 _ctx_prompt 的维度渲染，套上「讲给小白听」的任务说明。"""
    return "请把以下数据翻译成小白能懂的话：\n" + _ctx_prompt(ctx)


def _validate_plain(obj: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ex = obj.get("explain")
    if isinstance(ex, str) and ex.strip() and len(ex) <= 200:
        out["explain"] = ex.strip()
    w = obj.get("watch")
    if isinstance(w, list):
        keep = [x.strip() for x in w if isinstance(x, str) and x.strip() and len(x) <= 60]
        if 1 <= len(keep) <= 3:
            out["watch"] = keep
    return out if out.get("explain") else {}


def plain(ctx: Dict[str, Any], override: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """调 LLM 产大白话 JSON。异常上抛，由 analyze_stock 兜住。"""
    import llm_client
    raw = llm_client.chat([{"role": "system", "content": _PLAIN_SYS},
                           {"role": "user", "content": _plain_prompt(ctx)}],
                          max_tokens=400, temperature=0.4, override=override)
    obj = _parse_json(raw)
    return _validate_plain(obj) if obj else None


# ---------------------------------------------------------------------------
# A（常驻缓存）：同 code+hours 在 TTL 内复用上次结果，命中不消耗配额。
# 模块级 dict 即可——单进程 uvicorn，无需 Redis；重启即失效，可接受。
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = float(os.environ.get("TICK_ANALYZE_TTL", "1800"))   # 默认 30 分钟


def _cache_key(code: str, hours: int) -> str:
    return f"{str(code).strip().lower()}|{int(hours)}"


def cached(code: str, hours: int = 24) -> Optional[Dict[str, Any]]:
    """命中且未过期返回缓存结果（附 cache_age 秒），否则 None。不触 LLM、不查配额。"""
    hit = _CACHE.get(_cache_key(code, hours))
    if hit and time.time() - hit["at"] < _CACHE_TTL:
        res = dict(hit["res"])
        res.pop("ai_quota", None)                  # 配额随每次扣减变化，不缓存
        res["cache_age"] = int(time.time() - hit["at"])
        res["cached"] = True
        return res
    return None


def analyze_stock(code: str, hours: int = 24, refresh: bool = False,
                  with_plain: bool = True,
                  override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """主入口：build_context → LLM 解读 → schema 校验 → 降级为纯数据摘要。

    返回 {"ok", "data_only", "ctx", "llm", "plain"}：
      data_only=True  = LLM 不可用/schema 不过，只有原始数据摘要（不报错）；
      data_only=False = 附上通过校验的 llm 解读；
      plain           = C 小白视角 {explain, watch}，LLM 不可用时为 None。
    refresh=False 且 TTL 内命中缓存 → 直接返回，不触 LLM。
    """
    if not refresh:
        hit = cached(code, hours)
        if hit is not None:
            return hit
    ctx = build_context(code, hours=hours)
    if not ctx.get("ok"):
        return {"ok": False, "reason": ctx.get("reason", "无可用数据"),
                "data_only": True, "ctx": ctx, "llm": None, "plain": None}
    base = {"ok": True, "data_only": True, "ctx": ctx, "llm": None, "plain": None}
    try:
        import llm_client                          # 放 try 内：import 失败也降级
        obj = analyze(ctx, override=override)
    except Exception:
        return base                                # LLM 不可用 → 纯数据摘要（不缓存，下次可重试）
    v = _validate(obj) if obj else {}
    pl = None
    if with_plain:
        try:
            pl = plain(ctx, override=override)
        except Exception:
            pl = None
    if not v:
        # 主解读失败：不写长缓存——降级多是 LLM 临时抖动，锁 30 分钟会让用户
        # 反复进页都只看到「纯数据摘要」。让下次进页重试更合理。
        res = dict(base)
        res["plain"] = pl                          # 主解读失败但小白视角成功也用上
        return res
    res = {"ok": True, "data_only": False, "ctx": ctx, "llm": v, "plain": pl}
    _CACHE[_cache_key(code, hours)] = {"at": time.time(), "res": res}
    return res


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
        r = analyze_stock("sh600519", refresh=True)
        ok("analyze_stock：LLM 异常 → 纯数据摘要(data_only)",
           r["ok"] and r["data_only"] and r.get("llm") is None)
        llm_client.chat = lambda *a, **k: "解读\n```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
        r2 = analyze_stock("sh600519", refresh=True)
        ok("analyze_stock：LLM 正常 → 附加解读", r2["ok"] and not r2["data_only"] and r2["llm"]["score"] == 68)
        # plain 用独立 mock：主解读与小白视角各要一份 schema，按 prompt 里的关键词分流
        _plain_mock = json.dumps({"explain": "主力在流入，技术面偏多，但股价已连涨需防回调", "watch": ["获利盘偏高"]}, ensure_ascii=False)
        llm_client.chat = lambda msgs, **k: ("```json\n" + _plain_mock + "\n```"
            if any("小白" in (m.get("content") or "") for m in msgs)
            else "```json\n" + json.dumps(good, ensure_ascii=False) + "\n```")
        r2p = analyze_stock("sh600519", refresh=True)
        ok("analyze_stock：plain 小白视角解析", bool(r2p.get("plain") and r2p["plain"].get("explain")))
        llm_client.chat = lambda *a, **k: '```json\n{"foo":"bar"}\n```'
        r3 = analyze_stock("sh600519", refresh=True)
        ok("analyze_stock：schema 不过 → 纯数据摘要", r3["ok"] and r3["data_only"])
        # A 缓存：第一次 refresh=True 写缓存，第二次默认应命中（不调 LLM）
        calls = {"n": 0}
        def _count(*a, **k):
            calls["n"] += 1
            return "```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
        llm_client.chat = _count
        analyze_stock("sh600519", refresh=True)
        n1 = calls["n"]
        r_c = analyze_stock("sh600519")            # 默认读缓存
        ok("A 缓存：TTL 内命中不调 LLM", calls["n"] == n1 and r_c.get("cached") is True)
        ok("cached()：TTL 内返回且带 cache_age", (cached("sh600519") or {}).get("cache_age") is not None)
        _cached_res = _CACHE.get(_cache_key("sh600519", 24))
        if _cached_res:
            _cached_res["at"] = 0.0                 # 手动过期
        ok("cached()：过期返回 None", cached("sh600519") is None)
    finally:
        llm_client.chat, globals()["build_context"] = _orig_chat, _orig_bc
        _CACHE.clear()                              # 清掉自检污染
    ok("build_context：空代码 → ok False", build_context("").get("ok") is False)
    print(f"stock_analyzer selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
