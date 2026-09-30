"""管理员查看用户数据（自选股 / 历史选股 / 虚拟盘）+ 选股历史按账户隔离。

覆盖：
  1. 未登录访问新管理接口 → 401；非管理员登录 → 403（门禁没漏）
  2. 管理员可读取任意用户的自选分组/条目、历史选股、虚拟盘资金持仓流水
  3. 选股历史按账户隔离：A 的列表里没有 B 的记录；越权看明细 → 404
      管理员例外，可跨账户看
  4. 全量历史 /api/admin/screen_history 带统计；孤儿记录认领 claim 生效

跑法（需先启动服务）：
    ../.venv/bin/python tests/check_admin_inspect.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def req(method, path, body=None, timeout=120, cookie=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", f"tick_sid={cookie}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


# ---------------------------------------------------------------- 准备账号
store.initialize()
_adm = next((u for u in store.list_users()
             if (u.get("username") or "").lower() == "admin"), None)
if not _adm:
    print("FAIL  需要 admin 账号（TICK_ADMIN_USERS 里要有 admin）")
    sys.exit(1)
ADM_TOK = store.create_session(_adm["id"])

_suf = str(int(time.time()))
u_a = store.register(f"_ins_a{_suf}", "pass12345", "用户A")
u_b = store.register(f"_ins_b{_suf}", "pass12345", "用户B")
TOK_A = store.create_session(u_a["id"])
TOK_B = store.create_session(u_b["id"])

# A 的自选：默认分组里放两只（add_item 走请求级用户上下文，这里临时切过去）
_a_folders = store.list_folders(u_a["id"])
store.set_current_user(u_a["id"])
try:
    store.add_item("sh600519", "贵州茅台", folder_id=_a_folders[0]["id"])
    store.add_item("sz000001", "平安银行", folder_id=_a_folders[0]["id"])
finally:
    store.set_current_user(None)

# A / B 各存一条选股历史
h_a = store.save_screen_history("newbie", "A的历史", {"preset": "steady"},
                                [{"code": "sh600519", "name": "贵州茅台"}],
                                user_id=u_a["id"])
h_b = store.save_screen_history("strategy", "B的历史", {"keys": ["ma_bull"]},
                                [{"code": "sz000001", "name": "平安银行"}],
                                user_id=u_b["id"])

# ---------------------------------------------------------------- 1 门禁
for p in (f"/api/admin/users/{u_a['id']}/watch",
          f"/api/admin/users/{u_a['id']}/screen_history",
          f"/api/admin/users/{u_a['id']}/paper",
          "/api/admin/screen_history"):
    st, _ = req("GET", p)
    rec(f"未登录 401 {p.split('/api/admin')[1]}", st == 401, f"status={st}")

for p in (f"/api/admin/users/{u_a['id']}/watch",
          f"/api/admin/users/{u_a['id']}/screen_history",
          f"/api/admin/users/{u_a['id']}/paper",
          "/api/admin/screen_history"):
    st, _ = req("GET", p, cookie=TOK_A)
    rec(f"普通用户 403 {p.split('/api/admin')[1]}", st == 403, f"status={st}")

# ---------------------------------------------------------------- 2 管理员查看
st, d = req("GET", f"/api/admin/users/{u_a['id']}/watch?with_quote=false",
            cookie=ADM_TOK)
ok = (st == 200 and d.get("user", {}).get("id") == u_a["id"]
      and d.get("count", 0) >= 2 and len(d.get("folders", [])) >= 1
      and "我的自选" in d.get("grouped", {}))
rec("管理员看用户A自选股", ok, f"status={st} count={d.get('count')} "
    f"folders={len(d.get('folders', []))}")

st404, _ = req("GET", "/api/admin/users/999999/watch", cookie=ADM_TOK)
rec("管理员看不存在用户 404", st404 == 404, f"status={st404}")

st, d = req("GET", f"/api/admin/users/{u_a['id']}/screen_history",
            cookie=ADM_TOK)
ids = [x["id"] for x in d.get("items", [])]
ok = (st == 200 and h_a in ids and h_b not in ids
      and d.get("stat", {}).get("total", 0) >= 1)
rec("管理员看用户A历史选股", ok, f"status={st} items={len(ids)} "
    f"stat={d.get('stat')}")

st, d = req("GET", f"/api/admin/users/{u_b['id']}/paper", cookie=ADM_TOK)
ok = (st == 200 and "summary" in d and "positions" in d and "trades" in d
      and isinstance(d["summary"].get("cash"), (int, float)))
rec("管理员看用户B虚拟盘", ok, f"status={st} cash={d.get('summary', {}).get('cash')}")

# ---------------------------------------------------------------- 3 隔离
st, d = req("GET", "/api/screen/history", cookie=TOK_A)
ids_a = [x["id"] for x in d.get("items", [])]
rec("A 的历史列表只含自己", st == 200 and h_a in ids_a and h_b not in ids_a,
    f"status={st} n={len(ids_a)}")

st, d = req("GET", "/api/screen/history", cookie=TOK_B)
ids_b = [x["id"] for x in d.get("items", [])]
rec("B 的历史列表只含自己", st == 200 and h_b in ids_b and h_a not in ids_b,
    f"status={st} n={len(ids_b)}")

stA, _ = req("GET", f"/api/screen/history/{h_b}", cookie=TOK_A)
stB, _ = req("GET", f"/api/screen/history/{h_a}", cookie=TOK_B)
rec("越权看他人明细 404", stA == 404 and stB == 404, f"A→{stA} B→{stB}")

stOwn, d = req("GET", f"/api/screen/history/{h_a}", cookie=TOK_A)
rec("本人看自己明细 200", stOwn == 200 and d.get("id") == h_a, f"status={stOwn}")

stAdm, d = req("GET", f"/api/screen/history/{h_b}", cookie=ADM_TOK)
rec("管理员可跨账户看明细", stAdm == 200 and d.get("id") == h_b,
    f"status={stAdm}")

stPerf, _ = req("GET", f"/api/screen/history/{h_b}/performance", cookie=TOK_A)
rec("越权看他人表现 404", stPerf == 404, f"status={stPerf}")

# ---------------------------------------------------------------- 4 全量 + 认领
st, d = req("GET", "/api/admin/screen_history?limit=500", cookie=ADM_TOK)
ok = (st == 200 and h_a in [x["id"] for x in d.get("items", [])]
      and h_b in [x["id"] for x in d.get("items", [])]
      and d.get("stat", {}).get("total", 0) >= 2)
rec("管理员全量历史", ok, f"status={st} total={d.get('stat')}")

st, d = req("GET", f"/api/admin/screen_history?user_id={u_b['id']}",
            cookie=ADM_TOK)
ids_f = [x["id"] for x in d.get("items", [])]
rec("全量历史按 user_id 过滤", st == 200 and h_b in ids_f and h_a not in ids_f,
    f"status={st} n={len(ids_f)}")

# 造一条孤儿记录（user_id=0），再认领给 A
_orphan = store.save_screen_history("screen", "孤儿·待认领", {},
                                    [{"code": "sz000002", "name": "万科A"}],
                                    user_id=0)
st, d = req("POST", "/api/admin/screen_history/claim",
            {"user_id": u_a["id"]}, cookie=ADM_TOK)
rec("孤儿记录认领", st == 200 and d.get("moved", 0) >= 1,
    f"status={st} moved={d.get('moved')}")
_own = store.get_screen_history(_orphan)
rec("认领后归属正确", _own and _own["user_id"] == u_a["id"],
    f"user_id={(_own or {}).get('user_id')}")

st, _ = req("POST", "/api/admin/screen_history/claim", {"user_id": 999999},
            cookie=ADM_TOK)
rec("认领到不存在用户 404", st == 404, f"status={st}")

# ---------------------------------------------------------------- 5 用户列表带选股数
st, d = req("GET", "/api/admin/users", cookie=ADM_TOK)
_a_row = next((x for x in d.get("items", []) if x["id"] == u_a["id"]), None)
rec("用户列表带 screen_cnt",
    st == 200 and _a_row is not None and _a_row.get("screen_cnt", 0) >= 2,
    f"status={st} screen_cnt={(_a_row or {}).get('screen_cnt')}")

# ---------------------------------------------------------------- 6 模块状态纳管
st, d = req("GET", "/api/admin/modules", cookie=ADM_TOK)
_tasks = set((x.get("task") or "") for x in d.get("items", []))
rec("模块状态含 #103/#104/#105",
    st == 200 and {"#103", "#104", "#105"} <= _tasks,
    f"status={st} tasks={sorted(_tasks)}")

# ---------------------------------------------------------------- 清理
try:
    c = store._conn()
    c.execute("DELETE FROM screen_history WHERE user_id IN (?,?)",
              (u_a["id"], u_b["id"]))
    c.commit()
    store.delete_user(u_a["id"])
    store.delete_user(u_b["id"])
except Exception as e:
    print(f"(清理测试账号失败，可忽略: {e})")

print()
ok_n = sum(1 for _, ok, _ in results if ok)
print(f"==== {ok_n}/{len(results)} 通过 ====")
sys.exit(0 if ok_n == len(results) else 1)
