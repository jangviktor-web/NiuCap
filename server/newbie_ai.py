"""小白选股 AI 讲解（#224）：把 _pack 产出的机器理由翻译成小白能懂的大白话。

与 stock_analyzer（#93/#222）同一范式：LLM 只增强解释性字段，输出过 schema
护栏；LLM 不可用/输出不过 → 返回 None，由调用方降级为纯理由展示（不报错）。
输入数据全部来自前端已持有的 newbie_pick 结果项（item）+ 方案元信息（preset），
后端不重算、不查库——零额外数据依赖。
"""
from __future__ import annotations
import json
import re
from typing import Any, Dict, List, Optional

_SYS = """你是A股小白选股助手。给你一只股票的小白选股结果（方案信息 + 机器理由 + 行情数据），
请把「为什么选它」翻译成大白话，并提醒买入前要注意什么。仅输出 JSON：
{"explain":"≤150字大白话讲解","watch":["注意点1"],"style":"≤20字风格标签"}

规则（违反会被代码拒绝、白费 Token）：
- explain 用 2~3 句大白话：先说这套方案在帮小白找什么样的票，再说这只为什么符合；
  把机器理由里的术语（换手率/量比/均线/量比）换成生活化说法，引用具体数字；
  ≤150 字；
- watch 列 1~3 条「买之前要清楚的事」：基于给定数据的风险或条件式提示
  （如「方案偏短线，跌 5% 要按计划止损」「连涨多天，别追高」），≤50 字每条；
- style 一个 ≤20 字的风格标签（如「稳重型」「激进型」「低位待涨型」）；
- 只能基于给定数据，禁止编造数据里没有的信息；
- 这是选股结果解读，不是投资建议。"""


def _fmt(v: Any, unit: str = "") -> str:
    if v is None or v == "":
        return "暂无"
    return f"{v}{unit}"


def _prompt(item: Dict[str, Any], preset: Dict[str, Any]) -> str:
    def f(label: str, v: Any, unit: str = ""):
        return f"{label}: {_fmt(v, unit)}"

    lines = [
        f"方案: {preset.get('name') or '小白选股'}",
        f"方案定位: {preset.get('tagline') or '暂无'}",
        f"持股周期: {preset.get('hold') or '暂无'} · 风险等级: {preset.get('risk') or '暂无'}",
        "",
        f"股票: {item.get('name') or '暂无'}（{item.get('code') or '暂无'}）",
        f"现价: {_fmt(item.get('price'), ' 元')} · 今日涨跌: {_fmt(item.get('change_pct'), '%')}",
        f"友好度评分: {_fmt(item.get('score'))} / 100",
        f"市盈率PE: {_fmt(item.get('pe'))} · 市净率PB: {_fmt(item.get('pb'))}",
        f"总市值: {_fmt(item.get('total_cap'), ' 亿')} · 换手率: {_fmt(item.get('turnover'), '%')}",
        f"成交额: {_fmt(item.get('amount'), ' 万元')}",
        "机器理由:",
    ]
    reasons = item.get("reasons") or []
    if reasons:
        lines.extend(f"  - {r}" for r in reasons)
    else:
        lines.append("  - 暂无")
    tags = item.get("tags") or []
    lines.append("标签: " + ("、".join(tags) if tags else "暂无"))
    if item.get("drop20") is not None:
        lines.append(f"20日跌幅: {item['drop20']}%")
    if item.get("drawdown") is not None:
        lines.append(f"区间最大回撤: {item['drawdown']}%")
    return "\n".join(lines)


def _parse_json(text: str) -> Optional[dict]:
    """从 LLM 输出里抠 JSON（容忍 ```json 包裹与前后废话）。坏 JSON → None。"""
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _validate(obj: Dict[str, Any]) -> Dict[str, Any]:
    """schema 护栏：逐字段校验，任一硬伤字段整体不用（宁可降级也不放坏数据）。"""
    def _s(key: str, maxlen: int) -> Optional[str]:
        v = obj.get(key)
        if not isinstance(v, str) or not v.strip():
            return None
        v = v.strip()
        return v if len(v) <= maxlen else None

    def _strlist(key: str, lo: int, hi: int, maxlen: int) -> Optional[List[str]]:
        v = obj.get(key)
        if not isinstance(v, list) or not (lo <= len(v) <= hi):
            return None
        out = []
        for x in v:
            if not isinstance(x, str) or not x.strip() or len(x) > maxlen:
                return None
            out.append(x.strip())
        return out

    out: Dict[str, Any] = {}
    e = _s("explain", 160)
    if not e:
        return {}
    out["explain"] = e
    w = _strlist("watch", 1, 3, 60)
    if not w:
        return {}
    out["watch"] = w
    st = _s("style", 24)
    if not st:
        return {}
    out["style"] = st
    return out


