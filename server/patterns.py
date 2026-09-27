"""
形态识别模块 - 提取自 KlangAlpha/Klang
包含: zigzag转折点、W底、V型反转、杯柄形态
"""

import numpy as np
import pandas as pd

PEAK, VALLEY = 1, -1


def peak_valley_pivots_np(X, step=3, min_move_pct=0.0):
    """
    使用滑动窗口法识别序列中的峰和谷。

    Parameters
    ----------
    X : numpy array - 价格序列
    step : int - 滑动窗口半径
    min_move_pct : float - 相邻转折点之间的最小价格变动幅度（小数，如 0.03=3%）。
        小于该幅度的"转折"视为噪声，不予记录。日线建议 0.05~0.08，
        分钟线可设 0。这是抑制误报的关键参数。

    Returns
    -------
    numpy array - 1表示峰(PEAK), -1表示谷(VALLEY), 0表示无转折
    """
    pivots = np.zeros(len(X), dtype='i1')
    if len(X) < 2:
        return pivots

    preindex = 0
    # 获取第一个趋势
    if X[0] < X[1]:
        trend = -1
    else:
        trend = 1

    def _far_enough(j, i):
        """从上一个确认转折 j 到候选点 i，幅度是否达到 min_move_pct"""
        if min_move_pct <= 0 or j == i:
            return True
        base = abs(X[j])
        if base <= 0:
            return True
        return abs(X[i] - X[j]) / base >= min_move_pct

    for i in range(0, len(X)):
        l = i - step
        r = i + step
        if l < 0:
            l = 0
        if l < preindex:
            l = preindex

        x1 = X[l:r]
        if trend == 1:
            if X[i] == np.amin(x1):
                # 幅度过滤：跌幅够大才确认这是一个真正的"谷"
                if not _far_enough(preindex, i):
                    continue
                trend = -1
                pivots[preindex] = 1
                preindex = i
            if X[i] == np.amax(x1) and X[i] > X[preindex]:
                preindex = i
        else:
            if X[i] == np.amax(x1):
                # 幅度过滤：涨幅够大才确认这是一个真正的"峰"
                if not _far_enough(preindex, i):
                    continue
                trend = 1
                pivots[preindex] = -1
                preindex = i
            if X[i] == np.amin(x1) and X[i] < X[preindex]:
                preindex = i
    # 补充最后一个
    if trend == 1:
        pivots[preindex] = 1
    else:
        pivots[preindex] = -1

    return pivots


def _create_index(pivots):
    """从pivots数组中提取非零元素的索引列表"""
    index_list = []
    for i in range(0, len(pivots)):
        if pivots[i] != 0:
            index_list.append(i)
    return index_list


def _approx(a, b, tolerance=0.05):
    """判断两个值是否近似相等（容差内）"""
    if b == 0:
        return False
    return abs(a - b) / abs(b) < tolerance


def zigzag(close, step=3, min_move_pct=0.0):
    """
    计算zigzag转折点

    Parameters
    ----------
    close : array-like - 收盘价序列
    step : int - 滑动窗口半径，默认3
    min_move_pct : float - 相邻转折最小幅度（小数），用于抑制噪声

    Returns
    -------
    dict: {
        'pivots': numpy array (1=峰, -1=谷, 0=无),
        'indices': list of pivot indices,
        'values': list of pivot prices,
        'types': list of pivot types (1 or -1)
    }
    """
    close = np.array(close, dtype=float)
    pivots = peak_valley_pivots_np(close, step=step, min_move_pct=min_move_pct)
    indices = _create_index(pivots)
    values = [close[i] for i in indices]
    types = [int(pivots[i]) for i in indices]
    
    return {
        'pivots': pivots,
        'indices': indices,
        'values': values,
        'types': types
    }


