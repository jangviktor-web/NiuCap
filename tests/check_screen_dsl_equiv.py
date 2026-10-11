"""新旧选股筛选实现等价性对照测试。

为什么要有这个文件
------------------
``/api/screener`` 从「手写 ``if ... return False``」改成「翻译 + 通用算子引擎」
时，最大的风险不是写不出来，而是**行为悄悄漂移**：旧实现有一批非对称语义
（PE/PB 传了区间才补 >0 门槛、缺失值按 0 参与比较……），一旦翻译漏掉任何一条，
用户按同样的参数会拿到不同的股票，而且这种差异不会报错、不会崩，只会静默
少给或多给结果 —— 是所有回归里最难被发现的那一类。

所以这里把改动前的实现**原样保留**作为金标准，用随机数据做穷举对照：
只要还有一行不一致就说明翻译没翻对。

用法：``python3.11 tests/check_screen_dsl_equiv.py``
"""

import random
import sys

sys.path.insert(0, "server")

import screen_dsl as sdsl  # noqa: E402

# ---------------------------------------------------------------------------
# 金标准：改动前 /api/screener 里的 ok() 实现，逐行照抄
# ---------------------------------------------------------------------------


def legacy_ok_factory(pe_min, pe_max, pb_min, pb_max, cap_min, cap_max,
                      chg_min, chg_max, turnover_min, turnover_max,
                      amount_min, market, exclude_st, exclude_bj):
    """复刻旧实现，返回接受 row -> bool 的判定函数。"""

    def ok(r):
        pe, pb = r.get("pe") or 0, r.get("pb") or 0
        cap, chg = r.get("total_cap") or 0, r.get("change_pct") or 0
        tov, amt = r.get("turnover") or 0, r.get("amount") or 0

        if exclude_st and ("ST" in r["name"].upper() or "退" in r["name"]):
            return False
        if exclude_bj and r["code"].startswith("bj"):
            return False
        if market and not r["code"].startswith(market):
            return False

        # PE 区间（PE<=0 视为无效，仅在未指定区间时保留）
        if pe_min is not None or pe_max is not None:
            if pe <= 0:
                return False
            if pe_min is not None and pe < pe_min:
                return False
            if pe_max is not None and pe > pe_max:
                return False
        if pb_min is not None and (pb <= 0 or pb < pb_min):
            return False
        if pb_max is not None and (pb <= 0 or pb > pb_max):
            return False

        if cap_min is not None and cap < cap_min:
            return False
        if cap_max is not None and cap > cap_max:
            return False
        if chg_min is not None and chg < chg_min:
            return False
        if chg_max is not None and chg > chg_max:
            return False
        if turnover_min is not None and tov < turnover_min:
            return False
        if turnover_max is not None and tov > turnover_max:
            return False
        if amount_min is not None and amt < amount_min:
            return False
        return True

    return ok


def new_ok_factory(pe_min, pe_max, pb_min, pb_max, cap_min, cap_max,
                   chg_min, chg_max, turnover_min, turnover_max,
                   amount_min, market, exclude_st, exclude_bj):
    """走引擎的新实现（与 app.py 中一致的组合方式）。"""
    from app import _legacy_screen_conditions  # 延迟导入避免触发 app 副作用

    conds = _legacy_screen_conditions(
        pe_min, pe_max, pb_min, pb_max, cap_min, cap_max,
        chg_min, chg_max, turnover_min, turnover_max, amount_min)
    if market:
        conds = conds + [sdsl.compile_condition(
            {"field": "market", "op": "eq", "value": market})]

    def ok(r):
        if exclude_st and ("ST" in str(r.get("name") or "").upper()
                           or "退" in str(r.get("name") or "")):
            return False
        if exclude_bj and str(r.get("code") or "").startswith("bj"):
            return False
        return sdsl.match_row(r, conds)

    return ok


# ---------------------------------------------------------------------------
# 随机数据生成
# ---------------------------------------------------------------------------

NAMES = ["贵州茅台", "平安银行", "ST银河", "退市亚泰", "宁德时代", "中信证券",
         "中国建筑", "招商银行", "立讯精密", "万科A"]
PREFIX = ["sh", "sz", "bj"]


def maybe_none(v, p=0.25):
    """按概率返回 None，模拟上游取不到字段的情形。"""
    return v if random.random() > p else None


def maybe_zero(v, p=0.2):
    """按概率把值变成 0，模拟上游「取不到就填 0」的既有编码。"""
    return 0 if random.random() < p else v


def rand_row(i):
    pre = random.choice(PREFIX)
    return {
        "code": f"{pre}{600000 + i}",
        "symbol": f"{600000 + i}",
        "name": random.choice(NAMES),
        "price": maybe_none(round(random.uniform(2, 2000), 2)),
        "change_pct": maybe_none(round(random.uniform(-10.1, 10.1), 2)),
        "pe": maybe_zero(maybe_none(round(random.uniform(-5, 80), 2))),
        "pb": maybe_zero(maybe_none(round(random.uniform(-1, 15), 3))),
        "total_cap": maybe_zero(round(random.uniform(5, 30000), 1)),
        "float_cap": maybe_zero(round(random.uniform(5, 30000), 1)),
        "turnover": maybe_none(round(random.uniform(0, 25), 2)),
        "amount": maybe_none(round(random.uniform(0, 5e6), 1)),
        "volume": maybe_none(round(random.uniform(0, 1e7), 0)),
        "open": maybe_none(round(random.uniform(2, 2000), 2)),
        "high": maybe_none(round(random.uniform(2, 2100), 2)),
        "low": maybe_none(round(random.uniform(1, 1900), 2)),
        "prev_close": maybe_none(round(random.uniform(2, 2000), 2)),
    }


