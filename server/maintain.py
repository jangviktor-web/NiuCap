# -*- coding: utf-8 -*-
"""
后台运维工具集（#97）：数据库健康与维护 / 备份下载删除恢复 / 新模块纳管 /
缓存统一管理 / 日志运维。

本模块只做「读状态 + 文件级维护」，业务读写一律不碰。三条铁律：

  1. 绝不复用 store._conn()——它是 thread-local，维护操作必须开独立 sqlite3
     连接，否则会把维护动作混进业务事务（且 checkpoint 需要写连接）。
  2. 破坏性操作先留后路——恢复前强制自动快照；日志只清空（truncate 保留
     inode）不删除。
  3. 路径校验只走 _safe_backup_path() / _safe_log_path() 两个入口——任何
     调用点都不许自己拼路径，挡住 ../ 目录穿越。

来源：项目自身运维实践（曾踩 WAL 228MB 僵死锁、备份只能建不能管）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = __import__("logging").getLogger(__name__)

# 备份文件名白名单：tick_20260928_041418.db 或 auto_before_restore_*.db
_BACKUP_RE = re.compile(r"^(tick|auto_before_restore)_\d{8}_\d{6}\.db$")
# 日志文件名：data/ 下的普通 .log / .json
_LOG_RE = re.compile(r"^[\w.\-]+\.(log|json)$")


def _store():
    import store as _s
    return _s


def _backup_dir() -> str:
    return os.path.join(_store().DATA_DIR, "backups")


def _fmt_ts(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _size(p: str) -> int:
    try:
        return os.path.getsize(p) if os.path.isfile(p) else 0
    except OSError:
        return 0


def _human(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


# ===========================================================================
# ① 数据库健康与维护
# ===========================================================================

def db_status() -> Dict[str, Any]:
    """数据库占用画像：文件体积（db+wal+shm）、页统计、各表行数与体积、磁盘余量。

    ponytail: 表体积走 SQLite 的 dbstat 虚拟表（本环境可用）；不可用时降级为
    None，前端显示「—」，不为此引入全表扫描。
    """
    st = _store()
    out: Dict[str, Any] = {
        "backend": st.BACKEND,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": {}, "pages": {}, "tables": [], "indexes": [], "disk": {},
    }
    d = st.DATA_DIR
    try:
        du = shutil.disk_usage(d if os.path.isdir(d) else "/")
        out["disk"] = {"free": du.free, "total": du.total,
                       "used_pct": round(du.used / du.total * 100, 1) if du.total else 0,
                       "free_h": _human(du.free), "total_h": _human(du.total)}
    except Exception as e:
        out["disk"] = {"error": f"{type(e).__name__}: {e}"}

    if st.BACKEND == "sqlite":
        base = st.DB_PATH
        f = {"db": _size(base), "wal": _size(base + "-wal"), "shm": _size(base + "-shm")}
        f["total"] = f["db"] + f["wal"] + f["shm"]
        f["db_h"] = _human(f["db"])
        f["wal_h"] = _human(f["wal"])
        f["total_h"] = _human(f["total"])
        f["path"] = base
        out["files"] = f
        try:
            con = sqlite3.connect(f"file:{base}?mode=ro", uri=True, timeout=10.0)
            try:
                ps = con.execute("PRAGMA page_size").fetchone()[0]
                pc = con.execute("PRAGMA page_count").fetchone()[0]
                fl = con.execute("PRAGMA freelist_count").fetchone()[0]
                jm = con.execute("PRAGMA journal_mode").fetchone()[0]
                out["pages"] = {"size": ps, "count": pc, "freelist": fl,
                                "journal_mode": jm,
                                "reclaimable": _human(fl * ps),
                                "used": _human(pc * ps)}
                tabs = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                sizes: Dict[str, int] = {}
                try:
                    for nm, sz in con.execute(
                            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                        sizes[nm] = sz or 0
                except Exception:
                    sizes = {}      # dbstat 不可用就只报行数
                for t in tabs:
                    try:
                        rows = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    except Exception:
                        rows = None
                    out["tables"].append({
                        "name": t, "rows": rows,
                        "bytes": sizes.get(t), "bytes_h": _human(sizes[t]) if t in sizes else None,
                    })
                # 索引单独列（daily_bars 上 3 个索引占了 248MB，值得单看）
                idx = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND sql IS NOT NULL ORDER BY name")]
                for i in idx:
                    if i in sizes:
                        out["indexes"].append({"name": i, "bytes": sizes[i],
                                               "bytes_h": _human(sizes[i])})
                out["indexes"].sort(key=lambda x: -x["bytes"])
            finally:
                con.close()
        except Exception as e:
            out["pages"] = {"error": f"{type(e).__name__}: {e}"}
    else:
        # MySQL/TiDB：没有文件可量，用 information_schema 的估值
        out["files"] = {"note": "MySQL 后端无单文件数据库，体积请看宿主机"}
        try:
            import store as _s
            cur = _s._conn().execute(
                "SELECT table_name, table_rows FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name")
            for r in cur.fetchall():
                out["tables"].append({"name": r["table_name"],
                                      "rows": r["table_rows"], "est": True})
        except Exception as e:
            out["tables"] = [{"error": f"{type(e).__name__}: {e}"}]
    return out


def db_checkpoint(mode: str = "passive", timeout: float = 15.0) -> Dict[str, Any]:
    """WAL checkpoint。

    mode=passive  不阻塞读写方，只把能落的页落盘（默认，安全）。
    mode=truncate 落完后把 WAL 文件截到 0；**有活跃事务时会抛
                  "database table is locked"**（实测），此时自动降级 passive。

    ponytail: 不做 VACUUM——实测 freelist=0（无碎片），收益为零却要独占库
    几分钟 + 两倍磁盘。想整理先看 pages.reclaimable。
    """
    st = _store()
    if st.BACKEND != "sqlite":
        return {"ok": False, "error": "MySQL 后端无 WAL，无需 checkpoint"}
    mode = (mode or "passive").lower()
    if mode not in ("passive", "full", "restart", "truncate"):
        # bad_mode 是**调用方传错**，不是运行故障——接口层要返 400 而非 500
        return {"ok": False, "bad_mode": True,
                "error": f"未知 checkpoint 模式：{mode}（可选 passive/full/restart/truncate）"}
    base = st.DB_PATH
    wal_before = _size(base + "-wal")
    degraded = False
    note = ""

    def _run(con, m):
        return con.execute(f"PRAGMA wal_checkpoint({m.upper()})").fetchone()

    try:
        con = sqlite3.connect(base, timeout=30.0)     # 写连接（只读连不能 checkpoint）
        try:
            con.execute(f"PRAGMA busy_timeout={int(max(0.0, float(timeout)) * 1000)}")
            try:
                row = _run(con, mode)
            except sqlite3.OperationalError as e:
                # 有活跃写事务时 TRUNCATE/FULL/RESTART 会被拒 → 降级 passive 再试
                if mode == "passive":
                    raise
                degraded = True
                note = f"{mode} 被拒（{e}），已降级 passive"
                mode = "passive"
                row = _run(con, "passive")
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        # 连 passive 都被拒 = 此刻确实有活跃写事务（正在落库 / 同步）。
        # 这不是故障，如实返回 busy，前端提示稍后重试，绝不 500。
        return {"ok": False, "busy": True, "mode": mode, "degraded": degraded,
                "error": f"此刻有活跃写事务，checkpoint 被拒：{e}",
                "note": "数据同步或落库进行中会占写锁，稍后重试即可（不影响业务读写）。",
                "wal_before": wal_before, "wal_after": _size(base + "-wal"),
                "wal_before_h": _human(wal_before)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "wal_before": wal_before}
    busy, log, ckpt = (list(row) + [0, 0, 0])[:3]
    return {"ok": True, "mode": mode, "degraded": degraded, "note": note,
            "busy": busy, "log": log, "checkpointed": ckpt,
            "wal_before": wal_before, "wal_after": _size(base + "-wal"),
            "wal_before_h": _human(wal_before),
            "wal_after_h": _human(_size(base + "-wal"))}


# ===========================================================================
# ② 备份：列表 / 创建 / 下载路径 / 删除 / 恢复
# ===========================================================================

def _safe_backup_path(name: str) -> Optional[str]:
    """备份文件名的唯一校验入口：白名单正则 + realpath 前缀，双保险挡 ../ """
    if not isinstance(name, str) or not name:
        return None
    if not _BACKUP_RE.match(name):
        return None
    d = os.path.realpath(_backup_dir())
    p = os.path.realpath(os.path.join(d, name))
    if os.path.commonpath([d, p]) != d or not os.path.isfile(p):
        return None
    return p


def backup_list() -> Dict[str, Any]:
    """备份清单（按时间倒序）。"""
    st = _store()
    out: Dict[str, Any] = {"backend": st.BACKEND, "items": []}
    if st.BACKEND != "sqlite":
        return out
    d = _backup_dir()
    try:
        for fn in sorted(os.listdir(d), reverse=True):
            p = _safe_backup_path(fn)
            if not p:
                continue        # 非白名单文件（人工丢进去的）不纳入管理
            out["items"].append({"name": fn, "size": os.path.getsize(p),
                                 "size_h": _human(os.path.getsize(p)),
                                 "mtime": _fmt_ts(os.path.getmtime(p)),
                                 "auto": fn.startswith("auto_before_restore_")})
    except FileNotFoundError:
        pass
    return out


def backup_create(prefix: str = "tick") -> Dict[str, Any]:
    """在线一致备份（sqlite3.backup 读穿 WAL，服务无需停）。

    备份前检查磁盘余量：不足源文件 2 倍直接拒绝，免得把盘撑爆写不进库。
    """
    st = _store()
    if st.BACKEND != "sqlite":
        return {"ok": False, "error": "当前为 MySQL 后端，请在宿主机用 mysqldump 备份"}
    src = st.DB_PATH
    if not os.path.isfile(src):
        return {"ok": False, "error": f"数据库文件不存在：{src}"}
    d = _backup_dir()
    os.makedirs(d, exist_ok=True)
    need = os.path.getsize(src) + _size(src + "-wal")
    try:
        free = shutil.disk_usage(d).free
    except Exception:
        free = None
    if free is not None and free < need * 2:
        return {"ok": False, "error":
                f"磁盘余量不足：需 {_human(need)}，可用 {_human(free)}，请先删除旧备份"}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(d, f"{prefix}_{ts}.db")
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
        try:
            bk = sqlite3.connect(dst, timeout=30.0)
            try:
                con.backup(bk)
            finally:
                bk.close()
        finally:
            con.close()
    except Exception as e:
        try:                      # 失败不留 0 字节残档
            if os.path.exists(dst):
                os.remove(dst)
        except Exception:
            pass
        return {"ok": False, "error": f"备份失败：{type(e).__name__}: {e}"}
    return {"ok": True, "name": os.path.basename(dst),
            "size": os.path.getsize(dst), "size_h": _human(os.path.getsize(dst)),
            "path": dst}


def backup_delete(name: str) -> Dict[str, Any]:
    st = _store()
    if st.BACKEND != "sqlite":
        return {"ok": False, "error": "MySQL 后端无文件备份"}
    p = _safe_backup_path(name)
    if not p:
        return {"ok": False, "error": "非法备份名"}
    try:
        size = os.path.getsize(p)
        os.remove(p)
        return {"ok": True, "name": name, "freed": size, "freed_h": _human(size)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def backup_restore(name: str) -> Dict[str, Any]:
    """用备份覆盖当前库。

    安全网两道：
      - 恢复前**强制**先快照当前库为 auto_before_restore_{ts}.db，手滑还能回退；
      - 用 sqlite3 backup 反向写（src=备份 ro → dst=主库 rw），**不删不重命名**
        主库文件——否则 inode 变了，服务进程旧连接会写进幽灵文件。

    恢复完返回 need_restart=true：进程内旧连接的页缓存不会自动失效，必须重启。
    """
    st = _store()
    if st.BACKEND != "sqlite":
        return {"ok": False, "error": "MySQL 后端不支持在线恢复，请用 mysql < dump.sql"}
    src = _safe_backup_path(name)
    if not src:
        return {"ok": False, "error": "非法备份名"}
    snap = backup_create(prefix="auto_before_restore")
    if not snap.get("ok"):
        return {"ok": False, "error": f"恢复前自动快照失败，已中止：{snap.get('error')}"}
    dst = st.DB_PATH
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
        try:
            d = sqlite3.connect(dst, timeout=30.0)
            try:
                d.execute("PRAGMA busy_timeout=15000")
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "error": f"恢复失败：{type(e).__name__}: {e}",
                "snapshot": snap["name"]}

    # 恢复写进去的量等于整个库（实测 461MB 库 → WAL 464MB，磁盘占用直接翻倍），
    # 而自动 checkpoint 只挪了 3 页就停了。这里顺手截断一次；截断失败不影响
    # 恢复结果，只在 note 里说明，让用户去数据库面板手动点。
    ck = db_checkpoint("truncate", timeout=5.0)
    wal_note = ("并已收回 WAL"
                if ck.get("ok") and not ck.get("busy") and ck.get("wal_after") == 0
                else "（WAL 未收回，可在数据库面板手动落盘）")
    return {"ok": True, "name": name, "snapshot": snap["name"],
            "snapshot_h": snap.get("size_h"),
            "need_restart": True,
            "wal_before_h": ck.get("wal_before_h"), "wal_after_h": ck.get("wal_after_h"),
            "wal_note": wal_note,
            "msg": (f"已用备份覆盖当前库{wal_note}。"
                    "请重启服务进程，否则旧连接仍持有缓存页。")}


# ===========================================================================
# ③ 新模块纳管（#88 快讯 / #95 情绪周期 / #96 监控中心）
#
# 只做本地状态探测，一个都不联网——后台页面不该因为某个源超时而打不开。
# ===========================================================================

def modules_status() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []

    def add(key, name, task, state, detail, extra=None):
        d = {"key": key, "name": name, "task": task, "state": state, "detail": detail}
        if extra:
            d.update(extra)
        items.append(d)

    # --- #95 市场情绪周期（内存 + 磁盘双缓存） ---
    try:
        import market_phase as mp
        cs = mp.cache_status()
        add("market_phase", "市场情绪周期", "#95",
            "ready" if cs.get("ready") else "idle",
            f"缓存至 {cs.get('max_date') or '—'}，{cs.get('rows') or 0} 个交易日"
            + (f"，当前 {cs['phase']}" if cs.get("phase") else ""),
            {"cache_file": cs.get("path"), "cache_bytes": cs.get("bytes"),
             "cache_bytes_h": _human(cs["bytes"]) if cs.get("bytes") else None,
             "cache_mtime": cs.get("mtime"), "in_memory": cs.get("in_memory")})
    except Exception as e:
        add("market_phase", "市场情绪周期", "#95", "unknown", f"{type(e).__name__}: {e}")

    # --- #88 双源快讯 ---
    try:
        import newsfeed as nf
        cs = nf.cache_status()
        add("newsfeed", "双源快讯流", "#88",
            "ready" if cs.get("items") else "idle",
            f"{cs.get('items') or 0} 条，TTL {cs.get('ttl')}s"
            + (f"，{cs.get('age')}s 前更新" if cs.get("age") is not None else ""),
            {"sources": cs.get("sources"), "errors": cs.get("errors")})
    except Exception as e:
        add("newsfeed", "双源快讯流", "#88", "unknown", f"{type(e).__name__}: {e}")

    # --- #96 监控中心 ---
    try:
        import alerts as alt
        cs = alt.admin_status()
        add("alerts", "监控中心", "#96",
            "ready" if cs.get("n_rules") else "idle",
            f"{cs.get('n_rules') or 0} 条规则 · {cs.get('n_alerts') or 0} 条告警"
            + (f"（{cs['unread']} 未读）" if cs.get("unread") else ""),
            {"unread": cs.get("unread"), "webhook": cs.get("webhook"),
             "loop_running": cs.get("loop_running"), "last_check": cs.get("last_check")})
    except Exception as e:
        add("alerts", "监控中心", "#96", "unknown", f"{type(e).__name__}: {e}")

    # --- #100 连板梯队 + 题材雷达 ---
    try:
        add("limitup", "连板梯队", "#100", "ready",
            "实时计算：涨停子集取 K 线判连板天数，按交易日缓存 + 后台预热")
    except Exception as e:
        add("limitup", "连板梯队", "#100", "unknown", f"{type(e).__name__}: {e}")
    try:
        add("theme", "题材雷达", "#100", "ready",
            "westock 行业板块四维评分 + 融合去重（概念口径待东财开放）")
    except Exception as e:
        add("theme", "题材雷达", "#100", "unknown", f"{type(e).__name__}: {e}")

    # --- 数据同步守门（#85/#38） ---
    try:
        import store as _s
        cur = _s._conn().execute(
            "SELECT COUNT(*) c, MAX(synced_at) m, SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) f "
            "FROM bar_sync")
        r = cur.fetchone()
        add("bars_sync", "日线同步守门", "#38/#85",
            "ready" if r["c"] else "idle",
            f"{r['c'] or 0} 只标的已登记，失败 {r['f'] or 0} 只"
            + (f"，最近 {_fmt_ts(r['m'])}" if r["m"] else ""))
    except Exception as e:
        add("bars_sync", "日线同步守门", "#38/#85", "unknown", f"{type(e).__name__}: {e}")

    # #103/#104/#105 的新探测单独封装：整体兜底，坏掉也不牵连上面这批
    try:
        items.extend(_new_modules_status())
    except Exception as e:
        items.append({"key": "new_modules", "name": "新模块探测",
                      "task": "#103/#104/#105", "state": "unknown",
                      "detail": f"{type(e).__name__}: {e}"})

    return {"items": items, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _new_modules_status() -> List[Dict[str, Any]]:
    """近几轮新增模块的纳管（#103 交易时段守卫 / #104 交易日历 / #105 选股历史）。

    单独成一个函数：旧模块的探测逻辑一行都不动，新增的挂在这里。
    """
    items: List[Dict[str, Any]] = []

    def add(key, name, task, state, detail, extra=None):
        d = {"key": key, "name": name, "task": task, "state": state, "detail": detail}
        if extra:
            d.update(extra)
        items.append(d)

    # --- #103 虚拟盘交易时段守卫 ---
    try:
        import datasource as ds
        st = ds.market_state()
        # 口径与 app._require_tradable 一致：连续竞价 + 集合竞价才允许买卖
        tradable = st.get("state") in ("trading", "auction")
        # 状态恒为 active：守卫一直在工作，"空闲"会被误读成没生效
        add("trade_guard", "虚拟盘交易时段守卫", "#103", "active",
            f"{st.get('label') or st.get('state')} → "
            + ("允许买卖" if tradable else "已拦截买卖"),
            {"market_state": st.get("state"), "tradable": tradable})
    except Exception as e:
        add("trade_guard", "虚拟盘交易时段守卫", "#103", "unknown",
            f"{type(e).__name__}: {e}")

    # --- #104 本地交易日历（节假日 / 补班） ---
    try:
        import holidays as hl
        today = datetime.now().strftime("%Y-%m-%d")
        hol, mk = hl.HOLIDAYS or set(), hl.MAKEUP_WORKDAYS or set()
        ys = sorted({d[:4] for d in hol})
        future = sorted(d for d in hol if d >= today)
        add("calendar", "本地交易日历", "#104",
            "ready" if hol else "idle",
            f"休市 {len(hol)} 天 / 补班 {len(mk)} 天（覆盖 {'、'.join(ys) or '—'} 年）"
            + (f"，下一个休市日 {future[0]}" if future
               else "，⚠ 表中已无未来休市日，需补充新年度"),
            {"years": ys, "future_holidays": len(future)})
    except Exception as e:
        add("calendar", "本地交易日历", "#104", "unknown", f"{type(e).__name__}: {e}")

    # --- #105 选股历史（三选股页自动存档） ---
    try:
        import store as _s
        st = _s.count_screen_history()
        row = _s._conn().execute(
            "SELECT COUNT(*) c, MAX(created_at) m FROM screen_history "
            "WHERE created_at > ?", (time.time() - 86400,)).fetchone()
        _label = {"newbie": "小白选股", "strategy": "策略选股", "screen": "条件选股"}
        mods = "、".join(f"{_label.get(k, k)} {v}"
                        for k, v in (st.get("by_module") or {}).items())
        add("screen_history", "选股历史存档", "#105",
            "ready" if st.get("total") else "idle",
            f"累计 {st['total']} 条（{mods or '—'}），近 24 小时 {row['c'] or 0} 条"
            + (f"，最新 {_fmt_ts(row['m'])}" if row["m"] else "")
            + (f"，未归属 {st['orphan']} 条" if st.get("orphan") else ""),
            {"total": st.get("total"), "orphan": st.get("orphan"),
             "by_module": st.get("by_module")})
    except Exception as e:
        add("screen_history", "选股历史存档", "#105", "unknown",
            f"{type(e).__name__}: {e}")

    # --- #105 自选股分组 ---
    try:
        import store as _s
        f = _s._conn().execute("SELECT COUNT(*) c FROM folders").fetchone()["c"]
        it = _s._conn().execute("SELECT COUNT(*) c FROM watch_items").fetchone()["c"]
        add("watch_folder", "自选股分组", "#105",
            "ready" if f else "idle",
            f"全站 {f} 个分组 · {it} 条自选（支持批量加入自选）")
    except Exception as e:
        add("watch_folder", "自选股分组", "#105", "unknown", f"{type(e).__name__}: {e}")

    # --- #105 窗口胜率回测所依赖的分钟数据源 ---
    try:
        import datasource as ds
        s = ds.intraday_source_status() or {}
        add("intraday", "分钟数据源（回测依赖）", "#105",
            "ready" if s.get("eltdx") else "idle",
            f"主源 {s.get('primary') or '—'}"
            + (f" · eltdx {s.get('version')}" if s.get("version") else "")
            + (f" · {s.get('note')}" if s.get("note") else ""))
    except Exception as e:
        add("intraday", "分钟数据源（回测依赖）", "#105", "unknown",
            f"{type(e).__name__}: {e}")

    return items


# ===========================================================================
# ④ 缓存统一管理
# ===========================================================================

def cache_status(market_getter=None) -> Dict[str, Any]:
    """各模块缓存状态汇总。每项独立 try，坏一个不影响其它。"""
    items: List[Dict[str, Any]] = []

    # 体检缓存（#80，内存单槽；#98 起每天 16:30 自动预热）
    try:
        import strategy_eval as se
        c = se.cache_status()
        sch = c.get("schedule") or {}
        items.append({
            "key": "eval", "name": "策略体检缓存", "where": "内存（单槽）",
            "ready": c.get("ready"), "clearable": True,
            "detail": (f"{c.get('days') or 0} 天 / 前向 {c.get('forward')} / "
                       f"{c.get('n_strategies') or 0} 个策略"
                       + (f"，耗时 {c['cost_seconds']}s" if c.get("cost_seconds") else "")
                       + (f" ｜ 每日 {sch.get('at')} 自动预热"
                          if sch.get("at") else "")),
            "size": None, "mtime": c.get("computed_at"),
            "note": ("按数据判脏：数据更新或缓存为空才重算（单次 230 秒，"
                     "不无脑每天算）。清空后到点会自动补算。"),
            "schedule": sch,
        })
    except Exception as e:
        items.append({"key": "eval", "name": "策略体检缓存", "state": "unknown",
                      "detail": f"{type(e).__name__}: {e}", "clearable": False})

    # 情绪周期缓存（#95，内存 + 磁盘）
    try:
        import market_phase as mp
        c = mp.cache_status()
        items.append({
            "key": "market_phase", "name": "情绪周期缓存", "where": "内存 + 磁盘 JSON",
            "ready": c.get("ready"), "clearable": True,
            "detail": (f"缓存至 {c.get('max_date') or '—'}，{c.get('rows') or 0} 个交易日"
                       + ("，内存已装载" if c.get("in_memory") else "，仅磁盘")),
            "size": c.get("bytes"), "size_h": _human(c["bytes"]) if c.get("bytes") else None,
            "path": c.get("path"), "mtime": c.get("mtime"),
            "note": "清空后下次访问重算约 2 秒（窗口剪枝只扫 500 天）。",
        })
    except Exception as e:
        items.append({"key": "market_phase", "name": "情绪周期缓存", "state": "unknown",
                      "detail": f"{type(e).__name__}: {e}", "clearable": False})

    # 快讯缓存（#88，内存 TTL 30s）
    try:
        import newsfeed as nf
        c = nf.cache_status()
        items.append({
            "key": "newsfeed", "name": "快讯流缓存", "where": "内存（TTL 30s）",
            "ready": bool(c.get("items")), "clearable": True,
            "detail": (f"{c.get('items') or 0} 条"
                       + (f"，{c['age']}s 前更新" if c.get("age") is not None else "，尚未取过")),
            "size": None, "mtime": c.get("ts_text"),
            "note": "清空后下次打开会重新抓两个源（免费源别薅太狠，30s TTL 是有意的）。",
        })
    except Exception as e:
        items.append({"key": "newsfeed", "name": "快讯流缓存", "state": "unknown",
                      "detail": f"{type(e).__name__}: {e}", "clearable": False})

    # 行情快照（进程内，由 app 传入 getter 避免循环导入）
    if market_getter:
        try:
            rows, upd = market_getter()
            items.append({
                "key": "market", "name": "全市场行情快照", "where": "内存（后台刷新）",
                "ready": bool(rows), "clearable": False,
                "detail": f"{len(rows) if rows else 0} 只标的"
                          + (f"，更新于 {_fmt_ts(upd)}" if upd else "，尚未加载"),
                "size": None, "mtime": _fmt_ts(upd) if upd else None,
                "note": "只读展示；由后台线程按调度自动刷新，不提供手动清空。",
            })
        except Exception as e:
            items.append({"key": "market", "name": "全市场行情快照", "state": "unknown",
                          "detail": f"{type(e).__name__}: {e}", "clearable": False})

    return {"items": items, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def cache_clear(key: str) -> Dict[str, Any]:
    """按 key 清一个缓存。未知 key 直接报错（不静默成功）。"""
    if key == "eval":
        import strategy_eval as se
        return {"ok": True, "key": key, **se.clear_cache()}
    if key == "market_phase":
        import market_phase as mp
        return {"ok": True, "key": key, **mp.clear_cache()}
    if key == "newsfeed":
        import newsfeed as nf
        return {"ok": True, "key": key, **nf.clear_cache()}
    return {"ok": False, "error": f"未知缓存 key：{key}"}


# ===========================================================================
# ⑤ 日志运维
# ===========================================================================

def _valid_log_name(name: str) -> bool:
    """只看名字是否合法（不要求文件存在）——让接口能区分 400 与「文件不存在」。"""
    return bool(isinstance(name, str) and name and _LOG_RE.match(name))


def _safe_log_path(name: str) -> Optional[str]:
    if not _valid_log_name(name):
        return None
    d = os.path.realpath(_store().DATA_DIR)
    p = os.path.realpath(os.path.join(d, name))
    if os.path.commonpath([d, p]) != d or not os.path.isfile(p):
        return None
    return p


def log_files() -> Dict[str, Any]:
    d = _store().DATA_DIR
    out = []
    try:
        for fn in sorted(os.listdir(d)):
            p = _safe_log_path(fn)
            if not p:
                continue
            out.append({"name": fn, "size": os.path.getsize(p),
                        "size_h": _human(os.path.getsize(p)),
                        "mtime": _fmt_ts(os.path.getmtime(p))})
    except FileNotFoundError:
        pass
    out.sort(key=lambda x: -x["mtime"] and x["name"])
    return {"items": out}


def log_tail(name: str, lines: int = 200) -> Dict[str, Any]:
    p = _safe_log_path(name)
    if not p:
        return {"name": name, "exists": False, "lines": [], "error": "非法文件名"}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-max(1, min(int(lines), 5000)):]
        return {"name": name, "exists": True, "size": os.path.getsize(p),
                "size_h": _human(os.path.getsize(p)),
                "lines": [x.rstrip() for x in tail]}
    except Exception as e:
        return {"name": name, "exists": False, "lines": [],
                "error": f"{type(e).__name__}: {e}"}


def log_clear(name: str) -> Dict[str, Any]:
    """清空日志内容（truncate 到 0，保留 inode——正在写的句柄不会断）。

    ponytail: 刻意不提供删除文件。删了 inode，写方会继续往已 unlink 的文件
    里写，日志看着「清空了」实际还在吃磁盘，比不清更坑。
    """
    p = _safe_log_path(name)
    if not p:
        return {"ok": False, "error": "非法文件名"}
    try:
        before = os.path.getsize(p)
        with open(p, "w", encoding="utf-8"):
            pass
        return {"ok": True, "name": name, "before": before, "before_h": _human(before)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ===========================================================================
# 自检（非平凡逻辑留一个可运行自检：路径白名单 + 备份往返 + checkpoint 降级）
# ===========================================================================

def selfcheck() -> int:
    import tempfile
    fails = []

    def eq(got, want, label):
        if got != want:
            fails.append(f"{label}: got={got!r} want={want!r}")
            print(f"  ✗ {label}: got={got!r} want={want!r}")
        else:
            print(f"  ✓ {label}")

    print("[maintain.selfcheck]")

    # --- 路径白名单挡穿越 ---
    for bad in ("../../etc/passwd", "tick_1.db", "tick_20260928.db",
                "/etc/passwd", "tick_20260928_041418.db.bak", "", None, 123,
                "tick_20260928_041418.txt"):
        eq(_safe_backup_path(bad), None, f"备份名拒绝 {bad!r}")
    for bad in ("../app.py", "/etc/shadow", "a.sh", ""):
        eq(_safe_log_path(bad), None, f"日志名拒绝 {bad!r}")

    # --- 备份 → 列表 → 恢复 → 删除 往返（用临时库，不碰生产） ---
    import store as _s
    if _s.BACKEND == "sqlite":
        tmp = tempfile.mkdtemp(prefix="maint_")
        tdb = os.path.join(tmp, "t.db")
        con = sqlite3.connect(tdb)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE probe(a INTEGER)")
        con.executemany("INSERT INTO probe VALUES(?)", [(i,) for i in range(100)])
        con.execute("INSERT INTO probe VALUES(-1)")   # 留个活跃写事务
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            print("  · 活跃写事务下 TRUNCATE 未冲突（环境差异，跳过）")
        except sqlite3.OperationalError as e:
            print(f"  ✓ 活跃写事务下 TRUNCATE 如预期被拒：{e}")
        con.commit()
        con.close()

        # 用一个假 DB_PATH 跑完整往返
        real_db, real_dir = _s.DB_PATH, _s.DATA_DIR
        try:
            _s.DB_PATH = tdb
            _s.DATA_DIR = tmp
            # 无冲突时两种模式都应成功
            r1 = db_checkpoint("passive")
            eq(r1.get("ok"), True, "PASSIVE checkpoint 成功")
            r2 = db_checkpoint("truncate")
            eq(r2.get("ok"), True, "TRUNCATE checkpoint 成功")
            eq(r2.get("wal_after", 1), 0, "TRUNCATE 后 WAL 归零")
            # 持写事务时：TRUNCATE 被拒 → 降级 passive → 仍被拒 → 如实报 busy
            # 先把 WAL 撑起来。注意：最后一个连接关闭时 SQLite 会自动 checkpoint
            # 并把 -wal 文件删掉，所以全程得留一个连接开着。
            hold = sqlite3.connect(tdb)          # 占位连接，防止 WAL 被自动清理
            hold.execute("PRAGMA journal_mode").fetchone()   # 空 connect 不开文件，得先查一下
            w = sqlite3.connect(tdb)
            w.executemany("INSERT INTO probe VALUES(?)", [(i,) for i in range(5000)])
            w.commit()
            w.close()
            wal_full = _size(tdb + "-wal")
            eq(wal_full > 0, True, "提交后 WAL 非空")
            # checkpoint 只被**活跃读事务**阻塞（写事务不阻塞它）。占住读标记，
            # 验证接口如实报告 busy 且不抛异常——这是生产上最常撞到的场景。
            hold.execute("BEGIN")
            hold.execute("SELECT COUNT(*) FROM probe").fetchone()
            r3 = db_checkpoint("truncate", timeout=0)
            eq(r3.get("ok"), True, "有活跃读事务时 checkpoint 不抛异常")
            eq(r3.get("busy"), 1, "有活跃读事务时 busy=1（未能完成 checkpoint）")
            eq(r3.get("wal_after"), wal_full, "有活跃读事务时 WAL 未被截断")
            hold.close()
            r4 = db_checkpoint("truncate")
            eq(r4.get("ok"), True, "读事务释放后 checkpoint 成功")
            eq(r4.get("wal_after", 1), 0, "读事务释放后 WAL 被截断归零")

            b = backup_create()
            eq(b.get("ok"), True, f"备份创建成功 ({b.get('name')})")
            eq(os.path.isfile(b["path"]), True, "备份文件落盘")
            lst = backup_list()
            eq(any(i["name"] == b["name"] for i in lst["items"]), True, "备份出现在清单里")
            eq(_safe_backup_path(b["name"]) is not None, True, "备份名过白名单")

            # 造脏数据后恢复
            c2 = sqlite3.connect(tdb)
            c2.execute("INSERT INTO probe VALUES(999999)"); c2.commit()
            n_dirty = c2.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
            c2.close()
            rr = backup_restore(b["name"])
            eq(rr.get("ok"), True, "恢复成功")
            eq(rr.get("need_restart"), True, "恢复后标记需重启")
            eq(os.path.isfile(os.path.join(tmp, "backups", rr["snapshot"])), True,
               "恢复前自动快照存在")
            c3 = sqlite3.connect(tdb)
            n_after = c3.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
            eq(n_after, n_dirty - 1, f"脏数据被清除（{n_dirty} → {n_after}）")
            c3.close()

            dl = backup_delete(b["name"])
            eq(dl.get("ok"), True, "备份删除成功")
            eq(any(i["name"] == b["name"] for i in backup_list()["items"]), False,
               "删除后从清单消失")
        finally:
            _s.DB_PATH, _s.DATA_DIR = real_db, real_dir
            shutil.rmtree(tmp, ignore_errors=True)

    # --- 状态接口可用 ---
    st = db_status()
    eq("backend" in st, True, "db_status 返回 backend")
    eq(isinstance(st.get("tables"), list), True, "db_status 返回表清单")
    ms = modules_status()
    eq(len(ms["items"]) >= 4, True, f"modules_status 至少 4 项（实得 {len(ms['items'])}）")
    cst = cache_status()
    eq(len(cst["items"]) >= 3, True, f"cache_status 至少 3 项（实得 {len(cst['items'])}）")
    lr = log_clear("../../etc/passwd")
    eq(lr.get("ok"), False, "日志清空拒绝穿越")

    print(f"[maintain.selfcheck] {'通过' if not fails else str(len(fails)) + ' 项失败'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(selfcheck())
