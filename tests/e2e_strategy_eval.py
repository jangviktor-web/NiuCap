"""端到端：策略体检（方案 A · IC/ICIR 评估）。

跑法（需先启动服务在 8899）：
    python3 tests/e2e_strategy_eval.py

验的是「用户真能点到并看懂」，不是静态代码：
  1. 策略页签能加载出策略卡片（日线 22 + 分钟级 6 = 28）
  2. 点「策略体检」→ 表格出现 15 行结果（含方案 A 6 个经典指标）+ 7 行未评估说明
  3. 表格含「市況敏感度」列（方案 D：趋势/震荡市命中超额）
  4. 策略卡片上挂了「实测」徽章
  4. 数值自洽：ICIR = IC均值/标准差 方向一致、反向策略有 ⚠️ 标记
  5. 全程无 JS 报错

用 days=40 避免测试跑太久（真实 Assess 用 120）。
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        errs = []

        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1200)

        # 直接在页面里给 fetch 计时：评估要跑上百秒，若任何单次请求接近 60 秒，
        # 外网网关就会掐断返回 504。这是本次修复的核心回归保护。
        pg.evaluate("""() => {
          window.__evalDurs = [];
          var _f = window.fetch;
          window.fetch = function(){
            var t0 = performance.now(), url = String(arguments[0] || '');
            return _f.apply(this, arguments).then(function(r){
              try{
                if(url.indexOf('/api/strategy_eval') >= 0){
                  window.__evalDurs.push({d: Math.round(performance.now() - t0),
                                          u: url.replace(/^.*\/api\//, '')});
                }
              }catch(e){ /* 计时失败不影响主流程 */ }
              return r;
            });
          };
        }""")


        # ---------- 打开策略页签 ----------
        pg.click('button[data-tab="strategy"]')
        pg.wait_for_timeout(1500)

        n_card = len(pg.query_selector_all(".stratitem"))
        rec("策略卡片已渲染", n_card >= 22, f"{n_card} 个")

        btn = pg.query_selector("#evalBtn")
        rec("体检按钮存在", btn is not None)

        # 面板初始应隐藏
        panel = pg.query_selector("#evalPanel")
        rec("体检面板初始隐藏",
            panel is not None and panel.is_hidden())

        # ---------- 点体检（用 40 天，快）----------
        # evalDays 在初始隐藏的面板里，直接 select_option 会因不可见超时。
        # 用 JS 设值避免「先展开触发一次加载、再选天数又触发一次」。
        pg.evaluate("""() => {
          var s = document.getElementById('evalDays');
          if(s){ s.value = '40'; }
        }""")
        errs.clear()
        pg.click("#evalBtn")
        rec("面板已展开", not pg.query_selector("#evalPanel").is_hidden())

        # 等到表格渲染出来（40 天约 30 秒，留足时间）
        try:
            pg.wait_for_selector("#evalBody table.evt", timeout=180000)
            ok_tbl = True
        except Exception:
            ok_tbl = False
        rec("体检表格已渲染（180s 内）", ok_tbl)

        info = pg.evaluate("""() => {
          var t = document.querySelector('#evalBody table.evt');
          if(!t) return null;
          var rows = [].slice.call(t.querySelectorAll('tbody tr'));
          var meta = (document.getElementById('evalMeta')||{}).textContent || '';
          var bodyTxt = (document.getElementById('evalBody')||{}).textContent || '';
          return {
            n: rows.length,
            header: [].slice.call(t.querySelectorAll('thead th')).map(function(h){
              return h.textContent.trim(); }),
            rows: rows.map(function(r){
              var td = r.querySelectorAll('td');
              return {
                name: (td[0]||{}).textContent.trim(),
                ic: (td[1]||{}).textContent.trim(),
                icir: (td[2]||{}).textContent.trim(),
                excess: (td[5]||{}).textContent.trim(),
                level: (td[6]||{}).textContent.trim(),
                regime: (td[7]||{}).textContent.trim(),
                na: r.classList.contains('na'),
              };
            }),
            meta: meta,
            hasSkip: /未参与评估/.test(bodyTxt),
            hasQuant: /分层测试/.test(bodyTxt),
            badges: document.querySelectorAll('.stratitem .sic').length,
            badged: [].slice.call(document.querySelectorAll('.stratitem')).map(function(n){
              var b = n.querySelector('.sic');
              return b ? { name: n.querySelector('.sn').textContent.trim(),
                           txt: b.textContent.trim(), cls: b.className } : null;
            }).filter(Boolean),
          };
        }""")

        if not info:
            rec("读取表格内容", False, "表格未渲染")
        else:
            rec("表格行数 = 15（含方案A 6个经典指标）", info["n"] == 15,
                f"{info['n']} 行")
            rec("表头含命中超额列", "命中超额" in info["header"],
                str(info["header"]))
            rec("表头含市况敏感度列（方案D）", "市况敏感度" in info["header"],
                str(info["header"]))
            rec("显示分层测试（pattern_score 5 档）", info["hasQuant"], "含分层小节")
            rec("显示未参与评估说明", info["hasSkip"], "含跳过原因")

            meta = info["meta"]
            rec("元信息含日期区间", " ~ " in meta, meta[:60])
            rec("元信息含交易日数", "个交易日" in meta)

            # 数值自洽性
            rows = info["rows"]
            parsed = []
            for r in rows:
                if r["na"]:
                    continue
                try:
                    ic = float(r["ic"])
                    icir = float(r["icir"])
                    parsed.append((r["name"], ic, icir, r["level"]))
                except Exception:
                    pass
            rec("数值可解析", len(parsed) >= 7, f"{len(parsed)}/{len(rows)} 行有效")

            if parsed:
                # ICIR 符号应与 IC 同号（ICIR = IC均值/标准差，标准差恒正）
                bad_sign = [x for x in parsed
                            if abs(x[1]) > 1e-6 and abs(x[2]) > 1e-6
                            and (x[1] > 0) != (x[2] > 0)]
                rec("ICIR 与 IC 符号一致", not bad_sign,
                    f"{[x[0] for x in bad_sign]}" if bad_sign else f"{len(parsed)} 行")

                # 按 |ICIR| 降序排列
                icirs = [abs(x[2]) for x in parsed]
                rec("按 |ICIR| 降序", all(icirs[i] >= icirs[i + 1] - 1e-9
                                      for i in range(len(icirs) - 1)),
                    " → ".join(f"{v:.2f}" for v in icirs[:4]))

                # 较强的反向策略必须有 ⚠️反向 标记
                rev = [x for x in parsed if x[1] < 0 and abs(x[2]) >= 0.5]
                rev_marked = [x for x in rev if "反向" in x[3]]
                rec("强反向策略已标记 ⚠️",
                    len(rev) == len(rev_marked),
                    f"{len(rev_marked)}/{len(rev)} 个")

                # 至少有一个强策略（否则评估没区分度，说明算错了）
                strong = [x for x in parsed if abs(x[2]) >= 0.5]
                rec("存在 |ICIR|≥0.5 的策略", len(strong) >= 1,
                    f"{[x[0] for x in strong]}")

                # 命中超额列：绝大多数行应给出数值（insufficient 行允许为 —）
                with_ex = [x for x in rows if x["excess"] not in ("—", "")]
                rec("命中超额列有值", len(with_ex) >= 7,
                    f"{len(with_ex)}/{len(rows)} 行")

                # 市况敏感度列（方案 D）：近期多为震荡市，应至少给出震荡超额
                with_reg = [x for x in rows if x["regime"] not in ("—", "")]
                rec("市况敏感度列有值", len(with_reg) >= 7,
                    f"{len(with_reg)}/{len(rows)} 行")

            # 卡片徽章
            rec("策略卡片挂了实测徽章", info["badges"] >= 7,
                f"{info['badges']} 个")
            if info["badged"]:
                sample = info["badged"][0]
                rec("徽章文案含「实测」", "实测" in sample["txt"],
                    f"{sample['name']} → {sample['txt']}")

        rec("体检过程无 JS 错误", len(errs) == 0, str(errs[:2]))

        # ---------- 再次点击应收起 ----------
        errs.clear()
        pg.click("#evalBtn")
        pg.wait_for_timeout(400)
        rec("再次点击收起面板", pg.query_selector("#evalPanel").is_hidden())
        rec("收起无 JS 错误", len(errs) == 0, str(errs[:2]))

        # ---------- 二次展开不该再算一遍 ----------
        # 前端会直接复用上次结果（后台任务的内存会被回收，不能依赖重查 job），
        # 所以这里验的是「不再等上百秒」，而不是去后端重查一次。
        # 间隔要大于浏览器的双击判定阈值（约 500ms），否则第二次点击
        # 会被当成双击的第二下，刚展开就被收起。
        pg.wait_for_timeout(800)
        _t0 = time.time()
        pg.click("#evalBtn")
        try:
            pg.wait_for_selector("#evalBody table.evt", timeout=15000)
            _dt, _ok_show = time.time() - _t0, True
        except Exception:
            _dt, _ok_show = time.time() - _t0, False
        meta2 = pg.inner_text("#evalMeta")
        rec("二次展开秒开（不重算）", _ok_show and _dt < 5,
            f"{_dt:.1f}s · {(meta2 or '')[-26:]}")

        # ---------- 防网关 504 ----------
        raw = pg.evaluate("() => window.__evalDurs || []") or []
        req_durs = [x["d"] / 1000.0 for x in raw]
        worst = max(req_durs) if req_durs else 0.0
        slow = [x for x in raw if x["d"] >= 500]
        rec("体检走后台任务（无长请求）", worst < 10,
            f"{len(req_durs)} 次请求，最长 {worst:.2f}s"
            + (f" · 慢请求 {slow[:2]}" if slow else ""))
        # 命中缓存时会很快出结果，请求数少是正常的；关键是【不能出现长请求】
        rec("提交后确有请求发出", len(req_durs) >= 2, f"{len(req_durs)} 次请求")

        # ---------- 换收益窗口应触发重算 ----------
        # forward=1 与默认 5 是不同缓存键，必须重新算，meta 里应出现「未来 1 日」
        errs.clear()
        pg.evaluate("""() => {
          var s = document.getElementById('evalForward');
          if(s){ s.value = '1'; }
        }""")
        pg.locator("#evalForward").evaluate("el => el.dispatchEvent(new Event('change'))")
        try:
            pg.wait_for_function(
                "() => /未来 1 日/.test((document.getElementById('evalMeta')||{}).textContent||'')",
                timeout=180000)
            ok_fwd, meta3 = True, pg.inner_text("#evalMeta")
        except Exception:
            ok_fwd, meta3 = False, pg.inner_text("#evalMeta")
        rec("切收益窗口触发重算（未来 1 日）", ok_fwd, meta3[-40:])
        rec("换窗口无 JS 错误", len(errs) == 0, str(errs[:2]))

        b.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print("== 汇总 ==")
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
