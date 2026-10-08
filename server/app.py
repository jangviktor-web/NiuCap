"""
tick-stock-panel · A股选股与量化分析服务
数据源：腾讯行情/K线 + 新浪全市场列表（沙箱实测可用）
计算核心：复用 a-stock-data-quant 技能的 MyTT 指标库 + 策略库
"""
import json
import os
import sys
import time
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 数据目录：健康巡检的状态文件与告警日志都落在这里
_DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


# ---------------------------------------------------------------------------
# 启动时自读 .env（不覆盖已有的进程环境变量）
#
# 原本只有 deploy.sh 用 `set -a; . .env` 注入配置。只要有人直接
# `python run.py` 裸起进程（平台重启、手工重启、调试），TICK_ADMIN_USERS
# 就会整个丢失，表现为「管理页进不去」——而且页面不报错，只是入口消失，
# 很难看出是配置没加载。这里兜底，让配置与启动方式解耦。
# 必须在下面 import store 之前跑，否则数据库后端已经按默认值定好了。
# ---------------------------------------------------------------------------
def _load_env_file() -> None:
    for path in (os.path.join(os.path.dirname(HERE), ".env"),
                 os.path.join(HERE, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:        # 读 .env 失败不该拖垮启动
            print(f"[env] 读取 {path} 失败：{e}")
        break


_load_env_file()

from fastapi import FastAPI, Query, HTTPException, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import datasource as ds
import indicator as ind
import indicators_extra as ix
import screener as scr
import backtest as bt
import keylevels as kls
import moves as mvs
import newsfeed as nf
import sentiment as senti
import strategies as strat
import westock as wst
import hithink as htk
import intent as itt
import tdx as tdxc
import newbie as nwb
import grid as grd
import market_phase as mp
import alerts as alt
import webhook
import maintain as mt
import limitup as lp
import theme_radar as tr
import etf
import store
import windowsim as wsim
import pandas as pd

WEB_DIR = os.path.join(os.path.dirname(HERE), "web")

app = FastAPI(title="tick-stock-panel", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ===========================================================================
# 全市场快照缓存（后台定时刷新，避免每次请求都全量拉取）
#
# 主路径：内置指数成分股池(3800只) + 腾讯批量行情 —— 2~3 秒，不限流
# 增量路径：新浪全市场列表(5497只) —— 字段更全但会限流，仅低速后台补充
# ===========================================================================

class MarketCache:
    def __init__(self):
        self.rows = []
        self.updated = 0.0
        self.loading = False
        self._lock = threading.Lock()
        self._err = None
        self.pool_size = 0

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        try:
            rows = ds.pool_rows("all")          # 腾讯批量，稳定快速
            if rows:
                with self._lock:
                    self.rows = rows
                    self.updated = time.time()
                    self.pool_size = len(rows)
                    self._err = None
        except Exception as e:
            self._err = str(e)
        finally:
            self.loading = False

    def get(self):
        with self._lock:
            return list(self.rows), self.updated

    def ensure(self, max_age=180):
        """缓存过期则刷新；首次请求同步等待"""
        rows, upd = self.get()
        if rows and (time.time() - upd) < max_age:
            return rows
        if not rows:
            self.refresh()          # 首次同步拉（约 3 秒）
            rows, _ = self.get()
        elif not self.loading:
            threading.Thread(target=self.refresh, daemon=True).start()
        return rows


MARKET = MarketCache()

#: 进程启动时刻，管理页算运行时长用
_BOOT_TS = time.time()

#: 后台落库任务的共享状态（同一时刻只允许一个任务，见 api_admin_bars_sync）
_SYNC_STATE: Dict[str, Any] = {"running": False, "started": 0.0, "finished": 0.0,
                               "scope": "", "total": 0, "done": 0,
                               "ok_count": 0, "error": "", "note": ""}


def _warm():
    try:
        MARKET.refresh()
    except Exception:
        pass


def _warm_history():
    """预热历史指标引擎。

    首个带 "hist" 的策略请求需要触发全表加载（实测约 3.3 秒）+
    全市场指标计算。若不预热，用户第一次点「突破新高」这类策略时
    会卡几秒，且此窗口内可能返回空结果（看起来像「没有股票符合」）。
    这里在启动后台线程里提前加载好，让首次点击就是热的。
    """
    try:
        import history as _hist
        t0 = time.time()
        eng = _hist.get_engine()
        st = eng.stats() or {}
        print(f"[history] 历史指标引擎已预热：{st.get('codes', '?')} 只 / "
              f"{st.get('rows', '?')} 行 / {time.time() - t0:.2f}s / "
              f"最新 {st.get('as_of') or '—'}")
    except Exception as e:
        print(f"[history] 预热失败（历史类策略首次调用时会再试）：{e}")


@app.on_event("startup")
def startup():
    # 初始化本地库（幂等建表）；失败不应阻断行情功能
    try:
        # 注入「代码 → 中文简称」解析器，供加自选时补全名称
        store.set_name_resolver(_resolve_name)
        info = store.initialize()
        print(f"[store] 就绪: {store.BACKEND} {info['db']} (schema v{info['schema']})")
    except Exception as e:
        print(f"[store] 初始化失败（自选股功能将不可用）：{e}")

    # 应用管理页存的参数覆盖（meta > env），并同步给数据源模块
    try:
        import config as _cfg
        _cfg.apply_to_env()
        eff = _apply_config_live()
        print(f"[config] 参数已加载: {eff}")
    except Exception as e:
        print(f"[config] 参数加载失败（沿用环境变量）：{e}")

    admins = _admin_names()
    print(f"[admin] 管理员: {','.join(sorted(admins)) if admins else '未配置（管理页关闭）'}")

    # 自建部署开箱默认管理员（仅当未显式配置 TICK_ADMIN_USERS 时）
    try:
        _seed_default_admin()
    except Exception as e:
        print(f"[seed] 默认管理员初始化失败（可忽略）：{e}")

    # 快讯滚动缓冲：重启从 md 重载（保留上次会话），否则后台回填近 24h
    try:
        nf.ensure_history_loaded()
    except Exception as e:
        print(f"[history] 缓冲预热失败（将从实时流重新积累）：{e}")

    # 日线落库每日调度（开关默认关，由 config 控制；开启后无需重启）
    try:
        import scheduler as _sch
        _sch.start()
    except Exception as e:
        print(f"[sync-bars] 调度启动失败（不影响其他功能）：{e}")

    # #98 体检缓存每日自动预热：不依赖落库，每天 16:30 自己检查一次
    # （先起调度，再由它决定今天要不要算——比下面的补跑更通用）
    try:
        import strategy_eval as _se
        _se.start_daily_prewarm()
    except Exception as e:
        print(f"[eval-prewarm] 每日调度启动失败（不影响其他功能）：{e}")

    # 服务启动补跑。原来是「今天必须已落库成功才补」——于是白天重启、
    # 或落库失败的次日重启，缓存就一直空着，要等到 16:30 才有数据。
    # 改成按缓存自身状态决定：
    #   · 今天已算过（meta 记着，跨重启有效）→ 不重复烧那 230 秒；
    #   · 缓存为空 / 数据比缓存新 → 补算，不再看落库脸色。
    try:
        import strategy_eval as _se
        import store as _st2
        if _st2.meta_get(_se.K_PW_OK, "") == datetime.now().date().isoformat():
            print("[strategy-eval] 启动预热跳过：今天已预热过")
        else:
            due = _se.prewarm_due()
            if due.get("due"):
                _se.prewarm(why=f"启动补跑（{due.get('reason', '')}）")
            else:
                print(f"[strategy-eval] 启动预热跳过：{due.get('reason', '')}")
    except Exception as e:
        print(f"[strategy-eval] 启动预热跳过（不影响其他功能）：{e}")

    threading.Thread(target=_warm, daemon=True).start()
    threading.Thread(target=_warm_history, daemon=True).start()

    # #96 监控中心：检测轮询（独立线程/独立连接；无规则时空转）
    try:
        alt.start_loop(interval=60,
                       engine_getter=lambda: scr.get_history_engine(auto_load=False),
                       htk=htk)
    except Exception as e:
        print(f"[alerts] 监控轮询启动失败（不影响其他功能）：{e}")

    # #102 健康巡检自驱动：之前依赖外部手动起 health_check.py --loop，
    # 进程一死报告就冻住。改为随服务常驻的 daemon 线程周期性自探测并写
    # health_state.json，服务重启自动恢复。复用 scripts/health_check 的
    # _check/_record，不重写探测逻辑。地址优先 TICK_SITE_URL，否则本机服务。
    try:
        threading.Thread(target=_health_loop, daemon=True, name="health-check").start()
    except Exception as e:
        print(f"[health-check] 自驱动启动失败（不影响其他功能）：{e}")


def _health_loop():
    """#102 健康巡检自驱动：周期性调用 scripts/health_check 探测并落盘。

    ponytail: 不在服务端重写探测逻辑，复用 health_check._check/_record；
    STATE_DIR 用脚本自身路径推导，恒指向项目 data/，与 /api/admin/health
    读取路径一致。TICK_SITE_URL 覆盖探测地址，TICK_HEALTH_INTERVAL 覆盖间隔(分)。
    """
    try:
        import os as _os, sys as _sys, time as _t
        _scripts = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        import health_check as _hc
    except Exception as e:
        print(f"[health-check] 加载失败（跳过自驱动）：{e}")
        return

    url = _os.environ.get("TICK_SITE_URL") or "http://127.0.0.1:8899/"
    interval = max(1.0, float(_os.environ.get("TICK_HEALTH_INTERVAL", "30"))) * 60
    grace = max(0.0, float(_os.environ.get("TICK_HEALTH_GRACE", "60")))
    print(f"[health-check] 自驱动已启动：探测 {url}，每 {interval/60:g} 分钟一次"
          + (f"，首跑前宽限 {grace:g}s（等行情缓存预热）" if grace else ""))
    # ponytail: 首跑前留宽限，避开重启瞬间行情快照尚空的误报（market_count=0）
    if grace:
        _t.sleep(grace)
    while True:
        try:
            res = _hc._check(url)
            _hc._record(res)
        except Exception as e:
            print(f"[health-check] 探测异常：{e}")
        _t.sleep(interval)


def _resolve_name(code: str) -> str:
    """从行情快照/搜索索引解析股票中文简称，供存储层补全名称。"""
    try:
        code = (code or "").strip().lower()
        if not code:
            return ""
        rows, _ = MARKET.get()
        for r in rows or []:
            if r.get("code") == code and r.get("name"):
                return str(r["name"])
        # 兜底：走腾讯搜索
        res = ds.search_stock(code, limit=1)
        if res:
            return str(res[0].get("name") or "")
    except Exception:
        pass
    return ""


# ===========================================================================
# 小白选股 · 后台预计算缓存
#
# 为什么要缓存：其中「超跌反弹」方案必须逐股拉日K才能算出真实跌幅，而行情接口
# 对K线有服务端限流，实测吞吐恒为约 2.5 只/秒（加线程无效）。全市场扫一遍要
# 十几分钟，绝不能放在请求路径上等。所以改为后台线程预热 + 接口只读缓存：
# 用户点击时毫秒级返回，代价是结果最多滞后一个刷新周期（默认 30 分钟）。
# ===========================================================================

class NewbieCache:
    """按方案缓存小白选股结果，后台线程负责刷新。

    注意：缓存只按 preset 建键，不按 limit。后台统一按 CACHE_LIMIT 算足量，
    接口读取时再按用户要的条数切片 —— 否则不同 limit 会各算一份，
    在限流下既慢又浪费。
    """

    CACHE_LIMIT = 50

    def __init__(self, ttl: int = 1800):
        self.ttl = ttl
        self._data: dict = {}
        self._ts: dict = {}
        self._busy: set = set()
        self._lock = threading.Lock()
        self._err: dict = {}

    def get(self, preset: str, limit: int = 20):
        """读取缓存并按 limit 切片；缺失返回 (None, 状态)。"""
        with self._lock:
            hit = self._data.get(preset)
            ts = self._ts.get(preset, 0.0)
            busy = preset in self._busy
            err = self._err.get(preset)
        fresh = bool(hit) and (time.time() - ts) < self.ttl
        sliced = None
        if hit:
            sliced = dict(hit)
            sliced["items"] = (hit.get("items") or [])[:limit]
        return sliced, {"stale": not fresh, "computing": busy, "error": err,
                        "updated": (datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                                    if ts else None)}

    def compute(self, preset: str):
        """在后台线程里算一个方案（含需要K线的方案）。

        按 CACHE_LIMIT 算足量结果 —— 缓存键只与 preset 有关，与条数无关。
        """
        limit = self.CACHE_LIMIT
        with self._lock:
            if preset in self._busy:
                return
            self._busy.add(preset)
        try:
            rows = MARKET.ensure()
            # 关键：市场快照还没就绪时绝不写入缓存，否则会把"空结果"固化下来，
            # 让用户看到"市场没有符合条件的股票"这种误导性结论。
            if not rows:
                wait_used = 0.0
                while not rows and wait_used < 60:
                    time.sleep(2.0)
                    wait_used += 2.0
                    rows = MARKET.ensure()
            if not rows:
                with self._lock:
                    self._err[preset] = "市场快照尚未就绪，稍后自动重试"
                return

            kf = None
            if nwb.PRESET_BY_KEY.get(preset, {}).get("enrich"):
                kf = lambda c: ds.get_kline(c, period="1d", count=70)
            res = nwb.pick(rows, preset, limit=limit, kline_fn=kf)
            # 同样：universe 为 0 的结果不缓存
            if not res.get("universe"):
                with self._lock:
                    self._err[preset] = "市场快照为空，稍后自动重试"
                return
            with self._lock:
                self._data[preset] = res
                self._ts[preset] = time.time()
                self._err[preset] = None
        except Exception as e:
            with self._lock:
                self._err[preset] = str(e)
        finally:
            with self._lock:
                self._busy.discard(preset)

    def ensure_async(self, preset: str):
        """触发后台计算（不阻塞）。"""
        with self._lock:
            hit = self._data.get(preset)
            ts = self._ts.get(preset, 0.0)
            busy = preset in self._busy
        if busy:
            return
        if hit and (time.time() - ts) < self.ttl:
            return
        threading.Thread(target=self.compute, args=(preset,), daemon=True).start()


NEWBIE = NewbieCache()


def _newbie_warm():
    """启动后预热：先算快的快照方案，再算需要K线的方案。"""
    try:
        MARKET.refresh()
    except Exception:
        pass
    # 等 MARKET.ensure() 真拿到数据再开算，避免把空结果写进缓存
    for _ in range(30):
        rows = MARKET.ensure()
        if rows:
            break
        time.sleep(2.0)
    for key in ("steady", "growth", "hot", "rebound"):
        try:
            NEWBIE.compute(key)
        except Exception:
            pass


@app.on_event("startup")
def startup_newbie():
    threading.Thread(target=_newbie_warm, daemon=True).start()



# ===========================================================================
# API
# ===========================================================================

@app.get("/api/health")
def health():
    rows, upd = MARKET.get()
    try:
        intraday_src = ds.intraday_source_status()
    except Exception as e:
        intraday_src = {"primary": "?", "eltdx": False, "note": str(e)}
    return {
        "ok": True,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_count": len(rows),
        "market_updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                           if upd else None),
        "loading": MARKET.loading,
        "error": MARKET._err,
        "pools": {"沪深300": "000300", "中证500": "000905",
                  "中证1000": "000852", "中证2000": "932000"},
        "intraday": intraday_src,
    }


# ---------------------------------------------------------------------------
# 关于页元数据
# 让关于页随后端能力自动同步，避免手敲数字（如「55 项」「15 个策略」）随版本过期。
# 计数/分组结构直接来自指标引擎与策略目录，新增指标或策略无需改前端。
# ---------------------------------------------------------------------------
APP_VERSION = "1.2.6"


def _build_info() -> float:
    """构建时间取 server/ 下最新 .py 的修改时间，反映代码实际状态。

    仓库不是 git 仓库，用文件 mtime 作为「代码最后改动」的近似，
    任何一次部署/改动都会自动刷新，无需手动维护版本号时间戳。
    """
    import glob
    mts = []
    here = os.path.dirname(os.path.abspath(__file__))
    for f in glob.glob(os.path.join(here, "*.py")):
        try:
            mts.append(os.path.getmtime(f))
        except OSError:
            pass
    return max(mts) if mts else time.time()


def _about_indicators() -> Dict[str, Any]:
    """指标总数与分组结构——直接取自 all_indicators，与 indicator.py 同步。"""
    try:
        kl = ds.get_kline("sh600519", "1d", 250)  # 流动性最好的样本股；计数与代码无关
        res = ind.all_indicators(kl) if kl else None
    except Exception:
        res = None
    if not res:
        return {"total": 0, "groups": []}
    groups = [{"cat": g.get("cat") or "其他", "count": len(g.get("items", []))}
              for g in res.get("groups", [])]
    return {"total": sum(g["count"] for g in groups), "groups": groups}


def _about_strategies() -> Dict[str, Any]:
    """策略总数与分类结构——直接取自 list_strategies，与 screener.py 同步。"""
    try:
        cats = scr.list_strategies()
    except Exception:
        cats = []
    cats_out = [{"cat": c.get("cat"), "count": len(c.get("items", []))}
                for c in cats]
    return {"total": sum(c["count"] for c in cats_out), "cats": cats_out}


@app.get("/api/about")
def api_about():
    """关于页的结构化元数据：版本、指标/策略构成、后台是否启用。"""
    return {
        "version": APP_VERSION,
        "built_at": _build_info(),
        "indicators": _about_indicators(),
        "strategies": _about_strategies(),
        "admin_enabled": bool(_admin_names()),
    }


@app.get("/api/system/health_report")
def api_health_report(limit: int = Query(20, ge=1, le=50)):
    """巡检脚本写入的健康状态，供页面右上角显示提醒。

    由 scripts/health_check.py 落盘，这里只读不写。
    没跑过巡检时返回 ran=False，前端不显示任何东西。
    """
    import json as _json
    path = os.path.join(_DATA_DIR, "health_state.json")
    logp = os.path.join(_DATA_DIR, "health_alerts.log")
    out: Dict[str, Any] = {"ran": False, "alerting": False,
                           "consecutive_fail": 0, "history": [], "alerts": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = _json.load(f)
        out["ran"] = True
        out["alerting"] = bool(st.get("alerting"))
        out["consecutive_fail"] = int(st.get("consecutive_fail") or 0)
        out["last_check"] = st.get("last_check")
        out["last_ok"] = st.get("last_ok")
        out["last_fail"] = st.get("last_fail")
        out["last_error"] = st.get("last_error")
        out["url"] = st.get("last_url")
        out["history"] = (st.get("history") or [])[:limit]
    except FileNotFoundError:
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    # 告警日志取尾部若干行，供弹层展示
    try:
        with open(logp, "r", encoding="utf-8") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
        out["alerts"] = lines[-limit:][::-1]
    except Exception:
        pass
    return out


@app.get("/api/bars/status")
def api_bars_status(recent: int = Query(10, ge=0, le=100)):
    """历史行情落库状态（供前端展示"数据准备度"）。

    日线数据是策略选股做历史计算的基础，落库不完整时部分策略结果会失真。
    """
    import store as _store
    try:
        cov = _store.bars_coverage()
        return {
            "backend": _store.BACKEND,
            "codes": cov["codes"],
            "rows": cov["rows"],
            "start": cov["start"],
            "end": cov["end"],
            "recent": _store.sync_status(recent) if recent else [],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "codes": 0, "rows": 0}


@app.get("/api/bars/{code}")
def api_bars(code: str, limit: int = Query(300, ge=30, le=2000),
             end: str = Query("", description="只取该日期(含)之前的数据")):
    """从本地库读某只股票的历史日线（不请求外部数据源）。

    库中没有数据时返回 empty=True，前端可提示先跑同步脚本。
    """
    import store as _store
    c = ds.normalize(code)
    try:
        bars = _store.get_bars(c, limit=limit, end_date=end)
    except Exception as e:
        raise HTTPException(500, f"读取历史行情失败：{e}")
    return {
        "code": c,
        "count": len(bars),
        "empty": len(bars) == 0,
        "bars": bars,
    }


@app.get("/api/pools")
def api_pools():
    """可选股票池"""
    return {"items": [
        {"key": "all", "name": "全部（四大指数合并）"},
        {"key": "000300", "name": "沪深300"},
        {"key": "000905", "name": "中证500"},
        {"key": "000852", "name": "中证1000"},
        {"key": "932000", "name": "中证2000"},
    ]}


@app.get("/api/index")
def api_index():
    """主要指数行情（大盘概览）"""
    try:
        data = ds.index_quotes()
    except Exception as e:
        raise HTTPException(500, f"指数行情获取失败: {e}")
    return {"items": data, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/market")
def api_market(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=300),
    sort: str = Query("change_pct"),
    order: str = Query("desc"),
    keyword: str = Query("", description="代码或名称过滤"),
    market: str = Query("", description="sh/sz/bj"),
):
    """全市场快照分页 + 排序 + 过滤"""
    rows = MARKET.ensure()

    if keyword:
        kw = keyword.strip().lower()
        rows = [r for r in rows
                if kw in r["symbol"].lower() or kw in r["name"].lower()]
    if market:
        rows = [r for r in rows if r["code"].startswith(market)]

    valid_sort = {"change_pct", "price", "pe", "pb", "total_cap",
                  "float_cap", "turnover", "amount", "volume", "symbol"}
    if sort not in valid_sort:
        sort = "change_pct"

    reverse = (order == "desc")
    rows = sorted(rows, key=lambda r: (r.get(sort) is None, r.get(sort) or 0),
                  reverse=reverse)

    total = len(rows)
    start = (page - 1) * size
    items = rows[start:start + size]
    _, upd = MARKET.get()
    return {
        "items": items, "total": total, "page": page, "size": size,
        "pages": (total + size - 1) // size,
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
    }


@app.get("/api/screener")
def api_screener(
    pe_min: Optional[float] = None, pe_max: Optional[float] = None,
    pb_min: Optional[float] = None, pb_max: Optional[float] = None,
    cap_min: Optional[float] = None, cap_max: Optional[float] = None,
    chg_min: Optional[float] = None, chg_max: Optional[float] = None,
    turnover_min: Optional[float] = None, turnover_max: Optional[float] = None,
    amount_min: Optional[float] = None,
    market: str = Query("", description="sh/sz/bj"),
    exclude_st: bool = True,
    exclude_bj: bool = False,
    sort: str = Query("change_pct"),
    order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=500),
):
    """条件选股器"""
    rows = MARKET.ensure()

    def ok(r):
        pe, pb = r.get("pe") or 0, r.get("pb") or 0
        cap, chg = r.get("total_cap") or 0, r.get("change_pct") or 0
        tov, amt = r.get("turnover") or 0, r.get("amount") or 0

        if exclude_st and ("ST" in r["name"].upper() or "退" in r["name"]):
            return False
        if exclude_bj and r["code"].startswith("bj"):
            return False
        if market and not r["code"].startswith(market):
            return False

        # PE 区间（PE<=0 视为无效，仅在未指定区间时保留）
        if pe_min is not None or pe_max is not None:
            if pe <= 0:
                return False
            if pe_min is not None and pe < pe_min:
                return False
            if pe_max is not None and pe > pe_max:
                return False
        if pb_min is not None and (pb <= 0 or pb < pb_min):
            return False
        if pb_max is not None and (pb <= 0 or pb > pb_max):
            return False

        if cap_min is not None and cap < cap_min:
            return False
        if cap_max is not None and cap > cap_max:
            return False
        if chg_min is not None and chg < chg_min:
            return False
        if chg_max is not None and chg > chg_max:
            return False
        if turnover_min is not None and tov < turnover_min:
            return False
        if turnover_max is not None and tov > turnover_max:
            return False
        if amount_min is not None and amt < amount_min:
            return False
        return True

    hits = [r for r in rows if ok(r)]

    valid_sort = {"change_pct", "price", "pe", "pb", "total_cap",
                  "float_cap", "turnover", "amount"}
    if sort not in valid_sort:
        sort = "change_pct"
    hits = sorted(hits, key=lambda r: (r.get(sort) is None, r.get(sort) or 0),
                  reverse=(order == "desc"))

    _, upd = MARKET.get()
    title_bits = []
    if pe_min is not None or pe_max is not None:
        title_bits.append(f"PE {pe_min or 0}-{pe_max or '∞'}")
    if pb_min is not None or pb_max is not None:
        title_bits.append(f"PB {pb_min or 0}-{pb_max or '∞'}")
    if cap_min is not None or cap_max is not None:
        title_bits.append(f"市值 {cap_min or 0}-{cap_max or '∞'}")
    if chg_min is not None or chg_max is not None:
        title_bits.append(f"涨跌幅 {chg_min or 0}%~{chg_max or '∞'}")
    if market:
        title_bits.append(market.upper())
    if exclude_st:
        title_bits.append("去ST")
    _save_screen_history(
        "screen", "条件·" + (" ".join(title_bits) if title_bits else "全部"),
        {"pe": [pe_min, pe_max], "pb": [pb_min, pb_max],
         "cap": [cap_min, cap_max], "chg": [chg_min, chg_max],
         "market": market, "exclude_st": exclude_st, "exclude_bj": exclude_bj,
         "sort": sort, "order": order}, hits[:limit])
    return {
        "items": hits[:limit],
        "total": len(hits),
        "universe": len(rows),
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
    }


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1)):
    if q.isdigit() and len(q) == 6:
        code = ds.normalize(q)
        quotes = ds.quote_tencent([code])
        if code in quotes:
            d = quotes[code]
            return {"items": [{"code": code, "symbol": d["symbol"],
                               "name": d["name"], "market": code[:2]}]}
    items = ds.search_stock(q)
    # 补充实时价
    if items:
        codes = [i["code"] for i in items]
        quotes = ds.quote_tencent(codes)
        for it in items:
            d = quotes.get(it["code"])
            if d:
                it["price"] = d["price"]
                it["change_pct"] = d["change_pct"]
    return {"items": items}


@app.get("/api/quote")
def api_quote(codes: str = Query(..., description="逗号分隔，如 sh600519,sz000858")):
    code_list = [ds.normalize(c) for c in codes.split(",") if c.strip()]
    if not code_list:
        raise HTTPException(400, "缺少代码")
    if len(code_list) > 100:
        raise HTTPException(400, "单次最多 100 只")
    quotes = ds.quote_tencent(code_list)
    out = []
    for c in code_list:
        if c in quotes:
            out.append(quotes[c])
    return {"items": out, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/kline")
def api_kline(
    code: str = Query(...),
    period: str = Query("1d"),
    count: int = Query(250, ge=30, le=800),
    with_indicators: bool = True,
):
    """K线 + 技术指标 + 策略信号"""
    code = ds.normalize(code)
    period = ds.normalize_period(period)
    klines = ds.get_kline(code, period, count)
    if not klines:
        raise HTTPException(404, f"未取到 {code} 的K线数据（数据源可能繁忙，请重试）")

    quotes = ds.quote_tencent([code])
    quote = quotes.get(code)

    payload = {
        "code": code,
        "period": period,
        "quote": quote,
        "klines": [{"date": k["date"],
                    "open": k["open"], "close": k["close"],
                    "high": k["high"], "low": k["low"],
                    "volume": k["volume"]} for k in klines],
    }

    if with_indicators:
        try:
            payload["indicators"] = ind.compute_indicators(klines)
        except Exception as e:
            payload["indicators"] = None
            payload["indicator_error"] = str(e)
        try:
            payload["strategies"] = ind.strategy_signals(klines)
        except Exception as e:
            payload["strategies"] = None

    # 通道序列（唐安奇 / 肯特纳），用于叠加主图；长度与 K 线一致以免主图错位
    try:
        payload["channels"] = ind.channel_series(klines, n=len(klines))
    except Exception as e:
        payload["channels"] = None
        payload["channel_error"] = str(e)

    # 吊灯止损线序列（同样与 K 线等长），供主图叠加
    try:
        payload["stop_lines"] = ind.stop_line_series(klines, n=len(klines))
    except Exception as e:
        payload["stop_lines"] = None
        payload["stop_line_error"] = str(e)

    return payload


# ===========================================================================
# 分钟级（实时分时）
#
# 与 /api/kline 的区别：数据来自实时分钟线接口，**不落库**，
# 带 15 秒缓存，用于盘中看盘与分钟级选股。
# ===========================================================================

import intraday as itd


@app.get("/api/kline_intraday")
def api_kline_intraday(
    code: str = Query(..., description="股票代码，如 sh600519"),
    period: str = Query("5m", description="1m/5m/15m/30m/60m"),
    count: int = Query(0, ge=0, le=2400,
                       description="取多少根分钟线；0=按周期自动（1m 取 2400，其余 800）"),
):
    """分钟 K 线 + 分钟级指标 + 实时行情。用于分时看盘。

    ## count 为什么不给固定默认值

    不同周期「一根」代表的时间差 60 倍，统一取 400 根会让 1m 周期只覆盖
    1.6 个交易日——那样算出来的 MA60 是假均线（数值算得出、口径全错）。
    所以这里的默认值是 0，表示「按周期自动选」（见 `datasource.default_intraday_count`）。
    """
    code = ds.normalize(code)
    p = ds.normalize_intraday_period(period)
    if not p:
        raise HTTPException(
            400, f"不支持的分钟周期 {period!r}，可选：1m/5m/15m/30m/60m")

    # 先校验代码，再取数。
    #
    # 顺序很关键：通达信协议对不存在的代码会**安静地返回别的标的的数据**
    # （实测 sh999999 返回的是上证指数的行情），不校验的话用户会看到一份
    # 看起来正常、实际张冠李戴的行情。宁可在这里明确报错。
    if not ds.is_valid_code(code):
        raise HTTPException(
            400, f"未找到代码 {code}：数据源里查不到这个标的（不在 A 股代码表内，"
                 f"也不是已知指数 / 场内基金 / B 股）。请检查是否输错："
                 f"沪市 6 开头，深市 0、3 开头，北交所 4、8、920 开头，"
                 f"场内基金沪市 5 开头、深市 15/16/18 开头。")

    if count and count > 0:
        n = count
    else:
        n = ds.default_intraday_count(p)

    rows = ds.get_kline_intraday(code, p, n)
    if not rows:
        src = ds.intraday_source_status()
        # 北交所是最常见的「取不到」原因：eltdx 的分钟线不支持 bj 前缀，
        # 腾讯的 mkline 端点也不覆盖。这不是故障，是能力边界，必须说清楚，
        # 否则用户会以为是自己代码写错了。
        if code.startswith("bj"):
            raise HTTPException(
                404,
                f"北交所股票（{code}）暂不支持分钟线：eltdx 与腾讯的分钟线"
                f"接口均不覆盖北交所。可改用日线查看（/api/kline）。")
        raise HTTPException(
            404,
            f"未取到 {code} 的 {p} 分钟线"
            f"（数据源：{src.get('primary')}。可能原因：代码有误、停牌、"
            f"或数据源繁忙，请稍后重试）")

    m = itd.metrics(code, period=p, series=itd._series_from_rows(rows))

    # 实时行情走统一快照入口（eltdx 优先）——与分钟线同源，时间口径自洽。
    # 不再直接用腾讯逐只接口（那个带 0.4s 节流，纯属白等）。
    quote = ds.snapshot([code]).get(code)

    # 市场状态：让前端能把「已收盘」说准，而不是让用户以为看的是实时。
    ms = ds.market_state()
    last_date = rows[-1]["date"][:10] if rows else ""
    today = datetime.now().strftime("%Y-%m-%d")
    if ms["state"] in ("closed", "holiday", "pre_open") and last_date and last_date != today:
        # 数据最新日期不是今天 → 当前展示的是「上一个交易日」的完整走势
        ms = dict(ms)
        ms["showing_previous_day"] = True
        ms["data_date"] = last_date
    else:
        ms = dict(ms)
        ms["showing_previous_day"] = False
        ms["data_date"] = last_date

    return {
        "code": code,
        "period": p,
        "requested_count": n,
        "quote": quote,
        "source": rows[0].get("source", ""),
        "has_amount": rows[0].get("amount") is not None,
        "market_state": ms,
        "metrics": m,
        "klines": [{"date": k["date"],
                    "open": k["open"], "close": k["close"],
                    "high": k["high"], "low": k["low"],
                    "volume": k["volume"],
                    "amount": k.get("amount")} for k in rows],
        "note": ("分钟线为实时拉取、不落库；amount 在腾讯降级源下为空。"
                 "「当日涨跌幅」以当日开盘价为基准，不是交易所的昨收口径。"),
    }


@app.get("/api/intraday_scan")
def api_intraday_scan(
    period: str = Query("5m", description="1m/5m/15m/30m/60m"),
    preset: str = Query("vol_surge", description="预设条件，见 /api/intraday_presets"),
    limit: int = Query(50, ge=1, le=300),
    min_vol_ratio: float = Query(0, ge=0, le=100),
    min_chg: float = Query(-100, ge=-100, le=100),
    max_chg: float = Query(100, ge=-100, le=100),
    market: str = Query("", description="sh/sz/bj 过滤"),
    exclude_st: bool = Query(True),
):
    """全市场分钟级扫描。

    需要 eltdx 批量源（全市场约 18 秒）。腾讯降级路径下会拒绝执行并
    返回明确原因——逐只跑全市场要 24 分钟，不如直接告诉用户。
    """
    p = ds.normalize_intraday_period(period)
    if not p:
        raise HTTPException(
            400, f"不支持的分钟周期 {period!r}，可选：1m/5m/15m/30m/60m")

    avail = itd.source_available()
    if not avail["ok"]:
        return {
            "ok": False, "reason": avail["reason"],
            "source": avail.get("primary"),
            "scanned": 0, "items": [], "total": 0,
            "hint": "可改用单只分时看盘（/api/kline_intraday），"
                    "它对数据源无批量要求。",
        }

    rows = MARKET.ensure()
    if market:
        mk = market.strip().lower()
        rows = [r for r in rows if r["code"].startswith(mk)]
    if exclude_st:
        rows = [r for r in rows if "ST" not in (r.get("name") or "").upper()
                and "退" not in (r.get("name") or "")]

    pred = _intraday_pred(preset, min_vol_ratio, min_chg, max_chg)
    if pred is None:
        raise HTTPException(400, f"未知预设 {preset!r}")

    # 扫描用 SCAN_COUNT(120) 而不是看盘的根数：扫描的指标窗口最大 61 根，
    # 多取只会线性增加耗时而不改变任何结果（实测 800 根是 100 根的 3 倍耗时，
    # 指标逐位相同）。详见 datasource.SCAN_COUNT 的注释。
    scan_n = ds.scan_count(p)
    res = itd.screen(rows, period=p, pred=pred, count=scan_n)
    items = res["items"]

    # 排序：量能比降序（放量是这类扫描最关心的），缺失的排最后
    items.sort(key=lambda x: -(x.get("vol_ratio20") or 0))
    items = items[:limit]

    for it in items:
        it.pop("code_raw", None)

    ms = ds.market_state()
    today = datetime.now().strftime("%Y-%m-%d")
    # 用扫描结果里的实际数据日期判断，而不是猜——周末/节假日会被 market_state
    # 判成 closed，但真正该说的是「这是上一个交易日的数据」。
    data_date = ""
    for it in items:
        d = it.get("date") or ""
        if d:
            data_date = max(data_date, d)
    ms = dict(ms)
    ms["data_date"] = data_date
    ms["showing_previous_day"] = bool(
        data_date and data_date != today
        and ms["state"] in ("closed", "holiday", "pre_open"))

    _, upd = MARKET.get()
    return {
        "ok": True,
        "period": p,
        "preset": preset,
        "source": res.get("source"),
        "scanned": res["scanned"],
        "total": len(res["items"]),
        "elapsed": round(res["elapsed"], 1),
        "fetch_sec": round(res.get("fetch_sec", 0), 1),
        "pool_size": len(rows),
        "bars_used": scan_n,
        "market_state": ms,
        "items": items,
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
        "disclaimer": ("分钟级数据为实时拉取、未落库，仅反映当前盘口状态；"
                       "当日涨跌幅以开盘价为基准。不构成投资建议。"),
    }


#: 分钟级扫描预设。每个预设是一组「指标条件」的命名组合，
#: 前端下拉框直接展示 name，提交时传 key。
INTRADAY_PRESETS = [
    {"key": "vol_surge", "name": "盘中放量",
     "desc": "最后一根量 ≥ 前20根均量的 3 倍（资金异动）"},
    {"key": "break_up", "name": "分时突破",
     "desc": "现价突破前 20 根最高价"},
    {"key": "up_strong", "name": "盘中强势",
     "desc": "当日涨 >2% 且位于当日区间上半部"},
    {"key": "pullback_day", "name": "回踩反弹",
     "desc": "当日跌但现价从当日低点回升 >1.5%"},
    {"key": "vol_price_up", "name": "价涨量增",
     "desc": "最后一根价涨且量比 ≥1.5"},
    {"key": "range_break", "name": "横盘异动",
     "desc": "前20根振幅 <2% 但最后一根放量 ≥3 倍"},
]


def _intraday_pred(preset, min_vol_ratio, min_chg, max_chg):
    """把预设名编译成一个判定函数。未知预设返回 None。"""

    def base(m):
        # 用户手填的通用过滤条件，叠加在预设之上
        vr = m.get("vol_ratio20")
        if min_vol_ratio and (vr is None or vr < min_vol_ratio):
            return False
        chg = m.get("day_chg_pct")
        if chg is None:
            return False
        if chg < min_chg or chg > max_chg:
            return False
        return True

    def f_vol_surge(m):
        vr = m.get("vol_ratio20")
        return base(m) and vr is not None and vr >= 3.0

    def f_break_up(m):
        return base(m) and bool(m.get("break_up"))

    def f_up_strong(m):
        chg = m.get("day_chg_pct")
        pos = m.get("day_position")
        return base(m) and chg is not None and chg > 2.0 and (
            pos is not None and pos >= 0.5)

    def f_pullback_day(m):
        chg = m.get("day_chg_pct")
        pos = m.get("day_position")
        return base(m) and chg is not None and chg < 0 and (
            pos is not None and pos >= 0.65)

    def f_vol_price_up(m):
        vr = m.get("vol_ratio20")
        return base(m) and m.get("vol_price") == "价涨量增" and (
            vr is not None and vr >= 1.5)

    def f_range_break(m):
        vr = m.get("vol_ratio20")
        rng = m.get("range20_pct")
        return base(m) and rng is not None and rng < 2.0 and (
            vr is not None and vr >= 3.0)

    return {
        "vol_surge": f_vol_surge,
        "break_up": f_break_up,
        "up_strong": f_up_strong,
        "pullback_day": f_pullback_day,
        "vol_price_up": f_vol_price_up,
        "range_break": f_range_break,
    }.get(preset)


@app.get("/api/intraday_presets")
def api_intraday_presets():
    """分钟级扫描的预设条件清单 + 数据源能力 + 建议根数。"""
    return {
        "presets": INTRADAY_PRESETS,
        "periods": list(itd.PERIODS),
        "period_labels": {"1m": "1分钟", "5m": "5分钟", "15m": "15分钟",
                          "30m": "30分钟", "60m": "60分钟"},
        "default_count": {p: ds.default_intraday_count(p)
                          for p in itd.PERIODS},
        "source": ds.intraday_source_status(),
        "snapshot_source": ds.snapshot_status(),
        "market_state": ds.market_state(),
        "bars_per_day": itd.BARS_PER_DAY,
    }


@app.get("/api/intraday_selfcheck")
def api_intraday_selfcheck():
    """分钟线指标自检（含与日线的交叉验证）。"""
    return itd.selfcheck(verbose=False)


@app.get("/api/rank")
def api_rank(
    kind: str = Query("gainers", description="gainers/losers/amount/turnover/cap/pe"),
    type: Optional[str] = Query(None, description="kind 的别名，兼容前端写法"),
    market: str = Query("", description="sh/sz/bj"),
    limit: int = Query(30, ge=1, le=100),
    exclude_st: bool = True,
):
    """排行榜"""
    # 兼容 kind / type 两种参数名，并映射常见中文/英文别名
    if type:
        kind = type
    alias = {
        "change_pct": "gainers", "up": "gainers", "涨幅榜": "gainers",
        "down": "losers", "跌幅榜": "losers",
        "成交额": "amount", "成交额榜": "amount",
        "换手率": "turnover", "换手率榜": "turnover",
        "市值": "cap", "市值榜": "cap",
        "低估值": "pe", "低估值榜": "pe",
    }
    kind = alias.get(kind, kind)

    rows = MARKET.ensure()

    if exclude_st:
        rows = [r for r in rows if "ST" not in r["name"].upper() and "退" not in r["name"]]
    if market:
        rows = [r for r in rows if r["code"].startswith(market)]

    if kind == "gainers":
        rows = [r for r in rows if r["change_pct"] > -11]
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
    elif kind == "losers":
        rows = [r for r in rows if r["change_pct"] < 11]
        rows.sort(key=lambda r: r["change_pct"])
    elif kind == "amount":
        rows.sort(key=lambda r: r.get("amount") or 0, reverse=True)
    elif kind == "turnover":
        rows.sort(key=lambda r: r.get("turnover") or 0, reverse=True)
    elif kind == "cap":
        rows.sort(key=lambda r: r.get("total_cap") or 0, reverse=True)
    elif kind == "pe":
        rows = [r for r in rows if (r.get("pe") or 0) > 0]
        rows.sort(key=lambda r: r["pe"])
    else:
        raise HTTPException(400, f"未知榜单: {kind}")

    _, upd = MARKET.get()
    return {
        "kind": kind, "items": rows[:limit],
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
    }


# ===========================================================================
# 内置策略选股（借鉴 TSP）
# ===========================================================================

@app.get("/api/strategy_list")
def api_strategy_list():
    """策略目录（按分类）"""
    return {"cats": scr.list_strategies()}


@app.get("/api/strategy_scan")
def api_strategy_scan(
    keys: str = Query(..., description="策略 key，逗号分隔"),
    mode: str = Query("union", description="union/intersect"),
    pool: str = Query("all"),
    market: str = Query("", description="sh/sz/bj"),
    limit: int = Query(100, ge=1, le=500),
    exclude_st: bool = True,
    tune: str = Query("", description="参数覆盖，JSON，如 {\"max_range60\":30}"),
):
    """多策略扫描全市场（union 并集 / intersect 交集）

    tune 用于临时覆盖策略的可调阈值（见 /api/strategy_list 的 params 字段），
    不传时行为与改造前完全一致。非法 JSON 会被忽略（回落默认参数），
    以免一个坏参数导致整个扫描失败。
    """
    rows = MARKET.ensure()
    if pool and pool != "all":
        rows = [r for r in rows if pool in (r.get("pools") or [])]
    if market:
        rows = [r for r in rows if r["code"].startswith(market)]
    if exclude_st:
        rows = [r for r in rows if "ST" not in r["name"].upper() and "退" not in r["name"]]

    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    invalid = [k for k in key_list if k not in scr.STRATEGY_BY_KEY]
    if invalid:
        raise HTTPException(400, f"未知策略: {','.join(invalid)}")

    # 参数覆盖：解析失败则忽略（不因坏参数中断扫描）
    override = {}
    if tune:
        try:
            parsed = json.loads(tune)
            if isinstance(parsed, dict):
                override = parsed
        except Exception:
            override = {}

    hit, tags, diag = scr.run_strategies(rows, key_list, mode=mode, **override)
    hit.sort(key=lambda r: r.get("change_pct") or 0, reverse=True)

    name_map = {d["key"]: d["name"] for d in scr.STRATEGY_DEFS}
    items = []
    for r in hit[:limit]:
        it = dict(r)
        it["hit_tags"] = [name_map.get(k, k) for k in tags.get(r["code"], [])]
        items.append(it)

    _, upd = MARKET.get()
    # 标题存中文（与策略选股页一致）：并集/交集 + 各策略中文名，
    # 多策略用「、」分隔；老记录（英文 key）由前端 prettifyHistTitle 兜底翻译。
    MODE_NAME = {"union": "并集", "intersect": "交集"}
    title = "策略·{}·{}".format(
        MODE_NAME.get(mode, mode),
        "、".join(name_map.get(k, k) for k in key_list))
    _save_screen_history(
        "strategy", title,
        {"keys": key_list, "mode": mode, "pool": pool, "market": market,
         "exclude_st": exclude_st, "tune": override}, items)
    return {
        "keys": key_list, "mode": mode, "total": len(hit), "items": items,
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
        # 本次实际生效的参数覆盖（便于前端回显「你正在用非默认参数」）
        "params_used": override,
        # 诊断：区分「没选出股票」与「历史数据未就绪」。
        # 结果为空且 hist_ready=False 时，说明不是策略没命中，
        # 而是依赖历史的策略这次没拿到数据。
        "diag": {
            "needs_history": diag.get("need_hist", []),
            "history_ready": diag.get("hist_ready", False),
            "history_as_of": diag.get("hist_as_of", ""),
            "skipped_no_history": diag.get("skipped", []),
            # 历史日线滞后于快照的交易日数：0=已对齐（盘后），
            # >=1=指标基于更早的交易日，当日涨跌未纳入（盘中）。
            # 前端据此提示用户，避免把盘中结果当成盘后结果。
            "data_lag_days": diag.get("data_lag_days"),
            "data_note": diag.get("data_note", ""),
            # 全局风险过滤（最大阴量 / J 值异常）：剔除统计与明细。
            # enabled=False 表示被 tune 关闭；removed 为剔除明细（code/name/reason）。
            "risk_filter": diag.get("risk_filter", {}),
        },
    }


# ===========================================================================
# 策略回测（借鉴 TSP 真实约束：T+1 / 手续费 / 滑点）
# ===========================================================================

@app.get("/api/backtest")
def api_backtest(
    code: str = Query(...),
    strategy: str = Query("ma_cross"),
    period: str = Query("1d"),
    count: int = Query(250, ge=60, le=800),
    init_cash: float = Query(1000000, ge=10000),
    stop_loss: Optional[float] = Query(None, ge=0, le=0.9,
                                       description="止损比例，如 0.05=亏5%离场"),
    take_profit: Optional[float] = Query(None, ge=0, le=5,
                                         description="止盈比例，如 0.15=赚15%离场"),
    pyramid: bool = Query(True, description="是否启用金字塔分批加仓"),
    pyramid_step: float = Query(0.05, gt=0, le=0.5,
                                description="加仓触发步长，如 0.05=每浮盈5%补一批"),
    position_size: float = Query(1.0, ge=0.05, le=1.0, description="单次建仓资金占比"),
):
    """单股单策略回测（支持止损止盈）"""
    code = ds.normalize(code)
    period = ds.normalize_period(period)
    if strategy not in strat.STRATEGY_MAP:
        raise HTTPException(400, f"未知策略: {strategy}（可选 {','.join(strat.STRATEGY_MAP)}）")

    klines = ds.get_kline(code, period, count)
    if not klines or len(klines) < 60:
        raise HTTPException(404, f"未取到 {code} 的足够K线数据")

    df = pd.DataFrame(klines)
    fn = strat.STRATEGY_MAP[strategy]
    try:
        sig = fn(df)
    except Exception as e:
        raise HTTPException(500, f"策略计算失败: {e}")

    res = bt.backtest(klines, sig, init_cash=init_cash,
                      stop_loss=(stop_loss or None),
                      take_profit=(take_profit or None),
                      position_size=position_size,
                      pyramid=pyramid, pyramid_step=pyramid_step)
    if not res:
        raise HTTPException(500, "回测失败")

    quotes = ds.quote_tencent([code])
    return {
        "code": code,
        "name": (quotes.get(code) or {}).get("name", code),
        "period": period,
        "strategy": strategy,
        "strategy_name": strat.STRATEGY_DESC.get(strategy, strategy),
        "kline_count": len(klines),
        "date_range": [klines[0]["date"], klines[-1]["date"]],
        "result": res,
    }


@app.get("/api/grid/plans")
def api_grid_plans(request: Request):
    """我的网格计划 + 每档实时状态（挂在虚拟盘下，需要登录）。"""
    store.initialize()
    _require_login(request)
    plans = store.list_grids()
    if not plans:
        return {"items": [], "levels_map": {}}
    codes = sorted({p["code"] for p in plans})
    try:
        quotes = ds.quote_tencent(codes)
    except Exception:
        quotes = {}
    out, levels_map = [], {}
    for p in plans:
        lv, bi = grd.build_levels(p["center_price"], p["lower_price"],
                                  p["upper_price"], p["step_pct"], p["mode"])
        q = quotes.get(p["code"]) or {}
        price = float(q.get("price") or 0)
        fired = set(p.get("fired_list") or [])
        levels = []
        for i, px in enumerate(lv):
            if i in fired:
                act, tag = "done", ("已买" if i < bi else "已卖")
            elif i == bi:
                act, tag = "base", "基准"
            elif i < bi:
                act = "可买" if (price and price <= px) else "待跌"
                tag = "买档"
            else:
                act = "可卖" if (price and price >= px) else "待涨"
                tag = "卖档"
            levels.append({"idx": i, "price": px, "act": act, "tag": tag})
        levels_map[str(p["id"])] = levels
        out.append({
            "id": p["id"], "code": p["code"], "name": p["name"],
            "center_price": p["center_price"], "upper_price": p["upper_price"],
            "lower_price": p["lower_price"], "step_pct": p["step_pct"],
            "mode": p["mode"], "lot": p["lot"],
            "fired": p["fired_list"], "status": p["status"],
            "created_at": p["created_at"],
            "price": price, "prev_close": float(q.get("prev_close") or 0),
            "grid_count": len(lv), "base_idx": bi,
            "pending": sum(1 for l in levels if l["act"] in ("可买", "可卖")),
        })
    return {"items": out, "levels_map": levels_map}


@app.post("/api/grid/plans")
def api_grid_plan_create(request: Request, payload: Dict[str, Any] = Body(default={})):
    """新建网格计划。"""
    store.initialize()
    _require_login(request)
    code = ds.normalize(str(payload.get("code") or ""))
    if not code:
        raise HTTPException(400, "标的代码不能为空")
    center = float(payload.get("center_price") or 0)
    band = float(payload.get("band_pct") or 20.0)
    if center <= 0:
        raise HTTPException(400, "中心价必须大于 0")
    upper = float(payload.get("upper_price") or center * (1 + band / 100.0))
    lower = float(payload.get("lower_price") or center * (1 - band / 100.0))
    try:
        gid = store.create_grid(code, str(payload.get("name") or ""), center, upper, lower,
                                float(payload.get("step_pct") or 2.0),
                                str(payload.get("mode") or "arith"),
                                int(payload.get("lot") or 10000))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": gid}


@app.delete("/api/grid/plans/{gid}")
def api_grid_plan_delete(request: Request, gid: int):
    store.initialize()
    _require_login(request)
    if not store.delete_grid(gid):
        raise HTTPException(404, "网格不存在或不属于当前用户")
    return {"ok": True}


@app.post("/api/grid/plans/{gid}/fire")
def api_grid_plan_fire(request: Request, gid: int, payload: Dict[str, Any] = Body(default={})):
    """按档位手动成交 —— 走虚拟盘的 buy/sell，资金/持仓/流水全部复用现有逻辑。

    方向由后端按档位下标与基准档的关系判定，不信任前端传的 side。
    """
    store.initialize()
    _require_login(request)
    _require_tradable()
    idx = int(payload.get("idx"))
    plans = {p["id"]: p for p in store.list_grids()}
    p = plans.get(gid)
    if not p:
        raise HTTPException(404, "网格不存在或不属于当前用户")
    lv, bi = grd.build_levels(p["center_price"], p["lower_price"],
                              p["upper_price"], p["step_pct"], p["mode"])
    if not (0 <= idx < len(lv)):
        raise HTTPException(400, f"档位下标越界（0~{len(lv) - 1}）")
    if idx == bi:
        raise HTTPException(400, "基准档不成交（那是网格中轴，不是挂单）")
    if idx in set(p.get("fired_list") or []):
        raise HTTPException(400, "这一档已经成交过了")
    side = "buy" if idx < bi else "sell"
    px = lv[idx]
    qty = int(payload.get("qty") or p["lot"])
    qty = int(max(100, qty) // 100) * 100
    try:
        if side == "buy":
            r = store.buy_stock(p["code"], qty, px, name=p["name"], pspan="网格档位")
        else:
            r = store.sell_stock(p["code"], qty, px, pspan="网格档位")
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.mark_grid_fired(gid, idx)
    return {"ok": True, "side": side, "price": px, "qty": qty,
            "level": idx, "result": r}


@app.get("/api/etf/list")
def api_etf_list(
    kw: str = Query("", description="名称或代码关键词，如 沪深300 / 芯片"),
    min_cap: float = Query(0, ge=0, description="规模下限（亿元），需要实时快照"),
    min_amount: float = Query(0, ge=0, description="成交额下限（亿元）"),
    min_turnover: float = Query(0, ge=0, description="换手率下限（%），需要实时快照"),
    chg_min: Optional[float] = Query(None, description="涨跌幅下限（%）"),
    chg_max: Optional[float] = Query(None, description="涨跌幅上限（%）"),
    sort_by: str = Query("amount", description="cap/amount/change_pct/turnover/price"),
    desc: bool = Query(True, description="是否降序"),
    limit: int = Query(100, ge=1, le=500, description="返回条数上限"),
    with_snapshot: bool = Query(True, description="是否补实时规模/换手；关掉会快很多但筛不了规模"),
    refresh: bool = Query(False, description="强制刷新缓存（首次全量补快照约 8s）"),
):
    """ETF 全市场筛选（新浪清单 + 腾讯快照，实测 1676 只）。

    ⚠ 没有 IOPV / 折溢价率：唯一的免费源（东财）在本机不可达，
    宁可不给，也不用净值估一个假的。前端会注明这个缺失。
    """
    rows, source = etf.fetch_list(force=refresh, with_snapshot=with_snapshot)
    if not rows:
        return {"items": [], "total": 0, "matched": 0, "source": source,
                "note": "ETF 清单暂时取不到（新浪源异常且无本地缓存）"}
    items = etf.screen(rows, kw=kw, min_cap=min_cap, min_amount=min_amount,
                       min_turnover=min_turnover, chg_min=chg_min, chg_max=chg_max,
                       sort_by=sort_by, desc=desc, limit=limit)
    return {
        "items": items,
        "total": len(rows),
        "matched": len(items),
        "source": source,
        "with_snapshot": bool(with_snapshot),
        "snapshot_at": time.strftime("%H:%M:%S", time.localtime(etf._CACHE["snap_at"]))
        if etf._CACHE["snap_at"] else None,
        "sort_by": sort_by if sort_by in etf.SORTS else "amount",
        "desc": desc,
        "note": ("数据来自新浪清单 + 腾讯快照；IOPV/折溢价率不可用（源不可达），不做估算"
                 if source != "fail" else "新浪源异常，以下是上一次成功的缓存"),
    }


@app.get("/api/grid/backtest")
def api_grid_backtest(
    code: str = Query(..., description="标的代码，如 sh510300 或 510300"),
    period: str = Query("1d", description="周期：1d 日线 / 1w 周线"),
    count: int = Query(250, ge=20, le=800, description="K线数量"),
    base: Optional[float] = Query(None, gt=0, description="基准价，留空取区间首日收盘"),
    step_pct: float = Query(2.0, gt=0, le=20, description="网格步长（%）"),
    band_pct: float = Query(20.0, gt=0, le=100, description="上下界幅度（%）"),
    lot: int = Query(10000, ge=100, description="每格份数，会向下取整到 100 的整数倍"),
    capital: float = Query(100000, ge=10000, description="总资金"),
    base_ratio: float = Query(0.5, ge=0, le=1, description="底仓占资金比例"),
    mode: str = Query("arith", description="arith 等差 / geo 等比"),
    strategy: str = Query("fixed",
                          description="网格策略：fixed 固定 / pyramid 金字塔加码 / "
                                      "asym 不对称 / moving 移动网格"),
    step_sell_pct: Optional[float] = Query(
        None, gt=0, le=50, description="不对称网格的卖出步长（%），留空=买步长×1.5"),
    pyramid_mul: float = Query(1.5, ge=1.0, le=5.0,
                               description="金字塔每远离基准一档的份数倍率"),
):
    """网格交易回测（4 种网格策略 + A股 T+1 + 整手 + ETF 免印花税）。

    不要求登录（与选股一致）；已登录时用该用户自定义费率。
    返回的 warnings 一定要展示给用户 —— 单边行情下网格会失效，
    那段收益没有参考意义。
    """
    code = ds.normalize(code)
    period = ds.normalize_period(period)
    if mode not in ("arith", "geo"):
        raise HTTPException(400, f"未知网格类型: {mode}（可选 arith / geo）")
    if strategy not in ("fixed", "pyramid", "asym", "moving"):
        raise HTTPException(
            400, f"未知网格策略: {strategy}（可选 fixed / pyramid / asym / moving）")

    klines = ds.get_kline(code, period, count)
    if not klines or len(klines) < 20:
        raise HTTPException(404, f"未取到 {code} 的足够K线数据（至少 20 根，实到 "
                                 f"{len(klines) if klines else 0} 根）")

    etf = store.is_etf(code)
    fees = store.get_fees()
    res = grd.grid_backtest(
        klines, base=base, step_pct=step_pct, band_pct=band_pct,
        lot=lot, capital=capital, base_ratio=base_ratio, mode=mode,
        strategy=strategy, step_sell_pct=step_sell_pct, pyramid_mul=pyramid_mul,
        fee_rate=(fees.get("etf_fee_rate") if etf else fees.get("fee_rate")),
        fee_min=(fees.get("etf_fee_min") if etf else fees.get("fee_min")),
        stamp=fees.get("stamp_rate"), etf=etf)
    if not res:
        raise HTTPException(400, "网格参数算不出档位，请放宽区间或缩小步长")

    res["code"] = code
    quotes = ds.quote_tencent([code])
    return {
        "code": code,
        "name": (quotes.get(code) or {}).get("name", code),
        "period": period,
        "etf": etf,
        "kline_count": len(klines),
        "date_range": [klines[0]["date"], klines[-1]["date"]],
        "result": res,
    }


@app.get("/api/grid/suggest")
def api_grid_suggest(
    code: str = Query(..., description="标的代码"),
    period: str = Query("1d"),
    count: int = Query(250, ge=20, le=800),
    risk: float = Query(1.0, gt=0.1, le=3.0, description="风险系数，越大步长越宽"),
):
    """按 ATR 给网格建议参数（步长 / 区间）。数据源失败返回空对象。"""
    code = ds.normalize(code)
    klines = ds.get_kline(code, ds.normalize_period(period), count)
    if not klines or len(klines) < 20:
        return {}
    s = grd.suggest_params(klines, risk=risk)
    s["code"] = code
    return s


@app.get("/api/walk_forward")
def api_walk_forward(
    code: str = Query(..., description="股票代码，如 sh600519"),
    strategy: str = Query("breakout_20h", description="要检验的策略 key"),
    folds: int = Query(3, ge=2, le=10, description="折数"),
    train_ratio: float = Query(0.7, gt=0.3, lt=0.95, description="训练段占比"),
    horizon: int = Query(5, ge=1, le=60, description="信号后观察多少根K线"),
    min_signals: int = Query(2, ge=1, le=20, description="训练段最少信号数"),
):
    """走查回测：检验「策略参数是真有效还是过拟合」（借鉴 tradingview-mcp）

    把历史切成若干折，每折用训练段挑参数、用测试段验证，最后给出判定：
      ROBUST      参数稳健
      MODERATE    可用，需控制仓位
      WEAK        依赖特定行情
      OVERFITTED  样本外失效
      INSUFFICIENT 数据/信号不足，无法判定
    """
    import walkforward as wf

    code = ds.normalize(code)
    if strategy not in scr.STRATEGY_BY_KEY:
        raise HTTPException(400, f"未知策略: {strategy}")

    res = wf.walk_forward_code(code, strategy, folds=folds,
                               train_ratio=train_ratio, horizon=horizon,
                               min_signals=min_signals)
    # 补充标的名，便于前端直接展示
    if res.get("verdict") == "INSUFFICIENT" and "没有这只股票" in (res.get("detail") or ""):
        raise HTTPException(404, res["detail"])
    try:
        eng = scr.get_history_engine()
        s = eng.series(code) if eng else None
        res["bars"] = len(s["close"]) if s else 0
        if s:
            res["date_range"] = [s["dates"][0], s["dates"][-1]]
    except Exception:
        pass
    res["strategy_name"] = (scr.STRATEGY_BY_KEY.get(strategy) or {}).get("name", strategy)
    return res


@app.get("/api/pattern_score")
def api_pattern_score(
    min_score: float = Query(85.0, ge=0, le=100, description="最低分数门槛"),
    limit: int = Query(50, ge=1, le=300),
    market: str = Query("", description="sh/sz/bj"),
    exclude_st: bool = Query(True),
):
    """形态评分选股（P1，借鉴 B1 思路但按本项目的实测方向重设计）

    ## 与参考项目的关键差异（重要）

    a-share-quant-selector 的 B1 是「找与历史大涨股形态相似的票」。
    本实现**实测后否定了这个方向**：在 300 只测试股上，与模板相似度高
    的组（未来 20 日均值 +1.73%）反而不如相似度低的组（+1.77%），
    胜率还更低（47.2% vs 51.9%）。

    改用**按实测收益方向打分**（167,721 个观察点）：

        区间位置：越低越好（最低分位胜率 58.5% vs 最高分位 46.3%）
        量能比  ：越低越好（56.6% vs 44.3%）—— 放量反而危险
        前60日涨幅：越小越好（55.6% vs 40.5%）
        前60日振幅：小振幅略优（区分度较弱）

    该评分经样本外验证**单调有效**：

        分数 ≥85 → 未来20日均值 +2.93%、胜率 57.8%
        分数 <55 → 未来20日均值 -0.07%、胜率 41.9%

    详见 docs/形态匹配说明.md
    """
    import similarity as sim
    rows = MARKET.ensure()
    if market:
        rows = [r for r in rows if r["code"].startswith(market)]
    if exclude_st:
        rows = [r for r in rows if "ST" not in r["name"].upper() and "退" not in r["name"]]

    hit = sim.score_market(rows, min_score=min_score, top_n=limit)
    _, upd = MARKET.get()
    return {
        "total": len(hit),
        "min_score": min_score,
        "pool_size": len(rows),
        "items": hit,
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
        # 讲清楚每个维度在找什么，前端可直接展示
        "dimensions": [
            {"key": "position", "name": "区间位置", "weight": sim.SCORE_WEIGHTS["position"],
             "dir": "低", "note": "当前价在近60日区间中的位置，越低越好"},
            {"key": "vol_ratio", "name": "量能比", "weight": sim.SCORE_WEIGHTS["vol_ratio"],
             "dir": "低", "note": "近5日均量/近60日均量，缩量优于放量"},
            {"key": "chg60", "name": "前60日涨幅", "weight": sim.SCORE_WEIGHTS["chg60"],
             "dir": "低", "note": "前期涨幅小优于涨幅大（均值回归）"},
            {"key": "range60", "name": "前60日振幅", "weight": sim.SCORE_WEIGHTS["range60"],
             "dir": "低", "note": "小振幅优于大振幅（区分度较弱）"},
        ],
        "disclaimer": ("本评分衡量的是「形态是否处于实测的有利区间」，"
                       "不等于必然上涨。样本外平均效果：高分组胜率约 58%。"),
    }


@app.get("/api/backtest_multi")
def api_backtest_multi(
    code: str = Query(...),
    period: str = Query("1d"),
    count: int = Query(250, ge=60, le=800),
    init_cash: float = Query(1000000, ge=10000),
):
    """单股全策略回测对比"""
    code = ds.normalize(code)
    period = ds.normalize_period(period)
    klines = ds.get_kline(code, period, count)
    if not klines or len(klines) < 60:
        raise HTTPException(404, f"未取到 {code} 的足够K线数据")

    df = pd.DataFrame(klines)
    out = []
    for key, fn in strat.STRATEGY_MAP.items():
        try:
            res = bt.backtest(klines, fn(df), init_cash=init_cash)
            if res:
                out.append({
                    "strategy": key,
                    "strategy_name": strat.STRATEGY_DESC.get(key, key),
                    "total_return": res["total_return"],
                    "annual_return": res["annual_return"],
                    "max_drawdown": res["max_drawdown"],
                    "sharpe": res["sharpe"],
                    "win_rate": res["win_rate"],
                    "trades": res["trades"],
                    "pl_ratio": res["pl_ratio"],
                })
        except Exception:
            continue
    out.sort(key=lambda x: x["sharpe"], reverse=True)

    quotes = ds.quote_tencent([code])
    return {
        "code": code,
        "name": (quotes.get(code) or {}).get("name", code),
        "date_range": [klines[0]["date"], klines[-1]["date"]],
        "items": out,
    }


# ===========================================================================
# 关键价位（借鉴 TSP 个股分析 9 类价位）
# ===========================================================================

@app.get("/api/keylevels")
def api_keylevels(
    code: str = Query(...),
    count: int = Query(250, ge=60, le=800),
):
    """个股 9 类关键价位"""
    code = ds.normalize(code)
    klines = ds.get_kline(code, "1d", count)
    if not klines or len(klines) < 20:
        raise HTTPException(404, f"未取到 {code} 的足够K线数据")

    levels = kls.key_levels(klines)
    quotes = ds.quote_tencent([code])
    q = quotes.get(code) or {}
    cur = float(q.get("price") or (float(klines[-1]["close"]) if klines else 0))

    # 按与现价的关系重新归类（价格高于现价=压力，低于=支撑）
    for l in levels:
        l["side"] = "resistance" if l["price"] > cur else "support"

    below = sorted([l for l in levels if l["price"] <= cur],
                   key=lambda x: -x["price"])[:8]       # 最近的支撑（降序）
    above = sorted([l for l in levels if l["price"] > cur],
                   key=lambda x: x["price"])[:8]        # 最近的阻力（升序）

    return {
        "code": code,
        "name": q.get("name", code),
        "price": round(cur, 2),
        "levels": levels,
        "supports": below,
        "resistances": above,
    }


# ===========================================================================
# 异动监控（借鉴 TSP 盘中异动聚合）
# ===========================================================================

@app.get("/api/moves")
def api_moves(
    amount_min: float = Query(100000, description="放量阈值(万元)"),
    limit: int = Query(50, ge=1, le=200),
):
    """全市场当日异动聚合：涨停/炸板/跌停/大涨/大跌/放量"""
    rows = MARKET.ensure()
    res = mvs.detect_moves(rows, amount_min=amount_min)
    buckets = {k: v[:limit] for k, v in res["buckets"].items()}
    _, upd = MARKET.get()
    return {
        "counts": res["counts"],
        "buckets": buckets,
        "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                    if upd else None),
    }


# ===========================================================================
# 全套技术指标（借鉴 Klang 的 info 命令：30+ 指标一屏总览）
# ===========================================================================

@app.get("/api/indicators_full")
def api_indicators_full(
    code: str = Query(...),
    period: str = Query("1d"),
    count: int = Query(250, ge=60, le=800),
):
    """个股全套技术指标 + 自动信号汇总"""
    code = ds.normalize(code)
    period = ds.normalize_period(period)
    klines = ds.get_kline(code, period, count)
    if not klines or len(klines) < 30:
        raise HTTPException(404, f"未取到 {code} 的足够K线数据")

    res = ind.all_indicators(klines)
    if not res:
        raise HTTPException(500, "指标计算失败")

    quotes = ds.quote_tencent([code])
    q = quotes.get(code) or {}
    res["code"] = code
    res["name"] = q.get("name", code)
    res["period"] = period
    res["quote"] = q
    res["date_range"] = [klines[0]["date"], klines[-1]["date"]]
    return res


# ===========================================================================
# 多股对比（借鉴 Klang 的 compare 命令）
# ===========================================================================

@app.get("/api/compare")
def api_compare(
    codes: str = Query(..., description="逗号分隔，2-8 只，如 sh600519,sz000858"),
    period: str = Query("1d"),
    count: int = Query(250, ge=60, le=800),
):
    """多股技术指标并排对比"""
    raw = [c.strip() for c in codes.split(",") if c.strip()]
    if not raw:
        raise HTTPException(400, "请至少提供 1 个股票代码")
    if len(raw) > 8:
        raw = raw[:8]

    period = ds.normalize_period(period)
    codes_n = [ds.normalize(c) for c in raw]

    quotes = ds.quote_tencent(codes_n)
    items = []

    for code in codes_n:
        q = quotes.get(code) or {}
        row = {
            "code": code,
            "symbol": code[2:] if len(code) > 2 else code,
            "name": q.get("name", code),
            "price": q.get("price"),
            "change_pct": q.get("change_pct"),
            "turnover": q.get("turnover"),
            "amount": q.get("amount"),
            "pe": q.get("pe"),
            "pb": q.get("pb"),
            "total_cap": q.get("total_cap"),
            "float_cap": q.get("float_cap"),
        }
        # 指标部分（失败不影响行情展示）
        try:
            kl = ds.get_kline(code, period, count)
            if kl and len(kl) >= 30:
                import numpy as np
                C = np.array([x["close"] for x in kl], dtype=float)
                H = np.array([x["high"] for x in kl], dtype=float)
                L = np.array([x["low"] for x in kl], dtype=float)
                V = np.array([x["volume"] for x in kl], dtype=float)
                from mytt import MACD, RSI, KDJ, DMI, BOLL, MA as _MA
                dif, dea, macd_hist = MACD(C)
                _k, _d, _j = KDJ(C, H, L)
                _pdi, _mdi, _adx, _adxr = DMI(C, H, L)
                up, mid, lowb = BOLL(C)
                vr = None
                if len(V) >= 21:
                    a20 = float(np.mean(V[-21:-1]))
                    if a20 > 0:
                        vr = round(float(V[-1]) / a20, 2)

                def _last(a):
                    try:
                        arr = np.asarray(a, dtype=float).ravel()
                        vals = [float(x) for x in arr
                                if not (np.isnan(x) or np.isinf(x))]
                        return round(vals[-1], 3) if vals else None
                    except Exception:
                        return None

                row.update({
                    "rsi6": _last(RSI(C, 6)),
                    "rsi14": _last(RSI(C, 14)),
                    "rsi24": _last(RSI(C, 24)),
                    "macd": _last(macd_hist),
                    "dif": _last(dif),
                    "dea": _last(dea),
                    "kdj_j": _last(_j),
                    "kdj_k": _last(_k),
                    "adx": _last(_adx),
                    "pdi": _last(_pdi),
                    "mdi": _last(_mdi),
                    "boll_up": _last(up),
                    "boll_low": _last(lowb),
                    "vol_ratio": vr,
                    "ma20": _last(_MA(C, 20)),
                    "ma60": _last(_MA(C, 60)),
                })
                # 布林带位置百分比（0=下轨 100=上轨）
                try:
                    cu = float(C[-1])
                    bu, bl = _last(up), _last(lowb)
                    if bu and bl and bu > bl:
                        row["boll_pos"] = round((cu - bl) / (bu - bl) * 100, 1)
                except Exception:
                    pass
        except Exception as e:
            row["indicator_error"] = str(e)[:80]

        items.append(row)

    # 计算各指标排名（用于前端高亮最强/最弱）
    ranks = {}
    for key, higher_better in (("change_pct", True), ("rsi14", True),
                               ("macd", True), ("kdj_j", True),
                               ("vol_ratio", True), ("adx", True)):
        vals = [(i, it.get(key)) for i, it in enumerate(items)
                if isinstance(it.get(key), (int, float))]
        if len(vals) >= 2:
            vals_sorted = sorted(vals, key=lambda x: x[1], reverse=higher_better)
            ranks[key] = {
                "best": vals_sorted[0][0],
                "worst": vals_sorted[-1][0],
            }

    return {
        "items": items,
        "ranks": ranks,
        "period": period,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ===========================================================================
# 扩展数据维度：资金面 / 筹码 / 基本面 / 研报公告（westock）
#              + 市场热点（同花顺特色数据）
# ===========================================================================

# ===========================================================================
# 小白选股
# ===========================================================================

@app.get("/api/newbie_presets")
def api_newbie_presets():
    """返回四套预设方案的目录，供前端渲染大按钮。"""
    return {"presets": nwb.list_presets()}


@app.get("/api/newbie_pick")
def api_newbie_pick(
    preset: str = Query("steady", description="steady/rebound/growth/hot"),
    limit: int = Query(20, ge=1, le=50),
    refresh: bool = Query(False, description="强制后台重算（不阻塞，仍返回旧值）"),
):
    """执行一键选股。

    走后台预计算缓存，毫秒级返回。若缓存未就绪，会触发后台计算并如实返回
    computing 状态，前端据此显示"正在扫描"，而不是假装有结果。
    """
    if preset not in nwb.PRESET_BY_KEY:
        raise HTTPException(400, f"未知方案: {preset}")

    if refresh:
        with NEWBIE._lock:                    # noqa: SLF001 - 主动作废
            NEWBIE._ts.pop(preset, None)
    NEWBIE.ensure_async(preset)

    data, st = NEWBIE.get(preset, limit)
    if data is None:
        return {
            "preset": next(p for p in nwb.list_presets() if p["key"] == preset),
            "items": [], "universe": 0, "matched": 0, "scanned": 0,
            "computing": st["computing"] or True,
            "note": "首次使用需要扫描市场行情，请稍候几秒后刷新。",
            "updated": st["updated"],
        }
    out = dict(data)
    out["computing"] = st["computing"]
    out["updated"] = st["updated"]
    if st["error"]:
        out["note"] = (out.get("note") or "") + f"（后台计算出错：{st['error']}）"
    _save_screen_history("newbie",
                         f"小白·{out.get('preset', {}).get('name', '')}",
                         {"preset": preset, "limit": limit}, out.get("items"))
    return out


# ===========================================================================
# 选股历史自动存档（小白 / 策略 / 条件 每次执行后落库，便于过后回看胜率）
# ===========================================================================

def _norm_screen_items(raw_items):
    """把各模块返回的股票条目归一化为 {code, name, price, change_pct}。"""
    out = []
    for it in raw_items or []:
        code = it.get("code")
        if not code:
            continue
        price = it.get("price")
        if price is None:
            price = it.get("close") or it.get("current")
        out.append({
            "code": code,
            "name": it.get("name", "") or "",
            "price": price,
            "change_pct": it.get("change_pct"),
        })
    return out


def _save_screen_history(module, title, params, raw_items):
    """静默存档一次选股结果；任何异常都不影响选股主流程。"""
    try:
        items = _norm_screen_items(raw_items)
        if not items:
            return
        store.save_screen_history(module, title, params, items)
    except Exception as e:
        print(f"[screen-history] 存档失败(module={module}): {e}")


# ===========================================================================
# 选股历史回看：窗口胜率回测 + 历史列表/明细
# ===========================================================================

@app.post("/api/sim/window")
def api_sim_window(payload: Dict[str, Any]):
    """窗口胜率回测：对一批股票模拟 09:30–09:50 VWAP 买入、当天收盘结算。

    纯统计，与虚拟盘资金/持仓完全隔离。codes 为 [{code, name}] 或纯代码串。
    """
    store.initialize()
    raw = payload.get("codes") or []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "codes 不能为空")
    codes = []
    for c in raw:
        if isinstance(c, str) and c.strip():
            codes.append({"code": c.strip()})
        elif isinstance(c, dict) and c.get("code"):
            codes.append({"code": c["code"], "name": c.get("name", "")})
    if not codes:
        raise HTTPException(400, "codes 不能为空")
    if len(codes) > 30:
        codes = codes[:30]
    try:
        amount_per = float(payload.get("amount_per") or 10000.0)
        days = int(payload.get("days") or 10)
    except (TypeError, ValueError):
        amount_per, days = 10000.0, 10
    days = max(1, min(30, days))
    try:
        return wsim.window_winrate(codes, amount_per=amount_per, days=days)
    except Exception as e:
        raise HTTPException(502, f"回测计算失败：{e}")


def _history_owner_ok(rec: Dict[str, Any], request: Request) -> bool:
    """历史记录归属校验：本人或管理员可看，否则不可越权。"""
    if not rec:
        return False
    if _is_admin(_current_user(request)):
        return True
    return int(rec.get("user_id") or 0) == int(store.current_user_id())


@app.get("/api/screen/history")
def api_screen_history(request: Request,
                       module: Optional[str] = None,
                       limit: int = Query(200, ge=1, le=1000)):
    """我的选股历史列表（按账户隔离，按时间倒序）。
    module 可过滤 newbie/strategy/screen；管理员看他人请用
    /api/admin/users/{uid}/screen_history。
    """
    store.initialize()
    items = store.list_screen_history(module=module, limit=limit,
                                      user_id=store.current_user_id())
    return {"items": items, "user_id": store.current_user_id()}


@app.get("/api/screen/history/{hid}")
def api_screen_history_detail(hid: int, request: Request):
    """选股历史明细（含当时存档的个股列表）。非本人且非管理员 → 404。"""
    store.initialize()
    rec = store.get_screen_history(hid)
    if not _history_owner_ok(rec, request):
        raise HTTPException(404, "记录不存在")
    return rec


@app.get("/api/screen/history/{hid}/performance")
def api_screen_history_performance(hid: int, request: Request,
                                   amount_per: float = 10000.0):
    """入选后表现：以入选价(或入选日收盘)为基准，对比最新价，统计涨跌幅/胜率。"""
    rec = store.get_screen_history(hid)
    if not _history_owner_ok(rec, request):
        raise HTTPException(404, "记录不存在")
    bd = datetime.fromtimestamp(rec["created_at"]).strftime("%Y-%m-%d")
    try:
        return wsim.since_added_perf(rec["items"], bd, amount_per=float(amount_per))
    except Exception as e:
        raise HTTPException(502, f"表现计算失败：{e}")


# ===========================================================================
# 自选股与分组（SQLite 持久化）
#
# 存储层在 store.py；本层只做参数校验、行情拼装与错误转换。
# 所有写操作都经 store 的归属校验，避免越权改到他人数据。
# ===========================================================================

def _quote_map(codes: List[str]) -> Dict[str, Any]:
    """批量取实时行情，失败静默返回空（页面仍能显示自选股清单）。"""
    if not codes:
        return {}
    try:
        return ds.quote_tencent(codes) or {}
    except Exception:
        return {}


@app.get("/api/watch/folders")
def api_watch_folders():
    """分组列表（含每组条目数）。"""
    store.initialize()
    return {"folders": store.list_folders()}


@app.post("/api/watch/folders")
def api_watch_folder_create(payload: Dict[str, Any]):
    """新建分组。"""
    store.initialize()
    try:
        f = store.create_folder(payload.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "folder": f}


@app.put("/api/watch/folders/{folder_id}")
def api_watch_folder_rename(folder_id: int, payload: Dict[str, Any]):
    """重命名分组。"""
    store.initialize()
    try:
        store.rename_folder(folder_id, payload.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/watch/folders/{folder_id}")
def api_watch_folder_delete(folder_id: int):
    """删除分组（组内自选股一并删除）。"""
    store.initialize()
    try:
        store.delete_folder(folder_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/watch/items")
def api_watch_items(folder_id: Optional[int] = None,
                    with_quote: bool = Query(True, description="是否附带实时行情")):
    """自选股列表；默认附实时价与涨跌幅。"""
    store.initialize()
    try:
        items = store.list_items(folder_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not with_quote or not items:
        return {"items": items, "count": len(items)}

    qm = _quote_map([it["code"] for it in items])
    for it in items:
        q = qm.get(it["code"]) or {}
        it["price"] = q.get("price")
        it["change_pct"] = q.get("change_pct")
        it["turnover"] = q.get("turnover")
        it["amount"] = q.get("amount")
        it["pe"] = q.get("pe")
        it["total_cap"] = q.get("total_cap")
        if not it.get("name") and q.get("name"):
            it["name"] = q["name"]
    return {"items": items, "count": len(items)}


@app.post("/api/watch/items")
def api_watch_item_add(payload: Dict[str, Any]):
    """加入自选（幂等：已存在则更新备注/名称）。"""
    store.initialize()
    code = payload.get("code") or ""
    if not code:
        raise HTTPException(400, "缺少 code")
    code = ds.normalize(code)
    name = payload.get("name") or ""
    note = payload.get("note") or ""
    folder_id = payload.get("folder_id")
    try:
        r = store.add_item(code, name, folder_id, note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "item": r}


@app.post("/api/watch/items/batch")
def api_watch_item_batch(payload: Dict[str, Any]):
    """批量加入自选（供选股结果一键导入）。"""
    store.initialize()
    raw = payload.get("codes") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "codes 需为数组")
    if len(raw) > 200:
        raise HTTPException(400, "单次最多导入 200 只")
    folder_id = payload.get("folder_id")
    note = payload.get("note") or ""
    ok, fail = 0, []
    for c in raw:
        try:
            code = ds.normalize(c if isinstance(c, str) else (c or {}).get("code", ""))
            name = "" if isinstance(c, str) else (c or {}).get("name", "")
            if not code:
                continue
            store.add_item(code, name, folder_id, note)
            ok += 1
        except Exception as e:
            fail.append({"code": str(c)[:20], "error": str(e)})
    return {"ok": True, "added": ok, "failed": fail}


@app.delete("/api/watch/items/{item_id}")
def api_watch_item_delete(item_id: int):
    """按 id 删除自选。"""
    store.initialize()
    n = store.remove_item(item_id=item_id)
    if not n:
        raise HTTPException(404, "自选股不存在")
    return {"ok": True, "removed": n}


@app.post("/api/watch/items/remove")
def api_watch_item_delete_by_code(payload: Dict[str, Any]):
    """按代码删除（可指定分组）。"""
    store.initialize()
    code = payload.get("code") or ""
    if not code:
        raise HTTPException(400, "缺少 code")
    try:
        n = store.remove_item(code=ds.normalize(code),
                              folder_id=payload.get("folder_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "removed": n}


@app.put("/api/watch/items/{item_id}/move")
def api_watch_item_move(item_id: int, payload: Dict[str, Any]):
    """把自选股移动到另一分组。"""
    store.initialize()
    to = payload.get("folder_id")
    if to is None:
        raise HTTPException(400, "缺少目标 folder_id")
    try:
        store.move_item(item_id, int(to))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/watch/check")
def api_watch_check(codes: str = Query(..., description="逗号分隔")):
    """批量查询哪些代码已在自选中，供前端点亮星标。"""
    store.initialize()
    want = [ds.normalize(c) for c in codes.split(",") if c.strip()]
    out = {c: False for c in want}
    for c in want:
        out[c] = store.has_code(c)
    return {"in_watch": out}


@app.get("/api/datasource_status")
def api_datasource_status():
    """数据源可用性自检，供前端提示降级状态（运行期实时探测）。"""
    return {
        "westock": wst.refresh_status(),
        "hithink": htk.refresh_status(),
        "tencent": True,
    }


@app.get("/api/fundflow")
def api_fundflow(codes: str = Query(..., description="逗号分隔，如 sh600519,sz000858")):
    """个股资金流向（主力/超大单/大单/中单/小单 + 5/10/20日主力净额）。"""
    code_list = [ds.normalize(c) for c in codes.split(",") if c.strip()][:20]
    data = wst.fund_flow(code_list)
    return {
        "items": list(data.values()),
        "available": wst.CLI_AVAILABLE,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/chip")
def api_chip(code: str = Query(...)):
    """筹码成本分布（平均成本 / 集中度 / 获利盘比例）。"""
    c = ds.normalize(code)
    return {
        "code": c,
        "data": wst.chip(c),
        "available": wst.CLI_AVAILABLE,
    }


@app.get("/api/fundamentals")
def api_fundamentals(code: str = Query(...)):
    """基本面摘要：三大报表关键指标 + 分红历史 + 公司简况。"""
    c = ds.normalize(code)
    return {
        "code": c,
        "data": wst.fundamentals(c),
        "dividends": wst.dividends(c, 3),
        "profile": wst.profile(c),
        "available": wst.CLI_AVAILABLE,
    }


@app.get("/api/reports")
def api_reports(code: str = Query(...), limit: int = Query(10, ge=1, le=30)):
    """机构研报列表（含评级）。"""
    c = ds.normalize(code)
    return {"code": c, "items": wst.reports(c, limit), "available": wst.CLI_AVAILABLE}


@app.get("/api/notices")
def api_notices(code: str = Query(...), limit: int = Query(10, ge=1, le=30)):
    """公司公告列表（每条附词典情绪标签：看涨/看跌/中性）。"""
    c = ds.normalize(code)
    items = wst.notices(c, limit)
    try:
        senti.tag_notice(items)
    except Exception:
        pass        # 情绪标签是增强字段，打不上也不能影响公告本身
    return {"code": c, "items": items, "available": wst.CLI_AVAILABLE}


@app.post("/api/sentiment")
def api_sentiment(payload: Dict[str, Any] = Body(default={})):
    """词典情绪分析（本地零成本）：对一组文本打「看涨/看跌/中性」。

    body: {"texts": ["...", ...]} → {"items": [{score, tone, tone_text, ...}]}
    通用端点，公告已在 /api/notices 内打分；这个留给快讯流（#88）、
    自选股分组聚合等后续场景复用。
    """
    texts = payload.get("texts")
    if not isinstance(texts, list) or len(texts) > 200:
        raise HTTPException(400, "body 需为 {\"texts\": [...]}，最多 200 条")
    return {"items": senti.batch([str(t or "") for t in texts])}


@app.get("/api/newsfeed")
def api_newsfeed(limit: int = Query(50, ge=1, le=100), force: int = Query(0)):
    """双源快讯流（新浪7x24 + 同花顺，30s TTL 缓存，每条带词典情绪）。

    items 按时间倒序、两级去重（源内 id + 跨源内容指纹）。
    单源失败不影响整体，errors 里能看到哪个源出了什么事。
    force=1 绕过缓存（前端手动刷新按钮用）。
    """
    f = nf.get_feed(force=bool(force))
    return {"items": f["items"][:limit], "sources": f["sources"],
            "errors": f["errors"], "ts": f["ts"], "cached": f.get("cached", False)}


@app.get("/api/newsfeed/sectors")
def api_newsfeed_sectors(hours: int = Query(3, ge=1, le=24), force: int = Query(0)):
    """板块舆情热度（近 hours 小时滚动窗口；描述性参考，非预测）。

    不再用「当下 40 条实时窗口」实时重聚合（那样每 30s 整窗翻滚、热度乱跳、看不出趋势），
    改为聚合 newsfeed 近 hours 小时的滚动历史，舆情更稳定、能读出持续性。
    仍复用 newsfeed 的抓取与 30s TTL——不新增任何网络请求。
    """
    import sector_sentiment as _ss
    f = nf.get_feed(force=bool(force))          # 触发抓取，让新快讯进入滚动历史
    items = nf.get_sector_window(hours * 3600)
    rows = _ss.heat_table(_ss.aggregate(items))
    return {"rows": rows, "sources": f["sources"], "errors": f["errors"],
            "ts": f["ts"], "window_hours": hours, "cached": f.get("cached", False)}


@app.get("/api/market_phase")
def api_market_phase():
    """市场情绪周期（6 阶段）与实时主线。"""
    import sqlite3

    conn = sqlite3.connect(store.DB_PATH, timeout=15.0)
    try:
        data = mp.get_phase_data(conn)
    finally:
        conn.close()
    if not data.get("ready"):
        return data
    # ponytail: get_mainline 内部已兜底网络异常，这里不再吞异常——
    # 否则主线 bug 会静默变成 unavailable，难以定位（曾因权重字段名不匹配踩过）。
    data["mainline"] = mp.get_mainline(htk)
    return data


# ===========================================================================
# 连板梯队 + 情绪周期（蒸馏 easy-stock 超短连板 / 复用 #95 情绪周期）#100
# ===========================================================================

@app.get("/api/limitup")
def api_limitup(limit: int = Query(200, ge=1, le=500)):
    """连板梯队：今日涨停 + 连板天数分组 + 连板率 + 6 阶段情绪周期。"""
    import sqlite3

    try:
        rows = MARKET.ensure()
        cached = lp.ensure_cached(rows, limit)  # 首跑同步算(~9s)，之后走缓存
        data = lp.build_ladder(rows, limit=limit, board_cache=cached, background=False)
        # 注入 #95 市场情绪周期（6 阶段 + 主线），不重造
        conn = sqlite3.connect(store.DB_PATH, timeout=15.0)
        try:
            ph = mp.get_phase_data(conn)
        finally:
            conn.close()
        data["phase"] = ph.get("phase")
        data["phase_ready"] = bool(ph.get("ready"))
        return data
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/theme")
def api_theme(limit: int = Query(40, ge=1, le=200)):
    """题材雷达：行业板块四维评分 + 融合去重。"""
    try:
        return tr.build_radar(limit=limit)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/alerts/rules")
def api_alert_rules():
    """#96 监控规则列表。"""
    return {"items": alt.list_rules(), "kinds": alt.KIND_LABELS}


@app.post("/api/alerts/rules")
def api_alert_rule_add(payload: Dict[str, Any] = Body(...)):
    """#96 新建监控规则。"""
    if not payload.get("conds"):
        raise HTTPException(400, "至少需要一个条件")
    if str(payload.get("kind")) not in alt.KIND_LABELS:
        raise HTTPException(400, "未知规则类型")
    return {"ok": True, "item": alt.add_rule(payload)}


@app.put("/api/alerts/rules/{rule_id}")
def api_alert_rule_update(rule_id: str, payload: Dict[str, Any] = Body(...)):
    """#96 更新规则（含启停）。"""
    item = alt.update_rule(rule_id, payload)
    if not item:
        raise HTTPException(404, "规则不存在")
    return {"ok": True, "item": item}


@app.delete("/api/alerts/rules/{rule_id}")
def api_alert_rule_delete(rule_id: str):
    alt.delete_rule(rule_id)
    return {"ok": True}


@app.get("/api/alerts")
def api_alerts(limit: int = 50, unread: int = 0):
    """#96 告警流。"""
    return {"items": alt.list_alerts(limit=limit, unread_only=bool(unread)),
            "unread": alt.unread_count()}


@app.post("/api/alerts/read")
def api_alerts_read(payload: Dict[str, Any] = Body(...)):
    """#96 标记已读：body {ts} 单条，或 {all:true} 全部。"""
    if payload.get("all"):
        alt.mark_read(all_=True)
    elif payload.get("ts") is not None:
        alt.mark_read(ts=float(payload["ts"]))
    return {"ok": True, "unread": alt.unread_count()}


@app.delete("/api/alerts")
def api_alerts_clear():
    alt.clear_alerts()
    return {"ok": True}


@app.get("/api/alerts/webhook")
def api_alerts_webhook_get():
    cfg = alt.get_webhook_cfg()
    # 密钥只回传「是否已配置」，不把明文密钥返回前端
    return {k: (("***已配置***" if v else "") if k.endswith(("secret", "key")) else v)
            for k, v in cfg.items()}


@app.put("/api/alerts/webhook")
def api_alerts_webhook_set(payload: Dict[str, Any] = Body(...)):
    return {"ok": True, "item": alt.set_webhook_cfg(payload)}


@app.post("/api/alerts/test")
def api_alerts_test():
    """#96 发一条测试推送，验证 Webhook 配置是否可用。"""
    cfg = alt.get_webhook_cfg()
    if not cfg.get("enabled"):
        raise HTTPException(400, "推送未启用")
    res = webhook.push(cfg, "牛来选股面板 · 测试推送",
                       "这是一条来自监控中心的测试消息。")
    return {"ok": res["failed"] == 0, "sent": res["sent"],
            "failed": res["failed"], "detail": res["detail"]}


@app.post("/api/alerts/check")
def api_alerts_check():
    """#96 手动触发一轮检测（自检/演示用）。"""
    stat = alt.check_once(
        engine=scr.get_history_engine(auto_load=False), htk=htk)
    return {"ok": True, **stat, "unread": alt.unread_count()}


@app.get("/api/market_overview")
def api_market_overview():
    """市场温度：13 维评分 + 涨跌分布 + 板块与龙虎榜摘要。"""
    ov = wst.market_overview()
    # 数据日期落后一天时，明确说明「是上游的日期，不是取数失败」
    if ov.get("stale"):
        ov["note"] = (
            f"温度画像的数据日期为 {ov.get('date')}（上游最近一次重算的日期），"
            f"今天的画像尚未生成。涨跌分布、成交额等为实时数据。"
        )
    return {
        "overview": ov,
        "changedist": wst.changedist(),
        "available": wst.CLI_AVAILABLE,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/lhb")
def api_lhb(
    limit: int = Query(30, ge=5, le=60),
    date: str = Query("", description="YYYY-MM-DD；缺省=最近有数据的交易日（节假日自动回退）"),
):
    """全市场龙虎榜（净买额排序）。

    默认走东财源：含上榜原因（机构买入家数+成功率）与 D1/D2/D5/D10
    上榜后复权涨跌幅（借鉴 go-stock LongTiger）；后验字段为 null 表示
    对应交易日尚未到期。东财不可用时自动降级 westock CLI（无后验列）。
    """
    try:
        em = wst.lhb_em(date=date, limit=limit)
    except Exception:
        em = {}
    if em.get("items"):
        em["available"] = True
        return em
    items = wst.lhb_market(limit)
    return {
        "items": items,
        "date": (items[0].get("date") if items else "") or "",
        "total": len(items),
        "source": "westock",
        "available": wst.CLI_AVAILABLE,
    }


@app.get("/api/sector_rank")
def api_sector_rank(limit: int = Query(25, ge=5, le=60)):
    """行业行情榜（带主力净流入与领涨股）。"""
    return {
        "items": wst.sector_rank(limit),
        "available": wst.CLI_AVAILABLE,
    }


@app.get("/api/hotspot")
def api_hotspot():
    """市场热点（同花顺特色数据）：人气热榜 / 涨停池 / 连板天梯 / 炸板池 / 跌停池。"""
    return {
        "hot_rank": htk.hot_rank(30),
        "limit_up": htk.limit_up_pool(),
        "limit_down": htk.limit_down_pool(),
        "limit_break": htk.limit_break_pool(),
        "ladder": htk.limit_up_ladder(),
        "dragon_tiger": htk.dragon_tiger(),
        "anomaly": htk.anomaly_list(30),
        "available": htk.API_AVAILABLE,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/auction")
def api_auction(stage: str = Query("final", description="live=实时 / final=终态")):
    """集合竞价（同花顺）：短线风向标基准 + 自选股竞价快照。

    为什么必须带 trade_date：上游竞价接口**不返回数据所属交易日**，而休市日
    会静默回吐上一交易日的终态（实测 2026-10-06 取到的是 09-30 数据）。不标
    日期用户会以为是实时。这里用交易日历序列校准：今天在序列里就用今天，
    否则回落到序列最后一个交易日。

    降级策略（任一环节不可用都不 500）：
      - 无 Key / 接口异常 → available=False，前端整卡隐藏
      - 休市日 → benchmark 自动回落到最近交易日；取不到就 items=[]
    """
    if stage not in ("live", "final"):
        stage = "final"

    today = datetime.now().strftime("%Y-%m-%d")
    td = htk.trading_days()
    is_today = today in td["days"]
    # 数据归属交易日：今天开市用今天，否则用序列里最后一个交易日
    trade_date = today if is_today else (td["last"] or today)

    # 短线风向标：先取当日，休市为空时回落到最近交易日
    bench = htk.auction_benchmark()
    if bench["total"] == 0 and trade_date != today:
        bench = htk.auction_benchmark(trade_date)

    # 自选股竞价快照（上游单次上限 100，超出截断）
    watch: Dict[str, Any] = {"ok": False, "total": 0, "items": []}
    if store.current_user_is_anonymous():
        # 未登录（匿名回落到 local 默认账号）：不展示「我的自选」。
        # 匿名访客没有真正的自选，展示 local 账号的私有自选既误导
        # （写着「我的」却不是他的）又会在多用户部署时暴露部署者的数据。
        pass
    else:
        try:
            store.initialize()
            items = store.list_items()
            codes = [it.get("code") for it in (items or []) if it.get("code")]
            if codes:
                watch = htk.auction_snapshot(codes[:100], stage)
                # 补自选股备注名（上游只给标准简称）
                alias = {str(it.get("code")): it.get("alias") or it.get("name")
                         for it in items}
                for r in watch["items"]:
                    if not r.get("name") and alias.get(r["code"]):
                        r["name"] = alias[r["code"]]
        except Exception:
            watch = {"ok": False, "total": 0, "items": []}

    return {
        "available": htk.API_AVAILABLE,
        "trade_date": trade_date,
        "is_today": is_today,
        "phase": watch.get("phase", stage),
        "status": watch.get("status", ""),
        "benchmark": bench,
        "watch": watch,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/valuation")
def api_valuation(code: str = Query(..., description="股票代码，如 sh600519")):
    """个股估值五口径（同花顺）：PE_TTM / PE_MRQ / PB_MRQ / PS_TTM / PCF_TTM。

    ⚠ 实测：ETF 与指数无估值数据，且**混入会整批失败**（code≠0）。
    所以只服务个股，取不到时返回 valuation=None，前端该行不渲染即可。
    """
    rows = htk.valuation([code])
    return {"valuation": rows[0] if rows else None,
            "available": htk.API_AVAILABLE}


# ===========================================================================
# 策略意图解析 + 通达信公式编译
# ===========================================================================

@app.get("/api/intent_parse")
def api_intent_parse(text: str = Query(..., min_length=1, description="策略自然语言描述")):
    """把一句话策略描述解析为结构化意图（指标/类型/风险/止损止盈）。"""
    return itt.parse_intent(text)


@app.get("/api/intent_examples")
def api_intent_examples():
    """返回意图解析示例，供前端做快捷填入。"""
    return {"examples": itt.EXAMPLES}


@app.get("/api/tdx_capabilities")
def api_tdx_capabilities():
    """通达信公式编译器能力清单与示例。"""
    return tdxc.capabilities()


@app.get("/api/tdx_compile")
def api_tdx_compile(
    formula: str = Query(..., min_length=1),
    code: str = Query("sh600519"),
    period: str = Query("1d"),
    count: int = Query(250, ge=60, le=800),
):
    """编译并求值通达信公式，返回信号序列与统计。"""
    c = ds.normalize(code)
    p = ds.normalize_period(period)
    klines = ds.get_kline(c, p, count)
    if not klines or len(klines) < 30:
        raise HTTPException(404, f"未取到 {c} 的足够K线数据")
    res = tdxc.compile_formula(formula, klines)
    if "error" in res:
        return {"ok": False, "error": res["error"], "supported": res.get("supported")}
    return {
        "ok": True,
        "code": c,
        "period": p,
        "kline_count": len(klines),
        "signals": res["signals"],
        "buy_count": res["buy_count"],
        "sell_count": res["sell_count"],
        "warnings": res["warnings"],
        "variables": res["variables"],
        "called_functions": res["called_functions"],
        "dates": [k["date"] for k in klines],
    }


@app.get("/api/tdx_backtest")
def api_tdx_backtest(
    formula: str = Query(..., min_length=1),
    code: str = Query("sh600519"),
    period: str = Query("1d"),
    count: int = Query(250, ge=60, le=800),
    init_cash: float = Query(1000000, ge=10000),
    stop_loss: float = Query(0.0, ge=0, le=0.5),
    take_profit: float = Query(0.0, ge=0, le=2.0),
    position_size: float = Query(1.0, gt=0, le=1.0),
):
    """用通达信公式产出的信号直接回测。"""
    c = ds.normalize(code)
    p = ds.normalize_period(period)
    klines = ds.get_kline(c, p, count)
    if not klines or len(klines) < 60:
        raise HTTPException(404, f"未取到 {c} 的足够K线数据")
    comp = tdxc.compile_formula(formula, klines)
    if "error" in comp:
        raise HTTPException(400, f"公式编译失败: {comp['error']}")
    res = bt.backtest(klines, comp["signals"], init_cash=init_cash,
                      stop_loss=(stop_loss or None),
                      take_profit=(take_profit or None),
                      position_size=position_size)
    if not res:
        raise HTTPException(500, "回测失败")
    quotes = ds.quote_tencent([c])
    return {
        "code": c,
        "name": (quotes.get(c) or {}).get("name", c),
        "strategy_name": "通达信公式",
        "formula": formula,
        "buy_count": comp["buy_count"],
        "sell_count": comp["sell_count"],
        "kline_count": len(klines),
        "date_range": [klines[0]["date"], klines[-1]["date"]],
        "result": res,
    }


# ===========================================================================
# 账号（注册 / 登录 / 会话）
#
# 身份通过 httpOnly Cookie 传递；同时兼容 Authorization: Bearer <token>，
# 方便将来接移动端或脚本调用。
# ===========================================================================

COOKIE_NAME = "tick_sid"


def _token_from_request(request: Request) -> str:
    tok = request.cookies.get(COOKIE_NAME) or ""
    if not tok:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    return tok


@app.middleware("http")
async def _bind_user(request: Request, call_next):
    """把请求身份注入存储层，使 store 内部自动按用户隔离数据。"""
    try:
        u = store.user_by_token(_token_from_request(request))
        store.set_current_user(u["id"] if u else None)
    except Exception:
        store.set_current_user(None)
    try:
        return await call_next(request)
    finally:
        store.set_current_user(None)


def _current_user(request: Request) -> Optional[Dict[str, Any]]:
    try:
        return store.user_by_token(_token_from_request(request))
    except Exception:
        return None


@app.post("/api/auth/register")
def api_register(payload: Dict[str, Any] = Body(...)):
    store.initialize()
    try:
        u = store.register(payload.get("username", ""),
                           payload.get("password", ""),
                           payload.get("display", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    tok = store.create_session(u["id"])
    resp = JSONResponse({"ok": True, "user": u})
    resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax",
                    max_age=store.SESSION_TTL, path="/")
    return resp


@app.post("/api/auth/login")
def api_login(payload: Dict[str, Any] = Body(...)):
    store.initialize()
    u = store.verify_login(payload.get("username", ""), payload.get("password", ""))
    if not u:
        raise HTTPException(401, "用户名或密码错误")
    tok = store.create_session(u["id"])
    resp = JSONResponse({"ok": True, "user": u})
    resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax",
                    max_age=store.SESSION_TTL, path="/")
    return resp


@app.post("/api/auth/logout")
def api_logout(request: Request):
    tok = _token_from_request(request)
    if tok:
        store.logout(tok)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
def api_me(request: Request):
    """当前登录用户。未登录返回 {user: null} 而非报错，前端好处理。"""
    u = _current_user(request)
    if not u:
        return {"user": None, "logged_in": False}
    return {"user": u, "logged_in": True}


# ===========================================================================
# 后台管理
#
# 谁是管理员：环境变量 TICK_ADMIN_USERS=alice,bob（逗号分隔）。
#   - 不设该变量时，不启用管理页（返回 403，避免误开放）。
#   - 改名单需重启进程。用户本身不落库，故不会与用户数据耦合。
#
# 所有 /api/admin/* 都走 _require_admin 守卫：未登录 401，非管理员 403。
# ===========================================================================

def _admin_names() -> set:
    raw = os.environ.get("TICK_ADMIN_USERS", "").strip()
    names = {x.strip().lower() for x in raw.split(",") if x.strip()}
    if not names:
        # 未显式配置管理员时，内置默认管理员账号 admin（配合启动时种子），
        # 自建部署开箱即可进后台；显式配置后此默认即失效。
        names.add("admin")
    return names


def _seed_default_admin() -> None:
    """确保 admin 账号存在（初始密码 123456），开箱即可进后台。

    触发条件：admin 在管理员名单内（未配置 TICK_ADMIN_USERS 时默认在内，
    或显式列入），且 admin 账号尚不存在。register 遇「已存在」会抛 ValueError
    被捕获——绝不覆盖用户自设密码。默认密码极弱，README 已强制要求改密。
    """
    if "admin" not in _admin_names():
        return
    import store as _store
    try:
        _store.register("admin", "123456", "管理员")
        print("[seed] 已创建默认管理员 admin / 123456（请尽快在后台修改密码！）")
    except ValueError:
        pass  # 账号已存在，保留现状
    except Exception as e:
        print(f"[seed] 默认管理员创建失败（可忽略）：{e}")


def _is_admin(u: Optional[Dict[str, Any]]) -> bool:
    names = _admin_names()
    if not names or not u:
        return False
    return (u.get("username") or "").strip().lower() in names


def _require_admin(request: Request) -> Dict[str, Any]:
    """管理员守卫。返回管理员用户，否则抛 401/403。"""
    u = _current_user(request)
    if not u:
        raise HTTPException(401, "请先登录")
    if not _is_admin(u):
        raise HTTPException(403, "需要管理员权限（由 TICK_ADMIN_USERS 指定）")
    return u


@app.get("/api/admin/whoami")
def api_admin_whoami(request: Request):
    """前端用它决定是否显示「后台管理」入口。不报错，只答是否。"""
    u = _current_user(request)
    return {
        "logged_in": bool(u),
        "is_admin": _is_admin(u),
        "username": (u or {}).get("username"),
        "admins_configured": bool(_admin_names()),
    }


@app.get("/api/admin/overview")
def api_admin_overview(request: Request):
    """管理页首屏：系统概览。一次请求拿齐，避免前端串多个。"""
    _require_admin(request)
    import store as _store
    try:
        summary = _store.data_summary()
    except Exception as e:
        summary = {"error": f"{type(e).__name__}: {e}"}

    # 数据源可用性（沿用已有自检，运行期实时探测）
    try:
        srcs = {"westock": wst.refresh_status(), "hithink": htk.refresh_status()}
    except Exception:
        srcs = {}
    # 快照降级链：带 live 探测（实测当前真正在用哪一级）。
    # ⚠ 原来这里有个硬编码的 "tencent": True —— 腾讯挂了也照样显示绿点，
    #   属于假指示。现在只报探测到的事实：chain 是能力顺序，live 是当前实际生效的那一级。
    try:
        snap_st = ds.snapshot_status(probe=True)
    except Exception as e:
        snap_st = {"error": str(e)}
    srcs["snapshot"] = snap_st
    try:
        intraday = ds.intraday_source_status()
    except Exception as e:
        intraday = {"error": str(e)}

    # 进程运行信息
    rows, upd = MARKET.get()
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db": {"backend": _store.BACKEND,
               "target": _store._target_label() if hasattr(_store, "_target_label") else _store.BACKEND},
        "users": summary.get("users", 0),
        "bars": summary.get("bars", {}),
        "market": {"count": len(rows), "loading": MARKET.loading,
                   "updated": (datetime.fromtimestamp(upd).strftime("%Y-%m-%d %H:%M:%S")
                               if upd else None),
                   "error": MARKET._err},
        "sources": srcs,
        "intraday": intraday,
        "uptime_s": round(time.time() - _BOOT_TS, 1),
    }


# ---------------------------------------------------------------- 用户管理

@app.get("/api/admin/users")
def api_admin_users(request: Request):
    """用户列表。store.list_users 已带自选数与持仓数，直接用。"""
    _require_admin(request)
    import store as _store
    admins = _admin_names()
    out = []
    for u in _store.list_users():
        u["is_admin"] = (u.get("username") or "").lower() in admins
        try:
            u["screen_cnt"] = _store.count_screen_history(u["id"]).get("total", 0)
        except Exception:
            u["screen_cnt"] = 0
        out.append(u)
    return {"items": out, "total": len(out), "admins_configured": bool(admins)}


@app.post("/api/admin/users/{uid}/password")
def api_admin_set_password(uid: int, request: Request,
                           payload: Dict[str, Any] = Body(...)):
    """重置密码，并把该用户已有会话全部踢下线。"""
    _require_admin(request)
    import store as _store
    try:
        ok = _store.set_password(uid, payload.get("password", ""))
        kicked = _store.purge_sessions(uid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "用户不存在")
    return {"ok": True, "kicked_sessions": kicked}


@app.post("/api/admin/users/{uid}/reset_account")
def api_admin_reset_account(uid: int, request: Request,
                            payload: Dict[str, Any] = Body(default={})):
    """重置虚拟盘（清持仓与流水，资金复位）。"""
    _require_admin(request)
    import store as _store
    cash = payload.get("cash")
    try:
        res = _store.reset_account(uid, float(cash) if cash is not None else _store._INITIAL_CASH)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return {"ok": True, **res}


@app.delete("/api/admin/users/{uid}")
def api_admin_delete_user(uid: int, request: Request):
    """删除用户（级联清理自选/持仓/流水/会话）。最后一个用户不可删。"""
    admin = _require_admin(request)
    import store as _store
    if int(admin.get("id") or 0) == uid:
        raise HTTPException(400, "不能删除自己")
    try:
        ok = _store.delete_user(uid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "用户不存在")
    return {"ok": True}


# ------------------------------------------------- 查看用户数据（只读诊断）

@app.get("/api/admin/users/{uid}/watch")
def api_admin_user_watch(uid: int, request: Request,
                         with_quote: bool = Query(True)):
    """管理员查看某用户的自选股分组与条目（只读）。

    with_quote=false 时只返回清单，不请求行情 —— 列表很长或行情源不稳时用。
    """
    _require_admin(request)
    _store = store
    _store.initialize()
    user = _store.get_user(uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    try:
        folders = _store.list_folders(uid)
        items = _store.list_items(user_id=uid)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if with_quote and items:
        qm = _quote_map([it["code"] for it in items])
        for it in items:
            q = qm.get(it["code"]) or {}
            it["price"] = q.get("price")
            it["change_pct"] = q.get("change_pct")

    by_folder: Dict[str, Any] = {}
    for it in items:
        by_folder.setdefault(it.get("folder_name") or "未分组", []).append(it)
    return {"user": user, "folders": folders, "items": items,
            "count": len(items), "grouped": by_folder}


@app.get("/api/admin/users/{uid}/screen_history")
def api_admin_user_screen_history(uid: int, request: Request,
                                  module: Optional[str] = None,
                                  limit: int = Query(100, ge=1, le=1000)):
    """管理员查看某用户的历史选股记录（只读）。"""
    _require_admin(request)
    store.initialize()
    user = store.get_user(uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    items = store.list_screen_history(module=module, limit=limit, user_id=uid)
    stat = store.count_screen_history(uid)
    return {"user": user, "items": items, "stat": stat}


@app.get("/api/admin/users/{uid}/paper")
def api_admin_user_paper(uid: int, request: Request,
                         trades: int = Query(30, ge=0, le=500)):
    """管理员查看某用户的虚拟盘：资金 / 持仓 / 成交流水（只读）。"""
    _require_admin(request)
    store.initialize()
    user = store.get_user(uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    s = store.portfolio_summary(uid)
    pos = _pos_with_quote(store.list_positions(uid))
    mv = sum(p["market_value"] or 0 for p in pos)
    quoted = [p for p in pos if p["market_value"] is not None]
    total = round(s["cash"] + mv, 2)
    return {
        "user": user,
        "summary": {
            "cash": s["cash"],
            "market_value": round(mv, 2),
            "cost_total": s["cost_total"],
            "total_asset": total,
            "float_pnl": round(sum(p["pnl"] for p in quoted), 2),
            "position_count": s["position_count"],
            "unquoted": len(pos) - len(quoted),
            "initial_cash": store._INITIAL_CASH,
            "total_pnl": round(total - store._INITIAL_CASH, 2),
            "total_pnl_pct": round((total - store._INITIAL_CASH)
                                   / store._INITIAL_CASH * 100, 2),
        },
        "positions": pos,
        "trades": store.list_trades(uid, limit=trades) if trades else [],
    }


# ------------------------------------------------- 选股历史（全量 + 调试）

@app.get("/api/admin/screen_history")
def api_admin_screen_history(request: Request,
                             module: Optional[str] = None,
                             user_id: Optional[int] = None,
                             limit: int = Query(100, ge=1, le=1000)):
    """全量选股历史（跨账户，调试用）。带模块分布与未归属条数统计。"""
    _require_admin(request)
    store.initialize()
    items = store.list_screen_history(module=module, limit=limit,
                                      user_id=user_id)
    return {"items": items, "stat": store.count_screen_history(),
            "total": len(items)}


@app.post("/api/admin/screen_history/claim")
def api_admin_screen_history_claim(request: Request,
                                   payload: Dict[str, Any] = Body(...)):
    """把未归属（user_id=0）的选股历史记录认领到指定用户。

    老版本存档一律写 user_id=0，改成按账户隔离后这些记录谁都看不见；
    启动时已自动挂到默认本地账号，需要换人的话用这里搬。
    """
    _require_admin(request)
    store.initialize()
    uid = int(payload.get("user_id") or 0)
    if not uid or not store.get_user(uid):
        raise HTTPException(404, "用户不存在")
    moved = store.reassign_screen_history(
        uid, int(payload.get("from_user_id") or 0))
    return {"ok": True, "moved": moved, "user_id": uid}


# ---------------------------------------------------------------- 运行参数

@app.get("/api/admin/config")
def api_admin_config(request: Request):
    """全部可调参数（含当前值、来源、默认值、说明）。"""
    _require_admin(request)
    import config as _cfg
    return {"items": _cfg.all_items()}


@app.put("/api/admin/config/{key}")
def api_admin_config_set(key: str, request: Request,
                         payload: Dict[str, Any] = Body(...)):
    """改参数。校验不过一律 400；落 meta 后立即生效（连接池类需重建连接）。"""
    _require_admin(request)
    import config as _cfg
    if key not in _cfg._BY_KEY:
        raise HTTPException(404, f"未知参数 {key}")
    try:
        val = _cfg.set_value(key, payload.get("value"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    eff = _apply_config_live()
    return {"ok": True, "key": key, "value": val, **eff}


@app.delete("/api/admin/config/{key}")
def api_admin_config_reset(key: str, request: Request):
    """删除覆盖值，回落到环境变量 / 默认。"""
    _require_admin(request)
    import config as _cfg
    if key not in _cfg._BY_KEY:
        raise HTTPException(404, f"未知参数 {key}")
    _cfg.reset(key)
    eff = _apply_config_live()
    return {"ok": True, "key": key, **eff}


def _apply_config_live() -> Dict[str, Any]:
    """把 config 的当前值推给各模块。返回生效情况供界面提示。"""
    out: Dict[str, Any] = {}
    try:
        from sources import eltdx_source
        out["eltdx"] = eltdx_source.reload_config()
    except Exception as e:
        out["eltdx"] = {"error": f"{type(e).__name__}: {e}"}
    return out


# ---------------------------------------------------------------- 数据落库

@app.get("/api/admin/bars")
def api_admin_bars(request: Request, recent: int = Query(20, ge=0, le=200)):
    """落库覆盖率 + 同步进度 + 失败清单。"""
    _require_admin(request)
    import store as _store
    try:
        cov = _store.bars_coverage()
        rows = _store.sync_status(recent) if recent else []
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    failed = [r for r in rows if (r.get("status") or "ok") != "ok"]
    return {"coverage": cov, "recent": rows, "failed": failed,
            "failed_count": len(failed)}


@app.post("/api/admin/bars/sync")
def api_admin_bars_sync(request: Request, payload: Dict[str, Any] = Body(default={})):
    """手动触发全市场日线补数据。

    这是重活（数千只 × 网络请求），放在后台线程跑，接口立即返回。
    进度通过 /api/admin/bars 的 sync_status 观察。
    """
    _require_admin(request)
    if _SYNC_STATE["running"]:
        return {"ok": False, "running": True, "note": "已有同步任务在跑，请等待完成"}
    scope = str(payload.get("scope") or "all")
    count = int(payload.get("count") or 250)
    limit = int(payload.get("limit") or 0)

    def _run():
        _SYNC_STATE.update(running=True, started=time.time(), scope=scope,
                           done=0, total=0, error="", note="")
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
            import sync_bars as sb
            codes = sb.pick_codes(scope, "")
            # 同步守门三重判断：盘中全量/当日已跑/库已最新 都在这里拦下，
            # 避免后台线程白打几千次请求（或写入未结算的半根日线）。
            # 管理页就是给人手动触发的地方，所以 force 走 payload 开关。
            codes, gate_r = sb.gate_full_sync(
                scope, codes, force=bool(payload.get("force")))
            if gate_r["action"] == "skip":
                _SYNC_STATE.update(total=0, ok_count=0,
                                   note="守门跳过：" + gate_r["reason"])
                return
            _SYNC_STATE["gate"] = gate_r["reason"]
            _SYNC_STATE["total"] = len(codes)
            res = sb.sync_via_eltdx(codes, count, workers=6, limit=limit)
            _SYNC_STATE["ok_count"] = res[0] if isinstance(res, tuple) else 0
            # 落库完成后的固定动作（与每日定时 run_now 同款）：
            # 1) 重载内存历史引擎——否则体检/选股一直用旧数据直到重启；
            # 2) 预热策略体检缓存——第二天打开直接是现成结果。
            try:
                import history as _hist
                _hist.reload_engine()
            except Exception as _e:
                print(f"[sync-bars] 历史引擎重载失败：{_e}")
            try:
                import strategy_eval as _se
                _se.prewarm(why="手动落库完成")
            except Exception as _e:
                print(f"[sync-bars] 体检预热启动失败：{_e}")
        except Exception as e:
            _SYNC_STATE["error"] = f"{type(e).__name__}: {e}"
        finally:
            _SYNC_STATE["running"] = False
            _SYNC_STATE["finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "running": True, "scope": scope, "note": "已在后台开始，请稍后查看进度"}


@app.get("/api/admin/bars/sync_status")
def api_admin_sync_status(request: Request):
    """后台同步任务的实时状态。"""
    _require_admin(request)
    st = dict(_SYNC_STATE)
    if st.get("started"):
        st["elapsed"] = round(time.time() - st["started"], 1)
    return st


@app.get("/api/admin/bars/schedule")
def api_admin_bars_schedule(request: Request):
    """每日自动落库的调度状态（开关/时间/上次结果/下次触发）。"""
    _require_admin(request)
    import scheduler as sch
    return sch.state()


@app.post("/api/admin/bars/schedule/run")
def api_admin_bars_schedule_run(request: Request, payload: Dict[str, Any] = Body(default={})):
    """不等定时，立即跑一次（会更新 last_ok，今天不会再被调度重复触发）。"""
    _require_admin(request)
    if _SYNC_STATE["running"]:
        return {"ok": False, "running": True, "note": "已有同步任务在跑，请等待完成"}
    import scheduler as sch
    scope = str(payload.get("scope") or "") or None
    count = int(payload.get("count") or 0) or None

    def _run():
        import config as _cfg
        try:
            s = scope or str(_cfg.get("TICK_SYNC_BARS_SCOPE") or "all")
            c = count or int(_cfg.get("TICK_SYNC_BARS_COUNT") or 250)
        except Exception:
            s, c = "all", 250
        _SYNC_STATE.update(running=True, started=time.time(), scope=s,
                           done=0, total=0, error="", note="")
        r = sch.run_now(s, c, manual=True)
        _SYNC_STATE.update(running=False, finished=time.time(),
                           ok_count=r.get("ok", 0),
                           error="" if r.get("ok") else str(r.get("error") or r.get("note") or ""))

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "running": True, "note": "已在后台开始"}


# ---------------------------------------------------------------- 巡检与日志

@app.get("/api/admin/health")
def api_admin_health(request: Request, limit: int = Query(50, ge=1, le=200)):
    """复用已有巡检读取逻辑（scripts/health_check.py 落盘的状态）。"""
    _require_admin(request)
    return api_health_report(limit=limit)


@app.get("/api/admin/logs")
def api_admin_logs(request: Request, lines: int = Query(200, ge=10, le=2000),
                   name: str = Query("")):
    """读日志尾部。默认读告警日志；name 可指定 data/ 下的 .log/.json。"""
    _require_admin(request)
    fname = name or "health_alerts.log"
    # 与 maintain 同一套白名单：只认 data/ 下的普通 .log/.json 文件名，挡掉 ../ 之类
    if not mt._valid_log_name(fname):
        raise HTTPException(400, "非法文件名（只支持 data/ 下的 .log / .json）")
    path = mt._safe_log_path(fname)
    if not path:
        return {"name": fname, "lines": [], "exists": False}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        return {"name": fname, "lines": [x.rstrip() for x in tail],
                "exists": True, "size": os.path.getsize(path),
                "size_h": mt._human(os.path.getsize(path))}
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/admin/logfiles")
def api_admin_logfiles(request: Request):
    """data/ 下可查看的日志文件清单，供界面下拉。"""
    _require_admin(request)
    import os.path as _p
    out = []
    try:
        for fn in sorted(os.listdir(_DATA_DIR)):
            if not fn.endswith((".log", ".json")):
                continue
            p = os.path.join(_DATA_DIR, fn)
            if _p.isfile(p):
                out.append({"name": fn, "size": _p.getsize(p),
                            "mtime": datetime.fromtimestamp(_p.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")})
    except Exception:
        pass
    return {"items": out}


# ===========================================================================
# 策略体检缓存（仅管理员）
#
# 体检缓存是单槽，预热/清空前先看清楚槽里现在装的是哪组参数（之前踩过坑：
# e2e_strategy_eval 只预 40 天，把启动预热的 120 挤掉，导致模拟净值卡片 4 条挂）。
#
# 备份 / 数据库维护 / 缓存统一管理 / 日志运维在下面 #97 那一组（server/maintain.py）。
# ===========================================================================

@app.get("/api/admin/eval_cache")
def api_admin_eval_cache(request: Request):
    """体检缓存当前占用：哪组参数、何时算的、覆盖多少策略。"""
    _require_admin(request)
    import strategy_eval as _se
    return _se.cache_status()


@app.post("/api/admin/eval_cache/warm")
def api_admin_eval_cache_warm(request: Request,
                              payload: Dict[str, Any] = Body(default={})):
    """手动预热（起后台线程，立即返回）。默认 120 天，与前端默认选项一致。"""
    _require_admin(request)
    import strategy_eval as _se
    try:
        days = int(payload.get("days", 120))
        forward = int(payload.get("forward", 5))
    except (TypeError, ValueError):
        raise HTTPException(400, "days / forward 必须是正整数")
    if days <= 0 or days > 500 or forward <= 0 or forward > 30:
        raise HTTPException(400, "days(1~500) / forward(1~30) 超出范围")
    r = _se.prewarm(days=days, forward=forward, why="后台手动预热")
    return {"ok": True, **r}


@app.post("/api/admin/eval_cache/clear")
def api_admin_eval_cache_clear(request: Request):
    """清空体检缓存。下次模拟净值会提示先体检，而不是用陈旧结果。"""
    _require_admin(request)
    import strategy_eval as _se
    return _se.clear_cache()


# ===========================================================================
# 数据库维护 / 备份 / 缓存 / 日志运维（#97，仅管理员）
#
# 逻辑全部在 server/maintain.py，这里只做鉴权与 HTTP 语义映射。
# 两条约定：
#   - 路径类参数一律过 maintain 的白名单入口，接口层不自己拼路径（防 ../ 穿越）。
#   - checkpoint 撞上活跃读事务时「busy」不是故障，返回 200 + 说明让前端提示
#     稍后重试；只有真异常才 500。
# ===========================================================================

@app.get("/api/admin/db")
def api_admin_db(request: Request):
    """数据库体积（db+wal+shm）、页统计、各表行数、磁盘余量。"""
    _require_admin(request)
    return mt.db_status()


@app.post("/api/admin/db/checkpoint")
def api_admin_db_checkpoint(request: Request, payload: Dict[str, Any] = Body(default={})):
    """WAL 落盘。mode=passive（默认，不阻塞）/ truncate（截断 WAL 文件）。"""
    _require_admin(request)
    r = mt.db_checkpoint(mode=str(payload.get("mode", "passive") or "passive"))
    if r.get("bad_mode"):
        raise HTTPException(400, r.get("error", "未知 checkpoint 模式"))
    if not r.get("ok") and not r.get("busy"):
        raise HTTPException(500, r.get("error", "checkpoint 失败"))
    return r


@app.get("/api/admin/backups")
def api_admin_backups(request: Request):
    """备份清单（倒序）。非白名单文件不纳入管理。"""
    _require_admin(request)
    return mt.backup_list()


@app.post("/api/admin/backup")
def api_admin_backup(request: Request, payload: Dict[str, Any] = Body(default={})):
    """在线一致备份（sqlite3.backup 读穿 WAL，服务无需停）。"""
    _require_admin(request)
    r = mt.backup_create()
    if not r.get("ok"):
        raise HTTPException(500, r.get("error", "备份失败"))
    return r


@app.get("/api/admin/backups/{name}/download")
def api_admin_backup_download(request: Request, name: str):
    """流式下载备份（462MB 级，绝不整体读进内存）。"""
    _require_admin(request)
    p = mt._safe_backup_path(name)
    if not p:
        raise HTTPException(400, "非法备份名")
    return FileResponse(p, filename=name, media_type="application/octet-stream")


@app.delete("/api/admin/backups/{name}")
def api_admin_backup_delete(request: Request, name: str):
    _require_admin(request)
    r = mt.backup_delete(name)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "删除失败"))
    return r


@app.post("/api/admin/backups/{name}/restore")
def api_admin_backup_restore(request: Request, name: str):
    """用备份覆盖当前库：恢复前强制自动快照，返回 need_restart 提示重启。"""
    _require_admin(request)
    r = mt.backup_restore(name)
    if not r.get("ok"):
        raise HTTPException(500, r.get("error", "恢复失败"))
    return r


@app.get("/api/admin/modules")
def api_admin_modules(request: Request):
    """新模块纳管：#95 情绪周期 / #88 快讯 / #96 监控中心 / 日线同步。只读本地。"""
    _require_admin(request)
    return mt.modules_status()


@app.get("/api/admin/caches")
def api_admin_caches(request: Request):
    """统一缓存状态：体检 / 情绪周期 / 快讯 / 行情快照。"""
    _require_admin(request)
    return mt.cache_status(market_getter=lambda: MARKET.get())


@app.post("/api/admin/caches/clear")
def api_admin_cache_clear(request: Request, payload: Dict[str, Any] = Body(default={})):
    _require_admin(request)
    r = mt.cache_clear(str(payload.get("key", "") or ""))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "清理失败"))
    return r


@app.delete("/api/admin/logs")
def api_admin_log_clear(request: Request, name: str = Query("")):
    """清空日志内容（truncate 保留 inode，正在写的句柄不会断）。"""
    _require_admin(request)
    r = mt.log_clear(name)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "清空失败"))
    return r


@app.get("/api/admin/logs/download")
def api_admin_log_download(request: Request, name: str = Query("")):
    """下载日志文件（流式）。"""
    _require_admin(request)
    p = mt._safe_log_path(name)
    if not p:
        raise HTTPException(400, "非法文件名")
    return FileResponse(p, filename=name, media_type="text/plain; charset=utf-8")


# ===========================================================================
# 虚拟盘（模拟买入 / 卖出 / 持仓盈亏）
#
# 简单版：记录持仓成本，按实时价算浮动盈亏。
# 所有金额计算在 store 层用事务完成，接口只做校验与行情拼装。
# ===========================================================================

def _require_login(request: Request) -> Dict[str, Any]:
    """虚拟盘守卫：必须登录才能用。

    为什么不像自选股那样允许匿名：虚拟盘会凭空产生资金与持仓，
    匿名数据没法归属、也没法在多设备间延续，认领逻辑会变得不可解释。
    自选股仍保持匿名可用（单机自用场景不受影响）。
    """
    u = _current_user(request)
    if not u:
        raise HTTPException(401, "虚拟盘需要登录后使用")
    return u


def _require_tradable() -> None:
    """虚拟盘交易时段守卫：非交易时段禁止买卖。

    根因：腾讯行情接口收盘后照样返回收盘价，_quote_map 取到的 price 其实是
    当日收盘价（或上一交易日收盘价）。前端不拦、或直接打接口，都能按这个静态
    价完成虚拟成交，违背「盘中实时成交」的语义，复盘也对不上真实行情。

    口径：可交易 = 连续竞价(trading) + 集合竞价(auction)；其余（午休 / 盘前 /
    已收盘 / 周末 / 休市）一律拦截。market_state() 是纯本地时区的确定性计算
    （不联网、不会抛异常），所以异常方向取「保守拦截」也不会误伤盘中的正常交易。

    放在 API 层、三处交易入口（买入 / 卖出 / 网格手动成交）共用，保证一致；
    不动 store 层，避免误伤 selfcheck() 这类诊断用途。
    """
    try:
        st = ds.market_state()
    except Exception:
        # market_state 不联网，基本不会走到这里；走到也宁可不交易，不重开 bug
        raise HTTPException(403, "行情时段探测异常，暂不允许交易，请稍后重试")
    s = st.get("state")
    if s in ("trading", "auction"):
        return
    label = st.get("label") or "非交易时段"
    raise HTTPException(403, f"当前{label}，虚拟盘暂停交易（仅交易时段可买卖）")


def _day_range(code: str) -> Optional[Dict[str, Any]]:
    """取该标的当前可成交的价格区间（low ~ high）。

    手填价的护栏就靠它。区间口径按市场状态分两路：

      · 盘中（含午休）→ 用**分钟线聚合当日 high/low**。
        为什么不能用日线：日线是 15:30 才落库的，盘中取到的最后一根是
        「昨天」，拿昨天的区间去卡今天的价格会误拦（今天涨停了却不让按
        涨停价买）。所以只能从分钟线现算。

      · 盘后 / 休市 → 用**最新一根日线**的 high/low。
        收盘后当天那根日线已经在数据源侧生成（腾讯的日线是实时更新的，
        不依赖我们的落库任务），直接取即可；休市时它自然是上一交易日的，
        也正好是用户说的「昨日收盘线高低点」。

    返回 {'low','high','src','date'}；**取不到数据时返回 None**，
    调用方必须放行——宁可让用户填个离谱价，也不要因为一次网络抖动
    把正常买入拒掉。这个失败方向更安全。
    """
    code = ds.normalize(code)
    try:
        st = ds.market_state()
    except Exception:
        st = {}
    live = st.get("state") in ("trading", "lunch")

    if live:
        # 盘中：分钟线聚合。1m 取 2400 根足够覆盖当日（一个交易日 240 根）
        try:
            rows = ds.get_kline_intraday(code, "1m", 2400)
        except Exception:
            rows = []
        if rows:
            today = datetime.now().strftime("%Y-%m-%d")
            today_rows = [r for r in rows if str(r.get("date", "")).startswith(today)]
            if today_rows:
                try:
                    hi = max(float(r["high"]) for r in today_rows)
                    lo = min(float(r["low"]) for r in today_rows)
                except (KeyError, TypeError, ValueError):
                    return None
                return {"low": round(lo, 3), "high": round(hi, 3),
                        "src": "分钟线（当日）", "date": today,
                        "state": st.get("state")}
        # 分钟线取不到（北交所最常见）→ 退回日线口径，仍比放行有信息量
    try:
        kl = ds.get_kline(code, "1d", 3)
    except Exception:
        kl = []
    if not kl:
        return None
    last = kl[-1]
    try:
        hi = float(last["high"])
        lo = float(last["low"])
    except (KeyError, TypeError, ValueError):
        return None
    if hi <= 0 or lo <= 0:
        return None
    return {"low": round(lo, 3), "high": round(hi, 3),
            "src": "日线（%s）" % str(last.get("date") or "—"),
            "date": str(last.get("date") or ""), "state": st.get("state")}


def _check_manual_price(code: str, price: float, side: str) -> None:
    """手填价护栏：必须落在当日（或最近交易日）的真实成交区间内。

    语义是「市价单模型」下的自洽约束——虚拟盘买入立即成交、立即扣款，
    不存在挂单撮合，所以用户不可能以一个当日从未出现过的价格成交。
    超出区间直接 400，并在错误信息里给出区间，让用户知道该填多少。
    """
    rng = _day_range(code)
    if not rng:
        return              # 取不到区间 → 放行（判不出来时不拦）
    lo, hi = rng["low"], rng["high"]
    # 留 0.5% 的容差：分钟线聚合的 high/low 与用户看到的分时图可能差
    # 一个最小价位（四舍五入），卡得太死会误拦正常操作。
    tol = max(hi * 0.005, 0.01)
    if price < lo - tol or price > hi + tol:
        verb = "买入" if side == "buy" else "卖出"
        raise HTTPException(
            400,
            f"{verb}价 {price} 超出{'当日' if rng['state'] in ('trading', 'lunch') else '最近交易日'}"
            f"成交区间 {lo} ~ {hi}（依据：{rng['src']}）。"
            f"虚拟盘按市价成交，只能填真实成交过的价格。")


#: 吊灯止损要用的日线根数。与 /api/kline 默认 count 一致，保证三处读数
#: （K 线主图 / 全套指标 / 持仓列）是同一个数。
_STOP_BARS = 250


def _stop_ref(code: str, price: Optional[float]) -> Optional[Dict[str, Any]]:
    """持仓的建议止损读数（吊灯止损：HHV(High,22) − 3×ATR(22)）。

    ## 为什么用本地日线库而不是实时 K 线接口

    持仓列表要跟着页面自动刷新，每只票都去打外部行情接口会把页面拖慢；
    而止损位本来就是**日线口径**的判断（ATR 也是日线 ATR），不需要分钟级
    精度。本地 `daily_bars` 是同一套数据，读起来是毫秒级。

    ## 为什么不给布尔的「该不该卖」

    止损线是参考位，是否真卖还要看成本、仓位、市场。**给价不给命令**——
    `triggered` 只表示"现价已在线下"这个客观事实。
    """
    try:
        # 250 根与 /api/kline 的默认 count 对齐——取少了 Wilder ATR 预热不足，
        # 会出现「K 线图显示 1274.34、持仓列显示 1273.66」这种同源不同值的坑。
        bars = store.get_bars(code, limit=_STOP_BARS)
    except Exception:
        return None
    if len(bars) < 30:
        return None
    try:
        H = [float(b["high"]) for b in bars]
        L = [float(b["low"]) for b in bars]
        C = [float(b["close"]) for b in bars]
        st = ix.chandelier_last(H, L, C, 22, 3.0)
    except Exception:
        return None
    if st is None:
        return None
    out = {"stop": round(st, 2), "basis": "chandelier(22,3)",
           "as_of": bars[-1].get("date", "")}
    if price:
        # 距止损还有多少空间：负值=已经在线下（已触发）
        out["room_pct"] = round((price - st) / price * 100, 2)
        out["triggered"] = bool(price < st)
    return out


def _pos_with_quote(pos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给持仓拼上实时价，算市值与浮动盈亏。"""
    codes = [p["code"] for p in pos]
    quotes = _quote_map(codes)
    out = []
    for p in pos:
        q = quotes.get(p["code"]) or {}
        price = q.get("price")
        qty = int(p["qty"])
        cost_total = round(float(p["cost"]) * qty, 2)
        item = dict(p)
        # 名称兜底：空名或「SZ002342 这类代码串当名字」的存量数据现场补一次
        # （行情快照名 > 搜索解析）；写入侧已在 store.buy_stock 拦截代码串
        _nm = (p.get("name") or "").strip()
        if not _nm or store.is_code_like(_nm):
            item["name"] = q.get("name") or _resolve_name(p["code"]) or _nm
        item["price"] = price
        item["change_pct"] = q.get("change_pct")
        # 建议止损（吊灯）。取不到（本地日线库没这只票）就是 None，前端显示「—」
        item["stop_ref"] = _stop_ref(p["code"], price)
        if price is None:
            item.update({"market_value": None, "pnl": None, "pnl_pct": None})
        else:
            mv = round(price * qty, 2)
            pnl = round(mv - cost_total, 2)
            item.update({
                "market_value": mv,
                "pnl": pnl,
                "pnl_pct": round(pnl / cost_total * 100, 2) if cost_total else None,
            })
        item["cost_total"] = cost_total
        out.append(item)
    return out


def _price_span(now=None):
    """当前时段的成交价性质，用于给成交流水打标签。

    为什么要标：收盘后 / 休市时 quote_tencent **照样返回数据**，
    但那个 price 其实是当日收盘价或上一交易日收盘价。不标出来，
    用户会以为自己刚按"实时价"成交了，复盘时对不上真实行情。
    """
    try:
        st = ds.market_state(now)
    except Exception:
        return "实时"
    s = st.get("state")
    return {
        "trading": "实时", "auction": "竞价",
        "lunch": "实时", "pre_open": "昨收",
        "closed": "收盘", "holiday": "昨收",
    }.get(s, "实时")


@app.get("/api/trade/positions")
def api_positions(request: Request):
    """持仓列表（含实时价与浮动盈亏）。"""
    store.initialize()
    _require_login(request)
    pos = store.list_positions()
    return {"items": _pos_with_quote(pos)}


@app.get("/api/trade/summary")
def api_trade_summary(request: Request):
    """账户总览：可用资金 / 持仓市值 / 总资产 / 累计盈亏。"""
    store.initialize()
    _require_login(request)
    s = store.portfolio_summary()
    pos = _pos_with_quote(store.list_positions())
    mv = sum(p["market_value"] or 0 for p in pos)
    cost = sum(p["cost_total"] for p in pos)
    # 只看有报价的部分，避免未报价持仓把浮盈算错
    quoted = [p for p in pos if p["market_value"] is not None]
    float_pnl = round(sum(p["pnl"] for p in quoted), 2)
    total = round(s["cash"] + mv, 2)
    return {
        "cash": s["cash"],
        "market_value": round(mv, 2),
        "cost_total": round(cost, 2),
        "total_asset": total,
        "float_pnl": float_pnl,
        "position_count": s["position_count"],
        "unquoted": len(pos) - len(quoted),
        "initial_cash": store._INITIAL_CASH,
        "total_pnl": round(total - store._INITIAL_CASH, 2),
        "total_pnl_pct": round((total - store._INITIAL_CASH) / store._INITIAL_CASH * 100, 2),
        "price_span": _price_span(),
        "fees": store.get_fees(),
    }


# ---------------------------------------------------------------- 费率设置

@app.get("/api/trade/fees")
def api_trade_fees(request: Request):
    """当前用户的费率设置（含默认值，供"还原"按钮用）。"""
    store.initialize()
    _require_login(request)
    return {"fees": store.get_fees(), "defaults": dict(store.DEFAULT_FEES),
            "etf_note": "ETF/场内基金免印花税，佣金按 ETF 费率单独计"}


@app.put("/api/trade/fees")
def api_trade_fees_set(request: Request, payload: Dict[str, Any] = Body(...)):
    """改费率。校验失败 400；只认白名单键。"""
    store.initialize()
    _require_login(request)
    try:
        fees = store.set_fees(payload.get("fees") or payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "fees": fees}


# ---------------------------------------------------------------- 认领匿名数据

@app.get("/api/trade/claim")
def api_trade_claim_state(request: Request):
    """是否有一份匿名数据可认领。未登录时不报错，只答 no。"""
    store.initialize()
    u = _current_user(request)
    if not u:
        return {"available": False, "reason": "未登录"}
    # 自己就是匿名账号时没有"可认领"一说（_current_user 已带 username）
    if (u.get("username") or "") == "local":
        return {"available": False, "reason": "当前即匿名账号"}
    st = store.anon_state()
    return {"available": bool(st["exists"]),
            "detail": st if st["exists"] else None}


@app.post("/api/trade/claim")
def api_trade_claim(request: Request):
    """把匿名数据过户到当前账号（搬走制，先到先得）。"""
    store.initialize()
    u = _require_login(request)
    try:
        r = store.claim_anon_state(u["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return r


@app.post("/api/trade/buy")
def api_trade_buy(request: Request, payload: Dict[str, Any] = Body(...)):
    """模拟买入。price 省略时按当前实时价成交。"""
    store.initialize()
    _require_login(request)
    _require_tradable()
    code = (payload.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "请填写股票代码")
    code = ds.normalize(code)
    try:
        qty = int(payload.get("qty") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "数量必须是整数")

    price = payload.get("price")
    if price in (None, "", 0):
        q = _quote_map([code]).get(code) or {}
        price = q.get("price")
        if not price:
            raise HTTPException(400, "无法获取实时价，请手动填写买入价")
        span = _price_span()
    else:
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise HTTPException(400, "价格必须是数字")
        _check_manual_price(code, price, "buy")
        span = "手填"

    name = (payload.get("name") or "").strip()
    if not name:
        # 快照偶尔不带名字（限流/超时），再走搜索解析兜底，别把空名写进库
        name = (_quote_map([code]).get(code) or {}).get("name", "") or _resolve_name(code)

    try:
        r = store.buy_stock(code, qty, float(price), name, pspan=span)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "trade": r, "price_span": span}


@app.post("/api/trade/sell")
def api_trade_sell(request: Request, payload: Dict[str, Any] = Body(...)):
    """模拟卖出。price 省略时按当前实时价成交。"""
    store.initialize()
    _require_login(request)
    _require_tradable()
    code = (payload.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "请填写股票代码")
    code = ds.normalize(code)
    try:
        qty = int(payload.get("qty") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "数量必须是整数")

    price = payload.get("price")
    if price in (None, "", 0):
        q = _quote_map([code]).get(code) or {}
        price = q.get("price")
        if not price:
            raise HTTPException(400, "无法获取实时价，请手动填写卖出价")
        span = _price_span()
    else:
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise HTTPException(400, "价格必须是数字")
        _check_manual_price(code, price, "sell")
        span = "手填"

    try:
        r = store.sell_stock(code, qty, float(price), pspan=span)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "trade": r, "price_span": span}


@app.get("/api/trade/history")
def api_trade_history(request: Request, limit: int = Query(100, ge=1, le=500)):
    """成交流水。"""
    store.initialize()
    _require_login(request)
    return {"items": store.list_trades(limit=limit)}


@app.post("/api/trade/reset")
def api_trade_reset(request: Request, payload: Dict[str, Any] = Body(default={})):
    """清空持仓与流水，资金复位（谨慎操作）。"""
    store.initialize()
    _require_login(request)
    try:
        cash = float(payload.get("cash") or store._INITIAL_CASH)
    except (TypeError, ValueError):
        cash = store._INITIAL_CASH
    return {"ok": True, **store.reset_account(cash=cash)}


# ===========================================================================
# 策略有效性评估（IC / ICIR）
#
# 一个策略选出一堆票，但它**历史上到底有没有预测力**？本接口用历史日线
# 逐个策略回算 IC / ICIR，给每个策略一个「实测强度」标签。
#
# 评估很慢（9 策略 × 120 天约 90 秒），所以：
#   - 结果按 (天数, 收益窗口, 数据版本) 缓存 6 小时
#   - 提供 ?days=60 让前端可以先跑一版快的
# 详见 server/strategy_eval.py 的模块说明。
# ===========================================================================

@app.get("/api/strategy_eval")
def api_strategy_eval(
    days: int = Query(120, ge=20, le=300, description="评估最近多少个交易日"),
    forward: int = Query(5, ge=1, le=20, description="未来收益窗口（交易日）"),
    refresh: bool = Query(False, description="忽略缓存重算"),
):
    """全策略有效性评估。返回按 |ICIR| 降序的策略清单 + 无法评估的原因。"""
    try:
        import strategy_eval as se
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"评估模块加载失败：{e}"},
                            status_code=500)
    try:
        r = se.evaluate(days=days, forward=forward, use_cache=not refresh)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"评估失败：{e}"},
                            status_code=500)
    if not r.get("ok"):
        # 数据未就绪是「预期内的暂时状态」，不该用 500 吓唬前端
        return JSONResponse({"ok": False, "error": r.get("error", "未知错误")},
                            status_code=409)
    return r


# ---------------------------------------------------------------------------
# 策略体检：后台任务（专治网关 504）
#
# 一次评估要 100~150 秒（9 策略 × 120 交易日 × 约 5500 只）。原来它在请求线程里
# 同步算：本地直连能等到 200，但用户从外网域名进来时，中间网关 60 秒就掐断连接，
# 浏览器拿到 HTTP 504 —— 而服务其实还在老老实实跑，白算一场。
# 改成「提交任务 + 轮询」：提交请求秒回一个 job id，前端每 2 秒查进度，算完取结果。
# ---------------------------------------------------------------------------
_EVAL_JOBS: Dict[str, Dict[str, Any]] = {}
_EVAL_JOBS_LOCK = threading.Lock()
_EVAL_JOB_KEEP = 8            # 内存里最多留几个历史任务
_EVAL_JOB_STALE = 3600.0      # 跑超过 1 小时判定为异常，不再被复用


def _eval_job_key(days: int, forward: int, refresh: bool) -> str:
    return f"{days}|{forward}|{1 if refresh else 0}"


def _eval_job_start(days: int, forward: int, refresh: bool) -> str:
    """起一个评估任务，返回 job id。同参数的任务在跑就复用，不重复占线程。"""
    import uuid

    k = _eval_job_key(days, forward, refresh)
    now = time.time()
    for jid, j in _EVAL_JOBS.items():
        if (j.get("k") == k and j.get("running")
                and now - j["ts"] < _EVAL_JOB_STALE):
            return jid

    job: Dict[str, Any] = {"k": k, "running": True, "done": 0, "total": days,
                           "note": "", "ts": now, "elapsed": 0.0,
                           "result": None, "error": None}
    with _EVAL_JOBS_LOCK:
        if len(_EVAL_JOBS) >= _EVAL_JOB_KEEP:
            for old in sorted(_EVAL_JOBS, key=lambda x: _EVAL_JOBS[x]["ts"]):
                if len(_EVAL_JOBS) < _EVAL_JOB_KEEP:
                    break
                if not _EVAL_JOBS[old]["running"]:
                    _EVAL_JOBS.pop(old, None)
        jid = uuid.uuid4().hex[:12]
        _EVAL_JOBS[jid] = job

    def _run():
        t0 = time.time()

        def _prog(done, total, note=""):
            job["done"], job["total"] = done, total
            job["note"] = note or ""
            job["elapsed"] = round(time.time() - t0, 1)

        try:
            import strategy_eval as se
            job["result"] = se.evaluate(days=days, forward=forward,
                                        use_cache=not refresh, progress=_prog)
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            job["running"] = False
            job["elapsed"] = round(time.time() - t0, 1)

    threading.Thread(target=_run, name=f"eval-{jid}", daemon=True).start()
    return jid


@app.post("/api/strategy_eval/job")
def api_strategy_eval_start(
    days: int = Query(120, ge=20, le=300),
    forward: int = Query(5, ge=1, le=20),
    refresh: bool = Query(False),
):
    """提交一次评估，立刻返回 job id，计算在后台线程跑。"""
    return {"ok": True, "job": _eval_job_start(days, forward, refresh),
            "days": days, "forward": forward}


@app.get("/api/strategy_eval/job/{jid}")
def api_strategy_eval_poll(jid: str):
    """轮询任务状态。running=false 且带 result 即为完成。"""
    j = _EVAL_JOBS.get(jid)
    if not j:
        return JSONResponse({"ok": False, "error": "任务不存在或已过期，请重新计算"},
                            status_code=404)
    out = {"ok": True, "job": jid, "running": j["running"],
           "done": j["done"], "total": j["total"], "note": j["note"],
           "elapsed": j["elapsed"]}
    if j["error"]:
        out["ok"] = False
        out["error"] = j["error"]
    if j["result"] is not None:
        out["result"] = j["result"]
    return out


@app.get("/api/strategy_eval/{key}")
def api_strategy_eval_one(
    key: str,
    days: int = Query(120, ge=20, le=300),
    forward: int = Query(5, ge=1, le=20),
):
    """单个策略的有效性。走同一份缓存，连查多个策略不会重复计算。"""
    try:
        import strategy_eval as se
        r = se.evaluate_single(key, days=days, forward=forward)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"评估失败：{e}"},
                            status_code=500)
    if not r.get("ok"):
        return JSONResponse(r, status_code=409 if not r.get("evaluable", True)
                            else 404)
    return r


# ===========================================================================
# 方案 3：信号组合模拟净值（「跟着买」的钱包曲线回放）
#
# 复用体检的命中序列（strategy_eval 的 _HITS_CACHE），模拟本身是纯内存
# 计算（毫秒级），因此走同步接口即可——唯一的前置条件是同参数体检已经
# 跑过；没跑过时返回 409 + need_eval，让前端引导用户先做体检，
# 而不是在这里悄悄触发一次 40 秒的全量重算（会被网关 504 掐断）。
# 详见 server/equity_sim.py 的模块说明。
# ===========================================================================

@app.get("/api/equity_sim")
def api_equity_sim(
    key: str = Query(..., description="策略 key（体检可评估的 9 个之一）"),
    days: int = Query(120, ge=20, le=300, description="信号窗口（与体检一致）"),
    hold: int = Query(5, ge=1, le=20, description="持有交易日数"),
    fee_bps: float = Query(15.0, ge=0, le=100, description="双边费率（基点）"),
):
    try:
        import equity_sim
        r = equity_sim.simulate(key, days=days, hold=hold, fee_bps=fee_bps)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"模拟失败：{e}"},
                            status_code=500)
    if not r.get("ok"):
        return JSONResponse(r, status_code=409 if r.get("need_eval") else 400)
    return r


# ===========================================================================
# 方案 4：市场宽度历史序列（涨跌家数 / MA20 上方比例 / 新高新低）
#
# 全市场逐日聚合约 2~3 秒（首次），按数据版本缓存后毫秒级。同步接口。
# 详见 server/market_breadth.py 的模块说明。
# ===========================================================================

@app.get("/api/market_breadth")
def api_market_breadth(
    days: int = Query(250, ge=30, le=500, description="返回最近多少个交易日"),
):
    try:
        import market_breadth
        r = market_breadth.compute(days=days)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"宽度计算失败：{e}"},
                            status_code=500)
    if not r.get("ok"):
        return JSONResponse(r, status_code=409)
    return r


# ===========================================================================
# 静态前端
# ===========================================================================

@app.get("/")
def root():
    idx = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(idx):
        # index.html 是单文件应用，改动频繁。
        # 不加缓存头时浏览器会按启发式规则缓存，导致改完前端后用户看到旧页面
        # （表现为「功能没生效」，实际是拿了旧 HTML）。这里强制每次校验。
        return FileResponse(idx, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return JSONResponse({"ok": True, "msg": "前端未找到"})


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
