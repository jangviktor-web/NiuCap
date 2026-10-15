"""LLM 增强的策略意图解析（复用 sentiment_optimizer 的「LLM 产 JSON + 代码护栏」范式）。

切分边界（ponytail，风险最小化）：
  · LLM 只增强「解释性字段」：strategy / indicators / strategy_type /
    risk_profile / params(stop_loss,take_profit) / entry / exit / confidence；
  · conditions（Tier5 NL→DSL）**保持规则版** intent.parse_screener_conditions——
    DSL 精确映射规则版已做好且有 screen_dsl.compile 护栏，LLM 不碰可执行条件；
  · 输出逐字段过白名单校验，任何一步不过 → 整体回落规则版 parse_intent（对用户透明）。

护栏（与 sentiment_optimizer 同思路：AI 负责想，代码负责验，降级不用人管）：
  strategy      ∈ intent._STRATEGY_NAMES 的键 ∪ {None}
  indicators    ⊆ intent._INDICATORS 的 name 集合
  strategy_type ∈ intent._TYPES 的 name 集合
  risk_profile  ∈ intent._RISK 的 name 集合
  stop_loss / take_profit ∈ (0, 0.5]
  entry / exit  非空字符串且 ≤120 字（纯展示，无执行风险）
  LLM 未启用/未配置/网络错/JSON 结构异常 → 回落规则版（不报错、不 500）

name 字段（strategy_name 等）复用 intent._*_NAMES 常量，与规则版单一真相源。
被成功增强时附加 _llm=True，供前端显示「AI 增强」角标；conditions 永远来自
规则版，因此前端一键回测/选股行为与现状完全一致，AI 只让"理解"更准。
"""
from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional

import intent as itt

_SYS = """你是A股策略意图解析员。用户用一句自然语言描述交易策略，你把它解析成
结构化意图。只输出一个 ```json 代码块，仅含以下键（能确定的才填，不确定就省略）：
{"strategy":"内置策略键或null","indicators":["指标名"],"strategy_type":"类型键",
"risk_profile":"风险键","params":{"stop_loss":小数,"take_profit":小数},
"entry":"入场规则一句话","exit":"出场规则一句话","confidence":0.0~1.0}

合法取值（必须取这些值或省略，别的值会被代码拒绝、白费 Token）：
- strategy: ma_cross/macd/rsi/boll/kdj/ensemble/buy_hold 之一，或 null
- indicators: MA/MACD/RSI/KDJ/BOLL/ATR/CCI/WR/BIAS/DMI/OBV/MFI/VOLUME/通道 的子集
- strategy_type: mean_reversion/breakout/trend_following/reversal/momentum/grid 之一
- risk_profile: aggressive/balanced/conservative 之一
- stop_loss/take_profit: 0~0.5 之间的小数（亏5%止损=0.05，赚15%止盈=0.15）

要点：
- 「赚X%就跑/止盈」→ take_profit=X/100；「亏X%/跌X%止损」→ stop_loss=X/100；
- 「激进/搏一把/重仓」→ aggressive；「保守/稳健/轻仓」→ conservative；
- entry/exit 用不超过 60 字中文概括用户的买卖条件；
- confidence 反映把握程度（0.6~0.95），没把握就填低；
- 只输出 json 块，不要额外解释文字。"""


