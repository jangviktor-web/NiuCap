#!/usr/bin/env python3
"""
历史行情落库 —— 把日线数据从数据源同步到本地库（SQLite / TiDB）。

为什么需要它：
  当前"策略选股"只用当日快照字段，像"回踩年线""平台突破"这类
  需要历史数据的策略根本算不出来（现有实现是用当日涨跌幅近似的伪形态）。
  把日线沉淀到库里之后，才能做真正的历史形态计算，也避免每次请求都
  实时打数据源。

性能特征（实测，TiDB Cloud Serverless 后端）：

  数据源有两种，用 --source 选择：

  【eltdx（默认，推荐）】走通达信 7709 协议，支持一次请求批量取多只：
      · 全市场 5575 只 × 250 根日线：15.9 秒
      · 沪深300（300 只）         ：约 2.6 秒
      · 额外好处：提供真实成交额 amount（腾讯源此字段恒为 None，
        导致库里 daily_bars.amount 一直是空的）
      · 注意：许可证限个人学习/非商业用途，详见 server/sources/eltdx_source.py

  【tencent（原路径）】逐只调腾讯 get_kline：
      · 约 0.36s/只，几乎全是 datasource._throttle 的 400ms 硬性间隔
      · 该间隔是【速率上限】而非锁竞争，加线程突破不了
      · 全市场（3761 只）约 48 分钟

  TiDB 写入（两种源共用）：
      · upsert_bars(500 条) 约 1.4s
      · TiDB Serverless 单次往返约 190~260ms，成本由【语句条数】决定：
        逐行 367ms/条，批量 100/批仅 2.8ms/条
      · 全市场落库时写入会成为新瓶颈，这是预期内的

用法：
  python3 scripts/sync_bars.py --scope hs300            # 沪深300（默认走 eltdx）
  python3 scripts/sync_bars.py --scope all              # 全市场
  python3 scripts/sync_bars.py --scope all --source tencent   # 强制走腾讯源
  python3 scripts/sync_bars.py --codes sh600519,sz000001
  python3 scripts/sync_bars.py --scope hs300 --count 800
  python3 scripts/sync_bars.py --scope hs300 --workers 4      # 仅 tencent 源生效
  python3 scripts/sync_bars.py --status                 # 只看落库覆盖情况
  python3 scripts/sync_bars.py --scope hs300 --dry-run  # 只统计不发请求
  python3 scripts/sync_bars.py --check-source           # 数据源自检

建议：每个交易日收盘后（17:30 之后）跑一次 --scope hs300。
"""

import argparse
import datetime as _dt
import json
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "server"))

import store as st            # noqa: E402
import datasource as ds       # noqa: E402
from sources import eltdx_enabled, eltdx_source  # noqa: E402


# 股票池 scope 到 datasource.pool_rows() 里 pools 字段的映射
# pools 的取值是纯数字指数代码，如 '000300' 表示沪深300
SCOPE_POOLS = {
    "hs300":  ["000300"],      # 沪深300
    "zz500":  ["000905"],      # 中证500
    "zz1000": ["000852"],      # 中证1000
    "zz2000": ["932000"],      # 中证2000
}


# ================================================================
# 同步守门（蒸馏自 qs 的「智能更新三重判断」）
#
# 防两类事故：
#   ① 盘中全量落库 → 当日 K 线是半根，污染下游（回测/体检/选股全吃假数据）
#   ② 同一天反复全量 → 白白打数据源几千次（upsert 幂等，只是纯浪费）
#
# 三重判断按成本从低到高排，先便宜后贵，任一命中即短路：
#   判断1 时间守门（纯时钟，0 成本）
#   判断2 当日标记（读一个 json 文件）
#   判断3 覆盖率抽检（一次 SQL，抽 100 只查 max(date)）
# ================================================================

#: 当日已跑标记。放 data/（与 SQLite 同目录），备份/迁移顺手带上。
CACHE_PATH = os.path.join(_ROOT, "data", "update_cache.json")