def explain(item: Dict[str, Any], preset: Optional[Dict[str, Any]] = None,
            override: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """调 LLM 产讲解 JSON。异常上抛（由端点转降级）；schema 不过返回 None。"""
    if not item or not item.get("code"):
        return None
    import llm_client
    raw = llm_client.chat([{"role": "system", "content": _SYS},
                           {"role": "user", "content": _prompt(item, preset or {})}],
                          max_tokens=500, temperature=0.4, override=override)
    obj = _parse_json(raw)
    if not obj:
        return None
    return _validate(obj) or None      # schema 不过（{}）也要走降级，不能当成功


def selfcheck() -> int:
    """离线自检：prompt 渲染 + schema 正/负例 + 降级路径（假 chat，不调 LLM）。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    preset = {"name": "稳健优选", "tagline": "找波动小、业绩稳的票",
              "hold": "1~3 个月", "risk": "中低"}
    item = {"code": "sh600519", "name": "贵州茅台", "price": 1500.0,
            "change_pct": 1.2, "score": 88, "reasons": ["换手率适中", "PE 低于行业均值"],
            "tags": ["大盘蓝筹"], "pe": 30, "pb": 10, "total_cap": 18800,
            "turnover": 0.5, "amount": 450000}
    p = _prompt(item, preset)
    ok("_prompt：含方案定位/股票/理由/行情",
       "稳健优选" in p and "贵州茅台" in p and "换手率适中" in p and "1500" in p and "18800" in p)
    miss = _prompt({}, {})
    ok("_prompt：空输入全字段暂无不抛异常", miss.count("暂无") >= 5)

    good = {"explain": "这套方案专挑波动小、业绩稳的公司，茅台正符合：换手不瘟不火，估值也比同行便宜。",
            "watch": ["方案偏稳健，别指望短期暴涨", "跌 5% 要按计划止损"],
            "style": "稳重型"}
    v = _validate(good)
    ok("_validate：合法讲解提取全部字段",
       v.get("style") == "稳重型" and len(v.get("watch", [])) == 2 and "茅台" in v.get("explain", ""))
    ok("_validate：explain 超长丢弃", "explain" not in _validate({"explain": "x" * 200, "watch": ["a"], "style": "s"}))
    ok("_validate：watch 条数超限丢弃", "watch" not in _validate({"explain": "ok", "watch": ["a"] * 4, "style": "s"}))
    ok("_validate：watch 单条超长丢弃", "watch" not in _validate({"explain": "ok", "watch": ["x" * 80], "style": "s"}))
    ok("_validate：style 超长丢弃", "style" not in _validate({"explain": "ok", "watch": ["a"], "style": "x" * 40}))
    ok("_validate：缺 watch → 无采用", _validate({"explain": "ok", "style": "s"}) == {})
    ok("_validate：空 dict → 无采用", _validate({}) == {})
    ok("_parse_json：坏 JSON → None", _parse_json("```json\n{bad}\n```") is None)
    ok("_parse_json：非 dict JSON → None", _parse_json('[1,2]') is None)

    import llm_client
    _orig = llm_client.chat
    try:
        def _boom(*a, **k):
            raise RuntimeError("boom")
        llm_client.chat = _boom
        ok("explain：LLM 异常上抛（端点转降级）", _raises(lambda: explain(item, preset)))
        llm_client.chat = lambda *a, **k: "讲解\n```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
        r = explain(item, preset)
        ok("explain：LLM 正常 → 通过校验", r and r["style"] == "稳重型")
        llm_client.chat = lambda *a, **k: '```json\n{"foo":"bar"}\n```'
        ok("explain：schema 不过 → None", explain(item, preset) is None)
        llm_client.chat = lambda *a, **k: "ok"
        ok("explain：空 item → None", explain({}, preset) is None)
    finally:
        llm_client.chat = _orig
    print(f"newbie_ai selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
