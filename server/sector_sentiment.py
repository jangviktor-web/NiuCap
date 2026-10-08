"""新闻 → 板块 舆情聚合（纯函数，面板与 lab 共用，无 CLI 依赖）。

为什么独立成模块：快讯页（/api/newsfeed）返回带情绪 + stocks 的快讯，
与 sector_news_lab 同源；把聚合能力下沉到这里，app 与 lab 都 import 同一个
实现，不复制板块词库、也不把 lab 的 argparse/__main__ 外壳拖进服务进程。

板块归因优先级（见 aggregate）：先 ticker/公司名 经 stock_sector_map 精确反查；
反查不到（免费源无 stocks、或不在种子表）再回落关键词。关键词法的天花板
（一篇多板块命中、"不看好"被词典误判正面）属数据/模型边界，非代码问题。

范围（ponytail：先描述、后预测）：本模块只做「描述性参考」——把当日快讯聚成
板块情绪热度。不做「明天开盘资金流向预测」：那是方向性预测，需历史新闻归档 +
板块逐日资金流（依赖全市场股票→板块归属，库里没有），无法在本环境验证。
"""
from __future__ import annotations
import collections
import re
from typing import Any, Dict, List

import stock_sector_map as _ssm

# 板块 → 关键词（覆盖主要 A 股板块；一条新闻可命中多个板块）
# 中文走子串匹配；英文别名走词边界匹配（见 map_sectors）。
# 英文别名用于接入 OkSurf（免费免密钥的 Google News 源，标题为英文）。
SECTOR_KEYWORDS: Dict[str, List[str]] = {
    "新能源": ["新能源", "光伏", "锂电", "储能", "逆变器", "风电", "氢能",
              "solar", "lithium", "ev", "battery", "renewable", "energy storage",
              "hydrogen", "photovoltaic", "wind power"],
    "半导体": ["半导体", "芯片", "集成电路", "晶圆", "光刻", "封测",
              "semiconductor", "chip", "wafer", "foundry", "fab"],
    "医药": ["医药", "创新药", "生物制药", "医疗", "CXO", "疫苗", "中药",
            "pharma", "drug", "vaccine", "biotech", "healthcare", "medicine"],
    "消费": ["白酒", "食品饮料", "消费", "零售", "免税", "家电",
            "consumer", "retail", "liquor", "beverage", "grocery"],
    "军工": ["军工", "国防", "兵器", "航空发动机", "卫星",
            "defense", "military", "aerospace", "weapon"],
    "金融": ["券商", "银行", "保险", "金融", "信托", "信贷",
            "bank", "broker", "insurance", "credit", "fed", "rate", "bond"],
    "地产": ["地产", "房地产", "楼市", "房企", "棚改", "物业",
            "real estate", "property", "housing", "mortgage", "realtor"],
    "汽车": ["汽车", "整车", "智能驾驶", "新能源车", "零部件", "比亚迪",
            "automaker", "electric vehicle", "tesla", "byd", "car sales"],
    "有色": ["有色", "黄金", "铜", "铝", "锂矿", "稀土", "钴",
            "gold", "copper", "aluminum", "rare earth", "metal", "mining"],
    "化工": ["化工", "化肥", "化纤", "塑料", "纯碱", "钛白粉",
            "chemical", "petrochemical"],
    "能源": ["煤炭", "电力", "石油", "天然气", "火电", "水电", "核电",
            "oil", "coal", "natural gas", "power", "nuclear", "crude"],
    "建材": ["钢铁", "水泥", "玻璃", "螺纹钢", "建材",
            "steel", "cement", "cement"],
    "科技AI": ["人工智能", "AI", "算力", "机器人", "大模型", "数据中心", "GPU",
              "artificial intelligence", "robot", "gpu", "datacenter",
              "compute", "cloud", "software"],
    "农业": ["农业", "猪肉", "粮食", "种业", "养殖",
            "agriculture", "grain", "pork", "farm"],
    "中概港股": ["港股", "中概", "恒生", "南向", "互联互通",
               "hang seng", "hk", "china stock", "chinese shares", "adr"],
}


def map_sectors(content: str) -> List[str]:
    """扫描文本命中哪些板块（一条可命中多个）。

    中文关键词走子串匹配；英文关键词走词边界 + 大小写不敏感，
    避免 "ev" 误中 "revenue"、"ai" 误中 "again" 之类。
    """
    text = content or ""
    low = text.lower()
    hit = []
    for sec, kws in SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw.isascii():
                # ponytail: 朴素词边界启发式，足够舆情粗分；要精确换 NER
                if re.search(r"(?<![a-z])" + re.escape(kw.lower()) + r"(?![a-z])", low):
                    hit.append(sec)
                    break
            else:
                if kw in text:
                    hit.append(sec)
                    break
    return hit


def aggregate(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把快讯聚到板块。返回 {板块: {n,pos,neg,neu,net,score_sum,samples}}。

    板块归因优先级：先 ticker/公司名 经 stock_sector_map 反查（精确）；
    反查不到（OkSurf/FreeNews 无 stocks、或 ticker 不在种子表）再回落关键词。
    """
    agg: Dict[str, Dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "pos": 0, "neg": 0, "neu": 0,
                 "score_sum": 0.0, "samples": []})
    for it in items:
        content = it.get("content", "") or ""
        tone = (it.get("sentiment") or {}).get("tone", "neutral")
        score = float((it.get("sentiment") or {}).get("score", 0.0) or 0.0)
        # ponytail: ticker 反查优先，关键词作回落——避免一篇多板块的噪声放大
        secs = _ssm.sectors_for_stocks(it.get("stocks")) or map_sectors(content)
        if not secs:
            continue
        for s in secs:
            a = agg[s]
            a["n"] += 1
            a["score_sum"] += score
            if tone == "pos":
                a["pos"] += 1
            elif tone == "neg":
                a["neg"] += 1
            else:
                a["neu"] += 1
            if len(a["samples"]) < 3:
                a["samples"].append(f"[{tone}] {content[:42]}")
    return dict(agg)


def heat_table(agg: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按净情绪分排序的热度表。"""
    rows = []
    for sec, a in agg.items():
        # 净情绪：看涨占比 - 看跌占比（[-1,1]），再乘样本量做置信加权
        net = (a["pos"] - a["neg"]) / a["n"] if a["n"] else 0.0
        rows.append({"sector": sec, "n": a["n"], "pos": a["pos"],
                     "neg": a["neg"], "neu": a["neu"],
                     "net": round(net, 3),
                     "score_sum": round(a["score_sum"], 2),
                     "samples": a["samples"]})
    rows.sort(key=lambda r: (r["net"], r["score_sum"]), reverse=True)
    return rows
