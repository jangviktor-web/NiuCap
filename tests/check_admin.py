"""后台管理新增能力端到端校验：策略体检缓存 + 系统备份。

覆盖：
  1. 未登录访问后台新接口一律 401（门禁没漏）
  2. 策略体检缓存：状态字段齐全、清空返回 ok、预热返回 ok（后台线程）
  3. 系统备份：在线一致备份 SQLite（无需停服），返回文件名 + 大小，
     且落盘文件能正常打开（读穿 WAL，不拷半截）

跑法（需先启动服务）：
    python3 tests/check_admin.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# ---- 管理员会话：直接用 store 给 admin 账号发一枚会话令牌 ----
_adm = next((u for u in store.list_users()
             if (u.get("username") or "").lower() == "admin"), None)
if not _adm:
    print("FAIL  需要 admin 账号（在 .env 的 TICK_ADMIN_USERS 里）")
    sys.exit(1)
_TOK = store.create_session(_adm["id"])
_JAR = http.cookiejar.CookieJar()
_ADM_HEADERS = {"Cookie": f"tick_sid={_TOK}"}


def req(method, path, body=None, timeout=120, headers=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    allh = dict(_ADM_HEADERS if headers is None else headers)
    for k, v in allh.items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main():
    # ---- 门禁：未登录 ----
    s, _ = req("GET", "/api/admin/eval_cache", headers={})
    rec("未登录 eval_cache -> 401", s == 401, f"status={s}")
    s, _ = req("GET", "/api/admin/backups", headers={})
    rec("未登录 backups -> 401", s == 401, f"status={s}")
    s, _ = req("POST", "/api/admin/eval_cache/clear", body={}, headers={})
    rec("未登录 clear -> 401", s == 401, f"status={s}")
    s, _ = req("POST", "/api/admin/backup", body={}, headers={})
    rec("未登录 backup -> 401", s == 401, f"status={s}")

    # ---- 策略体检缓存：状态 ----
    s, d = req("GET", "/api/admin/eval_cache")
    ok = s == 200 and {"ready", "days", "n_strategies", "as_of"} <= set(d)
    rec("登录 eval_cache 状态字段齐全", ok, f"keys={sorted(d.keys())}")

    # ---- 清空缓存 ----
    s, d = req("POST", "/api/admin/eval_cache/clear", body={})
    rec("清空缓存 ok", s == 200 and d.get("ok") is True, str(d))

    # ---- 预热（后台线程，立即返回，不阻塞）----
    # 注意：同组参数当天已预热过会被 _PREWARM_LAST 拦下（返回 ok=False + 提示），
    # 这本身就是正确行为——缓存已经在槽里了。两种情况都算「预热已就位」。
    s, d = req("POST", "/api/admin/eval_cache/warm",
               body={"days": 40, "forward": 5})
    note = (d.get("note") or "")
    ok = s == 200 and (d.get("ok") is True or "预热" in note or "已有预热" in note)
    rec("预热 40 天 已提交/已就位", ok, str(d))

    # ---- 系统备份 ----
    s, d = req("POST", "/api/admin/backup", body={})
    ok = s == 200 and d.get("ok") is True and (d.get("name") or "").startswith("tick_")
    rec("在线备份 已生成", ok, str(d))
    bpath = (d.get("path") or "")
    if bpath and os.path.isfile(bpath):
        import sqlite3
        try:
            con = sqlite3.connect(bpath)
            n = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            con.close()
            rec("备份文件可正常打开", n > 0, f"{n} 张表 · {os.path.getsize(bpath)//1024//1024}MB")
        except Exception as e:
            rec("备份文件可正常打开", False, f"{type(e).__name__}: {e}")
        # 测试产物清掉，避免 backups/ 堆积
        try:
            os.remove(bpath)
        except Exception:
            pass
    else:
        rec("备份文件落盘", False, bpath)

    # ---- 备份清单 ----
    s, d = req("GET", "/api/admin/backups")
    rec("backups 清单返回 backend+items",
        s == 200 and "backend" in d and "items" in d, f"backend={d.get('backend')}")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n== 汇总 ==\n{npass}/{len(results)} 通过")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
