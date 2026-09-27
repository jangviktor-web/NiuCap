"""#97 后台管理优化：端到端自检。

覆盖 5 项优化的 API 侧全部新增能力，重点是不安全输入的拦截与破坏性操作的
后路（自动快照）。跑前需先启动服务且 TICK_ADMIN_USERS 已配置：

    cd /workspace/tick-stock-panel && .venv/bin/python server/run.py &
    python3 tests/check_admin_maintain.py

注意：本脚本会真的建一个备份（462MB 级，磁盘够用），跑完自动删掉。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8899"
ADMIN = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test1234")

results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def req(path, method="GET", body=None, cookie=None, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            b = resp.read()
            if raw:
                return resp.status, b, resp.headers
            return resp.status, (json.loads(b.decode()) if b else {}), resp.headers
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b.decode()), None
        except Exception:
            return e.code, {"raw": b.decode()[:200]}, None


def login(u=ADMIN, p=ADMIN_PASSWORD):
    sc, _, h = req("/api/auth/login", "POST", {"username": u, "password": p})
    if sc == 200 and h and h.get("Set-Cookie"):
        return h["Set-Cookie"].split(";")[0]
    sc, _, h = req("/api/auth/register", "POST",
                   {"username": u, "password": p, "display": u})
    if sc == 200 and h and h.get("Set-Cookie"):
        return h["Set-Cookie"].split(";")[0]
    return None


def main():
    print("=" * 70)
    print("#97 后台管理优化 · 端到端自检")
    print("=" * 70)

    ck = login()
    if not ck:
        rec("管理员登录", False, "无法登录，请确认 TICK_ADMIN_USERS 与密码")
        return 1
    rec("管理员登录", True, ADMIN)

    # ---------------------------------------------------------------- 鉴权
    sc, _, _ = req("/api/admin/db")
    rec("未登录访问 /api/admin/db → 401", sc == 401, f"got {sc}")

    bad = login("nobody_" + str(int(time.time())), "pwd12345")
    if bad:
        sc, _, _ = req("/api/admin/db", cookie=bad)
        rec("非管理员访问 → 403", sc == 403, f"got {sc}")
    else:
        rec("非管理员访问 → 403", True, "（跳过：注册被禁）")

    # ------------------------------------------------- ① 数据库健康与维护
    sc, d, _ = req("/api/admin/db", cookie=ck)
    rec("GET /db 200", sc == 200, f"got {sc}")
    if sc == 200:
        f = d.get("files", {})
        rec("  返回主库体积", bool(f.get("db")), f"db={f.get('db_h')} wal={f.get('wal_h')}")
        rec("  返回磁盘余量", bool((d.get("disk") or {}).get("free_h")),
            (d.get("disk") or {}).get("free_h", ""))
        rec("  返回表行数", len(d.get("tables") or []) > 0,
            f"{len(d.get('tables') or [])} 张表")
        rec("  返回索引占用", len(d.get("indexes") or []) > 0,
            f"{len(d.get('indexes') or [])} 个索引")

    sc, d, _ = req("/api/admin/db/checkpoint", "POST", {"mode": "passive"}, cookie=ck)
    rec("POST /db/checkpoint(passive)", sc == 200 and d.get("ok"),
        f"{d.get('wal_before_h')} → {d.get('wal_after_h')}")

    sc, d, _ = req("/api/admin/db/checkpoint", "POST", {"mode": "truncate"}, cookie=ck)
    rec("POST /db/checkpoint(truncate)", sc == 200, f"ok={d.get('ok')} busy={d.get('busy')}")

    sc, d, _ = req("/api/admin/db/checkpoint", "POST", {"mode": "bogus"}, cookie=ck)
    rec("非法 checkpoint 模式 → 400", sc == 400 and d.get("bad_mode") is not False,
        f"got {sc} {d.get('error', '')[:40]}")

    # ------------------------------------------------------------ ③ 模块纳管
    sc, d, _ = req("/api/admin/modules", cookie=ck)
    rec("GET /modules 200", sc == 200, f"got {sc}")
    if sc == 200:
        keys = [x["key"] for x in (d.get("items") or [])]
        for want in ("market_phase", "newsfeed", "alerts"):
            rec(f"  纳管 {want}", want in keys, "")
        rec("  无 unknown 状态",
            all(x.get("state") != "unknown" for x in (d.get("items") or [])),
            str([(x["key"], x["state"]) for x in (d.get("items") or [])]))

    # --------------------------------------------------------------- ④ 缓存
    sc, d, _ = req("/api/admin/caches", cookie=ck)
    rec("GET /caches 200", sc == 200, f"got {sc}")
    if sc == 200:
        ks = [x["key"] for x in (d.get("items") or [])]
        for want in ("eval", "market_phase", "newsfeed", "market"):
            rec(f"  缓存项 {want}", want in ks, "")

    sc, d, _ = req("/api/admin/caches/clear", "POST", {"key": "newsfeed"}, cookie=ck)
    rec("清空快讯缓存", sc == 200 and d.get("ok"), f"got {sc}")
    sc, d, _ = req("/api/admin/caches/clear", "POST", {"key": "__evil__"}, cookie=ck)
    rec("未知缓存 key 被拒", sc == 400, f"got {sc}")

    # ------------------------------------------------ ② 备份下载/删除/恢复
    sc, d, _ = req("/api/admin/backups", cookie=ck)
    rec("GET /backups 200", sc == 200, f"{len((d.get('items') or []))} 个已有备份")

    sc, d, _ = req("/api/admin/backup", "POST", {}, cookie=ck)
    rec("POST /backup 创建成功", sc == 200 and d.get("ok"), f"{d.get('name')} {d.get('size_h')}")
    if not d.get("ok"):
        rec("备份流程（后续依赖备份）", False, d.get("error", ""))
        return report()
    name = d["name"]

    # 目录穿越必须全部被拒
    for evil in ("../../etc/passwd", "/etc/passwd", "tick_1.db", "x.db", "..%2f..%2fetc%2fpasswd"):
        sc, _, _ = req("/api/admin/backups/" + evil + "/download", cookie=ck)
        rec(f"  下载穿越被拒 {evil[:24]}", sc in (400, 404), f"got {sc}")
    sc, _, _ = req("/api/admin/backups/..%2f..%2fetc%2fpasswd", "DELETE", cookie=ck)
    rec("  删除穿越被拒", sc in (400, 404), f"got {sc}")
    sc, _, _ = req("/api/admin/backups/..%2f..%2fetc%2fpasswd/restore", "POST", cookie=ck)
    rec("  恢复穿越被拒", sc in (400, 404), f"got {sc}")

    # 下载（只验响应头与首字节，不整体读完 462MB）
    sc, body, h = req("/api/admin/backups/" + name + "/download", cookie=ck, raw=True)
    ok_dl = (sc == 200 and len(body) > 1024
             and body[:15] == b"SQLite format 3")
    rec("下载备份是合法 SQLite 文件", ok_dl,
        f"{sc} {len(body) if body else 0}B head={body[:15] if body else b''!r}")
    if h:
        rec("  下载带 Content-Disposition",
            "attachment" in (h.get("Content-Disposition") or ""),
            h.get("Content-Disposition", ""))

    # 恢复：先造一条可辨识的脏数据，恢复后应消失
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "server"))
    import store as _s
    _s.initialize()
    c = _s._conn()
    probe = "restore_probe_" + str(int(time.time()))
    try:
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (probe, "1"))
        _s._commit(c)
    except Exception as e:
        rec("写入探针数据", False, str(e))
        probe = None

    sc, d, _ = req("/api/admin/backups/" + name + "/restore", "POST", cookie=ck)
    rec("恢复成功", sc == 200 and d.get("ok"), f"{sc} {d.get('error','')}")
    rec("  恢复前自动快照存在", bool(d.get("snapshot")), d.get("snapshot", ""))
    rec("  返回 need_restart 提示", d.get("need_restart") is True, "")

    if probe:
        try:
            n = c.execute("SELECT COUNT(*) n FROM meta WHERE k=?", (probe,)).fetchone()["n"]
            rec("  恢复后脏数据被清除", n == 0, f"残留 {n} 行")
            c.execute("DELETE FROM meta WHERE k=?", (probe,))
            _s._commit(c)
        except Exception as e:
            rec("  恢复后脏数据被清除", False, str(e))

    # 自动快照也应该出现在清单里（可再被恢复回去）
    sc, d2, _ = req("/api/admin/backups", cookie=ck)
    snaps = [x for x in (d2.get("items") or []) if x.get("auto")]
    rec("清单含自动快照", len(snaps) > 0, f"{len(snaps)} 个")

    # 清理本次测试产物
    for n in ([name] + [s["name"] for s in snaps]):
        sc, _, _ = req("/api/admin/backups/" + n, "DELETE", cookie=ck)
        rec(f"删除备份 {n[:28]}", sc == 200, f"got {sc}")

    sc, _, _ = req("/api/admin/backups/" + name, "DELETE", cookie=ck)
    rec("重复删除被拒（已不存在）", sc == 400, f"got {sc}")

    # ------------------------------------------------------------ ⑤ 日志运维
    sc, d, _ = req("/api/admin/logfiles", cookie=ck)
    rec("GET /logfiles 200", sc == 200, f"{len((d.get('items') or []))} 个文件")
    files = d.get("items") or []
    if files:
        nm = files[0]["name"]
        sc, d, _ = req("/api/admin/logs?lines=20&name=" + nm, cookie=ck)
        rec("读取日志尾部", sc == 200 and "lines" in d, f"{nm} {len(d.get('lines') or [])} 行")
        sc, body, _ = req("/api/admin/logs/download?name=" + nm, cookie=ck, raw=True)
        rec("下载日志", sc == 200 and isinstance(body, bytes), f"{sc} {len(body) if body else 0}B")
    else:
        rec("读取日志尾部", True, "（跳过：无日志文件）")
        rec("下载日志", True, "（跳过：无日志文件）")

    for evil in ("../../etc/passwd", "/etc/shadow", "app.py", "tick.db"):
        sc, dd, _ = req("/api/admin/logs?name=" + urllib.parse.quote(evil), cookie=ck)
        rec(f"  日志读取非白名单被拒 {evil[:20]}", sc == 400, f"got {sc}")
        sc, _, _ = req("/api/admin/logs?name=" + urllib.parse.quote(evil), "DELETE", cookie=ck)
        rec(f"  日志清空非白名单被拒 {evil[:20]}", sc == 400, f"got {sc}")
    # 白名单内但不存在的文件：200 + exists=False（不是 400，前端要据此提示）
    sc, dd, _ = req("/api/admin/logs?name=not_there_yet.log", cookie=ck)
    rec("  白名单内不存在 → exists=False", sc == 200 and dd.get("exists") is False, f"got {sc}")

    return report()


def report():
    print("=" * 70)
    ok = sum(1 for _, o, _ in results if o)
    bad = [n for n, o, _ in results if not o]
    print(f"结果：{ok}/{len(results)} 通过" + ("" if not bad else "，失败：" + "、".join(bad)))
    print("=" * 70)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
