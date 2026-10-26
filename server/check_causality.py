"""因果性检查 / 快照可复现的自证。

一个"检查器"最危险的失败模式是**永远通过**：界面上一排绿勾，没人会去验证
绿勾是不是真的在看东西。所以这里的断言分两类：

  · 正向：故意写坏的因子**必须**被抓出来（三类前视各一个）
  · 反向：真实因子与干净的自定义因子**必须**不被冤枉

跑法（不需要服务，不需要真实库）：
    cd server && ../.venv/bin/python check_causality.py
退出码：全过 0，失败 1。
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple

import numpy as np

import causality as ca
import snapshot as sn

_STEPS: List[Tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    _STEPS.append((name, bool(ok), detail))


def mk_mats(T: int = 300, N: int = 25, seed: int = 0) -> Dict[str, Any]:
    """合成行情：几何随机游走 + 随机量，OHLC 自洽，日期为真实可解析的交易日串。

    日期必须是 `YYYY-MM-DD`：快照会做缺口检测，解析不了就整份拒绝 ——
    这正是我们要的行为（宁可拒也不猜），所以测试数据不能图省事用 "d0001"。
    """
    import datetime as _dt
    rng = np.random.default_rng(seed)
    C = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.02, (T, N)), axis=0)
    V = rng.uniform(1e6, 5e6, (T, N))
    start = _dt.date(2025, 1, 1)
    dates, d, i = [], start, 0
    while len(dates) < T:                       # 只取工作日，贴近真实交易日
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return {
        "dates": dates,
        "codes": [f"c{j:03d}" for j in range(N)],
        "close": C, "open": C,
        "high": C * 1.01, "low": C * 0.99,
        "volume": V, "amount": C * V,
    }


# ---------------------------------------------------------------------------
# 1. 正向：三类前视必须被抓
# ---------------------------------------------------------------------------

def _bad_lookahead(m):
    """越界读：把明天的收盘价掺进来（错位一格）。"""
    C = m["close"]
    out = 10.0 * np.ones_like(C)
    out[:-1] = C[1:] / C[:-1] - 1.0
    return out


def _bad_roll(m):
    """np.roll(C, -1)：整体前移一位，最典型的手滑。"""
    C = m["close"]
    return np.roll(C, -1, axis=0) / C - 1.0


def _bad_zscore(m):
    """全样本标准化：没越界，但均值和标准差用到了未来。"""
    C = m["close"]
    return (C - C.mean()) / C.std()


def _bad_tail(m):
    """拿最后一行当基准：只有"知道序列会在哪结束"才可能算出来。"""
    C = m["close"]
    return C / C[-1]


def _good_causal_mom(m, n=20):
    """正确写法：只用 t-n 到 t。"""
    C = m["close"]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    if T > n:
        out[n:] = C[n:] / C[:-n] - 1.0
    return out


def _good_expanding_mean(m):
    """扩展窗口均值：只累积过去，应当通过。"""
    C = m["close"]
    cs = np.cumsum(C, axis=0)
    cnt = np.arange(1, C.shape[0] + 1)[:, None]
    return cs / cnt


# ---------------------------------------------------------------------------
# 2. 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    m = mk_mats()

    # ── 正向：坏因子
    for name, fn in (("越界读（明天价格掺进来）", _bad_lookahead),
                     ("np.roll 前移一位", _bad_roll),
                     ("全样本 z-score（统计量泄漏）", _bad_zscore),
                     ("以最后一行为基准（偷看终点）", _bad_tail)):
        r = ca.check_factor(m, fn, name)
        rec(f"抓出前视：{name}", r["ok"] is False, r["verdict"][:60])

    # ── 反向：好因子
    for name, fn in (("正确的 20 日动量", _good_causal_mom),
                     ("扩展窗口均值（只累积过去）", _good_expanding_mean)):
        r = ca.check_factor(m, fn, name)
        rec(f"不冤枉：{name}", r["ok"] is True, r["verdict"][:60])

    # ── 真实因子：全部应通过
    try:
        import factor_lab as fl
        fns = fl.factor_fns()
        bad = []
        for k, f in fns.items():
            r = ca.check_factor(m, f, k)
            if not r["ok"]:
                bad.append((k, r["verdict"]))
        rec(f"真实因子 {len(fns)} 个全部通过", not bad, str(bad[:1]))
    except Exception as e:                      # pragma: no cover - 环境缺依赖
        rec("真实因子检查（跳过）", False, f"{type(e).__name__}: {e}")

    # ── 混进一个好端端的假因子堆里：只有它该红，别人不该被带背
    #    （分派表写错会让因子凭空消失，那样"全部通过"会变成假绿，所以这里
    #      拿真实 fns 当基线，多塞一个前视因子进去。）
    try:
        import factor_lab as fl
        base = fl.factor_fns()

        def _leaky(mm, n=20):
            C = mm["close"]
            out = np.full_like(C, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                out[:-1] = C[1:] / C[:-1] - 1.0     # 错位一格：偷看明天
            return out

        mixed = dict(base)
        mixed["__leak__"] = _leaky
        r = ca.check_all(m, mixed)
        # 注意 failed 是**计数**（int），要拿 key 得从 items 里捞
        bad_keys = [i["key"] for i in r["items"] if not i["ok"]]
        rec("混入前视因子后整体报红", r["ok"] is False, str(bad_keys))
        rec("且有且仅有它一个报红", bad_keys == ["__leak__"], str(bad_keys))
        rec("failed 计数与 items 一致", r["failed"] == len(bad_keys),
            f"failed={r['failed']} items={len(bad_keys)}")
        other_bad = [k for k in bad_keys if k != "__leak__"]
        rec("其余真实因子未被牵连", not other_bad, str(other_bad[:2]))
    except Exception as e:                      # pragma: no cover
        rec("前视因子混检（跳过）", False, f"{type(e).__name__}: {e}")

    # ── 分派表写错 fname 时必须报错，不能静默少一个因子
    try:
        import factor_lab as fl
        orig = fl.FACTOR_DEFS
        fl.FACTOR_DEFS = list(orig) + [("__typo__", "拼错", "动量", "_f_nonexistent")]
        try:
            fl.factor_fns()
            raised = False
        except KeyError as e:
            raised = "_f_nonexistent" in str(e)
        finally:
            fl.FACTOR_DEFS = orig
        rec("fname 拼错时抛 KeyError（不静默丢因子）", raised)
    except Exception as e:                      # pragma: no cover
        rec("fname 拼错检查（跳过）", False, f"{type(e).__name__}: {e}")

    # ── 边界：池子恰好等于选股数那种"退化为唯一解"的场景不归本模块管，
    #    这里只验证因果模块自身的边界
    short = mk_mats(T=10, N=5)
    r = ca.check_factor(short, _good_causal_mom, "too_short")
    rec("序列太短时明确说不适用（不给假绿勾）",
        r["ok"] is False and "不足" in r["verdict"], r["verdict"][:40])

    # 非确定性函数：必须拒绝而不是给绿勾
    rng = np.random.default_rng(7)

    def _rand(m):
        return rng.normal(size=m["close"].shape)
    r = ca.check_factor(m, _rand, "random")
    rec("含随机性的函数被标为不适用",
        r["ok"] is False and r["deterministic"] is False, r["verdict"][:40])

    # 形状不对：要说清要什么、给了什么
    r = ca.check_factor(m, lambda mm: mm["close"][:, :3], "wrong_shape")
    rec("返回形状不对时给出期望与实际", r["ok"] is False and "形状" in r["verdict"],
        r["verdict"][:40])

    # ── 快照：同一份数据同哈希 / 改数据换哈希
    s1 = sn.freeze(m, today=m["dates"][-1])
    s2 = sn.freeze(m, today=m["dates"][-1])
    rec("快照 ok", s1["ok"] is True)
    rec("同数据两次哈希一致", s1["sha256"] == s2["sha256"],
        (s1["sha256"] or "")[:12])

    m2 = {k: (v.copy() if isinstance(v, np.ndarray) else list(v))
          for k, v in m.items()}
    m2["close"][50, 3] *= 1.0001               # 动一格
    s3 = sn.freeze(m2, today=m["dates"][-1])
    rec("改一个价格 → 哈希变化", s1["sha256"] != s3["sha256"],
        f"{(s1['sha256'] or '')[:8]} vs {(s3['sha256'] or '')[:8]}")

    # 行序无关：把矩阵列顺序打乱再冻结，哈希应当不变（因为按 code 排序拼接）
    perm = list(range(m["close"].shape[1]))[::-1]
    m3 = {k: (v[:, perm] if isinstance(v, np.ndarray) and v.ndim == 2
              else ([m["codes"][i] for i in perm] if k == "codes" else v))
          for k, v in m.items()}
    s4 = sn.freeze(m3, today=m["dates"][-1])
    rec("列顺序打乱后哈希不变（与查询顺序无关）", s1["sha256"] == s4["sha256"])

    # 脏数据必须拒绝并报坐标
    md = {k: (v.copy() if isinstance(v, np.ndarray) else list(v))
          for k, v in m.items()}
    md["high"][10, 2] = md["close"][10, 2] * 0.5
    sd = sn.freeze(md, today=m["dates"][-1])
    kinds = [p["kind"] for p in sd["problems"]]
    rec("high 低于收盘价 → 拒绝并报出", sd["ok"] is False and "high_lt_body" in kinds,
        str(sd["problems"][:1]))
    rec("脏数据不给哈希", sd["sha256"] is None)

    # 日期重复 / 乱序
    du = {k: (v.copy() if isinstance(v, np.ndarray) else list(v)) for k, v in m.items()}
    du["dates"] = list(m["dates"])
    du["dates"][5] = du["dates"][4]
    rec("日期重复 → 拒绝",
        "duplicate_date" in [p["kind"] for p in
                             sn.freeze(du, today=m["dates"][-1])["problems"]])

    # 未定稿 bar：今天那根要被丢弃
    today = "2026-10-26"
    mu = {k: (v.copy() if isinstance(v, np.ndarray) else list(v)) for k, v in m.items()}
    mu["dates"] = list(m["dates"])
    mu["dates"][-1] = today
    su = sn.freeze(mu, today=today, now_hm="11:00")
    rec("盘中（11:00）当天那根被丢弃", su["dropped"]["unsettled"] == 1,
        str(su["dropped"]))
    s5 = sn.freeze(mu, today=today, now_hm="16:00")
    rec("盘后（16:00）当天那根保留", s5["dropped"]["unsettled"] == 0)
    rec("未定稿与定稿的哈希必然不同",
        su["sha256"] != s5["sha256"])

    # 缺口检测：把日期整体拉伸成"每 10 天一个"，必然出现 >GAP_DAYS 的跨度
    mg = {k: (v.copy() if isinstance(v, np.ndarray) else list(v)) for k, v in m.items()}
    import datetime as _dt
    d0 = _dt.date.fromisoformat(m["dates"][0])
    mg["dates"] = [(d0 + _dt.timedelta(days=10 * i)).isoformat()
                   for i in range(len(m["dates"]))]
    sg = sn.freeze(mg, today="")
    rec("跨度超过阈值记为缺口", len(sg["gaps"]) > 0, f"{len(sg['gaps'])} 处")
    rec("缺口只记录、不判对错（仍然 ok）", sg["ok"] is True,
        f"ok={sg['ok']} problems={len(sg['problems'])}")

    # verify：一致 / 不一致都要如实回答
    rec("verify 一致时 ok=True",
        sn.verify(s1, m, today=m["dates"][-1])["ok"] is True)
    rec("verify 不一致时 ok=False",
        sn.verify(s1, m2, today=m["dates"][-1])["ok"] is False)
    rec("快照没有哈希时不当成通过",
        sn.verify({}, m, today=m["dates"][-1])["ok"] is False)

    # 汇总
    passed = sum(1 for _, ok, _ in _STEPS if ok)
    for name, ok, detail in _STEPS:
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(_STEPS)} PASS")
    return 0 if passed == len(_STEPS) else 1


if __name__ == "__main__":
    sys.exit(main())