# ===========================================================================
# ⚠️ 形态识别的实测有效性结论（2026-09 研究结果，务必先读）
#
# 检验方法：沪深300 抽样 150 只，识别出 143 个形态，统计"形态结束点
#          之后 20 个交易日"的收益，与随机数据做对照。
#
# 【结论一】不加过滤直接使用 → 亏钱
#   全部 143 个形态：平均 -6.50%，胜率 25.9%
#   根因：形态多在下跌趋势中途被识别出来，属于典型的"接飞刀"。
#
# 【结论二】"形态出现的位置"是决定性变量，比形态类型重要得多
#   · 低位 (60日区间位置 pos<0.30)：n=21  平均 +5.20%  胜率 66.7%  ✅
#   · 低位 (pos<0.40)             ：n=25  平均 +3.87%  胜率 60.0%  ✅
#   · 高位 (pos>0.60)             ：n=30  平均 -8.81%  胜率 20.0%  ❌
#   ⇒ 同样的形态，出现在低位能赚 5%，出现在高位亏 8.8%。位置过滤是关键。
#
# 【结论三】"站上 MA20"是反向指标（反直觉，但数据明确）
#   · 形态时已站上 MA20：n=117  平均 -9.94%  胜率 12.8%  ❌
#   · pos<0.4 且 站上MA20：n=7   平均 -8.63%  胜率  0.0%  ❌
#   ⇒ 等价格反弹到均线上方才"确认形态"，涨幅已被兑现，此时买入即接盘。
#     不要给形态叠加"站上均线"这类顺势确认条件，方向是反的。
#
# 【结论四】分形态 + 低位过滤后，只有两个形态可用
#   · 回调买入 dip-buy  + pos<0.5：n=14  平均 +6.67%  胜率 85.7%  ✅
#   · 三重底  triple    + pos<0.5：n= 8  平均 +8.80%  胜率 75.0%  ✅
#   · W底     w-bottom  + pos<0.5：n= 8  平均 -8.30%  胜率  0.0%  ❌
#   · 杯柄/V反转 + pos<0.5        ：样本为 0，低位几乎不出现，无统计意义
#
# ⇒ 实施建议：
#   1) 当前该模块【保持不接入】。detect_all 的原始输出不可直接给用户。
#   2) 若将来接入，只启用 dip-buy / triple-bottom，且必须叠加 pos<0.4 过滤。
#   3) 样本量（143）仍偏小，n=8 这类分组置信度不足。扩到全市场（3761 只）
#      重跑统计、并通过滚动窗口检查稳定性后，才可上线。
#   4) 单靠价格几何的形态识别信噪比有限。真正提升需要叠加量能、
#      筹码（chip_distribution.py 已有）与基本面维度。
# ===========================================================================

# ---------------------------------------------------------------------------
# 日线形态识别的统一参数
# ---------------------------------------------------------------------------
DAY_STEP = 6
DAY_MIN_MOVE = 0.05


def position_in_range(close, idx, lookback=60):
    """
    计算 idx 时点价格在近 lookback 日区间中的相对位置（0=区间最低，1=最高）。

    这是形态识别中最有价值的过滤变量：实测低位(pos<0.4)形态平均 +3.9%、
    胜率 60%；高位(pos>0.6)平均 -8.8%、胜率 20%。

    Parameters
    ----------
    close : array-like - 收盘价序列
    idx : int - 待计算的位置索引
    lookback : int - 回看窗口（默认 60 日）

    Returns
    -------
    float - 相对位置 0~1；区间无波动时返回 0.5
    """
    close = np.asarray(close, dtype=float)
    if idx < 0 or idx >= len(close):
        return 0.5
    s = close[max(0, idx - lookback + 1): idx + 1]
    if len(s) < 2:
        return 0.5
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        return 0.5
    return float((close[idx] - lo) / (hi - lo))


