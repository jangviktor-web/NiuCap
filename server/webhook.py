"""外部推送适配器：飞书 / 企业微信 / 通用 Webhook。

来源：蒸馏报告 8.4（shy3130/tick-stock-panel 的 webhook_adapter.py，MIT）。
签名口径照搬对方源码，不自行发明——三方平台对签名格式零容错。

铁律：**推送失败静默降级，绝不阻断告警主流程**（落盘优先）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 8
_RETRY = 3
_RETRY_SLEEPS = (0.5, 1.0, 2.0)   # 瞬时 5xx 退避；不重试会被冷却窗口压掉

FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
WECOM_LIMIT_BYTES = 4096          # 企微 markdown 按字节截断（中文 3 字节）


def _post(url: str, payload: bytes, headers: dict) -> tuple[bool, str]:
    """POST 一次；瞬时失败（5xx / 网络异常）退避重试。返回 (ok, 说明)。"""
    last = ""
    for i in range(_RETRY):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                r.read()
            return True, "ok"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:      # 4xx 是配置错误（key 错/参数错），重试无意义
                return False, last
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if i < len(_RETRY_SLEEPS):
            time.sleep(_RETRY_SLEEPS[i])
    return False, last


def _truncate_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节截断，不切坏字符。"""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:limit].decode("utf-8", errors="ignore")


# ───────────────────────── 飞书 ─────────────────────────
def feishu_sign(timestamp: str, secret: str) -> str:
    """飞书签名：HmacSHA256(key="{ts}\n{secret}", msg=b"") → Base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu(url: str, secret: str, title: str, body: str) -> tuple[bool, str]:
    ts = str(int(time.time()))
    payload: dict = {
        "msg_type": "text",
        "content": {"text": f"{title}\n{body}"},
    }
    if secret:
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(ts, secret)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _post(url or FEISHU_URL, data, {"Content-Type": "application/json"})


# ───────────────────────── 企业微信 ─────────────────────────
def send_wecom(key: str, title: str, body: str) -> tuple[bool, str]:
    if not key:
        return False, "缺少 key"
    content = _truncate_bytes(f"**{title}**\n{body}", WECOM_LIMIT_BYTES)
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _post(WECOM_URL + key, data, {"Content-Type": "application/json"})


# ───────────────────────── 通用第三方 ─────────────────────────
def generic_sign(body: bytes, timestamp: str, secret: str) -> str:
    """通用签名：HMAC-SHA256(secret, 原始 body) 的 hex，头里前缀 sha256=。"""
    digest = hmac.new(secret.encode("utf-8"),
                      timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def send_generic(url: str, secret: str, title: str, body: str,
                 event: str = "alert", data: dict | None = None) -> tuple[bool, str]:
    if not url:
        return False, "缺少 url"
    ts = str(int(time.time()))
    envelope = {
        "event": event,
        "timestamp": int(ts),
        "title": title,
        "body": body,
        "data": data or {},
    }
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "X-TickFlow-Timestamp": ts}
    if secret:
        headers["X-TickFlow-Signature"] = generic_sign(raw, ts, secret)
    return _post(url, raw, headers)


# ───────────────────────── 统一入口 ─────────────────────────
def push(cfg: dict, title: str, body: str, data: dict | None = None) -> dict:
    """按配置推送；未配置则静默跳过。

    返回 {"sent": int, "failed": int, "detail": [...]}。异常一律不向外抛，
    由调用方记日志——推送失败绝不能阻断告警落盘。
    """
    out = {"sent": 0, "failed": 0, "detail": []}
    if not cfg or not cfg.get("enabled"):
        return out

    if cfg.get("feishu_url"):
        ok, why = send_feishu(cfg["feishu_url"], cfg.get("feishu_secret", ""),
                              title, body)
        out["sent" if ok else "failed"] += 1
        out["detail"].append({"ch": "feishu", "ok": ok, "msg": why})

    if cfg.get("wecom_key"):
        ok, why = send_wecom(cfg["wecom_key"], title, body)
        out["sent" if ok else "failed"] += 1
        out["detail"].append({"ch": "wecom", "ok": ok, "msg": why})

    if cfg.get("generic_url"):
        ok, why = send_generic(cfg["generic_url"], cfg.get("generic_secret", ""),
                               title, body, data=data)
        out["sent" if ok else "failed"] += 1
        out["detail"].append({"ch": "generic", "ok": ok, "msg": why})

    if out["failed"]:
        logger.warning("webhook push failed: %s", out["detail"])
    return out