def _read_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_cache(payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"⚠ 守门标记写入失败（不影响同步结果）：{e}")


def _expected_last_date(now=None) -> str:
    """此刻能合理期待的最新日线日期（粗略，不含节假日表）。

    交易日且已过 15:00 → 期待今天；否则回退到最近的工作日。
    节假日会误期待（比如国庆后第一天已过 15:00）→ 抽检不达标 →
    放行重拉一次。方向是「宁可多同步不可漏同步」，可接受。
    """
    d = (now or _dt.datetime.now()).date()
    if (now or _dt.datetime.now()).time() < _dt.time(15, 0):
        d -= _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return d.isoformat()


def _is_full_sync(scope: str, n_codes: int) -> bool:
    """是否够格称「全量」——时间守门只拦全量，小池子（调试用）不拦。"""
    return scope in ("all", "snapshot") or n_codes > 1000


def gate_full_sync(scope: str, codes: list, force: bool = False,
                   sample: int = 100, min_cover: float = 0.9,
                   now=None) -> tuple:
    """全量同步前的三重守门。返回 (放行的 codes, 报告 dict)。

    now 参数供测试注入时间（默认真实当前时刻）。
    返回的 codes 原样透传（守门只决定「跑不跑」，不改名单——
    增量预筛在 sync_via_eltdx 里做，那里才知道 count 语义）。
    """
    g = {"action": "run", "reason": "", "gate1": None, "gate2": None, "gate3": None}
    now = now or _dt.datetime.now()

    # ---- 判断1：时间守门。盘中跑全量 = 往库里写半根日线 ----
    if _is_full_sync(scope, len(codes)) and now.weekday() < 5 and now.time() < _dt.time(15, 0):
        g.update(gate1={"hit": True, "now": now.strftime("%H:%M")})
        if not force:
            g.update(action="skip",
                     reason=f"现在是 {now.strftime('%H:%M')}（盘中），当日 K 线未结算，"
                            f"全量落库会写入半根日线污染回测/体检。收盘后（建议 17:00 后）再跑；"
                            f"确要盘中跑请加 --force")
            return codes, g
        g["reason"] = "判断1 命中（盘中）但 --force 强制放行"

    # ---- 判断2：当日标记。同 scope 今天已成功过 → 别再打几千次请求 ----
    cache = _read_cache()
    today = now.date().isoformat()
    if cache.get("date") == today and cache.get("scope") == scope and cache.get("ok"):
        g.update(gate2={"hit": True, "cache": cache})
        if not force:
            g.update(action="skip",
                     reason=f"今天（{today}）已成功同步过 scope={scope}"
                            f"（成功 {cache.get('ok')} 只，{_dt.datetime.fromtimestamp(cache.get('ts', 0)).strftime('%H:%M')}），"
                            f"无需重复。确要重跑请加 --force")
            return codes, g
        g["reason"] = (g.get("reason") + "；" if g.get("reason") else "") + \
                      "判断2 命中（当日已跑）但 --force 强制放行"

    # ---- 判断3：覆盖率抽检。库里已有期待的最新日线 → 数据齐了 ----
    ref = _expected_last_date(now)
    samp = random.sample(codes, min(sample, len(codes))) if codes else []
    prog = st.bars_progress(samp)
    fresh = sum(1 for p in prog.values() if p["last"] and p["last"] >= ref)
    cover = fresh / len(samp) if samp else 0.0
    g.update(gate3={"ref": ref, "sample": len(samp), "fresh": fresh,
                    "cover": round(cover, 3)})
    if samp and cover >= min_cover and not force:
        g.update(action="skip",
                 reason=f"抽检 {len(samp)} 只中 {fresh} 只已同步到 {ref}（覆盖率 {cover:.0%} ≥ "
                        f"{min_cover:.0%}），库里已是最新，跳过。确要重拉请加 --force")
        return codes, g
    if force and g["gate3"]:
        g["reason"] = (g.get("reason") + "；" if g.get("reason") else "") + \
                      "判断3 抽检完成但 --force 强制放行"

    if not g["reason"]:
        g["reason"] = "三重判断均未命中，正常放行"
    return codes, g