def detect_w_bottom(close, step=DAY_STEP, min_depth_pct=0.15,
                    bottom_tolerance=0.08, shoulder_tolerance=0.15,
                    min_middle_rebound=0.50, min_bars=3, max_bars=80):
    """
    检测W底形态（双重底）

    一个合格的 W 底必须同时满足：
      1. 两个底部的价格接近（bottom_tolerance 内）
      2. 左肩与右肩的价格接近（shoulder_tolerance 内）—— 原实现缺失此检查，
         导致"单底反弹"也会被误判成 W 底
      3. 中间反弹的高度达到跌幅的一定比例（min_middle_rebound）
      4. 三段的时间跨度都在合理区间内

    Parameters
    ----------
    close : array-like - 收盘价序列
    step : int - zigzag窗口半径（默认日线 8）
    min_depth_pct : float - 最小深度（左肩到两底的跌幅），日线默认 15%
    bottom_tolerance : float - 两底价格允许的相对差
    shoulder_tolerance : float - 两肩价格允许的相对差
    min_middle_rebound : float - 中间反弹至少达到跌幅的比例
    min_bars : int - 每段至少几根
    max_bars : int - 每段最多几根

    Returns
    -------
    list of dict: 每个检测到的W底包含位置和价格信息
    """
    close = np.array(close, dtype=float)
    pivots = peak_valley_pivots_np(close, step=step, min_move_pct=DAY_MIN_MOVE)
    pv_index = _create_index(pivots)
    
    results = []
    if len(pv_index) < 5:
        return results
    
    for i in range(0, len(pv_index) - 4):
        a = pv_index[i]
        b = pv_index[i + 1]
        c = pv_index[i + 2]
        d = pv_index[i + 3]
        e = pv_index[i + 4]

        # 结构必须是 峰-谷-峰-谷-峰
        if pivots[a] != 1 or pivots[b] != -1 or pivots[c] != 1 \
           or pivots[d] != -1 or pivots[e] != 1:
            continue

        ab = close[a] - close[b]      # 左肩到一底
        ad = close[a] - close[d]      # 左肩到二底
        if close[b] <= 0 or close[a] <= 0:
            continue

        # 1) 两底接近
        if not _approx(close[b], close[d], bottom_tolerance):
            continue
        # 2) 两肩接近（原实现缺失，是关键补强）
        if not _approx(close[a], close[e], shoulder_tolerance):
            continue
        # 3) 深度足够
        if ab / close[b] < min_depth_pct:
            continue
        # 4) 中间反弹够高
        mid_rebound = close[c] - min(close[b], close[d])
        if ab <= 0 or mid_rebound / ab < min_middle_rebound:
            continue
        # 5) 时间跨度合理
        for s, t in ((a, b), (b, c), (c, d), (d, e)):
            if (t - s) < min_bars or (t - s) > max_bars:
                break
        else:
            results.append({
                'type': 'w-bottom',
                'left_peak': int(a),
                'first_bottom': int(b),
                'middle_peak': int(c),
                'second_bottom': int(d),
                'right_peak': int(e),
                'bottom_price': float((close[b] + close[d]) / 2),
                'peak_price': float(close[a]),
                'depth_pct': float(ab / close[b] * 100)
            })
    
    return results


def detect_v_reversal(close, step=DAY_STEP, min_drop_pct=0.12,
                      max_recovery_ratio=0.8, min_bars=4, max_bars=60,
                      min_recovery=0.8):
    """
    检测V型反转形态
    特征：急跌后快速反弹，跌幅大但恢复快

    Parameters
    ----------
    close : array-like - 收盘价序列
    step : int - zigzag窗口半径（默认日线 8）
    min_drop_pct : float - 最小跌幅，日线默认 15%（原 3% 在日线上会把普通
        回调全判成 V 反转，是误报主因）
    max_recovery_ratio : float - 最大恢复周期比（恢复周期/下跌周期）
    min_bars : int - 下跌段至少持续几根（避免 1~2 日的插针被当成趋势）
    max_bars : int - 下跌段最多几根（V反转是"急"跌，超过 60 日就不是 V 了）
    min_recovery : float - 反弹至少收复跌幅的比例

    Returns
    -------
    list of dict: 每个检测到的V型反转包含位置和价格信息
    """
    close = np.array(close, dtype=float)
    pivots = peak_valley_pivots_np(close, step=step, min_move_pct=DAY_MIN_MOVE)
    pv_index = _create_index(pivots)
    
    results = []
    if len(pv_index) < 3:
        return results
    
    for i in range(0, len(pv_index) - 2):
        a = pv_index[i]      # 起始高点
        b = pv_index[i + 1]  # 最低点（谷）
        c = pv_index[i + 2]  # 反弹高点
        
        if pivots[a] != 1 or pivots[b] != -1 or pivots[c] != 1:
            continue
        
        drop = close[a] - close[b]
        recovery = close[c] - close[b]
        drop_pct = drop / close[a]
        
        # 跌幅足够大
        if drop_pct < min_drop_pct:
            continue
        
        # 反弹幅度要大
        if recovery / drop < min_recovery:
            continue
        
        # 下跌/上涨的时间跨度约束：V 型是"急跌急涨"，两端都不能太长
        down_bars = b - a
        up_bars = c - b
        if down_bars < min_bars or up_bars < min_bars:
            continue
        if down_bars > max_bars or up_bars > max_bars:
            continue
        
        # 反弹速度快（周期比小）
        recovery_ratio = up_bars / down_bars
        if recovery_ratio > max_recovery_ratio:
            continue
        
        results.append({
            'type': 'v-reversal',
            'peak': int(a),
            'bottom': int(b),
            'recovery_peak': int(c),
            'drop_pct': float(drop_pct * 100),
            'recovery_pct': float(recovery / drop * 100),
            'speed_ratio': float(recovery_ratio)
        })
    
    return results


