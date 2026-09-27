"""双源快讯流 —— 新浪 7x24 + 同花顺快讯，蒸馏自 go-stock（财联社源已废）。

## 源的选型（2026-09-26 十轮探测实测，tests/probe_news_sources.py 可复跑）

  · 新浪 7x24    100% 成功 · p50 177ms · 20 条/轮 · JSONP 剥壳（go-stock 同款）
  · 同花顺快讯   100% 成功 · p50 129ms · 20 条/轮 · color=2 即重大新闻
  · 华尔街见闻   100% 成功但偶发 7s 长尾 → 只做备选不引入
  · 财联社电报   HTML 已改版成 JS 客户端渲染（抓到 0 条），JSON API
    需要 sha1(md5(...)) 服务端签名，社区公开算法全部 10012 拒签 → 放弃
  · 东财 7x24    np-listapi 返回反爬挑战页（567）→ 放弃

go-stock 原实现抓财联社 HTML 的前提（服务端渲染）已不存在，这属于
蒸馏时必须重新验证的外部依赖——正好是「先测稳定性再动手」的意义。

## 设计

  · 每源归一化成 {id, source, time, content, red}，合并按时间倒序；
  · 去重两级：源内 id 去重 + 跨源内容指纹（同一条快讯两个源都会发）；
  · 30s TTL 缓存（蒸馏报告：11s 级可降为 30s，免费源别薅太狠）；
  · 单源失败不影响另一源——快讯宁可少一个来源也不能整页白屏；
  · 每条自动挂 sentiment（复用 #89 词典情绪，看涨/看跌/中性）。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Dict, List

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")

#: 缓存秒数。快讯 30s 足够新鲜，且两个免费源都别高频薅
TTL = 30

_lock = threading.Lock()
_cache: Dict[str, Any] = {"ts": 0.0, "items": [], "errors": {}, "sources": {}}


def _ts_text(v, fmt="%m-%d %H:%M") -> str:
    """epoch 秒（int/str 都可能）→ 展示文本。脏值原样截断，不抛。"""
    try:
        return time.strftime(fmt, time.localtime(int(v or 0)))
    except Exception:
        return str(v or "")[:16]


def _norm(sid: str, source: str, tm: str, content: str, red: bool = False,
          url: str = "", stocks: list = None) -> Dict[str, Any]:
    """归一化一条快讯。id = 源前缀 + 源内 id（无 id 用内容指纹）。

    stocks: 关联股票 [{symbol: 'sh600010', name: '包钢股份'}]（新浪 ext 提供）。
    """
    content = re.sub(r"\s+", " ", str(content or "")).strip()
    key = str(sid or "").strip() or hashlib.md5(
        (tm + content).encode()).hexdigest()[:16]
    return {"id": f"{source}:{key}", "source": source, "time": tm,
            "content": content, "red": bool(red), "url": url,
            "stocks": stocks or [],
            "fp": hashlib.md5(content[:120].encode()).hexdigest()[:16]}


def fetch_sina(timeout: int = 8) -> List[Dict[str, Any]]:
    """新浪 7x24 全球直播（zhibo_id=152）。JSONP 剥壳照抄 go-stock。"""
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?callback=callback"
           "&page=1&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1"
           f"&pagesize=20&id=4161089&type=0&_={int(time.time() * 1000)}")
    r = requests.get(url, timeout=timeout,
                     headers={"Referer": "https://finance.sina.com.cn",
                              "User-Agent": UA})
    r.raise_for_status()
    js = r.text.replace("try{callback(", "var data=").replace(
        ");}catch(e){};", ";")
    i, j = js.find("{"), js.rfind("}")
    if i < 0 or j <= i:
        raise ValueError(f"JSONP 剥壳失败：{js[:80]!r}")
    d = json.loads(js[i:j + 1])
    raw = ((d.get("result") or {}).get("data") or {}).get("feed") or {}
    out = []
    for x in (raw.get("list") or []):
        # ext 是 JSON 字符串：含关联股票 stocks:[{market,symbol,key}] 与 docurl。
        # ponytail: 关联股票先只做展示与 A 股跳转，不做行情联动
        stocks, docurl = [], ""
        try:
            ext = json.loads(x.get("ext") or "{}")
            stocks = [{"symbol": s.get("symbol"), "name": s.get("key") or ""}
                      for s in (ext.get("stocks") or []) if s.get("symbol")]
            docurl = ext.get("docurl") or ""
        except Exception:
            pass
        # create_time 形如 '2026-09-26 20:39:05'，取 '09-26 20:39'
        ct = str(x.get("create_time") or "")
        out.append(_norm(x.get("id"), "新浪7x24", ct[5:16],
                         x.get("rich_text") or "", red=False,
                         url=docurl, stocks=stocks))
    return out


def fetch_ths(timeout: int = 8) -> List[Dict[str, Any]]:
    """同花顺快讯（10jqka push 接口，无签名）。color=2 即重大新闻。"""
    url = ("https://news.10jqka.com.cn/tapp/news/push/stock/"
           "?page=1&tag=&track=website&pagesize=20")
    r = requests.get(url, timeout=timeout,
                     headers={"Referer": "https://news.10jqka.com.cn/",
                              "User-Agent": UA})
    r.raise_for_status()
    lst = (r.json().get("data") or {}).get("list") or []
    return [_norm(x.get("id"), "同花顺",
                  _ts_text(x.get("ctime")),
                  ((x.get("title") or "") + " " + (x.get("digest") or "")).strip(),
                  red=str(x.get("color")) == "2",
                  url=x.get("url") or "") for x in lst]


def _dedup_merge(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """两级去重 + 时间倒序。跨源同内容保留时间更早（信息更原始）的那条。"""
    seen, out = set(), []
    for x in sorted(items, key=lambda r: r["time"], reverse=True):
        if x["id"] in seen or x["fp"] in seen:
            continue
        seen.add(x["id"])
        seen.add(x["fp"])
        out.append(x)
    return out


def get_feed(force: bool = False) -> Dict[str, Any]:
    """拿当前快讯流（带 TTL 缓存）。返回 {items, sources, errors, ts, cached}。

    线程安全；失败源记录在 errors，items 至少含成功源的数据。
    """
    with _lock:
        if not force and time.time() - _cache["ts"] < TTL and _cache["items"]:
            return {**_cache, "cached": True}

        items, errors, sources = [], {}, {}
        for name, fn in (("新浪7x24", fetch_sina), ("同花顺", fetch_ths)):
            try:
                got = fn()
                sources[name] = len(got)
                items.extend(got)
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {str(e)[:80]}"

        merged = _dedup_merge(items)
        # 复用 #89 词典情绪：单条打分失败不影响整流（tag_notice 同款容错）
        try:
            import sentiment as senti
            for x in merged:
                r = senti.analyze(x["content"])
                x["sentiment"] = {"tone": r["tone"], "tone_text": r["tone_text"],
                                  "score": r["score"]}
        except Exception:
            pass

        _cache.update({"ts": time.time(), "items": merged,
                       "errors": errors, "sources": sources})
        return {**_cache, "cached": False}


def cache_status() -> Dict[str, Any]:
    """缓存占用情况，供后台「缓存统一管理」展示（#97）。纯内存读取，不联网。"""
    with _lock:
        ts = float(_cache.get("ts") or 0)
        items = _cache.get("items") or []
        age = int(time.time() - ts) if ts else None
        return {
            "items": len(items),
            "ttl": TTL,
            "age": age,
            "fresh": age is not None and age < TTL,
            "sources": dict(_cache.get("sources") or {}),
            "errors": dict(_cache.get("errors") or {}),
            "ts": ts,
            "ts_text": _ts_text(ts) if ts else None,
        }