def pick_codes(scope: str, codes_arg: str) -> list:
    """确定要同步的股票代码列表。

    all 与 snapshot 的区别很重要：

      · all（推荐）：用 eltdx 的【交易所完整代码表】，实测 5575 只。
        含北交所（bj 前缀，349 只）和新上市次新股，是真正的「全市场」。
      · snapshot   ：沿用旧口径，取 datasource.pool_rows()（新浪分页快照），
        实测 3761 只。它是 eltdx 代码表的【严格子集】——实测交集 3761、
        「仅池中有」0 只，说明池子只是不全，并没有额外股票。

    之所以拆开，是因为新浪分页有抓取上限、且完全不含北交所。若目标是
    把 daily_bars 覆盖率拉满，必须走 all。
    """
    if codes_arg:
        return [ds.normalize(c.strip()) for c in codes_arg.split(",") if c.strip()]

    if scope == "all":
        if not eltdx_enabled():
            print("⚠ 未启用 eltdx，无法获取完整代码表，回退到快照口径（3761 只）")
            return [r["code"] for r in ds.pool_rows()]
        try:
            codes = eltdx_source.all_a_shares()
            print(f"  代码表来源：eltdx 全量（{len(codes)} 只，含北交所）")
            return codes
        except Exception as e:
            print(f"⚠ 取 eltdx 代码表失败（{e}），回退到快照口径")
            return [r["code"] for r in ds.pool_rows()]

    rows = ds.pool_rows()
    if scope == "snapshot":
        return [r["code"] for r in rows]

    # 支持直接传指数代码，或走别名映射
    want = SCOPE_POOLS.get(scope.lower(), [scope])
    want = [str(w).upper() for w in want]

    out = []
    for r in rows:
        pools = r.get("pools") or []
        if not isinstance(pools, (list, tuple, set)):
            pools = [pools]
        if any(str(p).upper() in want for p in pools):
            out.append(r["code"])
    return out


def sync_one(code: str, count: int, prog: dict = None, ref: str = "") -> dict:
    """同步单只股票（腾讯源路径）：拉取 → 落库 → 记进度。返回结果摘要。

    prog/ref 非空时走增量：查预筛结果决定拉多少根（与 eltdx 路径的
    阶段0 同一套规则）。腾讯源 400ms/只 的限流是硬成本，增量在这里
    省的是「整只跳过」，比省根数更值钱。
    """
    t0 = time.time()
    fetch_n = count
    if prog and ref:
        p = prog.get(code)
        if p and p["last"] and p["last"] >= ref and p["n"] >= count:
            st.record_sync(code, p["last"], p["n"], status="ok")
            return {"code": code, "ok": True, "bars": 0, "skip": True,
                    "last": p["last"], "sec": 0.0}
        if p and p["last"] and p["n"] >= count:
            try:
                gap = (_dt.date.fromisoformat(ref)
                       - _dt.date.fromisoformat(p["last"])).days + 3
                fetch_n = min(count, max(gap, 5))
            except ValueError:
                pass
    try:
        bars = ds.get_kline(code, "1d", fetch_n, use_cache=False)
        if not bars:
            st.record_sync(code, "", 0, status="empty", err="数据源返回空")
            return {"code": code, "ok": False, "bars": 0, "msg": "数据源返回空"}

        # 只保留落库需要的字段
        payload = [{
            "date": b.get("date"), "open": b.get("open"), "close": b.get("close"),
            "high": b.get("high"), "low": b.get("low"),
            "volume": b.get("volume"), "amount": b.get("amount"),
        } for b in bars if b.get("date")]

        n = st.upsert_bars(code, payload)
        last = payload[-1]["date"]
        st.record_sync(code, last, n, status="ok")
        return {"code": code, "ok": True, "bars": n,
                "last": last, "sec": time.time() - t0}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        try:
            st.record_sync(code, "", 0, status="error", err=msg)
        except Exception:
            pass
        return {"code": code, "ok": False, "bars": 0, "msg": msg,
                "sec": time.time() - t0}


