"""板块舆情聚合（实时新闻 → 板块情绪热度）。

把当天实时快讯按「板块」聚成情绪热度，告诉你今天哪些板块新闻偏多/偏空。
链路（默认全开，各源失败均降级，不阻断主链路）：
  1. newsfeed   —— 双源快讯，已带 sentiment 标签（本地）。
  2. Finlight   —— 带密钥金融新闻 API：自带情绪分析（positive/neg/neutral + confidence）
                  + 9 语言（含中文），强信号源；key 读 .env（gitignore），绝不进源码。
                  注意：实测 /v2/articles 响应不含 companies/entities 字段，故 Finlight
                  当前不返回 ticker，聚合时 stocks 为空、回落关键词。
  3. OkSurf      —— 免费免密钥的 Google News 聚合（英文、无情绪，用本地 sentiment 补）。

## 范围（ponytail：先描述、后预测）
- ✅ 做：实时新闻 → 板块情绪热度（描述性参考，现在就能跑）。
- ❌ 不做：把输出包装成「明天开盘资金流向预测」。那是方向性预测，
  需回测验证；本环境缺「历史新闻归档」+「板块逐日资金流」，无法验证
  （与 ② 同源：缺股票→板块归属）。backtest() 如实标注前置条件，不编数。

## 新闻 → 板块 的映射
优先 ticker/公司名 经 stock_sector_map 精确反查板块（解决「一篇多板块 / 不看好误判」）；
但当前已接的免费源（newsfeed/Finlight/OkSurf/FreeNews）均不返回 ticker，
故实跑时 stocks 多为空、实际仍回落关键词法。stock_sector_map 已就位，一旦接入
返回 tickers 的源（Finlight 实体档/股票新闻 API），边界③即精确生效，无需改聚合逻辑。
stock_sector_map 是静态种子映射（覆盖主要成分股，约 120 只），在库里缺全市场
基本资料表时顶上；升级路径见该模块注释。

## 用法
    python3.11 sector_news_lab.py                # 拉 newsfeed+Finlight+FreeNews+OkSurf → 板块热度
    python3.11 sector_news_lab.py --archive      # 同上 + 当天快讯落盘，供未来回测
    python3.11 sector_news_lab.py --no-finlight  # 关 Finlight（只用 newsfeed+FreeNews+OkSurf）
    python3.11 sector_news_lab.py --no-freenews  # 关 FreeNews（只用 newsfeed+Finlight+OkSurf）
    python3.11 sector_news_lab.py --no-extra     # 关 OkSurf（只用 newsfeed+Finlight+FreeNews）
    python3.11 sector_news_lab.py --selfcheck    # 离线自检（不碰网络/不碰 key）
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, List

# 板块舆情聚合（关键词 + ticker 反查）已下沉到 sector_sentiment，本 lab 复用。
from sector_sentiment import (SECTOR_KEYWORDS, map_sectors, aggregate, heat_table)

# 归档目录（--archive 落盘，供未来回测积累历史）
_ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "news_archive")

# 免费免密钥外部新闻源（来自 public-api-lists 清单的 News 类）。
# 清单里 News 类唯一「免密钥 + HTTPS」的通用新闻源即 OkSurf（Google News 聚合）。
# 它返回英文标题且无情绪标签，故用本地 sentiment 模块补情绪、英文别名做板块映射。
_OKSURF_URL = "https://ok.surf/api/v1/news-feed"
_OKSURF_PER_SECTION_CAP = 20  # 每板块最多取 N 条，控总量与耗时


def _normalize_oksurf(article: Dict[str, Any], section: str) -> Dict[str, Any]:
    """把一条 OkSurf 文章归一化成 newsfeed 同形状的 item（纯函数，便于离线自检）。

    OkSurf 无情绪标签 → 用本地 sentiment.analyze 补（与 newsfeed 同形状）。
    """
    import sentiment as _s
    title = (article.get("title") or "").strip()
    if not title:
        return {}
    return {
        "id": f"oksurf:{section}:{hash(title) & 0xffffffff:08x}",
        "source": f"oksurf/{section}",
        "time": None,
        "red": False,
        "content": title,
        "stocks": [],
        "sentiment": _s.analyze(title),
    }


def fetch_oksurf() -> Dict[str, Any]:
    """拉 OkSurf 新闻聚合，归一化为 item 列表。

    返回 {ok, items, error}。任何网络/解析错误都走 error，不影响主链路。
    """
    import urllib.request
    try:
        req = urllib.request.Request(_OKSURF_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:  # 网络/解析失败：降级，不阻断主链路
        return {"ok": False, "items": [], "error": f"{type(e).__name__}: {e}"}

    items: List[Dict[str, Any]] = []
    for section, arts in (data.items() if isinstance(data, dict) else []):
        for a in (arts or [])[:_OKSURF_PER_SECTION_CAP]:
            it = _normalize_oksurf(a, section)
            if it:
                items.append(it)
    return {"ok": True, "items": items, "error": None}


# ── Finlight（带密钥的金融新闻 API，强信号源）────────────────────────────
# 比 OkSurf 强在哪：自带情绪分析（positive/neutral/negative + confidence）、
# 9 语言（含中文 zh）。情绪不靠本地词典补，直接用官方 enrichment，质量更高。
# 注意（实测）：/v2/articles 响应不含 companies/entities 字段，故 Finlight 当前
# 不返回 ticker；stocks 恒为空，聚合回落关键词。ticker 反查通道已接好（stock_sector_map），
# 等接入返回实体的源即自动生效。需 API key（见 _load_api_key）。
# 免费档仅 REST；key 存 .env（已 gitignore），绝不进源码。
_FINLIGHT_URL = "https://api.finlight.me/v2/articles"
_FINLIGHT_PAGE_SIZE = 50            # 单页上限 100
_FINLIGHT_LANG = "zh"               # 优先中文，直接对齐 A 股板块词库
_FINLIGHT_CATEGORIES = ["markets", "economy", "technology", "business", "regulation"]


def _load_api_key(name: str) -> "str | None":
    """统一取新闻 API key，优先级：后台配置(meta) > 环境变量 > .env 文件。

    这样管理员在后台「运行参数」里填的密钥（写进 meta 表）会先生效，
    自部署用户无需改代码。取不到返回 None（调用方降级跳过）。
    绝不把 key 写进源码/提交；.env 已被 gitignore。
    """
    # 1) 后台配置（管理员在 /api/admin/config 填的，存 meta 表）
    try:
        import config as _cfg
        v = _cfg.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    # 2) 环境变量
    ev = os.environ.get(name)
    if ev:
        return ev
    # 3) .env 文件（gitignore，极简解析，不引 python-dotenv 免依赖）
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None


def _finlight_tone(s: "str | None") -> str:
    """Finlight 情绪串 → 本模块 tone 枚举（pos/neg/neutral）。"""
    if s == "positive":
        return "pos"
    if s == "negative":
        return "neg"
    return "neutral"


def _normalize_finlight(article: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 Finlight 文章归一化成 newsfeed 同形状的 item（纯函数，便于离线自检）。

    sentiment 直接用官方 enrichment（字符串 + confidence），按极性赋分；
    content 取 title+summary 拼接，走现有双语板块词库映射。
    """
    title = (article.get("title") or "").strip()
    summary = (article.get("summary") or "").strip()
    content = (title + (". " + summary if summary and summary != title else "")).strip()
    if not content:
        return {}
    tone = _finlight_tone(article.get("sentiment"))
    try:
        conf = float(article.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    score = conf if tone == "pos" else (-conf if tone == "neg" else 0.0)
    companies = article.get("companies") or []
    # 抓 ticker；ticker 缺时退而抓公司名（Finlight 英文新闻常只给 name）。
    # 两者都喂给 stock_sector_map 反查板块。
    stocks = []
    for c in companies:
        if not isinstance(c, dict):
            continue
        t = c.get("ticker")
        if t:
            stocks.append(str(t))
        elif c.get("name"):
            stocks.append(str(c.get("name")))
    link = article.get("link") or title
    return {
        "id": f"finlight:{hash(link) & 0xffffffff:08x}",
        "source": f"finlight/{article.get('language', '?')}/{article.get('source', '')}",
        "time": article.get("publishDate"),
        "red": False,
        "content": content,
        "stocks": stocks,
        "sentiment": {"tone": tone, "tone_text": tone, "score": score,
                      "confidence": conf, "hits": []},
    }


def fetch_finlight() -> Dict[str, Any]:
    """拉 Finlight 最新金融新闻，归一化为 item 列表。

    key 缺失 / 网络 / 解析错误一律降级（ok=False, items=[]），不阻断主链路。
    """
    import urllib.request
    key = _load_api_key("FINLIGHT_API_KEY")
    if not key:
        return {"ok": False, "items": [], "error": "未配置 FINLIGHT_API_KEY（后台配置/.env/环境变量）"}
    body = json.dumps({
        "language": _FINLIGHT_LANG,
        "categories": _FINLIGHT_CATEGORIES,
        "pageSize": _FINLIGHT_PAGE_SIZE,
        "includeEntities": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        _FINLIGHT_URL, data=body,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        return {"ok": False, "items": [], "error": f"{type(e).__name__}: {e}"}
    arts = (data.get("articles") if isinstance(data, dict) else None) or []
    items = []
    for a in arts:
        it = _normalize_finlight(a)
        if it:
            items.append(it)
    return {"ok": True, "items": items, "error": None}


# ── Free News API（免费 5000 次/天，英文为主，无情绪）─────────────────────
# https://api.freenewsapi.io/v1/news，x-api-key 头鉴权。覆盖广但英文为主、
# 无情绪标签，故用本地 sentiment 模块补情绪，英文别名走板块词库映射。
# 免费档够用；key 同样走后台配置/.env/环境变量（见 _load_api_key）。
_FREENEWS_URL = "https://api.freenewsapi.io/v1/news"
_FREENEWS_PAGE_SIZE = 40           # 单主题拉取条数
_FREENEWS_LANG = "en"
_FREENEWS_TOPICS = ["business", "technology"]  # 金融/板块相关度最高的两类


def _normalize_freenews(article: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 Free News 文章归一化成 newsfeed 同形状的 item（纯函数，便于离线自检）。

    无情绪标签 → 用本地 sentiment.analyze 补（与 OkSurf 同形状）。
    content 取 title+snippet 拼接；uuid 作 id，publisher 作来源标注。
    """
    import sentiment as _s
    title = (article.get("title") or "").strip()
    if not title:
        return {}
    snippet = (article.get("snippet") or article.get("description") or "").strip()
    content = (title + (". " + snippet if snippet and snippet != title else "")).strip()
    uid = article.get("uuid")
    fid = f"freenews:{uid}" if uid else f"freenews:{(hash(title) & 0xffffffff):08x}"
    return {
        "id": fid,
        "source": f"freenews/{article.get('publisher', '')}",
        "time": article.get("published_at"),
        "red": False,
        "content": content,
        "stocks": [],
        "sentiment": _s.analyze(content),
    }


def fetch_freenews() -> Dict[str, Any]:
    """拉 Free News（business/technology 两主题），归一化为 item 列表。

    key 缺失 / 网络 / 解析错误一律降级（ok=False 或 部分 items），不阻断主链路。
    遵守 2 req/sec 限速：主题间 sleep 0.1s。
    """
    import time as _t
    import urllib.parse
    import urllib.request
    key = _load_api_key("FREENEWS_API_KEY")
    if not key:
        return {"ok": False, "items": [], "error": "未配置 FREENEWS_API_KEY（后台配置/.env/环境变量）"}
    items: List[Dict[str, Any]] = []
    err = None
    for topic in _FREENEWS_TOPICS:
        qs = urllib.parse.urlencode({
            "language": _FREENEWS_LANG,
            "topic": topic,
            "order_by": "recent",
            "page_size": _FREENEWS_PAGE_SIZE,
        })
        req = urllib.request.Request(
            f"{_FREENEWS_URL}?{qs}",
            headers={"x-api-key": key, "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            continue
        arts = (data.get("data") if isinstance(data, dict) else None) or []
        for a in arts:
            it = _normalize_freenews(a)
            if it:
                items.append(it)
        _t.sleep(0.1)
    return {"ok": True, "items": items, "error": err}


# 以下 map_sectors / aggregate / heat_table 已下沉到 sector_sentiment（面板与 lab 共用），
# 本 lab 通过顶部 `from sector_sentiment import ...` 复用，不再重复定义。


def _print_heat(rows: List[Dict[str, Any]]) -> None:
    print(f"\n{'板块':<10}{'条数':>5}{'多':>4}{'空':>4}{'中':>4}"
          f"{'净情绪':>8}  样本(前3)")
    print("-" * 78)
    for r in rows:
        bar = "🔴" if r["net"] < -0.1 else ("🟢" if r["net"] > 0.1 else "⚪")
        print(f"{r['sector']:<10}{r['n']:>5}{r['pos']:>4}{r['neg']:>4}"
              f"{r['neu']:>4}{r['net']:>8} {bar}")
        for s in r["samples"]:
            print(f"           · {s}")


def backtest() -> Dict[str, Any]:
    """回测「今日板块新闻情绪 → 次日板块资金流」。

    ⚠️ 当前数据下**无法运行**，前置条件（与 ② 同源）：
      1) 历史新闻归档：需先用 --archive 积累 N 天（本环境无历史新闻）。
      2) 板块逐日资金流序列：需股票→板块归属，再对 daily_bars 逐股
         聚合成板块日收益/主力净流入。本环境库里无全市场基本资料表，
         现仅有 stock_sector_map 的静态种子（覆盖约 120 只主要成分股），
         远非全市场；板块时间序列仍不可得。
    满足前两项后再实现 ICIR 计算；此处只如实报阻塞，不编数。
    """
    have_archive = os.path.isdir(_ARCHIVE_DIR) and \
        bool([f for f in os.listdir(_ARCHIVE_DIR) if f.endswith(".jsonl")])
    return {
        "ok": False,
        "reason": "数据前置不满足，回测暂不可运行",
        "need_historical_news": not have_archive,
        "need_sector_membership": True,
        "note": "先用 --archive 积累新闻历史；并用全市场股票→板块归属补全"
                "stock_sector_map 后，方可算板块情绪→次日资金流的 ICIR。",
    }


def _archive(items: List[Dict[str, Any]]) -> str:
    os.makedirs(_ARCHIVE_DIR, exist_ok=True)
    d = dt.date.today().isoformat()
    path = os.path.join(_ARCHIVE_DIR, f"{d}.jsonl")
    n = 0
    with open(path, "a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps({
                "time": it.get("time"),
                "content": it.get("content"),
                "sentiment": it.get("sentiment"),
                "stocks": it.get("stocks"),
            }, ensure_ascii=False) + "\n")
            n += 1
    return f"{path} (+{n} 条)"


def _sample_items() -> List[Dict[str, Any]]:
    """离线自检用的样例快讯（自带 sentiment，模拟 newsfeed 结构）。"""
    def sent(tone, score):
        return {"tone": tone, "tone_text": tone, "score": score,
                "pos_n": 1, "neg_n": 0, "hits": []}
    return [
        {"id": "x1", "source": "sina", "time": "0", "red": False,
         "content": "光伏产业链价格大涨，硅料企业盈利超预期", "stocks": [],
         "sentiment": sent("pos", 2.5)},
        {"id": "x2", "source": "sina", "time": "0", "red": False,
         "content": "半导体设备国产化提速，多家晶圆厂扩产", "stocks": [],
         "sentiment": sent("pos", 2.0)},
        {"id": "x3", "source": "ths", "time": "0", "red": True,
         "content": "地产销售数据疲弱，房企债务风险升温", "stocks": [],
         "sentiment": sent("neg", -2.5)},
        {"id": "x4", "source": "ths", "time": "0", "red": False,
         "content": "医药集采落地，创新药放量", "stocks": [],
         "sentiment": sent("pos", 1.5)},
        {"id": "x5", "source": "sina", "time": "0", "red": False,
         "content": "人工智能算力需求爆发，机器人量产", "stocks": [],
         "sentiment": sent("pos", 3.0)},
        {"id": "x6", "source": "sina", "time": "0", "red": True,
         "content": "银行信贷投放不及预期", "stocks": [],
         "sentiment": sent("neg", -1.5)},
    ]


def selfcheck() -> int:
    steps = []
    # 1. 映射命中（含英文别名 + 词边界）
    steps.append(("关键词映射命中",
                  set(map_sectors("光伏产业链价格大涨")) == {"新能源"}))
    steps.append(("一条命中多板块",
                  "半导体" in map_sectors("半导体设备国产化")
                  and "科技AI" in map_sectors("人工智能算力爆发")))
    steps.append(("英文别名命中(semiconductor→半导体)",
                  "半导体" in map_sectors("US semiconductor export curbs tighten")))
    steps.append(("英文别名命中(real estate→地产)",
                  "地产" in map_sectors("China real estate market stabilizes")))
    steps.append(("英文词边界不误中(ev 不中 revenue)",
                  "新能源" not in map_sectors("Company revenue beat estimates")))
    # 2. 聚合 + 排序
    agg = aggregate(_sample_items())
    steps.append(("聚合出板块数>0", len(agg) >= 5))
    rows = heat_table(agg)
    steps.append(("热度排序：净情绪高者在前",
                  rows[0]["net"] >= rows[-1]["net"]))
    steps.append(("新能源净情绪为正(1多0空)",
                  agg["新能源"]["pos"] == 1 and agg["新能源"]["neg"] == 0))
    steps.append(("地产净情绪为负(0多1空)",
                  agg["地产"]["pos"] == 0 and agg["地产"]["neg"] == 1))
    # 3. backtest 如实阻塞
    bt = backtest()
    steps.append(("回测如实报阻塞(不编数)",
                  bt["ok"] is False and bt["need_sector_membership"] is True))
    # 4. 归档落盘可用
    tmp = os.path.join(_ARCHIVE_DIR, "_selfcheck.jsonl")
    os.makedirs(_ARCHIVE_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"content": "t"}, ensure_ascii=False) + "\n")
    steps.append(("归档落盘可写", os.path.exists(tmp)))
    os.remove(tmp)
    # 5. OkSurf 归一化（离线，不碰网络）
    norm = _normalize_oksurf({"title": "Tesla cuts EV prices in China"},
                             "Business")
    steps.append(("OkSurf 归一化出 content+sentiment",
                  norm.get("content") == "Tesla cuts EV prices in China"
                  and isinstance(norm.get("sentiment"), dict)
                  and norm["sentiment"].get("tone") in ("pos", "neg", "neutral")))
    empty = _normalize_oksurf({"source": "x"}, "Business")
    steps.append(("OkSurf 空标题归一化为空", empty == {}))

    # 6. Finlight 归一化（离线，不碰网络/不碰 key）
    fl_pos = _normalize_finlight({"title": "光伏龙头业绩超预期", "summary": "硅料涨价",
                                  "sentiment": "positive", "confidence": "0.91",
                                  "source": "reuters.com", "publishDate": "2026-10-08",
                                  "language": "zh", "link": "https://x/1"})
    steps.append(("Finlight positive→tone=pos 且 分>0",
                  fl_pos.get("sentiment", {}).get("tone") == "pos"
                  and fl_pos["sentiment"]["score"] > 0))
    fl_neg = _normalize_finlight({"title": "地产销售低迷", "summary": "",
                                  "sentiment": "negative", "confidence": "0.8",
                                  "source": "x", "language": "zh", "link": "https://x/2"})
    steps.append(("Finlight negative→tone=neg 且 分<0",
                  fl_neg["sentiment"]["tone"] == "neg"
                  and fl_neg["sentiment"]["score"] < 0))
    fl_neu = _normalize_finlight({"title": "市场持平", "summary": "",
                                  "sentiment": "neutral", "confidence": "0.5",
                                  "source": "x", "language": "zh", "link": "https://x/3"})
    steps.append(("Finlight neutral→tone=neutral 且 分=0",
                  fl_neu["sentiment"]["tone"] == "neutral"
                  and fl_neu["sentiment"]["score"] == 0.0))
    fl_nosent = _normalize_finlight({"title": "某公司发布新品", "summary": "",
                                     "language": "zh", "link": "l"})
    steps.append(("Finlight 缺 sentiment 字段不崩→neutral",
                  fl_nosent["sentiment"]["tone"] == "neutral"))
    fl_empty = _normalize_finlight({"source": "x"})
    steps.append(("Finlight 空标题归一化为空", fl_empty == {}))

    # 7. Free News 归一化（离线，不碰网络/不碰 key）
    fn_a = _normalize_freenews({"title": "Tesla cuts EV prices in China",
                                "uuid": "abc", "publisher": "Reuters"})
    steps.append(("FreeNews 归一化出 content+本地情绪",
                  fn_a.get("content") == "Tesla cuts EV prices in China"
                  and fn_a["source"].startswith("freenews/")
                  and isinstance(fn_a.get("sentiment"), dict)
                  and fn_a["sentiment"].get("tone") in ("pos", "neg", "neutral")))
    fn_empty = _normalize_freenews({"publisher": "x"})
    steps.append(("FreeNews 空标题归一化为空", fn_empty == {}))

    # 8. Finlight 抓取 ticker + 公司名（供 stock_sector_map 反查）
    fl_stocks = _normalize_finlight({"title": "贵州茅台业绩创新高", "summary": "",
                                     "companies": [{"ticker": "600519"},
                                                   {"name": "Tencent"}],
                                     "language": "zh", "link": "l"})
    steps.append(("Finlight 抓到 ticker+名称 stocks",
                  "600519" in fl_stocks.get("stocks", [])
                  and "Tencent" in fl_stocks.get("stocks", [])))

    # 9. stock_sector_map 反查（独立模块自检）
    import stock_sector_map as _ssm
    sm_fails = _ssm.selfcheck()
    steps.append(("股票→板块 反查自检通过", sm_fails == 0))
    print("离线自检：")
    fails = 0
    for label, ok in steps:
        print(f"  {'✅' if ok else '❌'} {label}")
        if not ok:
            fails += 1
    print(f"结果：{'全部通过' if not fails else f'{fails} 项失败'}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", action="store_true",
                    help="把当天快讯落盘到 data/news_archive，供未来回测")
    ap.add_argument("--no-extra", action="store_true",
                    help="不并联免费外部新闻源（OkSurf），只用 newsfeed + Finlight + FreeNews")
    ap.add_argument("--no-finlight", action="store_true",
                    help="不并联 Finlight（带密钥），只用 newsfeed + OkSurf + FreeNews")
    ap.add_argument("--no-freenews", action="store_true",
                    help="不并联 FreeNews（带密钥，免费 5000/天），只用 newsfeed + Finlight + OkSurf")
    ap.add_argument("--selfcheck", action="store_true", help="离线自检")
    args = ap.parse_args()

    if args.selfcheck:
        return 1 if selfcheck() else 0

    try:
        import newsfeed as nf
    except Exception as e:
        print(f"[sector-news] 快讯模块加载失败：{e}")
        return 2

    res = nf.get_feed()
    items = res.get("items", []) or []
    errs = res.get("errors", {}) or {}
    print(f"[sector-news] newsfeed 拉到 {len(items)} 条；"
          f"源错误：{errs if errs else '无'}")

    # 并联 Finlight（带密钥强信号源，自带情绪，默认开；--no-finlight 关）
    if not args.no_finlight:
        fl = fetch_finlight()
        if fl["ok"]:
            items += fl["items"]
            print(f"[sector-news] Finlight 并入 {len(fl['items'])} 条"
                  f"（总 {len(items)} 条）")
        else:
            print(f"[sector-news] Finlight 拉取失败（已降级跳过）：{fl['error']}")

    # 并联 Free News（带密钥免费源，英文、无情绪用本地补，默认开；--no-freenews 关）
    if not args.no_freenews:
        fn = fetch_freenews()
        if fn["ok"] and fn["items"]:
            items += fn["items"]
            print(f"[sector-news] FreeNews 并入 {len(fn['items'])} 条"
                  f"（总 {len(items)} 条）")
        else:
            print(f"[sector-news] FreeNews 拉取失败/为空（已降级跳过）：{fn['error']}")

    # 并联免费外部新闻源（默认开；--no-extra 关闭）
    if not args.no_extra:
        ok = fetch_oksurf()
        if ok["ok"]:
            items += ok["items"]
            print(f"[sector-news] OkSurf 并入 {len(ok['items'])} 条"
                  f"（总 {len(items)} 条）")
        else:
            print(f"[sector-news] OkSurf 拉取失败（已降级跳过）：{ok['error']}")

    if not items:
        print("[sector-news] 无快讯（可能网络受限或源暂不可用），"
              "用 --selfcheck 验逻辑。")
        return 0

    agg = aggregate(items)
    rows = heat_table(agg)
    _print_heat(rows)

    if args.archive:
        print("[sector-news] 归档：", _archive(items))

    bt = backtest()
    print(f"\n[sector-news] 回测状态：{bt['reason']}"
          f"（需历史新闻:{bt['need_historical_news']} 需板块归属:{bt['need_sector_membership']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