def rand_bound(lo, hi):
    """生成可能是 None（未设置）或具体值的边界参数。"""
    if random.random() < 0.45:
        return None
    return round(random.uniform(lo, hi), 2)


# ---------------------------------------------------------------------------


def main():
    random.seed(20261012)
    print("新旧选股筛选等价性对照\n" + "=" * 46)

    n_fail = 0

    # 覆盖 AE{o}攻坚点：边界值必须包含 0
    rows = [rand_row(i) for i in range(400)]
    # 手工补一批"刚好踩线"的行，边界漂移最容易在这类数据上暴露
    rows += [
        {"code": "sh600001", "name": "踩线A", "pe": 20.0, "pb": 1.5,
         "total_cap": 100.0, "float_cap": 90.0, "turnover": 3.0,
         "amount": 5000.0, "volume": 100.0, "change_pct": 5.0,
         "price": 10.0, "open": 9.0, "high": 11.0, "low": 8.0,
         "prev_close": 9.5},
        {"code": "sh600002", "name": "踩线B", "pe": 0, "pb": 0,
         "total_cap": 0, "float_cap": 0, "turnover": 0,
         "amount": 0, "volume": 0, "change_pct": 0,
         "price": 0, "open": 0, "high": 0, "low": 0, "prev_close": 0},
        {"code": "sz000003", "name": "ST踩线", "pe": -3.0, "pb": -0.5,
         "total_cap": -1.0, "float_cap": -1.0, "turnover": -1.0,
         "amount": -1.0, "volume": 0, "change_pct": -5.0,
         "price": 1.0, "open": 1.0, "high": 1.0, "low": 1.0,
         "prev_close": 1.0},
    ]

    cases = []
    # 1) 纯随机排列组合
    for _ in range(3000):
        cases.append(dict(
            pe_min=rand_bound(-10, 60), pe_max=rand_bound(-10, 60),
            pb_min=rand_bound(-2, 10), pb_max=rand_bound(-2, 10),
            cap_min=rand_bound(-100, 25000), cap_max=rand_bound(-100, 25000),
            chg_min=rand_bound(-11, 11), chg_max=rand_bound(-11, 11),
            turnover_min=rand_bound(-1, 25), turnover_max=rand_bound(-1, 25),
            amount_min=rand_bound(-1000, 4e6),
            market=random.choice(["", "", "sh", "sz", "bj"]),
            exclude_st=random.random() < 0.7,
            exclude_bj=random.random() < 0.5,
        ))
    # 2) 极限场景：全部为空 / 只设一端 / 0 值边界
    keys = list(cases[0].keys())
    cases.append({k: None for k in keys} | {"market": "", "exclude_st": False,
                                            "exclude_bj": False})
    for zero in (0, 0.0):
        for k in ("pe_min", "pb_min", "cap_min", "chg_min", "turnover_min",
                  "amount_min"):
            c = {kk: None for kk in keys} | {"market": "", "exclude_st": False,
                                             "exclude_bj": False}
            c[k] = zero
            cases.append(c)

    total_cmp = 0
    mismatch_samples = []
    for ci, c in enumerate(cases):
        try:
            old = legacy_ok_factory(**c)
            new = new_ok_factory(**c)
        except Exception as e:  # pragma: no cover
            print(f"  ❌ 用例#{ci} 构造失败：{e}")
            n_fail += 1
            continue
        for r in rows:
            total_cmp += 1
            a, b = old(r), new(r)
            if a != b:
                n_fail += 1
                if len(mismatch_samples) < 5:
                    mismatch_samples.append((ci, c, r, a, b))

    print(f"  用例数：{len(cases)} 组参数")
    print(f"  行情行：{len(rows)} 条（含 ST/退市/负值/全 0 踩线行）")
    print(f"  比对次数：{total_cmp:,}")
    for s in mismatch_samples:
        ci, c, r, a, b = s
        print(f"  ❌ 用例#{ci} 不一致：旧={a} 新={b}")
        print(f"     参数={{{', '.join(f'{k}:{v}' for k, v in c.items() if v is not None and v is not False and v != '')}}}")
        print(f"     行: code={r.get('code')} name={r.get('name')} "
              f"pe={r.get('pe')} pb={r.get('pb')} cap={r.get('total_cap')}")

    if n_fail == 0:
        print(f"\n  ✅ 全部一致：{total_cmp:,} 次比对零差异")
    else:
        print(f"\n  ❌ 发现 {n_fail} 处不一致")

    print("\n结果:", "全部通过" if n_fail == 0 else "存在失败")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
