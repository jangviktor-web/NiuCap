"""落库 → 预热体检 的钩子链路测试（不碰真实行情，不写真实库）。

验证 scheduler.run_now() 落库成功后：
  1. history.reload_engine() 被调用（否则内存引擎停在旧数据）
  2. strategy_eval.prewarm() 被调用（默认 120 天）

用 mock 替换真实落库/重载/预热，只验调用链。meta 状态（last_try/last_ok/
last_result）先快照、测试后精确还原——还原错了会导致今晚真实落库被跳过，
所以断言里专门盯这一点。

跑法：
    python3 tests/check_prewarm_hook.py
"""
import sys

sys.path.insert(0, "server")

import scheduler  # noqa: E402

results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    st = scheduler  # 简写
    import store

    # ---- 快照 meta（跑完精确还原，否则今晚真实落库会被跳过）----
    snap = {k: store.meta_get(k, "") for k in
            (st.K_LAST_TRY, st.K_LAST_OK, st.K_LAST_RESULT)}
    rec("meta 状态已快照", all(v for v in snap.values()), str(snap))

    # ---- mock：替换真实动作 ----
    calls = {"reload": 0, "prewarm": []}
    orig_do_sync = st._do_sync
    st._do_sync = lambda scope, count: {"scope": scope, "count": count,
                                        "synced": 5568, "fail": 8,
                                        "bars": 1370018, "total": 5576}
    import history
    orig_reload = history.reload_engine
    history.reload_engine = lambda: calls.__setitem__("reload", calls["reload"] + 1) or history.get_engine()
    import strategy_eval as se
    orig_prewarm = se.prewarm
    se.prewarm = lambda **kw: calls["prewarm"].append(kw) or {"ok": True, "note": "mock"}

    try:
        r = st.run_now(manual=True)
        rec("run_now 走到成功分支", r.get("ok") is True, str({k: r.get(k) for k in ("ok", "ok_", "bars") if k in r}))
        rec("落库后重载了历史引擎", calls["reload"] == 1, f"reload 次数={calls['reload']}")
        rec("触发了体检预热", len(calls["prewarm"]) == 1, f"参数={calls['prewarm']}")
        rec("预热用的是默认 120 天", calls["prewarm"] and calls["prewarm"][0].get("days") is None,
            "run_now 不应指定 days（跟随 prewarm 默认值）")
        rec("last_ok 已记为今天", store.meta_get(st.K_LAST_OK, "") == st._today(),
            store.meta_get(st.K_LAST_OK, ""))
    finally:
        # ---- 还原：mock + meta ----
        st._do_sync = orig_do_sync
        history.reload_engine = orig_reload
        se.prewarm = orig_prewarm
        for k, v in snap.items():
            if v:
                store.meta_set(k, v)
        restored = {k: store.meta_get(k, "") for k in snap}
        rec("meta 已精确还原（今晚真实落库不受影响）", restored == snap, str(restored))

    print()
    print(f"== 汇总 ==")
    print(f"{sum(1 for _, ok in results if ok)}/{len(results)} 通过")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
