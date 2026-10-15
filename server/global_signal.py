"""全球领先信号层（#225）：抓海外权威源 → 按板块聚合 → AI 提炼中文要点。

为什么要有：A股板块的驱动信号常先出现在全球英文源（半导体设备禁令、OpenAI 发布、
OPEC 减产…），国内快讯流是二手的、滞后的。本模块把 12 赛道 ~73 个实测可达的源
聚成中文要点，挂到快讯页「板块舆情」里，补上「全球视角」这一层。

数据流：
  fetch_all()  并发抓 RSS（标准库 urllib+ET，零依赖）→ URL/标题归一化 + story 合并
               （同一条通讯社稿被 N 家转 → 合成 1 条，记 src_n 源数）
  digest()     每板块喂当日条目给 llm_client → 3~5 条中文要点（带源数上下文 + 溯源）
  run_daily()  fetch + digest + 落盘 data/global_signals.json（供 /api/newsfeed/global 读）

设计要点（源自 investment-news 的成熟做法 + 我们对多源权重的需求）：
  - **story 合并**：相同 URL 或 48h 内相同标题 → 一条，src_n 记「几家在报」。
    源数是要给 LLM 和权重的信号：10 家转同稿 = 共识级，不该被当 10 条独立消息。
  - **源数权重亚线性** w=1+0.35*log2(src_n)：w(1)=1, w(3)=1.55, w(10)=2.16。
    线性会让 syndication 网络最大的通讯社垄断每个板块的情绪——那是 1 个编辑决策的
    N 次转载，不是 N 次独立确认。knob 见 SRC_WEIGHT_BETA。
  - **不引依赖、不占请求路径**：抓取走 daemon 线程每日一次，接口只读落盘文件。

ponytail: 板块映射用「赛道→板块」的静态表，够用；要更细再做关键词级映射。
"""
from __future__ import annotations
import concurrent.futures as _cf
import datetime as _dt
import json
import math
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
OUT_FILE = os.path.join(DATA_DIR, "global_signals.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = int(os.environ.get("TICK_GLOBAL_FETCH_TIMEOUT", "20"))
PER_SOURCE = 5                    # 每源取最新 N 条
RECENT_HOURS = 72                 # 「近 N 小时」窗口（海外源更新慢，比国内放宽）
DUP_TITLE_WINDOW_S = 48 * 3600    # 标题去重时间窗（防「每周综述」类固定栏目误合）
SRC_WEIGHT_BETA = 0.35            # 源数权重系数（knob，见文件头说明）

# 只列无歧义的跟踪参数；source/from/ref 这类有些站点是文章标识的一部分，剥了会丢内容
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "spm", "share_token", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}

