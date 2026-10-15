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

2026-10 扩展层双轨（#189 对账 + AI 优化闭环 P1）：SECTOR_BULLISH/BEARISH 为
基础层（selfcheck 锁死、不可运行期删改）；data/sector_lexicon_extra.json 为
扩展层（运行期增删、反极性覆盖基础层同名词），是 sentiment_optimizer 的 AI
优化建议唯一允许的写入口——AI 只写扩展层，动不了基础层，改错可一键回滚。
"""
from __future__ import annotations
import collections
import json
import os
import re
from typing import Any, Dict, List, Tuple

import stock_sector_map as _ssm

# 板块 → 关键词（覆盖主要 A 股板块；一条新闻可命中多个板块）
# 中文走子串匹配；英文别名走词边界匹配（见 map_sectors）。
# 英文别名用于接入 OkSurf（免费免密钥的 Google News 源，标题为英文）。
SECTOR_KEYWORDS: Dict[str, List[str]] = {
    "新能源": ["新能源", "光伏", "锂电", "储能", "逆变器", "风电", "氢能",
              "充电中心", "充换电", "超充站", "充电桩", "充电站",
              "solar", "lithium", "ev", "battery", "renewable", "energy storage",
              "hydrogen", "photovoltaic", "wind power", "charging"],
    "半导体": ["半导体", "芯片", "集成电路", "晶圆", "光刻", "封测",
              "美光", "高通", "安谋", "ARM", "存储芯片", "中芯国际",
              "韦尔股份", "北方华创", "寒武纪",
              "semiconductor", "chip", "wafer", "foundry", "fab"],
    "医药": ["医药", "创新药", "生物制药", "医疗", "CXO", "疫苗", "中药",
            "疫情", "肺炎", "埃博拉", "痛风", "临床试验", "医保", "药店", "制药",
            # 新发/不明病原体：用组合词而非裸"病毒"，避免"勒索病毒"(网络安全)
            # 误归医药。裸"病毒"只在板内催化剂方向里按医药语境使用。
            "不明病毒", "不明细菌", "不明病原", "新型病毒", "新病毒", "超级细菌",
            "耐药菌", "病毒感染", "病毒传播", "病毒泄露", "毒株泄露", "传染病",
            "病原体", "变异株", "后遗症",
            "FDA", "CVS", "Lupin", "HUMANA", "Ginkgo",
            "pharma", "drug", "vaccine", "biotech", "healthcare", "medicine",
            "epidemic", "pandemic", "ebola", "pneumonia", "xortx", "fda"],
    "消费": ["白酒", "食品饮料", "消费", "零售", "免税", "家电",
            "苹果", "星巴克", "可口可乐", "沃尔玛", "迪士尼", "iPhone",
            "consumer", "retail", "liquor", "beverage", "grocery",
            "starbucks", "coca-cola", "walmart", "disney", "apple"],
    # 举一反三（2026-10-13 24h 回溯修正）：军工归类只认产业词，不收地缘实体。
    # 旧版收了伊朗/俄乌/以色列/胡塞/普京/基辅… 结果 24h 内 288 条军工命中里
    # 128 条纯由地缘词触发（连"亚马逊收购以色列工程师创办的 AI 公司"都因
    # "以色列"进军工）。外交辞令与别国冲突对 A 股军工无稳定映射，只会灌水。
    "军工": ["军工", "国防", "兵器", "航空发动机", "卫星",
            "航母", "导弹", "战机", "军费", "军贸", "军演",
            "弹药", "军火", "武器援助", "防空",
            "defense", "military", "aerospace", "weapon"],
    # 举一反三（2026-10-13 修正）：删 "network" —— "neural network"（神经网络）
    # 是科技AI不是通信，词边界匹配挡不住这种复合词；删 "Spectrum" —— 大小写
    # 不敏感会误中普通词 spectrum（范围）。
    "通信": ["通信", "卫星", "星链", "卫星互联网", "5G", "电信",
            "SpaceX", "Starlink", "telecom", "satellite", "5g",
            "broadband", "星网", "Verizon", "AT&T",
            "铁塔", "Crown Castle", "American Tower", "光模块", "卫星通信",
            "crown castle", "american tower", "optical module"],
    "金融": ["券商", "银行", "保险", "金融", "信托", "信贷", "信用卡",
            "美元指数", "欧元/美元", "英镑/美元", "美元/日元", "汇率",
            "国债", "美债", "财政部", "联储", "央行", "货币市场",
            "反洗钱", "反洗钱缺陷", "监管处罚", "合规缺陷", "罚单",
            # 举一反三（2026-10-13 修正）：删英文裸词 "rate" —— "at any rate"
            # （无论如何）会被词边界匹配误中金融；利率义留给 降息/加息/央行 等
            # 中文词与 fed/central bank。
            "bank", "broker", "insurance", "credit", "fed", "bond",
            "treasury", "central bank", "anti-money laundering", "aml"],
    "地产": ["地产", "房地产", "楼市", "房企", "棚改", "物业",
            "real estate", "property", "housing", "mortgage", "realtor"],
    "汽车": ["汽车", "整车", "智能驾驶", "新能源车", "零部件", "比亚迪",
            "automaker", "electric vehicle", "tesla", "byd", "car sales"],
    "有色": ["有色", "黄金", "金价", "期金", "现货黄金", "伦敦金", "comex黄金", "现货白银", "现货钯金", "现货铂金",
            "铜", "铝", "锂矿", "稀土", "钴",
            "淡水河谷", "铁矿石", "电解铝", "氧化铝",
            "gold", "silver", "copper", "aluminum", "rare earth", "metal", "mining"],
    "化工": ["化工", "化肥", "化纤", "塑料", "纯碱", "钛白粉",
            "有机硅", "硅料", "硅宝", "氟化工", "磷化工", "草甘膦",
            "chemical", "petrochemical"],
    # 举一反三（2026-10-13 修正）：删 伊朗/中东/也门/霍尔木兹/五角大楼/空袭/航母
    # 等地缘实体——旧版 24h 内 227 条能源命中 87 条纯由地缘词触发，"伊朗外长
    # 通话沙特"这类外交辞令全进能源。保留商品与设施本体词（油轮/炼油厂/海峡）。
    "能源": ["煤炭", "电力", "石油", "原油", "布伦特原油", "WTI原油", "美原油", "油价", "天然气", "火电", "水电", "核电",
            "飓风", "风暴", "钻井平台", "炼油厂", "墨西哥湾", "油气", "页岩油",
            "油轮", "航线", "航运", "海峡", "输油管道", "港口",
            "柴油", "汽油", "成品油", "燃油", "燃料油", "汽柴油", "炼油", "油轮禁运",
            "oil", "coal", "natural gas", "power", "nuclear", "crude",
            "tanker", "shipping", "straits"],
    "建材": ["钢铁", "水泥", "玻璃", "螺纹钢", "建材",
            "steel", "cement"],
    "科技AI": ["人工智能", "AI", "算力", "机器人", "大模型", "数据中心", "GPU",
              "artificial intelligence", "robot", "gpu", "datacenter",
              "compute", "cloud", "software", "openai", "codex", "green ai cloud",
              "anthropic", "谷歌云", "google cloud", "Palo Alto", "Anaconda",
              "网络安全", "cybersecurity",
              "量子计算", "英伟达", "微软", "quantum computing", "nvidia", "microsoft",
              # 举一反三：AI伦理/治理/权益类新闻（如"禁止虐待AI"）= 科技AI，绝非医药；
              # 注意"虐待/泄露/幻觉/成瘾"等医学感官词本身不进医药归因，靠 AI 上下文词归类
              "AI伦理", "AI安全", "AI权益", "AI福利", "AI监管", "AI法案",
              "AI使用政策", "聊天机器人", "AI助手", "智能体",
              "chatbot", "Claude", "claude", "Gemini", "gemini", "GPT"],
    "农业": ["农业", "猪肉", "粮食", "种业", "养殖",
            "玉米", "小麦", "谷物", "CBOT", "生猪", "猪周期", "能繁母猪",
            "旱灾", "干旱", "洪涝", "水灾", "台风", "虫灾", "病虫害", "蝗灾",
            "霜冻", "极端天气", "歉收", "粮食危机",
            "agriculture", "grain", "pork", "farm", "corn", "wheat", "drought", "flood"],
    "中概港股": ["港股", "中概", "恒生", "南向", "互联互通", "金龙指数", "中概股",
               "hang seng", "hk", "china stock", "chinese shares", "adr",
               "golden dragon"],
}


# ───────────────────────── 板块级利空/利好催化剂词 ─────────────────────────
# 来源：公开研报与行业共识（2026-10 联网核实）。用于「解析每个板块的利空利好
# 词语」——在聚合时标注每条快讯命中了哪些板块级催化剂，作为板块情绪的二级信号。
# 中文走子串匹配；英文走词边界匹配。词越具体越好，避免全局误伤。
SECTOR_BULLISH: Dict[str, List[str]] = {
    # 举一反三（2026-10-13 修正）：只认产业基本面（军费/军贸/订单/装备/预算），
    # 不收战争本体词。旧版把"战争/交火/空袭/遭袭/袭击/军事打击"当利好，任何
    # 别国冲突（油轮遇袭、炼油厂被炸）都刷成军工利好；对 A 股军工而言，
    # "俄军袭击乌克兰数据中心"这类既非订单也非预算，方向应为中性。
    # 再删"导弹/无人机/雷达/商业航天"：品类词不是催化剂——"伊朗导弹瞄准航母"
    # "沙特拦截弹道导弹""航天智装芯片应用于卫星"都曾被刷成军工利好（24h 实证）。
    "军工": ["军费", "军贸", "装备出口", "实战检验", "航展", "国防预算",
            "订单落地", "军备",
            "扩产", "军售",
            "军演", "实弹演习", "联合军演",
            "武器援助", "军援", "军火", "弹药",
            "军备竞赛", "防空", "防空系统"],
    "农业": ["价格上涨", "猪价", "产能去化", "收储", "减产", "粮食安全", "厄尔尼诺",
            "供需紧张", "提价", "上行", "去化", "涨价",
            # 反向直觉：旱涝/虫灾/台风/极端天气 词典判"灾害负面"，但
            # 农作物减产→供给收紧→粮价上行=农业(种植/种业)利好
            "旱灾", "干旱", "洪涝", "水灾", "台风", "虫灾", "病虫害", "蝗灾",
            "霜冻", "低温冻害", "极端天气", "粮食减产", "粮食危机", "歉收",
            "减产预期", "受灾"],
    # 举一反三（2026-10-13 二轮修正）：删"算力/数据中心/AI基础设施/光模块/光通信"
    # ——全是品类词，新闻提到品类≠利好。"美股光通信股开盘普跌"曾因"光通信"
    # 被判通信利好（方向都反了）；"智算中心竣工"是基建进度非板块催化。
    # 保留资本开支/订单等事件词与技术标准词（5G-A/6G/CPO 等）。
    "通信": ["CPO", "NPO", "5G-A", "6G", "卫星通信",
            "资本开支", "订单", "提速"],
    "医药": ["获批", "FDA批准", "创新药", "医保", "商保", "集采豁免", "BD出海",
            "临床", "定价新政", "出海", "商业化", "里程碑",
            # 疫情方向：爆发/扩散/失控 → 医药需求上升 = 利好（与词典直觉相反，
            # 词典会把"爆发/失控"判负面，故必须靠板块词表显式纠正）
            "疫情爆发", "疫情扩散", "疫情失控", "疫情蔓延", "疫情反弹", "疫情加重",
            "疫情严峻", "疫情升级", "病毒变异", "传染病爆发", "突发疫情",
            "病例激增", "感染人数上升", "新增病例增加", "确诊病例增加",
            "不明原因肺炎", "人传人", "传播风险", "公共卫生事件", "防疫升级",
            "疫情防控升级", "检测需求", "检测试剂", "疫苗研发", "抗病毒药物",
            # 不明病原/新发传染病：不明病毒、不明细菌、不明肺炎 → 排查与检测需求暴增 = 利好
            "不明病毒", "不明细菌", "不明病原", "不明病原体", "不明肺炎",
            "新型病毒", "新病毒", "变异病毒", "变异毒株", "未知病原体",
            # 泄露/超级细菌：实验室病毒泄露、毒株泄露、超级细菌蔓延 → 防控与用药需求上升 = 利好
            "病毒泄露", "毒株泄露", "实验室泄露", "细菌泄露", "实验室病毒泄露",
            "P3泄露", "P4泄露", "超级细菌", "超级耐药菌", "耐药菌蔓延", "耐药菌",
            "院内感染", "院感暴发",
            # 举一反三（刚果金埃博拉）：疫情严峻+跨境传播风险上升 → 检测/用药/防控需求=利好
            "埃博拉", "跨境传播", "病例上升", "确诊上升", "活跃传播", "输入病例",
            "死亡病例上升", "疫情依然严峻", "卫生区活跃传播"],
    # 举一反三（2026-10-13 修正）：删 "增长" —— 子串误中「同比下降」（千里科技
    # 整车产量同比下降52.7% 曾因"增长"被判汽车利好）；删 "签约"（泛）。
    # 二轮删"放量"（股价结果，同汽车/化工）。
    "新能源": ["装机", "中标", "订单", "出口", "渗透率", "涨价", "扩容",
              "投产", "充电中心", "充换电", "超充站", "充电桩建设"],
    # 二轮删"突破"（泛，"市值突破/跌破"都含它）、"算力"（品类词）。
    "半导体": ["国产替代", "涨价", "订单", "扩产", "景气", "出货",
              "自主可控"],
    # 举一反三（2026-10-13 修正）：删 "突破/合作/推出/发布/升级" —— 全是泛词。
    # "俄方发布通报打击乌数据中心"曾因"发布"判科技AI利好；保留产业硬指标词。
    # 二轮再删"芯片/云/大模型/AI基础设施/算力/落地"：同为品类词或泛词——
    # "AI债务发行规模跌超50%"曾因"芯片"判利好（neg→pos，方向反了）、
    # "云知声成立公司"因"云"、"梁文锋考虑进具身智能"因"大模型"被判利好。
    # 品类词归归类词表，催化剂只留事件/政策类。
    "科技AI": ["资本开支", "订单", "扩产", "特别工作组",
              "人工智能计划", "政策扶持"],
    # 举一反三（2026-10-13 修正）：删 "上涨" —— 涨跌措辞是词典 tone 的职责，
    # 板块专属词只放事件/政策类，否则同一信号判两遍、口径迟早不一致。
    "金融": ["降息", "降准", "宽松", "成交量放大", "放水", "牛市", "业绩超预期"],
    # 举一反三（2026-10-13 修正）：删 "政策" —— "调控政策"是利空但含"政策"，
    # 利好侧保留更精确的"放松/销售改善/去库存"。
    "地产": ["放松", "回暖", "销售改善", "降息", "去库存", "企稳", "企稳回升"],
    # 举一反三（2026-10-13 修正）：删 "增长"（同比下降子串误中，同新能源）。
    # 二轮再删"突破/销量/智能化/放量"："江淮市值跌破500亿 成交额突破25亿"
    # 曾因"突破"判利好（neg→pos）；"本田中国销量同比下降53.1%"因"销量"判利好；
    # "智能化"是品类词；"放量"是股价结果非产业催化（同化工上轮清理）。
    "汽车": ["出海", "出口", "订单"],
    # 举一反三（2026-10-13 修正）：删 "需求" —— "需求不及预期/需求疲软"是利空，
    # 裸"需求"会把利空新闻刷成利好；保留价格与库存类硬词。
    "有色": ["降息", "美元走弱", "涨价", "短缺", "库存下降", "上行"],
    # 举一反三（2026-10-13 修正）：删 "需求"（同上）、"拉升/异动/板块走强/涨停"
    # —— 后四个是股价结果不是产业催化，与词典 tone 重复。
    "化工": ["涨价", "价差修复", "油价高位", "投产", "上行",
            "景气修复"],
    # 举一反三（2026-10-13 修正）：删 "霍尔木兹/地缘/空袭/航母/对伊打击/封锁"
    # ——"霍尔木兹"一提就利好太敏感（伊朗外长谈海峡通行=利好原油？），"地缘"更泛。
    # 保留明确供给中断事件词（爆炸/油轮袭击/航运受阻/海峡封锁/制裁伊朗/油轮禁运）。
    "能源": ["减产", "OPEC减产", "油价上涨", "供给受阻",
            "库存下降", "断供", "供应中断",
            # 举一反三：墨西哥湾飓风/事故致油气停产 → 供给收缩=利好原油天然气
            "停产", "关停", "停产检修", "供应下降",
            # 举一反三：航线爆炸、油轮袭击 → 供给中断风险溢价=利好原油
            "爆炸", "猛烈爆炸", "油轮", "油轮袭击", "航运受阻", "航线中断",
            "港口袭击", "海峡封锁", "航道受阻", "输油管道中断",
            # 举一反三（中东军事升级）：对伊制裁/封锁致原油装卸停止 → 供给中断
            "制裁伊朗", "对伊制裁",
            "油轮禁运", "原油装卸停止", "停止原油装卸", "中东局势升级",
            "海湾局势", "中东紧张"],
    # 举一反三（2026-10-13 修正）：删裸 "需求"（"需求不及预期"子串误中）。
    "建材": ["地产复苏", "基建", "涨价", "稳增长", "回暖"],
    # 举一反三（2026-10-13 修正）：删 "增长"（同比下降子串误中）、"签约"（泛）。
    # "网球公开赛数据创新高"曾因"增长"判消费利好——体育赛事不是消费板块催化。
    # 二轮删"放量"（股价结果，同汽车/化工）。
    "消费": ["提价", "复苏", "回暖", "扩产", "涨价"],
    # 举一反三（2026-10-13 修正）：删 "AI"（提到AI就利好中概太泛）、"上涨"
    # （措辞类，归词典 tone）；保留南向资金/回购/估值修复等真信号。
    # 二轮删"政策"——"调控政策/监管政策"是利空但含"政策"（同地产/新能源上轮清理）。
    "中概港股": ["南向资金", "回购", "估值修复", "出海", "业绩超预期"],
}

SECTOR_BEARISH: Dict[str, List[str]] = {
    "军工": ["局势缓和", "订单不及预期", "列装不及预期", "获利了结", "抽血效应",
            "军品降价", "谈判", "收缩", "回调"],
    # 二轮删"探底"：泛词，"探底回升"是利好语境，A股午评类快讯会被误判利空
    # （24h 实证：午评提到农业股探底，pos→neg）。
    "农业": ["价格下跌", "产能过剩", "供给充足", "供给宽松", "需求疲软",
            "降价", "下行", "累库"],
    "通信": ["FCC限制", "禁令", "降价", "贸易限制", "制裁", "限制", "出口管制",
            "价格回落", "需求不及预期", "竞争加剧", "砍单"],
    "医药": ["集采", "降价", "砍价", "临床失败", "不及预期", "反腐", "内卷",
            "专利悬崖", "调查", "违约",
            # 疫情方向：得到控制/缓解 → 医药需求回落 = 利空（同样与直觉相反）
            "疫情得到控制", "疫情已控制", "疫情可控", "疫情缓解", "疫情消退",
            "疫情好转", "疫情趋缓", "疫情结束", "疫情受控", "疫情平稳",
            "病例下降", "新增病例减少", "感染人数下降", "解除防控", "防控降级",
            "应急响应终止", "风险等级下调", "疫情基本结束",
            # 疫苗后遗症：接种后出现后遗症/不良反应 → 疫苗信任与需求回落 = 利空医药
            "疫苗后遗症", "接种后遗症", "疫苗不良反应", "疫苗后并发症", "疫苗副作用",
            "接种异常反应", "后遗症"],
    "新能源": ["价格战", "内卷", "降价", "产能过剩", "补贴退坡", "减值", "砍单"],
    "半导体": ["制裁", "出口管制", "降价", "砍单", "库存", "需求疲软", "周期下行",
              "限制"],
    "科技AI": ["制裁", "限制", "出口管制", "砍单", "降价", "竞争加剧", "估值高位",
              "回调", "禁令"],
    # 举一反三（2026-10-13 修正）：删 "下跌"（措辞类归词典 tone，同金融"上涨"）。
    "金融": ["加息", "收紧", "息差", "坏账", "违约", "熊市", "缩表",
            # 举一反三：金融机构因反洗钱/合规缺陷被监管重罚 → 板块监管风险重定价=利空
            "被罚", "罚单", "罚款", "反洗钱", "反洗钱缺陷", "监管处罚", "合规缺陷",
            "没收违法所得", "监管罚款", "处罚决定书", "天价罚单"],
    "地产": ["下滑", "违约", "停工", "调控", "降价", "库存高", "下行"],
    "汽车": ["价格战", "内卷", "降价", "需求疲软", "关税", "销量下滑", "制裁"],
    "有色": ["美元走强", "加息", "需求疲软", "累库", "降价", "下行"],
    "化工": ["降价", "成本上升", "需求疲软", "产能过剩", "下行"],
    "能源": ["增产", "需求疲软", "地缘缓和", "油价下跌", "释放储备", "累库"],
    "建材": ["供需双弱", "需求疲软", "价格下跌", "库存高", "下行"],
    "消费": ["降价", "需求疲软", "下滑", "内卷", "下行"],
    # 举一反三（2026-10-13 修正）：删 "地缘"（泛，"地缘风险上升"才是利空）、
    # "利率"（"利率决议"中性）、"下跌"（措辞类归词典 tone）。
    "中概港股": ["加息", "退市", "监管", "制裁", "收紧"],
}

# 全局催化剂（跨板块通用，方向明确）。注意：不放"减产/停产"这类行业专属动作词——
# 它们在能源是利好、在农业/其他是利空，方向由板块定义，放全局会自相矛盾
# （曾踩：OPEC减产同时命中能源利好与全局利空，cat_net 互相抵消）。
# 二轮删"签约"（"二手房签约量创十二年最低"是利空却含"签约"——签约量是指标
# 不是事件）、"放量"（股价结果，同各板块清理）。
GLOBAL_BULLISH: List[str] = ["利好", "大涨", "提振", "复苏", "超预期",
                            "中标", "获批", "扩产", "提价", "宽松",
                            "降准", "降息", "订单落地", "高增长"]
GLOBAL_BEARISH: List[str] = ["利空", "大跌", "下调", "制裁", "调查", "诉讼",
                            "违约", "收紧", "不及预期", "承压", "回调", "内卷",
                            "砍单", "下挫", "跳水"]


# ───────────────────────── 扩展层（AI 优化建议落点，双轨制） ─────────────────────────
# ponytail: 板块词表走「硬编码基础层 + data/sector_lexicon_extra.json 扩展层」双轨
# （照 sentiment.py 模式）。基础层由 selfcheck 锁死；扩展层运行期增删、可回滚，
# 是 AI 舆情优化器（sentiment_optimizer）唯一允许的写入口。
# 覆盖语义：扩展层词按极性「反噬」基础层同名词——扩展层 bear 含「医保」时，
# 合并表从基础层 bull 剔除「医保」、bear 收录「医保」（#189 实证：「医保」被
# 基础层列为医药利好词，导致「国家医保局强化医药价格管理」这类控费利空被
# sec_net=1 强推看涨）。基础层词本身不可删（remove_word 只作用于扩展层），
# 需要覆盖某基础层词时，加一条反极性扩展词即可。

EXTRA_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                          "sector_lexicon_extra.json")


def _load_extra() -> Dict[str, Dict[str, List[str]]]:
    """读扩展层 {板块: {bull:[...], bear:[...]}}；文件缺失/损坏返回 {}。"""
    try:
        with open(EXTRA_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    out: Dict[str, Dict[str, List[str]]] = {}
    for sec, pol in d.items():
        if not isinstance(pol, dict):
            continue
        bull = [w for w in (pol.get("bull") or []) if isinstance(w, str) and w.strip()]
        bear = [w for w in (pol.get("bear") or []) if isinstance(w, str) and w.strip()]
        if bull or bear:
            out[sec] = {"bull": bull, "bear": bear}
    return out


def _save_extra(d: Dict[str, Dict[str, List[str]]]) -> None:
    os.makedirs(os.path.dirname(EXTRA_PATH), exist_ok=True)
    with open(EXTRA_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _tables(extra=None):
    """合并基础层 + 扩展层 → (bull, bear)。扩展层反极性词覆盖基础层同名词。

    只认 SECTOR_KEYWORDS 里已有的板块（手改 JSON 也注入不了新板块）；
    同词若同时出现在扩展层两侧（add_word 已阻止，手改 JSON 才会出现），
    两侧都收录、sec_net 互相抵消为中性，不制造方向。
    """
    bull = {s: list(v) for s, v in SECTOR_BULLISH.items()}
    bear = {s: list(v) for s, v in SECTOR_BEARISH.items()}
    for sec, pol in (extra or {}).items():
        if sec not in SECTOR_KEYWORDS:
            continue
        ex_b = [w for w in (pol.get("bull") or []) if w not in bull.get(sec, [])]
        ex_d = [w for w in (pol.get("bear") or []) if w not in bear.get(sec, [])]
        for w in ex_d:                    # 反极性覆盖：从对侧基础层剔除同名词
            if w in bull.get(sec, []):
                bull[sec].remove(w)
        for w in ex_b:
            if w in bear.get(sec, []):
                bear[sec].remove(w)
        bull[sec] = bull.get(sec, []) + ex_b
        bear[sec] = bear.get(sec, []) + ex_d
    return bull, bear


def rebuild(extra=None):
    """重建进程级合并词表 _BULL/_BEAR。extra=None 时从磁盘读扩展层。"""
    global _BULL, _BEAR
    _BULL, _BEAR = _tables(extra if extra is not None else _load_extra())


_BULL, _BEAR = _tables(_load_extra())   # 初始合并表（rebuild 会覆盖）


def list_extra():
    """扩展层现状（只读，不含基础层）。"""
    return _load_extra()


def add_word(sector, polarity, word):
    """扩展层新增一个板块催化剂词。polarity∈{'bull','bear'}。

    · sector 必须在 SECTOR_KEYWORDS；词非空且 ≤12 字（长词匹配不到正文，
      防止手滑/程序bug 往词表灌脏数据）
    · 同名词只保留一侧：先移除对侧扩展层的同名词
    · 基础层同极性已有该词 → 拒绝（纯重复）；基础层反极性有该词 → 允许，
      这正是「反极性覆盖」的合法路径（如给基础层利好词「医保」加一条 bear）
    返回更新后的 list_extra()。
    """
    sector = (sector or "").strip()
    word = (word or "").strip()
    if sector not in SECTOR_KEYWORDS:
        raise ValueError(f"未知板块：{sector!r}（须在 SECTOR_KEYWORDS 内）")
    if polarity not in ("bull", "bear"):
        raise ValueError("极性必须是 bull 或 bear")
    if not word:
        raise ValueError("词不能为空")
    if len(word) > 12:
        raise ValueError("词长超 12 字，拒绝")
    base_same = SECTOR_BULLISH if polarity == "bull" else SECTOR_BEARISH
    if word in base_same.get(sector, []):
        raise ValueError(f"基础层{polarity}词已有「{word}」，无需重复添加")
    other = "bear" if polarity == "bull" else "bull"
    d = _load_extra()
    d.setdefault(sector, {"bull": [], "bear": []})
    d[sector][other] = [w for w in d[sector].get(other, []) if w != word]
    if word not in d[sector][polarity]:
        d[sector][polarity].append(word)
    _save_extra(d)
    rebuild()
    return d


def remove_word(sector, word):
    """删除扩展层词（基础层不可删）。返回 (removed, 更新后的 list_extra())。"""
    sector = (sector or "").strip()
    word = (word or "").strip()
    d = _load_extra()
    removed = False
    if sector in d:
        for pol in ("bull", "bear"):
            if word in d[sector][pol]:
                d[sector][pol] = [w for w in d[sector][pol] if w != word]
                removed = True
        if not d[sector].get("bull") and not d[sector].get("bear"):
            d.pop(sector)
    if removed:
        _save_extra(d)
        rebuild()
    return removed, d


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
        # ponytail: 读合并表（基础层 + 扩展层覆盖），扩展层反极性词在此生效
        b = _match_terms(content, _BULL.get(s, []))
        d = _match_terms(content, _BEAR.get(s, []))
        sec_bull += b
        sec_bear += d
        bull += b
        bear += d
    # 去重：同词可能同时落在多个命中板块的词表里（如「空袭」既在军工又在能源），
    # 不去除会虚高 sec_net / cat_net。保留首次出现顺序。
    def _uniq(xs):
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    bull, bear = _uniq(bull), _uniq(bear)
    sec_bull, sec_bear = _uniq(sec_bull), _uniq(sec_bear)
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
    # 举一反三：一揽子并列快讯会被关键词散归到多个板块，造成"X新闻利好Y"式误读。
    # 两种形态：
    #  · 一揽子品类（如「统计局流通领域重要生产资料」罗列 生猪/电解铜/螺纹钢/天然气）
    #    ——螺纹钢只是被顺带列举，并非新闻主体；
    #  · 一揽子行业/个股（如「低估值个股回购增持价值榜」罗列 机械设备/电力设备/电子/
    #    医药生物/华新建材…）——板块标签只是顺带出现在更长专有名词里（华新建材→建材、
    #    医药生物→医药、电力设备→电力），属子串误中，与新闻主体无关。
    # 命中 ≥3 板块视为汇总型：先收敛到「标题/首句」命中的主品类；
    # 若标题无板块锚点（即不是针对某板块的催化事件），且全文也无任何板块专属
    # 利好/利空词，则视为全市场/跨板块事件，不向具体板块归因——避免「低估值个股
    # 回购」式误利好建材/医药/能源。（独立讲某品类/某板块的快讯命中数 <3，不受影响）
    if len(hit) >= 3:
        head = re.split(r"[；。!！?？]", text, 1)[0]
        head_secs = [s for s in hit if any(k in head for k in SECTOR_KEYWORDS[s])]
        if head_secs:
            return head_secs
        cats = detect_catalysts(text, hit)
        if not (cats["bull"] or cats["bear"]):
            return []
    return hit


# ───────────────────────── 时效衰减（#189 对账驱动） ─────────────────────────
# 对账发现 3/3 方向错失都由「主导新闻发布于前一日收盘前、市场已提前反映」驱动：
# 24h 窗口内旧闻与今日消息等权累加，会把已消化的利好重复计成当日偏多。
# 给每条快讯按 pub_ts 乘衰减权重（越新权重越高），昨日旧闻自然降权。
# ponytail: 参考时刻取「窗口内最新快讯的 pub_ts」而非墙钟——aggregate 保持
# 纯函数可离线自检，面板与对账（sector_verify）两端口径天然一致。
DECAY_HALF_LIFE_H = 12.0   # 半衰期：12h 前的快讯权重减半（24h 前 ≈0.25）
LOW_CONF_N = 3             # 样本数低于此值 → 面板标注低置信（与对账 MIN_N 一致）


def age_weight(pub_ts, ref_ts, half_life_h: float = DECAY_HALF_LIFE_H) -> float:
    """快讯时效权重 = 0.5 ** (距参考时刻小时数 / 半衰期)，∈(0, 1]。

    pub_ts 缺失/0/异常记 1.0（无发布时间信息不惩罚，宁可不少算）；
    ref_ts <= pub_ts（时钟回拨/未来戳）同样记 1.0。ref_ts 由调用方给
    （面板=窗口内最新 pub_ts），本函数不读墙钟，保证可测。

    踩坑记录（P1 自检抓出）：早年写成 `int(pub_ts or 0)` 后直接算 age，
    缺失/0 被当成「Unix 元年发布」→ age 巨大 → 权重≈0，把无时间戳的旧缓存
    条目静默清零（与文档意图正相反）。现显式判 `not pub` 提前返回 1.0。
    """
    try:
        pub = int(pub_ts or 0)
        age = (ref_ts - pub) / 3600.0
    except (TypeError, ValueError):
        return 1.0
    if not pub or age <= 0 or half_life_h <= 0:
        return 1.0
    return 0.5 ** (age / half_life_h)


def aggregate(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把快讯聚到板块。返回 {板块: {n,pos,neg,neu,score_sum,bull,bear,
    wsum,pos_w,neg_w,cat_net,samples,catalyst_samples}}。

    wsum/pos_w/neg_w 为时效衰减后的权重和（net 的加权口径）；n/pos/neg/neu
    仍是原始计数（面板展示、巡检阈值用）。

    samples 为结构化样本列表：[{tone,origin,corrected,text}]，tone=对本板块
    的生效方向，origin=词典初判，corrected=方向是否被板块词修正过。

    板块归因优先级：先 ticker/公司名 经 stock_sector_map 反查（精确）；
    反查不到（OkSurf/FreeNews 无 stocks、或 ticker 不在种子表）再回落关键词。

    cat_net（催化剂净信号）= 该板块命中的利好词数 - 利空词数（跨窗口累加），
    作为「词典情绪 tone」之外的二级信号：tone 看的是涨跌措辞，catalyst 看的是
    事件性质（如 OPEC减产=能源利好、集采=医药利空）。
    """
    agg: Dict[str, Dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "pos": 0, "neg": 0, "neu": 0,
                 "score_sum": 0.0, "bull": 0, "bear": 0,
                 "wsum": 0.0, "pos_w": 0.0, "neg_w": 0.0,
                 "samples": [], "catalyst_samples": []})
    # 时效参考时刻 = 窗口内最新快讯的 pub_ts（纯函数，不读墙钟）
    ref = max((int(it.get("pub_ts") or 0) for it in items), default=0)
    for it in items:
        content = it.get("content", "") or ""
        tone = (it.get("sentiment") or {}).get("tone", "neutral")
        score = float((it.get("sentiment") or {}).get("score", 0.0) or 0.0)
        w = age_weight(it.get("pub_ts"), ref)
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
            a["wsum"] += w
            a["score_sum"] += score * w
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
                a["pos_w"] += w
            elif eff == "neg":
                a["neg"] += 1
                a["neg_w"] += w
            else:
                a["neu"] += 1
            # 被板块词修正过方向的，样本记 corrected（便于回看"为什么读反了"）
            # 结构化而非拼 "[pos*] 文本"：前端正则只认 [pos]/[neg]/[neutral]，
            # 当年把 * 拼进前缀直接导致徽标错判中性、[pos*] 泄漏进正文（真 bug）。
            corrected = bool(sec_net) and eff != tone
            if len(a["samples"]) < 3:
                a["samples"].append({"tone": eff, "origin": tone,
                                     "corrected": corrected,
                                     "text": content[:56]})
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
        # ponytail: 净情绪走时效衰减后的加权占比（#189 对账口径），
        # 旧闻降权后不再把「已消化的利好」重复计成当日偏多
        wsum = a.get("wsum") or 0.0
        net = ((a.get("pos_w", 0.0) - a.get("neg_w", 0.0)) / wsum) if wsum else 0.0
        cat_net = a["bull"] - a["bear"]
        rows.append({"sector": sec, "n": a["n"], "pos": a["pos"],
                     "neg": a["neg"], "neu": a["neu"],
                     "net": round(net, 3),
                     "score_sum": round(a["score_sum"], 2),
                     "bull": a["bull"], "bear": a["bear"],
                     "cat_net": cat_net,
                     "low_conf": a["n"] < LOW_CONF_N,
                     "samples": a["samples"],
                     "catalyst_samples": a["catalyst_samples"]})
    rows.sort(key=lambda r: (r["net"], r["cat_net"], r["score_sum"]), reverse=True)
    return rows


