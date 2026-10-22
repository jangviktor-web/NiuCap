"""因子实验室 AI 解读（#factor-ai）：把 IC/ICIR 检验结果翻译成小白能懂的大白话。

与 newbie_ai（#224）同一范式：LLM 只增强解释性字段，输出过 schema 护栏；
LLM 不可用/输出不过 → 返回 None，由调用方降级（页面脚注常驻，不报错）。

两个关键设计，别改：

1. **只喂前 N 个因子，不让 LLM 挑。**
   后端先按 |ICIR| 降序取前 top_n 个，把这几个的名字写进 prompt，validate 时
   逐个校验返回的 name 必须在这份名单里。少了这道，LLM 会自己挑因子、甚至
   编一个不存在的名字——而用户看不出区别。

2. **所有数字必须来自传入的检验结果。**
   prompt 里带具体数值，system 明确禁止编造。LLM 的活是「翻译 + 解释怎么用」，
   不是「算数」。检验结果是确定性算出来的，不能反过来被 LLM 改写。

输入是 factor_lab.evaluate 的返回值，后端不重算。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOP_N = 5            # 只解读最值得讲的几个：全 12 个都讲，小白一个都记不住
WHAT_MAX = 90        # 「这个因子在算什么」上限
USE_MAX = 90         # 「能怎么用」上限
SUM_MAX = 240        # 总览上限
CAV_MAX = 70         # 注意点上限

_SYS = """你是A股量化小白助手。给你一份「因子实验室」的 IC/ICIR 检验结果，
请把统计结论翻译成大白话，告诉小白这些因子算出来能怎么用。仅输出 JSON：
{"summary":"≤240字总览","factors":[{"name":"因子名","what":"≤90字","use":"≤90字"}],"caveats":["≤70字"]}

规则（违反会被代码拒绝、白费 Token）：
- **显著性一律以「统计显著性」字段为准**（它按 q 值判定，q 已做多重检验校正）。
  字段写「不显著」时，任何地方都不许出现「显著」二字——哪怕 t 值看着很大：
  t 值只是单次检验的数字，多个因子一起检验时靠它会把假阳性当成真发现；
- summary 第一句必须与【整体结论】的通过数一致：通过数为 0 时，第一句就要说清
  「本批没有一个因子通过判定」，不许先说「某因子显著/有效」再补「但也未通过」；
- summary 其余部分说最值得关注的一两个因子，引用给定数字；≤240 字；
- factors：对给定的每一个因子逐个输出，name 必须与给定完全一致（不许自己增删改），
  what 用一句大白话说明这个因子在算什么、算出来的数说明什么（引用 ICIR/方向），
  use 说它能怎么用、用在哪里（如「可作轮动池的排序依据」），
  注意：判定未通过的因子，use 要说明「只适合观察、暂不宜当决策依据」；
- 分层收益是各档的**算术平均**。A股 5 日收益被涨跌停截断，右尾远长于左尾
  （实测中证500 近 750 日：中位数 0.00%、均值 +0.62%、最大 +136%），所以
  **五档可能全为正，而因子其实无效甚至反向**。判断方向一律以 IC/ICIR 符号为准，
  分层只用来观察单调性，不要拿「五档收益递增/全为正」当因子有效的证据；
- caveats 列 1~3 条使用前的注意点，必须基于给定数据
  （如「本结论是中证500 近 750 日的历史统计，市况变了可能失效」
    「分层不单调说明预测力不稳定」）；
