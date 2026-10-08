"""新闻 → 板块 舆情聚合（纯函数，面板与 lab 共用，无 CLI 依赖）。

为什么独立成模块：快讯页（/api/newsfeed）返回带情绪 + stocks 的快讯，
与 sector_news_lab 同源；把聚合能力下沉到这里，app 与 lab 都 import 同一个
实现，不复制板块词库、也不把 lab 的 argparse/__main__ 外壳拖进服务进程。

板块归因优先级（见 aggregate）：先 ticker/公司名 经 stock_sector_map 精确反查；
反查不到（免费源无 stocks、或不在种子表）再回落关键词。关键词法的天花板
（一篇多板块命中、"不看好"被词典误判正面）属数据/模型边界，非代码问题。

范围（ponytail：先描述、后预测）：本模块只做「描述性参考」——把当日快讯聚成
板块情绪热度，并解析每条快讯里的「板块级利空/利好催化剂词」。不做「明天开盘
资金流向预测」：那是方向性预测，需历史新闻归档 + 板块逐日资金流（依赖全市场
股票→板块归属，库里没有），无法在本环境验证。

2026-10 扩展（联网归纳）：
  · 抓取 24h 滚动快讯，量化出 ~68% 未归类（地缘军事/农产品期货/消费品牌/
    医药个股/通信铁塔/宏观金融），回填关键词覆盖主要漏网来源；
  · 新建 SECTOR_BULLISH / SECTOR_BEARISH 板块级利空利好词表（基于公开研报与
    行业共识联网核实），并与全局词合并，接入 aggregate 打分（catalyst 信号）。
"""
from __future__ import annotations
import collections
import re
from typing import Any, Dict, List, Tuple

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
            "疫情", "肺炎", "埃博拉", "痛风", "临床试验", "医保", "药店", "制药",
            "FDA", "CVS", "Lupin", "HUMANA", "Ginkgo",
            "pharma", "drug", "vaccine", "biotech", "healthcare", "medicine",
            "epidemic", "pandemic", "ebola", "pneumonia", "xortx", "fda"],
    "消费": ["白酒", "食品饮料", "消费", "零售", "免税", "家电",
            "苹果", "星巴克", "可口可乐", "沃尔玛", "迪士尼", "iPhone",
            "consumer", "retail", "liquor", "beverage", "grocery",
            "starbucks", "coca-cola", "walmart", "disney", "apple"],
    "军工": ["军工", "国防", "兵器", "航空发动机", "卫星",
            "航母", "空袭", "导弹", "胡塞", "伊朗", "俄乌", "北约", "以色列",
            "五角大楼", "中东", "战机", "军费", "军贸",
            "defense", "military", "aerospace", "weapon"],
    "通信": ["通信", "卫星", "星链", "卫星互联网", "5G", "电信",
            "SpaceX", "Starlink", "telecom", "satellite", "5g",
            "broadband", "network", "星网", "Verizon", "AT&T", "Spectrum",
            "铁塔", "Crown Castle", "American Tower", "光模块", "卫星通信",
            "crown castle", "american tower", "optical module"],
    "金融": ["券商", "银行", "保险", "金融", "信托", "信贷", "信用卡",
            "国债", "美债", "财政部", "联储", "央行", "货币市场",
            "bank", "broker", "insurance", "credit", "fed", "rate", "bond",
            "treasury", "central bank"],
    "地产": ["地产", "房地产", "楼市", "房企", "棚改", "物业",
            "real estate", "property", "housing", "mortgage", "realtor"],
    "汽车": ["汽车", "整车", "智能驾驶", "新能源车", "零部件", "比亚迪",
            "automaker", "electric vehicle", "tesla", "byd", "car sales"],
    "有色": ["有色", "黄金", "金价", "期金", "铜", "铝", "锂矿", "稀土", "钴",
            "gold", "copper", "aluminum", "rare earth", "metal", "mining"],
    "化工": ["化工", "化肥", "化纤", "塑料", "纯碱", "钛白粉",
            "chemical", "petrochemical"],
    "能源": ["煤炭", "电力", "石油", "原油", "油价", "天然气", "火电", "水电", "核电", "霍尔木兹",
            "飓风", "风暴", "钻井平台", "炼油厂", "墨西哥湾", "油气", "页岩油",
            "oil", "coal", "natural gas", "power", "nuclear", "crude"],
    "建材": ["钢铁", "水泥", "玻璃", "螺纹钢", "建材",
            "steel", "cement"],
    "科技AI": ["人工智能", "AI", "算力", "机器人", "大模型", "数据中心", "GPU",
              "artificial intelligence", "robot", "gpu", "datacenter",
              "compute", "cloud", "software", "openai", "codex", "green ai cloud",
              "anthropic", "谷歌云", "google cloud", "Palo Alto", "Anaconda",
              "网络安全", "cybersecurity",
              "量子计算", "英伟达", "微软", "quantum computing", "nvidia", "microsoft"],
    "农业": ["农业", "猪肉", "粮食", "种业", "养殖",
            "玉米", "小麦", "谷物", "CBOT", "生猪", "猪周期", "能繁母猪",
            "agriculture", "grain", "pork", "farm", "corn", "wheat"],
    "中概港股": ["港股", "中概", "恒生", "南向", "互联互通", "金龙指数", "中概股",
               "hang seng", "hk", "china stock", "chinese shares", "adr",
               "golden dragon"],
}