def _whitelists() -> Dict[str, set]:
    """从 intent 复用白名单（单一真相源，不重复定义）。"""
    return {
        "strategy": set(itt._STRATEGY_NAMES) | {None},
        "indicators": {name for name, _ in itt._INDICATORS},
        "type": {t for t, _ in itt._TYPES},
        "risk": {r for r, _ in itt._RISK},
    }


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
    """逐字段过白名单，返回被增强的字段子集（空 dict = 无有效增强，触发回落）。"""
    wl = _whitelists()
    out: Dict[str, Any] = {}
    # strategy 含 None（LLM 明确"无内置策略"）；但须键存在才算——
    # 键缺失时 get() 也返回 None，不区分会误把空 dict 产出 strategy=None
    if "strategy" in obj and obj.get("strategy") in wl["strategy"]:
        out["strategy"] = obj["strategy"]
    inds = obj.get("indicators")
    if isinstance(inds, list):
        keep = [i for i in inds if isinstance(i, str) and i in wl["indicators"]]
        if keep:
            out["indicators"] = keep              # 只留合法项，不全丢
    if obj.get("strategy_type") in wl["type"]:
        out["strategy_type"] = obj["strategy_type"]
    if obj.get("risk_profile") in wl["risk"]:
        out["risk_profile"] = obj["risk_profile"]
    p = obj.get("params")
    if isinstance(p, dict):
        pp: Dict[str, float] = {}
        for k in ("stop_loss", "take_profit"):
            try:
                v = round(float(p.get(k)), 4)
            except (TypeError, ValueError):
                continue
            if 0 < v <= 0.5:                      # 超范围=LLM 幻觉，丢弃
                pp[k] = v
        if pp:
            out["params"] = pp
    for k in ("entry", "exit"):                   # 纯展示，无执行风险，只限长度
        v = obj.get(k)
        if isinstance(v, str) and v.strip() and len(v) <= 120:
            out[k] = v.strip()
    try:
        c = round(float(obj.get("confidence")), 2)
        if 0.5 <= c <= 1.0:
            out["confidence"] = c
    except (TypeError, ValueError):
        pass
    return out