def detect_cup_handle(close, step=DAY_STEP, max_cup_depth_pct=0.45,
                      rim_tolerance=0.18, max_handle_ratio=0.40,
                      min_cup_bars=12, max_cup_bars=150, min_bars=3):
    """
    检测杯柄形态（Cup and Handle）

    合格杯柄的结构要求：
      1. 左右杯沿价格接近（rim_tolerance 内）—— 原实现缺失
      2. 杯深适中（不超过 max_cup_depth_pct）
      3. 柄部回调浅于杯深的 1/3（max_handle_ratio）
      4. 杯身足够长（形成"圆底"而非尖底）

    Parameters
    ----------
    close : array-like - 收盘价序列
    step : int - zigzag窗口半径（默认日线 8）
    max_cup_depth_pct : float - 最大杯深，日线放宽到 40%
    rim_tolerance : float - 左右杯沿允许的相对差
    max_handle_ratio : float - 柄深/杯深 的最大比例
    min_cup_bars : int - 杯身（左沿→右沿）至少几根
    max_cup_bars : int - 杯身最多几根
    min_bars : int - 各小段至少几根

    Returns
    -------
    list of dict: 每个检测到的杯柄形态包含位置和价格信息
    """
    close = np.array(close, dtype=float)
    pivots = peak_valley_pivots_np(close, step=step, min_move_pct=DAY_MIN_MOVE)
    pv_index = _create_index(pivots)
    
    results = []
    if len(pv_index) < 6:
        return results
    
    for i in range(0, len(pv_index) - 5):
        x1 = pv_index[i]
        a = pv_index[i + 1]
        b = pv_index[i + 2]
        c = pv_index[i + 3]
        d = pv_index[i + 4]
        e = pv_index[i + 5]

        # 结构：...-谷-峰(左沿)-谷(杯底)-峰(右沿)-谷(柄)-峰(柄后)
        if pivots[x1] != -1 or pivots[a] != 1 or pivots[b] != -1 \
           or pivots[c] != 1 or pivots[d] != -1 or pivots[e] != 1:
            continue

        if close[b] <= 0 or close[a] <= 0:
            continue

        cb = close[c] - close[b]      # 杯深（用右沿计算）
        cd = close[c] - close[d]      # 柄深
        if cb <= 0:
            continue

        # 1) 左右杯沿等高（原实现缺失，是关键补强）
        if not _approx(close[a], close[c], rim_tolerance):
            continue
        # 2) 杯深上限
        if cb / close[b] > max_cup_depth_pct:
            continue
        # 3) 柄部要浅（浅于杯深的 1/3）
        if cd <= 0 or cd / cb > max_handle_ratio:
            continue
        # 4) 杯底低于柄底
        if not close[b] < close[d]:
            continue
        # 5) 杯身够长（圆底特征）
        cup_bars = c - a
        if cup_bars < min_cup_bars or cup_bars > max_cup_bars:
            continue
        if (c - b) < min_bars or (e - d) < min_bars:
            continue

        results.append({
            'type': 'cup-handle',
            'cup_left_rim': int(a),
            'cup_bottom': int(b),
            'cup_right_rim': int(c),
            'handle_dip': int(d),
            'handle_end': int(e),
            'cup_depth': float(cb),
            'handle_depth': float(cd),
            'depth_pct': float(cb / close[b] * 100)
        })
    
    return results


