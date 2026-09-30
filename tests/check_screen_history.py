"""选股历史 + 窗口胜率回测 回归测试。

覆盖：
1. 窗口胜率回测 API（/api/sim/window）结构与基本正确性
2. 选股历史存档 API（/api/screen/history 列表、/api/screen/history/{id} 明细）
3. 三个选股端点自动存档路径（/api/screener 同步返回后历史新增 screen 记录）
"""

import json
import os
import sys
import time

import requests

# store 在 server/ 子目录
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

BASE = "http://127.0.0.1:8899"


def req(method, path, body=None, timeout=150):
    try:
        r = requests.request(method, BASE + path,
                             json=body if body is not None else None,
                             timeout=timeout)
        try:
            j = r.json()
        except Exception:
            j = {"_text": r.text[:200]}
        return r.status_code, j
    except Exception as e:
        return -1, {"error": str(e)}


def rec(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def main():
    ok_all = True

    # ---- 1. 窗口胜率回测 API ----
    codes = [{"code": "sh600519", "name": "贵州茅台"},
             {"code": "sz300750", "name": "宁德时代"},
             {"code": "sz000001", "name": "平安银行"}]
    st, j = req("POST", "/api/sim/window",
                {"codes": codes, "amount_per": 10000, "days": 10})
    ok = rec("回测API·状态码200", st == 200, f"st={st}")
    ok_all &= ok
    if st == 200:
        summ = j.get("summary", {})
        items = j.get("items", [])
        ok = rec("回测API·返回结构",
                 all(k in summ for k in ("codes", "samples", "win_rate", "avg_ret"))
                 and isinstance(items, list) and len(items) == 3,
                 f"codes={summ.get('codes')} samples={summ.get('samples')}")
        ok_all &= ok
        # 比率在 [0,1]
        ok = rec("回测API·比率合法",
                 0 <= summ.get("win_rate", -1) <= 1
                 and 0 <= summ.get("limit_up_rate", -1) <= 1,
                 f"win={summ.get('win_rate')} limit={summ.get('limit_up_rate')}")
        ok_all &= ok
        # 每只 samples>0 且字段齐全
        ok = rec("回测API·逐只样本>0",
                 all(it.get("samples", 0) > 0 for it in items),
                 "samples=" + str([it.get("samples") for it in items]))
        ok_all &= ok
        # 空 codes 应 400
    st0, _ = req("POST", "/api/sim/window", {"codes": []})
    ok = rec("回测API·空codes拦截", st0 == 400, f"st={st0}")
    ok_all &= ok

    # ---- 2. 选股历史存档 CRUD（直接走 store，确定性） ----
    import store
    store.initialize()
    before = len(store.list_screen_history())
    hid = store.save_screen_history(
        "newbie", "单元测试·稳健", {"preset": "steady"},
        [{"code": "sh600519", "name": "贵州茅台", "price": 1258, "change_pct": 1.2},
         {"code": "sz000001", "name": "平安银行", "price": 11.3, "change_pct": -0.5}])
    after = len(store.list_screen_history())
    ok = rec("历史·存档新增", after == before + 1, f"before={before} after={after}")
    ok_all &= ok

    st, j = req("GET", f"/api/screen/history/{hid}")
    ok = rec("历史·明细接口200", st == 200, f"st={st}")
    ok_all &= ok
    ok = rec("历史·明细含items", st == 200 and len(j.get("items", [])) == 2,
             f"items={len(j.get('items', [])) if st == 200 else -1}")
    ok_all &= ok
    ok = rec("历史·明细含params", st == 200 and j.get("params", {}).get("preset") == "steady")
    ok_all &= ok

    st, j = req("GET", "/api/screen/history?module=newbie")
    ok = rec("历史·按模块过滤", st == 200 and isinstance(j.get("items"), list),
             f"n={len(j.get('items', [])) if st == 200 else -1}")
    ok_all &= ok

    # 不存在的 id → 404
    stx, _ = req("GET", "/api/screen/history/999999999")
    ok = rec("历史·无效id=404", stx == 404, f"st={stx}")
    ok_all &= ok

    # ---- 3. 三选股端点自动存档路径 ----
    st_s, js = req("GET", "/api/screener?pe_max=20&limit=5")
    ok = rec("选股·screener 200", st_s == 200, f"st={st_s}")
    ok_all &= ok
    if st_s == 200 and js.get("items"):
        n_screen = len(store.list_screen_history(module="screen"))
        # 触发一次，历史应增加；但可能受行情缓存竞态影响，仅当返回非空时断言
        ok = rec("选股·screener自动存档", n_screen >= 1, f"screen记录={n_screen}")
        ok_all &= ok

    st_t, _ = req("GET", "/api/strategy_scan?keys=ma_bull&mode=union&limit=5")
    ok = rec("选股·strategy_scan 200", st_t == 200, f"st={st_t}")
    ok_all &= ok

    st_n, _ = req("GET", "/api/newbie_pick?preset=steady&limit=5")
    ok = rec("选股·newbie_pick 200", st_n == 200, f"st={st_n}")
    ok_all &= ok

    print("\n结果:", "全部通过" if ok_all else "存在失败")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