def catalyst_lexicon() -> List[Dict[str, Any]]:
    """板块级利空利好词表（解析报告数据源）。

    返回生效中的**合并表**（基础层 + 扩展层，扩展层覆盖已反映），并附每板块
    扩展层词清单（extra_bull/extra_bear），便于前端区分「哪些词是 AI/用户
    后来加的、可删除」与「基础层硬编码、不可删」。
    """
    extra = _load_extra()
    out = []
    for sec in SECTOR_KEYWORDS:
        ex = extra.get(sec, {})
        out.append({
            "sector": sec,
            "bullish": _BULL.get(sec, []),
            "bearish": _BEAR.get(sec, []),
            "extra_bull": ex.get("bull", []),
            "extra_bear": ex.get("bear", []),
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
    # 举一反三（2026-10-13 修正）：纯地缘新闻（别国外交/冲突）不再归军工——
    # 旧版"伊朗/乌克兰/胡塞"等地缘实体词让 24h 内 128 条纯外交辞令误进军工
    ok("纯地缘新闻 不归军工", "军工" not in map_sectors("五角大楼拟定新的对伊朗打击方案"))
    ok("军工仍认产业词(导弹/军演)", "军工" in map_sectors("多国联合军演 试射新型导弹"))
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
    c1 = detect_catalysts("OPEC+减产，油价获得支撑", ["能源"])
    ok("能源 催化剂：减产=利好(霍尔木兹一词不再单独触发)", "减产" in c1["bull"] and "霍尔木兹" not in c1["bull"])
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

    # 不明病原/泄露/超级细菌/疫苗后遗症（用户补充规则）
    c10 = detect_catalysts("发现不明病毒，来源待查", ["医药"])
    ok("医药 不明病毒→利好", c10["sec_net"] > 0 and "不明病毒" in c10["sec_bull"])
    c11 = detect_catalysts("实验室病毒泄露，紧急封控", ["医药"])
    ok("医药 病毒泄露→利好", c11["sec_net"] > 0 and "病毒泄露" in c11["sec_bull"])
    c12 = detect_catalysts("超级细菌在院内蔓延", ["医药"])
    ok("医药 超级细菌→利好", c12["sec_net"] > 0 and "超级细菌" in c12["sec_bull"])
    c13 = detect_catalysts("出现疫苗后遗症病例", ["医药"])
    ok("医药 疫苗后遗症→利空", c13["sec_net"] < 0 and "疫苗后遗症" in c13["sec_bear"])
    c14 = detect_catalysts("医院系统遭勒索病毒攻击", ["医药"])
    ok("医药 勒索病毒不归医药(网络安全)", c14["sec_net"] == 0 and len(c14["sec_bull"]) == 0)

    # 反向直觉：充电中心→新能源利好；战争/交火/军演→军工利好；旱涝/虫灾/台风→农业利好
    c15 = detect_catalysts("某城市充电中心正式投运，超充站覆盖", ["新能源"])
    ok("新能源 充电中心→利好", c15["sec_net"] > 0 and "充电中心" in c15["sec_bull"])
    # 举一反三（2026-10-13 修正）：战争本体词（战争/交火/空袭/遭袭/袭击）不再判
    # 军工利好——"俄军袭击乌克兰数据中心"既非军费也非订单，方向应为中性。
    # 军工利好只认基本面：军费/军贸/装备出口/订单/预算/航展/军演。
    c16 = detect_catalysts("战争升级，边境爆发交火", ["军工"])
    ok("军工 战争/交火不再判利好(只认基本面)", c16["sec_net"] == 0 and not c16["sec_bull"])
    c17 = detect_catalysts("多国联合军演，实战检验新型装备", ["军工"])
    ok("军工 军演/装备→利好", c17["sec_net"] > 0 and "军演" in c17["sec_bull"])
    c18 = detect_catalysts("南方台风肆虐，农作物大面积受灾歉收", ["农业"])
    ok("农业 台风/受灾→利好(反向直觉)", c18["sec_net"] > 0 and "台风" in c18["sec_bull"])
    c19 = detect_catalysts("华北持续干旱，玉米减产预期升温", ["农业"])
    ok("农业 干旱/减产预期→利好", c19["sec_net"] > 0 and "干旱" in c19["sec_bull"])

    # 举一反三：金融机构反洗钱缺陷被罚→金融利空；霍尔木兹爆炸/油轮袭击→能源利好
    c20 = detect_catalysts("美国运通因反洗钱系统缺陷被罚3.5亿美元", ["金融"])
    ok("金融 反洗钱被罚→利空(举一反三)", c20["sec_net"] < 0 and "被罚" in c20["sec_bear"] and "反洗钱" in c20["sec_bear"])
    c21 = detect_catalysts("某银行因合规缺陷遭监管处罚，没收违法所得", ["金融"])
    ok("金融 监管处罚/没收违法所得→利空", c21["sec_net"] < 0 and "监管处罚" in c21["sec_bear"])
    c22 = detect_catalysts("霍尔木兹海峡南部航线发生数起猛烈爆炸", ["能源"])
    ok("能源 霍尔木兹爆炸→利好(举一反三)", c22["sec_net"] > 0 and "爆炸" in c22["sec_bull"])
    c23 = detect_catalysts("红海油轮遭袭，航运受阻推升油价", ["能源"])
    ok("能源 油轮袭击/航运受阻→利好", c23["sec_net"] > 0 and "油轮" in c23["sec_bull"])

    # 举一反三（2026-10-13 修正）：删 霍尔木兹/空袭/航母/对伊打击 后，
    # "五角大楼制定空袭计划/航母部署"不再算能源利好；明确的对伊制裁才是。
    c24 = detect_catalysts("美国宣布对伊朗实施新一轮制裁，油轮禁运升级", ["军工", "能源"])
    ok("能源 对伊制裁/油轮禁运→利好(举一反三)", c24["sec_net"] > 0 and ("制裁伊朗" in c24["sec_bull"] or "油轮禁运" in c24["sec_bull"]))
    c25 = detect_catalysts("中东某港口发生猛烈爆炸，原油装卸被迫停止", ["军工", "能源"])
    ok("能源 港口爆炸/装卸停止→利好", c25["sec_net"] > 0 and "爆炸" in c25["sec_bull"])
    c26 = detect_catalysts("刚果（金）埃博拉疫情依然严峻，跨境传播风险上升", ["医药"])
    ok("医药 埃博拉/跨境传播→利好(举一反三)", c26["sec_net"] > 0 and "埃博拉" in c26["sec_bull"] and "跨境传播" in c26["sec_bull"])
    # 跨板块同词去重：出口管制同时命中半导体+通信利空，sec_bear 不应重复计数
    c27 = detect_catalysts("美国拟对华实施芯片出口管制", ["半导体", "通信"])
    ok("跨板块同词去重(出口管制不重复计)", c27["sec_bear"].count("出口管制") == 1)

    # 举一反三（用户实样）：AI资本开支/算力订单 → 科技AI利好；墨西哥湾油气停产 → 能源利好
    c28 = detect_catalysts("OpenAI Codex开发者称GPT-6.1Sol的超快模式（Ultrafast）正在推出", ["科技AI"])
    ok("科技AI 泛词(推出/发布)不再单独触发利好", c28["sec_net"] == 0)
    c29 = detect_catalysts("特朗普人工智能特别工作组负责人将于周四会面", ["科技AI"])
    ok("科技AI 特别工作组/AI政策→利好", c29["sec_net"] > 0 and "特别工作组" in c29["sec_bull"])
    c30 = detect_catalysts("墨西哥湾目前约62.89%的日均石油产量和57.35%的日均天然气产量已停产", ["能源"])
    ok("能源 墨西哥湾油气停产→利好(举一反三)", c30["sec_net"] > 0 and "停产" in c30["sec_bull"])
    # 举一反三（2026-10-13 修正）：基辅遭袭/俄军袭击 不再归军工也不判利好；
    # 军工利好只认军贸/装备出口等基本面词
    ok("地缘遭袭 不归军工", "军工" not in map_sectors("乌称首都基辅遭袭 俄军袭击基辅"))
    c31 = detect_catalysts("我国军贸订单落地，装备出口增长", ["军工"])
    ok("军工 军贸订单/装备出口→利好", c31["sec_net"] > 0 and "军贸" in c31["sec_bull"])

    # 举一反三（AI伦理/治理类）：Anthropic禁止"虐待"旗下AI = 科技AI，绝非医药；
    # "虐待/数据泄露/幻觉/成瘾"等医学感官词不得把新闻拖进医药
    ai_eth = map_sectors("Anthropic宣布禁止“虐待”其旗下AI")
    ok("AI伦理 禁止虐待AI→科技AI非医药", ai_eth == ["科技AI"])
    ok("AI伦理 无公司名也归科技AI", "科技AI" in map_sectors("AI伦理新规出台 禁止虐待AI模型") and "医药" not in map_sectors("AI伦理新规出台 禁止虐待AI模型"))
    ok("AI权益/AI福利归科技AI", "科技AI" in map_sectors("AI福利与AI权益保护政策出台") and "医药" not in map_sectors("AI福利与AI权益保护政策出台"))
    ok("数据泄露不归医药", "医药" not in map_sectors("某大厂服务器数据泄露，用户信息外流"))
    ok("AI幻觉不归医药", "医药" not in map_sectors("AI幻觉导致客服答非所问"))
    ok("AI成瘾/防沉迷不归医药", "医药" not in map_sectors("用户对AI聊天成瘾，平台推出防沉迷"))

    # 举一反三：一揽子品类并列快讯收敛（猪肉新闻不再利好建材）
    # 统计局「流通领域重要生产资料」同时罗列生猪/电解铜/天然气/螺纹钢/液化石油气
    basket = ("国家统计局：9月下旬生猪（外三元）为10.3元/千克，环比下降3.7%；"
              "电解铜为111742.5元/吨，环比增长2.7%；液化天然气为6185.9元/吨，环比下降0.6%；"
              "螺纹钢为3175.9元/吨，环比增长0.3%；复合肥为3599.7元/吨，环比下降0.3%；"
              "液化石油气为7506.1元/吨，环比增长0.2%。")
    basket_secs = map_sectors(basket)
    ok("一揽子品类快讯 收敛到主品类(农业)", basket_secs == ["农业"])
    ok("一揽子品类快讯 不再误归建材", "建材" not in basket_secs)
    ok("一揽子品类快讯 不再误归有色/能源", "有色" not in basket_secs and "能源" not in basket_secs)
    # 独立讲某品类的快讯：命中数<3，不受影响，正常归建材
    ok("独立螺纹钢快讯 仍归建材", "建材" in map_sectors("螺纹钢主力合约涨2.1%，钢厂补库需求回暖"))

    # 举一反三：一揽子行业/个股罗列型汇总快讯（板块标签只是顺带出现在更长专有名词里）
    # 如「低估值个股回购增持价值榜」罗列 机械设备/电力设备/电子/医药生物/华新建材…
    # 华新建材→建材(公司名含"建材")、医药生物→医药、电力设备→电力 均为子串误中，
    # 属跨板块回购/增持价值榜，非针对某板块的催化事件，不应利好建材/医药/能源。
    buyback = ("13只低估值个股推出大手笔回购或增持计划 自2026年7月1日以来，截至10月9日，"
               "拟增持金额上限及拟回购金额上限合计超过千亿元。整体来看，下半年以来约470股发起"
               "增持或回购计划，主要分布于机械设备、电力设备、电子、医药生物等行业。"
               "按照市盈率折价率排序，华新建材、山东路桥、中国建筑最新市盈率较所属行业市盈率"
               "折价均超过40%。（人民财讯）")
    bb_secs = map_sectors(buyback)
    ok("回购增持价值榜 不再误归建材", "建材" not in bb_secs)
    ok("回购增持价值榜 不再误归医药/能源", "医药" not in bb_secs and "能源" not in bb_secs)
    ok("回购增持价值榜 跨板块汇总不归因", bb_secs == [])

    # 举一反三（24h快讯缺口扫描修复）：本应归类却被具体关键词漏掉的硬缺口
    # 柴油/成品油 → 能源（俄解除柴油出口禁令）
    c_diesel = detect_catalysts("俄副总理：俄将提前解除柴油出口禁令", ["能源"])
    ok("柴油出口禁令→能源(油品类补漏)", "能源" in map_sectors("俄副总理：俄将提前解除柴油出口禁令")
       and c_diesel["sec_net"] >= 0)
    # 汽油/成品油 → 能源
    ok("汽油价格新闻→能源", "能源" in map_sectors("国内汽油价格上调 成品油迎年内最大涨幅"))
    # 美光/高通/安谋(ARM) → 半导体
    ok("美光科技→半导体", "半导体" in map_sectors("美光科技将股票回购计划扩大至350亿美元"))
    ok("高通诉安谋→半导体", "半导体" in map_sectors("高通诉安谋控股一案的陪审团尚未作出裁决"))
    # 有机硅/硅宝 → 化工。举一反三（2026-10-13 修正）："拉升/异动/涨停"是股价
    # 结果不是产业催化（与词典 tone 重复），利好侧只认"涨价"等价格信号。
    c_si = detect_catalysts("有机硅价格再度上涨，东岳硅材涨价", ["化工"])
    ok("有机硅→化工(涨价=利好信号)", "化工" in map_sectors("A股有机硅板块异动拉升 东岳硅材涨停")
       and "涨价" in c_si["sec_bull"] and c_si["sec_net"] > 0)
    # 淡水河谷/铁矿石 → 有色
    ok("淡水河谷→有色(矿业个股补漏)", "有色" in map_sectors("淡水河谷称将对原董事会副主席提起刑事诉讼"))

    # 举一反三（金十数据源补充）：从金十 flashMarketNameTable/app-configs 提取的
    # 权威行情市场名词，原 SECTOR_KEYWORDS 全部缺失，补进对应板块归因
    # 布伦特原油/WTI原油/美原油 → 能源（原油基准价）
    ok("布伦特原油→能源(金十行情词)", "能源" in map_sectors("布伦特原油期货结算价上涨 供应端趋紧"))
    ok("WTI原油→能源(金十行情词)", "能源" in map_sectors("WTI原油盘中突破每桶80美元"))
    # 现货黄金/伦敦金/comex黄金/现货白银/现货铂钯 → 有色（贵金属）
    ok("现货黄金→有色(金十行情词)", "有色" in map_sectors("现货黄金刷新历史高位 避险需求升温"))
    ok("伦敦金→有色(金十行情词)", "有色" in map_sectors("伦敦金站上每盎司2600美元"))
    ok("现货白银→有色(金十行情词)", "有色" in map_sectors("现货白银跟随黄金走强 工业需求回暖"))
    ok("现货铂金→有色(金十行情词)", "有色" in map_sectors("现货铂金供应缺口扩大 价格上行"))
    # 美元指数/欧元美元/英镑美元 → 金融（汇率/货币市场）
    ok("美元指数→金融(金十行情词)", "金融" in map_sectors("美元指数冲高 非美货币普遍承压"))
    ok("欧元/美元→金融(金十行情词)", "金融" in map_sectors("欧元/美元跌破1.05关口 欧央行表态偏鸽"))


    # 3) aggregate 产出催化剂列
    fake = [
        {"content": "OPEC+减产 霍尔木兹受阻 油价大涨", "sentiment": {"tone": "pos", "score": 1.0}, "stocks": []},
        {"content": "第十二批集采 仿制药降价", "sentiment": {"tone": "neg", "score": -1.0}, "stocks": []},
    ]
    agg = aggregate(fake)
    ok("aggregate 含 能源.cat_net>0", agg.get("能源", {}).get("cat_net", 0) > 0)
    ok("aggregate 含 医药.cat_net<0", agg.get("医药", {}).get("cat_net", 0) < 0)
    ok("heat_table 含 catalyst_samples", bool(heat_table(agg)[0].get("catalyst_samples")))
    rows = heat_table(agg)
    flat = [x for r in rows for x in r["samples"]]
    ok("samples 为结构化 {tone,origin,corrected,text}",
       bool(flat) and all(set(x) >= {"tone", "origin", "corrected", "text"} for x in flat))
    ok("corrected 样本满足 origin!=tone",
       all(x["origin"] != x["tone"] for x in flat if x["corrected"]))
    # 防回归：曾因把 "[pos*]" 拼进文本导致前缀泄漏到正文（前端正则不认 *）
    ok("样本文本不含 [ 前缀", not any(str(x.get("text", "")).startswith("[") for x in flat))

    # ── 时效衰减（#189 对账驱动）────────────────────────────────────────
    # 固定参考基准（不读墙钟，纯函数可测）；内容只带板块归类词、不带任何
    # 催化剂词，确保 sec_net=0、eff=词典 tone，单独检验衰减本身。
    _BASE = 2_000_000_000
    ok("age_weight：同时刻=1.0", age_weight(_BASE, _BASE) == 1.0)
    ok("age_weight：半衰期前≈0.5", abs(age_weight(_BASE - 12 * 3600, _BASE) - 0.5) < 1e-9)
    ok("age_weight：缺失 pub_ts=1.0（不惩罚）", age_weight(0, _BASE) == 1.0)
    ok("age_weight：未来戳=1.0（时钟回拨）", age_weight(_BASE + 3600, _BASE) == 1.0)

    def _dec_net(items):
        r = {x["sector"]: x for x in heat_table(aggregate(items))}
        return r.get("新能源", {})

    _old_pos = {"content": "光伏板块快讯", "sentiment": {"tone": "pos", "score": 2.0},
                "stocks": [], "pub_ts": _BASE - 20 * 3600}
    _fresh_neg = {"content": "风电板块快讯", "sentiment": {"tone": "neg", "score": -2.0},
                  "stocks": [], "pub_ts": _BASE - 3600}
    _fresh_pos = {"content": "光伏板块快讯", "sentiment": {"tone": "pos", "score": 2.0},
                  "stocks": [], "pub_ts": _BASE - 3600}
    # 等权下 1 旧看多 + 1 新看跌 = 中性；衰减后旧闻权重≈0.21，净情绪应转负
    _r = _dec_net([_old_pos, _fresh_neg])
    ok("衰减：旧看多+新看跌 → 净情绪转负（等权本为中性）", _r.get("net", 0) < 0)
    ok("衰减：旧看多+新看跌 → 判低置信（n=2）", _r.get("low_conf") is True)
    # 反向：旧看跌 + 新看多 → 净情绪转正（衰减对称，不制造方向偏置）
    _r2 = _dec_net([dict(_fresh_neg, pub_ts=_BASE - 20 * 3600), _fresh_pos])
    ok("衰减：旧看跌+新看多 → 净情绪转正", _r2.get("net", 0) > 0)
    # 全新鲜样本不受影响（权重都=1，与旧口径一致）
    _r3 = _dec_net([_fresh_pos, _fresh_pos, dict(_fresh_neg, pub_ts=0)])
    ok("衰减：全新鲜/无 pub_ts → 权重=1 口径不变", _r3.get("net", 0) > 0 and not _r3.get("low_conf"))
    # 旧口径行为不回归：无 pub_ts 的全部样本，net 仍等于原始计数占比
    _r4 = _dec_net([_fresh_pos, _fresh_pos, _fresh_pos, _fresh_neg])
    ok("衰减：4 条无衰减扰动时 net 仍=原始占比(0.5)", abs(_r4.get("net", 0) - 0.5) < 1e-9)

    # 4) lexicon 覆盖所有板块
    lex = {x["sector"] for x in catalyst_lexicon()}
    ok("catalyst_lexicon 覆盖全部板块", lex == set(SECTOR_KEYWORDS.keys()))

    # ── 防回归（2026-10-13 24h 回溯的教训，词表只许收紧不许放松）────────────
    # 2026-10-13 回溯 24h 数据发现：军工 288 条命中里 128 条纯由"伊朗/乌克兰/
    # 胡塞/普京"等地缘实体词触发（连"亚马逊收购以色列工程师创办的AI公司"
    # 都因"以色列"进军工）；能源 227 条里 87 条同理。以下断言锁死修复成果，
    # 防止后来者"再补几个地缘词"把舆情重新灌水。
    # 注意"航母"是例外：军工词表保留它——"中国第三艘航母下水"是产业新闻，
    # 与"美军航母部署"不同主体；24h 回溯中它未造成误伤，故不锁死。
    _GEO = {"伊朗", "俄乌", "乌克兰", "以色列", "胡塞", "普京", "基辅", "莫斯科",
            "克里姆林宫", "中东", "五角大楼", "也门", "萨那", "霍尔木兹", "北约",
            "空袭", "海湾", "俄军"}
    _geo_leak = [(s, w) for s in ("军工", "能源") for w in _GEO if w in SECTOR_KEYWORDS[s]]
    ok("军工/能源归类词 不含地缘实体", not _geo_leak)
    # 战争本体词不得回军工/能源利好（别国冲突≠A股军工订单）
    _WAR = {"战争", "战争升级", "交火", "开战", "冲突升级", "冲突外溢", "空袭",
            "遭袭", "袭击", "军事打击", "导弹袭击", "战事", "对峙", "边境冲突",
            "边境摩擦", "基辅", "俄军", "乌克兰"}
    _war_leak = [(s, w) for s in ("军工", "能源") for w in _WAR if w in SECTOR_BULLISH.get(s, [])]
    ok("军工/能源利好词 不含战争本体词", not _war_leak)
    # 否定子串误中："增长"会命中"同比下降"，任何板块利好词都不许有
    _neg_sub = [(s, w) for s in SECTOR_BULLISH for w in ("增长",) if w in SECTOR_BULLISH[s]]
    ok("利好词 不含'增长'(同比下降子串误中)", not _neg_sub)
    # 涨跌措辞归词典 tone，板块专属词不得重复判定
    _tone_leak = [(s, w) for s in SECTOR_BULLISH for w in ("上涨",) if w in SECTOR_BULLISH[s]]
    _tone_leak += [(s, w) for s in SECTOR_BEARISH for w in ("下跌",) if w in SECTOR_BEARISH[s]]
    ok("板块专属词 不含涨跌措辞(上涨/下跌归词典)", not _tone_leak)
    # 股价结果词不是产业催化（与词典 tone 重复）
    _result_leak = [(s, w) for s in SECTOR_BULLISH
                    for w in ("涨停", "异动", "拉升", "板块走强") if w in SECTOR_BULLISH[s]]
    ok("利好词 不含股价结果词(涨停/异动/拉升/走强)", not _result_leak)
    # 泛化动作词（一提就利好，不分主体）
    _vague_leak = [(s, w) for s in ("科技AI",) for w in ("发布", "推出", "升级", "合作") if w in SECTOR_BULLISH[s]]
    ok("科技AI利好词 不含泛化动作词(发布/推出/升级/合作)", not _vague_leak)
    # 英文复合词误伤：network 会命中 neural network；Spectrum 会命中 spectrum(范围)
    _en_leak = [(s, w) for s in SECTOR_KEYWORDS for w in ("network", "Spectrum") if w in SECTOR_KEYWORDS[s]]
    ok("归类词 不含 network/Spectrum(复合词误伤)", not _en_leak)

    # ── 防回归二轮（2026-10-13 巡检工作流首跑暴露的品类词污染）────────────
    # 教训：催化剂词表混入"品类/硬件/指标"名词——新闻只要提到品类就被刷成利好，
    # 连方向反的都不放过（"市值跌破500亿"因"突破"判利好、"销量下降53%"因"销量"
    # 判利好、"AI债务发行跌超50%"因"芯片"判利好、"美股光通信股普跌"因"光通信"
    # 判利好）。原则：催化剂只收"事件/动作"，品类词归归类词表。
    _CAT = {"导弹", "无人机", "雷达", "商业航天",            # 军工硬件/品类
            "算力", "数据中心", "AI基础设施", "光模块", "光通信",  # 通信硬件
            "芯片", "云", "大模型", "落地",                  # 科技AI品类/泛词
            "销量", "智能化", "突破",                        # 汽车指标/泛词
            "政策", "签约", "探底", "放量"}                   # 跨板块泛词/结果词
    _cat_leak = [(s, w) for s in SECTOR_BULLISH for w in _CAT if w in SECTOR_BULLISH[s]]
    ok("利好词 不含品类/泛词(导弹/芯片/销量/突破/政策…)", not _cat_leak)
    _cat_leak_b = [(s, w) for s in SECTOR_BEARISH for w in _CAT if w in SECTOR_BEARISH[s]]
    ok("利空词 不含品类/泛词(同上)", not _cat_leak_b)
    _glob_leak = [w for w in ("签约", "放量") if w in GLOBAL_BULLISH]
    ok("全局利好词 不含签约/放量", not _glob_leak)
    # 行为断言：真实 24h 例句，修正后应为中性（不被品类词改写方向）
    _c1 = detect_catalysts("伊朗革命卫队发言人发布了一张导弹阵地瞄准一艘航空母舰的图片", ["军工"])
    ok("军工 伊朗导弹瞄准航母→不判利好", _c1["sec_net"] == 0)
    _c2 = detect_catalysts("沙特主导的多国联军称拦截胡塞武装一枚弹道导弹", ["军工"])
    ok("军工 沙特拦截弹道导弹→不判利好", _c2["sec_net"] == 0)
    _c3 = detect_catalysts("【美股光通信股开盘普跌】Lumentum跌2.61%，康宁跌1.55%", ["通信"])
    ok("通信 美股光通信股普跌→不判利好", _c3["sec_net"] == 0)
    _c4 = detect_catalysts("摩根士丹利：9月全球AI相关债务发行规模跌超50%", ["科技AI"])
    ok("科技AI AI债务发行跌超50%→不翻成利好", _c4["sec_net"] == 0)
    _c5 = detect_catalysts("江淮汽车市值跌破500亿，成交额突破25亿元", ["汽车"])
    ok("汽车 市值跌破+成交额突破→不判利好", _c5["sec_net"] == 0)
    _c6 = detect_catalysts("本田中国9月汽车销量为25,564辆，同比下降53.1%", ["汽车"])
    ok("汽车 销量同比下降→不判利好", _c6["sec_net"] == 0)
    _c7 = detect_catalysts("A股午评：创业板指半日跌超1%，农业板块探底", ["农业"])
    ok("农业 午评探底→不判利空", _c7["sec_net"] == 0)
    # 真催化剂仍有效（只收紧品类词，不误杀事件词）
    _c8 = detect_catalysts("问界汽车出海欧洲，计划2027年进入欧洲市场，欧盟整车型式认证全面启动", ["汽车"])
    ok("汽车 出海欧洲市场→仍判利好(出海保留)", _c8["sec_net"] > 0)
    _c9 = detect_catalysts("中国军贸订单落地，装备出口额创新高", ["军工"])
    ok("军工 军贸订单→仍判利好", _c9["sec_net"] > 0)

    # ── 扩展层双轨（AI 优化建议落点）────────────────────────────────────
    # 覆盖语义：扩展层反极性词覆盖基础层同名词。#189 实证：「医保」被基础层
    # 列为医药利好词，导致「国家医保局强化医药价格管理」（控费利空）被强推
    # 看涨；扩展层 bear 收录「医保」后，合并表须读成利空。
    _orig_extra = _load_extra()
    try:
        rebuild({"医药": {"bear": ["医保"]}})
        c_ex = detect_catalysts("国家医保局强化医药价格管理，控费力度加大", ["医药"])
        ok("扩展层覆盖：医保 改判医药利空",
           "医保" in c_ex["sec_bear"] and "医保" not in c_ex["sec_bull"] and c_ex["sec_net"] < 0)
        ok("扩展层覆盖：同板块基础层其他词不动",
           "集采" in _BEAR.get("医药", []) and "创新药" in _BULL.get("医药", []))
        ok("扩展层覆盖：其他板块不受影响", "医保" not in _BULL.get("消费", []))
        # add/remove roundtrip（写真实文件，finally 还原）
        add_word("医药", "bear", "自检临时词")
        ok("add_word：扩展层可见", "自检临时词" in list_extra().get("医药", {}).get("bear", []))
        ok("add_word：合并表生效", "自检临时词" in _BEAR.get("医药", []))
        _rt = detect_catalysts("某品种自检临时词落地，市场关注度上升", ["医药"])
        ok("add_word：命中即判利空", _rt["sec_net"] < 0 and "自检临时词" in _rt["sec_bear"])
        # 同名词只保留一侧：改加到 bull 后，bear 侧应消失
        add_word("医药", "bull", "自检临时词")
        ok("add_word：同词只保留一侧(转bull)",
           "自检临时词" in _BULL.get("医药", []) and "自检临时词" not in _BEAR.get("医药", []))
        _rm, _ex_after = remove_word("医药", "自检临时词")
        ok("remove_word：删除并生效", _rm and "自检临时词" not in _BULL.get("医药", []))
        # 护栏：未知板块/坏极性/超长词/基础层同极性重复词 一律拒绝
        def _raises(fn, label):
            try:
                fn()
                ok(label, False)
            except ValueError:
                ok(label, True)
            except Exception:
                ok(label, False)
        _raises(lambda: add_word("不存在的板块", "bull", "词"), "add_word：拒绝未知板块")
        _raises(lambda: add_word("医药", "sideways", "词"), "add_word：拒绝坏极性")
        _raises(lambda: add_word("医药", "bull", "一二三四五六七八九十十一十二十三"),
                "add_word：拒绝超长词(>12字)")
        _raises(lambda: add_word("医药", "bull", "创新药"), "add_word：拒绝基础层同极性重复")
        # 反极性覆盖基础层词合法（「医保」就在基础层 bull，加 bear 是允许路径）
        add_word("医药", "bear", "医保")
        ok("add_word：允许反极性覆盖基础层词",
           "医保" in _BEAR.get("医药", []) and "医保" not in _BULL.get("医药", []))
        remove_word("医药", "医保")
        # 手改 JSON 注入未知板块：_tables 忽略，不扩散
        rebuild({"火星": {"bull": ["陨石"]}})
        ok("扩展层：未知板块被忽略", "火星" not in _BULL and "陨石" not in _match_terms("陨石", _BULL.get("医药", [])))
    finally:
        _save_extra(_orig_extra)
        rebuild(_orig_extra)
    # ponytail: 还原断言对齐「重载 _orig_extra 后的应有状态」，不预设扩展层为空
    # （否则真实扩展层里若已有「医保」反极性覆盖，自检会假失败——P1 调试实测踩到）
    ok("finally 还原：扩展层恢复原状",
       _load_extra() == _orig_extra
       and _BULL == _tables(_orig_extra)[0] and _BEAR == _tables(_orig_extra)[1])

    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
