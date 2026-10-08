"""板块归因缺口分析：加载 24h 滚动快讯，统计未归类条目并打印样本。
用于驱动 SECTOR_KEYWORDS 关键词扩展与板块级利空利好词表建设。
"""
from __future__ import annotations
import collections
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import newsfeed as nf
import sector_sentiment as ss
import stock_sector_map as ssm


def main():
    nf.ensure_history_loaded()
    items = nf.get_sector_window(24 * 3600)
    print(f"窗口内快讯总数: {len(items)}")

    unclassified = []
    classified = []
    sec_counter = collections.Counter()
    for it in items:
        content = it.get("content", "") or ""
        secs = ssm.sectors_for_stocks(it.get("stocks")) or ss.map_sectors(content)
        if secs:
            classified.append(it)
            for s in secs:
                sec_counter[s] += 1
        else:
            unclassified.append(it)

    print(f"已归类: {len(classified)} ({len(classified)/max(1,len(items)):.0%})")
    print(f"未归类: {len(unclassified)} ({len(unclassified)/max(1,len(items)):.0%})")
    print("\n已归类板块分布:")
    for s, c in sec_counter.most_common():
        print(f"  {s:8s} {c}")

    print(f"\n未归类样本（前 80 条）:")
    for it in unclassified[:80]:
        print(f"  - {it.get('content','')[:70]}")

    # 高频词提示：从未归类里抽中文 2-4 字潜在板块词
    print(f"\n未归类关键词频数（粗略，按已有关键词反查对照用）:")
    # 直接统计未归类内容里出现的已知行业词，辅助判断漏网
    pass


if __name__ == "__main__":
    main()
