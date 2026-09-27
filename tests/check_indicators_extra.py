"""离线自检：indicators_extra 指标 + 方案 A 6 策略可调用性（不依赖数据库）。

跑法：
    python3 tests/check_indicators_extra.py

验什么：
  1. indicators_extra.selfcheck() 全过（23 步）
  2. 6 个新策略都已注册、都声明 hist=True、都在 EVALUABLE_KEYS 白名单
  3. 用合成序列逐一构造「会触发」的场景，确认策略函数能跑出非空命中集
     （证明 screener 与 indicators_extra 的接线正确，而非只注册了名字）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import numpy as np

import indicators_extra as ix
import screener
import strategy_eval as se


results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


class FakeHist:
    """只实现 series(code)，模拟 real engine / _TruncatedEngine 的接口。"""

    def __init__(self, series):
        self._s = series

    def series(self, code):
        return self._s


def mk(close, high=None, low=None, volume=None, open_=None):
    """把 list 拼成 series dict（float64 转 float32 以贴近真实存储）。"""
    close = np.asarray(close, dtype=np.float32)
    n = len(close)
    return {
        "close": close,
        "high": np.asarray(high if high is not None else close, dtype=np.float32),
        "low": np.asarray(low if low is not None else close, dtype=np.float32),
        "open": np.asarray(open_ if open_ is not None else close, dtype=np.float32),
        "volume": np.asarray(volume if volume is not None else np.ones(n),
                            dtype=np.float32),
        "amount": np.asarray(np.ones(n), dtype=np.float32),
        "dates": [f"2026-01-{i:02d}" for i in range(1, n + 1)],
    }


def run(key, series):
    d = screener.STRATEGY_BY_KEY[key]
    rows = [{"code": "X"}]
    return d["fn"](rows, hist=FakeHist(series))


# ---------------------------------------------------------------------------
# 1. indicators_extra 自检
# ---------------------------------------------------------------------------
r = ix.selfcheck(verbose=False)
rec("indicators_extra 自检全过", r["ok"], f"{r['passed']}/{r['total']}")

# ---------------------------------------------------------------------------
# 2. 注册与白名单
# ---------------------------------------------------------------------------
NEW_KEYS = ("supertrend_long", "atr_breakout", "connors_rsi_dip",
            "td9_buy", "mfi_oversold", "cmf_breakout")
for key in NEW_KEYS:
    d = screener.STRATEGY_BY_KEY.get(key)
    rec(f"{key} 已注册且 hist=True",
        d is not None and d.get("hist") is True)
    rec(f"{key} 在 EVALUABLE_KEYS", key in se.EVALUABLE_KEYS)

# 白名单与 screener 对照：6 个新策略必须真实存在（reuse se 自检的逻辑）
rec("EVALUABLE_KEYS 增至 15", len(se.EVALUABLE_KEYS) == 15,
    f"{len(se.EVALUABLE_KEYS)} 个")

# ---------------------------------------------------------------------------
# 3. 触发场景：每个策略都应跑出非空命中
# ---------------------------------------------------------------------------

# 3.1 超级趋势翻多：先跌后涨，截到翻多点
seg_down = np.linspace(100, 70, 30)
seg_up = np.linspace(70, 90, 20)
seg = np.concatenate([seg_down, seg_up]).astype(float)
hh = seg + 1.0
ll = seg - 1.0
t, _, _ = ix.supertrend_series(hh, ll, seg, 10, 3.0)
flips = np.where((t[1:] == 1) & (t[:-1] == -1))[0] + 1
fi = int(flips[0]) if len(flips) else len(seg) - 1
s_st = mk(seg[:fi + 1], high=hh[:fi + 1], low=ll[:fi + 1])
rec("supertrend_long 翻多点触发", "X" in run("supertrend_long", s_st),
    f"flip@{fi}")

# 3.2 ATR 突破：长平台后一根巨阳
flat = [10.0] * 30
big = flat[:-1] + [10.0 + 100.0]     # 最后一根暴涨
s_atr = mk(big, high=[x + 0.5 for x in big], low=[x - 0.5 for x in big])
rec("atr_breakout 巨阳触发", "X" in run("atr_breakout", s_atr))

# 3.3 康纳丝超卖：前段平稳 + 末尾急跌（ROC 处于自身近期最低区）
base = [50.0] * 100
crash = [45, 40, 35, 30, 25]
s_crsi = mk(base + crash)
rec("connors_rsi_dip 急跌触发", "X" in run("connors_rsi_dip", s_crsi))

# 3.4 TD9 抄底：单调递减 13 根，末根计数恰为 9
decl = list(np.linspace(100, 60, 13))
s_td = mk(decl)
cnt = ix.td_count_last(np.asarray(decl, dtype=float))
rec("td9_buy 计满9触发", "X" in run("td9_buy", s_td), f"末计数={cnt}")

# 3.5 资金流超卖：价跌 + 量均匀 → MFI=0
mc = list(np.linspace(50, 30, 20))
s_mfi = mk(mc, high=[x + 1 for x in mc], low=[x - 1 for x in mc],
           volume=[1000] * 20)
rec("mfi_oversold 带量跌触发", "X" in run("mfi_oversold", s_mfi))

# 3.6 资金流入突破：价升且收在高位 → CMF>0.1；末根=20日新高
# 注意：close 必须贴近 high 才能让 CLV>0（high=close+1、low=close-1、close 居中时 CLV=0）
mc2 = np.arange(10, 31, dtype=float)          # 21 根，单调升
ch = mc2 + 1.0                                # high
cl = mc2 - 1.0                                # low
cc = mc2 + 0.8                                # close 贴近高位 → CLV≈+0.8
s_cmf = mk(cc.tolist(), high=ch.tolist(), low=cl.tolist(), volume=[500] * 21)
rec("cmf_breakout 量价齐升触发", "X" in run("cmf_breakout", s_cmf),
    f"CMF={ix.cmf_last(ch, cl, cc, np.full(21,500.0),20):.3f}")

# ---------------------------------------------------------------------------
# 4. 健壮性：随机数据下每个策略都返回 set（不崩、不返回 None）
# ---------------------------------------------------------------------------
rng = np.random.default_rng(7)
for key in NEW_KEYS:
    for _ in range(5):
        n = rng.integers(60, 120)
        c = np.cumsum(rng.normal(0, 1, int(n))) + 50
        s = mk(c.tolist(), high=(c + 0.5).tolist(), low=(c - 0.5).tolist(),
               volume=rng.integers(1, 5, int(n)).tolist())
        try:
            res = run(key, s)
            if not isinstance(res, set):
                rec(f"{key} 返回 set（随机数据）", False, f"类型={type(res)}")
                break
        except Exception as e:
            rec(f"{key} 随机数据不崩", False, f"{type(e).__name__}: {e}")
            break
    else:
        rec(f"{key} 返回 set（随机数据）", True)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print("== 汇总 ==")
    print(f"{passed}/{len(results)} 通过")
    sys.exit(0 if passed == len(results) else 1)
