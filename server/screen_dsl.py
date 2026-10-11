"""条件选股 DSL —— 字段 + 算子驱动的条件表达与求值。

由来
----
``/api/screener`` 原本把每个筛选维度都写成一对 ``_min/_max`` 形参 + 一段
``if ... return False``（旧实现 app.py 684-750，6 个字段 9 段重复判断），
导致每加一个字段都要改签名、改 UI、改历史存档三处。本模块把它收敛成一处：

    FIELDS(可取什么) × OPS(怎么比) → compile(校验) → test(row)(求值)

用法
----
    conds = compile_filters([{"field": "pe", "op": "lte", "value": 20}])
    hits = [r for r in rows if match_row(r, conds)]

边界（明确标出天花板，别到时候当成 bug）
----------------------------------------
· 本期只作用于**快照行情行**，因此**没有** cross_above / cross_below：
  穿越至少要两根 K 线，快照里没有历史。等 Tier 3（条件级多周期）把 bar
  数据接进来后，只需在 OPS 补一条 + 让取值拿到序列，求值框架不用动。

· 数值比较走**严格比较**，故意不用 screener.py 的 ``_gt/_lt/_ge`` 容差。
  那套容差是为「float32 历史序列 vs float64 快照价」精度不一致准备的；
  这里比较双方要么都是 float64 原始精度、要么是用户手填常数，加容差
  反而会把「PE < 15」里合法的 14.99 判掉（正确做法是含进来）。
  ponytail: 天花板在此。**当比较任意一侧是「指标计算结果」时，必须换回
  screener._gt/_lt/_ge**，否则会重演当初「5.10 平前高误判为突破」的 bug。

· 缺失值统一按 0 参与数值比较（``float(r.get(k) or 0)``）。这不是随手写的：
  上游 ``datasource.pool_rows`` 对取不到 PE/PB 的股票本来就填 0
  （见其 docstring「无 PE/PB 时留 0」），0 即是"缺失"的既有编码。
"""

import json
from typing import Any, Callable, Dict, List, NamedTuple, Sequence

# ---------------------------------------------------------------------------
# 字段注册表
# ---------------------------------------------------------------------------


class Field(NamedTuple):
    """一个可筛字段。

    label  中文显示名（也用于自动生成条件摘要）
    kind   num=数值 / text=文本，决定可用算子与取值归一化方式
    unit   单位（展示用，不参与比较）
    cat    分类（展示分组用）
    get    取值函数 row -> value
    """

    label: str
    kind: str
    unit: str
    cat: str
    get: Callable[[Dict[str, Any]], Any]


def _num(key: str) -> Callable[[Dict[str, Any]], float]:
    """数值取值：缺失/None 一律按 0 参与比较（见模块 docstring 边界说明）。"""
    return lambda r: float(r.get(key) or 0)


def _txt(key: str) -> Callable[[Dict[str, Any]], str]:
    return lambda r: str(r.get(key) or "")


FIELDS: Dict[str, Field] = {
    # ---- 价格 / 涨跌 ----
    "price": Field("现价", "num", "元", "行情", _num("price")),
    "change_pct": Field("涨跌幅", "num", "%", "行情", _num("change_pct")),
    "open": Field("今开", "num", "元", "行情", _num("open")),
    "high": Field("最高", "num", "元", "行情", _num("high")),
    "low": Field("最低", "num", "元", "行情", _num("low")),
    "prev_close": Field("昨收", "num", "元", "行情", _num("prev_close")),
    # ---- 估值 ----
    "pe": Field("市盈率", "num", "", "估值", _num("pe")),
    "pb": Field("市净率", "num", "", "估值", _num("pb")),
    "total_cap": Field("总市值", "num", "亿元", "规模", _num("total_cap")),
    "float_cap": Field("流通市值", "num", "亿元", "规模", _num("float_cap")),
    # ---- 成交 ----
    "turnover": Field("换手率", "num", "%", "量能", _num("turnover")),
    "amount": Field("成交额", "num", "万元", "量能", _num("amount")),
    "volume": Field("成交量", "num", "手", "量能", _num("volume")),
    # ---- 标识（文本） ----
    "name": Field("股票名称", "text", "", "标识", _txt("name")),
    "code": Field("代码", "text", "", "标识", _txt("code")),
    # ponytail: market 由 code 前缀派生而非独立字段，避免上游多存一份冗余值
    "market": Field("市场", "text", "", "标识",
                    lambda r: str(r.get("code") or "")[:2]),
}

