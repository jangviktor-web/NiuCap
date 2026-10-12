"""Tier 5：自然语言 → Tier 1 DSL 条件解析的回归测试。

直接复用 server/intent.py 的 selfcheck()，断言零失败。
覆盖：快照字段映射、单位换算（亿/万/元）、区间识别（含无"介于"写法）、
低估值自动补 pe>0、技术指标类归入 unsupported、组合语句、空输入。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import intent  # noqa: E402


def main():
    fails = intent.selfcheck()
    if fails != 0:
        print(f"[FAIL] intent 解析自检有 {fails} 项失败")
        sys.exit(1)
    print("[OK] intent 自然语言→条件解析 全部通过")


if __name__ == "__main__":
    main()
