"""词典情绪分析 —— 纯本地零成本，蒸馏自 go-stock（stock_sentiment_analysis.go）。

给新闻/公告/快讯文本打「看涨 / 看跌 / 中性」标签，不需要任何外部 API 或模型。

## 为什么不用 jieba 分词（与蒸馏来源 go-stock 的差异）

go-stock 用 gse 分词后查词典。但中文分词有个隐患：像「创新高」这样的
金融短语常被切成「创新 / 高」，词典词就丢了。这里改用**免分词词典扫描**：

    对每个词典词在原文里 find() 定位，再看它前面 1~2 个字
    是不是否定词 / 程度副词。

词典词在文本中一定是连续出现的，所以这个方案不丢词、不依赖任何
分词器（连 jieba 都不用装），对新闻标题这种短文本尤其鲁棒。
打分规则（否定反转 / 程度乘数 / 转折后段 ×1.5）与 go-stock 保持一致。

## 打分规则（照抄 go-stock，一处明确标注的例外）

  · 正负词各约 40 个（2026-10 词表体检后扩充），权重 1.0~3.0；
  · 否定词（不/没/无/非/未/别/勿）紧邻在前 → 极性反转；
    （例外：「利好 / 利空」被否定时不反转，改判中性——无利好≠有利空）
  · 程度副词（非常 1.8 / 极其 2.2 / 稍微 0.6 …）紧邻在前 → 权重相乘；
  · 转折词（但是/然而/不过/却/可是）把文本切段，**后段整体 ×1.5**；
  · 阈值：score > +1.0 → 看涨，< -1.0 → 看跌，否则中性。

  例外：go-stock 的「程度词只看紧邻前一词」，且否定与程度同时出现时
  只生效一个（先命中先得）。这里保持同样语义，不做"更聪明"的叠加——
  词典法的收益来自简单可预期，而不是精巧。

## 已知天花板（词典法的固有缺陷）

  词典法只求把 80% 的常见表达判对，上 LLM 才是根治方案（#93 的活）。
  已修掉的两类高频误判：①「不 + 看涨词（看好/看涨）」→ 看跌；
  ②「无/不 + 利好/利空」→ 中性（而非反转为利空）。
  仍存在的硬伤：否定词与情绪词隔 >3 个衬字时（如「拟不进行现金分红」
  中 不 距 分红 5 字）会漏判，需靠更长的字面短语兜底。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

# ---------------------------------------------------------------- 词典
# 与 go-stock 逐词对齐（stock_sentiment_analysis.go L12~L47）

POSITIVE_WORDS: Dict[str, float] = {
    "上涨": 2.0, "涨停": 3.0, "牛市": 3.0, "反弹": 2.0, "新高": 2.5,
    "利好": 2.5, "增持": 2.0, "买入": 2.0, "推荐": 1.5, "看多": 2.0,
    "盈利": 2.0, "增长": 2.0, "超预期": 2.5, "强劲": 1.5, "回升": 1.5,
    "复苏": 2.0, "突破": 2.0, "创新高": 3.0, "回暖": 1.5, "上扬": 1.5,
    "利好消息": 3.0, "收益增长": 2.5, "利润增长": 2.5, "业绩优异": 2.5,
    "潜力股": 2.0, "绩优股": 2.0, "强势": 1.5, "走高": 1.5, "攀升": 1.5,
    "大涨": 2.5, "飙升": 3.0, "井喷": 3.0, "爆发": 2.5, "暴涨": 3.0,
    "站上": 1.5,     "收涨": 2.0, "批准": 1.5, "获批": 1.5,
    # ponytail: AI 词表体检(2026-10) 采纳的新词，已人工过目，非 LLM 自动写入
    "拉升": 1.5, "拓展业务": 1.2, "纷纷走高": 1.8,
    "看好": 2.0, "看涨": 2.0, "分红": 2.0,
}

NEGATIVE_WORDS: Dict[str, float] = {
    "下跌": 2.0, "跌停": 3.0, "熊市": 3.0, "回调": 1.5, "新低": 2.5,
    "利空": 2.5, "减持": 2.0, "卖出": 2.0, "看空": 2.0, "亏损": 2.5,
    "下滑": 2.0, "萎缩": 2.0, "不及预期": 2.5, "疲软": 1.5, "恶化": 2.0,
    "衰退": 2.0, "跌破": 2.0, "创新低": 3.0, "走弱": 1.5, "下挫": 1.5,
    "利空消息": 3.0, "收益下降": 2.5, "利润下滑": 2.5, "业绩不佳": 2.5,
    "垃圾股": 2.0, "风险股": 2.0, "弱势": 1.5, "走低": 1.5, "缩量": 2.5,
    "大跌": 2.5, "暴跌": 3.0, "崩盘": 3.0, "跳水": 3.0, "重挫": 3.0,
    "收跌": 2.0, "回落": 1.5, "下探": 1.5,
    # ponytail: AI 词表体检(2026-10) 采纳的新词（上调已移出，因其高频误判）
    "传出爆炸声": 2.0, "双重挑战": 1.5,
    "不分红": 2.0, "取消分红": 2.0, "不进行现金分红": 2.0,
}

#: 否定词：紧邻在情绪词前 → 极性反转
NEGATION_WORDS = {"不", "没", "无", "非", "未", "别", "勿"}

#: ponytail: 「否定 + 利好/利空」不按通用规则反转，改判中性
#: （无利好 ≠ 有利空，只是预期落空）。其余情绪词仍走通用反转。
NEG_NEUTRAL_WORDS = {"利好", "利空", "利好消息", "利空消息"}

#: 程度副词：紧邻在情绪词前 → 权重乘数
DEGREE_WORDS: Dict[str, float] = {
    "非常": 1.8, "极其": 2.2, "太": 1.8, "很": 1.5,
    "比较": 0.8, "稍微": 0.6, "有点": 0.7, "显著": 1.5,
    "大幅": 1.8, "急剧": 2.0, "轻微": 0.6, "小幅": 0.7,
}

#: 转折词：首个转折词把文本切段，后段整体 ×1.5
TRANSITION_WORDS = {"但是", "然而", "不过", "却", "可是"}

#: 情绪词按长度倒序（长词优先：先匹配「创新高」再让「新高」跳过重叠区）。
#: 负词在入表时就带上负号——后面 sign/base/mult 统一相乘，
#: 否定反转（sign=-1 × 负 base = 正分）天然就是对的。
def _base_all_words():
    return [(w, s) for w, s in POSITIVE_WORDS.items()] \
         + [(w, -s) for w, s in NEGATIVE_WORDS.items()]


# ---------------------------------------------------------------- 用户扩展词典
# ponytail: 词表走「硬编码基础层 + data/lexicon_extra.json 用户层」双轨。
# 运行时不改源码即可增删词；用户词排在同名基础词之前（稳定排序）用于覆盖含糊基础词
# （如基础层「减持」为看跌，但某语境需区分时，用户加一条同名词即可覆盖——排序在前、先命中生效）。
# selfcheck 只锁基础层语义，不约束用户词，避免后人加词误改已有规则。

EXTRA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "lexicon_extra.json")


def _load_extra():
    try:
        with open(EXTRA_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "pos" in d and "neg" in d:
            return d
    except Exception:
        pass
    return {"pos": {}, "neg": {}}


def _extra_all_words(extra):
    return [(w, s) for w, s in (extra.get("pos") or {}).items()] \
         + [(w, -s) for w, s in (extra.get("neg") or {}).items()]


def rebuild():
    """合并基础层 + 用户层，重建扫描表。用户词在前（同名先命中），基础层不动。"""
    global _ALL_WORDS
    extra = _load_extra()
    merged = _extra_all_words(extra) + _base_all_words()
    _ALL_WORDS = sorted(merged, key=lambda kv: -len(kv[0]))


_ALL_WORDS = sorted(_base_all_words(), key=lambda kv: -len(kv[0]))  # 初始（rebuild 会覆盖）
rebuild()

#: 转折词正则（用于定位首个转折点；「却」单字容易误伤「却步」，
#: 但词典法的固有精度就这样，不值得为单字转折写例外表）
_TRANS_RE = re.compile("但是|然而|不过|可是|却")

_TONE_TEXT = {"pos": "看涨", "neg": "看跌", "neutral": "中性"}


def _prefix_mod(text: str, i: int) -> tuple:
    """看情绪词前面的字：返回 (符号修正, 乘数)。

    否定：往前 3 字逐字找（从近到远）。这是相对 go-stock 的一处增强——
    它只看分词后的紧邻前一词，但中文否定常带衬词（不【进行】分配、
    未【出现】好转、无【重大】利好），紧邻窗口会漏。3 字内逐字找
    能接住这些常见句式，误伤概率低（这 3 字里恰好有独立否定词
    而语义上不否定该情绪词的情况罕见）。
    程度：保持紧邻（非常强势 / 小幅上涨 都是紧邻的自然表达）。

    ⚠ 程度必须先于否定判断：「非常」的首字「非」在否定词集里，
    若先做逐字否定扫描，「非常强势」会被误判成否定反转。
    程度词是整词命中，天然优先级更高。
    """
    for n in (2, 1):
        seg = text[max(0, i - n):i]
        if seg and seg in DEGREE_WORDS:
            return 1, DEGREE_WORDS[seg]
    for n in (1, 2, 3):
        if i - n >= 0 and text[i - n] in NEGATION_WORDS:
            return -1, 1.0
    return 1, 1.0


def analyze(text: str) -> Dict[str, Any]:
    """对一段文本打情绪分。返回：
        {score, tone: pos|neg|neutral, tone_text, pos_n, neg_n, hits}
    """
    t = str(text or "")
    if not t.strip():
        return {"score": 0.0, "tone": "neutral", "tone_text": _TONE_TEXT["neutral"],
                "pos_n": 0, "neg_n": 0, "hits": []}

    # 转折切段：后段整体 ×1.5（go-stock 同款）
    m = _TRANS_RE.search(t)
    trans_at = m.start() if m else -1

    score = 0.0
    pos_n = neg_n = 0
    hits: List[str] = []

    # 词典词扫描：长词优先，命中后跳过该词长度，避免「创新高」再吃一次「新高」
    i = 0
    n = len(t)
    while i < n:
        hit = None
        for w, base in _ALL_WORDS:
            if t.startswith(w, i):
                hit = (w, base)
                break
        if hit is None:
            i += 1
            continue
        w, base = hit
        sign, mult = _prefix_mod(t, i)
        # ponytail: 否定 + 利好/利空 → 中性（不反转），仅标注不计分
        if sign < 0 and w in NEG_NEUTRAL_WORDS:
            hits.append(w + "(否定→中性)")
            i += len(w)
            continue
        if trans_at >= 0 and i > trans_at:
            mult *= 1.5            # 转折后段权重
        s = sign * base * mult
        score += s
        if s > 0:
            pos_n += 1
        elif s < 0:
            neg_n += 1
        hits.append(w + ("" if (sign > 0 and mult == 1.0) else
                         ("(否定)" if sign < 0 else f"(×{mult:g})")))
        i += len(w)

    tone = "pos" if score > 1.0 else ("neg" if score < -1.0 else "neutral")
    return {"score": round(score, 2), "tone": tone,
            "tone_text": _TONE_TEXT[tone], "pos_n": pos_n, "neg_n": neg_n,
            "hits": hits}


def batch(texts: List[str]) -> List[Dict[str, Any]]:
    """批量打分（同输入顺序）。"""
    return [analyze(t) for t in (texts or [])]


def tag_notice(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给公告/研报条目列表就地补 sentiment 字段（title 为主，fallback content）。

    单条失败不影响整批——公告列表宁可少个标签也不能渲染不出来。
    """
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        try:
            text = str(it.get("title") or it.get("content") or "")
            r = analyze(text)
            it["sentiment"] = {"score": r["score"], "tone": r["tone"],
                               "tone_text": r["tone_text"]}
        except Exception:
            pass
    return items


