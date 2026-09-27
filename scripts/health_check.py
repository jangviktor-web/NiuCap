#!/usr/bin/env python3
"""网站健康巡检 —— 从外部探测线上地址是否真的可用。

设计意图
--------
平台自带的健康检查只看「端口有没有人监听」，看不出页面是否真的正常。
本脚本从**外部**打真实 HTTP 请求，覆盖到平台探针看不到的故障类型：

    ① 首页可访问        GET /            → 200 且 HTML 完整
    ② 接口正常          GET /api/health  → ok: true
    ③ 行情已加载        market_count > 0 → 防止「空壳服务」（进程活着但数据没起来）
    ④ 数值合理          market_count 在合理区间，时间戳不太旧
    ⑤ 响应耗时          超过阈值仅告警不判故障（沙箱冷启动会慢）

防抖
----
单次失败不算故障 —— 网络抖动、沙箱冷启动都会造成偶发超时。
连续 2 次失败才判定为故障，并把结果写入状态文件供页面读取。

关于自动重启
------------
本脚本**故意不做自动重启**。实践中大部分「打不开」的根因是配置错误
（如服务监听端口与平台转发端口不一致），此时重启只会把错配的进程再拉起来，
重启一百次也好不了。自动化的价值在「快速发现 + 明确告警」，
修复动作应当由人确认后执行。

用法
----
    # 默认探测线上地址，输出人类可读报告
    python3 scripts/health_check.py

    # 指定地址
    python3 scripts/health_check.py --url https://xxx.app.workbuddy.host

    # 机器可读（供计划任务/监控消费）
    python3 scripts/health_check.py --json

    # 连续监测模式（每 60 分钟一次，Ctrl+C 结束）
    python3 scripts/health_check.py --loop --interval 60

退出码：0 = 健康，1 = 故障，2 = 参数错误

依赖：仅 Python 标准库（urllib / json / argparse），Windows / macOS / Linux 通用。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------ 默认配置

# 线上地址通过环境变量或 --url 传入，不在代码里内置真实域名。
#   环境变量：TICK_SITE_URL=https://your-site.example.com
#   命令行  ：python3 scripts/health_check.py --url https://your-site.example.com
# 未配置时脚本会明确报错退出（退出码 2），而不是去探测一个写死的地址。
DEFAULT_URL = os.environ.get("TICK_SITE_URL", "")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
STATE_DIR = os.path.join(_ROOT, "data")
STATE_PATH = os.path.join(STATE_DIR, "health_state.json")
LOG_PATH = os.path.join(STATE_DIR, "health_alerts.log")

FAIL_THRESHOLD = 2          # 连续失败几次才判定故障（防抖）
SLOW_SECONDS = 8.0          # 单次请求超过这个耗时记「慢」，但不判故障
TIMEOUT = 25                # 单次请求超时（沙箱冷启动唤醒需要时间）


# ------------------------------------------------------------------ 工具

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _probe(url: str, timeout: int = TIMEOUT) -> Dict[str, Any]:
    """发一次 GET，返回状态码、耗时、正文与错误。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "tick-health-check/1.0",
        "Cache-Control": "no-cache",
    })
    # 证书校验保持开启；某些平台网关证书链不完整时降级重试一次
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8", "replace")
            return {"ok": True, "status": r.status,
                    "elapsed": round(time.time() - t0, 2), "body": body, "error": None}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "elapsed": round(time.time() - t0, 2),
                "body": "", "error": f"HTTP {e.code} {e.reason}"}
    except ssl.SSLError as e:
        try:
            ctx2 = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx2) as r:
                body = r.read().decode("utf-8", "replace")
                return {"ok": True, "status": r.status,
                        "elapsed": round(time.time() - t0, 2), "body": body,
                        "error": None, "tls_warn": str(e)}
        except Exception as e2:
            return {"ok": False, "status": 0, "elapsed": round(time.time() - t0, 2),
                    "body": "", "error": f"TLS 失败: {e2}"}
    except socket.timeout:
        return {"ok": False, "status": 0, "elapsed": round(time.time() - t0, 2),
                "body": "", "error": f"请求超时（{timeout}s 无响应）"}
    except Exception as e:
        return {"ok": False, "status": 0, "elapsed": round(time.time() - t0, 2),
                "body": "", "error": f"{type(e).__name__}: {e}"}


def _check(url: str) -> Dict[str, Any]:
    """完整跑一轮检查，返回各项结果。"""
    checks: List[Dict[str, Any]] = []
    base = url.rstrip("/")

    def add(name: str, passed: bool, detail: str, fatal: bool = True):
        checks.append({"name": name, "pass": passed, "detail": detail,
                       "fatal": fatal})

    # ① 首页
    home = _probe(base + "/")
    html_ok = home["ok"] and home["status"] == 200 and len(home["body"]) > 5000
    add("首页可访问", html_ok,
        f"HTTP {home['status']} · {home['elapsed']}s · {len(home['body'])} 字节"
        + (f" · {home['error']}" if home["error"] else ""))

    if not home["ok"]:
        # 首页都打不开，后续检查没有意义，直接返回
        add("接口健康", False, "跳过（首页不通）")
        add("行情已加载", False, "跳过（首页不通）")
        add("数据合理性", False, "跳过（首页不通）", fatal=False)
    else:
        # ② 健康接口
        hp = _probe(base + "/api/health")
        try:
            hj = json.loads(hp["body"])
        except Exception:
            hj = {}
        api_ok = hp["ok"] and hp["status"] == 200 and hj.get("ok") is True
        add("接口健康", api_ok,
            f"HTTP {hp['status']} · ok={hj.get('ok')}"
            + (f" · {hp['error']}" if hp["error"] else ""))

        # ③ 行情是否已加载
        mc = hj.get("market_count") or 0
        mkt_ok = mc > 0
        add("行情已加载", mkt_ok,
            f"market_count={mc} · 更新时间={hj.get('market_updated') or '—'}"
            + ("（进程活着但行情未就绪）" if not mkt_ok else ""))

        # ④ 数值合理性 + 服务端报错
        issues = []
        if hj.get("error"):
            issues.append(f"服务端 error={hj['error']}")
        if mc and not (100 < mc < 20000):
            issues.append(f"market_count={mc} 异常")
        add("数据合理性", not issues, "、".join(issues) if issues else "无异常",
            fatal=False)

    # ⑤ 响应耗时
    slow = home["elapsed"] > SLOW_SECONDS
    add("响应耗时", not slow,
        f"{home['elapsed']}s（阈值 {SLOW_SECONDS}s）"
        + ("，沙箱冷启动唤醒属正常" if slow else ""), fatal=False)

    failed_fatal = [c for c in checks if c["fatal"] and not c["pass"]]
    return {
        "time": _now(),
        "url": url,
        "healthy": len(failed_fatal) == 0,
        "checks": checks,
        "failed": [c["name"] for c in failed_fatal],
        "elapsed": home["elapsed"],
    }