_NUM_CMP = {
    "gt": (lambda a, b: a > b, ">"),
    "gte": (lambda a, b: a >= b, "≥"),
    "lt": (lambda a, b: a < b, "<"),
    "lte": (lambda a, b: a <= b, "≤"),
    "eq": (lambda a, b: a == b, "="),
    "ne": (lambda a, b: a != b, "≠"),
}

# 算子 → 元数（value 的形态）
OP_ARITY = {
    "gt": 1, "gte": 1, "lt": 1, "lte": 1, "eq": 1, "ne": 1,
    "between": 2, "not_between": 2,
    "in": -1,        # 数组，长度自定
    "match": 1,      # 文本包含
}

OPS = set(OP_ARITY)


# ---------------------------------------------------------------------------
# 编译 / 求值
# ---------------------------------------------------------------------------


class Compiled(NamedTuple):
    """编译后的单条条件。

    test 是编译期就固化好的闭包 —— 求值阶段没有任何查表，
    5000 行 × N 条件也只剩函数调用开销（见 README 性能一节）。
    """

    field: str
    op: str
    desc: str
    test: Callable[[Dict[str, Any]], bool]


def _as_list(v: Any) -> List[Any]:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def compile_condition(spec: Dict[str, Any]) -> Compiled:
    """校验并编译一条条件。

    所有非法输入都在这里以 ValueError 抛出（附带可定位的错误信息），
    由上层转成 HTTP 400。**绝不允许静默忽略未知字段/算子** —— 那会表现为
    "筛过了但其实没筛"，是最难发现的一类失败。

    :raises ValueError: 字段/算子非法、值缺失或元数不对
    """
    if not isinstance(spec, dict):
        raise ValueError(f"条件必须是对象，收到 {type(spec).__name__}")

    key = spec.get("field")
    if key not in FIELDS:
        raise ValueError(f"未知字段 '{key}'，可选：{', '.join(sorted(FIELDS))}")

    op = spec.get("op")
    if op not in OPS:
        raise ValueError(f"未知算子 '{op}'，可选：{', '.join(sorted(OPS))}")

    if "value" not in spec:
        raise ValueError(f"字段 '{key}' 缺少 value")

    f = FIELDS[key]
    arity = OP_ARITY[op]
    raw = spec["value"]

    # ---- 文本包含（只有文本字段可用） ----
    if op == "match":
        if f.kind != "text":
            raise ValueError(f"算子 match 只能用于文本字段，'{key}' 是{f.kind}字段")
        needle = str(raw).strip().lower()
        if not needle:
            raise ValueError(f"字段 '{key}' 的 match 值不能为空")
        getter = f.get
        return Compiled(key, op, f"{f.label} 包含 “{str(raw).strip()}”",
                        lambda r: needle in getter(r).lower())

    # ---- 枚举多选 ----
    if op == "in":
        vals = _as_list(raw)
        if not vals:
            raise ValueError(f"字段 '{key}' 的 in 值不能为空数组")
        if f.kind == "num":
            try:
                nums = [float(v) for v in vals]
            except (TypeError, ValueError):
                raise ValueError(f"字段 '{key}' 是数值字段，in 的值必须是数字列表")
            getter = f.get
            return Compiled(key, op, f"{f.label} ∈ {{{', '.join(str(v) for v in nums)}}}",
                            lambda r: getter(r) in nums)
        strs = [str(v).strip().lower() for v in vals]
        getter = f.get
        return Compiled(key, op, f"{f.label} ∈ {{{', '.join(strs)}}}",
                        lambda r: getter(r).strip().lower() in strs)

    # ---- 区间 ----
    if arity == 2:
        vals = _as_list(raw)
        if len(vals) != 2:
            raise ValueError(
                f"算子 {op} 需要 2 个值 [下限, 上限]，收到 {len(vals)} 个")
        if f.kind == "num":
            try:
                lo, hi = float(vals[0]), float(vals[1])
            except (TypeError, ValueError):
                raise ValueError(f"字段 '{key}' 是数值字段，区间值必须是数字")
            if lo > hi:
                # 上下界颠倒不会报错、只会恒不匹配，比直接失败更坑人
                raise ValueError(
                    f"字段 '{key}' 区间上下界颠倒：[{lo}, {hi}]，应为 [{hi}, {lo}]")
            getter = f.get
            if op == "between":
                return Compiled(key, op, f"{f.label} ∈ [{lo}, {hi}]",
                                lambda r: lo <= getter(r) <= hi)
            return Compiled(key, op, f"{f.label} ∉ [{lo}, {hi}]",
                            lambda r: not (lo <= getter(r) <= hi))
        raise ValueError(f"算子 {op} 暂不支持文本字段 '{key}'")

    # ---- 数值比较 ----
    if f.kind == "num":
        try:
            target = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"字段 '{key}' 是数值字段，value 必须是数字，收到 {raw!r}")
        fn, sym = _NUM_CMP[op]
        getter = f.get
        return Compiled(key, op, f"{f.label} {sym} {target}",
                        lambda r: fn(getter(r), target))

    # ---- 文本相等 / 不等（大小写不敏感，代码与市场符号多为小写统一写入） ----
    target = str(raw).strip().lower()
    getter = f.get
    if op == "eq":
        return Compiled(key, op, f"{f.label} = {target}",
                        lambda r: getter(r).strip().lower() == target)
    if op == "ne":
        return Compiled(key, op, f"{f.label} ≠ {target}",
                        lambda r: getter(r).strip().lower() != target)
    raise ValueError(f"字段 '{key}'({f.kind}) 不支持算子 '{op}'")


