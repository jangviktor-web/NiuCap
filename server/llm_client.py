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
_KEY_DISABLE_THINKING = "LLM_DISABLE_THINKING"

_DEFAULT_BASE = "https://apihub.agnes-ai.com/v1"
_DEFAULT_MODEL = "agnes-2.5-flash"

_TIMEOUT = 120  # 单次调用超时（秒）；推理模型思考较慢，给足时间
_MAX_BUDGET = 8000  # 单次调用 token 预算上限（含推理思考）


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
            # 关闭推理模型的「思考」步骤：让 Agents-A1 这类思考型模型直接出答案，
            # 避免思考吃掉 max_tokens 预算导致 content 空、接口超时。默认开。
            "disable_thinking": _g(_KEY_DISABLE_THINKING) not in (False, "false", "0", ""),
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

    推理模型兼容：像 Agents-A1 这类「思考型」模型会把推理过程塞进
    message.reasoning_content，而真正的答案在 message.content。思考会吃掉
    max_tokens 预算，预算不够时 content 直接为空（finish_reason="length"）。
    这里做自动放大重试：一旦检测到「content 空 + 有 reasoning + 因长度截断」，
    就把 max_tokens 放大到 4 倍（下限 4000、上限 _MAX_BUDGET）再试，最多 3 次。
    这样上层调用方（stock_analyzer / factor_ai / newbie_ai）无需关心模型是不是
    推理模型、该给多少 token。
    """
    c = _conf(override)
    if not override and not c["enabled"]:
        raise RuntimeError("LLM 未启用（后台「AI 模型」打开「启用 AI 模型」开关）")
    if not c["base_url"] or not c["model"]:
        raise RuntimeError("LLM 未配置：请填 API Base URL 与模型名")

    url = c["base_url"] + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if c["api_key"]:
        headers["Authorization"] = "Bearer " + c["api_key"]

    budget = max_tokens
    last_text = ""
    for _ in range(3):
        body = {
            "model": c["model"],
            "messages": messages,
            "max_tokens": budget,
            "temperature": temperature,
            "stream": False,
        }
        # 关掉推理模型的「思考」：直接出答案，省下思考消耗的 token 与耗时。
        # 这是 Qwen 系推理模型的扩展参数，非推理模型/其他提供方通常忽略。
        if c.get("disable_thinking"):
            body["chat_template_kwargs"] = {"enable_thinking": False}
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
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("LLM 返回结构异常：" + r.text[:300])

        # 拿到真实答案就返回（保留原始文本，含 ```json 代码块，由上层 _parse_json 提取）
        if content.strip():
            return content

        # content 为空：看是不是推理模型把预算烧在思考上
        reason = msg.get("reasoning_content")
        finish = data["choices"][0].get("finish_reason")
        last_text = content
        if reason and finish == "length":
            budget = min(max(budget * 4, 4000), _MAX_BUDGET)
            continue  # 放大预算重试
        # 真没东西可说（非长度截断）→ 返回空串，由上层降级
        return ""
    return last_text


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


def selfcheck() -> int:
    """离线自检：推理模型（空 content + 放大重试）与正常路径，不调真实 LLM。

    用假 requests.post 模拟 API：第一次返回空 content + reasoning_content +
    finish_reason=length（思考模型预算不足），第二次返回正常 content。验证 chat()
    会自动放大预算重试并拿到答案。同时验证：未启用抛错、HTTP 非 200 抛错、空 content
    非长度截断直接返回空串（上层降级）。
    """
    import requests as _req
    fails = 0

    def ok(label, cond):
        print(("  ✅ " if cond else "  ❌ ") + label)
        if not cond:
            nonlocal fails
            fails += 1

    real_post = _req.post

    def _resp(status, payload):
        class _R:
            status_code = status
            def json(self):
                return payload
            @property
            def text(self):
                return str(payload)
        return _R()

    # 1) 推理模型：第 1 次 content 空 + length，第 2 次给答案
    calls = {"n": 0, "budgets": []}

    def fake_reasoning(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        calls["budgets"].append(json["max_tokens"])
        if calls["n"] == 1:
            return _resp(200, {"choices": [{"finish_reason": "length",
                "message": {"role": "assistant", "content": "",
                            "reasoning_content": "Thinking…很长的思考"}}]})
        return _resp(200, {"choices": [{"finish_reason": "stop",
                "message": {"role": "assistant",
                            "content": "```json\n{\"ok\":true}\n```"}}]})

    _req.post = fake_reasoning
    try:
        out = chat([{"role": "user", "content": "x"}], max_tokens=700)
        ok("推理模型空 content→放大重试→拿到答案", out.strip() == "```json\n{\"ok\":true}\n```")
        ok("重试时放大了预算(700→≥4000)", any(b >= 4000 for b in calls["budgets"]))
        ok("最多 1 次重试", calls["n"] == 2)
    finally:
        _req.post = real_post

    # 2) 正常模型：一次拿到 content
    calls2 = {"n": 0}
    def fake_normal(url, json=None, headers=None, timeout=None):
        calls2["n"] += 1
        return _resp(200, {"choices": [{"finish_reason": "stop",
                "message": {"role": "assistant", "content": "hello"}}]})
    _req.post = fake_normal
    try:
        out = chat([{"role": "user", "content": "x"}], max_tokens=800)
        ok("正常模型一次返回 content", out == "hello" and calls2["n"] == 1)
    finally:
        _req.post = real_post

    # 3) 未启用 → 抛 RuntimeError（用 override 会跳过启用校验，故这里改 config.get）
    import config as _cfgmod
    _orig_get = _cfgmod.get
    _cfgmod.get = lambda k, d=None: (False if k == "LLM_ENABLED"
                                     else _orig_get(k) if d is None else _orig_get(k, d))
    try:
        chat([{"role": "user", "content": "x"}])
        ok("未启用抛 RuntimeError", False)
    except RuntimeError:
        ok("未启用抛 RuntimeError", True)
    finally:
        _cfgmod.get = _orig_get

    # 4) HTTP 非 200 → 抛 RuntimeError
    def fake_500(url, json=None, headers=None, timeout=None):
        return _resp(500, {"error": {"message": "boom"}})
    _req.post = fake_500
    try:
        chat([{"role": "user", "content": "x"}], override={"LLM_ENABLED": True})
        ok("HTTP 500 抛 RuntimeError", False)
    except RuntimeError:
        ok("HTTP 500 抛 RuntimeError", True)
    finally:
        _req.post = real_post

    # 5) content 空但非 length 截断 → 返回空串（上层降级），不无限重试
    def fake_empty(url, json=None, headers=None, timeout=None):
        return _resp(200, {"choices": [{"finish_reason": "stop",
                "message": {"role": "assistant", "content": ""}}]})
    _req.post = fake_empty
    try:
        out = chat([{"role": "user", "content": "x"}], max_tokens=800)
        ok("空 content 非长度截断→返回空串", out == "")
    finally:
        _req.post = real_post

    # 6) 关闭思考：请求体应带 chat_template_kwargs.enable_thinking=false
    captured = {}
    def fake_capture(url, json=None, headers=None, timeout=None):
        captured["body"] = json
        return _resp(200, {"choices": [{"finish_reason": "stop",
                "message": {"role": "assistant", "content": "hi"}}]})
    _req.post = fake_capture
    try:
        chat([{"role": "user", "content": "x"}], max_tokens=800)
        kw = (captured.get("body") or {}).get("chat_template_kwargs")
        ok("关闭思考→请求体带 enable_thinking=false",
           isinstance(kw, dict) and kw.get("enable_thinking") is False)
    finally:
        _req.post = real_post

    # 7) 打开思考：请求体不应带 chat_template_kwargs（避免误伤非推理模型）
    captured2 = {}
    def fake_capture2(url, json=None, headers=None, timeout=None):
        captured2["body"] = json
        return _resp(200, {"choices": [{"finish_reason": "stop",
                "message": {"role": "assistant", "content": "hi"}}]})
    _req.post = fake_capture2
    try:
        chat([{"role": "user", "content": "x"}], max_tokens=800,
             override={"LLM_DISABLE_THINKING": False})
        ok("打开思考→请求体不带 chat_template_kwargs",
           "chat_template_kwargs" not in (captured2.get("body") or {}))
    finally:
        _req.post = real_post

    print(f"llm_client selfcheck: {'OK' if fails == 0 else str(fails) + ' 项失败'}")
    return fails


if __name__ == "__main__":
    import pprint
    pprint.pprint(_conf())
    print("ready:", is_ready())
    pprint.pprint(test())
    print("--- selfcheck ---")
    selfcheck()
