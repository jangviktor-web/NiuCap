"""运行期可调配置。

优先级：meta 表覆盖值 > 环境变量 > 内置默认。
管理员在后台改的参数写进 meta（key 前缀 `cfg.`），读取时立即生效，无需重启。

设计取舍（ponytail）：
- 不引入 pydantic-settings 之类依赖，就是「查 meta → 查 env → 用默认」三层。
- 每项带校验器：连接池数量级填错会让取数慢十倍，管理页必须挡在写入前。
- 读缓存 TTL=5s，避免每个请求都查一次库；改配置后能秒级看到效果。
"""
from __future__ import annotations

import os
import re as _re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 参数声明

# (key, 标题, 类型, 默认值, 说明, 校验函数)
# 类型：int / float / bool / choice
_REGISTRY: List[Tuple[str, str, str, Any, str]] = [
    ("TICK_ELTDX_SERVERS", "eltdx 连接池 · 服务端数", "int", 4,
     "实测 4 台 × 6 连接最快（全市场 5575 只约 15.9s）。调到 8 以上收益递减且易被限流。"),
    ("TICK_ELTDX_CONNS", "eltdx 连接池 · 每台连接数", "int", 6,
     "与「服务端数」相乘即总连接数，建议保持 ≤ 32。"),
    ("TICK_ELTDX_BATCH", "eltdx 批量取数 · 单批代码数", "int", 200,
     "K 线接口单批代码数，200 实测吞吐最佳。注意与「实时快照批大小 80」不是一回事。"),
    ("TICK_ELTDX_TIMEOUT", "eltdx 批量取数超时（秒）", "float", 120.0,
     "全市场拉取正常 15~25s，此值为超时保护上限。"),
    ("TICK_ELTDX_KEEP_SUSPENDED", "保留停牌占位行", "bool", False,
     "默认剔除。停牌日的占位行（OHLC 等于前收、volume=0）会污染均量与形态识别，非排查问题不要打开。"),
    ("TICK_QUOTE_TTL", "实时快照缓存（秒）", "float", 3.0,
     "毫秒级快照的缓存时长。调小更实时但请求更多，调大省流量但可能看到旧价。"),
    ("TICK_SCAN_COUNT", "全市场扫描 · 取样K线根数", "int", 120,
     "条件选股/异动扫描每只股票取多少根K线算指标。加大更准但更慢。"),
    ("TICK_SYNC_BARS_AUTO", "日线落库 · 每天自动", "bool", False,
     "开启后服务会每天定时把全市场日线写入数据库。改动即时生效，无需重启。"),
    ("TICK_SYNC_BARS_AT", "日线落库 · 执行时间", "time", "15:30",
     "24 小时制 HH:MM。注意 A 股 15:00 收盘、盘后结算需要时间，"
     "15:30 执行时当日 K 线可能不完整；写入按 (代码,日期) 幂等覆盖，"
     "次日再跑会修正，不会累积脏数据。"),
    ("TICK_SYNC_BARS_COUNT", "日线落库 · 每只根数", "int", 250,
     "每次同步每只股票拉多少根日线（250 ≈ 1 年）。加大首次会更慢。"),
    ("TICK_SYNC_BARS_SCOPE", "日线落库 · 股票池", "scope", "all",
     "all=全市场（约 5575 只，含北交所）；也可填 hs300 / zz500 / zz1000 / zz2000。"),

    # ── 新闻 API 密钥（自部署用户在后台填写，存 meta 表；前端密码框渲染）────
    # 优先级同样 meta > env > 默认（空）。sector_news_lab 读取顺序：
    # 后台 config.get → 环境变量 → .env 文件。留空则该新闻源自动降级跳过。
    ("FINLIGHT_API_KEY", "新闻 API · Finlight 密钥", "secret", "",
     "Finlight 金融新闻 API 密钥（api.finlight.me）。自带情绪分析+公司标注+中文；"
     "留空则板块舆情聚合跳过 Finlight。也可在 .env 用同名校验变量配置。"),
    ("FREENEWS_API_KEY", "新闻 API · FreeNews 密钥", "secret", "",
     "Free News API 密钥（api.freenewsapi.io，5000 次/天免费）。英文为主、无情绪，"
     "用本地词典补情绪；留空则跳过。也可在 .env 用同名校验变量配置。"),
]

_BY_KEY = {r[0]: r for r in _REGISTRY}

_META_PREFIX = "cfg."
_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL = 5.0


