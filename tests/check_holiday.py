# tests/check_holiday.py
"""#104 回归：本地节假日表 + market_state 交易日历判断。

验证：
  1) 工作日法定休市日 -> holiday / 不可交易（核心：避免节假日按上一日收盘价成交）
  2) 周末中的调休补班日 -> 照常按交易时段判断（可交易）
  3) 普通周末 -> holiday
  4) 普通交易日 -> trading / closed（时段不受影响）
  5) 前端内联表（PAPER_HOLIDAYS / PAPER_MAKEUP）与后端 holidays.py 完全一致
"""
import sys, time, re, os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import holidays as hl
import datasource as ds


def st_at(y, m, d, H=10, M=0):
    # 用 mktime 在本地时区(CST)构造，再 localtime 推导星期，避免手算 wd 出错
    tt = time.mktime((y, m, d, H, M, 0, 0, 0, -1))
    return time.localtime(tt)


# (描述, struct_time, 期望state, 期望is_trading)
CASES = [
    ("2026-10-01 国庆(周四) 盘中",   st_at(2026, 10, 1),  "holiday", False),
    ("2026-02-17 春节(周三) 盘中",   st_at(2026, 2, 17),  "holiday", False),
    ("2026-04-06 清明(周一) 盘中",   st_at(2026, 4, 6),   "holiday", False),
    ("2026-10-03 普通周六",          st_at(2026, 10, 3),  "holiday", False),
    ("2026-10-10 周六补班 盘中",     st_at(2026, 10, 10), "trading", True),
    ("2026-01-04 周日补班 盘中",     st_at(2026, 1, 4),   "trading", True),
    ("2026-10-09 周五 盘中",         st_at(2026, 10, 9),  "trading", True),
    ("2026-10-09 周五 收盘后",       st_at(2026, 10, 9, 16, 0), "closed", False),
    ("2027-01-01 元旦(周五) 盘中",   st_at(2027, 1, 1),   "holiday", False),
    ("2027-10-05 国庆(周二) 盘中",   st_at(2027, 10, 5),  "holiday", False),
]


def check_market_state():
    ok = True
    for desc, t, exp_state, exp_trade in CASES:
        st = ds.market_state(t)
        got_state = st["state"]
        got_trade = st["is_trading"]
        good = (got_state == exp_state and got_trade == exp_trade)
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'}  {desc:<28} -> state={got_state:<8} trade={got_trade} "
              f"(期望 {exp_state}/{exp_trade})")
    return ok


def check_frontend_sync():
    """前端内联节假日表必须与后端 holidays.py 完全一致，否则前后端口径漂移。"""
    html = open(os.path.join(HERE, "..", "web", "index.html"), encoding="utf-8").read()

    def block(name):
        m = re.search(r"var %s = \{(.*?)\};" % name, html, re.S)
        if not m:
            return set()
        return set(re.findall(r"'(20\d\d-\d\d-\d\d)':1", m.group(1)))

    fe_h = block("PAPER_HOLIDAYS")
    fe_m = block("PAPER_MAKEUP")
    be_h = hl.HOLIDAYS
    be_m = hl.MAKEUP_WORKDAYS

    ok_h = (fe_h == be_h)
    ok_m = (fe_m == be_m)
    if not ok_h:
        print("FAIL  前端休市日与后端不一致")
        print("       仅前端有:", sorted(fe_h - be_h))
        print("       仅后端有:", sorted(be_h - fe_h))
    else:
        print(f"PASS  前端休市日({len(fe_h)}) 与后端完全一致")
    if not ok_m:
        print("FAIL  前端补班日与后端不一致")
        print("       仅前端有:", sorted(fe_m - be_m))
        print("       仅后端有:", sorted(be_m - fe_m))
    else:
        print(f"PASS  前端补班日({len(fe_m)}) 与后端完全一致")
    return ok_h and ok_m


def main():
    print("== #104 本地节假日表回归 ==")
    ok1 = check_market_state()
    print("")
    ok2 = check_frontend_sync()
    ok = ok1 and ok2
    print("")
    print("结果:", "全部通过 ✅" if ok else "存在失败 ❌")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