# ===========================================================================
# 源清单：实测可达的 ~73 个源（2026-10-16 全量探测，33 个不可达已剔除：
# 付费墙 FT/WSJ 部分、反爬站 SpaceNews/Engadget、被墙的国内源等）。
# 内嵌代码而非 data/*.json：清单随连通性迭代，改代码有 git 历史可追溯。
# ===========================================================================
#: (源名, 赛道, URL)
SOURCES: List[Tuple[str, str, str]] = [
    # ---- ai ----
    ("OpenAI", "ai", "https://openai.com/news/rss.xml"),
    ("量子位", "ai", "https://www.qbitai.com/feed"),
    ("MIT Tech Review AI", "ai", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("The Verge AI", "ai", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch AI", "ai", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MarkTechPost", "ai", "https://www.marktechpost.com/feed/"),
    ("智东西", "ai", "https://zhidx.com/rss"),
    ("机器之心", "ai", "https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml"),
    ("新智元", "ai", "https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml"),
    # ---- semi ----
    ("DIGITIMES", "semi", "https://www.digitimes.com/rss/daily.xml"),
    ("Semiconductor Engineering", "semi", "https://semiengineering.com/feed/"),
    ("EE Times", "semi", "https://www.eetimes.com/feed/"),
    ("IEEE Spectrum 半导体", "semi", "https://spectrum.ieee.org/feeds/topic/semiconductors.rss"),
    ("Semiconductor Today", "semi", "https://www.semiconductor-today.com/rss/news.xml"),
    # ---- robot ----
    ("IEEE Spectrum 机器人", "robot", "https://spectrum.ieee.org/feeds/topic/robotics.rss"),
    ("Robohub", "robot", "https://robohub.org/feed/"),
    ("Robotics & Automation", "robot", "https://roboticsandautomationnews.com/feed/"),
    # ---- auto ----
    ("Electrek", "auto", "https://electrek.co/feed/"),
    ("InsideEVs", "auto", "https://insideevs.com/rss/articles/all/"),
    ("The Verge Transport", "auto", "https://www.theverge.com/rss/transportation/index.xml"),
    ("CnEVPost", "auto", "https://cnevpost.com/feed/"),
    # ---- energy ----
    ("Utility Dive", "energy", "https://www.utilitydive.com/feeds/news/"),
    ("Energy Storage News", "energy", "https://www.energy-storage.news/feed/"),
    ("OilPrice", "energy", "https://oilprice.com/rss/main"),
    ("Canary Media", "energy", "https://www.canarymedia.com/articles.rss"),
    ("PV Tech", "energy", "https://www.pv-tech.org/feed/"),
    # ---- bio ----
    ("STAT News", "bio", "https://www.statnews.com/feed/"),
    ("Endpoints News", "bio", "https://endpts.com/feed/"),
    ("BioPharma Dive", "bio", "https://www.biopharmadive.com/feeds/news/"),
    # ---- space ----
    ("Space.com", "space", "https://www.space.com/feeds/all"),
    ("Spaceflight Now", "space", "https://spaceflightnow.com/feed/"),
    ("NASA", "space", "https://www.nasa.gov/news-release/feed/"),
    # ---- security ----
    ("Krebs on Security", "security", "https://krebsonsecurity.com/feed/"),
    ("Dark Reading", "security", "https://www.darkreading.com/rss.xml"),
    # ---- tech ----
    ("TechCrunch", "tech", "https://techcrunch.com/feed/"),
    ("The Verge", "tech", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "tech", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Hacker News", "tech", "https://hnrss.org/frontpage"),
    ("WIRED", "tech", "https://www.wired.com/feed/rss"),
    ("Techmeme", "tech", "https://www.techmeme.com/feed.xml"),
    ("钛媒体", "tech", "https://www.tmtpost.com/rss.xml"),
    ("IT之家", "tech", "https://www.ithome.com/rss/"),
    ("GitHub Blog", "tech", "https://github.blog/feed/"),
    ("虎嗅", "tech", "https://rss.huxiu.com/"),
    ("Solidot", "tech", "https://www.solidot.org/index.rss"),
    ("白鲸出海", "tech", "https://www.baijingapp.com/feed"),
    # ---- consumer ----
    ("9to5Mac", "consumer", "https://9to5mac.com/feed/"),
    ("9to5Google", "consumer", "https://9to5google.com/feed/"),
    ("GSMArena", "consumer", "https://www.gsmarena.com/rss-news-reviews.php3"),
    ("Android Authority", "consumer", "https://www.androidauthority.com/feed/"),
    ("少数派", "consumer", "https://sspai.com/feed"),
    # ---- macro ----
    ("CNBC", "macro", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "macro", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("华尔街见闻", "macro", "https://dedicated.wallstreetcn.com/rss.xml"),
    ("SEC", "macro", "https://www.sec.gov/news/pressreleases.rss"),
    ("Federal Reserve", "macro", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Seeking Alpha", "macro", "https://seekingalpha.com/market_currents.xml"),
    ("东方财富股票", "macro", "http://rss.eastmoney.com/rss_stock.xml"),
    ("经济观察网", "macro", "http://www.eeo.com.cn/rss.xml"),
    # ---- science ----
    ("ScienceDaily", "science", "https://www.sciencedaily.com/rss/all.xml"),
    ("Quanta Magazine", "science", "https://api.quantamagazine.org/feed/"),
    ("New Scientist", "science", "https://www.newscientist.com/feed/home/"),
    ("Live Science", "science", "https://www.livescience.com/feeds/all"),
    ("Science News", "science", "https://www.sciencenews.org/feed"),
]

#: 赛道 → 我们 16 板块（对齐 sector_sentiment.SECTOR_KEYWORDS）。
#: science 归「科技AI」（前沿科学主要落在科技叙事），consumer 归「消费」。
TRACK_TO_SECTOR = {
    "ai": "科技AI", "semi": "半导体", "robot": "科技AI", "auto": "汽车",
    "energy": "新能源", "bio": "医药", "space": "军工", "security": "科技AI",
    "tech": "科技AI", "consumer": "消费", "macro": "金融", "science": "科技AI",
}

#: 合规红线：命中即丢弃（不进入聚合/LLM）。借鉴 investment-news。
REDLINE = re.compile(
    r"赌博|博彩|赌场|彩票|下注|押注|预测市场|polymarket|"
    r" porn|nsfw|crypto pump|memecoin|赌博平台", re.I)


# ===========================================================================
# 抓取 + 解析
# ===========================================================================
def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _norm_url(url: str) -> str:
    """URL 归一化（剥跟踪参数 + 去锚点 + 去尾斜杠）；解析失败退回小写原串，绝不抛。"""
    if not url:
        return ""
    url = url.strip()
    try:
        p = urlsplit(url)
    except ValueError:
        return url.lower()
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                       urlencode(kept), ""))


def _norm_title(t: str) -> str:
    return re.sub(r"[\W_]+", "", (t or "").lower())


def _parse_time(s: str) -> float:
    """RSS pubDate / Atom updated → epoch 秒；解析不了返回 0。"""
    if not s:
        return 0.0
    s = s.strip()
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except Exception:
            continue
    return 0.0


def _parse_feed(xml_bytes: bytes, source: str, track: str) -> List[Dict[str, Any]]:
    """解析 RSS 2.0 / Atom，取每个 entry 的 title/link/time。坏 XML → []（不抛）。"""
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    # RSS: rss/channel/item；Atom: feed/entry
    items = root.iter()
    for it in items:
        if _local(it.tag) not in ("item", "entry"):
            continue
        title, link, pub = "", "", ""
        for ch in it:
            tag = _local(ch.tag)
            if tag == "title" and not title:
                title = _strip_html(ch.text or "")
            elif tag == "link" and not link:
                link = (ch.get("href") or ch.text or "").strip()
            elif tag in ("pubDate", "published", "updated") and not pub:
                pub = (ch.text or "").strip()
        if title and link:
            out.append({"title": title, "url": link, "ts": _parse_time(pub),
                        "source": source, "track": track})
    return out[:PER_SOURCE]


def _fetch_one(src: Tuple[str, str, str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    name, track, url = src
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
        return _parse_feed(body, name, track), None
    except Exception as e:
        return [], f"{name}: {type(e).__name__}"


# ===========================================================================
# 去重 + story 合并（多源同稿 → 一条，记 src_n）
# ===========================================================================
def merge_stories(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同 URL 或近 48h 同标题 → 合成一条 story，记 src_n/sources。

    src_n 是「几家在报」——给 LLM 做共识判断、给权重做加成。
    返回按时间倒序的 story 列表。
    """
    items = [x for x in items if x.get("title") and x.get("url")]
    items.sort(key=lambda x: x["ts"], reverse=True)
    by_url: Dict[str, Dict[str, Any]] = {}
    stories: List[Dict[str, Any]] = []
    for x in items:
        u = _norm_url(x["url"])
        if u and u in by_url:
            s = by_url[u]
            _bump(s, x)
            continue
        nt = _norm_title(x["title"])
        hit = None
        for s in stories:                       # ponytail: O(n²) 扫描，源条目量小够用
            if s["_nt"] == nt and abs(s["ts"] - x["ts"]) <= DUP_TITLE_WINDOW_S:
                hit = s
                break
        if hit:
            _bump(hit, x)
            if u:
                by_url[u] = hit
            continue
        s = {"title": x["title"], "url": x["url"], "ts": x["ts"],
             "track": x["track"], "src_n": 1, "sources": [x["source"]], "_nt": nt}
        stories.append(s)
        if u:
            by_url[u] = s
    for s in stories:
        s.pop("_nt", None)
    return stories


def _bump(s: Dict[str, Any], x: Dict[str, Any]) -> None:
    s["src_n"] += 1
    if x["source"] not in s["sources"]:
        s["sources"].append(x["source"])


def src_weight(src_n: int) -> float:
    """源数权重（亚线性）：w = 1 + 0.35*log2(src_n)。w(1)=1, w(3)=1.55, w(10)=2.16。"""
    n = max(int(src_n or 1), 1)
    return round(1.0 + SRC_WEIGHT_BETA * math.log2(n), 3)


# ===========================================================================
# 聚合到板块
# ===========================================================================
def aggregate(stories: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按赛道→板块分组，每条附 src_weight。返回 {板块: [story...]}（组内时间倒序）。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in stories:
        sec = TRACK_TO_SECTOR.get(s.get("track", ""), "科技AI")
        s["sector"] = sec
        s["weight"] = src_weight(s["src_n"])
        groups.setdefault(sec, []).append(s)
    for v in groups.values():
        v.sort(key=lambda x: x["ts"], reverse=True)
    return groups


# ===========================================================================
# 抓取入口
# ===========================================================================
def fetch_all(workers: int = 12) -> Dict[str, Any]:
    """并发抓全部源 → 过滤近 RECENT_HOURS + 红线 → story 合并 → 按板块聚合。"""
    cutoff = time.time() - RECENT_HOURS * 3600
    all_items: List[Dict[str, Any]] = []
    ok_src, err = 0, []
    with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for got, e in ex.map(_fetch_one, SOURCES):
            if e:
                err.append(e)
            else:
                ok_src += 1
                all_items.extend(got)
    fresh = [x for x in all_items
             if (not x["ts"] or x["ts"] >= cutoff) and not REDLINE.search(x["title"])]
    stories = merge_stories(fresh)
    groups = aggregate(stories)
    return {
        "groups": groups,
        "meta": {"sources_total": len(SOURCES), "sources_ok": ok_src,
                 "items_raw": len(all_items), "stories": len(stories),
                 "recent_hours": RECENT_HOURS, "errors": err[:20],
                 "fetched_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    }


# ===========================================================================
# AI 提炼（每板块 3~5 条中文要点 + 源数上下文 + 溯源）
# ===========================================================================
_SYS = """你是中文产业资讯分析助手。给你某个板块最近的海外/权威源新闻（每条带源数与序号），
请提炼 3~5 条中文「今日要点」，并给每条新闻一个简洁中文标题翻译。
仅输出 JSON：
{"points":[{"t":"要点","refs":[0,2]}],"items":[{"i":0,"zh":"中文标题"}]}

规则（违反会被代码拒绝）：
- 要点每条 ≤40 字，聚合同类、突出数字/公司/趋势，客观陈述；refs 标注主要来自哪几条
  （序号，至少 1 个，用于溯源原文）；
- 括号里的「N源」表示有几家媒体在报同一条——**N 大的优先**（共识级信号），
  但**绝不要在要点正文里写「N源/多家报道/来源：6」这类元信息**，直接陈述事实本身；
- 中文标题翻译简洁准确；原文已是中文则原样返回；
- 只能基于给的内容，禁止编造。"""


def _prompt_block(sector: str, stories: List[Dict[str, Any]], topn: int = 16) -> str:
    lines = [f"板块: {sector}", "新闻:"]
    for i, s in enumerate(stories[:topn]):
        tag = f"[{s['src_n']}源]" if s["src_n"] > 1 else ""
        lines.append(f"{i}. {tag}{s['title']} （{s['sources'][0]}）")
    return "\n".join(lines)


def _extract_json(s: str) -> Optional[dict]:
    if not s:
        return None
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(s[a:b + 1])
    except Exception:
        obj = None
    if isinstance(obj, dict):
        return obj
    # LLM 偶发在 max_tokens 处截断（JSON 未闭合）——只救 points 这个关键数组：
    # 逐条贪婪匹配完整的 {…} 对象，能补多少补多少，补不出就交给上层重试。
    pts = []
    for m in re.finditer(r'\{\s*"t"\s*:\s*"(.*?)"\s*,\s*"refs"\s*:\s*\[([0-9,\s]*)\]', s[a:], re.S):
        t = m.group(1).strip()
        if t:
            refs = [int(x) for x in m.group(2).split(",") if x.strip().isdigit()]
            pts.append({"t": t, "refs": refs})
    return {"points": pts, "items": []} if pts else None


def digest_sector(sector: str, stories: List[Dict[str, Any]],
                  override: Optional[Dict[str, Any]] = None,
                  attempts: int = 2) -> Dict[str, Any]:
    """对单个板块调 LLM 产要点。返回 {points:[{t,url}], items:[{zh}]}；失败返回空。

    重试 attempts 次：LLM 偶发截断/空返回（实测 ~1/8 概率），多试一次显著降低空窗。
    """
    if not stories:
        return {"points": [], "items": []}
    import llm_client
    topn = 16
    user = _prompt_block(sector, stories, topn)
    d = None
    for _ in range(max(attempts, 1)):
        try:
            raw = llm_client.chat([{"role": "system", "content": _SYS},
                                   {"role": "user", "content": user}],
                                  max_tokens=1500, temperature=0.3, override=override)
        except Exception:
            continue
        d = _extract_json(raw)
        if d and (d.get("points") or d.get("items")):
            break
        d = None
    if not d:
        return {"points": [], "items": []}
    shown = stories[:topn]
    points = []
    for p in d.get("points", [])[:5]:
        if not isinstance(p, dict):
            continue
        t = (p.get("t") or "").strip()
        if not t or len(t) > 60:
            continue
        url = ""
        for r in p.get("refs", []):
            if isinstance(r, int) and 0 <= r < len(shown):
                url = shown[r]["url"]
                break
        points.append({"t": t, "url": url})
    return {"points": points, "items": d.get("items", []) if isinstance(d.get("items"), list) else []}


def digest(groups: Dict[str, List[Dict[str, Any]]],
           override: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """逐板块提炼（线性，板块数少；失败单板块不影响其它）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for sec, stories in groups.items():
        try:
            out[sec] = digest_sector(sec, stories, override=override)
        except Exception as e:
            out[sec] = {"points": [], "items": [], "error": type(e).__name__}
    return out


# ===========================================================================
# 每日任务 + 落盘
# ===========================================================================
def run_daily(force: bool = False, do_ai: bool = True) -> Dict[str, Any]:
    """抓取 → 聚合 → （可选）AI 提炼 → 落盘。返回写出的产物概要。"""
    t0 = time.time()
    r = fetch_all()
    groups = r["groups"]
    digests: Dict[str, Any] = {}
    if do_ai:
        digests = digest(groups)
    payload = {
        "sectors": [
            {"sector": sec,
             "count": len(stories),
             "points": digests.get(sec, {}).get("points", []),
             "sys_n": sum(s["src_n"] - 1 for s in stories),
             "items": [{"title": (digests.get(sec, {}).get("items") or [{}])[i].get("zh")
                        if i < len(digests.get(sec, {}).get("items") or []) else "",
                        "orig": s["title"], "url": s["url"], "ts": s["ts"],
                        "src_n": s["src_n"], "sources": s["sources"][:4],
                        "weight": s["weight"]}
                       for i, s in enumerate(stories[:16])]}
            for sec, stories in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ],
        "meta": dict(r["meta"], ai=bool(do_ai), elapsed_s=round(time.time() - t0, 1)),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_FILE)           # 原子替换：读端永远看到完整文件
    return payload


def load() -> Optional[Dict[str, Any]]:
    """读落盘产物（供 API）。文件不存在/坏 → None。"""
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ===========================================================================
# 每日调度（daemon 线程，抄 sector_audit 模式）
# ===========================================================================
_thread = None
_stop = None
_K_TRY = "GLOBAL_SIGNAL_TRY"
_K_OK = "GLOBAL_SIGNAL_OK"


def _at() -> str:
    try:
        import config
        return str(config.get("TICK_GLOBAL_SIGNAL_AT") or "08:30")
    except Exception:
        return "08:30"


def _ai_on() -> bool:
    try:
        import config
        v = config.get("TICK_GLOBAL_SIGNAL_AI")
        return str(v).lower() not in ("0", "false", "off", "no", "")
    except Exception:
        return True


def start_daily(hours_unused: int = 0) -> bool:
    """拉起每日抓取线程（daemon）。重复调用只启动一次。"""
    global _thread, _stop
    import threading
    if _thread is not None and _thread.is_alive():
        return False
    _stop = threading.Event()

    def _loop():
        last = ""
        while not _stop.is_set():
            try:
                import store as _st
                today = _dt.date.today().isoformat()
                if last != today and _st.meta_get(_K_TRY, "") == today:
                    last = today
                now = _dt.datetime.now()
                hh, mm = (int(x) for x in _at().split(":"))
                if (now.hour, now.minute) >= (hh, mm) and last != today:
                    last = today
                    _st.meta_set(_K_TRY, today)
                    if _st.meta_get(_K_OK, "") == today:
                        print("[global-signal] 今天已抓取过，跳过")
                    else:
                        p = run_daily(do_ai=_ai_on())
                        _st.meta_set(_K_OK, today)
                        print(f"[global-signal] 完成：板块 {len(p['sectors'])}，"
                              f"故事 {p['meta']['stories']}，耗时 {p['meta']['elapsed_s']}s")
            except Exception as e:
                print(f"[global-signal] 本轮出错（下轮重试）：{e}")
            _stop.wait(60)

    _thread = threading.Thread(target=_loop, name="global-signal", daemon=True)
    _thread.start()
    return True


# ===========================================================================
# 自检
# ===========================================================================
def selfcheck() -> int:
    """离线自检：URL/标题归一化 + story 合并 + 源数权重 + 板块映射 + prompt（不联网）。"""
    fails = 0

    def ok(label, cond, detail=""):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}" + (f" | {detail}" if detail else ""))
        if not cond:
            fails += 1

    # URL 归一化：剥跟踪参数、去锚点，保留文章 id
    ok("_norm_url：剥 utm 与锚点",
       _norm_url("https://x.com/a?utm_source=y#frag") == "https://x.com/a")
    ok("_norm_url：保留文章 id query",
       "id=1" in _norm_url("https://x.com/a?id=1&utm_medium=z"))
    ok("_norm_url：畸形 URL 不抛", isinstance(_norm_url("https://[broken"), str))

    ok("_norm_title：去标点空白大小写",
       _norm_title("Fed Holds Rates!") == _norm_title("fed holds rates"))
    ok("_parse_time：RFC822 可解析", _parse_time("Mon, 13 Oct 2025 10:00:00 GMT") > 0)
    ok("_parse_time：坏值 → 0", _parse_time("nonsense") == 0.0)

    # story 合并：3 家转同标题 → 1 条 src_n=3
    now = time.time()
    items = [
        {"title": "Fed holds rates steady", "url": "https://a.com/1", "ts": now, "source": "Reuters", "track": "macro"},
        {"title": "Fed holds rates steady", "url": "https://b.com/x", "ts": now - 3600, "source": "Bloomberg", "track": "macro"},
        {"title": "FED HOLDS RATES STEADY!", "url": "https://c.com/y", "ts": now - 7200, "source": "CNBC", "track": "macro"},
        {"title": "Other unrelated news", "url": "https://d.com/z", "ts": now, "source": "X", "track": "tech"},
    ]
    st = merge_stories(items)
    fed = [s for s in st if "Fed" in s["title"]]
    ok("merge_stories：3 家同稿合成 1 条 src_n=3", len(fed) == 1 and fed[0]["src_n"] == 3,
       f"{len(fed)} 条, src_n={fed[0]['src_n'] if fed else '?'}")
    ok("merge_stories：sources 去重记 3 家", len(set(fed[0]["sources"])) == 3 if fed else False)
    ok("merge_stories：不同标题不合并", any("Other" in s["title"] for s in st))

    # 标题含时间窗：同标题但超 48h 不合并
    st2 = merge_stories([
        {"title": "Weekly roundup", "url": "https://a.com/w1", "ts": now, "source": "A", "track": "tech"},
        {"title": "Weekly roundup", "url": "https://a.com/w2", "ts": now - 5 * 86400, "source": "A", "track": "tech"},
    ])
    ok("merge_stories：超 48h 同标题不合（防固定栏目误合）", len(st2) == 2, len(st2))

    # URL 相同则合并（即使标题略有差别）
    st3 = merge_stories([
        {"title": "Title A", "url": "https://a.com/same", "ts": now, "source": "A", "track": "ai"},
        {"title": "Title B changed", "url": "https://a.com/same?utm_source=x", "ts": now, "source": "B", "track": "ai"},
    ])
    ok("merge_stories：同 URL（剥跟踪参数）合并", len(st3) == 1, len(st3))

    ok("src_weight：w(1)=1.0", src_weight(1) == 1.0)
    ok("src_weight：w(10)≈2.16 亚线性", abs(src_weight(10) - 2.16) < 0.02, src_weight(10))
    ok("src_weight：w(3)<w(10) 单调", src_weight(3) < src_weight(10))

    groups = aggregate(merge_stories(items))
    ok("aggregate：赛道映射到板块", "金融" in groups and "科技AI" in groups, list(groups.keys()))
    ok("aggregate：每条附 weight", all("weight" in s for v in groups.values() for s in v))
    ok("TRACK_TO_SECTOR：12 赛道全覆盖", len(TRACK_TO_SECTOR) == 12)
    ok("SOURCES：≥60 源且三元组完整",
       len(SOURCES) >= 60 and all(len(s) == 3 and s[2].startswith("http") for s in SOURCES),
       f"{len(SOURCES)} 源")
    ok("REDLINE：命中博彩丢弃", bool(REDLINE.search("best 赌博 site")))

    # digest：假 chat 正常路径 + 坏 JSON 降级
    import llm_client
    _orig = llm_client.chat
    try:
        good = {"points": [{"t": "美联储维持利率不变", "refs": [0]}],
                "items": [{"i": 0, "zh": "美联储维持利率"}]}
        llm_client.chat = lambda *a, **k: "```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
        d = digest_sector("金融", groups["金融"])
        ok("digest_sector：正常 → 要点带溯源 URL", d["points"] and d["points"][0]["url"])
        llm_client.chat = lambda *a, **k: "不是 JSON"
        ok("digest_sector：坏输出 → 空（降级）",
           digest_sector("金融", groups["金融"])["points"] == [])
        llm_client.chat = _orig
        ok("_prompt_block：多源带 [N源] 标注",
           "[3源]" in _prompt_block("金融", groups["金融"]))
    finally:
        llm_client.chat = _orig

    # 截断容错：max_tokens 处 JSON 未闭合，仍能从 points 里救回完整条目
    trunc = '{"points":[{"t":"美联储按兵不动","refs":[0]},{"t":"OPEC减产","refs":[1,2]}],"items":[{"i":0,"zh":"未闭'
    d = _extract_json(trunc)
    ok("_extract_json：截断的 JSON 仍救回 points", d and len(d["points"]) == 2,
       d["points"] if d else None)
    ok("_extract_json：彻底非 JSON 返回 None", _extract_json("hello world") is None)

    print(f"global_signal selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


if __name__ == "__main__":
    import sys
    if "--run" in sys.argv:
        p = run_daily(do_ai="--ai" in sys.argv)
        m = p["meta"]
        print(f"抓取完成：源 {m['sources_ok']}/{m['sources_total']}，"
              f"原始 {m['items_raw']} 条 → 故事 {m['stories']} 条，"
              f"板块 {len(p['sectors'])}，耗时 {m['elapsed_s']}s")
        for s in p["sectors"]:
            pts = len(s["points"])
            print(f"  {s['sector']}: {s['count']} 条故事, {pts} 条要点")
        if m["errors"]:
            print(f"  失败源 {len(m['errors'])}: {m['errors'][:5]}")
        sys.exit(0)
    sys.exit(1 if selfcheck() else 0)
