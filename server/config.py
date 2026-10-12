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

# ------------------------------------------------- 模块登录权限
# 哪些功能「必须注册登录后才能用」。管理员在后台「登录权限」面板逐项开关，立即生效。
#
# 为什么默认全都要登录：这些都是**让服务器动起来**的功能，不是「看一眼」的行情 ——
#   · 选股类（screen / newbie / nlpick / strategy / intraday / rating）：
#     一次请求 = 拉全市场快照 + 逐票算几十项指标，实测一次全市场扫描约 6 秒 CPU；
#   · paper / grid：要把持仓、成交流水、计划明细长期写进数据库，还要按轮轮询。
# 匿名放开等价于把 CPU 与磁盘送给脚本批量刷，且这些数据无法归属到账号
# （认领时会把同机别人的记录一并领走）。
#
# 做成开关而不是写死，是因为「放开某一个」的场景真实存在：内网自用、
# 只想让人看盘不想让他算、临时演示等。默认值保持收紧，放开要管理员显式点一下。
ACCESS_MODULES: List[Tuple[str, str, str, bool]] = [
    ("screen", "条件选股（含高级筛选）",
     "每次执行都要扫全市场数千只票并逐只算指标，是本机最贵的请求；"
     "结果还会落「筛选历史」做后续业绩跟踪，需要归属到账号。", True),
    ("newbie", "一键选股",
     "小白策略对候选池做多因子打分排序，走的是同一套全市场计算。", True),
    ("nlpick", "一句话选股",
     "自然语言只是解析方式不同，解析完同样落到全市场选股，算力成本与条件选股一致。", True),
    ("strategy", "策略选股",
     "一次要跑多组策略、每组对每只票做历史回看，算力通常是普通选股的数倍。", True),
    ("intraday", "盘中扫描",
     "分钟级全市场轮询，每轮要取数百只票的实时快照再逐只算分钟指标。", True),
    ("rating", "指标评级",
     "批量评级要逐票拉 K 线算多周期指标，结果还需按配额缓存、占用内存。", True),
    ("grid", "网格交易计划",
     "计划要长期保存、由后台线程定时检查触发，必须归属到某个账号才有意义。", True),
    ("paper", "虚拟盘",
     "持仓 / 成交流水 / 费率设置长期存在服务端数据库，匿名数据无法跨设备延续。", True),
]

_MOD_KEY = {m[0]: "TICK_AUTH_REQ_" + m[0].upper() for m in ACCESS_MODULES}
_MOD_NAME = {m[0]: m[1] for m in ACCESS_MODULES}

# 给前端登录弹窗用的统一原因说明。
# 放在后端而不是前端写死：这套文案要跟着「哪些模块被拦」一起演进，
# 分散在两处迟早会出现「界面改了、接口没改」的口径不一致。
ACCESS_REASON = (
    "选股一次要在服务器上扫描数千只股票、逐只计算几十项指标；"
    "虚拟盘与网格还要长期保存持仓、成交流水和计划明细 —— "
    "这些功能会占用较大的服务器算力与存储空间，所以只对已登录账号开放。"
    "账号登录后，你的筛选历史、持仓与计划才能真正归属到你的名下，换设备也不丢。"
)

for _m in ACCESS_MODULES:
    _REGISTRY.append(
        (_MOD_KEY[_m[0]], "🔒 " + _m[1] + " · 要求登录", "bool", _m[3], _m[2])
    )

_BY_KEY = {r[0]: r for r in _REGISTRY}

# 参数分组：access 组的开关在后台「登录权限」面板单独渲染，
# 「运行参数」面板按 group 过滤掉它们 —— 否则同一批开关在两处出现，改哪儿都说不清。
_GROUP: Dict[str, str] = {_MOD_KEY[m[0]]: "access" for m in ACCESS_MODULES}

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
            "group": _GROUP.get(key, "runtime"),
        })
    return out


# ------------------------------------------------- 权限判定

def need_login(mod: str) -> bool:
    """该模块当前是否要求登录。

    未知模块名保守返回 True —— 宁可多拦一次，也不能因为拼错 key 把重算接口放成匿名。
    """
    key = _MOD_KEY.get(mod)
    if not key:
        return True
    try:
        return bool(get(key))
    except Exception:
        return True      # 读失败一律按「要登录」处理


def module_name(mod: str) -> str:
    """模块显示名，用于 401 文案；未知模块给个通用名而不是抛。"""
    return _MOD_NAME.get(mod) or "该功能"


def access_rules() -> Dict[str, Any]:
    """本实例当前的开放策略，供 ``/api/access`` **匿名**下发。

    只回答「哪些门上锁」，不含任何用户数据 —— 它不是权限本身，只是门牌。
    """
    return {
        "require_login": {m[0]: need_login(m[0]) for m in ACCESS_MODULES},
        "modules": [{"key": m[0], "name": m[1], "desc": m[2],
                     "default": m[3]} for m in ACCESS_MODULES],
        "reason": ACCESS_REASON,
    }


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

    # 6. 模块登录权限：默认全锁、改开关立即生效、未知模块保守加锁
    assert len(ACCESS_MODULES) == len(_MOD_KEY), "模块 key 需唯一，否则 Config 互相覆盖"
    for m in ACCESS_MODULES:
        assert need_login(m[0]) is m[3], f"{m[0]} 默认值应为 {m[3]}"
    assert need_login("no_such_module") is True, "未知模块必须保守要求登录"
    _k = _MOD_KEY["rating"]
    _old = get(_k)
    try:
        set_value(_k, not _old)
        assert need_login("rating") is (not _old), "写 meta 后应跳过缓存立即生效"
    finally:
        # 无论断言是否失败都要还原：selfcheck 不该在运行库里留下开关脏值
        try:
            set_value(_k, _old)
        except Exception:
            reset(_k)
        invalidate()
    assert need_login("rating") is _old, "还原后应回到原值"
    assert access_rules()["require_login"]["screen"] is ACCESS_MODULES[0][3]
    assert "算力" in ACCESS_REASON, "原因说明要讲清算力/存储这笔账"
    steps.append("module access rules OK")

    return {"ok": True, "steps": steps, "params": len(_REGISTRY)}


if __name__ == "__main__":
    import json
    print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