def _validator(key: str):
    """按类型返回校验器。越界直接抛，由接口层转成 400。"""
    _, title, typ, default, _desc = _BY_KEY[key]

    if typ == "bool":
        def v(x):
            if isinstance(x, bool):
                return x
            s = str(x).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            raise ValueError(f"{title} 应为开关值")
        return v

    if typ == "int":
        # 上下界按参数语义给：连接池 1~64，批大小 1~800，超时 5~600，扫描 20~800
        lo, hi = {"TICK_ELTDX_SERVERS": (1, 64),
                  "TICK_ELTDX_CONNS": (1, 64),
                  "TICK_ELTDX_BATCH": (1, 800),
                  "TICK_ELTDX_TIMEOUT": (5, 600),
                  "TICK_SCAN_COUNT": (20, 800),
                  "TICK_SYNC_BARS_COUNT": (30, 3000)}.get(key, (1, 10000))

        def v(x):
            n = int(float(str(x).strip()))
            if not (lo <= n <= hi):
                raise ValueError(f"{title} 应在 {lo}~{hi} 之间，收到 {n}")
            return n
        return v

    if typ == "time":
        # HH:MM，24 小时制。不做"是否在交易时段"之类的业务校验——
        # 调度只要一个合法时间点，业务合理性由 description 说明。
        def v(x):
            s = str(x).strip()
            m = _re.fullmatch(r"(\d{1,2}):(\d{2})", s)
            if not m:
                raise ValueError(f"{title} 应为 HH:MM 格式（如 15:30），收到 {s!r}")
            hh, mm = int(m.group(1)), int(m.group(2))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError(f"{title} 不是合法时间：{s!r}")
            return f"{hh:02d}:{mm:02d}"
        return v

    if typ == "scope":
        # 不限定枚举：sync_bars.pick_codes 还支持直接传指数代码（如 000300）。
        # 只挡明显非法的字符，避免把股票池参数变成 SQL/路径注入面。
        def v(x):
            s = str(x).strip()
            if not s or not _re.fullmatch(r"[A-Za-z0-9_]{1,16}", s):
                raise ValueError(f"{title} 应为股票池名（all/hs300/zz500…）或 6 位指数代码，收到 {s!r}")
            return s.lower()
        return v

    if typ == "secret":
        # 密钥类：只做去空白，不限制长度/字符（不同服务 key 格式差异大）。
        # 不落日志、不在 /api/about 暴露；管理页以密码框渲染（见 web/index.html）。
        def v(x):
            return str(x).strip()
        return v

    if typ == "float":
        lo, hi = {"TICK_ELTDX_TIMEOUT": (5.0, 600.0),
                  "TICK_QUOTE_TTL": (0.0, 60.0)}.get(key, (0.0, 1e9))

        def v(x):
            n = float(str(x).strip())
            if not (lo <= n <= hi):
                raise ValueError(f"{title} 应在 {lo}~{hi} 之间，收到 {n}")
            return n
        return v

    def v(x):
        return str(x).strip()
    return v


def _env_raw(key: str) -> Optional[str]:
    return os.environ.get(key)


def _meta_raw(key: str) -> Optional[str]:
    """读 meta 覆盖值。store 未初始化或表不存在时安全返回 None。"""
    try:
        import store
        return store.meta_get(_META_PREFIX + key)
    except Exception:
        return None


def get(key: str) -> Any:
    """取参数当前值：meta > env > 默认。带 5s 缓存。"""
    if key not in _BY_KEY:
        raise KeyError(key)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    _, title, typ, default, _desc = _BY_KEY[key]
    val = default
    raw = _meta_raw(key)
    if raw is None:
        raw = _env_raw(key)
    if raw is not None:
        try:
            val = _validator(key)(raw)
        except (ValueError, TypeError):
            val = default          # 库里/环境里的脏值不该拖垮服务
    _CACHE[key] = (time.time(), val)
    return val


def source_of(key: str) -> str:
    """该参数当前值来自哪一层：meta / env / default。供界面标注。"""
    if _meta_raw(key) is not None:
        return "meta"
    if _env_raw(key) is not None:
        return "env"
    return "default"


def set_value(key: str, raw: Any) -> Any:
    """写 meta 覆盖值。校验失败抛 ValueError。"""
    if key not in _BY_KEY:
        raise KeyError(key)
    val = _validator(key)(raw)     # 先校验，脏值永不落库
    import store
    store.meta_set(_META_PREFIX + key, "1" if val is True else ("0" if val is False else str(val)))
    _CACHE.pop(key, None)          # 立刻失效，读到的就是新值
    return val


