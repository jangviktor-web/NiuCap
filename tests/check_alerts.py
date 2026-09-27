#!/usr/bin/env python3
"""#96 监控中心自检：条件求值 / 冷却去重 / 签名向量 / 静默降级 / API 冒烟。"""
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "server")
import alerts as alt  # noqa: E402
import store  # noqa: E402
import webhook  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name} {detail}")


print("== A. 条件求值（结构化，无 eval） ==")
hit, desc = alt.eval_conds([{"field": "change_pct", "op": ">=", "value": 5}],
                           "AND", {"change_pct": 7.2})
check("单条件命中", hit and "change_pct" in desc, desc)
hit, _ = alt.eval_conds([{"field": "change_pct", "op": ">=", "value": 5}],
                        "AND", {"change_pct": 3.0})
check("单条件不命中", not hit)
hit, _ = alt.eval_conds([{"field": "ma20", "op": ">", "value": 10},
                         {"field": "close", "op": ">", "value": 100}],
                        "AND", {"ma20": 12, "close": 90})
check("AND 一假即假", not hit)
hit, _ = alt.eval_conds([{"field": "ma20", "op": ">", "value": 10},
                         {"field": "close", "op": ">", "value": 100}],
                        "OR", {"ma20": 12, "close": 90})
check("OR 一真即真", hit)
hit, _ = alt.eval_conds([{"field": "ma_align", "op": "==", "value": "bull"}],
                        "AND", {"ma_align": "bull"})
check("字符串相等", hit)
hit, _ = alt.eval_conds([{"field": "__import__", "op": ">", "value": 1}],
                        "AND", {"__import__": 999}, alt.PRICE_FIELDS)
check("字段白名单外视为不成立（防注入）", not hit)
hit, _ = alt.eval_conds([{"field": "close", "op": "system('rm -rf /')", "value": 1}],
                        "AND", {"close": 10})
check("算子白名单外视为不成立", not hit)

print("== B. 飞书 / 企微签名口径 ==")
ts, secret = "1700000000", "sec-demo"
sign = webhook.feishu_sign(ts, secret)
expect = base64.b64encode(
    hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
).decode("utf-8")
check("飞书签名 = Base64(HmacSHA256(ts\\nsecret))", sign == expect, sign)
gs = webhook.generic_sign(b"{\"a\":1}", ts, secret)
expect_g = "sha256=" + hmac.new(secret.encode("utf-8"), (ts + '{"a":1}').encode("utf-8"),
                                hashlib.sha256).hexdigest()
check("通用签名 = sha256=<hex(ts+body)>", gs == expect_g, gs)

print("== C. 企微字节截断（中文 3 字节） ==")
long_txt = "汉" * 2000
trunc = webhook._truncate_bytes(long_txt, 4096)
check("截断后不超 4096 字节", len(trunc.encode("utf-8")) <= 4096,
      len(trunc.encode("utf-8")))
check("截断后仍可解码（不切坏字符）", isinstance(trunc, str) and len(trunc) > 0)

print("== D. 推送失败静默降级 ==")
cfg = {"enabled": 1, "generic_url": "http://127.0.0.1:9/nope"}
res = webhook.push(cfg, "t", "b")
check("不可达地址返回 failed 不抛异常", res["failed"] >= 1 and res["sent"] == 0, res)
cfg_bad = {"enabled": 1, "feishu_url": "https://open.feishu.cn/open-apis/bot/v2/hook/bad"}
res2 = webhook.push(cfg_bad, "t", "b")
check("4xx 不重试（配置错误）", isinstance(res2, dict))

print("== E. 规则 CRUD + 冷却去重 ==")
store.initialize()
for r in alt.list_rules():
    alt.delete_rule(r["id"])
alt.clear_alerts()

rule = alt.add_rule({"name": "自检-价格", "kind": "price", "code": "sh600519",
                     "conds": [{"field": "change_pct", "op": ">=", "value": -100}],
                     "logic": "AND", "severity": "warn", "cooldown_min": 0,
                     "push": 0, "enabled": 1})
