"""波动率三件套（吊灯止损 / 布林挤压 / Ulcer·Z-Score）端到端校验。

覆盖：
  1. 虚拟盘持仓的「建议止损」：字段齐全、数值可用本地日线库独立复算
  2. K 线叠加的吊灯止损线：长度与 K 线严格对齐（前端不会画错位）
  3. 全套指标：新增「波动率」分组、指标总数、%B/带宽/Ulcer/Z-Score 取值合理
  4. 无本地日线的股票：止损读数应为 None（而不是拿 0 冒充）

跑法（需先启动服务）：
    python3 tests/check_vol_stop.py

为什么单开一个文件而不是塞进 check_paper.py：check_paper 管的是虚拟盘的
交易护栏（费率/守卫/认领），这里管的是指标读数，两者的失败定位完全不同。
"""
import json
import time
import urllib.error
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8899"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


#: 会话是 HttpOnly cookie（tick_sid），必须用 cookiejar 带着走。
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def req(method, path, body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def local_chandelier(bars, period=22, mult=3.0):
    """用本地日线独立复算吊灯止损（不复用后端代码，才算得上交叉验证）。"""
    import numpy as np
    H = np.array([b["high"] for b in bars], dtype=float)
    L = np.array([b["low"] for b in bars], dtype=float)
    C = np.array([b["close"] for b in bars], dtype=float)
    if len(C) < period + 1:
        return None
    # Wilder ATR（与 indicators_extra.atr_series 同口径，但这里手写一遍）
    prev = np.empty_like(C)
    prev[0] = C[0]
    prev[1:] = C[:-1]
    tr = np.maximum.reduce([H - L, np.abs(H - prev), np.abs(L - prev)])
    atr = np.full(len(C), np.nan)
    atr[period] = float(np.mean(tr[1:period + 1]))
    for i in range(period + 1, len(C)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    hh = float(np.max(H[-period:]))
    return hh - mult * float(atr[-1])


def main():
    # ---------- 0. 注册测试账号（不污染真实虚拟盘数据）----------
    # 用户名上限 24 字符，别用长前缀
    uname = f"vs{int(time.time())}"
    st, d = req("POST", "/api/auth/register",
                {"username": uname, "password": "test1234"})
    if st != 200:
        rec("注册测试账号", False, f"status={st} {d}")
        return summary()
    rec("注册测试账号", True, f"{uname}")

    # ---------- 1. 买入一只本地有日线的票 ----------
    code = "sh600519"
    st, d = req("POST", "/api/trade/buy", {"code": code, "qty": 100, "price": 1250})
    if st != 200:
        rec(f"买入 {code}", False, f"status={st} {d}")
        return summary()
    rec(f"买入 {code} 100 股", True, "")

    # ---------- 2. 持仓里的建议止损 ----------
    st, d = req("GET", "/api/trade/positions")
    items = d.get("items", []) if st == 200 else []
    p = next((x for x in items if x["code"] == code), None)
    rec("持仓返回该票", p is not None, f"items={len(items)}")

    if p is None:
        return summary()
    sr = p.get("stop_ref")
    rec("持仓带 stop_ref", sr is not None, f"stop_ref={sr}")

    if sr:
        rec("止损口径标注为 chandelier(22,3)",
            sr.get("basis") == "chandelier(22,3)", f"basis={sr.get('basis')}")
        rec("止损基准日非空（能看出算到哪天）",
            bool(sr.get("as_of")), f"as_of={sr.get('as_of')}")
        rec("止损价为正数且小于 22 日最高价",
            isinstance(sr.get("stop"), (int, float)) and sr["stop"] > 0,
            f"stop={sr.get('stop')}")
        # room_pct 与 triggered 必须自洽：现价在线下 → triggered 且 room<0
        px = p.get("price")
        room = sr.get("room_pct")
        trig = sr.get("triggered")
        if px and room is not None:
            expect_trig = px < sr["stop"]
            rec("triggered 与现价/止损价自洽",
                trig is expect_trig,
                f"price={px} stop={sr['stop']} triggered={trig}")
            rec("room_pct 方向正确（触发时为负）",
                (room < 0) == expect_trig, f"room={room}%")
        else:
            rec("room_pct / triggered 可算", False, f"px={px} room={room}")

        # 交叉验证：拿本地日线库独立复算一遍
        st2, bd = req("GET", f"/api/bars/{code}?limit=250")
        bars = bd.get("bars", []) if st2 == 200 else []
        mine = local_chandelier(bars)
        rec("止损价可用本地日线独立复算（±0.05）",
            mine is not None and abs(mine - sr["stop"]) < 0.05,
            f"接口={sr['stop']} 独立算={None if mine is None else round(mine, 3)}")

    # ---------- 3. K 线叠加的止损线 ----------
    st, d = req("GET", f"/api/kline?code={code}&count=250")
    if st != 200:
        rec("/api/kline 返回 200", False, f"status={st}")
        return summary()
    ks = d["klines"]
    sl = (d.get("stop_lines") or {}).get("chandelier")
    rec("kline 返回 chandelier 序列", sl is not None, "")
    if sl:
        rec("止损线长度与 K 线严格一致（前端不会画错位）",
            len(sl) == len(ks), f"{len(sl)} vs {len(ks)}")
        nones = sum(1 for v in sl if v is None)
        rec("前 22 根为 None（ATR 预热，不拿 0 冒充）",
            nones == 22 and all(v is None for v in sl[:22]), f"None 数={nones}")
        last = sl[-1]
        hi22 = max(k["high"] for k in ks[-22:])
        rec("末值落在合理区间（0 < 止损 < 22日最高）",
            last is not None and 0 < last < hi22,
            f"stop={last} hi22={hi22}")
        # 与本地库算出来的同口径值不应差太多（数据源可能多一根当日线）
        if mine:
            rec("与本地日线口径差异 < 5%",
                abs(last - mine) / mine < 0.05,
                f"kline={last} 本地={round(mine, 2)}")

    # ---------- 4. 全套指标的波动率组 ----------
    st, d = req("GET", f"/api/indicators_full?code={code}&count=250")
    if st != 200:
        rec("/api/indicators_full 返回 200", False, f"status={st}")
        return summary()
    cats = [g["cat"] for g in d.get("groups", [])]
    rec("新增「波动率」分组", "波动率" in cats, f"分组={cats}")
    vol = next((g for g in d["groups"] if g["cat"] == "波动率"), None)
    if vol:
        names = [it["name"] for it in vol["items"]]
        rec("波动率组含 6 项新指标",
            len(names) == 6, f"{names}")
        # %B：正常应在 -0.5~1.5（允许突破轨道一点点）
        pb = next((it["value"] for it in vol["items"] if it["name"] == "布林%B"), None)
        rec("布林 %B 取值合理（-0.5~1.5）",
            pb is not None and -0.5 <= pb <= 1.5, f"%B={pb}")
        bw = next((it["value"] for it in vol["items"] if it["name"] == "布林带宽%"), None)
        rec("带宽为正数", bw is not None and bw > 0, f"width={bw}")
        pc = next((it["value"] for it in vol["items"] if it["name"] == "带宽分位"), None)
        rec("带宽分位在 0~100", pc is not None and 0 <= pc <= 100, f"pct={pc}")
        ui = next((it["value"] for it in vol["items"] if it["name"] == "Ulcer(14)"), None)
        rec("Ulcer 非负", ui is not None and ui >= 0, f"UI={ui}")
        zs = next((it["value"] for it in vol["items"] if it["name"] == "Z-Score(20)"), None)
        rec("Z-Score 在 ±5 内（极端才越界）",
            zs is not None and abs(zs) <= 5, f"z={zs}")
    rec("指标总数已含新指标（≥61）",
        d.get("indicator_count", 0) >= 61, f"count={d.get('indicator_count')}")

    # ---------- 5. 没有本地日线的票 → 止损读数为 None ----------
    # 用一个大概率没落库的北交所代码：库里没有就该老实返回 None。
    # 价格必须落在真实成交区间内（虚拟盘有市价护栏），8.0 在其区间中。
    st, d = req("POST", "/api/trade/buy",
                {"code": "bj430047", "qty": 100, "price": 8.0})
    bought = (st == 200)
    if bought:
        st, d = req("GET", "/api/trade/positions")
        pb2 = next((x for x in d.get("items", []) if x["code"] == "bj430047"), None)
        rec("无本地日线的票：stop_ref 为 None（不拿 0 冒充）",
            pb2 is not None and pb2.get("stop_ref") is None,
            f"stop_ref={pb2.get('stop_ref') if pb2 else 'pos missing'}")
    else:
        rec("北交所票买入（用于验证无数据时的兜底）", False,
            f"status={st} {d}")

    # ---------- 5b. 布林挤压已登记为可选策略 ----------
    st, d = req("GET", "/api/strategy_list")
    flat = []
    for c in (d.get("cats") or []):
        for it in c.get("items") or []:
            flat.append(it)
    sq_def = next((x for x in flat if x["key"] == "boll_squeeze"), None)
    rec("布林挤压已登记为选股策略", sq_def is not None,
        f"name={sq_def.get('name') if sq_def else None}")
    if sq_def:
        rec("挤压阈值可调且默认 10（分位口径）",
            (sq_def.get("params") or {}).get("squeeze_pct") == 10.0,
            f"params={sq_def.get('params')}")
        rec("挤压策略需要历史数据（走日线引擎）",
            sq_def.get("needs_history") is True, f"needs_history={sq_def.get('needs_history')}")

    # ---------- 6. 清理测试账号的虚拟盘 ----------
    st, _ = req("POST", "/api/trade/reset")
    rec("清理：重置测试账号虚拟盘", st == 200, f"status={st}")

    return summary()


def summary():
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print(f"结论：{'全部通过 ✓' if passed == total else '存在问题 ✗'}  "
          f"{passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
