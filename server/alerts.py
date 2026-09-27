"""#96 监控中心：规则管理 + 触发检测 + 告警落盘 + 冷却去重。

来源：蒸馏报告 8.4（shy3130/tick-stock-panel 的 alert_store.py，MIT）。
四类规则：价格涨跌 / 个股信号 / 全市场异动 / 策略扫描命中。

关键约束（逐条对应排查文档 R1~R7）：
  - 条件**结构化求值**，字段走白名单、算子白名单，绝不 eval（R7）。
  - 检测线程用**独立 DB 连接**（store._conn 是 thread-local），批量行情、
    单事务落盘，不长时间持锁（R1）。
  - 冷却 + 同规则同标的同日去重，双保险防刷屏（R2）。
  - 推送走 webhook.push，失败静默降级，绝不阻断落盘（R3）。
  - 保留策略照搬对方：7 天 / 5000 条，每 20 次写入滚动清理一次。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import store
import webhook

logger = logging.getLogger(__name__)

MAX_DAYS = 7
MAX_RECORDS = 5000
PRUNE_EVERY = 20
DEFAULT_COOLDOWN_MIN = 60

_lock = threading.Lock()
_write_count = 0
_loop_running = False

# ───────────────────────── 字段白名单（防 eval） ─────────────────────────
PRICE_FIELDS = ("price", "change_pct", "change", "open", "high", "low",
                "prev_close", "amount", "turnover", "volume")
SIGNAL_FIELDS = ("close", "ma5", "ma10", "ma20", "ma60", "ma_align",
                 "chg20", "chg60", "hhv20", "hhv60", "llv20",
                 "vol_ma5", "vol_ma20", "vol_ratio", "bias_ma20",
                 "range20_pct", "range60_pct", "amount_ma20")
MARKET_FIELDS = ("limit_up_cnt", "anomaly_cnt")

OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

KIND_LABELS = {"price": "价格", "signal": "信号", "market": "市场", "strategy": "策略"}
SEVERITY_LABELS = {"info": "提示", "warn": "警告", "critical": "严重"}


def _now() -> float:
    return time.time()


def _rule_id() -> str:
    return f"r{int(_now() * 1000)}"


# ───────────────────────── 条件求值 ─────────────────────────
def eval_conds(conds: List[dict], logic: str, ctx: Dict[str, Any],
               allowed: Optional[tuple] = None) -> tuple[bool, str]:
    """结构化条件求值。返回 (是否命中, 命中描述)。

    字段/算子双重白名单：不在 allowed 内的字段（如 `__import__`）直接判不命中，
    绝不 eval —— 规则是用户可编辑的输入，这里就是信任边界。
    """
    if not conds:
        return False, ""
    results = []
    desc = []
    for c in conds:
        field = str(c.get("field") or "")
        op = str(c.get("op") or ">=")
        want = c.get("value")
        if allowed is not None and field not in allowed:
            results.append(False)
            desc.append(f"{field}?")
            continue
        if field not in ctx or ctx[field] is None or op not in OPS:
            results.append(False)
            desc.append(f"{field}?")
            continue
        cur = ctx[field]
        if isinstance(cur, str) or isinstance(want, str):
            ok = OPS[op](str(cur), str(want))
        else:
            try:
                ok = OPS[op](float(cur), float(want))
            except (TypeError, ValueError):
                ok = False
        results.append(ok)
        desc.append(f"{field}={cur}{op}{want}" if ok else f"{field}={cur}")
    hit = all(results) if str(logic).upper() == "AND" else any(results)
    return hit, ("、".join(desc) if hit else "")


# ───────────────────────── 取值 ─────────────────────────
def _fetch_prices(codes: List[str]) -> Dict[str, dict]:
    import datasource as ds
    try:
        return ds.quote_tencent(codes) or {}
    except Exception as e:
        logger.warning("alerts quote failed: %s", e)
        return {}


def _fetch_signal(code: str, engine) -> Optional[dict]:
    if engine is None:
        return None
    try:
        return engine.metrics(code)
    except Exception as e:
        logger.warning("alerts metrics(%s) failed: %s", code, e)
        return None


def _fetch_market(htk) -> dict:
    out = {"limit_up_cnt": 0, "anomaly_cnt": 0}
    if htk is None:
        return out
    try:
        out["limit_up_cnt"] = len((htk.limit_up_pool() or {}).get("items") or [])
    except Exception as e:
        logger.warning("alerts limit_up_pool failed: %s", e)
    try:
        out["anomaly_cnt"] = len(htk.anomaly_list(30) or [])
    except Exception as e:
        logger.warning("alerts anomaly_list failed: %s", e)
    return out


# ───────────────────────── 规则 CRUD ─────────────────────────
def _row2rule(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "kind": r["kind"], "code": r["code"],
        "conds": json.loads(r["conds"] or "[]"),
        "logic": r["logic"], "severity": r["severity"],
        "cooldown_min": r["cooldown_min"], "push": r["push"],
        "enabled": r["enabled"], "last_fired": r["last_fired"],
        "fired_count": r["fired_count"], "created_at": r["created_at"],
        "params": json.loads(r["params"] or "{}"),
    }


def list_rules() -> List[dict]:
    c = store._conn()
    rows = c.execute("SELECT * FROM alert_rules ORDER BY created_at DESC").fetchall()
    return [_row2rule(r) for r in rows]


def get_rule(rule_id: str) -> Optional[dict]:
    c = store._conn()
    r = c.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone()
    return _row2rule(r) if r else None


def add_rule(payload: dict) -> dict:
    rid = _rule_id()
    now = _now()
    c = store._conn()
    c.execute(
        "INSERT INTO alert_rules(id,name,kind,code,conds,logic,severity,"
        "cooldown_min,push,enabled,last_fired,fired_count,created_at,params) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, str(payload.get("name") or "未命名规则"),
         str(payload.get("kind") or "price"),
         str(payload.get("code") or "").strip(),
         json.dumps(payload.get("conds") or [], ensure_ascii=False),
         str(payload.get("logic") or "AND").upper(),
         str(payload.get("severity") or "warn"),
         int(payload.get("cooldown_min") or DEFAULT_COOLDOWN_MIN),
         int(1 if payload.get("push", 1) else 0),
         int(1 if payload.get("enabled", 1) else 0),
         0.0, 0, now,
         json.dumps(payload.get("params") or {}, ensure_ascii=False)))
    store._commit(c)
    return get_rule(rid) or {}


def update_rule(rule_id: str, payload: dict) -> Optional[dict]:
    r = get_rule(rule_id)
    if not r:
        return None
    c = store._conn()
    c.execute(
        "UPDATE alert_rules SET name=?,kind=?,code=?,conds=?,logic=?,severity=?,"
        "cooldown_min=?,push=?,enabled=?,params=? WHERE id=?",
        (str(payload.get("name") or r["name"]),
         str(payload.get("kind") or r["kind"]),
         str(payload.get("code") or r["code"]),
         json.dumps(payload.get("conds") if "conds" in payload else r["conds"],
                    ensure_ascii=False),
         str(payload.get("logic") or r["logic"]).upper(),
         str(payload.get("severity") or r["severity"]),
         int(payload.get("cooldown_min") or r["cooldown_min"]),
         int(1 if payload.get("push", r["push"]) else 0),
         int(1 if payload.get("enabled", r["enabled"]) else 0),
         json.dumps(payload.get("params") if "params" in payload else r["params"],
                    ensure_ascii=False),
         rule_id))
    store._commit(c)
    return get_rule(rule_id)


def delete_rule(rule_id: str) -> bool:
    c = store._conn()
    c.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))
    store._commit(c)
    return True


# ───────────────────────── 告警 ─────────────────────────
def _row2alert(r) -> dict:
    return {"ts": r["ts"], "rule_id": r["rule_id"], "rule_name": r["rule_name"],
            "kind": r["kind"], "code": r["code"], "severity": r["severity"],
            "msg": r["msg"], "value": r["value"], "is_read": r["is_read"]}


def list_alerts(limit: int = 50, unread_only: bool = False) -> List[dict]:
    c = store._conn()
    sql = "SELECT * FROM alerts"
    if unread_only:
        sql += " WHERE is_read=0"
    sql += " ORDER BY ts DESC LIMIT ?"
    rows = c.execute(sql, (int(limit),)).fetchall()
    return [_row2alert(r) for r in rows]


def unread_count() -> int:
    c = store._conn()
    r = c.execute("SELECT COUNT(*) AS n FROM alerts WHERE is_read=0").fetchone()
    return int(r["n"] or 0)


def _prune_if_needed(c):
    """照搬对方保留策略：7 天 / 5000 条，每 20 次写入清理一次。"""
    global _write_count
    _write_count += 1
    if _write_count % PRUNE_EVERY:
        return
    cutoff = _now() - MAX_DAYS * 86400
    try:
        c.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
        c.execute(
            "DELETE FROM alerts WHERE ts NOT IN "
            "(SELECT ts FROM alerts ORDER BY ts DESC LIMIT ?)", (MAX_RECORDS,))
        store._commit(c)
    except Exception as e:
        logger.warning("alerts prune failed: %s", e)


def _dedup(c, rule_id: str, code: str) -> bool:
    """同规则同标的当日已告警 → True（跳过）。"""
    today = time.strftime("%Y-%m-%d")
    lo = time.mktime(time.strptime(today, "%Y-%m-%d"))
    r = c.execute(
        "SELECT 1 FROM alerts WHERE rule_id=? AND code=? AND ts>=? LIMIT 1",
        (rule_id, code, lo)).fetchone()
    return r is not None


def _emit(c, rule: dict, code: str, msg: str, value: Optional[float],
          cfg: dict) -> bool:
    if _dedup(c, rule["id"], code):
        return False
    ts = round(_now(), 3)
    c.execute(
        "INSERT OR REPLACE INTO alerts(ts,rule_id,rule_name,kind,code,severity,"
        "msg,value,is_read) VALUES(?,?,?,?,?,?,?,?,0)",
        (ts, rule["id"], rule["name"], rule["kind"], code,
         rule["severity"], msg, value))
    c.execute("UPDATE alert_rules SET last_fired=?, fired_count=fired_count+1 "
              "WHERE id=?", (ts, rule["id"]))
    store._commit(c)
    _prune_if_needed(c)
    if rule.get("push"):
        try:
            webhook.push(cfg, f"[{SEVERITY_LABELS.get(rule['severity'], '警告')}] "
                              f"{rule['name']}", msg,
                         {"code": code, "kind": rule["kind"], "value": value})
        except Exception as e:
            logger.warning("alerts webhook push failed: %s", e)   # 静默降级
    return True


def mark_read(ts: Optional[float] = None, all_: bool = False) -> int:
    c = store._conn()
    if all_:
        c.execute("UPDATE alerts SET is_read=1 WHERE is_read=0")
    elif ts is not None:
        c.execute("UPDATE alerts SET is_read=1 WHERE ts=?", (float(ts),))
    else:
        return 0
    store._commit(c)
    return 1


def clear_alerts() -> int:
    c = store._conn()
    c.execute("DELETE FROM alerts")
    store._commit(c)
    return 1


# ───────────────────────── 检测 ─────────────────────────
def check_once(engine=None, htk=None) -> dict:
    """跑一轮全部启用规则。返回 {"checked":n,"fired":n,"skipped":n}。"""
    rules = [r for r in list_rules() if r.get("enabled")]
    stat = {"checked": len(rules), "fired": 0, "skipped": 0}
    if not rules:
        return stat
    c = store._conn()
    cfg = get_webhook_cfg()
    now = _now()

    price_rules = [r for r in rules if r["kind"] == "price" and r.get("code")]
    quotes = _fetch_prices([r["code"] for r in price_rules]) if price_rules else {}
    market_ctx = _fetch_market(htk) if any(r["kind"] == "market" for r in rules) else {}
    market_done = False

    for r in rules:
        try:
            cd = float(r.get("cooldown_min") or DEFAULT_COOLDOWN_MIN) * 60
            if r.get("last_fired") and now - float(r["last_fired"]) < cd:
                stat["skipped"] += 1
                continue
            if r["kind"] == "price":
                q = quotes.get(r["code"])
                if not q:
                    continue
                hit, desc = eval_conds(r["conds"], r["logic"],
                                       {k: q.get(k) for k in PRICE_FIELDS},
                                       PRICE_FIELDS)
                if hit:
                    name = q.get("name") or r["code"]
                    if _emit(c, r, r["code"],
                             f"{name}（{r['code']}）{desc}",
                             q.get("change_pct"), cfg):
                        stat["fired"] += 1
            elif r["kind"] == "signal":
                m = _fetch_signal(r["code"], engine)
                if not m:
                    continue
                hit, desc = eval_conds(r["conds"], r["logic"],
                                       {k: m.get(k) for k in SIGNAL_FIELDS},
                                       SIGNAL_FIELDS)
                if hit:
                    if _emit(c, r, r["code"],
                             f"{r['code']} {desc}", m.get("close"), cfg):
                        stat["fired"] += 1
            elif r["kind"] == "market":
                if market_done:      # 一轮只判一次，避免重复告警
                    continue
                hit, desc = eval_conds(r["conds"], r["logic"], market_ctx,
                                       MARKET_FIELDS)
                if hit:
                    market_done = True
                    if _emit(c, r, "", f"全市场 {desc}",
                             market_ctx.get("limit_up_cnt"), cfg):
                        stat["fired"] += 1
            elif r["kind"] == "strategy":
                hits = _scan_strategy(r, engine)
                min_hit = int((r.get("params") or {}).get("min_hit") or 1)
                if len(hits) >= min_hit:
                    codes = "、".join(list(hits)[:8])
                    if _emit(c, r, "", f"扫描命中 {len(hits)} 只（阈值 {min_hit}）：{codes}",
                             float(len(hits)), cfg):
                        stat["fired"] += 1
        except Exception as e:
            logger.warning("alerts rule %s failed: %s", r.get("id"), e)
    return stat


def _scan_strategy(rule: dict, engine) -> List[str]:
    """按规则的个股条件扫全市场，返回命中的 code 列表。"""
    if engine is None or not getattr(engine, "loaded", False):
        return []
    conds = rule.get("conds") or []

    def pred(m, dates):
        hit, _ = eval_conds(conds, rule.get("logic", "AND"),
                            {k: m.get(k) for k in SIGNAL_FIELDS}, SIGNAL_FIELDS)
        return hit

    try:
        return list(engine.screen(pred).keys())
    except Exception as e:
        logger.warning("alerts strategy scan failed: %s", e)
        return []


# ───────────────────────── Webhook 配置（存 meta） ─────────────────────────
_WEBHOOK_KEY = "alerts_webhook_cfg"
_WEBHOOK_DEFAULT = {"enabled": 0, "feishu_url": "", "feishu_secret": "",
                    "wecom_key": "", "generic_url": "", "generic_secret": ""}


def get_webhook_cfg() -> dict:
    c = store._conn()
    r = c.execute("SELECT v FROM meta WHERE k=?", (_WEBHOOK_KEY,)).fetchone()
    if not r:
        return dict(_WEBHOOK_DEFAULT)
    try:
        cfg = json.loads(r["v"] or "{}")
    except Exception:
        cfg = {}
    out = dict(_WEBHOOK_DEFAULT)
    out.update({k: v for k, v in cfg.items() if k in _WEBHOOK_DEFAULT})
    return out


def set_webhook_cfg(payload: dict) -> dict:
    cfg = dict(_WEBHOOK_DEFAULT)
    cfg.update({k: v for k, v in (payload or {}).items() if k in _WEBHOOK_DEFAULT})
    cfg["enabled"] = int(1 if cfg.get("enabled") else 0)
    c = store._conn()
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
              (_WEBHOOK_KEY, json.dumps(cfg, ensure_ascii=False)))
    store._commit(c)
    return cfg


# ───────────────────────── 后台轮询 ─────────────────────────
def start_loop(interval: int = 60, engine_getter=None, htk=None):
    """启动检测线程（daemon）。重复调用只启动一次。"""
    global _loop_running
    if _loop_running:
        return False
    _loop_running = True

    def _run():
        while True:
            try:
                eng = engine_getter() if engine_getter else None
                check_once(engine=eng, htk=htk)
            except Exception as e:
                logger.warning("alerts loop error: %s", e)
            time.sleep(max(15, int(interval)))

    threading.Thread(target=_run, daemon=True).start()
    logger.info("alerts loop started, interval=%ss", interval)
    return True