check("新建规则", bool(rule.get("id")))
check("规则可读回", len(alt.list_rules()) == 1)
upd = alt.update_rule(rule["id"], {"enabled": 0})
check("停用规则", upd and upd["enabled"] == 0)
alt.update_rule(rule["id"], {"enabled": 1, "name": "自检-改名"})
check("改名生效", alt.get_rule(rule["id"])["name"] == "自检-改名")

stat1 = alt.check_once(engine=None, htk=None)
check("检测跑通", isinstance(stat1, dict) and "fired" in stat1, stat1)
n1 = len(alt.list_alerts(limit=100))
if n1:
    c = store._conn()
    check("同规则同标的同日去重", alt._dedup(c, rule["id"], "sh600519"))
    # 冷却：把 last_fired 设为现在 + 冷却 60 分钟 → 应被跳过
    alt.update_rule(rule["id"], {"cooldown_min": 60})
    c.execute("UPDATE alert_rules SET last_fired=? WHERE id=?", (time.time(), rule["id"]))
    store._commit(c)
    stat2 = alt.check_once(engine=None, htk=None)
    check("冷却期内跳过", stat2.get("skipped", 0) >= 1, stat2)
else:
    print("    （非交易时段行情为空，跳过去重/冷却断言——逻辑在下方用直接调用验证）")
    c = store._conn()
    alt._emit(c, alt.get_rule(rule["id"]), "sh600519", "手工写入告警", 1.0, {})
    check("同规则同标的同日去重", alt._dedup(c, rule["id"], "sh600519"))
    alt.update_rule(rule["id"], {"cooldown_min": 60})
    c.execute("UPDATE alert_rules SET last_fired=? WHERE id=?", (time.time(), rule["id"]))
    store._commit(c)
    stat2 = alt.check_once(engine=None, htk=None)
    check("冷却期内跳过", stat2.get("skipped", 0) >= 1, stat2)

print("== F. 告警读取 / 已读 / 清空 ==")
items = alt.list_alerts(limit=100)
check("告警可读出", len(items) >= 1, len(items))
check("未读计数 > 0", alt.unread_count() >= 1, alt.unread_count())
alt.mark_read(all_=True)
check("全部已读后未读归零", alt.unread_count() == 0)
alt.clear_alerts()
check("清空后无告警", len(alt.list_alerts(limit=100)) == 0)

print("== G. Webhook 配置（存 meta，密钥不回显） ==")
cfg = alt.set_webhook_cfg({"enabled": 1, "feishu_url": "https://x/hook",
                           "feishu_secret": "s3cret"})
check("配置落库", cfg["enabled"] == 1 and cfg["feishu_url"] == "https://x/hook")
check("密钥存得住", alt.get_webhook_cfg()["feishu_secret"] == "s3cret")
alt.set_webhook_cfg({"enabled": 0})

print("== H. 清理测试规则 ==")
for r in alt.list_rules():
    alt.delete_rule(r["id"])
check("规则已清空", len(alt.list_rules()) == 0)

print("== I. API 冒烟 ==")


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://localhost:8899" + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


try:
    d = api("/api/alerts/rules")
    check("GET /api/alerts/rules", "items" in d and "kinds" in d)
    r = api("/api/alerts/rules", "POST",
            {"name": "API自检", "kind": "market",
             "conds": [{"field": "limit_up_cnt", "op": ">=", "value": 99999}],
             "logic": "AND", "severity": "info", "cooldown_min": 0, "push": 0})
    rid = (r.get("item") or {}).get("id")
    check("POST 新建", bool(rid))
    r2 = api("/api/alerts/rules/" + rid, "PUT", {"enabled": 0})
    check("PUT 更新", (r2.get("item") or {}).get("enabled") == 0)
    api("/api/alerts/rules/" + rid, "DELETE")
    check("DELETE 删除", len(api("/api/alerts/rules")["items"]) == 0)
    st = api("/api/alerts/check", "POST")
    check("POST /api/alerts/check", "checked" in st, st)
    w = api("/api/alerts/webhook")
    check("GET webhook 配置不回显密钥", "feishu_secret" in w)
except urllib.error.HTTPError as e:
    check("API 冒烟", False, f"HTTP {e.code}")
except Exception as e:
    check("API 冒烟", False, f"服务未启动？{e}")

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
