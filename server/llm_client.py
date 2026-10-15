"""LLM 客户端（ponytail：最小依赖，复用 venv 自带 requests）。

从 config 读 LLM_* 配置，对 OpenAI 兼容的 /v1/chat/completions 发请求。
Agnes 为默认免费提供方；base_url / key / model 全可在后台「AI 模型」面板改，
切 DeepSeek / 本地 Ollama 只改这两个字段即可，业务代码不动。

设计取舍：
- 不引入 openai SDK：标准 OpenAI 兼容接口用 requests 直发更轻，且避开 SDK 版本差异。
- 不在此做 RPM 限流：面板里的 LLM 调用都是用户点出来的低频请求（< 20/min 免费档足够），
  真要做批量（如舆情全量复核）再在上层加退避。
- 鉴权/连通性失败都包成 RuntimeError，由调用方决定降级（如回落规则逻辑）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests


# ponytail: 配置键集中在此，避免散落到多处。
_KEY_ENABLED = "LLM_ENABLED"
_KEY_BASE = "LLM_BASE_URL"
_KEY_APIKEY = "LLM_API_KEY"
_KEY_MODEL = "LLM_MODEL"

_DEFAULT_BASE = "https://apihub.agnes-ai.com/v1"
_DEFAULT_MODEL = "agnes-2.5-flash"

_TIMEOUT = 60  # 单次调用超时（秒）


def _conf(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读当前 LLM 配置。config 未就绪时回落默认，不抛。

    override: 可选临时配置（如前端未保存就点「测试连接」），非空字段覆盖 config。
    所有字符串字段统一 strip，避免复制粘贴带入首尾空格导致鉴权失败。
    """
    try:
        import config as _cfg
        def _g(k):
            if override and k in override and override[k] not in (None, ""):
                return override[k]
            return _cfg.get(k)
        return {
            "enabled": bool(_g(_KEY_ENABLED)),
            "base_url": (_g(_KEY_BASE) or _DEFAULT_BASE).strip().rstrip("/"),
            "api_key": (_g(_KEY_APIKEY) or "").strip(),
            "model": (_g(_KEY_MODEL) or _DEFAULT_MODEL).strip(),
        }
    except Exception:
        return {"enabled": False, "base_url": _DEFAULT_BASE.rstrip("/"),
                "api_key": "", "model": _DEFAULT_MODEL}


def is_ready() -> bool:
    """是否已启用且填好 base_url + model（不要求 key：本地 Ollama 可无 key）。"""
    c = _conf()
    return bool(c["enabled"] and c["base_url"] and c["model"])


def chat(messages: List[Dict[str, str]], *,
         max_tokens: int = 800, temperature: float = 0.3,
         timeout: int = _TIMEOUT,
         override: Optional[Dict[str, Any]] = None) -> str:
    """发一次对话补全，返回文本。

    messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
    override: 临时配置（测试连接用），提供时跳过「是否启用」校验。
    未启用 / 未配置 / 网络错 / 结构异常都抛 RuntimeError，便于上层 try 降级。
    """
    c = _conf(override)
    if not override and not c["enabled"]:
        raise RuntimeError("LLM 未启用（后台「AI 模型」打开「启用 AI 模型」开关）")
    if not c["base_url"] or not c["model"]:
        raise RuntimeError("LLM 未配置：请填 API Base URL 与模型名")

    url = c["base_url"] + "/chat/completions"
    body = {
        "model": c["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if c["api_key"]:
        headers["Authorization"] = "Bearer " + c["api_key"]

    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        raise RuntimeError(f"LLM 调用超时（>{timeout}s），可能服务端繁忙或网络不通")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"LLM 网络错误：{type(e).__name__}: {e}")

    if r.status_code != 200:
        detail = ""
        try:
            detail = (r.json().get("error", {}) or {}).get("message", "") or r.text
        except Exception:
            detail = r.text
        raise RuntimeError(f"LLM HTTP {r.status_code}：{str(detail)[:300]}")

    try:
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise RuntimeError("LLM 返回结构异常：" + r.text[:300])


def test(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """冒烟测试：用极短 prompt 验证 key / 连通性。

    返回 {"ok": bool, "msg": str}。供后台「测试连接」按钮与自检调用。
    override: 临时配置（前端未保存也能测），且不要求已启用开关。
    """
    c = _conf(override)
    if not (c["base_url"] and c["model"]):
        return {"ok": False, "msg": "未填好 Base URL / 模型名（填好再点测试连接）"}
    try:
        txt = chat([{"role": "user", "content": "ping"}],
                   max_tokens=8, timeout=30, override=override)
    except Exception as e:  # 吞掉具体异常，转成前端可读结果
        return {"ok": False, "msg": str(e)}
    return {"ok": True, "msg": (txt or "").strip()[:80]}


if __name__ == "__main__":
    import pprint
    pprint.pprint(_conf())
    print("ready:", is_ready())
    pprint.pprint(test())
