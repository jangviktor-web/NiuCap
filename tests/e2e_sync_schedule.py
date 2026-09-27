#!/usr/bin/env python3
"""日线落库每日调度的端到端校验。

分两层：
  · 纯逻辑层（不碰网络/数据库业务数据）：scheduler.selfcheck()
  · HTTP 层：接口鉴权、参数校验、热生效、调度状态字段

跑法：
    python3 tests/e2e_sync_schedule.py            # 只跑不触发实际落库
    python3 tests/e2e_sync_schedule.py --trigger  # 额外触发一次真实落库（慢）
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8899"
ADMIN = "admin"
ok_n = 0
fail: list[str] = []


def check(cond, good, bad):
    global ok_n
    if cond:
        ok_n += 1
        print(f"  ✅ {good}")
    else:
        print(f"  ❌ {bad}")
        fail.append(bad)


def req(path, method="GET", body=None, cookie=""):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw), resp.headers.get("Set-Cookie", "")
            except json.JSONDecodeError:
                return resp.status, raw, resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), ""
        except json.JSONDecodeError:
            return e.code, raw, ""


def login(u, p=None):
    p = p or os.environ.get("ADMIN_PASSWORD") or "test1234"
    sc, _, ck = req("/api/auth/login", "POST", {"username": u, "password": p})
    if sc == 200 and ck:
        return ck.split(";")[0]
    # admin 密码由 ADMIN_PASSWORD 提供时【只登录、绝不重置】——
    # 用户改过的密码不能被一次测试悄悄覆盖掉
    if u == "admin" and os.environ.get("ADMIN_PASSWORD"):
        return ""
    sc, _, ck = req("/api/auth/register", "POST", {"username": u, "password": p})
    if sc in (200, 201):
        return ck.split(";")[0]
    # 已存在但密码不符 → 用 store 重置
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "server"))
        import store as st
        st.initialize()
        for u2 in st.list_users():
            if u2.get("username") == u:
                st.set_password(u2["id"], p)
        sc, _, ck = req("/api/auth/login", "POST", {"username": u, "password": p})
        return ck.split(";")[0] if sc == 200 else ""
    except Exception:
        return ""


def main():
    trigger = "--trigger" in sys.argv

    # ---------- 1. 纯逻辑自检 ----------
    print("[1] scheduler 纯逻辑自检")
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [str(root / ".venv/bin/python"), str(root / "server/scheduler.py")],
        capture_output=True, text=True, timeout=120)
    check(r.returncode == 0, "scheduler.selfcheck() 全部通过",
          f"自检失败:\n{r.stdout[-600:]}{r.stderr[-300:]}")
    # 自检不能污染生产配置
    check("生产配置已原样还原" in r.stdout, "自检还原生产配置",
          "自检未声明还原生产配置")

    # ---------- 2. 鉴权 ----------
    print("\n[2] 接口鉴权")
    ck = login(ADMIN)
    check(bool(ck), "管理员登录", "管理员登录失败")
    sc, d, _ = req("/api/admin/bars/schedule")
    check(sc == 401, f"未登录被拒（{sc}）", f"未登录竟然能访问: {sc}")
    bob = login("bob_sched_test")
    if bob:
        sc, _, _ = req("/api/admin/bars/schedule", cookie=bob)
        check(sc == 403, f"非管理员被拒（{sc}）", f"普通用户能读调度状态: {sc}")

    # ---------- 3. 状态字段 ----------
    print("\n[3] 调度状态字段")
    sc, st, _ = req("/api/admin/bars/schedule", cookie=ck)
    check(sc == 200, "状态接口可读", f"status={sc}")
    for k in ("enabled", "at", "last_ok", "last_try", "today", "today_done", "next", "running"):
        check(k in st, f"含字段 {k}", f"缺字段 {k}")
    check(isinstance(st.get("enabled"), bool), "enabled 是布尔",
          f"enabled 类型异常: {type(st.get('enabled'))}")
    check(bool(st.get("at")), f"执行时间 = {st.get('at')}", "执行时间为空")

    # ---------- 4. 参数校验 ----------
    print("\n[4] 参数校验（非法值必须 400）")
    for val, label in [(25, "25:00"), ("15-30", "15-30"), ("abc", "abc"), ("", "空")]:
        sc, _, _ = req("/api/admin/config/TICK_SYNC_BARS_AT", "PUT", {"value": val}, ck)
        check(sc == 400, f"拒绝时间 {label!r}", f"时间 {label!r} 未被拒: {sc}")
    for val in ("../etc/passwd", "all; drop", ""):
        sc, _, _ = req("/api/admin/config/TICK_SYNC_BARS_SCOPE", "PUT", {"value": val}, ck)
        check(sc == 400, f"拒绝股票池 {val!r}", f"股票池 {val!r} 未被拒: {sc}")
    sc, _, _ = req("/api/admin/config/TICK_SYNC_BARS_COUNT", "PUT", {"value": 1}, ck)
    check(sc == 400, "拒绝根数 1（下限 30）", f"根数 1 未被拒: {sc}")

    # ---------- 5. 热生效 ----------
    print("\n[5] 改完立即生效（不重启）")
    sc, _, _ = req("/api/admin/config/TICK_SYNC_BARS_AT", "PUT", {"value": "17:45"}, ck)
    check(sc == 200, "写入 17:45", f"status={sc}")
    _, st2, _ = req("/api/admin/bars/schedule", cookie=ck)
    check(st2.get("at") == "17:45", f"状态立刻变 17:45（实为 {st2.get('at')}）",
          f"未热生效: at={st2.get('at')}")

    sc, _, _ = req("/api/admin/config/TICK_SYNC_BARS_AUTO", "PUT", {"value": True}, ck)
    check(sc == 200, "开启开关", f"status={sc}")
    _, st3, _ = req("/api/admin/bars/schedule", cookie=ck)
    check(st3.get("enabled") is True, "状态立刻变已开启", "开关未热生效")
    check(st3.get("next") and st3["next"] != "已关闭",
          f"下次触发已计算：{st3.get('next')}", "开启后未计算下次触发")

    # 关掉再开，验证可反复
    req("/api/admin/config/TICK_SYNC_BARS_AUTO", "PUT", {"value": False}, ck)
    _, st4, _ = req("/api/admin/bars/schedule", cookie=ck)
    check(st4.get("enabled") is False and st4.get("next") == "已关闭",
          "关闭后 next=已关闭", f"关闭状态异常: {st4}")

    # ---------- 6. 可选：真实触发 ----------
    if trigger:
        print("\n[6] 真实触发一次（--trigger）")
        req("/api/admin/config/TICK_SYNC_BARS_AUTO", "PUT", {"value": True}, ck)
        sc, d, _ = req("/api/admin/bars/schedule/run", "POST", {}, ck)
        check(sc == 200 and d.get("ok"), f"触发接口 {sc}", f"触发失败: {sc} {d}")
        print("   ⏳ 后台跑落库中，最多等 300s…")
        import time
        for _ in range(60):
            time.sleep(5)
            _, s5, _ = req("/api/admin/bars/schedule", cookie=ck)
            if not s5.get("running") and s5.get("last_ok") == s5.get("today"):
                break
        check(s5.get("last_ok") == s5.get("today"),
              f"落库完成: {s5.get('last_result')}",
              f"未完成: {s5.get('last_result')}")
        # 关键：结果文案里的成功数不该是 0（曾把 fail 当 total 显示成 300/0）
        res = s5.get("last_result") or ""
        check("成功" in res, f"结果含成功数：{res}", f"结果文案异常: {res}")
        n_ok = 0
        try:
            n_ok = int(res.split("成功")[1].split("/")[0].strip())
        except Exception:
            pass
        check(n_ok > 0, f"成功数 {n_ok} > 0", f"成功数显示为 0：{res}")
        # 跑完今天不该再触发
        _, s6, _ = req("/api/admin/bars/schedule", cookie=ck)
        check(s6.get("today_done") is True, "落库后 today_done=true",
              "落库后 today_done 仍为 false，会被重复触发")
    else:
        print("\n[6] 真实触发（跳过，加 --trigger 才会跑）")

    print("\n" + "=" * 56)
    if fail:
        print(f"❌ {len(fail)} 项未通过：")
        for f in fail:
            print(f"   · {f}")
        return 1
    print(f"✅ 全部通过（{ok_n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
