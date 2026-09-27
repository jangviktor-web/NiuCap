"""
网格交易回测 —— 固定档位网格 + A股真实约束

档位模型（与 grider 的「移动基准价」不同，这里用固定档位）：
  - 先按 基准价 ± n×步长 铺出上下档位，每档只在第一次被触及的时候成交一次
  - 跌破买档 → 买入一份；涨破卖档 → 卖出一份（卖的是低位买进来的那些）
  - 固定档位的好处：档位表可以画出来给人看（虚拟盘的网格档位表直接复用本函数）

为什么不用 grider 的移动基准价：
  实测过，单边上涨时基准价会被一路推高，档位跟着漂移，最后踏空
  （行情 +81.67%，网格只赚 8.85%）。固定档位至少能把这个失败暴露给用户。

真实约束（比 grider 多出来的三条，都是实测踩出来的）：
  - T+1：当天买的份额当天不能卖。grider 完全没考虑（它是美股口径），
    直接搬会导致回测结果偏乐观 —— 网格一天可能触发多次，T+1 会吃掉不少卖单
  - 整手：A股/场内基金最小 100 份
  - 费率：佣金双边 + 最低 5 元；ETF 免印花税（税法硬规则，不可配）

成交价：按档位价成交（挂单语义），不是 grider 用的 (H+L+O+C)/4 均价。
  跳空低开时按开盘价成交（挂单在更高价位确实会以开盘价成交）。

已知天花板见各处 `ponytail:` 注释。
"""

import math

LOT = 100                  # A股 / 场内基金最小交易单位（份）
DEFAULT_FEE_RATE = 0.00025  # 佣金 万2.5（双边）
DEFAULT_FEE_MIN = 5.0       # 最低佣金 5 元
DEFAULT_STAMP = 0.001       # 印花税 千1（卖出单边，ETF 免）
TRADING_DAYS = 244


def _fee(amount, rate=DEFAULT_FEE_RATE, min_fee=DEFAULT_FEE_MIN):
    return max(amount * rate, min_fee)


def build_levels(base, lower, upper, step_pct, mode="arith"):
    """铺网格档位（升序，含基准价）。

    mode='arith' 等差：步长 = base * step_pct / 100（绝对值固定）
    mode='geo'   等比：每档 × (1 + step_pct/100)

    返回 (levels, base_idx)
    """
    base = float(base)
    if base <= 0 or upper <= lower:
        return [base], 0
    step_pct = float(step_pct)
    if step_pct <= 0:
        return [base], 0

    levels = [base]
    if mode == "geo":
        r = 1.0 + step_pct / 100.0
        p = base
        while p * r <= upper:
            p = p * r
            levels.append(p)
        p = base
        while p / r >= lower:
            p = p / r
            levels.append(p)
    else:
        step = base * step_pct / 100.0
        p = base
        while p + step <= upper:
            p += step
            levels.append(p)
        p = base
        while p - step >= lower:
            p -= step
            levels.append(p)

    levels.sort()
    # 去重阈值跟着步长走，别写死 0.001 —— 低价 ETF 用 0.5% 步长时
    # 档间距只有 0.005，写死 0.001 会把相邻档误合并
    tol = (base * step_pct / 100.0) / 10.0 if mode == "arith" else base * step_pct / 1000.0
    uniq = []
    for x in levels:
        if not uniq or abs(x - uniq[-1]) > tol:
            uniq.append(round(x, 4))
    base_idx = min(range(len(uniq)), key=lambda i: abs(uniq[i] - base))
    return uniq, base_idx


