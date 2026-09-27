#!/usr/bin/env python3
"""
「市场榜单 / 市场热点 / 异动监控」三页签合并的静态校验。

合并后的结构约束（改坏了这里会红）：
  1. 只剩一个可见页签 data-tab="rank"；hot/moves 按钮必须存在但 display:none
     （保留是为了让任何 data-tab="hot" 的深链/内联跳转不至于静默报错）。
  2. tab-hot / tab-moves 容器必须已删除；对应 id 不得在 DOM 里重复出现。
  3. 原 19 个内容项（6 榜单 + 7 热点 + 6 异动）必须都能被 showBoard() 触达，
     不允许出现"下拉里没有、按钮也没了"的孤儿功能。
  4. 旧的平铺按钮 data-rank / data-hot / data-mv 必须全部清掉，
     否则会出现新旧两套入口并存、状态不同步。

用法：python3 tests/check_merged_tabs.py [web/index.html]
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "web" / "index.html"

RANK_KEYS = ["gainers", "losers", "amount", "turnover", "cap", "pe"]
HOT_KEYS = ["hotrank", "lhb", "sector", "limitup", "ladder", "break", "anomaly"]
MOVE_KEYS = ["涨停", "炸板", "跌停", "大涨", "大跌", "放量"]

fails: list[str] = []
ok_n = 0


def check(cond: bool, ok: str, bad: str) -> None:
    global ok_n
    if cond:
        ok_n += 1
        print(f"  ✅ {ok}")
    else:
        print(f"  ❌ {bad}")
        fails.append(bad)


def main() -> int:
    if not HTML.exists():
        print(f"找不到 {HTML}")
        return 2
    src = HTML.read_text(encoding="utf-8")

    print(f"文件: {HTML.relative_to(ROOT) if HTML.is_relative_to(ROOT) else HTML}")
    print(f"大小: {len(src):,} 字符\n")

    # ---- 1. 页签按钮 ----
    print("[1] 页签按钮")
    # 只扫 <div class="tabs"> 内部，避免误抓页面别处的 data-tab（如 tabBtnAdmin）
    tabsbox = re.search(r'<div class="tabs">(.*?)</div>', src, re.S)
    check(bool(tabsbox), "找到 .tabs 容器", "未找到 .tabs 容器")
    tb = tabsbox.group(1) if tabsbox else ""
    btn_re = re.compile(r'<button\b([^>]*)>')
    vis_tabs, hid_tabs = [], []
    for m in btn_re.finditer(tb):
        attrs = m.group(1)
        t = re.search(r'data-tab="([a-z]+)"', attrs)
        if not t:
            continue
        (hid_tabs if "display:none" in attrs.replace(" ", "") else vis_tabs).append(t.group(1))
    check(vis_tabs.count("rank") == 1, "rank 是唯一榜首可见页签", f"可见页签异常: {vis_tabs}")
    check("hot" not in vis_tabs, "hot 不再是可见页签", "hot 仍是可见页签")
    check("moves" not in vis_tabs, "moves 不再是可见页签", "moves 仍是可见页签")
    for t in ("hot", "moves"):
        check(t in hid_tabs,
              f"{t} 按钮保留且隐藏（深链兜底）",
              f"{t} 隐藏按钮缺失，data-tab=\"{t}\" 的跳转会报错")


    # ---- 2. 旧容器必须删除 ----
    print("\n[2] 旧容器下线")
    for tid in ("tab-hot", "tab-moves"):
        check(f'id="{tid}"' not in src, f'{tid} 容器已删除', f'{tid} 容器仍存在')
    # 热点/异动的卡片 id 迁移到 rank 页内部
    for cid in ("hotCard", "hotBody", "hotTitle", "movesCard", "movesBody", "movesTitle", "moveCounts"):
        n = len(re.findall(r'id="%s"' % cid, src))
        check(n == 1, f'{cid} 唯一存在', f'{cid} 出现 {n} 次（应为 1 次）')
    check('id="rankCard"' in src, "rankCard 容器存在", "rankCard 缺失（showBoard 无法隐藏榜单卡）")

    # 工具栏必须独立成卡：放进 rankCard 里会在切到热点时被 display:none，
    # 下拉跟着消失 → 用户再也切不回来（曾真实踩过这个坑）
    print("\n[2.1] 工具栏位置（防回归）")
    bar_card = re.search(r'<div class="card" id="rankBarCard">(.*?)</div>\s*</div>', src, re.S)
    check(bool(bar_card), "rankBarCard 独立卡片存在", "rankBarCard 缺失")
    if bar_card:
        check('id="rankSel"' in bar_card.group(1),
              "下拉在 rankBarCard 内", "下拉不在 rankBarCard 内")
    rank_card = re.search(r'<div class="card" id="rankCard">(.*?)(?=<div class="card" id="hotCard">|$)', src, re.S)
    if rank_card:
        check('id="rankSel"' not in rank_card.group(1),
              "rankSel 不在 rankCard 内（不会被连带隐藏）",
              "rankSel 又被塞回 rankCard，切热点后无法切回")
        check('id="rankAll"' not in rank_card.group(1),
              "rankAll 不在 rankCard 内", "rankAll 被塞回 rankCard")

    # ---- 3. 19 个内容项全部可达 ----
    print("\n[3] 内容项可达性（下拉 value 口径）")
    bar = re.search(r"function buildRankBar\(\).*?\n}", src, re.S)
    check(bool(bar), "buildRankBar() 存在", "buildRankBar() 缺失")
    body = bar.group(0) if bar else ""
    check("BOARD_GROUPS" in body, "下拉由 BOARD_GROUPS 驱动", "buildRankBar 未用 BOARD_GROUPS")
    for k in RANK_KEYS:
        check(f"'{k}'" in src, f"榜单 {k} 已声明", f"榜单 {k} 未声明")
    for k in HOT_KEYS:
        check(f"'{k}'" in src, f"热点 {k} 已声明", f"热点 {k} 未声明")
    for k in MOVE_KEYS:
        check(f"'{k}'" in src, f"异动 {k} 已声明", f"异动 {k} 未声明")
    check("optgroup" in body.lower(), "使用原生 optgroup 分组", "未见 optgroup 分组")
    check("showBoard(" in src, "showBoard() 分发函数存在", "showBoard() 缺失")
    # optgroup 无 value 属性：必须靠 dataset.group 标分组
    check("dataset.group" in src, "分组用 dataset.group（不依赖 optgroup.value）",
          "分组依赖 optgroup.value —— HTML 无此属性，展开功能会失效")
    check("function curRankGroup(" in src, "curRankGroup() 存在", "curRankGroup() 缺失")

    # HOT_CHOICES 覆盖全部 7 个热点 key
    hc = re.search(r"var HOT_CHOICES = \[(.*?)\];", src, re.S)
    check(bool(hc), "HOT_CHOICES 常量存在", "HOT_CHOICES 缺失")
    hc_txt = hc.group(1) if hc else ""
    missing = [k for k in HOT_KEYS if f"'{k}'" not in hc_txt]
    check(not missing, "HOT_CHOICES 覆盖 7 项", f"HOT_CHOICES 缺少 {missing}")
    mc = re.search(r"var MOVE_CHOICES = \[(.*?)\];", src, re.S)
    mc_txt = mc.group(1) if mc else ""
    missing_m = [k for k in MOVE_KEYS if f"'{k}'" not in mc_txt]
    check(not missing_m, "MOVE_CHOICES 覆盖 6 项", f"MOVE_CHOICES 缺少 {missing_m}")

    # ---- 4. 旧平铺按钮清干净 ----
    print("\n[4] 旧入口清理")
    for attr in ("data-rank", "data-hot", "data-mv"):
        n = len(re.findall(r'%s=' % attr, src))
        check(n == 0, f"{attr} 已无残留", f"{attr} 仍有 {n} 处残留（新旧入口并存）")

    # ---- 5. 关键的 _cur / 标题联动 ----
    print("\n[5] 状态联动")
    check("function hotLabel(" in src, "hotLabel() 存在（热点标题联动）", "hotLabel() 缺失")
    check("function rankLabel(" in src, "rankLabel() 存在（榜单标题联动）", "rankLabel() 缺失")
    check("MV_LABEL" in src, "MV_LABEL 存在（异动标题联动）", "MV_LABEL 缺失")
    check("function refreshBoard(" in src, "refreshBoard() 存在", "refreshBoard() 缺失")
    check("function syncMvBtns(" in src, "syncMvBtns() 存在", "syncMvBtns() 缺失")
    # RE_LEVEL 不应再有 hot / moves 键
    rel = re.search(r"var RE_LEVEL = \{(.*?)\};", src, re.S)
    rel_txt = rel.group(1) if rel else ""
    check(re.search(r"\bhot\s*:", rel_txt) is None, "RE_LEVEL 已移除 hot 键", "RE_LEVEL 仍有 hot 键（指向不存在的页签）")
    check(re.search(r"\bmoves\s*:", rel_txt) is None, "RE_LEVEL 已移除 moves 键", "RE_LEVEL 仍有 moves 键")

    # ---- 汇总 ----
    print("\n" + "=" * 56)
    if fails:
        print(f"❌ {len(fails)} 项未通过：")
        for f in fails:
            print(f"   · {f}")
        return 1
    print(f"✅ 全部通过（{ok_n} 项检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