# ───────────────────────── 板块级利空/利好催化剂词 ─────────────────────────
# 来源：公开研报与行业共识（2026-10 联网核实）。用于「解析每个板块的利空利好
# 词语」——在聚合时标注每条快讯命中了哪些板块级催化剂，作为板块情绪的二级信号。
# 中文走子串匹配；英文走词边界匹配。词越具体越好，避免全局误伤。
SECTOR_BULLISH: Dict[str, List[str]] = {
    "军工": ["军费", "军贸", "装备出口", "地缘冲突", "实战检验", "航展", "国防预算",
            "导弹", "无人机", "雷达", "商业航天", "订单落地", "冲突升级", "军备",
            "扩产", "军售"],
    "农业": ["价格上涨", "猪价", "产能去化", "收储", "减产", "粮食安全", "厄尔尼诺",
            "供需紧张", "提价", "上行", "去化", "涨价"],
    "通信": ["算力", "光模块", "光通信", "CPO", "NPO", "5G-A", "6G", "卫星通信",
            "资本开支", "订单", "数据中心", "AI基础设施", "提速", "放量"],
    "医药": ["获批", "FDA批准", "创新药", "医保", "商保", "集采豁免", "BD出海",
            "临床", "定价新政", "出海", "商业化", "里程碑", "突破",
            # 疫情方向：爆发/扩散/失控 → 医药需求上升 = 利好（与词典直觉相反，
            # 词典会把"爆发/失控"判负面，故必须靠板块词表显式纠正）
            "疫情爆发", "疫情扩散", "疫情失控", "疫情蔓延", "疫情反弹", "疫情加重",
            "疫情严峻", "疫情升级", "病毒变异", "传染病爆发", "突发疫情",
            "病例激增", "感染人数上升", "新增病例增加", "确诊病例增加",
            "不明原因肺炎", "人传人", "传播风险", "公共卫生事件", "防疫升级",
            "疫情防控升级", "检测需求", "检测试剂", "疫苗研发", "抗病毒药物"],
    "新能源": ["装机", "中标", "订单", "出口", "渗透率", "涨价", "扩容", "政策",
              "投产", "放量"],
    "半导体": ["国产替代", "涨价", "订单", "扩产", "突破", "算力", "景气", "出货",
              "自主可控"],
    "科技AI": ["算力", "资本开支", "大模型", "订单", "云", "突破", "合作", "扩产",
              "芯片", "AI基础设施"],
    "金融": ["降息", "降准", "宽松", "成交量放大", "放水", "牛市", "上涨", "业绩超预期"],
    "地产": ["放松", "政策", "回暖", "销售改善", "降息", "去库存", "企稳", "企稳回升"],
    "汽车": ["出海", "销量", "增长", "出口", "智能化", "订单", "放量", "突破"],
    "有色": ["降息", "美元走弱", "涨价", "需求", "短缺", "库存下降", "上行"],
    "化工": ["涨价", "价差修复", "油价高位", "投产", "需求", "上行"],
    "能源": ["减产", "OPEC减产", "霍尔木兹", "地缘", "油价上涨", "供给受阻",
            "库存下降", "断供", "供应中断"],
    "建材": ["地产复苏", "基建", "需求", "涨价", "稳增长", "回暖"],
    "消费": ["提价", "复苏", "放量", "回暖", "增长", "签约", "扩产", "涨价"],
    "中概港股": ["南向资金", "回购", "AI", "政策", "上涨", "估值修复", "出海", "业绩超预期"],
}