- 只能基于给定数据，禁止编造数据里没有的数字或因子；
- 这是统计方法解读，不是投资建议，不要承诺收益。"""


def pick_top(res: Dict[str, Any], n: int = TOP_N
             ) -> List[Dict[str, Any]]:
    """按 |ICIR| 降序取前 n 个「讲得动」的因子。

    讲得动 = 有 ICIR 且有分层数据。没有 ICIR 的（样本不足，ic_summary 会给
    obs<10 的因子 icir=None）讲不出东西，硬讲就是让 LLM 编。
    """
    items = [it for it in (res.get("items") or [])
             if it.get("icir") is not None]
    items.sort(key=lambda x: abs(x["icir"]), reverse=True)
    return items[:max(1, n)]


def _f(v: Any, fmt: str = "{}", dash: str = "暂无") -> str:
    return dash if v is None else fmt.format(v)


def _prompt(res: Dict[str, Any], top: Sequence[Dict[str, Any]]) -> str:
    span = res.get("span") or ["", ""]
    allit = res.get("items") or []
    npass = sum(1 for it in allit if it.get("pass"))
    L: List[str] = [
        f"股票池: {res.get('pool') or '暂无'}（{res.get('universe') or 0} 只）",
        f"区间: {span[0]}~{span[1]}（{res.get('days') or 0} 个交易日）",
        f"收益窗口: {res.get('forward') or 0} 日（信号次日收盘买入，看之后若干天的收益）",
        f"判定门槛: |ICIR| ≥ {res.get('icir_pass')} 且 q ≤ {res.get('q_pass')}"
        "（q 是对同时检验多个因子做的多重检验校正）",
        # 总数与通过数由后端数好直接给。原先只给逐个「判定: 未通过」，让模型自己
        # 统计「本批到底有没有因子能用」——它数对了是运气，数错了就是整段结论错。
        f"【整体结论】本批共检验 {len(allit)} 个因子，其中通过判定的有 {npass} 个"
        + ("（即一个都没有，这批因子当前都不具备可用于排序的预测力）" if npass == 0
           else f"（通过的是: {'、'.join(it.get('name') or '' for it in allit if it.get('pass'))}）"),
        "",
        # 这句原先写反了：在「全都没通过」时说的是「都通过判定」，等于把错误结论
        # 写进 prompt 让模型照抄。四个状态分开写，不留一句靠布尔取反撑着的句子。
        f"以下 {len(top)} 个因子按 |ICIR| 从高到低排列（|ICIR| 绝对值最大的几个），"
        + ({"none": "它们都没通过判定", "all": "它们都通过了判定",
            "some": "其中部分通过、部分未通过"}[
               "all" if all(it.get("pass") for it in top)
               else "none" if not any(it.get("pass") for it in top)
               else "some"] + "，请逐个解读："),
    ]
    for i, it in enumerate(top, 1):
        ir = it.get("icir")
        lay = it.get("layers") or []
        lay_s = " ".join(_f(x, "{:+.2f}%", "n/a") if x is not None else "n/a"
                         for x in (x * 100 if x is not None else None for x in lay))
        # 显著性只给一个词，且依据 q 值（已做 BH 校正）。同时点明「t 值过了但
        # q 没过」这种情况，否则模型看到 |t|>1.96 自己会补一句「统计显著」，
        # 和紧跟着的「未通过」直接打架。
        sig = "显著" if it.get("significant") else "不显著"
        ts = it.get("t_stat")
        sig_note = ""
        if ts is not None and abs(ts) >= 1.96 and not it.get("significant"):
            sig_note = "（单次检验的 t 值虽超 1.96，但多重检验校正后 q 未达标）"
        L += [
            "",
            f"[{i}] {it.get('name')}（类别: {it.get('cat')}）",
            f"  - IC均值 {_f(it.get('ic_mean'), '{:+.4f}')}，"
            f"ICIR {_f(ir, '{:+.3f}')}，"
            f"IC>0 占比 {_f(it.get('pos_ratio'), '{:.0%}')}，"
            f"t 值 {_f(ts, '{:+.2f}')}，"
            f"q 值 {_f(it.get('q_stat'), '{:.4f}')}",
            f"  - 统计显著性: {sig}{sig_note}",
            f"  - 方向: {it.get('direction') or '暂无'}",
            f"  - 五档分层收益 Q1→Q5: {lay_s}（Q1=因子最小，Q5=因子最大）",
            f"  - 分层单调: {'是' if it.get('monotonic') else '否'}",
            f"  - 有效率: {_f(it.get('cover'), '{:.0%}')}"
            "（因子值算得出来的格子占比）",
            f"  - 判定: {'通过' if it.get('pass') else '未通过'}",
        ]
    return "\n".join(L)


def _parse_json(text: str) -> Optional[dict]:
    """从 LLM 输出里抠 JSON（容忍 ```json 包裹与前后废话）。坏 JSON → None。"""
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _validate(obj: Dict[str, Any], names: Sequence[str]) -> Dict[str, Any]:
    """schema 护栏：逐字段校验，任一硬伤整体不用（宁可降级也不放坏数据）。

    names 是「喂给 LLM 的因子名名单」——返回的 name 不在这份名单里，
    说明 LLM 自己编了因子，整份丢弃。这是本模块最重要的一条护栏。
    """
    # 不写成闭包读 obj 的 _s(key)：factors 是嵌套结构，what/use 在子 dict 里，
    # 平铺取 key 会永远拿到 None（这个 bug 自检第一次跑就抓到了）。
    def _str(v: Any, maxlen: int) -> Optional[str]:
        if not isinstance(v, str) or not v.strip():
            return None
        v = v.strip()
        return v if len(v) <= maxlen else None

    out: Dict[str, Any] = {}
    s = _str(obj.get("summary"), SUM_MAX)
    if not s:
        return {}
    out["summary"] = s

    fs = obj.get("factors")
    if not isinstance(fs, list) or not (1 <= len(fs) <= len(names)):
        return {}
    good: List[Dict[str, str]] = []
    for x in fs:
        if not isinstance(x, dict):
            return {}
        nm = x.get("name")
        if not isinstance(nm, str) or nm not in names:
            return {}                      # 编名字 → 整份作废
        w, u = _str(x.get("what"), WHAT_MAX), _str(x.get("use"), USE_MAX)
        if not w or not u:
            return {}
        good.append({"name": nm, "what": w, "use": u})
    out["factors"] = good
    # 刻意不强制「必须覆盖所有给定因子」：漏一个只少一段解读，而强制全覆盖
    # 会让 LLM 偶尔漏输出就整体降级，用户经常看不到解读，功能形同虚设。
    # 把实际覆盖数带回前端，让 UI 能诚实显示「已解读 N/M 个」。
    out["covered"] = len(good)
    out["total"] = len(names)

    cv = obj.get("caveats")
    if not isinstance(cv, list) or not (1 <= len(cv) <= 3):
        return {}
    cs = []
    for x in cv:
        if not isinstance(x, str) or not x.strip() or len(x.strip()) > CAV_MAX:
            return {}
        cs.append(x.strip())
    out["caveats"] = cs
    return out


class OutputRejected(Exception):
    """LLM 回复了，但格式不合规（坏 JSON / 字段超长 / 编了不存在的因子）。

    与「模型不可用」必须分开报：前者重试一次可能就好，后者要用户去改配置。
    端点靠这个异常给出准确提示，否则前端只会笼统显示「AI 模型暂不可用」，
    用户会去检查明明正常的配置，而实际上只是这次输出没守格式。
    """

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def explain(res: Dict[str, Any], top_n: int = TOP_N,
            override: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """调 LLM 产解读 JSON。网络/限流异常上抛；输出不合规抛 OutputRejected。"""
    if not res or not res.get("items"):
        return None
    top = pick_top(res, top_n)
    if not top:
        return None
    import llm_client
    raw = llm_client.chat([{"role": "system", "content": _SYS},
                           {"role": "user", "content": _prompt(res, top)}],
                          max_tokens=1200, temperature=0.4, override=override)
    obj = _parse_json(raw)
    if not obj:
        raise OutputRejected(f"回复不是合法 JSON（{len(raw or '')} 字）")
    v = _validate(obj, [t.get("name") or "" for t in top])
    if not v:
        raise OutputRejected("字段不合规（超长 / 因子名对不上 / 缺 caveats）")
    return v


# ---------------------------------------------------------------------------
# 解读结果缓存
# ---------------------------------------------------------------------------

_EXPLAIN_CACHE: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
# 与 factor_lab.evaluate_cached 同一个 TTL：解读绑定在一份检验结果上，
# 结果 30 分钟换一次，解读跟着换即可。
_EXPLAIN_TTL = 1800.0


def cache_peek(key: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    """只看缓存、不调 LLM。命中返回解读，未命中/过期返回 None。

    端点用来在**扣配额之前**判断这次请求能否零成本复用：命中就直接返回，
    不扣次数、不花 Token。与 explain_cached 共用同一份 _EXPLAIN_CACHE。
    """
    hit = _EXPLAIN_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _EXPLAIN_TTL:
        return hit[1]
    return None


def explain_cached(key: Tuple[Any, ...], res: Dict[str, Any]
                   ) -> Tuple[Optional[dict], bool]:
    """带 TTL 缓存的 explain。返回 (结果 or None, 是否命中缓存)。

    刻意**只缓存成功结果**：LLM 临时故障（超时/限流）时若把 None 也缓存，
    用户接下来半小时都看不到解读，且无从得知只是暂时的网络问题。
    """
    hit = cache_peek(key)
    if hit is not None:
        return hit, True
    out = explain(res)
    if out:
        _EXPLAIN_CACHE[key] = (time.time(), out)
    return out, False


def selfcheck() -> int:
    """离线自检：prompt 渲染 + schema 正/负例 + 降级路径（假 chat，不调 LLM）。"""
    fails = 0

    def ok(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}{(' -> ' + str(extra)) if extra else ''}")
        if not cond:
            fails += 1

    res = {
        "pool": "中证500", "universe": 499, "span": ["2023-09-07", "2026-10-19"],
        "days": 750, "forward": 5, "icir_pass": 0.3, "q_pass": 0.1,
        "items": [
            {"name": "20日动量", "cat": "动量", "ic_mean": -0.049, "icir": -0.251,
             "pos_ratio": 0.42, "t_stat": -5.56, "q_stat": 0.00003,
             "significant": True,
             "direction": "反向(因子越小收益越高)",
             "layers": [0.0045, 0.0037, 0.0037, 0.0033, 0.0044],
             "monotonic": False, "cover": 0.658, "pass": False},
            # 真实案例：沪深300 的 60日波动率，|t|=2.58 过了 1.96，但 q=0.12 不过门槛。
            # 模型曾据此写出「统计显著」，与紧跟的「未通过」自相矛盾。
            {"name": "60日波动率", "cat": "波动", "ic_mean": -0.0002, "icir": -0.098,
             "pos_ratio": 0.51, "t_stat": -2.58, "q_stat": 0.1201,
             "significant": False,
             "direction": "反向(因子越小收益越高)",
             "layers": [-0.001, 0.0, 0.001, 0.002, 0.003],
             "monotonic": True, "cover": 0.9, "pass": False},
            # 没有 icir 的因子（样本不足）不该被选中去讲
            {"name": "60日最大回撤", "cat": "低波", "ic_mean": None, "icir": None,
             "pos_ratio": None, "t_stat": None, "q_stat": None,
             "significant": False,
             "direction": "样本不足", "layers": [], "monotonic": False,
             "cover": 0.5, "pass": False},
        ],
    }

    top = pick_top(res)
    ok("pick_top：只选有 ICIR 的因子，按 |ICIR| 降序",
       [t["name"] for t in top] == ["20日动量", "60日波动率"],
       [t["name"] for t in top])
    ok("pick_top：top_n 上限生效", len(pick_top(res, 1)) == 1)
    ok("pick_top：空结果不炸", pick_top({"items": []}) == [])

    p = _prompt(res, top)
    ok("_prompt：含池/区间/门槛/因子明细",
       "中证500" in p and "750 个交易日" in p and "20日动量" in p
       and "-0.251" in p and "反向" in p and "未通过" in p)
    ok("_prompt：分层收益转成百分数", "+0.45%" in p)
    ok("_prompt：空输入不抛异常", "暂无" in _prompt({"items": []}, []))
    # 整体通过数由后端数好写进 prompt，模型不必自己统计（数错了整段结论就错）
    ok("_prompt：给出整体结论与通过数", "【整体结论】" in p and "通过判定的有 0 个" in p)
    ok("_prompt：全部未通过时点明「一个都没有」", "一个都没有" in p)
    res_pass = dict(res)
    it1 = dict(res["items"][1])
    it1["pass"] = True
    res_pass["items"] = [it1] + res["items"][:1] + res["items"][2:]
    ok("_prompt：通过数非 0 时点名通过的因子", "（通过的是: 60日波动率）" in _prompt(res_pass, ()))
    # 「显著」只读后端算好的布尔值，不靠模型自己看 t 值
    ok("_prompt：q 未达标时标「不显著」并解释 t 值为何会骗人",
       "统计显著性: 不显著" in p and "q 未达标" in p)
    ok("_prompt：q 达标时标「显著」", "统计显著性: 显著" in p)
    ok("_SYS：禁止在 q 未达标时写「显著」", "不许出现「显著」二字" in _SYS)

    names = [t["name"] for t in top]
    good = {"summary": "这批因子整体预测力一般：ICIR 最高 -0.251，未达 0.3 门槛。",
            "factors": [{"name": "20日动量", "what": "看过去20天涨跌幅，ICIR负说明跌得多的反而涨得好",
                         "use": "可作观察项，判定未过暂不宜当决策依据"}],
            "caveats": ["这是近750日历史统计，市况变了可能失效"]}
    v = _validate(good, names)
    ok("_validate：合法解读提取全部字段",
       v.get("summary") and len(v.get("factors", [])) == 1 and v["caveats"])
    ok("_validate：name 不在名单 → 整份作废（防 LLM 编因子）",
       _validate(dict(good, factors=[{"name": "不存在的因子", "what": "x", "use": "y"}]), names) == {})
    ok("_validate：漏掉给定因子之一仍可用（只少一段解读，不整体降级）",
       _validate(good, ["20日动量", "5日反转"]).get("covered") == 1)
    ok("_validate：covered/total 反映真实覆盖数",
       _validate(good, names).get("covered") == 1
       and _validate(good, names).get("total") == 2)
    ok("_validate：summary 超长丢弃", "summary" not in _validate(
        dict(good, summary="x" * 300), names))
    ok("_validate：factors 超数量上限丢弃",
       "factors" not in _validate(dict(good, factors=[
           {"name": n, "what": "a", "use": "b"} for n in names + ["额外"]]), names))
    ok("_validate：what 超长丢弃",
       "factors" not in _validate(dict(good, factors=[
           {"name": "20日动量", "what": "x" * 200, "use": "y"}]), names))
    ok("_validate：caveats 空列表丢弃",
       "caveats" not in _validate(dict(good, caveats=[]), names))
    ok("_validate：caveats 超 3 条丢弃",
       "caveats" not in _validate(dict(good, caveats=["a"] * 4), names))
    ok("_validate：缺 caveats → 无采用", _validate(
        {"summary": "ok", "factors": good["factors"]}, names) == {})
    ok("_parse_json：坏 JSON → None", _parse_json("```json\n{bad}\n```") is None)
    ok("_parse_json：非 dict JSON → None", _parse_json('[1,2]') is None)

    import llm_client
    _orig = llm_client.chat
    try:
        def _boom(*a, **k):
            raise RuntimeError("boom")
        llm_client.chat = _boom
        ok("explain：LLM 异常上抛（端点转降级）", _raises(lambda: explain(res)))
        llm_client.chat = lambda *a, **k: "解读\n```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
        r = explain(res)
        ok("explain：LLM 正常 → 通过校验", bool(r) and r["factors"][0]["name"] == "20日动量")
        ok("explain：带回覆盖数 covered/total", r.get("covered") == 1 and r.get("total") == 2)
        llm_client.chat = lambda *a, **k: '```json\n{"foo":"bar"}\n```'
        # 输出不合规抛 OutputRejected，而不是静默返回 None：端点要据此把
        # 「格式没守住」和「模型不可用」分开提示，否则用户会去查正常的配置。
        ok("explain：schema 不过 → OutputRejected（可辨识，供端点分类提示）",
           _raises(lambda: explain(res), OutputRejected))
        ok("explain：坏 JSON → OutputRejected", _raises(
            lambda: (_set_chat(llm_client, "不是JSON"), explain(res))[1],
            OutputRejected))
        _e = _exc(lambda: explain(res))       # 此刻 chat 仍返回 {"foo":"bar"}
        ok("OutputRejected：带上具体原因（端点要拼进用户提示）",
           isinstance(_e, OutputRejected) and bool(_e.why), getattr(_e, "why", None))
        llm_client.chat = lambda *a, **k: "ok"
        ok("explain：空结果 → None", explain({}) is None)
    finally:
        llm_client.chat = _orig

    # 缓存：命中不重算、失败不进缓存（否则临时故障会冻结半小时）
    _EXPLAIN_CACHE.clear()
    llm_client.chat = lambda *a, **k: "解读\n```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
    ok("cache_peek：空缓存返回 None", cache_peek(("k",)) is None)
    r1, c1 = explain_cached(("k",), res)
    ok("cache_peek：写入后可读到同一份结果", cache_peek(("k",)) == r1)
    ok("cache_peek：不匹配的 key 读不到", cache_peek(("other",)) is None)
    r2, c2 = explain_cached(("k",), res)
    ok("explain_cached：首次未命中、二次命中", r1 and not c1 and r2 and c2)
    ok("explain_cached：命中返回同一份结果", r1 == r2)
    llm_client.chat = _boom
    _EXPLAIN_CACHE.clear()
    # explain_cached 刻意不吞异常（端点要拿错误消息提示用户），这里包一层验证
    # 「失败不进缓存」：临时故障不该把接下来半小时都冻住。
    try:
        r3, c3 = explain_cached(("k2",), res)
        frozen = (r3 is None and not c3)
    except Exception:
        frozen = ("k2" not in _EXPLAIN_CACHE)
    ok("explain_cached：失败不进缓存（下次仍会重试）", frozen)
    llm_client.chat = _orig
    print(f"factor_ai selfcheck: {'OK' if fails == 0 else str(fails) + ' 失败'}")
    return fails


def _raises(fn, exc: Optional[type] = None) -> bool:
    """fn 是否抛异常；给了 exc 就只认这一类（别的异常照旧算失败）。"""
    try:
        fn()
        return False
    except Exception as e:
        return isinstance(e, exc) if exc else True


def _exc(fn):
    """跑 fn，返回它抛的异常对象（没抛返回 None）。自检要看异常里的原因用。"""
    try:
        fn()
        return None
    except Exception as e:
        return e


def _set_chat(mod, text: str) -> None:
    """把 llm_client.chat 换成固定回复（自检用，不碰网络）。"""
    mod.chat = lambda *a, **k: text


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