def _atr(klines, n=14):
    """平均真实波幅（绝对值）。数据不足返回 None。"""
    if len(klines) < n + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i].get("high") or klines[i]["close"])
        lo = float(klines[i].get("low") or klines[i]["close"])
        pc = float(klines[i - 1]["close"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def suggest_params(klines, risk=1.0):
    """按 ATR 给建议步长与上下界（grider 的 optimizer 思路，简化版）。

    ponytail: 只用 ATR 一个因子，没接管 ADX 趋势过滤和夏普最优化,
    add when 有人真的按建议去下单、需要更保守的参数。
    """
    if not klines or len(klines) < 20:
        return {}
    close = float(klines[-1]["close"])
    a = _atr(klines)
    if not a or close <= 0:
        return {}
    atr_pct = a / close * 100.0
    step = max(0.5, min(8.0, round(atr_pct * risk * 0.8, 1)))
    closes = [float(k["close"]) for k in klines]
    lo, hi = min(closes), max(closes)
    pad = (hi - lo) / close * 100.0
    band = max(10.0, min(60.0, round(pad * 1.2 + step * 3, 1)))
    return {
        "step_pct": step,
        "band_pct": band,
        "atr": round(a, 4),
        "atr_pct": round(atr_pct, 2),
        "note": f"近 14 日 ATR 占价 {atr_pct:.2f}%，建议步长 {step}%、区间 ±{band}%",
    }


def _metrics(nav, init_cash, final_cash, position, last_px, klines):
    """净值曲线 → 收益/年化/回撤/夏普/卡玛 + 买入持有基准。"""
    final_net = final_cash + position * last_px
    total_ret = (final_net - init_cash) / init_cash * 100.0 if init_cash else 0.0
    years = len(nav) / TRADING_DAYS
    if years > 0 and final_net > 0 and init_cash > 0:
        annual = ((final_net / init_cash) ** (1.0 / years) - 1) * 100.0
    else:
        annual = 0.0

    peak = -1e18
    mdd = 0.0
    for _, v in nav:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100.0)

    rets = []
    for i in range(1, len(nav)):
        p0, p1 = nav[i - 1][1], nav[i][1]
        if p0 > 0:
            rets.append((p1 - p0) / p0)
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(TRADING_DAYS)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    bh = ((last_px - float(klines[0]["close"])) / float(klines[0]["close"]) * 100.0
          if float(klines[0]["close"]) else 0.0)

    curve = nav
    if len(curve) > 400:
        step = len(curve) // 400 + 1
        curve = curve[::step]
        if curve[-1] != nav[-1]:
            curve.append(nav[-1])

    return {
        "total_return": round(total_ret, 2),
        "annual_return": round(annual, 2),
        "max_drawdown": round(mdd, 2),
        "sharpe": round(sharpe, 3),
        "calmar": round(annual / mdd, 2) if mdd > 0 else 0.0,
        "benchmark_return": round(bh, 2),
        "final_asset": round(final_net, 2),
        "nav_curve": curve,
    }


def grid_backtest(klines, *, base=None, step_pct=2.0, band_pct=20.0,
                  lot=10000, capital=100000.0, base_ratio=0.5,
                  mode="arith", strategy="fixed", step_sell_pct=None,
                  pyramid_mul=1.5,
                  fee_rate=DEFAULT_FEE_RATE,
                  fee_min=DEFAULT_FEE_MIN, stamp=DEFAULT_STAMP,
                  etf=False, count=None):
    """网格交易回测（固定档位 / 金字塔 / 不对称 / 移动网格）。

    klines        : [{'date','open','high','low','close','volume'}]
    base          : 基准价，None 表示用首根 K 线收盘（不用未来数据）
    step_pct      : 步长（%），等差时按基准价折算绝对值；不对称网格时为「买入步长」
    band_pct      : 上下界幅度（%），相对基准价
    lot           : 每格份数（会被向下取整到 100 的整数倍）
    capital       : 总资金
    base_ratio    : 底仓比例（0-1），按首根收盘价买入
    mode          : 'arith' 等差 / 'geo' 等比（档位的铺法）
    strategy      : 网格策略，见下：
        'fixed'    固定网格：上下界固定，每格等份买入、等份卖出（原行为）
        'pyramid'  金字塔加码：跌得越深买得越多（每远离基准一档 × pyramid_mul），
                   卖出仍是一格一份 —— 网格被套时摊低成本，代价是下跌段更吃资金
        'asym'     不对称网格：买入用 step_pct、卖出用 step_sell_pct（默认买步长
                   ×1.5）。跌一小格就买、涨一大格才卖 = 攒筹码；反过来就是快进快出
        'moving'   移动网格：价格跑出上下界后，区间整体跟随平移并重铺档位，
                   解决单边行情里网格「停摆」的固有缺陷
    step_sell_pct : 不对称网格的卖出步长（%），None 表示买步长 ×1.5
    pyramid_mul   : 金字塔每档放大倍率（1 = 退化成固定网格）
    etf           : 是否 ETF（免印花税）

    返回 dict；klines 太短返回 None。

    注意：这里只卡「跑不出东西」的 3 根下限，业务上「至少 20 根才有意义」
    由 API 层判断并给明确报错 —— 核心函数不替调用方决定体验。
    """
    if not klines or len(klines) < 3:
        return None
    if count:
        klines = klines[-int(count):]
    n = len(klines)

    capital = float(capital)
    if capital <= 0:
        return None

    first_close = float(klines[0]["close"])
    base = float(base) if base else first_close
    if base <= 0:
        return None
    lower, upper = base * (1 - band_pct / 100.0), base * (1 + band_pct / 100.0)
    levels, base_idx = build_levels(base, lower, upper, step_pct, mode)
    if len(levels) < 3:
        return None

    # 不对称网格：买卖各一套档位（买档密 / 卖档疏，或反过来）
    strat = (strategy or "fixed").lower()
    levels_buy, bidx = levels, base_idx
    levels_sell, sidx = levels, base_idx
    sell_step = float(step_sell_pct) if step_sell_pct else step_pct * 1.5
    if strat == "asym" and sell_step > 0:
        levels_sell, sidx = build_levels(base, lower, upper, sell_step, mode)

    # 每格金额自适应：一份就吃掉大半资金的话，买两格就没钱了，
    # 后半段全是"资金不足"的拒单，回测结果没有意义。
    # ponytail: 简单启发式（按档数均分），不是真正的仓位规划器,
    # add when 需要按「预留 N 格下跌资金」精细配置。
    lot = int(max(LOT, lot) // LOT) * LOT          # 整手
    lot_auto = False
    budget_per_grid = capital / len(levels)
    if lot * base > budget_per_grid:
        lot = int(budget_per_grid / base / LOT) * LOT
        lot = max(LOT, lot)
        lot_auto = True

    cash = capital
    min_cash = capital    # 期间现金最低点 → 峰值资金占用
    position = 0          # 总持仓
    sellable = 0          # T+1 可卖部分
    avg_cost = 0.0        # 加权成本（★ grider 靠 total_asset 倒推，会失真，这里单独维护）
    realized = 0.0        # 已实现盈亏
    trades = []
    nav = []
    out_days = 0          # 价格跑出网格区间的天数
    rejected = 0          # 因资金/持仓/T+1 不足被拒的单
    rejected_levels = set()   # 已计过拒单的档位（同一档不重复计数）
    triggered = set()     # 被触发过的档位下标（★ 触发率按档位算，不是按成交笔数）

    def _buy(px, qty, date, tag):
        nonlocal cash, position, avg_cost
        amount = px * qty
        cost = amount + _fee(amount, fee_rate, fee_min)
        if cost > cash:
            return 0
        # 真实成交价含滑点的近似：这里按档位价成交，不再额外加滑点
        # ponytail: 未建模冲击成本与排队成交, add when 单格资金超过日均成交额 1%
        cash -= cost
        avg_cost = (avg_cost * position + px * qty) / (position + qty) if position + qty else 0.0
        position += qty
        trades.append({"date": date, "side": "buy", "price": round(px, 4),
                       "qty": qty, "fee": round(cost - amount, 2), "tag": tag})
        return qty

    def _sell(px, qty, date, tag):
        nonlocal cash, position, sellable, realized
        qty = min(qty, sellable)              # T+1：当天买的不算
        qty = int(qty // LOT) * LOT
        if qty <= 0:
            return 0
        amount = px * qty
        fee = _fee(amount, fee_rate, fee_min) + (0.0 if etf else amount * stamp)
        cash += amount - fee
        realized += (px - avg_cost) * qty - fee     # ★ 用真实加权成本，不是最后一次成交价
        position -= qty
        sellable -= qty
        trades.append({"date": date, "side": "sell", "price": round(px, 4),
                       "qty": qty, "fee": round(fee, 2),
                       "pnl": round((px - avg_cost) * qty - fee, 2), "tag": tag})
        return qty

    # ---- 底仓：按首根 K 线收盘买入 ----
    q0 = int((capital * max(0.0, min(1.0, base_ratio)) / first_close) // LOT) * LOT
    if q0 > 0:
        got = _buy(first_close, q0, klines[0]["date"], "底仓")
        if got == 0:
            rejected += 1

    next_buy = bidx - 1          # 下一个待买的档（向下）
    next_sell = sidx + 1         # 下一个待卖的档（向上）
    rebalanced = 0               # 移动网格的区间平移次数

    # 金字塔：跌得越深买得越多（每远离基准一档 × mul）
    # ponytail: 倍率档数封顶 8 档，防止档位多时份数爆炸；真正的资金约束
    # 由 _buy 里的现金检查兜底（买不动就拒单并提示），
    # add when 需要按「预留 N 格下跌资金」做真正的仓位规划。
    def _lot_for(k):
        if strat != "pyramid":
            return lot
        dist = min(abs(k - bidx), 8)
        return int(max(LOT, lot * (pyramid_mul ** dist)) // LOT) * LOT

    # 移动网格：以新中轴重铺区间与档位。持仓不动（已买的继续拿着），
    # 只是把网格挪到新区间继续做 —— 单边行情里原网格会永久停摆。
    def _rebuild(new_base):
        nonlocal base, lower, upper, levels, base_idx
        nonlocal levels_buy, bidx, levels_sell, sidx, next_buy, next_sell
        base = round(float(new_base), 4)
        if base <= 0:
            return
        lower = base * (1 - band_pct / 100.0)
        upper = base * (1 + band_pct / 100.0)
        levels, base_idx = build_levels(base, lower, upper, step_pct, mode)
        levels_buy, bidx = levels, base_idx
        if strat == "asym" and sell_step > 0:
            levels_sell, sidx = build_levels(base, lower, upper, sell_step, mode)
        else:
            levels_sell, sidx = levels, base_idx
        next_buy, next_sell = bidx - 1, sidx + 1

    for i in range(1, n):
        k = klines[i]
        date = k["date"]
        op = float(k.get("open") or k["close"])
        hi = float(k.get("high") or k["close"])
        lo = float(k.get("low") or k["close"])
        cl = float(k["close"])

        # T+1 解禁：上一日及之前买入的全部可卖
        sellable = position

        # 跑出网格区间 → 当日停摆（网格的固有缺陷，记下来告诉用户）
        skip_today = False
        if cl > upper or cl < lower:
            out_days += 1
            if strat == "moving":
                # 区间整体平移跟随价格重铺；当天只做平移、不成交（避免同日双算）
                _rebuild(cl)
                rebalanced += 1
                skip_today = True

        # 日线内无法判断先触 low 还是先触 high，按开盘价离哪边近决定先后
        # ponytail: 日线级简化，分钟级数据能消掉这个假设, add when 接入分钟线回测
        down_first = (op - lo) <= (hi - op)

        # 拒单不推进指针 —— 真实网格里挂单没成交会继续挂着，
        # 下次价格再回到这档（或 T+1 解禁后）仍然会成交。
        # 推进指针会把单子永久漏掉（实测踩到：d2 被 T+1 拦下后 d4 再触同档也不成交了）
        def _do_buy():
            nonlocal next_buy, rejected
            while next_buy >= 0 and lo <= levels_buy[next_buy]:
                lv = levels_buy[next_buy]
                px = min(lv, op) if op < lv else lv
                triggered.add(next_buy)
                if _buy(px, _lot_for(next_buy), date, f"档{next_buy}") > 0:
                    next_buy -= 1
                else:
                    if next_buy not in rejected_levels:   # 同一档只计一次，别刷屏
                        rejected_levels.add(next_buy)
                        rejected += 1
                    break

        def _do_sell():
            nonlocal next_sell, rejected
            while next_sell < len(levels_sell) and hi >= levels_sell[next_sell]:
                lv = levels_sell[next_sell]
                px = max(lv, op) if op > lv else lv
                triggered.add(next_sell)
                if _sell(px, lot, date, f"档{next_sell}") > 0:
                    next_sell += 1
                else:
                    if next_sell not in rejected_levels:
                        rejected_levels.add(next_sell)
                        rejected += 1
                    break

        if not skip_today:
            if down_first:
                _do_buy()
                _do_sell()
            else:
                _do_sell()
                _do_buy()

        min_cash = min(min_cash, cash)
        nav.append((date, round(cash + position * cl, 2)))

    last_px = float(klines[-1]["close"])
    m = _metrics(nav, capital, cash, position, last_px, klines)

    # ---- 风险提示：这几条是实测出来的真实失效场景，必须让用户看见 ----
    warnings = []
    if out_days > n * 0.2:
        if strat == "moving":
            warnings.append(
                f"价格有 {out_days}/{n} 天跑出原区间（±{band_pct}%），移动网格已跟随平移 "
                f"{rebalanced} 次 —— 单边行情里平移能续命，但每次平移都会重铺档位，"
                f"低位筹码可能在平移中错过卖点。")
        else:
            warnings.append(
                f"价格有 {out_days}/{n} 天跑出网格区间（±{band_pct}%），网格在那些天是停摆的。"
                f"本区间偏单边行情，网格策略不适用，收益不具参考性。"
                f"（可试「移动网格」让区间跟随平移）")
    if strat == "pyramid":
        warnings.append(
            f"金字塔加码：每远离基准一档，买入份数 ×{pyramid_mul}（8 档封顶）。"
            f"摊低成本更快，但下跌段会加速吃资金 —— 本次峰值资金占用 "
            f"{(capital - min_cash) / capital * 100:.0f}%。")
    if strat == "asym":
        warnings.append(
            f"不对称网格：买入步长 {step_pct}%、卖出步长 {sell_step:.2f}% —— "
            f"卖步长更大 = 攒筹码慢卖；若买步长更大则是快进快出。")
    if strat == "moving" and rebalanced == 0 and out_days == 0:
        warnings.append("移动网格：本区间价格未跑出上下界，未触发平移，表现与固定网格一致。")
    if m["total_return"] < m["benchmark_return"] - 5:
        warnings.append("网格跑输买入持有超过 5 个百分点 —— 上涨行情里网格会不断卖飞。")
    if rejected:
        warnings.append(f"有 {rejected} 次触发因资金不足或 T+1 未解禁未成交，实际收益低于理论值。")
    if position > 0 and last_px < avg_cost:
        warnings.append(f"期末仍持仓 {position} 份，浮亏 {(last_px - avg_cost) * position:.0f} 元（未计入收益）。")
    if len(levels) < 5:
        warnings.append("档位太少（<5），网格密度不足，建议缩小步长或放宽区间。")
    if lot_auto:
        warnings.append(f"每格份数已自动缩小到 {lot} 份（按 {len(levels)} 档均分资金）—— "
                        f"按你填的份数，买两三格就会资金耗尽，后半段全是拒单。")

    m.update({
        "code": None,
        # K 线随结果一起回传：前端要在主图上标买卖点，而它本来就在手里，
        # 让前端另发一次 /api/kline 会多一次网络往返 + 时刻不一致的风险
        "klines": [{"date": k["date"], "open": k["open"], "close": k["close"],
                    "high": k["high"], "low": k["low"]} for k in klines],
        "levels": [round(x, 4) for x in levels],
        "triggered_levels": sorted(triggered),   # 被触发过的档位下标，前端画档位表用
        "base_idx": base_idx,
        "base_price": round(base, 4),
        "lower": round(lower, 4),
        "upper": round(upper, 4),
        "step_pct": step_pct,
        "band_pct": band_pct,
        "mode": mode,
        "strategy": strat,
        "rebalanced": rebalanced,
        "step_sell_pct": round(sell_step, 4) if strat == "asym" else None,
        "pyramid_mul": float(pyramid_mul) if strat == "pyramid" else None,
        "lot": lot,
        "etf": bool(etf),
        "init_cash": capital,
        "cash": round(cash, 2),
        "position": position,
        "avg_cost": round(avg_cost, 4),
        "sellable": sellable,
        "realized_pnl": round(realized, 2),
        "float_pnl": round((last_px - avg_cost) * position, 2) if position else 0.0,
        "trades": len(trades),
        "buy_trades": sum(1 for t in trades if t["side"] == "buy"),
        "sell_trades": sum(1 for t in trades if t["side"] == "sell"),
        # ★ 触发率按「被触发过的档位 / 总档位」，不是「成交笔数 / 网格数」
        #   grider 用后者，实测算出 1.4（>1，概念上就不该超过 1）
        "grid_trigger_rate": round(len(triggered) / len(levels), 3),
        # 峰值资金占用（期间现金最低点），不是期末口径 ——
        # 期末如果赚钱了，1 - cash/capital 会算出负数，没意义
        "capital_utilization": round((capital - min_cash) / capital, 3) if capital else 0.0,
        "lot_auto": lot_auto,
        "out_of_range_days": out_days,
        "rejected": rejected,
        "warnings": warnings,
        "trade_list": trades[-100:],
    })
    return m


# --------------------------------------------------------------------------
# 自检：python3.11 grid.py
# --------------------------------------------------------------------------

def _mkkl(rows):
    """rows: [(date, open, high, low, close)]"""
    return [{"date": d, "open": o, "high": h, "low": l, "close": c, "volume": 1000}
            for d, o, h, l, c in rows]


def selfcheck() -> int:
    """纯逻辑自检，不联网。"""
    fails = []

    def eq(got, want, label):
        if got == want:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}: got={got!r} want={want!r}")
            fails.append(label)

    def near(got, want, label, tol=0.02):
        if abs(got - want) <= tol:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}: got={got!r} want≈{want!r}")
            fails.append(label)

    def check(cond, good, bad):
        if cond:
            print(f"  ✅ {good}")
        else:
            print(f"  ❌ {bad}")
            fails.append(bad)

    print("\n[1] 档位生成")
    lv, bi = build_levels(5.0, 3.5, 6.5, 10.0, "arith")
    eq(len(lv), 7, "等差档位数 7（3.5~6.5 步长 0.5）")
    near(lv[bi], 5.0, "base_idx 指向基准价 5.0")
    lv2, _ = build_levels(5.0, 3.5, 6.5, 10.0, "geo")
    check(len(lv2) >= 5, f"等比档位数 {len(lv2)} ≥ 5", "等比档位生成失败")
    # ★ 去重阈值回归：低价 ETF + 小步长，写死 0.001 会误合并相邻档
    lv3, _ = build_levels(1.0, 0.8, 1.2, 0.5, "arith")
    check(len(lv3) >= 9, f"1 元 ETF + 0.5% 步长档位数 {len(lv3)} ≥ 9（相邻档没被误合并）",
          f"1 元 ETF + 0.5% 步长档位只有 {len(lv3)} 档，去重阈值又写死了")

    print("\n[2] B1 回归 · 加权平均成本（grider 用最后一次成交价，实测失真 +50）")
    kl = _mkkl([
        ("d1", 5.0, 5.0, 5.0, 5.0),      # 底仓 @5.0
        ("d2", 5.0, 5.0, 4.4, 4.7),      # 触 4.5 档买
        ("d3", 4.7, 4.8, 3.9, 4.0),      # 触 4.0 档买
    ])
    r = grid_backtest(kl, base=5.0, step_pct=10.0, band_pct=30.0, lot=10000,
                      capital=1000000.0, base_ratio=0.1)
    check(r is not None, "回测跑通", "回测返回 None")
    if r:
        # 底仓 20000@5.0 + 10000@4.5 + 10000@4.0 → 加权 4.625
        near(r["avg_cost"], 4.625, f"加权成本 4.625（grider 会给 4.0）")
        eq(r["position"], 40000, "持仓 40000 份")
        eq(r["trades"], 3, "成交 3 笔（底仓 + 2 次网格买入）")

    print("\n[3] B2 回归 · 单根 K 线双向都要成交（grider 买了就 return，卖单被吞）")
    kl2 = _mkkl([
        ("d1", 5.0, 5.0, 5.0, 5.0),
        ("d2", 5.0, 5.6, 4.4, 5.0),      # low 触 4.5 买档，high 触 5.5 卖档
        ("d3", 5.0, 5.0, 5.0, 5.0),      # 平盘，不触任何档（只为凑够 3 根）
    ])
    r2 = grid_backtest(kl2, base=5.0, step_pct=10.0, band_pct=30.0, lot=10000,
                       capital=1000000.0, base_ratio=0.05)
    if r2:
        day2 = [t for t in r2["trade_list"] if t["date"] == "d2"]
        sides = sorted(t["side"] for t in day2)
        eq(sides, ["buy", "sell"], "同一根 K 线买、卖都成交")

    print("\n[4] B3 回归 · 触发率按档位算，必须 ≤ 1")
    if r:
        check(0.0 <= r["grid_trigger_rate"] <= 1.0,
              f"触发率 {r['grid_trigger_rate']} ∈ [0,1]",
              f"触发率 {r['grid_trigger_rate']} 越界（grider 实测算出 1.4）")

    print("\n[5] T+1 · 当天买的当天不能卖（grider 是美股口径，没有这条）")
    kl3 = _mkkl([
        ("d1", 5.0, 5.0, 5.0, 5.0),
        ("d2", 5.0, 5.6, 4.4, 5.0),      # 先买（4.5）后卖（5.5），但当天买的还没解禁
        ("d3", 5.0, 5.0, 5.0, 5.0),
        ("d4", 5.0, 5.6, 5.0, 5.5),      # 次日再触卖档 —— 这时底仓已解禁，应该能卖
    ])
    r3 = grid_backtest(kl3, base=5.0, step_pct=10.0, band_pct=30.0, lot=10000,
                       capital=1000000.0, base_ratio=0.0)   # 无底仓
    if r3:
        d2_sell = [t for t in r3["trade_list"] if t["date"] == "d2" and t["side"] == "sell"]
        d4_sell = [t for t in r3["trade_list"] if t["date"] == "d4" and t["side"] == "sell"]
        eq(len(d2_sell), 0, "d2 当天买入的份额当天不能卖（T+1 拦下）")
        eq(len(d4_sell), 1, "d4 次日解禁后同一档卖出成功")
        check(r3["rejected"] >= 1, f"拒单计数 {r3['rejected']} ≥ 1", "拒单没有被计数")
        check(any("T+1" in w for w in r3["warnings"]), "warnings 里说明了 T+1 拒单",
              "warnings 没提 T+1")

    print("\n[6] ETF 免印花税")
    kl4 = _mkkl([
        ("d1", 5.0, 5.0, 5.0, 5.0),
        ("d2", 5.0, 5.6, 5.0, 5.5),      # 只触卖档
        ("d3", 5.0, 5.0, 5.0, 5.0),
    ])
    r4s = grid_backtest(kl4, base=5.0, step_pct=10.0, band_pct=30.0, lot=10000,
                        capital=1000000.0, base_ratio=0.05, etf=False)
    r4e = grid_backtest(kl4, base=5.0, step_pct=10.0, band_pct=30.0, lot=10000,
                        capital=1000000.0, base_ratio=0.05, etf=True)
    if r4s and r4e:
        fs = [t["fee"] for t in r4s["trade_list"] if t["side"] == "sell"]
        fe = [t["fee"] for t in r4e["trade_list"] if t["side"] == "sell"]
        check(bool(fs) and bool(fe), "两边都产生了卖出", "没有卖出成交")
        if fs and fe:
            check(fe[0] < fs[0], f"ETF 卖出费用 {fe[0]} < 股票 {fs[0]}（省了印花税）",
                  f"ETF 费用 {fe[0]} 没比股票 {fs[0]} 低")

    print("\n[7] 单边行情必须报警（实测：网格 +81% 行情只赚 8.85%）")
    up = _mkkl([(f"u{i}", p, p * 1.005, p * 0.995, p)
                for i, p in enumerate([4.0 * (1.01 ** i) for i in range(60)])])
    ru = grid_backtest(up, step_pct=2.0, band_pct=20.0, lot=10000,
                       capital=100000.0, base_ratio=0.5)
    if ru:
        check(ru["out_of_range_days"] > 0, f"跑出区间 {ru['out_of_range_days']} 天被记录",
              "跑出区间没被统计")
        check(any("单边" in w or "停摆" in w for w in ru["warnings"]),
              "warning 提示了单边行情/停摆", f"没提示单边行情：{ru['warnings']}")

    print("\n[8] 新策略 · 金字塔 / 不对称 / 移动网格")
    # 下跌行情：连续触发下方买入档，看每格份数
    dn = _mkkl([
        ("d1", 5.0, 5.0, 5.0, 5.0),
        ("d2", 5.0, 5.0, 4.4, 4.4),      # 触 4.5 档买
        ("d3", 4.4, 4.4, 3.9, 3.9),      # 触 4.0 档买
        ("d4", 3.9, 3.9, 3.4, 3.4),      # 触 3.5 档买
    ])
    kw = dict(base=5.0, step_pct=10.0, band_pct=30.0, lot=10000,
              capital=1000000.0, base_ratio=0.02)
    rf = grid_backtest(dn, strategy="fixed", **kw)
    rp = grid_backtest(dn, strategy="pyramid", pyramid_mul=2.0, **kw)
    rp1 = grid_backtest(dn, strategy="pyramid", pyramid_mul=1.0, **kw)
    if rf and rp and rp1:
        qb = lambda r: [t["qty"] for t in r["trade_list"]
                        if t["side"] == "buy" and t["tag"] != "底仓"]
        qf, qp, q1 = qb(rf), qb(rp), qb(rp1)
        check(len(qf) >= 2 and len(set(qf)) == 1,
              f"固定网格每格等份 {qf}", f"固定网格份数不一致 {qf}")
        check(len(qp) >= 2 and qp == sorted(qp) and qp[-1] > qp[0],
              f"金字塔越跌买越多 {qp}", f"金字塔份数没递增 {qp}")
        check(q1 == qf, f"倍率 1.0 退化成固定网格 {q1}",
              f"倍率 1.0 与固定不一致 {q1} vs {qf}")

    ra = grid_backtest(dn, strategy="asym", **kw)
    ra2 = grid_backtest(dn, strategy="asym", step_sell_pct=25.0, **kw)
    if ra and ra2:
        check(ra.get("step_sell_pct") is not None
              and abs(ra["step_sell_pct"] - 15.0) < 0.01,
              f"不对称默认卖步长 = 买步长×1.5 = {ra.get('step_sell_pct')}",
              f"卖步长默认值不对：{ra.get('step_sell_pct')}")
        check(abs((ra2.get("step_sell_pct") or 0) - 25.0) < 0.01,
              f"自定义卖步长 {ra2.get('step_sell_pct')} 生效",
              f"自定义卖步长没生效：{ra2.get('step_sell_pct')}")

    # 单边上涨：固定网格卖完停摆，移动网格跟随平移续命
    rm = grid_backtest(up, step_pct=2.0, band_pct=20.0, lot=10000,
                       capital=100000.0, base_ratio=0.5, strategy="moving")
    if ru and rm:
        check(rm["rebalanced"] > 0,
              f"移动网格在单边行情平移了 {rm['rebalanced']} 次",
              "移动网格没触发平移")
        check(rm["trades"] > ru["trades"],
              f"移动网格成交 {rm['trades']} 笔 > 固定 {ru['trades']} 笔（停摆后续命）",
              f"移动网格成交 {rm['trades']} 没多于固定 {ru['trades']}")

    print("\n[9] 参数兜底")
    eq(grid_backtest(_mkkl([("d1", 1, 1, 1, 1)] * 2)), None, "K线不足 3 根返回 None")
    eq(grid_backtest([]), None, "空序列返回 None")
    lv4, bi4 = build_levels(0, 0, 0, 2.0)
    eq(len(lv4), 1, "基准价 0 不炸，退回单档")

    print("\n" + "=" * 50)
    if fails:
        print(f"❌ {len(fails)} 项失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(selfcheck())
