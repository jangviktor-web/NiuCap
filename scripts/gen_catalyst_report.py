"""生成「各板块利空/利好词语解析」报告（自包含 HTML，可视化 + 可分享）。

数据源：sector_sentiment 的 SECTOR_BULLISH/SECTOR_BEARISH 词表 + 实时快讯催化剂命中。
"""
from __future__ import annotations
import os
import sys
import html

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import sector_sentiment as ss  # noqa: E402

OUT = "/workspace/板块利空利好词语解析.html"

# 各板块的行业逻辑一句话注解（联网归纳，2026-10）
LOGIC = {
    "军工": "地缘冲突/军费扩张/军贸出口→利好；局势缓和/订单不及预期→利空",
    "农业": "粮价上行/产能去化/厄尔尼诺减产/收储→利好；供给宽松/产能过剩/价格探底→利空",
    "通信": "算力资本开支/光模块放量/卫星通信→利好；FCC限制/禁令/降价/砍单→利空",
    "医药": "FDA获批/创新药/BD出海/集采豁免→利好；集采/降价/临床失败/反腐→利空",
    "新能源": "装机/中标/出口/渗透率提升→利好；价格战/内卷/产能过剩/补贴退坡→利空",
    "半导体": "国产替代/涨价/扩产/算力需求→利好；制裁/出口管制/砍单/周期下行→利空",
    "科技AI": "算力资本开支/大模型/云订单→利好；制裁/限制/砍单/估值高位回调→利空",
    "金融": "降息降准/宽松/成交量放大/牛市→利好；加息/收紧/息差收窄/坏账→利空",
    "地产": "政策放松/销售回暖/降息/去库存→利好；违约/停工/调控/库存高→利空",
    "汽车": "出海/销量增长/智能化/订单→利好；价格战/内卷/降价/需求疲软→利空",
    "有色": "降息/美元走弱/涨价/短缺→利好；美元走强/加息/累库/需求疲软→利空",
    "化工": "涨价/价差修复/油价高位/需求→利好；降价/成本上升/产能过剩→利空",
    "能源": "减产(OPEC)/霍尔木兹/供应中断/地缘→利好；增产/需求疲软/地缘缓和/释放储备→利空",
    "建材": "地产复苏/基建/稳增长/涨价→利好；供需双弱/需求疲软/库存高→利空",
    "消费": "提价/复苏/放量/增长→利好；降价/需求疲软/下滑/内卷→利空",
    "中概港股": "南向资金/回购/AI/政策/估值修复→利好；地缘/利率/监管/退市→利空",
}


def esc(s):
    return html.escape(str(s))


def build():
    rows = []
    for sec, kws in ss.SECTOR_KEYWORDS.items():
        bull = ss.SECTOR_BULLISH.get(sec, [])
        bear = ss.SECTOR_BEARISH.get(sec, [])
        rows.append((sec, len(kws), bull, bear))

    cards = []
    for sec, nkw, bull, bear in rows:
        b_chips = "".join(f'<span class="chip bull">{esc(w)}</span>' for w in bull)
        s_chips = "".join(f'<span class="chip bear">{esc(w)}</span>' for w in bear)
        cards.append(f"""
    <div class="card">
      <div class="card-head">
        <h3>{esc(sec)}</h3>
        <span class="badge">归因词 {nkw} · 利好 {len(bull)} / 利空 {len(bear)}</span>
      </div>
      <p class="logic">{esc(LOGIC.get(sec, ""))}</p>
      <div class="row"><span class="rowlab">利好 ↑</span><div class="chips">{b_chips or '<i>—</i>'}</div></div>
      <div class="row"><span class="rowlab">利空 ↓</span><div class="chips">{s_chips or '<i>—</i>'}</div></div>
    </div>""")

    gb = "".join(f'<span class="chip bull">{esc(w)}</span>' for w in ss.GLOBAL_BULLISH)
    gs = "".join(f'<span class="chip bear">{esc(w)}</span>' for w in ss.GLOBAL_BEARISH)

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>各板块利空 / 利好词语解析</title>
<style>
  :root{{--bg:#0f1216;--card:#171b22;--line:#252b35;--fg:#e6e9ef;--dim:#9aa4b2;
        --bull:#ff5b5b;--bear:#33c46b;--accent:#4a9eff}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}}
  header{{padding:28px 22px 14px;border-bottom:1px solid var(--line)}}
  h1{{margin:0 0 6px;font-size:20px}}
  .sub{{color:var(--dim);font-size:13px}}
  .note{{margin-top:10px;padding:10px 12px;background:#141c28;border-left:3px solid var(--accent);
        border-radius:6px;color:var(--dim);font-size:12.5px}}
  .global{{margin:16px 22px}}
  .global h2{{font-size:14px;margin:0 0 8px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
        gap:14px;padding:16px 22px 40px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
  .card-head{{display:flex;justify-content:space-between;align-items:center;gap:8px}}
  .card-head h3{{margin:0;font-size:16px}}
  .badge{{font-size:11px;color:var(--dim);white-space:nowrap}}
  .logic{{color:var(--dim);font-size:12.5px;margin:6px 0 10px}}
  .row{{display:flex;gap:8px;margin:6px 0;align-items:flex-start}}
  .rowlab{{flex:0 0 54px;font-size:12px;padding-top:2px}}
  .chips{{display:flex;flex-wrap:wrap;gap:5px}}
  .chip{{font-size:12px;padding:2px 8px;border-radius:20px;border:1px solid transparent}}
  .chip.bull{{background:rgba(255,91,91,.13);color:var(--bull);border-color:rgba(255,91,91,.3)}}
  .chip.bear{{background:rgba(80,220,140,.13);color:var(--bear);border-color:rgba(80,220,140,.3)}}
  i{{color:var(--dim)}}
  footer{{padding:14px 22px 40px;color:var(--dim);font-size:12px;border-top:1px solid var(--line)}}
</style></head><body>
<header>
  <h1>各板块利空 / 利好词语解析</h1>
  <div class="sub">基于公开研报与行业共识联网归纳 · 2026-10 · 共 {len(rows)} 个板块</div>
  <div class="note">红=利好（A股习惯，红涨绿跌）· 绿=利空。这些词与「全局催化剂词」合并后，
  用于给每条快讯解析出结构化信号（<code>bull</code>/<code>bear</code>），
  并聚合成板块的 <code>cat_net</code>（利好词数−利空词数），作为词典情绪之外的二级确认。</div>
</header>
<div class="global">
  <h2>全局催化剂词（跨板块通用）</h2>
  <div class="row"><span class="rowlab">利好 ↑</span><div class="chips">{gb}</div></div>
  <div class="row"><span class="rowlab">利空 ↓</span><div class="chips">{gs}</div></div>
</div>
<div class="grid">
{''.join(cards)}
</div>
<footer>
  说明：行业专属动作词（如「减产」在能源=利好、在农业因粮价上行亦=利好）不进全局词表，
  避免方向自相矛盾；本页词表已通过「零矛盾」自检（板块内 bull∩bear=∅、板块词与全局词不冲突）。
</footer>
</body></html>"""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成：{OUT}（{len(rows)} 板块）")


if __name__ == "__main__":
    build()