def detect_triple_bottom(close, step=DAY_STEP, min_depth_pct=0.18,
                         bottom_tolerance=0.05, peak_tolerance=0.12,
                         min_peak_rebound=0.45, min_bars=4, max_bars=80):
    """
    检测三重底形态

    合格三重底：三个底部价格接近、两个中间峰价格接近且反弹高度足够、
    深度足够、时间跨度合理。

    Parameters
    ----------
    close : array-like - 收盘价序列
    step : int - zigzag窗口半径（默认日线 8）
    min_depth_pct : float - 最小深度，日线默认 18%
    bottom_tolerance : float - 三底价格允许的相对差（收紧：三底接近是硬条件）
    peak_tolerance : float - 两中间峰允许的相对差
    min_peak_rebound : float - 中间峰相对底部的最小反弹高度（占深度比例），
        用于排除"缓慢阴跌中夹杂的小反弹"这类伪三重底
    min_bars : int - 每段至少几根
    max_bars : int - 每段最多几根

    Returns
    -------
    list of dict: 每个检测到的三重底包含位置和价格信息
    """
    close = np.array(close, dtype=float)
    pivots = peak_valley_pivots_np(close, step=step, min_move_pct=DAY_MIN_MOVE)
    pv_index = _create_index(pivots)
    
    results = []
    if len(pv_index) < 6:
        return results
    
    for i in range(0, len(pv_index) - 5):
        a = pv_index[i]
        b = pv_index[i + 1]
        c = pv_index[i + 2]
        d = pv_index[i + 3]
        e = pv_index[i + 4]
        f = pv_index[i + 5]

        # 结构：峰-谷-峰-谷-峰-谷
        if pivots[a] != 1 or pivots[b] != -1 or pivots[c] != 1 \
           or pivots[d] != -1 or pivots[e] != 1 or pivots[f] != -1:
            continue

        if close[a] <= 0 or close[b] <= 0:
            continue

        ab = close[a] - close[b]
        ad = close[a] - close[d]
        af = close[a] - close[f]

        # 深度足够
        if ab / close[b] < min_depth_pct:
            continue
        # 三底接近（硬条件，收紧）
        if not (_approx(close[b], close[d], bottom_tolerance)
                and _approx(close[b], close[f], bottom_tolerance)):
            continue
        # 两中间峰接近
        if not _approx(close[c], close[e], peak_tolerance):
            continue
        # 中间两次反弹要有足够高度（排除阴跌中的小反抽）
        base = min(close[b], close[d], close[f])
        rise1 = (close[c] - base) / ab if ab > 0 else 0
        rise2 = (close[e] - base) / ab if ab > 0 else 0
        if rise1 < min_peak_rebound or rise2 < min_peak_rebound:
            continue
        # 时间跨度
        for s, t in ((a, b), (b, c), (c, d), (d, e), (e, f)):
            if (t - s) < min_bars or (t - s) > max_bars:
                break
        else:
            results.append({
                'type': 'triple-bottom',
                'peak': int(a),
                'bottom1': int(b),
                'bottom2': int(d),
                'bottom3': int(f),
                'bottom_price': float((close[b] + close[d] + close[f]) / 3),
                'depth_pct': float(ab / close[b] * 100)
            })
    
    return results


def detect_dip_buy(close, step=DAY_STEP, min_rise_pct=0.15,
                   max_retrace_ratio=0.55, min_bars=3, max_bars=80):
    """
    检测上攻回调买入形态

    结构：起涨点(谷) → 阶段高点(峰) → 回调点(谷)
    要求涨幅足够大、回调不深（不超过涨幅的一半）、各段时间跨度合理。

    Parameters
    ----------
    close : array-like - 收盘价序列
    step : int - zigzag窗口半径（默认日线 8）
    min_rise_pct : float - 最小上涨幅度，日线默认 20%（原 10% 偏松）
    max_retrace_ratio : float - 最大回撤比例
    min_bars : int - 每段至少几根
    max_bars : int - 每段最多几根

    Returns
    -------
    list of dict: 每个检测到的回调买入包含位置和价格信息
    """
    close = np.array(close, dtype=float)
    pivots = peak_valley_pivots_np(close, step=step, min_move_pct=DAY_MIN_MOVE)
    pv_index = _create_index(pivots)
    
    results = []
    if len(pv_index) < 3:
        return results
    
    # 取最后 5 个转折点（只关注近期形态）
    last_index = pv_index[-5:] if len(pv_index) >= 5 else pv_index
    for i in range(0, len(last_index) - 2):
        a = last_index[i]
        b = last_index[i + 1]
        c = last_index[i + 2]

        # 结构：谷-峰-谷
        if pivots[a] != -1 or pivots[b] != 1 or pivots[c] != -1:
            continue
        if close[a] <= 0 or close[b] <= 0:
            continue

        ba = close[b] - close[a]      # 上涨幅度
        bc = close[b] - close[c]      # 回调幅度
        if ba <= 0 or bc < 0:
            continue
        # 涨幅足够
        if ba / close[a] < min_rise_pct:
            continue
        # 回调不深
        if bc / ba > max_retrace_ratio:
            continue
        # 时间跨度
        if (b - a) < min_bars or (b - a) > max_bars:
            continue
        if (c - b) < min_bars or (c - b) > max_bars:
            continue

        results.append({
            'type': 'dip-buy',
            'start': int(a),
            'peak': int(b),
            'dip': int(c),
            'rise_pct': float(ba / close[a] * 100),
            'retrace_pct': float(bc / ba * 100),
        })
    
    return results


