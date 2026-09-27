#!/usr/bin/env python3
"""#88 双源快讯流 —— 数据源稳定性探测（动手写代码前的先决条件）。

探测两个免费源，各连续 10 轮：
  · 财联社电报   https://www.cls.cn/telegraph        （HTML 解析，go-stock 同款）
  · 新浪 7x24    https://zhibo.sina.com.cn/api/zhibo/feed （JSONP 剥壳）

统计：成功率 / 延迟 / 条数 / 字段完整度 / 红条命中 / 两轮间增量（去重键验证）。
只读探测，不碰主服务、不落库。

用法：python3 tests/probe_news_sources.py [轮数=10]
"""
import json
import re
import statistics
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def probe_cls_once(timeout=10):
    """财联社电报单次抓取。返回 (ok, latency, items)。item: {time,content,red}"""
    t0 = time.time()
    r = requests.get(
        "https://www.cls.cn/telegraph", timeout=timeout,
        headers={"Referer": "https://www.cls.cn/", "User-Agent": UA})
    lat = time.time() - t0
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    boxes = soup.select(".telegraph-content-box")
    items = []
    for box in boxes:
        spans = box.find_all("span")
        # go-stock：两个 span（时间 + 内容）；红条 class c-de0422
        if len(spans) >= 2:
            content = spans[-1].get_text(" ", strip=True)
            tm = spans[0].get_text(strip=True)
            red = any("c-de0422" in (s.get("class") or []) for s in spans)
            items.append({"time": tm, "content": content, "red": red,
                          "title": content[:40]})
    return True, lat, items


def probe_sina_once(timeout=10):
    """新浪 7x24 单次抓取（JSONP 剥壳）。返回 (ok, latency, items)。"""
    t0 = time.time()
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?callback=callback"
           "&page=1&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1"
           f"&pagesize=20&id=4161089&type=0&_={int(time.time()*1000)}")
    r = requests.get(url, timeout=timeout,
                     headers={"Referer": "https://finance.sina.com.cn",
                              "User-Agent": UA})
    lat = time.time() - t0
    r.raise_for_status()
    js = r.text
    js = js.replace("try{callback(", "var data=").replace(");}catch(e){};", ";")
    m = re.search(r"var data=(\{.*\});?\s*$", js, re.S)
    if not m:
        # 宽松兜底：截取第一个 { 到最后一个 }
        i, j = js.find("{"), js.rfind("}")
        if i < 0 or j <= i:
            raise ValueError(f"JSONP 剥壳失败：{js[:120]!r}")
        payload = js[i:j + 1]
    else:
        payload = m.group(1)
    d = json.loads(payload)
    feed = (d.get("result", {}).get("data", {}).get("feed") or {})
    raw = feed.get("list") or []
    items = []
    for x in raw:
        rich = x.get("rich_text") or ""
        items.append({
            "time": (x.get("create_time") or "")[:16],
            "content": rich,
            "red": str(x.get("ext")) == "1" or "red" in str(x.get("style", "")),
            "title": rich[:40],
            "id": x.get("id"),
        })
    return True, lat, items


def run_probe(name, fn, rounds):
    print(f"\n{'═' * 62}\n{name}  ·  {rounds} 轮\n{'═' * 62}")
    oks, lats, counts, red_n, empty_n = 0, [], [], 0, 0
    errs = {}
    prev_ids, inc_history = None, []
    for i in range(rounds):
        try:
            ok, lat, items = fn()
            oks += 1
            lats.append(lat * 1000)
            counts.append(len(items))
            red_n += sum(1 for x in items if x["red"])
            if not items:
                empty_n += 1
            ids = [x.get("id") or x.get("time") + x["content"][:20]
                   for x in items]
            if prev_ids is not None:
                fresh = [x for x in ids if x not in prev_ids]
                inc_history.append(len(fresh))
            prev_ids = set(ids)
            has_c = sum(1 for x in items if len(x["content"]) >= 10)
            print(f"  [{i+1:>2}] ok {lat*1000:6.0f}ms  {len(items):>3} 条 "
                  f"(内容完整 {has_c:>3})")
        except Exception as e:
            key = f"{type(e).__name__}: {str(e)[:60]}"
            errs[key] = errs.get(key, 0) + 1
            print(f"  [{i+1:>2}] ✗ {key}")
        if i < rounds - 1:
            time.sleep(3)
    print(f"\n  成功率：{oks}/{rounds}")
    if lats:
        lats.sort()
        print(f"  延迟  ：p50 {lats[len(lats)//2]:.0f}ms · "
              f"p95 {lats[int(len(lats)*0.95)-1]:.0f}ms · "
              f"max {lats[-1]:.0f}ms")
        print(f"  条数  ：min {min(counts)} / med {statistics.median(counts):.0f}"
              f" / max {max(counts)}")
        print(f"  红条  ：累计命中 {red_n} · 空轮 {empty_n}")
        if inc_history:
            print(f"  两轮间新增（去重键稳定性）：{inc_history} "
                  f"（非 0 说明 id 键有效；恒 0 需检查时间戳键）")
    if errs:
        print(f"  错误分布：{errs}")
    return {"name": name, "oks": oks, "rounds": rounds, "lats": lats,
            "counts": counts, "errs": errs}


def main():
    print(f"#88 数据源稳定性探测 · {ROUNDS} 轮 × 2 源 · 间隔 3s")
    r1 = run_probe("财联社电报（HTML .telegraph-content-box）", probe_cls_once, ROUNDS)
    r2 = run_probe("新浪 7x24（JSONP zhibo_id=152）", probe_sina_once, ROUNDS)

    print(f"\n{'═' * 62}\n结论\n{'═' * 62}")
    for r in (r1, r2):
        rate = r["oks"] / r["rounds"]
        verdict = ("✅ 稳定" if rate >= 0.9 and r["lats"] and
                   r["lats"][int(len(r["lats"]) * 0.95) - 1] < 5000
                   else "⚠ 需降级兜底" if rate >= 0.5 else "✗ 不可用")
        print(f"  {verdict}  {r['name']}：成功率 {rate:.0%}")
    # 样例
    try:
        _, _, s = probe_sina_once()
        print("\n新浪样例（最新 3 条）：")
        for x in s[:3]:
            print(f"    [{x['time']}] {x['content'][:50]}")
    except Exception as e:
        print(f"新浪样例取失败：{e}")
    try:
        _, _, s = probe_cls_once()
        print("财联社样例（最新 3 条）：")
        for x in s[:3]:
            print(f"    [{x['time']}{' 红条' if x['red'] else ''}] {x['content'][:50]}")
    except Exception as e:
        print(f"财联社样例取失败：{e}")


if __name__ == "__main__":
    main()
