"""
回测引擎 —— 借鉴 TSP 的「真实约束」思路 + 三省六部的「批次持仓」模型

输入：日K序列 + 策略信号序列（1=买 / -1=卖 / 0=无）
输出：净值曲线、收益率、年化、最大回撤、夏普、卡玛、胜率、批次交易明细

真实约束（与 TSP 口径对齐）：
  - T+1：当日买入的批次，次日才可卖出（按批次的可卖日判断）
  - 手续费：双边各万分之 2.5（佣金），卖出另收千分之一印花税
  - 滑点：单边万分之 5
  - 整手交易（100 股为单位）

批次持仓模型（借鉴 shangshu_sheng.py）：
  - 每个买入动作生成一个 lot：{date, price, qty, cost, sellable_from}
  - 卖出时按 FIFO（先进先出）消耗批次，可精确模拟分批建仓 / 部分减仓
  - 单笔交易记录会聚合同一次买入的完整生命周期，便于观察盈亏归因
"""

import math

COMMISSION = 0.00025      # 佣金 万2.5（双边）
STAMP_TAX = 0.001         # 印花税 千1（卖出单边）
SLIPPAGE = 0.0005         # 滑点 万5（单边）


def _ret_pct(a, b):
    return (b - a) / a * 100.0 if a else 0.0


def _sellable_qty(lots, idx):
    """T+1 可卖量：统计 sellable_from <= 当前索引的批次股数。

    对应三省六部的 _sellable_qty_t1()。
    """
    total = 0
    for lot in lots:
        if lot["sellable_from"] <= idx:
            total += lot["qty"]
    return total


def _consume_lots_fifo(lots, qty, idx):
    """按 FIFO 消耗可卖批次，返回被消耗批次明细与加权成本。

    对应三省六部的 _consume_lots_fifo()。
    返回 (consumed, avg_cost, remaining_lots)
      consumed: [{'date','price','qty','cost_price'}...]
      avg_cost: 本次卖出部分的加权成本价
    """
    remaining = []
    consumed = []
    need = qty
    total_cost = 0.0
    total_qty = 0
    for lot in lots:
        if need <= 0:
            remaining.append(lot)
            continue
        if lot["sellable_from"] > idx:
            # 尚未解禁，保留
            remaining.append(lot)
            continue
        take = min(need, lot["qty"])
        if take > 0:
            consumed.append({
                "date": lot["date"],
                "price": round(lot["price"], 3),
                "qty": take,
                "cost_price": lot["price"],
            })
            total_cost += take * lot["price"]
            total_qty += take
            need -= take
        left = lot["qty"] - take
        if left > 0:
            new_lot = dict(lot)
            new_lot["qty"] = left
            remaining.append(new_lot)
    avg_cost = (total_cost / total_qty) if total_qty > 0 else 0.0
    return consumed, avg_cost, remaining