# ------------------------------------------------------------------ 状态与告警

def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"consecutive_fail": 0, "alerting": False, "history": []}


def _save_state(st: Dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _alert(msg: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    line = f"[{_now()}] {msg}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    print(line.rstrip(), flush=True)


def _record(res: Dict[str, Any]) -> Dict[str, Any]:
    """把本轮结果并入状态文件，处理防抖与告警去重。"""
    st = _load_state()
    if res["healthy"]:
        if st.get("alerting"):
            _alert(f"✅ 已恢复：{res['url']}（此前连续失败 {st.get('consecutive_fail')} 次）")
        st["consecutive_fail"] = 0
        st["alerting"] = False
        st["last_ok"] = res["time"]
    else:
        st["consecutive_fail"] = int(st.get("consecutive_fail", 0)) + 1
        st["last_fail"] = res["time"]
        st["last_error"] = "；".join(
            f"{c['name']}: {c['detail']}" for c in res["checks"] if not c["pass"])
        # 达到阈值才正式告警，且不重复告警（避免刷屏）
        if st["consecutive_fail"] >= FAIL_THRESHOLD and not st.get("alerting"):
            _alert(f"❌ 故障告警：{res['url']} —— {st['last_error']}")
            st["alerting"] = True

    st["last_check"] = res["time"]
    st["last_url"] = res["url"]
    st["last_result"] = res
    # 保留最近 30 条历史，供页面展示
    hist = st.get("history", [])
    hist.insert(0, {"time": res["time"], "healthy": res["healthy"],
                    "failed": res["failed"], "elapsed": res["elapsed"]})
    st["history"] = hist[:30]
    _save_state(st)
    return st


# ------------------------------------------------------------------ 输出

def _print_report(res: Dict[str, Any], st: Dict[str, Any]) -> None:
    print(f"\n  巡检时间  {res['time']}")
    print(f"  目标地址  {res['url']}")
    print(f"  连续失败  {st.get('consecutive_fail', 0)} 次"
          f"（达到 {FAIL_THRESHOLD} 次即告警）")
    print("  " + "─" * 60)
    for c in res["checks"]:
        mark = "✓" if c["pass"] else ("✗" if c["fatal"] else "!")
        tag = "" if c["fatal"] else "（仅提示）"
        print(f"   {mark} {c['name']}{tag}  {c['detail']}")
    print("  " + "─" * 60)
    if res["healthy"]:
        print("  结论：正常")
    elif st.get("alerting"):
        print(f"  结论：故障已告警 —— {st.get('last_error', '')}")
    else:
        print(f"  结论：本轮异常（首次，待下次复检确认）")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="牛来选股面板健康巡检",
        epilog="地址可用 --url 传入，或设环境变量 TICK_SITE_URL。")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="要探测的地址（也可用环境变量 TICK_SITE_URL）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--loop", action="store_true", help="持续监测")
    ap.add_argument("--interval", type=float, default=60,
                    help="持续模式的间隔（分钟），默认 60")
    ap.add_argument("--no-state", action="store_true",
                    help="不写状态文件（只做一次性探测）")
    a = ap.parse_args()

    if not a.url:
        print("未指定要探测的地址。请用 --url 传入，或设置环境变量 TICK_SITE_URL：\n"
              "  python3 scripts/health_check.py --url https://your-site.example.com\n"
              "  TICK_SITE_URL=https://your-site.example.com python3 scripts/health_check.py",
              file=sys.stderr)
        return 2

    if not a.url.startswith(("http://", "https://")):
        print(f"地址必须以 http:// 或 https:// 开头：{a.url}", file=sys.stderr)
        return 2

    def once() -> int:
        res = _check(a.url)
        if a.no_state:
            st: Dict[str, Any] = {}
        else:
            st = _record(res)
        if a.json:
            print(json.dumps({"result": res, "state": {
                k: st.get(k) for k in ("consecutive_fail", "alerting")}},
                ensure_ascii=False, indent=2))
        else:
            _print_report(res, st)
        # 只有正式告警（达到阈值）才返回失败码，避免抖动误报
        if res["healthy"]:
            return 0
        return 1 if st.get("alerting") else 0

    if not a.loop:
        return once()

    print(f"持续监测已启动：每 {a.interval:g} 分钟探测一次，Ctrl+C 结束")
    print(f"日志：{LOG_PATH}")
    try:
        while True:
            once()
            time.sleep(max(1.0, a.interval * 60))
    except KeyboardInterrupt:
        print("\n已停止监测")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