SECTOR_BEARISH: Dict[str, List[str]] = {
    "军工": ["局势缓和", "订单不及预期", "列装不及预期", "获利了结", "抽血效应",
            "军品降价", "谈判", "收缩", "回调"],
    "农业": ["价格下跌", "产能过剩", "探底", "供给充足", "供给宽松", "需求疲软",
            "降价", "下行", "累库"],
    "通信": ["FCC限制", "禁令", "降价", "贸易限制", "制裁", "限制", "出口管制",
            "价格回落", "需求不及预期", "竞争加剧", "砍单"],
    "医药": ["集采", "降价", "砍价", "临床失败", "不及预期", "反腐", "内卷",
            "专利悬崖", "调查", "违约",
            # 疫情方向：得到控制/缓解 → 医药需求回落 = 利空（同样与直觉相反）
            "疫情得到控制", "疫情已控制", "疫情可控", "疫情缓解", "疫情消退",
            "疫情好转", "疫情趋缓", "疫情结束", "疫情受控", "疫情平稳",
            "病例下降", "新增病例减少", "感染人数下降", "解除防控", "防控降级",
            "应急响应终止", "风险等级下调", "疫情基本结束"],
    "新能源": ["价格战", "内卷", "降价", "产能过剩", "补贴退坡", "减值", "砍单"],
    "半导体": ["制裁", "出口管制", "降价", "砍单", "库存", "需求疲软", "周期下行",
              "限制"],
    "科技AI": ["制裁", "限制", "出口管制", "砍单", "降价", "竞争加剧", "估值高位",
              "回调", "禁令"],
    "金融": ["加息", "收紧", "息差", "坏账", "违约", "下跌", "熊市", "缩表"],
    "地产": ["下滑", "违约", "停工", "调控", "降价", "库存高", "下行"],
    "汽车": ["价格战", "内卷", "降价", "需求疲软", "关税", "销量下滑", "制裁"],
    "有色": ["美元走强", "加息", "需求疲软", "累库", "降价", "下行"],
    "化工": ["降价", "成本上升", "需求疲软", "产能过剩", "下行"],
    "能源": ["增产", "需求疲软", "地缘缓和", "油价下跌", "释放储备", "累库"],
    "建材": ["供需双弱", "需求疲软", "价格下跌", "库存高", "下行"],
    "消费": ["降价", "需求疲软", "下滑", "内卷", "下行"],
    "中概港股": ["地缘", "利率", "加息", "退市", "监管", "下跌", "制裁", "收紧"],
}

# 全局催化剂（跨板块通用，方向明确）。注意：不放"减产/停产"这类行业专属动作词——
# 它们在能源是利好、在农业/其他是利空，方向由板块定义，放全局会自相矛盾
# （曾踩：OPEC减产同时命中能源利好与全局利空，cat_net 互相抵消）。
GLOBAL_BULLISH: List[str] = ["利好", "大涨", "提振", "复苏", "超预期",
                            "中标", "签约", "获批", "扩产", "提价", "宽松",
                            "降准", "降息", "订单落地", "放量", "高增长"]