# ---------------------------------------------------------------- 用户扩展词典 API

def list_extra():
    """返回用户扩展词 {pos:{...}, neg:{...}}（只读，不暴露基础层）。"""
    return _load_extra()


def add_word(word, polarity, weight=2.0):
    """新增/覆盖一个用户情绪词。polarity∈{'pos','neg'}，weight 夹在 1.0~3.0。

    落在对侧用户层的同名词会被移除（避免一词双极性）。返回更新后的 list_extra()。
    基础层硬编码词不受此影响；若需覆盖某含糊基础词，加一条同名用户词即可
    （排序在前、先命中生效）。
    """
    word = (word or "").strip()
    if not word:
        raise ValueError("词不能为空")
    if polarity not in ("pos", "neg"):
        raise ValueError("极性必须是 pos 或 neg")
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        raise ValueError("权重必须是数字")
    weight = max(1.0, min(3.0, round(weight, 2)))
    d = _load_extra()
    d.setdefault("pos", {})
    d.setdefault("neg", {})
    other = "neg" if polarity == "pos" else "pos"
    d[other].pop(word, None)          # ponytail: 同词只保留一侧极性
    d[polarity][word] = weight
    _save_extra(d)
    rebuild()
    return d


def remove_word(word):
    """删除一个用户词（基础层不受影响）。返回更新后的 list_extra()。"""
    word = (word or "").strip()
    d = _load_extra()
    d.setdefault("pos", {})
    d.setdefault("neg", {})
    removed = d["pos"].pop(word, None) is not None or d["neg"].pop(word, None) is not None
    if removed:
        _save_extra(d)
        rebuild()
    return d


