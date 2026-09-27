"""通达信（TDX）公式编译器。

借鉴三省六部框架 tdx/formula_compiler.py 的函数映射思路，
但**完全重写实现方式**：原实现是把公式文本拼成 Python 源码字符串再交给
`exec()` 执行 —— 这条路在 Web 服务里等于开放任意代码执行，不可接受。

本实现改为「白名单解释器」：
  1. 解析公式为「变量赋值语句 + 最终信号表达式」；
  2. 表达式交给一个**受限 AST 求值器**，只允许出现白名单函数、白名单序列名、
     数值字面量与有限算子；任何未授权的节点类型直接拒绝；
  3. 所有序列运算用 numpy 完成，天然是向量化语义。

支持的通达信语法（与主流用法一致）：
  序列：C/CLOSE、O/OPEN、H/HIGH、L/LOW、V/VOL、AMOUNT
  函数：MA EMA SMA WMA HHV LLV SUM REF COUNT STD CROSS IF
        BARSLAST EXIST EVERY BETWEEN VALUEWHEN BARSSINCE ABS MAX MIN
  算子：+ - * / > < >= <= = <> AND OR NOT

示例公式：
  MA5:=MA(C,5);
  MA20:=MA(C,20);
  BUY:CROSS(MA5,MA20);
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------- 白名单

SERIES_ALIAS = {
    "C": "close", "CLOSE": "close",
    "O": "open", "OPEN": "open",
    "H": "high", "HIGH": "high",
    "L": "low", "LOW": "low",
    "V": "vol", "VOL": "vol", "VOLUME": "vol",
    "AMOUNT": "amount",
}

FUNCTIONS = {
    "MA", "EMA", "SMA", "WMA", "HHV", "LLV", "SUM", "REF", "COUNT",
    "STD", "CROSS", "IF", "BARSLAST", "EXIST", "EVERY", "BETWEEN",
    "VALUEWHEN", "BARSSINCE", "ABS", "MAX", "MIN",
}

ARITH_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)
CMP_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
BOOL_OPS = (ast.BitAnd, ast.BitOr, ast.BitXor)

_ALIAS_TO_UPPER = {k.upper(): k.upper() for k in SERIES_ALIAS}


# ---------------------------------------------------------------- 向量化函数


def _as_arr(x):
    return np.asarray(x, dtype=float)


def f_MA(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    out = np.full(len(s), np.nan)
    if len(s) >= n:
        c = np.cumsum(np.insert(s, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def f_EMA(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    a = 2.0 / (n + 1.0)
    out = np.full(len(s), np.nan)
    if len(s) == 0:
        return out
    out[0] = s[0]
    for i in range(1, len(s)):
        out[i] = a * s[i] + (1 - a) * out[i - 1]
    return out


def f_SMA(s, n, m=1):
    """通达信 SMA(X,N,M)：Y = (M*X + (N-M)*Y') / N。"""
    s = _as_arr(s)
    n = max(1, int(n))
    m = max(1, int(m))
    out = np.full(len(s), np.nan)
    if len(s) == 0:
        return out
    out[0] = s[0]
    for i in range(1, len(s)):
        out[i] = (m * s[i] + (n - m) * out[i - 1]) / n
    return out


def f_WMA(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    out = np.full(len(s), np.nan)
    if len(s) < n:
        return out
    w = np.arange(1, n + 1, dtype=float)
    wsum = w.sum()
    for i in range(n - 1, len(s)):
        out[i] = float(np.dot(s[i - n + 1:i + 1], w) / wsum)
    return out


def f_HHV(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    out = np.full(len(s), np.nan)
    for i in range(len(s)):
        lo = max(0, i - n + 1)
        seg = s[lo:i + 1]
        if len(seg):
            out[i] = np.nanmax(seg)
    return out


def f_LLV(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    out = np.full(len(s), np.nan)
    for i in range(len(s)):
        lo = max(0, i - n + 1)
        seg = s[lo:i + 1]
        if len(seg):
            out[i] = np.nanmin(seg)
    return out


def f_SUM(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    out = np.full(len(s), np.nan)
    for i in range(len(s)):
        lo = max(0, i - n + 1)
        out[i] = float(np.nansum(s[lo:i + 1]))
    return out


def f_REF(s, n):
    s = _as_arr(s)
    n = int(n)
    out = np.full(len(s), np.nan)
    if n >= 0 and len(s) > n:
        out[n:] = s[:len(s) - n]
    elif n < 0 and len(s) > -n:
        out[:len(s) + n] = s[-n:]
    return out


def f_COUNT(cond, n):
    c = _as_arr(cond)
    n = max(1, int(n))
    out = np.full(len(c), np.nan)
    for i in range(len(c)):
        lo = max(0, i - n + 1)
        out[i] = float(np.nansum((c[lo:i + 1] != 0).astype(float)))
    return out


def f_STD(s, n):
    s = _as_arr(s)
    n = max(1, int(n))
    out = np.full(len(s), np.nan)
    for i in range(len(s)):
        lo = max(0, i - n + 1)
        seg = s[lo:i + 1]
        if len(seg) >= 2:
            out[i] = float(np.nanstd(seg, ddof=1))
        elif len(seg) == 1:
            out[i] = 0.0
    return out


def f_CROSS(a, b):
    a = _as_arr(a)
    b = _as_arr(b)
    n = min(len(a), len(b))
    out = np.zeros(n, dtype=bool)
    if n >= 2:
        out[1:] = (a[1:n] > b[1:n]) & (a[:n - 1] <= b[:n - 1])
    return out


def f_IF(cond, a, b):
    c = _as_arr(cond) != 0
    a = _as_arr(a) if np.ndim(a) else np.full(len(c), float(a))
    b = _as_arr(b) if np.ndim(b) else np.full(len(c), float(b))
    return np.where(c, a, b)


def f_BARSLAST(cond):
    c = _as_arr(cond) != 0
    out = np.full(len(c), np.nan)
    last = -1
    for i in range(len(c)):
        if c[i]:
            last = i
        out[i] = (i - last) if last >= 0 else np.nan
    return out


def f_BARSSINCE(cond):
    c = _as_arr(cond) != 0
    out = np.full(len(c), np.nan)
    first = -1
    for i in range(len(c)):
        if c[i] and first < 0:
            first = i
        out[i] = (i - first) if first >= 0 else np.nan
    return out


def f_EXIST(cond, n):
    c = _as_arr(cond)
    n = max(1, int(n))
    out = np.full(len(c), np.nan)
    for i in range(len(c)):
        lo = max(0, i - n + 1)
        out[i] = 1.0 if (c[lo:i + 1] != 0).any() else 0.0
    return out


def f_EVERY(cond, n):
    c = _as_arr(cond)
    n = max(1, int(n))
    out = np.full(len(c), np.nan)
    for i in range(len(c)):
        lo = max(0, i - n + 1)
        out[i] = 1.0 if (c[lo:i + 1] != 0).all() else 0.0
    return out


def f_BETWEEN(x, a, b):
    return (x >= a) & (x <= b)


def f_VALUEWHEN(cond, x):
    c = _as_arr(cond) != 0
    x = _as_arr(x)
    out = np.full(len(x), np.nan)
    last = np.nan
    for i in range(len(x)):
        if c[i]:
            last = x[i]
        out[i] = last
    return out


_ALLOWED_FUNCS = {
    "MA": f_MA, "EMA": f_EMA, "SMA": f_SMA, "WMA": f_WMA,
    "HHV": f_HHV, "LLV": f_LLV, "SUM": f_SUM, "REF": f_REF,
    "COUNT": f_COUNT, "STD": f_STD, "CROSS": f_CROSS, "IF": f_IF,
    "BARSLAST": f_BARSLAST, "BARSSINCE": f_BARSSINCE,
    "EXIST": f_EXIST, "EVERY": f_EVERY, "BETWEEN": f_BETWEEN,
    "VALUEWHEN": f_VALUEWHEN,
    "ABS": np.abs, "MAX": np.maximum, "MIN": np.minimum,
}


# ---------------------------------------------------------------- 安全求值器


class FormulaError(Exception):
    pass


class _Evaluator:
    """受限 AST 求值器：仅允许白名单函数、序列名与数值字面量。"""

    def __init__(self, series: Dict[str, Any], vars_: Dict[str, Any]):
        self.series = series
        self.vars = vars_

    def eval(self, node: ast.AST):
        if isinstance(node, ast.Expression):
            return self.eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise FormulaError(f"不支持的常量类型: {type(node.value).__name__}")
        if isinstance(node, ast.Name):
            up = node.id.upper()
            if up in _ALIAS_TO_UPPER:
                key = SERIES_ALIAS[up]
                if key not in self.series:
                    raise FormulaError(f"缺少序列数据: {up}")
                return self.series[key]
            if up in self.vars:
                return self.vars[up]
            raise FormulaError(f"未定义变量: {node.id}")
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, ARITH_OPS):
                raise FormulaError("不支持的算术运算符")
            a, b = self.eval(node.left), self.eval(node.right)
            return self._binop(node.op, a, b)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -self.eval(node.operand)
            if isinstance(node.op, ast.Not):
                return ~(np.asarray(self.eval(node.operand)) != 0)
            if isinstance(node.op, ast.UAdd):
                return self.eval(node.operand)
            raise FormulaError("不支持的一元运算符")
        if isinstance(node, ast.BoolOp):
            vals = [np.asarray(self.eval(v) != 0) for v in node.values]
            out = vals[0]
            if isinstance(node.op, ast.And):
                for v in vals[1:]:
                    out = out & v
            else:
                for v in vals[1:]:
                    out = out | v
            return out
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                raise FormulaError("暂不支持链式比较")
            op = node.ops[0]
            if not isinstance(op, CMP_OPS):
                raise FormulaError("不支持的比较运算符")
            a = self.eval(node.left)
            b = self.eval(node.comparators[0])
            return self._cmp(op, a, b)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise FormulaError("仅支持直接函数调用")
            fname = node.func.id.upper()
            fn = _ALLOWED_FUNCS.get(fname)
            if fn is None:
                raise FormulaError(f"不支持的函数: {fname}")
            args = [self.eval(a) for a in node.args]
            # 变量名（用于生成更友好的缺参提示）
            _ARG_NAMES = {
                "MA": ["序列", "周期"], "EMA": ["序列", "周期"],
                "SMA": ["序列", "周期", "权重M"], "WMA": ["序列", "周期"],
                "HHV": ["序列", "周期"], "LLV": ["序列", "周期"],
                "SUM": ["序列", "周期"], "REF": ["序列", "前推周期"],
                "COUNT": ["条件", "周期"], "STD": ["序列", "周期"],
                "CROSS": ["序列A", "序列B"], "IF": ["条件", "条件成立值", "条件不成立值"],
                "BARSLAST": ["条件"], "BARSSINCE": ["条件"],
                "EXIST": ["条件", "周期"], "EVERY": ["条件", "周期"],
                "BETWEEN": ["值", "下界", "上界"], "VALUEWHEN": ["条件", "取值"],
                "ABS": ["值"], "MAX": ["值A", "值B"], "MIN": ["值A", "值B"],
            }
            try:
                return fn(*args)
            except FormulaError:
                raise
            except TypeError:
                need = _ARG_NAMES.get(fname, [])
                want = len(need)
                raise FormulaError(
                    f"{fname} 参数数量不对：需要 {want} 个"
                    + (f"（{'、'.join(need)}）" if need else "")
                    + f"，实际传入 {len(args)} 个"
                )
            except Exception as e:
                raise FormulaError(f"{fname} 执行失败: {e}")
        raise FormulaError(f"不支持的语法节点: {type(node).__name__}")

    def _binop(self, op, a, b):
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.divide(a, b)
        if isinstance(op, ast.Pow):
            with np.errstate(invalid="ignore", over="ignore"):
                return np.power(a, b)
        if isinstance(op, ast.Mod):
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.mod(a, b)
        raise FormulaError("不支持的算术运算符")

    def _cmp(self, op, a, b):
        if isinstance(op, ast.Lt):
            return np.asarray(a) < b
        if isinstance(op, ast.LtE):
            return np.asarray(a) <= b
        if isinstance(op, ast.Gt):
            return np.asarray(a) > b
        if isinstance(op, ast.GtE):
            return np.asarray(a) >= b
        if isinstance(op, ast.Eq):
            return np.asarray(a) == b
        if isinstance(op, ast.NotEq):
            return np.asarray(a) != b
        raise FormulaError("不支持的比较运算符")


# ---------------------------------------------------------------- 预处理


def _strip_comments(text: str) -> str:
    """去掉 {} 与 // 注释。"""
    out = re.sub(r"\{[^}]*\}", "", text or "")
    out = re.sub(r"//[^\n]*", "", out)
    return out


def _translate(text: str) -> str:
    """把通达信语法翻译为 Python 可解析的表达式。"""
    out = _strip_comments(text)
    # 逻辑算子
    out = re.sub(r"<>", "!=", out)
    out = re.sub(r"(?<![<>=!])=(?!=)", "==", out)
    # AND/OR/NOT 作为独立词
    out = re.sub(r"\bAND\b", " and ", out, flags=re.IGNORECASE)
    out = re.sub(r"\bOR\b", " or ", out, flags=re.IGNORECASE)
    out = re.sub(r"\bNOT\b", " not ", out, flags=re.IGNORECASE)
    return out


def _split_statements(text: str) -> List[Tuple[Optional[str], str]]:
    """拆分语句，返回 [(变量名或None, 表达式)]。

    支持 `VAR:=expr`（中间变量，不输出）与 `EXPR`（输出信号）。
    也支持 `VAR: expr`（输出型变量）。
    """
    text = _strip_comments(text).replace("\r", "\n")
    parts = re.split(r"[;\n]+", text)
    result: List[Tuple[Optional[str], str]] = []
    for p in parts:
        item = p.strip()
        if not item:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*=(.*)$", item, re.S)
        if m:
            result.append((m.group(1).upper(), m.group(2).strip()))
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$", item, re.S)
        if m and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:=", item):
            result.append((m.group(1).upper(), m.group(2).strip()))
            continue
        result.append((None, item))
    return result


def _called_functions(text: str) -> List[str]:
    found = []
    seen = set()
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", _strip_comments(text)):
        fn = m.group(1).upper()
        if fn in ("AND", "OR", "NOT") or fn in seen:
            continue
        seen.add(fn)
        found.append(fn)
    return found


def capabilities() -> Dict[str, Any]:
    """返回编译器能力清单，供前端展示。"""
    return {
        "functions": sorted(FUNCTIONS),
        "series": sorted(SERIES_ALIAS.keys()),
        "operators": ["+", "-", "*", "/", ">", "<", ">=", "<=", "=", "<>",
                      "AND", "OR", "NOT"],
        "examples": [
            {"name": "均线金叉", "formula": "MA5:=MA(C,5);\nMA20:=MA(C,20);\nCROSS(MA5,MA20)"},
            {"name": "突破20日新高", "formula": "C>REF(HHV(H,20),1)"},
            {"name": "放量上涨", "formula": "(C>REF(C,1)) AND (V>MA(V,5)*1.5)"},
            {"name": "RSI式超卖反弹", "formula": "(C<MA(C,20)*0.95) AND CROSS(C,REF(C,1))"},
            {"name": "布林下轨买入", "formula": "MID:=MA(C,20);\nSTD20:=STD(C,20);\nLOWB:=MID-2*STD20;\nC<LOWB"},
            {"name": "连续三天下跌", "formula": "EVERY(C<REF(C,1),3)"},
        ],
    }


def compile_formula(formula: str, klines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """编译通达信公式并在给定K线上求值。

    返回 {signals:[...], buy_count, sell_count, warnings, variables}
      signals: 与 klines 等长的 int 列表，1=买 -1=卖 0=无
    公式求值结果按「最后一行为输出信号」处理；布尔结果为 True 时记买入信号。
    """
    if not klines:
        return {"error": "缺少K线数据"}
    text = str(formula or "").strip()
    if not text:
        return {"error": "请输入通达信公式"}

    n = len(klines)
    series = {
        "close": np.array([float(k["close"]) for k in klines], dtype=float),
        "open": np.array([float(k.get("open", k["close"])) for k in klines], dtype=float),
        "high": np.array([float(k.get("high", k["close"])) for k in klines], dtype=float),
        "low": np.array([float(k.get("low", k["close"])) for k in klines], dtype=float),
        "vol": np.array([float(k.get("volume", 0) or 0) for k in klines], dtype=float),
        "amount": np.array([float(k.get("amount", 0) or 0) for k in klines], dtype=float),
    }

    # 函数白名单校验
    called = _called_functions(text)
    unsupported = [f for f in called if f not in FUNCTIONS and f not in SERIES_ALIAS]
    if unsupported:
        return {"error": f"不支持的函数: {', '.join(unsupported)}",
                "supported": sorted(FUNCTIONS)}

    statements = _split_statements(text)
    if not statements:
        return {"error": "公式为空"}

    ev = _Evaluator(series, {})
    warnings: List[str] = []
    last_value = None
    var_names: List[str] = []

    for name, expr in statements:
        py_expr = _translate(expr)
        try:
            tree = ast.parse(py_expr, mode="eval")
        except SyntaxError as e:
            return {"error": f"语法错误：{expr} → {e.msg}"}
        try:
            value = ev.eval(tree)
        except FormulaError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"求值失败: {e}"}
        if name:
            ev.vars[name] = value
            var_names.append(name)
        else:
            last_value = value

    # 若没有任何无名输出行，取最后一个变量的值
    if last_value is None:
        if var_names:
            last_value = ev.vars[var_names[-1]]
        else:
            return {"error": "公式未产出任何结果"}

    arr = np.asarray(last_value)
    if arr.ndim == 0:
        arr = np.full(n, float(arr))
    arr = arr.reshape(-1)
    if len(arr) != n:
        if len(arr) > n:
            arr = arr[-n:]
        else:
            pad = np.full(n - len(arr), np.nan)
            arr = np.concatenate([pad, arr])

    # 布尔型 → 买入信号；数值型 → 正负号作为买卖
    if arr.dtype == bool:
        sig = np.where(arr, 1, 0).astype(int)
    else:
        with np.errstate(invalid="ignore"):
            finite = np.isfinite(arr)
        sig = np.zeros(n, dtype=int)
        sig[finite & (arr > 0)] = 1
        sig[finite & (arr < 0)] = -1

    buy_count = int((sig == 1).sum())
    sell_count = int((sig == -1).sum())
    if buy_count == 0 and sell_count == 0:
        warnings.append("公式未产生任何信号，请检查条件是否过于严格")

    # 返回信号序列（前端可画标记）
    return {
        "signals": sig.tolist(),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "warnings": warnings,
        "variables": var_names,
        "called_functions": called,
        "last_value_type": "bool" if arr.dtype == bool else "number",
    }


if __name__ == "__main__":
    import datasource as ds
    kl = ds.get_kline("sh600519", "1d", 250)
    print("K线:", len(kl))
    for item in capabilities()["examples"]:
        r = compile_formula(item["formula"], kl)
        print(f"{item['name']:14s} 买={r.get('buy_count')} 卖={r.get('sell_count')} "
              f"{r.get('error','')} {r.get('warnings')}")