def clear_cache() -> Dict[str, Any]:
    """清空快讯内存缓存。下次打开会重新抓两个源——免费源别薅太狠，慎用。"""
    global _cache
    with _lock:
        had = len(_cache.get("items") or [])
        _cache = {"ts": 0.0, "items": [], "errors": {}, "sources": {}}
    return {"ok": True, "cleared": had}


def selfcheck() -> int:
    """归一化/去重/合并 的纯逻辑真值表 + 一次真实抓取冒烟。"""
    fails = []

    def eq(got, want, label):
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {label}" + ("" if ok else f"：got={got!r} want={want!r}"))
        if not ok:
            fails.append(label)

    print("[1] 归一化")
    x = _norm("", "新浪7x24", "09-26 20:00", "  大涨   大涨\n ")
    eq(x["id"].startswith("新浪7x24:"), True, "无 id → 内容指纹兜底")
    eq(x["content"], "大涨 大涨", "空白归一")
    y = _norm("123", "同花顺", "09-26 20:01", "标题")
    eq(y["id"], "同花顺:123", "有 id → 源前缀+id")

    print("\n[2] 两级去重 + 倒序")
    a = _norm("1", "新浪7x24", "09-26 20:02", "同一条新闻")
    b = _norm("9", "同花顺", "09-26 20:01", "同一条新闻")   # 内容指纹相同
    c = _norm("2", "新浪7x24", "09-26 20:03", "另一条新闻")
    merged = _dedup_merge([a, b, c])
    eq([m["id"] for m in merged], ["新浪7x24:2", "新浪7x24:1"],
       "跨源同内容去重（保留时间早者）+ 时间倒序")
    eq(len(_dedup_merge([a, a.copy()])), 1, "源内 id 重复 → 去 1")

    print("\n[3] 真实抓取冒烟（网络）")
    for name, fn in (("新浪7x24", fetch_sina), ("同花顺", fetch_ths)):
        try:
            got = fn()
            ok = len(got) >= 10 and all(x["content"] for x in got)
            print(f"  {'✅' if ok else '❌'} {name}：{len(got)} 条"
                  + (f" · 红条 {sum(1 for i in got if i['red'])}" if ok else ""))
            if not ok:
                fails.append(f"{name} 冒烟")
        except Exception as e:
            print(f"  ❌ {name}：{type(e).__name__}: {str(e)[:60]}")
            fails.append(f"{name} 冒烟")

    print("\n[4] get_feed 汇总")
    f = get_feed()
    eq(len(f["items"]) > 10, True, f"合并条数 {len(f['items'])}")
    eq(f["sources"].get("新浪7x24", 0) > 0 and f["sources"].get("同花顺", 0) > 0,
       True, f"双源都有数据 {f['sources']}")
    f2 = get_feed()
    eq(f2.get("cached"), True, "30s 内二次取走缓存")
    tones = [x["sentiment"]["tone"] for x in f["items"] if "sentiment" in x]
    eq(len(tones) == len(f["items"]), True, "每条都挂了 sentiment")

    print("\n" + "=" * 46)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(selfcheck())