def _save_extra(d):
    os.makedirs(os.path.dirname(EXTRA_PATH), exist_ok=True)
    with open(EXTRA_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 自检

def selfcheck() -> int:
    """真值表：规则逐条锁死，防止后人"优化"词典时悄悄改语义。"""
    fails = []

    def eq(got, want, label):
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {label}" + ("" if ok else f"：got={got!r} want={want!r}"))
        if not ok:
            fails.append(label)

    def tone_of(t):
        return analyze(t)["tone"]

    def score_of(t):
        return analyze(t)["score"]

    print("[1] 单词基础分")
    eq(score_of("股价大涨"), 2.5, "大涨 → +2.5")
    eq(score_of("股价暴跌"), -3.0, "暴跌 → -3.0")
    eq(tone_of("股价大涨"), "pos", "大涨 → 看涨")
    eq(tone_of("股价暴跌"), "neg", "暴跌 → 看跌")

    print("\n[2] 阈值（|score|<=1.0 为中性）")
    eq(tone_of("股价强势"), "pos", "强势(+1.5>1.0) → 看涨")
    eq(tone_of("今天收盘"), "neutral", "无情绪词 → 中性")
    eq(tone_of(""), "neutral", "空文本 → 中性")

    print("\n[3] 否定反转（含衬词场景：否定词与情绪词隔着衬字）")
    # ponytail: 「无利好」不再判利空，改中性（2026-10 词表体检采纳）
    eq(tone_of("无重大利好兑现"), "neutral", "无【重大】利好 → 中性（否定利好≠利空）")
    eq(score_of("无重大利好"), 0.0, "无重大利好 → 0.0")
    eq(tone_of("业绩不及预期"), "neg", "不及预期 → 看跌（词典原生负词）")
    eq(tone_of("未出现上涨"), "neg", "未【出现】上涨 → 看跌（衬词窗口）")
    eq(tone_of("拟不进行卖出"), "pos", "不【进行】+卖出(负词) → 反转看涨")
    eq(tone_of("机构并不看空该股"), "pos", "不+看空(负词) → 反转看涨（双向否定）")
    eq(tone_of("不看好后市走势"), "neg", "不+看好(正词) → 反转看跌")
    eq(tone_of("公司不分红"), "neg", "不+分红(正词) → 反转看跌")
    eq(tone_of("取消分红计划"), "neg", "取消分红 → 看跌（字面短语）")
    eq(tone_of("拟不进行现金分红"), "neg", "不进行现金分红 → 看跌（字面兜底）")

    print("\n[4] 程度乘数")
    eq(score_of("股价非常强势"), 1.5 * 1.8, "非常+强势 → 1.5×1.8=2.7")
    eq(score_of("股价小幅上涨"), 2.0 * 0.7, "小幅+上涨 → 2.0×0.7=1.4")

    print("\n[5] 转折后段 ×1.5")
    r = analyze("短期回调，但是长期依然看多")
    eq(r["score"], round(-1.5 + 2.0 * 1.5, 2), "回调-1.5 + 转折后看多×1.5=+3.0 → 1.5")
    eq(r["tone"], "pos", "转折后主导 → 看涨")

    print("\n[6] 长词优先（不重复计分）")
    eq(analyze("股价创新高")["hits"], ["创新高"], "创新高 命中一次，不再吃 新高")

    print("\n[7] 混合文本")
    eq(tone_of("公司业绩超预期，订单暴涨"), "pos", "双正面 → 看涨")
    eq(tone_of("利好落地，股价大跌"), "neutral", "利好+大跌=-0.5 落在±1.0内 → 中性（阈值语义）")
    eq(tone_of("利空叠加股价大跌"), "neg", "-2.5-3.0=-5.5 → 看跌")

    print("\n[8] batch 与 tag_notice")
    rs = batch(["大涨", "大跌", "横盘"])
    eq([r["tone"] for r in rs], ["pos", "neg", "neutral"], "batch 保序")
    items = [{"title": "公司拟回购股份"}, {"title": None, "content": "股价创新高"},
             {"title": "公告"}, "bad-item"]
    tag_notice(items)
    eq(items[0]["sentiment"]["tone"], "neutral", "tag_notice：无情绪词中性")
    eq(items[1]["sentiment"]["tone"], "pos", "tag_notice：content 兜底生效")
    eq("sentiment" in items[2], True, "tag_notice：普通标题也有字段")
    eq(isinstance(items[3], str), True, "tag_notice：坏条目原样保留不炸")

    print("\n[9] AI 词表体检新词（2026-10 采纳）")
    eq(tone_of("板块短线拉升"), "pos", "拉升 → 看涨")
    eq(tone_of("汇丰拓展业务"), "pos", "拓展业务 → 看涨")
    eq(tone_of("电力煤炭股纷纷走高"), "pos", "纷纷走高 → 看涨")
    eq(tone_of("行长看多并看好"), "pos", "看好 → 看涨")
    eq(tone_of("机构看涨该股"), "pos", "看涨 → 看涨")
    eq(tone_of("公司现金分红"), "pos", "分红 → 看涨")
    eq(tone_of("基辅传出爆炸声"), "neg", "传出爆炸声 → 看跌")
    eq(tone_of("面临双重挑战"), "neg", "双重挑战 → 看跌")
    eq(tone_of("利率上调"), "neutral", "上调已移出看跌词 → 中性（不再误判）")

    print("\n" + "=" * 46)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(selfcheck())