def store_one(code: str, payload: list) -> dict:
    """把已取到的日线落库并记进度（eltdx 源路径，取数与落库分离）。"""
    t0 = time.time()
    try:
        if not payload:
            st.record_sync(code, "", 0, status="empty", err="数据源返回空")
            return {"code": code, "ok": False, "bars": 0, "msg": "数据源返回空"}
        n = st.upsert_bars(code, payload)
        last = payload[-1]["date"]
        st.record_sync(code, last, n, status="ok")
        return {"code": code, "ok": True, "bars": n,
                "last": last, "sec": time.time() - t0}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        try:
            st.record_sync(code, "", 0, status="error", err=msg)
        except Exception:
            pass
        return {"code": code, "ok": False, "bars": 0, "msg": msg,
                "sec": time.time() - t0}


def sync_via_eltdx(codes: list, count: int, workers: int, limit: int = 0,
                   incremental: bool = True, adjust: str = "qfq") -> tuple:
    """走 eltdx 批量取数，再逐只落库。

    分两阶段，因为两者的瓶颈完全不同：
      · 取数是「网络批量」——一次请求多只，全市场 15.9 秒，几乎不吃并发
      · 落库是「数据库往返」——每只若干条多值 INSERT，受 TiDB RTT 限制

    incremental=True（默认）时先查库算缺口（阶段0）：
      · 已到参考日且根数足够的整只跳过（全市场重复同步的大头）
      · 历史根数不足的按 count 整段拉（补历史）
      · 只落后几天的按「缺口天数+3 缓冲」拉（upsert 幂等，多拉无害）
    参考日取全库众数而非 max——个别新股/停牌股不会把全体拉成"落后 N 天"。

    返回 (ok, fail, total_bars, failed_list)。
    """
    ok = fail = total_bars = 0
    failed = []

    # ---- 阶段 0：增量预筛（查库算缺口）----
    if limit:
        # limit 是调试语义：所见即所得，关闭预筛
        fetch_plan = [(count, codes[:limit])]
    elif incremental:
        prog = st.bars_progress(codes)
        lasts = [p["last"] for p in prog.values() if p["last"]]
        mode = Counter(lasts).most_common(1)[0][0] if lasts else ""
        # 参考日 = max(库内众数, 守门期待日)。只看众数会自相矛盾：
        # 守门判断3 发现"缺周五的线"放行同步，预筛却因"全库已到周四众数"
        # 整体跳过——缺口永远补不上（真实踩到：2026-09-26 周六补周五的线）。
        # 众数管"大多数到哪了"（防个别新股/停牌拉偏），期待日管"至少该到哪"。
        exp = _expected_last_date()
        ref = max(mode, exp) if (mode and exp) else (mode or exp)
        groups = {}
        skipped = 0
        for c in codes:
            p = prog.get(c)
            if ref and p and p["last"] and p["last"] >= ref and p["n"] >= count:
                skipped += 1
                continue
            if not p or not p["last"] or p["n"] < count:
                fn = count
            else:
                try:
                    gap = (_dt.date.fromisoformat(ref)
                           - _dt.date.fromisoformat(p["last"])).days + 3
                except ValueError:
                    gap = count
                fn = min(count, max(gap, 5))
            groups.setdefault(fn, []).append(c)
        fetch_plan = sorted(groups.items(), key=lambda kv: -kv[0])
        skipped_note = (f"跳过 {skipped} 只（已到参考日 {ref}），"
                        if skipped else "")
        print(f"【阶段0】增量预筛：{skipped_note}"
              f"其余 {len(codes) - skipped} 只分 {len(fetch_plan)} 档拉取")
    else:
        fetch_plan = [(count, codes)]

    # ---- 阶段 1：批量取数 ----
    print("【阶段1/2】批量拉取日线……")
    t0 = time.time()
    data = {}
    try:
        for fn, cs in fetch_plan:
            if cs:
                data.update(eltdx_source.get_bars_batch(cs, count=fn, adjust=adjust))
    except eltdx_source.EltdxUnavailable as e:
        print(f"  ✗ eltdx 不可用：{e}")
        raise
    fetch_sec = time.time() - t0
    got = len(data)
    requested = [c for _, cs in fetch_plan for c in cs]
    print(f"  取到 {got}/{len(requested)} 只，耗时 {fetch_sec:.1f}s"
          f"（{fetch_sec/max(len(requested),1)*1000:.1f} ms/只）")
    dr = eltdx_source.last_dropped()
    if dr:
        print(f"  已剔除 {dr} 条停牌占位行（OHLC 全等且成交量为 0，会污染均量计算）")

    missing = [c for c in requested if c not in data]
    if missing:
        print(f"  ⚠ {len(missing)} 只无数据（停牌/退市/代码异常），示例：{missing[:5]}")
        for c in missing:
            try:
                st.record_sync(c, "", 0, status="empty", err="eltdx 未返回数据")
            except Exception:
                pass
            fail += 1
            failed.append({"code": c, "msg": "eltdx 未返回数据"})

    if not data:
        return ok, fail, total_bars, failed

    # ---- 阶段 2：逐只落库 ----
    print(f"\n【阶段2/2】写入数据库（{workers} 线程）……")
    t1 = time.time()
    items = list(data.items())
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(store_one, c, rows): c for c, rows in items}
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if r["ok"]:
                ok += 1
                total_bars += r["bars"]
            else:
                fail += 1
                failed.append(r)
            if done % 50 == 0 or done == len(items):
                el = time.time() - t1
                eta = el / done * (len(items) - done)
                print(f"  [{done:>4d}/{len(items)}] 成功 {ok:>4d} 失败 {fail:>3d} "
                      f"| 已用 {el/60:.1f}分 预计剩余 {eta/60:.1f}分")
    store_sec = time.time() - t1
    print(f"  写入完成，耗时 {store_sec:.1f}s（{store_sec/max(len(items),1)*1000:.1f} ms/只）")
    return ok, fail, total_bars, failed