def backtest(klines, signals, init_cash=1000000.0,
             commission=COMMISSION, stamp=STAMP_TAX, slippage=SLIPPAGE,
             stop_loss=None, take_profit=None, position_size=1.0,
             max_lots=8, pyramid=True, pyramid_step=0.05):
    """
    klines : list[dict] 含 date/open/close/high/low
    signals: list[int]  与 klines 等长，1=买入 -1=卖出 0=持有
    init_cash: 初始资金（默认 100 万，保证高价股也能买 1 手）
    stop_loss  : 止损比例（如 0.05 = 亏 5% 离场），None 表示不启用
    take_profit: 止盈比例（如 0.15 = 赚 15% 离场），None 表示不启用
    position_size: 单次建仓占可用资金比例（0-1，默认满仓）
    max_lots: 最多同时持有的批次数（超限则合并最早两批）
    pyramid: 是否启用金字塔加仓（持仓浮盈每达 pyramid_step 补一批，直至满仓）
    pyramid_step: 加仓触发步长（0.05 = 每浮盈 5% 补一批）
    返回 dict
    """
    n = len(klines)
    if n < 2 or len(signals) != n:
        return None

    # 若初始资金买不起 1 手最贵的股票，自动放大到能买 1 手
    max_px = max(float(k["close"]) for k in klines)
    if max_px > 0 and init_cash < max_px * 100 * 1.001:
        init_cash = max_px * 100 * 1.001

    cash = init_cash
    lots = []                    # 批次持仓：[{date, price, qty, cost, sellable_from}]
    trades = []
    nav = []                     # (date, net_asset)
    next_pyr_price = None        # 下一档金字塔加仓触发价
    pyr_count = 0                # 已加仓次数统计

    def total_shares():
        return sum(l["qty"] for l in lots)

    def avg_entry():
        q = total_shares()
        if q <= 0:
            return 0.0
        return sum(l["qty"] * l["price"] for l in lots) / q

    def close_lots(date, idx, px, reason, qty=None):
        """按 FIFO 平仓（qty=None 表示全平），登记交易记录。"""
        nonlocal cash, lots
        held = total_shares()
        if held <= 0:
            return
        sellable = _sellable_qty(lots, idx)
        if sellable <= 0:
            return
        want = sellable if qty is None else min(qty, sellable)
        want = int(want // 100) * 100
        if want <= 0:
            return

        consumed, cost_px, remaining = _consume_lots_fifo(lots, want, idx)
        sell_px = px * (1 - slippage)
        proceeds = want * sell_px
        fee = proceeds * (commission + stamp)
        cash += proceeds - fee
        pnl = (sell_px - cost_px) * want - fee

        # 汇聚来源批次，保留归因信息
        src = ",".join(sorted({c["date"] for c in consumed}))
        trades.append({
            "entry_date": consumed[0]["date"] if consumed else None,
            "exit_date": date,
            "entry_price": round(cost_px, 3),
            "exit_price": round(sell_px, 3),
            "shares": want,
            "pnl": round(pnl, 2),
            "ret_pct": round(_ret_pct(cost_px, sell_px), 2),
            "reason": reason,
            "hold_days": None,
            "lots": len(consumed),
            "lot_dates": src,
            "partial": want < held,
        })
        lots = remaining

    for i in range(n):
        k = klines[i]
        px = float(k["close"])
        lo = float(k.get("low", px))
        hi = float(k.get("high", px))
        open_px = float(k.get("open", px))
        date = k["date"]
        sig = signals[i]

        # ---- 止损 / 止盈（盘中触发，优先级高于信号）----
        # 基准价用加权成本；只有可卖部分才可能被风控平掉
        if total_shares() > 0:
            base = avg_entry()
            sl_px = base * (1 - stop_loss) if stop_loss else None
            tp_px = base * (1 + take_profit) if take_profit else None
            if sl_px is not None and lo <= sl_px:
                fill = min(sl_px, open_px)
                close_lots(date, i, fill, "stop_loss")
            elif tp_px is not None and hi >= tp_px:
                fill = max(tp_px, open_px)
                close_lots(date, i, fill, "take_profit")

        # 结算前市值
        net = cash + total_shares() * px

        # 净值记录（在信号操作前记录当日权益）
        nav.append((date, round(net, 2)))

        # ---- 信号处理 ----
        if sig == -1 and total_shares() > 0:
            close_lots(date, i, px, "signal")
            next_pyr_price = None

        elif sig == 1:
            # 空仓 → 首次建仓：只投总仓位的 1/3，其余留给金字塔加仓
            if total_shares() == 0:
                target = cash * max(0.0, min(1.0, position_size))
                first_ratio = 0.4 if pyramid else 1.0
                budget = target * first_ratio
                buy_px = px * (1 + slippage)
                usable = budget / (1 + commission)
                qty = int(usable / buy_px / 100) * 100      # 整手
                if qty > 0:
                    cost = qty * buy_px
                    fee = cost * commission
                    cash -= (cost + fee)
                    lots.append({
                        "date": date, "price": buy_px, "qty": qty,
                        "cost": cost + fee, "sellable_from": i + 1,
                    })
                    next_pyr_price = buy_px * (1 + pyramid_step) if pyramid else None

        # ---- 金字塔加仓：持仓浮盈达到步长则补一批（同一多头趋势内的分批建仓）----
        if (pyramid and sig != -1 and total_shares() > 0
                and next_pyr_price is not None and hi >= next_pyr_price
                and len(lots) < max_lots and cash > 0):
            add_px = max(next_pyr_price, open_px) * (1 + slippage)
            # 每批规模 = 首次建仓规模的等比缩减，形成金字塔
            equity = cash + total_shares() * px
            target_value = equity * max(0.0, min(1.0, position_size))
            cur_value = total_shares() * px
            room = max(target_value - cur_value, 0.0)
            budget = min(cash, room * 0.6)      # 补仓不超过剩余空间 60%，避免一次打满
            usable = budget / (1 + commission)
            qty = int(usable / add_px / 100) * 100
            if qty > 0:
                cost = qty * add_px
                fee = cost * commission
                cash -= (cost + fee)
                lots.append({
                    "date": date, "price": add_px, "qty": qty,
                    "cost": cost + fee, "sellable_from": i + 1,
                })
                pyr_count += 1
                next_pyr_price = add_px * (1 + pyramid_step)
            else:
                # 资金不足一手，后续不再尝试，避免反复判断
                next_pyr_price = None
            # 批次数上限保护：过多则合并最早两批
            if len(lots) > max_lots:
                lots.sort(key=lambda l: l["date"])
                a, b = lots[0], lots[1]
                merged_qty = a["qty"] + b["qty"]
                merged = {
                    "date": a["date"],
                    "price": (a["price"] * a["qty"] + b["price"] * b["qty"]) / merged_qty,
                    "qty": merged_qty,
                    "cost": a["cost"] + b["cost"],
                    "sellable_from": max(a["sellable_from"], b["sellable_from"]),
                }
                lots = [merged] + lots[2:]

    # 期末强制平仓（计入净值，仅登记不改净值曲线）
    if total_shares() > 0:
        close_lots(klines[-1]["date"], n - 1, float(klines[-1]["close"]), "end_of_data")
        if nav:
            nav[-1] = (nav[-1][0], round(cash, 2))

    # 补算持仓天数
    date_idx = {k["date"]: i for i, k in enumerate(klines)}
    for t in trades:
        a, b = date_idx.get(t["entry_date"]), date_idx.get(t["exit_date"])
        t["hold_days"] = (b - a) if (a is not None and b is not None) else None

    final_net = cash + total_shares() * float(klines[-1]["close"])
    final_px = float(klines[-1]["close"])

    total_ret = (final_net - init_cash) / init_cash * 100.0

    # 年化（按交易日 244/年）
    days = n
    years = days / 244.0
    if years > 0 and final_net > 0:
        annual = ((final_net / init_cash) ** (1.0 / years) - 1) * 100.0
    else:
        annual = 0.0

    # 最大回撤
    peak = -1e18
    mdd = 0.0
    for _, v in nav:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            mdd = max(mdd, dd)

    # 日收益序列 -> 夏普
    rets = []
    for i in range(1, len(nav)):
        p0 = nav[i - 1][1]
        p1 = nav[i][1]
        if p0 > 0:
            rets.append((p1 - p0) / p0)
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(244)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    # 胜率
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    losses = [t for t in trades if t["pnl"] <= 0]
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    pl_ratio = (avg_win / abs(avg_loss)) if avg_loss < 0 else (0.0 if not wins else 999.0)

    # 卡玛比率（年化收益 / 最大回撤）
    calmar = (annual / mdd) if mdd > 0 else 0.0

    # 最大连续亏损次数
    max_consec_loss = 0
    cur_streak = 0
    for t in trades:
        if t["pnl"] <= 0:
            cur_streak += 1
            max_consec_loss = max(max_consec_loss, cur_streak)
        else:
            cur_streak = 0

    # 平均持仓天数
    holds = [t["hold_days"] for t in trades if t.get("hold_days") is not None]
    avg_hold = (sum(holds) / len(holds)) if holds else 0.0

    # 平仓原因统计
    reason_stats = {}
    for t in trades:
        r = t.get("reason", "signal")
        reason_stats[r] = reason_stats.get(r, 0) + 1

    # 基准（买入持有）
    bh_ret = _ret_pct(float(klines[0]["close"]), final_px)

    # 净值曲线降采样（最多 400 点）
    curve = nav
    if len(curve) > 400:
        step = len(curve) // 400 + 1
        curve = curve[::step]
        if curve[-1] != nav[-1]:
            curve.append(nav[-1])

    # 分批程度统计
    multi_lot = sum(1 for t in trades if (t.get("lots") or 1) > 1)
    partial_cnt = sum(1 for t in trades if t.get("partial"))

    return {
        "total_return": round(total_ret, 2),
        "annual_return": round(annual, 2),
        "max_drawdown": round(mdd, 2),
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 2),
        "win_rate": round(win_rate, 1),
        "trades": len(trades),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pl_ratio": round(pl_ratio, 2),
        "max_consec_loss": max_consec_loss,
        "avg_hold_days": round(avg_hold, 1),
        "reason_stats": reason_stats,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "benchmark_return": round(bh_ret, 2),
        "final_asset": round(final_net, 2),
        "init_cash": init_cash,
        "nav_curve": curve,
        "trade_list": trades[-50:],          # 最近 50 笔
        "multi_lot_exits": multi_lot,        # 由多个批次汇聚而成的平仓次数
        "partial_exits": partial_cnt,        # 部分减仓次数
        "pyramid_adds": pyr_count,           # 金字塔加仓次数
        "pyramid": bool(pyramid),
        "pyramid_step": pyramid_step if pyramid else None,
    }