def parse_enhanced(text: str, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """LLM 增强版意图解析：规则版兜底 + LLM 增强解释性字段。

    任何失败（LLM 未启用/网络错/JSON 坏/白名单全不过）都静默回落规则版
    parse_intent，返回结构与规则版一致（conditions 永远来自规则版），
    仅在被成功增强时附加 _llm=True 供前端显示「AI 增强」角标。
    """
    base = itt.parse_intent(text)                 # 规则版兜底（conditions 唯一来源）
    if base.get("error"):
        return base
    try:
        import llm_client                          # 放 try 内：import 失败也回落
        raw = llm_client.chat(
            [{"role": "system", "content": _SYS},
             {"role": "user", "content": str(text)}],
            max_tokens=600, temperature=0.2, override=override)
    except Exception:
        return base                                # LLM 不可用 → 规则版（用户无感）
    obj = _parse_json(raw)
    if obj is None:
        return base
    enh = _validate(obj)
    if not enh:
        return base                                # 白名单全不过 → 规则版
    out = dict(base)
    for k in ("strategy", "indicators", "strategy_type", "risk_profile",
              "entry", "exit", "confidence"):
        if k in enh:
            out[k] = enh[k]
    if "params" in enh:                            # 合并：LLM 只给部分键时保留规则版另一键
        merged = dict(base.get("params") or {})
        merged.update(enh["params"])
        out["params"] = merged
    # name 字段跟随被覆盖的键重算（复用 intent 常量，与规则版一致）
    if "strategy" in enh:
        out["strategy_name"] = itt._STRATEGY_NAMES.get(enh["strategy"], "未匹配到内置策略")
    if "strategy_type" in enh:
        out["strategy_type_name"] = itt._TYPE_NAMES.get(enh["strategy_type"], enh["strategy_type"])
    if "risk_profile" in enh:
        out["risk_profile_name"] = itt._RISK_NAMES.get(enh["risk_profile"], enh["risk_profile"])
    out["_llm"] = True
    return out


def selfcheck() -> int:
    """离线自检：护栏正/负例 + 增强/回落路径（假 chat 注入，不调真实 LLM）。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    import llm_client
    _orig = llm_client.chat
    try:
        # ── _parse_json 容错
        ok("_parse_json：坏 JSON → None", _parse_json("```json\n{bad}\n```") is None)
        ok("_parse_json：无 json 块 → None", _parse_json("纯文本没有块") is None)
        ok("_parse_json：非 dict → None", _parse_json("```json\n[1,2]\n```") is None)

        # ── _validate 正例
        good = _parse_json(
            '```json\n{"strategy":"ma_cross","indicators":["MA","VOLUME"],'
            '"strategy_type":"trend_following","risk_profile":"aggressive",'
            '"params":{"stop_loss":0.05,"take_profit":0.15},'
            '"entry":"金叉放量买入","exit":"跌破10日线止损","confidence":0.85}\n```')
        v = _validate(good)
        ok("_validate：合法输出提取全部字段",
           v.get("strategy") == "ma_cross" and v.get("risk_profile") == "aggressive"
           and v.get("params", {}).get("stop_loss") == 0.05
           and v.get("indicators") == ["MA", "VOLUME"]
           and v.get("confidence") == 0.85)

        # ── _validate 负例（每种真实会出现的 LLM 毛病）
        ok("_validate：strategy 白名单外丢弃", "strategy" not in _validate({"strategy": "hacked"}))
        ok("_validate：strategy=null 保留(明确无策略)", _validate({"strategy": None}).get("strategy") is None)
        ok("_validate：indicators 过滤非法项",
           _validate({"indicators": ["MA", "FAKE", "RSI"]}).get("indicators") == ["MA", "RSI"])
        ok("_validate：stop_loss 超范围(>0.5)丢弃", "params" not in _validate({"params": {"stop_loss": 0.9}}))
        ok("_validate：stop_loss=0 丢弃", "params" not in _validate({"params": {"stop_loss": 0}}))
        ok("_validate：type/risk 白名单外丢弃",
           "strategy_type" not in _validate({"strategy_type": "xx"})
           and "risk_profile" not in _validate({"risk_profile": "yy"}))
        ok("_validate：confidence<0.5 丢弃", "confidence" not in _validate({"confidence": 0.3}))
        ok("_validate：entry 超长丢弃", "entry" not in _validate({"entry": "x" * 200}))
        ok("_validate：空 dict → 无增强", _validate({}) == {})

        # ── parse_enhanced：增强路径（假 chat 返回合法 JSON）
        llm_client.chat = lambda msgs, **kw: (
            '诊断\n```json\n{"strategy":"rsi","risk_profile":"conservative",'
            '"params":{"take_profit":0.08},"entry":"RSI超卖低吸","exit":"止盈8%",'
            '"confidence":0.8}\n```')
        r = parse_enhanced("RSI超卖抄底，保守，赚8%止盈")
        ok("parse_enhanced：LLM 覆盖 strategy/risk + 打 _llm 标",
           r.get("strategy") == "rsi" and r.get("risk_profile") == "conservative" and r.get("_llm") is True)
        ok("parse_enhanced：name 跟随重算(单一真相源)",
           r.get("risk_profile_name") == "保守" and r.get("strategy_name") == "RSI超买超卖")
        ok("parse_enhanced：conditions 仍来自规则版",
           isinstance(r.get("conditions"), list))
        ok("parse_enhanced：params 合并保留规则版另一键(stop_loss)",
           r["params"].get("take_profit") == 0.08 and "stop_loss" in r["params"])

        # ── parse_enhanced：回落路径
        def _boom(msgs, **kw):
            raise RuntimeError("LLM 挂了")
        llm_client.chat = _boom
        r2 = parse_enhanced("均线金叉放量买入，跌破10日线止损")
        ok("parse_enhanced：LLM 异常 → 回落规则版(无 _llm)",
           "_llm" not in r2 and r2.get("strategy") == "ma_cross")
        llm_client.chat = lambda msgs, **kw: '```json\n{"strategy":"nonsense","indicators":["NOPE"]}\n```'
        r3 = parse_enhanced("均线金叉放量买入")
        ok("parse_enhanced：白名单全不过 → 回落规则版", "_llm" not in r3)
        llm_client.chat = lambda msgs, **kw: "没有 json 块的纯文本"
        r4 = parse_enhanced("均线金叉放量买入")
        ok("parse_enhanced：无 json 块 → 回落规则版", "_llm" not in r4)
        ok("parse_enhanced：空输入 → 规则版 error",
           parse_enhanced("").get("error") is not None)
    finally:
        llm_client.chat = _orig
    print(f"intent_llm selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