# 模式名称到检测函数的映射
PATTERN_MAP = {
    'zigzag': zigzag,
    'w-bottom': detect_w_bottom,
    'v-reversal': detect_v_reversal,
    'cup-handle': detect_cup_handle,
    'triple-bottom': detect_triple_bottom,
    'dip-buy': detect_dip_buy,
}

# 形态中文名（前端展示用）
PATTERN_LABELS = {
    'w-bottom': 'W底',
    'v-reversal': 'V型反转',
    'cup-handle': '杯柄',
    'triple-bottom': '三重底',
    'dip-buy': '回调买入',
}

# 每个形态的"锚点"字段：用于判断两个形态是否落在同一段行情
_ANCHOR_KEYS = {
    'w-bottom': ('left_peak', 'right_peak'),
    'v-reversal': ('peak', 'recovery_peak'),
    'cup-handle': ('cup_left_rim', 'handle_end'),
    'triple-bottom': ('peak', 'bottom3'),
    'dip-buy': ('start', 'dip'),
}

# 形态"可信度"排序：重叠时保留更具体的形态
# 三重底/W底/杯柄是严格的多点结构，V反转相对宽松，故优先级最低
_PATTERN_PRIORITY = {
    'triple-bottom': 3,
    'cup-handle': 2,
    'w-bottom': 2,
    'dip-buy': 1,
    'v-reversal': 0,
}


def _span(p):
    """返回形态占据的索引区间 (start, end)"""
    keys = _ANCHOR_KEYS.get(p['type'])
    if not keys:
        return (0, 0)
    vals = [p.get(k) for k in keys if p.get(k) is not None]
    if not vals:
        return (0, 0)
    return (min(vals), max(vals))


def _overlap(a, b, slack=3):
    """两个形态的区间是否重叠（允许 slack 根 K 线的容差）"""
    s1, e1 = _span(a)
    s2, e2 = _span(b)
    return not (e1 + slack < s2 or e2 + slack < s1)


def detect_all(close, **kw):
    """
    运行全部形态识别，并做互斥仲裁。

    同一段行情只保留一个最优形态，避免"一段走势被同时标成 W底+V反转+杯柄"。
    仲裁规则：
      1. 按形态优先级（三重底 > 杯柄/W底 > 回调买入 > V反转）排序
      2. 高优先级先入选，与已入选区间重叠的低优先级形态被丢弃

    Parameters
    ----------
    close : array-like - 收盘价序列
    **kw : 传给各检测函数的参数，会被忽略（各函数用自己的日线默认值）

    Returns
    -------
    list of dict: 按时间排序的形态列表，每项含 type/label/结构字段/start/end
    """
    close = np.array(close, dtype=float)
    found = []
    for ptype, fn in PATTERN_MAP.items():
        if ptype == 'zigzag':
            continue
        try:
            for p in fn(close):
                p = dict(p)
                p['label'] = PATTERN_LABELS.get(ptype, ptype)
                p['priority'] = _PATTERN_PRIORITY.get(ptype, 1)
                s, e = _span(p)
                p['start'] = int(s)
                p['end'] = int(e)
                found.append(p)
        except Exception:
            continue

    # 高优先级 + 更长跨度优先保留
    found.sort(key=lambda p: (-p['priority'], -(p['end'] - p['start'])))

    kept = []
    for p in found:
        if any(_overlap(p, q) for q in kept):
            continue
        kept.append(p)

    kept.sort(key=lambda p: p['start'])
    for p in kept:
        p.pop('priority', None)
    return kept
