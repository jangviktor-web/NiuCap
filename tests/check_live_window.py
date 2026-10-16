"""盘中数据窗口判定验证：mktSession/liveTag/liveNote 在各时段的表现。

跑法：python3 tests/check_live_window.py（服务在 8899）
"""
from playwright.sync_api import sync_playwright

CASES = [
    # (名称, 时间, 期望hasToday, 期望标签词)
    ("周五 15:19 收盘后", "2026-10-16T15:19:00+08:00", True, "收盘"),
    ("周五 20:00 晚间", "2026-10-16T20:00:00+08:00", True, "收盘"),
    ("周五 23:59", "2026-10-16T23:59:00+08:00", True, "收盘"),
    ("周六 10:00 周末", "2026-10-17T10:00:00+08:00", False, ""),
    ("周一 08:00 盘前", "2026-10-19T08:00:00+08:00", False, ""),
    ("周一 09:20 竞价", "2026-10-19T09:20:00+08:00", True, "实时"),
    ("周一 12:00 午休", "2026-10-19T12:00:00+08:00", True, "午休"),
    ("周一 14:00 交易", "2026-10-19T14:00:00+08:00", True, "实时"),
    ("元旦 10:00 节假日", "2027-01-01T10:00:00+08:00", False, ""),
]

JS = """(iso) => {
  var d = new Date(iso);
  var s = mktSession(d);
  var tagHtml = mktLiveTag(d);
  var m = tagHtml.match(/>([^<]+)</);
  return {cls: s.cls, hasToday: s.hasToday, line: s.line,
          word: mktLiveWord(d), note: mktLiveNote(d),
          tag: m ? m[1] : ''};
}"""

fails = 0
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto("http://127.0.0.1:8899/", wait_until="domcontentloaded")
    pg.wait_for_timeout(800)
    for name, iso, exp_today, exp_tag in CASES:
        r = pg.evaluate(JS, iso)
        ok = (r["hasToday"] == exp_today and r["tag"] == exp_tag
              and r["word"] == (exp_tag or "盘后"))
        if not ok:
            fails += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:14s} cls={r['cls']:10s} "
              f"hasToday={str(r['hasToday']):5s} tag={r['tag'] or '(空)':4s} "
              f"word={r['word']:3s} | {r['note']}")
    b.close()
print(f"\n结果: {len(CASES)-fails} 通过 / {fails} 失败")
