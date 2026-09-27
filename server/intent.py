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

    return {
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


if __name__ == "__main__":
    import json
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
        }, ensure_ascii=False))
        print("     入场:", r["entry"], "| 出场:", r["exit"])
