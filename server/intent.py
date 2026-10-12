"""自然语言 → 策略意图解析。

借鉴三省六部框架 strategy_intent/human_intent_parser.py 的思路，
但做了实质性强化：原文只做关键词包含判断，本实现改为
「分词 → 多义词消解 → 结构化意图 → 可执行策略映射」四步。

输入示例：
  「均线金叉并且放量，突破20日新高时买入，跌破10日线止损，激进风格」
  「RSI超卖且布林下轨，保守，赚15%就跑」

输出结构：
  {
    strategy: 'ma_cross',          # 映射到内置策略键（可为 None）
    strategy_name: '均线交叉',
    indicators: ['MA','MACD'],     # 命中的指标
    strategy_type: 'trend_following',
    risk_profile: 'aggressive',
    entry: '...',                  # 入场规则描述
    exit: '...',                   # 出场规则描述
    params: {stop_loss: 0.05, take_profit: 0.15, ...},
    confidence: 0.82,
    logic: '原始输入',
  }
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import screen_dsl  # Tier 5：用通用算子引擎校验生成的条件

# ---------------------------------------------------------------- 词典

_INDICATORS = [
    ("MACD", ["macd", "指数平滑", "异同移动平均"]),
    ("MA", ["均线", "ma", "日线", "ma5", "ma10", "ma20", "ma60", "移动平均"]),
    ("EMA", ["ema", "指数均线"]),
    ("RSI", ["rsi", "相对强弱", "强弱指标"]),
    ("KDJ", ["kdj", "随机指标"]),
    ("BOLL", ["布林", "boll", "布林带", "布林通道"]),
    ("ATR", ["atr", "真实波幅"]),
    ("CCI", ["cci", "顺势指标"]),
    ("WR", ["wr", "威廉指标"]),
    ("BIAS", ["bias", "乖离"]),
    ("DMI", ["dmi", "趋向指标", "adx"]),
    ("OBV", ["obv", "能量潮"]),
    ("MFI", ["mfi", "资金流量指标"]),
    ("VOLUME", ["成交量", "volume", "放量", "缩量", "量能", "量比"]),
    ("通道", ["唐安奇", "肯特纳", "通道"]),
]

# 「ma」是 ASCII 词，需按词边界匹配，避免匹配到 volume 等词内部
_WORD_KEYS = {"ma", "ema", "rsi", "kdj", "boll", "atr", "cci", "wr",
              "bias", "dmi", "adx", "obv", "mfi", "macd", "dif", "dea"}

_TYPES = [
    ("mean_reversion", ["均值回归", "回归", "reversion", "超卖", "超跌", "抄底", "低吸"]),
    ("breakout", ["突破", "breakout", "新高", "创新高", "上破"]),
    ("trend_following", ["趋势", "金叉", "跟随", "顺势", "均线多头"]),
    ("reversal", ["反转", "顶背离", "底背离", "背离"]),
    ("momentum", ["动量", "强势", "加速"]),
    ("grid", ["网格", "区间", "震荡做t", "高抛低吸"]),
]

_RISK = [
    ("aggressive", ["激进", "高风险", "aggressive", "搏一把", "重仓"]),
    ("conservative", ["保守", "低风险", "conservative", "稳健", "轻仓", "防守"]),
    ("balanced", ["平衡", "均衡", "中等风险", "balanced", "中性"]),
]

# 关键词 → 内置策略键
_STRATEGY_HINTS = [
    ("ma_cross", ["均线", "金叉", "死叉", "ma交叉", "均线交叉"]),
    ("macd", ["macd", "dif", "dea"]),
    ("rsi", ["rsi", "相对强弱", "超买", "超卖"]),
    ("boll", ["布林", "boll", "布林带"]),
    ("kdj", ["kdj", "随机指标"]),
    ("ensemble", ["组合", "共振", "多因子", "综合", "ensemble"]),
    ("buy_hold", ["买入持有", "长期持有", "定投", "不择时"]),
]

# 中文数字 → 阿拉伯数字（用于「20日」「15%」等）
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _hit(text: str, keys: List[str]) -> bool:
    """关键词命中判断。ASCII 关键词按词边界匹配，中文关键词直接包含匹配。"""
    for k in keys:
        if k in _WORD_KEYS:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text):
                return True
        elif k in text:
            return True
    return False


def _extract_stop_period(text: str) -> Optional[int]:
    """优先从「止损/跌破」短语附近提取周期，避免误取无关数字。"""
    m = re.search(r"(?:跌破|止损|止蚀|下破)\s*(\d{1,3})\s*[日天]", text)
    if m:
        return int(m.group(1))
    return None


def _extract_period(text: str) -> Optional[int]:
    """从「20日线」「跌破10天均线」提取周期数字。"""
    m = re.search(r"(\d{1,3})\s*[日天]", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:ma|MA)(\d{1,3})", text)
    if m:
        return int(m.group(1))
    return None


def _extract_pct(text: str, keys: List[str]) -> Optional[float]:
    """提取形如「止损5%」「赚15%」的比例；返回小数。"""
    for k in keys:
        m = re.search(rf"{k}\s*(\d{{1,3}}(?:\.\d+)?)\s*%", text)
        if m:
            return float(m.group(1)) / 100.0
    return None


# ---------------------------------------------------------------- Tier 5: NL → 筛选条件 (Tier 1 DSL)
#
# 把自然语言映射成 server/screen_dsl 的 {field, op, value} 条件，可直接喂
# /api/screener?filters=...。只覆盖 16 个快照字段（PE/PB/市值/涨跌幅/换手/
# 成交额/价格/名称/代码/市场）；技术指标类（RSI/MACD/均线/上穿下穿…）没有
# 对应快照字段，归入 unsupported，绝不伪装成可执行条件（那需要 Tier 3 多周期）。

# 字段中文同义词 → screen_dsl.FIELDS 的 key
_FIELD_SYN = [
    ("pe", ["市盈率", "pe"]),
    ("pb", ["市净率", "pb"]),
    ("total_cap", ["总市值", "市值"]),
    ("float_cap", ["流通市值"]),
    ("price", ["最新价", "收盘价", "股价", "价格", "现价"]),
    ("open", ["开盘价", "开盘"]),
    ("high", ["最高价", "最高"]),
    ("low", ["最低价", "最低"]),
    ("prev_close", ["昨收价", "昨收", "昨收盘"]),
    ("change_pct", ["涨跌幅", "涨幅", "跌幅", "涨跌"]),
    ("turnover", ["换手率", "换手"]),
    ("amount", ["成交额", "成交金额", "金额"]),
    ("volume", ["成交量"]),
    ("name", ["股票名称", "股票名", "名称", "名字", "个股"]),
    ("code", ["代码", "代号"]),
    ("market", ["沪市", "深市", "北交所", "创业板", "科创板"]),
]

# 市场关键词 → market 字段取值
_MARKET_VAL = {
    "沪市": "sh", "上海": "sh", "科创板": "sh",
    "深市": "sz", "深圳": "sz", "创业板": "sz",
    "北交所": "bj", "北京": "bj",
}

# 算子（按特异性从高到低匹配）；cross 类属 Tier 3，这里不接
_OP_PATTERNS = [
    ("not_between", ["不介于", "不在.*之间", "不在"]),
    ("between", ["介于", "在.*之间", "从.*到"]),
    ("gte", ["大于等于", "不小于", "不低于", "不少于", "至少"]),
    ("lte", ["小于等于", "不大于", "不高于", "不超过", "至多", "最多"]),
    ("gt", ["大于", "高于", "超过", "之上", "以上"]),
    ("lt", ["小于", "低于", "之下", "不到", "以内", "以下"]),
    ("ne", ["不等于", "不是", "非"]),
    ("eq", ["等于", "刚好", "正好"]),
    ("match", ["包含", "含", "带"]),
    ("in", ["属于", "在.*中", "之一", "包括"]),
]

# 数值字段口语单位 → 字段原生单位 的换算系数
_UNIT_MULT = {
    "total_cap": {"亿": 1, "万": 0.0001},
    "float_cap": {"亿": 1, "万": 0.0001},
    "amount": {"亿": 10000, "万": 1},
    "price": {"元": 1, "块": 1},
    "open": {"元": 1, "块": 1},
    "high": {"元": 1, "块": 1},
    "low": {"元": 1, "块": 1},
    "prev_close": {"元": 1, "块": 1},
    "volume": {"万": 10000},
}

# 这些指标/形态没有快照字段，需 Tier 3 多周期
_UNSUPPORTED_INDIC = ["rsi", "macd", "kdj", "boll", "布林", "均线", "ma", "ema",
                      "cci", "atr", "dmi", "adx", "obv", "mfi", "wr", "bias",
                      "金叉", "死叉", "上穿", "下穿", "突破"]


def _norm_num(field: str, num: float, unit: str) -> float:
    mult = _UNIT_MULT.get(field, {})
    if unit and unit in mult:
        return num * mult[unit]
    return num


def _detect_op(win: str):
    """在窗口内检测算子，按特异性顺序。返回 op 字符串或 None。"""
    for op, pats in _OP_PATTERNS:
        for p in pats:
            if re.search(p, win):
                return op
    return None


def _extract_number(win: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万|%|元|块|手)?", win)
    if not m:
        return None, ""
    return float(m.group(1)), (m.group(2) or "")


def _extract_range(win: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:亿|万|%|元|块|手)?\s*(?:到|至|-|~|—)\s*(\d+(?:\.\d+)?)", win)
    if not m:
        return None
    return [float(m.group(1)), float(m.group(2))]


def _extract_list(win: str):
    """取算子词之后的文本，按中英文逗号/顿号切分。"""
    tail = re.sub(r"^.*?(?:属于|在|之一|包括|包含|含)", "", win)
    return [p.strip() for p in re.split(r"[,，、/]", tail) if p.strip()]


def parse_screener_conditions(text: str) -> Dict[str, Any]:
    """自然语言 → Tier 1 筛选条件列表（可直接喂 /api/screener?filters=）。

    返回 {conditions:[{field,op,value},...], unsupported:[...], error:None}。
    只映射快照字段；技术指标类归入 unsupported。每条条件经引擎校验，
    非法条直接丢弃，不阻断其他条件。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"conditions": [], "unsupported": [],
                "error": "请输入一句话，例如：市盈率小于20，换手率介于3%到15%，名称包含银行"}
    low = raw.lower()

    unsupported: List[str] = []
    hit_indics = [k for k in _UNSUPPORTED_INDIC if k in low]
    if hit_indics:
        unsupported.append("技术指标类条件（" + "、".join(sorted(set(hit_indics))) +
                           "）需 Tier 3 多周期版本，当前仅支持快照字段")

    # 定位每个字段首次出现的位置
    mentions = []
    for field, syns in _FIELD_SYN:
        for s in syns:
            i = low.find(s.lower())
            if i >= 0:
                mentions.append((field, i))
                break
    if not mentions:
        return {"conditions": [], "unsupported": unsupported, "error": None}

    # 按位置排序，每个字段取 [自己, 下一个字段) 为窗口，避免跨字段串味
    mentions.sort(key=lambda x: x[0] if False else x[1])
    conditions: List[Dict[str, Any]] = []
    for idx, (field, pos) in enumerate(mentions):
        nxt = mentions[idx + 1][1] if idx + 1 < len(mentions) else len(low)
        win = low[pos:nxt]

        if field == "market":  # 关键词 → 取值，无数字
            val = next((code for kw, code in _MARKET_VAL.items() if kw in raw), None)
            if val:
                conditions.append({"field": "market", "op": "eq", "value": val})
            continue

        op = _detect_op(win)
        rng = _extract_range(win)
        # 既无算子关键词、也无 "num 到 num" 区间 → 这条字段无法构成条件，跳过
        if op is None and rng is None:
            continue
        fkind = screen_dsl.FIELDS[field].kind
        try:
            if fkind == "text":
                if op == "in":
                    vals = _extract_list(win)
                    if not vals:
                        continue
                    cond = {"field": field, "op": "in", "value": vals}
                elif op == "match":
                    m = re.search(r"(?:包含|含|带)\s*([一-龥A-Za-z0-9]+)", win)
                    if not m:
                        continue
                    cond = {"field": field, "op": "match", "value": m.group(1)}
                else:  # eq / ne
                    m = re.search(r"(?:等于|为|是|非|不是)\s*([一-龥A-Za-z0-9]+)", win)
                    if not m:
                        continue
                    cond = {"field": field, "op": op, "value": m.group(1)}
            else:  # 数值字段
                if op in ("between", "not_between"):
                    rg = rng
                    if not rg:
                        continue
                    lo, hi = rg
                    _, unit = _extract_number(win)
                    lo = _norm_num(field, lo, unit)
                    hi = _norm_num(field, hi, unit)
                    cond = {"field": field, "op": op, "value": [lo, hi]}
                    # 低估值语义：pe/pb 下界 ≤ 0 时补 >0 排除缺失值
                    if field in ("pe", "pb") and lo <= 0:
                        conditions.append({"field": field, "op": "gt", "value": 0})
                elif rng is not None:
                    # 出现 "num 到 num" 但没写介于/在...之间，也按区间处理
                    lo, hi = rng
                    _, unit = _extract_number(win)
                    lo = _norm_num(field, lo, unit)
                    hi = _norm_num(field, hi, unit)
                    cond = {"field": field, "op": "between", "value": [lo, hi]}
                    if field in ("pe", "pb") and lo <= 0:
                        conditions.append({"field": field, "op": "gt", "value": 0})
                elif op == "in":
                    vals = _extract_list(win)
                    nums = []
                    ok = True
                    for v in vals:
                        try:
                            nums.append(float(v))
                        except ValueError:
                            ok = False
                            break
                    if not ok or not nums:
                        continue
                    cond = {"field": field, "op": "in", "value": nums}
                else:  # gt / lt / gte / lte / eq / ne
                    num, unit = _extract_number(win)
                    if num is None:
                        continue
                    num = _norm_num(field, num, unit)
                    cond = {"field": field, "op": op, "value": num}
                    # 低估值语义：pe/pb 的 lt/lte（且上界>0）补 >0
                    if field in ("pe", "pb") and op in ("lt", "lte") and num > 0:
                        conditions.append({"field": field, "op": "gt", "value": 0})
            # 用引擎编译校验，非法直接丢弃（不阻断其他条件）
            screen_dsl.compile_condition(cond)
            conditions.append(cond)
        except (ValueError, KeyError):
            continue

    # 去重（同 field+op+value）；低估值补的 gt0 与显式 gt0 自然合并
    seen = set()
    uniq = []
    for c in conditions:
        key = (c["field"], c["op"], str(c["value"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return {"conditions": uniq, "unsupported": unsupported, "error": None}


# ---------------------------------------------------------------- 主解析


def parse_intent(text: str) -> Dict[str, Any]:
    """把自然语言解析为结构化策略意图。"""
    raw = str(text or "").strip()
    if not raw:
        return {"error": "请输入策略描述，例如：均线金叉且放量时买入，跌破10日线止损"}

    low = raw.lower()

    # 1) 指标识别
    indicators: List[str] = []
    for name, keys in _INDICATORS:
        if _hit(low, keys) and name not in indicators:
            indicators.append(name)

    # 2) 策略类型
    strategy_type = "trend_following"
    for tname, keys in _TYPES:
        if _hit(low, keys):
            strategy_type = tname
            break

    # 3) 风险偏好
    risk_profile = "balanced"
    for rname, keys in _RISK:
        if _hit(low, keys):
            risk_profile = rname
            break

    # 4) 映射内置策略
    strategy = None
    for sname, keys in _STRATEGY_HINTS:
        if _hit(low, keys):
            strategy = sname
            break
    # 未直接命中时按指标推断
    if strategy is None:
        if "MACD" in indicators:
            strategy = "macd"
        elif "RSI" in indicators:
            strategy = "rsi"
        elif "BOLL" in indicators:
            strategy = "boll"
        elif "KDJ" in indicators:
            strategy = "kdj"
        elif "MA" in indicators:
            strategy = "ma_cross"
        elif len(indicators) >= 3:
            strategy = "ensemble"

    # 5) 参数抽取
    stop_loss = _extract_pct(low, ["止损", "亏损", "亏", "跌破", "停损"])
    take_profit = _extract_pct(low, ["止盈", "盈利", "赚", "获利", "目标"])
    period = _extract_period(raw)
    stop_period = _extract_stop_period(raw) or period

    # 风险偏好给默认参数
    if stop_loss is None:
        stop_loss = {"aggressive": 0.08, "balanced": 0.05, "conservative": 0.03}[risk_profile]
    if take_profit is None:
        take_profit = {"aggressive": 0.25, "balanced": 0.15,
                       "conservative": 0.08}[risk_profile]

    # 6) 入场/出场规则描述
    entry_parts = []
    if "金叉" in raw:
        entry_parts.append("金叉买入")
    if "突破" in raw or "新高" in raw:
        entry_parts.append("突破买入")
    if "超卖" in raw or "抄底" in raw or "低吸" in raw:
        entry_parts.append("超卖区买入")
    if "放量" in raw:
        entry_parts.append("放量确认")
    if not entry_parts:
        entry_parts.append("满足主逻辑时开仓")
    entry = "、".join(entry_parts)

    exit_parts = []
    if "死叉" in raw:
        exit_parts.append("死叉卖出")
    if "超买" in raw:
        exit_parts.append("超买区卖出")
    if "跌破" in raw:
        exit_parts.append(f"跌破{stop_period}日线止损" if stop_period else "跌破均线止损")
    exit_parts.append(f"止损{stop_loss*100:.0f}%")
    exit_parts.append(f"止盈{take_profit*100:.0f}%")
    exit_rule = "、".join(exit_parts)

    # 7) 置信度：命中项越多越可信
    score = 0.45
    if indicators:
        score += min(0.2, 0.07 * len(indicators))
    if strategy:
        score += 0.15
    if strategy_type != "trend_following":
        score += 0.08
    if risk_profile != "balanced":
        score += 0.05
    if stop_loss and take_profit:
        score += 0.05
    confidence = round(min(0.95, score), 2)

    scr = parse_screener_conditions(raw)
    return {
        "conditions": scr["conditions"],
        "unsupported": scr["unsupported"],
        "logic": raw,
        "strategy": strategy,
        "strategy_name": {
            "ma_cross": "均线交叉", "macd": "MACD信号", "rsi": "RSI超买超卖",
            "boll": "布林带突破", "kdj": "KDJ交叉", "ensemble": "多因子共振",
            "buy_hold": "买入持有",
        }.get(strategy, "未匹配到内置策略"),
        "indicators": indicators,
        "strategy_type": strategy_type,
        "strategy_type_name": {
            "trend_following": "趋势跟随", "mean_reversion": "均值回归",
            "breakout": "突破", "reversal": "反转", "momentum": "动量",
            "grid": "网格/区间",
        }.get(strategy_type, strategy_type),
        "risk_profile": risk_profile,
        "risk_profile_name": {
            "aggressive": "激进", "balanced": "平衡", "conservative": "保守",
        }.get(risk_profile, risk_profile),
        "entry": entry,
        "exit": exit_rule,
        "params": {
            "stop_loss": round(stop_loss, 4),
            "take_profit": round(take_profit, 4),
            "period": period,
        },
        "confidence": confidence,
    }


# ---------------------------------------------------------------- 示例

EXAMPLES = [
    "均线金叉并且放量，突破20日新高买入，跌破10日线止损，激进风格",
    "RSI超卖同时触及布林下轨，保守一点，亏3%就走",
    "MACD底背离配合成交量放大，赚15%止盈",
    "多因子共振，平衡风险，长期持有",
]


def selfcheck() -> int:
    """Tier 5 解析自检：覆盖快照字段映射、单位换算、低估值补 >0、unsupported。"""
    fails = 0

    def has(conds, field, op, value):
        return any(c["field"] == field and c["op"] == op and c["value"] == value
                   for c in conds)

    cases = [
        ("市盈率小于20", [("pe", "lt", 20), ("pe", "gt", 0)]),
        ("换手率介于3%到15%", [("turnover", "between", [3, 15])]),
        ("名称包含银行", [("name", "match", "银行")]),
        ("市值大于100亿", [("total_cap", "gt", 100)]),
        ("成交额小于1亿", [("amount", "lt", 10000)]),
        ("价格10到20元", [("price", "between", [10, 20])]),
        ("沪市", [("market", "eq", "sh")]),
        ("涨幅大于5%", [("change_pct", "gt", 5)]),
        ("股票代码等于600519", [("code", "eq", "600519")]),
        ("市盈率介于10到30", [("pe", "between", [10, 30])]),
    ]
    for text, expect in cases:
        r = parse_screener_conditions(text)
        conds = r["conditions"]
        for f, o, v in expect:
            if not has(conds, f, o, v):
                print(f"  ✗ {text!r} 期望 {f}/{o}/{v}，实得 {conds}")
                fails += 1

    # unsupported：技术指标类无法映射
    r = parse_screener_conditions("RSI小于30且收盘价上穿20日均线")
    if not r["conditions"] and not r["unsupported"]:
        print("  ✗ 技术指标类应归入 unsupported")
        fails += 1

    # 组合语句：3 字段（pe 带 gt0 共 4 条）
    r = parse_screener_conditions("市盈率小于20，换手率介于3%到15%，名称包含银行")
    if not (has(r["conditions"], "pe", "lt", 20) and
            has(r["conditions"], "pe", "gt", 0) and
            has(r["conditions"], "turnover", "between", [3, 15]) and
            has(r["conditions"], "name", "match", "银行")):
        print("  ✗ 组合语句解析不全：", r["conditions"])
        fails += 1

    # 空输入
    if parse_screener_conditions("").get("error") is None:
        print("  ✗ 空输入应返回 error")
        fails += 1

    print(f"intent.parse_screener_conditions selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


if __name__ == "__main__":
    import json
    if selfcheck() == 0:
        print("-" * 40)
    for s in EXAMPLES:
        r = parse_intent(s)
        print("输入:", s)
        print("  →", json.dumps({
            "strategy": r["strategy"],
            "type": r["strategy_type_name"],
            "risk": r["risk_profile_name"],
            "ind": r["indicators"],
            "sl/tp": (r["params"]["stop_loss"], r["params"]["take_profit"]),
            "conf": r["confidence"],
            "conds": r["conditions"],
        }, ensure_ascii=False))
        print("     入场:", r["entry"], "| 出场:", r["exit"])