def reset(key: str) -> None:
    """删掉覆盖值，回落到 env/默认。"""
    import store
    store.meta_del(_META_PREFIX + key)
    _CACHE.pop(key, None)
    _CACHE.pop("__all__", None)


def invalidate() -> None:
    _CACHE.clear()


def all_items() -> List[Dict[str, Any]]:
    """全量参数（含当前值、来源、默认值、说明），供管理页渲染。"""
    out = []
    for key, title, typ, default, desc in _REGISTRY:
        try:
            cur = get(key)
        except Exception:
            cur = default
        out.append({
            "key": key,
            "title": title,
            "type": typ,
            "value": cur,
            "default": default,
            "source": source_of(key),
            "desc": desc,
        })
    return out


def apply_to_env() -> None:
    """把 meta 覆盖值同步回 os.environ。

    给那些在模块加载期就读常量的老代码用（如 sources/eltdx_source.py）。
    在 app 启动时调用一次；改配置后由接口再次调用。
    """
    for key in _BY_KEY:
        raw = _meta_raw(key)
        if raw is None:
            continue
        try:
            val = _validator(key)(raw)
        except (ValueError, TypeError):
            continue
        os.environ[key] = "1" if val is True else ("0" if val is False else str(val))
    invalidate()


def selfcheck() -> Dict[str, Any]:
    """自检：校验器边界 + 覆盖优先级 + 回退。不依赖真实数据库。"""
    steps = []
    # 1. 校验器：越界必须抛
    for key, bad in [("TICK_ELTDX_SERVERS", "0"), ("TICK_ELTDX_SERVERS", "999"),
                     ("TICK_ELTDX_BATCH", "0"), ("TICK_SCAN_COUNT", "1"),
                     ("TICK_QUOTE_TTL", "-1"),
                     # 新类型：时间 / 股票池
                     ("TICK_SYNC_BARS_AT", "25:00"), ("TICK_SYNC_BARS_AT", "15-30"),
                     ("TICK_SYNC_BARS_AT", "abc"), ("TICK_SYNC_BARS_AT", ""),
                     ("TICK_SYNC_BARS_SCOPE", "../etc/passwd"),
                     ("TICK_SYNC_BARS_SCOPE", "all; drop"),
                     ("TICK_SYNC_BARS_COUNT", "5")]:
        try:
            _validator(key)(bad)
            raise AssertionError(f"{key}={bad} 应被拒绝")
        except ValueError:
            steps.append(f"reject {key}={bad} OK")
    # 2. 合法值放行
    assert _validator("TICK_ELTDX_SERVERS")("4") == 4
    assert _validator("TICK_ELTDX_KEEP_SUSPENDED")("true") is True
    assert _validator("TICK_ELTDX_KEEP_SUSPENDED")("0") is False
    assert _validator("TICK_QUOTE_TTL")("0") == 0.0
    assert _validator("TICK_SYNC_BARS_AT")("9:05") == "09:05", "时间应补零归一"
    assert _validator("TICK_SYNC_BARS_AT")("15:30") == "15:30"
    assert _validator("TICK_SYNC_BARS_SCOPE")("ALL") == "all"
    assert _validator("TICK_SYNC_BARS_SCOPE")("000300") == "000300", "应支持直接传指数代码"
    steps.append("accept valid values OK")
    # 3. 未配置时回落默认
    os.environ.pop("TICK_ELTDX_SERVERS", None)
    invalidate()
    assert get("TICK_ELTDX_SERVERS") == 4
    steps.append("fallback to default OK")
    # 4. 环境变量层生效
    os.environ["TICK_ELTDX_SERVERS"] = "8"
    invalidate()
    assert get("TICK_ELTDX_SERVERS") == 8
    assert source_of("TICK_ELTDX_SERVERS") == "env"
    steps.append("env override OK")
    os.environ.pop("TICK_ELTDX_SERVERS", None)
    invalidate()
    # 5. 脏环境值不拖垮服务
    os.environ["TICK_ELTDX_SERVERS"] = "abc"
    invalidate()
    assert get("TICK_ELTDX_SERVERS") == 4, "脏值应回落默认"
    steps.append("dirty env falls back OK")
    os.environ.pop("TICK_ELTDX_SERVERS", None)
    invalidate()
    return {"ok": True, "steps": steps, "params": len(_REGISTRY)}


if __name__ == "__main__":
    import json
    print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
