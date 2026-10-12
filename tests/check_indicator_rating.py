"""Tier 4：技术指标 → 多空评级（BUY/SELL/NEUTRAL）回归测试。

直接复用 server/indicator_rating.py 的 selfcheck()，断言零失败。
覆盖：震荡/趋势/通道/波动率各类指标阈值、float 边界、配对指标（DIF/DEA、
PDI/MDI、TRIX/TRMA）、价格位置（BOLL/唐安奇/肯特纳/吊灯）、缺失/无规则、聚合。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import indicator_rating as ir  # noqa: E402


def main():
    rc = ir.selfcheck(verbose=True)
    if rc["fail"] != 0:
        print(f"[FAIL] indicator_rating 自检有 {rc['fail']} 项失败（共 {rc['total']} 例）")
        sys.exit(1)
    print(f"[OK] indicator_rating 多空评级 全部通过（{rc['total']} 例）")


if __name__ == "__main__":
    main()
