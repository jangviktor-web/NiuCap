#!/usr/bin/env python3
"""QuanTAlib 桥接：把 447 个交叉验证过的技术指标接进因子池
==================================================

QuanTAlib（Apache 2.0，mihakralj/QuanTAlib）：每个指标都与 TA-Lib / Tulip /
Skender / pandas-ta 交叉验证，分歧处回溯原始论文。Python 绑定是 NativeAOT
编译的原生 wheel（pip install quantalib，Linux x64 manylinux 可直接装），
streaming O(1) / batch SIMD，天生因果（无未来函数）。

为什么只做「因子实验室」入口而不全量替换自有指标：
  - 面板已有 indicator.py（K线图 UI 的 30 个经典指标）与 indicators_extra.py
    （信号用的十几个），各自有 UI 与回归依赖，替换是高风险零收益；
  - 因子池才是「多一个验证过的指标 = 多一个候选因子」的直接增量，
    factor_lab 的 IC/ICIR 流水线现成，桥接进来即测。
桥接纪律：
  - 逐列计算，样本 <30 的列保持 NaN（与因子池其他成员同纪律）；
  - 任何一列计算异常只废那一列，不炸整个矩阵；
  - qtl 部分实现（如 SMA warmup）用部分窗口填充而不是 NaN——因果、可用，
    因果性由 tests/check_causality.py 的 shift 检验兜底。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

AVAILABLE = True
_ERR = ""
try:
    import quantalib as qtl  # noqa: F401
except Exception as _e:      # noqa: BLE001
    AVAILABLE = False
    _ERR = f"{type(_e).__name__}: {_e}"


def column_factor(mats: Dict[str, Any], fn_name: str, *args,
                  key: str = "close", second: Optional[str] = None,
                  min_samples: int = 30, **kw) -> np.ndarray:
    """对横截面矩阵逐列调用 qtl 指标 → (T,N) 因子矩阵。

    second: 需要第二条序列的指标用（如 vortex 要 high/low/close 三条）。
    返回 (2,T,N) 元组的指标（vortex）取第一条减第二条——调用方按需包装。
    """
    C = mats[key]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    if not AVAILABLE:
        return out
    fn = getattr(qtl, fn_name, None)
    if fn is None:
        raise KeyError(f"quantalib 没有指标 {fn_name!r}（版本不匹配？）")
    C2 = mats[second] if second else None
    for j in range(N):
        col = C[:, j]
        ok = ~np.isnan(col)
        if ok.sum() < min_samples:
            continue
        try:
            if C2 is not None:
                r = fn(col[ok], C2[:, j][ok], *args, **kw)
            else:
                r = fn(col[ok], *args, **kw)
            if isinstance(r, tuple):
                r = np.asarray(r[0], dtype=float) - np.asarray(r[1], dtype=float)
            else:
                r = np.asarray(r, dtype=float)
            if r.shape != (int(ok.sum()),):
                continue
            out[ok, j] = r
        except Exception:    # noqa: BLE001
            continue         # 单列异常只废该列
    return out


def selfcheck() -> int:
    """与自有实现交叉验证 + NaN 纪律检查，不联网。"""
    bad = 0

    def ck(ok: bool, msg: str) -> None:
        nonlocal bad
        if not ok:
            bad += 1
            print(f"  ❌ {msg}")

    ck(AVAILABLE, f"quantalib 可导入（{_ERR}）")
    if not AVAILABLE:
        print("quantalib_bridge.selfcheck: SKIP（未安装）")
        return 0
    rng = np.random.default_rng(7)
    pd_c = np.maximum(np.cumsum(rng.normal(0, 0.5, 300)) + 100, 1)

    # 1) RSI 口径自证：单边上涨 ≈100、单边下跌 ≈0（Wilder 口径）。
    #    不与自有 indicator.RSI 对读——那是 MyTT 的 SMA 平滑口径，两者都对但不同
    up = np.linspace(100, 200, 100)
    dn = np.linspace(200, 100, 100)
    ck(float(qtl.rsi(up, 14)[-1]) > 95, "单边上涨 RSI ≈100")
    ck(float(qtl.rsi(dn, 14)[-1]) < 5, "单边下跌 RSI ≈0")

    # 2) SMA 对齐：qtl.sma 末端值应等于手算均值
    c = 100 + np.cumsum(rng.normal(0, 1, 300))
    tail = float(np.mean(c[-14:]))
    got = float(qtl.sma(c, 14)[-1])
    ck(abs(tail - got) < 1e-6, f"SMA 末端值等于手算（{got:.4f}）")

    # 3) 逐列纪律：<30 样本列保持 NaN、全 NaN 列保持 NaN
    short = np.where(np.arange(300) < 20, c, np.nan)   # 20 点 < min_samples
    mats = {"close": np.column_stack([c, np.full(300, np.nan), short])}
    m2 = column_factor(mats, "sma", 14)
    ck(not np.isnan(m2[299, 0]), "有效列有值")
    ck(bool(np.isnan(m2[:, 1]).all()), "全 NaN 列保持 NaN")
    ck(bool(np.isnan(m2[:, 2]).all()), "样本不足列保持 NaN")

    # 4) vortex 双线（签名 vortex(high, low, close, n)，返回 (vip, vim) 元组）
    try:
        vip, vim = qtl.vortex(pd_c, np.minimum(c - 1, pd_c - 1), c, 14)
        d = np.asarray(vip, dtype=float) - np.asarray(vim, dtype=float)
        ck(np.isfinite(d[-1]) and len(d) == 300, "vortex 双线取差可用")
    except Exception as e:   # noqa: BLE001
        ck(False, f"vortex 计算异常 {e}")

    print(f"quantalib_bridge.selfcheck: {'OK' if bad == 0 else str(bad) + ' 项失败'}")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if selfcheck() else 0)