GLOBAL_BEARISH: List[str] = ["利空", "大跌", "下调", "制裁", "调查", "诉讼",
                            "违约", "收紧", "不及预期", "承压", "回调", "内卷",
                            "砍单", "下挫", "跳水"]


def _match_terms(content: str, terms: List[str]) -> List[str]:
    """在文本中匹配词表，返回命中项（保持词表顺序）。中文子串 / 英文词边界。"""
    text = content or ""
    low = text.lower()
    hit = []
    for t in terms:
        if not t:
            continue
        if t.isascii():
            if re.search(r"(?<![a-z])" + re.escape(t.lower()) + r"(?![a-z])", low):
                hit.append(t)
        elif t in text:
            hit.append(t)
    return hit


def detect_catalysts(content: str, sectors: List[str] = None) -> Dict[str, Any]:
    """解析一条快讯命中的利空/利好催化剂词。

    返回 {"bull","bear","sec_bull","sec_bear","sec_net"}：
      · bull/bear = 全局词 + 板块词合并（展示用）
      · sec_bull/sec_bear = 仅板块专属词；sec_net = 板块专属净信号，
        用于修正词典情绪——板块词的方向由业务定义，可能与词典直觉相反
        （如「疫情爆发」词典判负面、但对医药是利好；「疫情得到控制」反之）。
        全局词与词典同向，不参与修正，否则会把方向搞乱。
    """
    sectors = sectors or []
    bull = _match_terms(content, GLOBAL_BULLISH)
    bear = _match_terms(content, GLOBAL_BEARISH)
    sec_bull, sec_bear = [], []
    for s in sectors:
        b = _match_terms(content, SECTOR_BULLISH.get(s, []))
        d = _match_terms(content, SECTOR_BEARISH.get(s, []))
        sec_bull += b
        sec_bear += d
        bull += b
        bear += d
    return {"bull": bull, "bear": bear,
            "sec_bull": sec_bull, "sec_bear": sec_bear,
            "sec_net": len(sec_bull) - len(sec_bear)}


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
    """把快讯聚到板块。返回 {板块: {n,pos,neg,neu,net,score_sum,bull,bear,
    cat_net,samples,catalyst_samples}}。

    板块归因优先级：先 ticker/公司名 经 stock_sector_map 反查（精确）；
    反查不到（OkSurf/FreeNews 无 stocks、或 ticker 不在种子表）再回落关键词。

    cat_net（催化剂净信号）= 该板块命中的利好词数 - 利空词数（跨窗口累加），
    作为「词典情绪 tone」之外的二级信号：tone 看的是涨跌措辞，catalyst 看的是
    事件性质（如 OPEC减产=能源利好、集采=医药利空）。
    """
    agg: Dict[str, Dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "pos": 0, "neg": 0, "neu": 0,
                 "score_sum": 0.0, "bull": 0, "bear": 0,
                 "samples": [], "catalyst_samples": []})
    for it in items:
        content = it.get("content", "") or ""
        tone = (it.get("sentiment") or {}).get("tone", "neutral")
        score = float((it.get("sentiment") or {}).get("score", 0.0) or 0.0)
        # ponytail: ticker 反查优先，关键词作回落——避免一篇多板块的噪声放大
        secs = _ssm.sectors_for_stocks(it.get("stocks")) or map_sectors(content)
        if not secs:
            continue
        cats = detect_catalysts(content, secs)
        bull_n, bear_n = len(cats["bull"]), len(cats["bear"])
        sec_net = cats["sec_net"]
        for s in secs:
            a = agg[s]
            a["n"] += 1
            a["score_sum"] += score
            a["bull"] += bull_n
            a["bear"] += bear_n
            # ponytail: 板块专属词方向由业务定义，可能与词典直觉相反
            # （「疫情爆发」→医药利好，词典却判负面）。命中板块词时以板块词
            # 方向为准修正本条对该板块的贡献，避免舆情读反。
            eff = tone
            if sec_net > 0:
                eff = "pos"
            elif sec_net < 0:
                eff = "neg"
            if eff == "pos":
                a["pos"] += 1
            elif eff == "neg":
                a["neg"] += 1
            else:
                a["neu"] += 1
            # 被板块词修正过方向的，样本加 * 标记（便于回看"为什么读反了"）
            flag = "*" if (sec_net and eff != tone) else ""
            if len(a["samples"]) < 3:
                a["samples"].append(f"[{eff}{flag}] {content[:56]}")
            # 催化剂样本：只保留「有利好或利空命中」且样本未满的，便于回看触发词
            if (bull_n or bear_n) and len(a["catalyst_samples"]) < 3:
                tag = "↑" + "/".join(cats["bull"]) if cats["bull"] else ""
                if cats["bear"]:
                    tag += ("↓" + "/".join(cats["bear"]))
                a["catalyst_samples"].append(f"{tag} | {content[:48]}")
    # 逐板块补 cat_net（利好词数 - 利空词数），与 heat_table 口径一致
    for a in agg.values():
        a["cat_net"] = a["bull"] - a["bear"]
    return dict(agg)


