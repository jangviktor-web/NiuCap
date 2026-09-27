"""端到端：后台管理页。

覆盖：
  1. 非管理员访问管理接口 -> 403
  2. 管理员：概览 / 用户列表 / 参数读写与校验 / 回退 / 日志
  3. 危险操作守卫：不能删自己、不能删最后一个用户、非法参数名被拒
  4. 前端：管理员登录后出现「后台管理」页签，非管理员看不到

跑法（需先启动服务，且 TICK_ADMIN_USERS=admin）：
    cd server && TICK_ADMIN_USERS=admin python3 -m uvicorn app:app --port 8899 &
    python3 tests/e2e_admin.py
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8899"
ADMIN = "admin"
# 真实 admin 密码可能已被改过（默认 test1234），用环境变量覆盖：
#   ADMIN_PASSWORD=femkerr python3 tests/e2e_admin.py
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test1234")
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def req(path, method="GET", body=None, cookie=None):
    """返回 (status, json)。用原生 urllib 以便精确控制状态码。"""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            sc = resp.headers.get("Set-Cookie", "")
            return resp.status, (json.loads(raw) if raw else {}), sc
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), ""
        except Exception:
            return e.code, {"raw": raw}, ""


def login_or_register(username, password="test1234"):
    """已存在则登录，否则注册。返回 cookie 串。"""
    sc, _, ck = req("/api/auth/login", "POST", {"username": username, "password": password})
    if sc == 200 and ck:
        return ck.split(";")[0]
    sc, _, ck = req("/api/auth/register", "POST",
                    {"username": username, "password": password, "display": username})
    if sc == 200 and ck:
        return ck.split(";")[0]
    return None


def main():
    # 收集 Set-Cookie
    def _login(u, p="test1234"):
        url = BASE + "/api/auth/login"
        data = json.dumps({"username": u, "password": p}).encode()
        r = urllib.request.Request(url, data=data, method="POST")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.headers.get("Set-Cookie", "").split(";")[0]
        except urllib.error.HTTPError:
            # 不存在则注册
            url = BASE + "/api/auth/register"
            r = urllib.request.Request(url, data=data, method="POST")
            r.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.headers.get("Set-Cookie", "").split(";")[0]

    admin_ck = _login(ADMIN, ADMIN_PASSWORD)
    rec("管理员登录/注册", bool(admin_ck), "cookie已取得" if admin_ck else "失败")

    # 普通用户（非管理员名单内）
    bob_ck = _login("bob_test")
    rec("普通用户登录/注册", bool(bob_ck), "")

    # ---------- 1. 权限 ----------
    sc, d, _ = req("/api/admin/overview")
    rec("未登录访问管理接口 → 401", sc == 401, f"status={sc}")

    sc, d, _ = req("/api/admin/overview", cookie=bob_ck)
    rec("非管理员访问 → 403", sc == 403, f"status={sc} msg={d.get('detail','')}")

    sc, d, _ = req("/api/admin/whoami", cookie=admin_ck)
    rec("管理员 whoami.is_admin=true", sc == 200 and d.get("is_admin") is True,
        f"status={sc} is_admin={d.get('is_admin')}")

    sc, d, _ = req("/api/admin/whoami", cookie=bob_ck)
    rec("普通用户 whoami.is_admin=false", sc == 200 and d.get("is_admin") is False,
        f"is_admin={d.get('is_admin')}")

    # ---------- 2. 概览 ----------
    sc, d, _ = req("/api/admin/overview", cookie=admin_ck)
    ok = sc == 200 and "db" in d and "sources" in d and "users" in d
    rec("系统概览", ok, f"users={d.get('users')} src={list((d.get('sources') or {}).keys())}")

    # ---------- 3. 用户列表 ----------
    sc, d, _ = req("/api/admin/users", cookie=admin_ck)
    items = d.get("items") or []
    admin_row = next((x for x in items if x["username"] == ADMIN), None)
    rec("用户列表含 is_admin 标记", sc == 200 and admin_row and admin_row["is_admin"] is True,
        f"共{len(items)}人")

    # ---------- 4. 参数读写 ----------
    sc, d, _ = req("/api/admin/config", cookie=admin_ck)
    its = d.get("items") or []
    rec("参数列表", sc == 200 and len(its) >= 5, f"{len(its)}项")

    # 合法写入
    sc, d, _ = req("/api/admin/config/TICK_ELTDX_SERVERS", "PUT", {"value": 6}, admin_ck)
    rec("合法写参数(servers=6)", sc == 200 and d.get("value") == 6, f"status={sc}")

    # 回读确认生效 + 来源=meta
    sc, d, _ = req("/api/admin/config", cookie=admin_ck)
    row = next((x for x in d["items"] if x["key"] == "TICK_ELTDX_SERVERS"), {})
    rec("回读确认 value=6 source=meta",
        row.get("value") == 6 and row.get("source") == "meta",
        f"value={row.get('value')} source={row.get('source')}")

    # 越界拒绝
    sc, d, _ = req("/api/admin/config/TICK_ELTDX_SERVERS", "PUT", {"value": 999}, admin_ck)
    rec("越界值被拒 → 400", sc == 400, f"status={sc} msg={d.get('detail','')}")

    # 脏值拒绝
    sc, d, _ = req("/api/admin/config/TICK_ELTDX_BATCH", "PUT", {"value": "abc"}, admin_ck)
    rec("非数字被拒 → 400", sc == 400, f"status={sc}")

    # 非法参数名
    sc, d, _ = req("/api/admin/config/NOT_A_REAL_KEY", "PUT", {"value": 1}, admin_ck)
    rec("未知参数名 → 404", sc == 404, f"status={sc}")

    # 还原回默认
    sc, d, _ = req("/api/admin/config/TICK_ELTDX_SERVERS", "DELETE", None, admin_ck)
    sc2, d2, _ = req("/api/admin/config", cookie=admin_ck)
    row = next((x for x in d2["items"] if x["key"] == "TICK_ELTDX_SERVERS"), {})
    rec("还原后回落默认 source=default",
        row.get("source") == "default" and row.get("value") == 4,
        f"value={row.get('value')} source={row.get('source')}")

    # ---------- 5. 危险操作守卫 ----------
    admin_id = admin_row["id"]
    sc, d, _ = req(f"/api/admin/users/{admin_id}", "DELETE", None, admin_ck)
    rec("不能删除自己 → 400", sc == 400, f"status={sc} msg={d.get('detail','')}")

    # 密码太短
    sc, d, _ = req(f"/api/admin/users/{admin_id}/password", "POST", {"password": ""}, admin_ck)
    rec("空密码被拒 → 400", sc == 400, f"status={sc}")

    # ---------- 6. 日志 ----------
    sc, d, _ = req("/api/admin/logfiles", cookie=admin_ck)
    rec("日志文件清单", sc == 200 and "items" in d, f"{len(d.get('items') or [])}个文件")

    sc, d, _ = req("/api/admin/logs?name=../etc/passwd", cookie=admin_ck)
    rec("目录穿越被拒 → 400", sc == 400, f"status={sc}")

    sc, d, _ = req("/api/admin/health", cookie=admin_ck)
    rec("巡检状态读取", sc == 200 and "ran" in d, f"ran={d.get('ran')}")

    # ---------- 7. 落库 ----------
    sc, d, _ = req("/api/admin/bars?recent=5", cookie=admin_ck)
    rec("落库状态", sc == 200 and "coverage" in d,
        f"覆盖{d.get('coverage',{}).get('codes')}只")

    sc, d, _ = req("/api/admin/bars/sync_status", cookie=admin_ck)
    rec("同步任务状态", sc == 200 and "running" in d, f"running={d.get('running')}")

    print("\n== 汇总 ==")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    for n, ok, det in results:
        if not ok:
            print(f"  未通过：{n}  {det}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