def show_source_check() -> int:
    """数据源自检：确认 eltdx 能连上、能取数、单位正确。"""
    print("═" * 64)
    print("数据源自检")
    print("═" * 64)
    if not eltdx_enabled():
        print("  eltdx 不可用 → 未安装，或已被总开关 TICK_ELTDX=0 关闭。")
        print("  请 pip install eltdx，或改用 --source tencent")
        return 1
    r = eltdx_source.selfcheck(verbose=True)
    print("─" * 64)
    print("结论：" + ("全部通过 ✓" if r["ok"] else "存在问题 ✗"))
    if r.get("sample"):
        s = r["sample"][0]
        print(f"  样本：{s['date']} close={s['close']} volume={s['volume']}手 "
              f"amount={s['amount']:,.0f}元" if s.get("amount") else "")
    print()
    return 0 if r["ok"] else 1



def show_status() -> None:
    """打印当前落库覆盖情况"""
    print("═" * 64)
    print("历史行情落库状态")
    print("═" * 64)
    print(f"  数据库后端：{st.BACKEND}"
          + (f"（{st.MYSQL_HOST}:{st.MYSQL_PORT}/{st.MYSQL_DB}）" if st.IS_MYSQL
             else f"（{st.DB_PATH}）"))
    cov = st.bars_coverage()
    print(f"  已落库股票：{cov['codes']} 只")
    print(f"  总行数    ：{cov['rows']:,} 条")
    if cov["start"]:
        print(f"  日期范围  ：{cov['start']} ~ {cov['end']}")
    else:
        print("  日期范围  ：（空，尚未同步）")

    # 最近同步的若干只
    recent = st.sync_status(5)
    if recent:
        print("\n  最近同步：")
        for r in recent:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["synced_at"]))
            flag = "OK " if r["status"] == "ok" else r["status"][:4].upper()
            print(f"    [{flag}] {r['code']:<12s} {r['bars']:>4d}条 "
                  f"至 {r['last_date'] or '-':<12s} {ts}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="历史行情落库")
    ap.add_argument("--scope", default="hs300",
                    help="股票池：all(全市场，eltdx完整代码表~5575只) / hs300 / zz500 / "
                         "zz1000 / zz2000 / snapshot(旧快照口径~3761只)，"
                         "也可直接传指数代码如 000300（默认 hs300）")
    ap.add_argument("--codes", default="", help="直接指定代码，逗号分隔")
    ap.add_argument("--count", type=int, default=500, help="每只拉多少根日线（默认 500 ≈ 2年）")
    ap.add_argument("--workers", type=int, default=4,
                    help="并发数（默认4）。eltdx 源下仅作用于【落库】阶段；"
                         "腾讯源下同时作用于取数（受 400ms 限流，超过4无提升）")
    ap.add_argument("--source", default="auto", choices=["auto", "eltdx", "tencent"],
                    help="数据源：auto(默认，优先eltdx失败降级) / eltdx / tencent")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    ap.add_argument("--force", action="store_true",
                    help="跳过同步守门三重判断（盘中跑全量/当日重跑/已最新重拉）")
    ap.add_argument("--no-incremental", action="store_true",
                    help="关闭增量预筛，整段重拉（默认按缺口拉）")
    ap.add_argument("--dry-run", action="store_true", help="只统计股票池，不发请求")
    ap.add_argument("--status", action="store_true", help="只看落库状态")
    ap.add_argument("--adjust", default="qfq",
                    choices=["qfq", "hfq", "none"],
                    help="复权口径：qfq(默认，仅取最新段) / hfq(补多年历史推荐) / none(不复权)")
    ap.add_argument("--check-source", action="store_true", help="数据源自检")
    args = ap.parse_args()

    if args.status:
        show_status()
        return 0

    if args.check_source:
        return show_source_check()

    print(f"数据库后端：{st.BACKEND}")
    t_start = time.time()

    codes = pick_codes(args.scope, args.codes)
    if args.limit:
        codes = codes[:args.limit]
    if not codes:
        print(f"✗ 股票池为空（scope={args.scope}），请检查参数")
        return 1

    # ---- 同步守门三重判断（--force 可全部绕过）----
    codes, gate_r = gate_full_sync(args.scope, codes, force=args.force)
    print(f"守门：{gate_r['reason']}")
    for i, k in ((1, "gate1"), (2, "gate2"), (3, "gate3")):
        if gate_r[k]:
            print(f"  判断{i}: {gate_r[k]}")
    if gate_r["action"] == "skip":
        return 0

    # ---- 数据源决策 ----
    # 注意用 eltdx_enabled() 而非 eltdx_source.available()：前者还受总开关
    # TICK_ELTDX=0 控制，便于一键整体退回腾讯源。
    use_eltdx = False
    if args.source == "eltdx":
        if not eltdx_enabled():
            print("✗ 指定了 --source eltdx 但不可用"
                  "（未安装，或总开关 TICK_ELTDX=0 已关闭）。"
                  "请 pip install eltdx，或改用 --source tencent")
            return 1
        use_eltdx = True
    elif args.source == "auto":
        use_eltdx = eltdx_enabled()
    src_name = "eltdx" if use_eltdx else "tencent"

    print(f"股票池【{args.scope}】：{len(codes)} 只，每只 {args.count} 根日线")
    print(f"数据源：{src_name}" + (f"（v{eltdx_source.version()}）" if use_eltdx else "")
          + f"  复权：{args.adjust}")

    if args.dry_run:
        # eltdx 走批量，体感主要是落库的数据库往返；腾讯源受 400ms 限流主导。
        per = 0.02 if use_eltdx else 0.76
        est = len(codes) * per / 60
        print(f"[dry-run] 预计耗时约 {max(est, 0.05):.1f} 分钟"
              f"（按 {src_name} 实测 {per}s/只 估算），不发请求")
        print(f"[dry-run] 示例代码：{codes[:10]}")
        return 0

    # ---- 执行同步 ----
    failed = []
    ok = fail = total_bars = 0
    if use_eltdx:
        try:
            ok, fail, total_bars, failed = sync_via_eltdx(
                codes, args.count, args.workers,
                incremental=not args.no_incremental, adjust=args.adjust)
        except eltdx_source.EltdxUnavailable as e:
            if args.source == "eltdx":
                print(f"✗ eltdx 失败：{e}")
                return 1
            print(f"⚠ eltdx 不可用（{e}），降级到腾讯源……\n")
            use_eltdx = False
            src_name = "tencent"

    if not use_eltdx:
        # 腾讯路径：先批量查一次进度（增量预筛），逐只循环里不再碰库。
        # 参考日同 eltdx 路径：max(众数, 期待日)，防止与守门判断3 打架。
        prog, ref = {}, ""
        if not args.no_incremental:
            prog = st.bars_progress(codes)
            lasts = [p["last"] for p in prog.values() if p["last"]]
            mode = Counter(lasts).most_common(1)[0][0] if lasts else ""
            exp = _expected_last_date()
            ref = max(mode, exp) if (mode and exp) else (mode or exp)
            if ref:
                print(f"增量预筛：参考日 {ref}（众数 {mode or '-'} / 期待 {exp or '-'}），"
                      f"已到参考日的整只跳过")
        print(f"并发 {args.workers} 线程，开始同步……\n")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(sync_one, c, args.count, prog, ref): c for c in codes}
            done = 0
            for fut in as_completed(futs):
                r = fut.result()
                done += 1
                if r["ok"]:
                    ok += 1
                    total_bars += r["bars"]
                else:
                    fail += 1
                    failed.append(r)
                if done % 10 == 0 or done == len(codes):
                    el = time.time() - t_start
                    eta = el / done * (len(codes) - done)
                    print(f"  [{done:>4d}/{len(codes)}] 成功 {ok:>4d} 失败 {fail:>3d} "
                          f"| 已用 {el/60:.1f}分 预计剩余 {eta/60:.1f}分")

    el = time.time() - t_start
    print(f"\n{'═'*64}")
    print(f"同步完成（{src_name}）：成功 {ok} 只，失败 {fail} 只，共写入 {total_bars:,} 条")
    print(f"耗时 {el/60:.1f} 分钟")
    # 成功后写当日守门标记（判断2 依据）。失败【或一只都没拉到】不写——
    # ok=0 意味着缺口没补上（或被预筛全跳过），写标记会让当天真正需要的
    # 同步被判断2 拦死（真实踩到：周六补周五的线，预筛全跳后 ok=0 也写了标记）。
    if fail == 0 and ok > 0:
        _write_cache({"date": _dt.date.today().isoformat(), "scope": args.scope,
                      "ok": ok, "fail": fail, "bars": total_bars,
                      "ts": time.time()})
        print(f"守门标记已写入 {CACHE_PATH}")
    if failed:
        print(f"\n失败明细（前10）：")
        for r in failed[:10]:
            print(f"  ✗ {r['code']}: {r.get('msg','')}")
    print(f"{'═'*64}")
    show_status()
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