def compile_filters(specs: Sequence[Dict[str, Any]]) -> List[Compiled]:
    """编译一组条件（AND 关系）。错误会带上序号，便于前端定位哪一条写错了。"""
    out = []
    for i, spec in enumerate(specs or []):
        try:
            out.append(compile_condition(spec))
        except ValueError as e:
            raise ValueError(f"第 {i + 1} 条条件有问题：{e}")
    return out


def parse_filters(raw: str) -> List[Compiled]:
    """把请求里的 filters JSON 字符串编译成条件列表。

    单独拎出来是因为 JSON 解析错误和"结构合法但内容非法"要分开报，
    前者让调用方去看自己的编码，后者去改业务参数。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"filters 不是合法 JSON（{e.msg}，第 {e.pos} 个字符附近）")
    if not isinstance(specs, list):
        raise ValueError(f"filters 必须是数组，收到 {type(specs).__name__}")
    return compile_filters(specs)


def match_row(row: Dict[str, Any], conds: Sequence[Compiled]) -> bool:
    """一条行情行是否**全部**满足条件（AND）。空条件集视为全部命中。"""
    for c in conds:
        if not c.test(row):
            return False
    return True


def describe(conds: Sequence[Compiled], limit: int = 4) -> str:
    """把条件集渲染成人类可读摘要，用于历史存档标题。"""
    if not conds:
        return ""
    head = [c.desc for c in conds[:limit]]
    tail = "…" if len(conds) > limit else ""
    return "、".join(head) + tail


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def selfcheck() -> int:
    """纯函数、零依赖自检。返回失败条数（0 = 全通过）。"""
    fail = 0

    def ok(name: str, cond: bool, extra: str = ""):
        nonlocal fail
        if cond:
            print(f"  ✅ {name}")
        else:
            fail += 1
            print(f"  ❌ {name} {extra}")

    rows = [
        {"code": "sh600519", "name": "贵州茅台", "price": 1680.0, "change_pct": 2.1,
         "pe": 22.5, "pb": 8.1, "total_cap": 21100.0, "float_cap": 20000.0,
         "turnover": 0.35, "amount": 380000.0, "volume": 22600.0,
         "open": 1650.0, "high": 1690.0, "low": 1645.0, "prev_close": 1645.4},
        {"code": "sz000001", "name": "平安银行", "price": 11.2, "change_pct": -1.4,
         "pe": 5.1, "pb": 0.52, "total_cap": 2170.0, "float_cap": 2169.0,
         "turnover": 0.88, "amount": 96000.0, "volume": 857000.0,
         "open": 11.4, "high": 11.45, "low": 11.1, "prev_close": 11.36},
        # 上游拿不到 PE/PB 时填 0 —— 必须能被 >0 门槛挡掉
        {"code": "bj430047", "name": "诺思兰德", "price": 18.0, "change_pct": 5.6,
         "pe": 0, "pb": 0, "total_cap": 46.0, "float_cap": 30.0,
         "turnover": 3.2, "amount": 12000.0, "volume": 66000.0,
         "open": 17.1, "high": 18.4, "low": 17.0, "prev_close": 17.04},
    ]

    def hit(spec, rows_=rows):
        cs = compile_filters([spec] if isinstance(spec, dict) else spec)
        return [r["name"] for r in rows_ if match_row(r, cs)]

    # ---- 基础数值算子 ----
    # ponytail: 注意这条刻意保留的语义——单独 lte 20 会把 pe=0（上游表示"缺失"）
    # 也算进去，因为 0 ≤ 20 成立。旧 /api/screener 只在"传了 PE 区间"时才顺手
    # 加了 >0 门槛，导致「条件写没写」会改变同一批数据的结果。DSL 不藏这种隐式
    # 规则：要不要排除缺失值，由调用方显式写一条 gt 0（见下一行用例）。
    ok("lte 市盈率≤20 含缺失值", hit({"field": "pe", "op": "lte", "value": 20})
       == ["平安银行", "诺思兰德"])
    ok("补 gt 0 后才挡掉缺失值", hit([{"field": "pe", "op": "gt", "value": 0},
                                     {"field": "pe", "op": "lte", "value": 20}])
       == ["平安银行"])
    ok("not_between 排除区间", hit([{"field": "pe", "op": "gt", "value": 0},
                                    {"field": "pe", "op": "not_between",
                                     "value": [5, 10]}]) == ["贵州茅台"])
    ok("between 命中市值区间", hit({"field": "total_cap", "op": "between",
                                    "value": [1000, 3000]}) == ["平安银行"])
    ok("ne 不等于", hit([{"field": "market", "op": "ne", "value": "sh"}]) == ["平安银行", "诺思兰德"])

    # ---- 文本算子 ----
    ok("eq 市场=sh", hit({"field": "market", "op": "eq", "value": "sh"}) == ["贵州茅台"])
    ok("in 市场多选", hit({"field": "market", "op": "in", "value": ["bj", "sz"]})
       == ["平安银行", "诺思兰德"])
    ok("match 名称包含(大小写不敏感)", hit({"field": "name", "op": "match", "value": "银行"})
       == ["平安银行"])
    ok("match 英文名忽略大小写", hit({"field": "code", "op": "match", "value": "SH600"})
       == ["贵州茅台"])

    # ---- 空条件 = 全命中 ----
    ok("空条件全部命中", len(hit([])) == 3)

    # ---- 非法输入必须报错，绝不静默忽略 ----
    def bad(spec):
        try:
            compile_filters([spec])
            return None
        except ValueError as e:
            return str(e)

    ok("未知字段报错", "未知字段" in (bad({"field": "p_e", "op": "lte", "value": 10}) or ""))
    ok("未知算子报错", "未知算子" in (bad({"field": "pe", "op": "==", "value": 10}) or ""))
    ok("缺 value 报错", "缺少 value" in (bad({"field": "pe", "op": "lte"}) or ""))
    ok("between 元数不足报错", "2 个值" in (bad({"field": "pe", "op": "between",
                                                "value": [10]}) or ""))
    ok("between 上下界颠倒报错", "上下界颠倒" in (bad({"field": "pe", "op": "between",
                                                     "value": [50, 10]}) or ""))
    ok("数值字段传非数字报错", "必须是数字" in (bad({"field": "pe", "op": "lte",
                                                    "value": "abc"}) or ""))
    ok("数值字段用 match 报错", "match" in (bad({"field": "pe", "op": "match",
                                                 "value": "x"}) or ""))
    ok("in 空数组报错", "不能为空数组" in (bad({"field": "market", "op": "in",
                                               "value": []}) or ""))

    # ---- parse_filters 的分层报错 ----
    try:
        parse_filters("{不是json")
        ok("非法 JSON 报错", False)
    except ValueError as e:
        ok("非法 JSON 报错", "filters 不是合法 JSON" in str(e))
    try:
        parse_filters('{"field":"pe"}')
        ok("filters 非数组报错", False)
    except ValueError as e:
        ok("filters 非数组报错", "必须是数组" in str(e))
    ok("空串 filters 返回空条件", parse_filters("") == [])
    ok("正常 JSON 可解析", len(parse_filters(
        '[{"field":"pe","op":"lte","value":20}]')) == 1)

    # ---- 摘要 ----
    cs = compile_filters([{"field": "pe", "op": "gt", "value": 0},
                          {"field": "pe", "op": "lte", "value": 20}])
    ok("describe 生成可读摘要", describe(cs) == "市盈率 > 0.0、市盈率 ≤ 20.0",
       repr(describe(cs)))

    # ---- 缺失字段的行不应崩溃 ----
    ok("残缺行不抛异常", match_row({"code": "sh600000"}, cs) is False)

    print(f"\n  selfcheck 失败 {fail} 项")
    return fail


if __name__ == "__main__":
    sys_exit = 0
    sys_exit = selfcheck()
    raise SystemExit(1 if sys_exit else 0)