def heat_table(agg: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按净情绪分排序的热度表（含催化剂信号列）。"""
    rows = []
    for sec, a in agg.items():
        # 净情绪：看涨占比 - 看跌占比（[-1,1]），再乘样本量做置信加权
        net = (a["pos"] - a["neg"]) / a["n"] if a["n"] else 0.0
        cat_net = a["bull"] - a["bear"]
        rows.append({"sector": sec, "n": a["n"], "pos": a["pos"],
                     "neg": a["neg"], "neu": a["neu"],
                     "net": round(net, 3),
                     "score_sum": round(a["score_sum"], 2),
                     "bull": a["bull"], "bear": a["bear"],
                     "cat_net": cat_net,
                     "samples": a["samples"],
                     "catalyst_samples": a["catalyst_samples"]})
    rows.sort(key=lambda r: (r["net"], r["cat_net"], r["score_sum"]), reverse=True)
    return rows


def catalyst_lexicon() -> List[Dict[str, Any]]:
    """板块级利空利好词表（解析报告数据源）。返回每个板块的利好/利空词清单。"""
    out = []
    for sec in SECTOR_KEYWORDS:
        out.append({
            "sector": sec,
            "bullish": SECTOR_BULLISH.get(sec, []),
            "bearish": SECTOR_BEARISH.get(sec, []),
        })
    return out


def selfcheck() -> int:
    """离线自检（不碰网络/不碰文件）。返回失败条数。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    # 1) 关键词扩展命中（未归类来源回填）
    ok("军工 命中 '伊朗空袭'", "伊朗" in "五角大楼拟定新的对伊朗打击方案" and
       "军工" in map_sectors("五角大楼拟定新的对伊朗打击方案"))
    ok("农业 命中 'CBOT玉米期货'", "农业" in map_sectors("CBOT玉米期货跌0.45%"))
    ok("消费 命中 '星巴克'", "消费" in map_sectors("星巴克表示全神贯注于现有战略"))
    ok("消费 命中 '苹果'", "消费" in map_sectors("苹果计划推出触屏Mac"))
    ok("医药 命中 'FDA批准'", "医药" in map_sectors("鲁宾制药获得美国FDA批准"))
    ok("医药 命中 'CVS HEALTH'", "医药" in map_sectors("CVS HEALTH股价盘后下跌"))
    ok("通信 命中 'American Tower'", "通信" in map_sectors("American Tower盘后上涨"))
    ok("通信 命中 '铁塔'", "通信" in map_sectors("通信铁塔运营商资本开支上行"))
    ok("科技AI 命中 'anthropic'", "科技AI" in map_sectors("Anthropic推出网络安全计划"))
    ok("科技AI 命中 '谷歌云'", "科技AI" in map_sectors("谷歌云存储延迟上升"))
    ok("金融 命中 '美债'", "金融" in map_sectors("美国10年期国债收益率上涨"))
    ok("中概港股 命中 '金龙指数'", "中概港股" in map_sectors("纳斯达克中国金龙指数收跌"))

    # 2) 催化剂解析
    c1 = detect_catalysts("OPEC+减产，霍尔木兹海峡受阻推升油价", ["能源"])
    ok("能源 催化剂：减产/霍尔木兹=利好", "减产" in c1["bull"] and "霍尔木兹" in c1["bull"])
    c2 = detect_catalysts("第十二批集采启动，仿制药面临降价", ["医药"])
    ok("医药 催化剂：集采/降价=利空", "集采" in c2["bear"] and "降价" in c2["bear"])
    c3 = detect_catalysts("美军拟对伊朗发动空袭，军贸订单落地", ["军工"])
    ok("军工 催化剂：军贸=利好", "军贸" in c3["bull"])
    c4 = detect_catalysts("FCC拟限制中国光模块出口", ["通信"])
    ok("通信 催化剂：限制=利空", "限制" in c4["bear"])
    c5 = detect_catalysts("玉米因厄尔尼诺减产，粮价上行", ["农业"])
    ok("农业 催化剂：减产/上行=利好", "减产" in c5["bull"] and "上行" in c5["bull"])

    # 疫情方向与词典直觉相反：爆发/扩散/失控→医药利好；得到控制→医药利空
    c6 = detect_catalysts("某地疫情爆发，已出现失控迹象", ["医药"])
    ok("医药 疫情爆发→利好", c6["sec_net"] > 0 and "疫情爆发" in c6["sec_bull"])
    c7 = detect_catalysts("疫情扩散，感染人数持续上升", ["医药"])
    ok("医药 疫情扩散→利好", c7["sec_net"] > 0)
    c8 = detect_catalysts("疫情已得到控制，新增病例下降", ["医药"])
    ok("医药 疫情受控→利空", c8["sec_net"] < 0 and "病例下降" in c8["sec_bear"])
    c9 = detect_catalysts("疫情可控，应急响应终止", ["医药"])
    ok("医药 疫情可控→利空", c9["sec_net"] < 0)
    # 板块词方向应覆盖词典情绪（词典会把"爆发/失控"判正面，这里须读成利好/或至少不被反向）
    _fake = [{"content": "疫情扩散，感染人数持续上升",
              "sentiment": {"tone": "neg", "score": -2.0}, "stocks": []}]
    ok("板块词修正词典情绪：疫情扩散(词典neg)→计为偏多",
       aggregate(_fake)["医药"]["pos"] == 1)

    # 3) aggregate 产出催化剂列
    fake = [
        {"content": "OPEC+减产 霍尔木兹受阻 油价大涨", "sentiment": {"tone": "pos", "score": 1.0}, "stocks": []},
        {"content": "第十二批集采 仿制药降价", "sentiment": {"tone": "neg", "score": -1.0}, "stocks": []},
    ]
    agg = aggregate(fake)
    ok("aggregate 含 能源.cat_net>0", agg.get("能源", {}).get("cat_net", 0) > 0)
    ok("aggregate 含 医药.cat_net<0", agg.get("医药", {}).get("cat_net", 0) < 0)
    ok("heat_table 含 catalyst_samples", bool(heat_table(agg)[0].get("catalyst_samples")))

    # 4) lexicon 覆盖所有板块
    lex = {x["sector"] for x in catalyst_lexicon()}
    ok("catalyst_lexicon 覆盖全部板块", lex == set(SECTOR_KEYWORDS.keys()))

    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
