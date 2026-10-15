"""AI 舆情优化器（P1：AI 提议 + 代码护栏 + 人工勾选应用，绝不动基础层）。

闭环（#188/#189 工作流的自动化下半场）：
  每日对账/巡检报告里的错失信号（修正率异常 / 小样本低置信 / 词表失衡）
  → build_context 挑出 flagged 板块 + 真实快讯样本
  → propose 让 LLM 产出 JSON 补丁（只写板块级催化剂词表扩展层）
  → validate_patch 代码护栏逐条校验（schema / 证据真实性 / A-B 方向翻转 /
     基础层 selfcheck 零回归 / 批量上限）
  → apply_patch 对人工勾选子集重新过护栏后落盘 + 审计日志
  → 次日对账准确率 = 验收 KPI（诚实口径：严格回放需存对账时点快照，见下）

为什么护栏在代码而不是 prompt：LLM 会一本正经编造样本外"证据"、会把基础层词
当成可删、会一次吐 30 条改动。代码侧逐条验，不过就不进候选，人工只需在已通过
的条目里勾选——AI 负责想，代码负责验，人负责拍板。

已知天花板（ponytail：先描述、后预测）：
  · "次日对账准确率提升"是这套闭环唯一的诚实 KPI；样本回归只覆盖"给定证据
    文本的逐条方向翻转"，不等于全量新闻准确率提升，别在 UI 上夸大成"准确率+X%"。
  · 严格回放（用对账时点的快照验证补丁）需要额外存储，P1 不做；当前 A/B 校验
    用的是当前窗口重算，口径与面板/对账一致（同 _sector_stats 原语）。
"""
from __future__ import annotations
import contextlib
import datetime as _dt
import io
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import sector_sentiment as ss
import sector_verify as sv
import stock_sector_map as ssm

_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_ROOT, "..", "data", "sentiment_optimizer_log.jsonl")

MAX_PATCH = 10            # 单批补丁上限（防 LLM 一次吐 30 条）
FLAG_CORRECT_RATIO = 0.3  # 板块词修正占比 ≥ 此值 → flagged（#189 医药 9/16=56%）
FLAG_IMBALANCE = 3        # 词表利好/利空词数差 ≥ 此值 → flagged（#189 医药 69:35）
MIN_CONFIDENCE = 0.5      # 置信度低于此值的补丁条目直接拒


# ───────────────────────── 上下文构建（信号 → flagged 板块 + 证据样本） ─────────────────────────

def _lexicon_imbalance() -> Dict[str, Tuple[int, int]]:
    """各板块基础层词表失衡度 → {板块: (利好词数, 利空词数)}。

    #189 实证：医药 69 利好 vs 35 利空（+34）、能源 29:6、农业 28:8、军工 20:9
    ——偏多侧系统性过灵，「医保」被列为医药利好词即由此而来。
    """
    return {sec: (len(ss.SECTOR_BULLISH.get(sec, [])),
                  len(ss.SECTOR_BEARISH.get(sec, [])))
            for sec in ss.SECTOR_KEYWORDS}


def build_context(hours: int = 24,
                  items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """从当前快讯窗口挑 flagged 板块 + 证据样本（供 LLM 与护栏共用）。

    items 可注入（自检/测试用）；缺省走 sector_verify._load_news——与收盘对账
    同一窗口、同一归因原语（_sector_stats），保证「优化器看到的信号」就是
    「对账报告里报的信号」，不第二套口径。
    """
    if items is None:
        items = sv._load_news(hours)
    if not items:
        return {"ok": False, "reason": f"近 {hours}h 无快讯（新闻缓存为空？）",
                "flagged": [], "samples": []}
    per = sv._sector_stats(items)
    imb = _lexicon_imbalance()
    flagged: List[Dict[str, Any]] = []
    for sec, a in per.items():
        n = a["n"]
        if not n:
            continue
        reasons: List[str] = []
        if a["corrected"] / n >= FLAG_CORRECT_RATIO:
            reasons.append(f"修正率异常：{a['corrected']}/{n} 条被板块词修正方向")
        if n < sv.MIN_N:
            reasons.append(f"小样本低置信：仅 {n} 条撑方向")
        b_n, d_n = imb.get(sec, (0, 0))
        if abs(b_n - d_n) >= FLAG_IMBALANCE:
            reasons.append(f"词表失衡：利好词 {b_n} vs 利空词 {d_n}（差 {b_n - d_n}）")
        if not reasons:
            continue
        flagged.append({
            "sector": sec, "reasons": reasons, "n": n,
            "pos": a["pos"], "neg": a["neg"], "neu": a["neu"],
            "corrected": a["corrected"],
            "corrected_ratio": round(a["corrected"] / n, 3),
            "old_ratio": round(a["old"] / n, 3),
            "sent_dir": sv._dir_of(a),
            "bull_words": b_n, "bear_words": d_n,
        })
    # 证据样本池：flagged 板块的真实快讯全文（evidence 必须逐字取自这里，
    # 供 LLM 摘录 + 护栏 substring 校验；截断会让"逐字"失去意义，故给全文）
    flag_secs = {f["sector"] for f in flagged}
    pool: Dict[str, List[Dict[str, Any]]] = {}
    ref = max((int(it.get("pub_ts") or 0) for it in items), default=0)
    for it in items:
        content = it.get("content", "") or ""
        tone = (it.get("sentiment") or {}).get("tone", "neutral")
        secs = ssm.sectors_for_stocks(it.get("stocks")) or ss.map_sectors(content)
        if not secs:
            continue
        sec_net = ss.detect_catalysts(content, secs)["sec_net"]
        eff = tone
        if sec_net > 0:
            eff = "pos"
        elif sec_net < 0:
            eff = "neg"
        for s in secs:
            if s not in flag_secs or len(pool.get(s, [])) >= 5:
                continue
            pool.setdefault(s, []).append({
                "sector": s, "text": content, "tone": eff, "origin": tone,
                "corrected": bool(sec_net) and eff != tone,
                "age_h": round(max(0.0, (ref - int(it.get("pub_ts") or 0)) / 3600.0), 1),
            })
    samples = [x for sec in sorted(pool) for x in pool[sec]]
    flagged.sort(key=lambda f: -f["corrected_ratio"])
    return {"ok": True, "hours": hours, "n_items": len(items),
            "flagged": flagged, "samples": samples,
            "generated_at": int(time.time())}


# ───────────────────────── LLM 提议（输出契约：报告 + ```json 补丁块） ─────────────────────────

_SYS = """你是A股板块舆情词表优化员。给你「问题板块 + 触发原因 + 真实快讯样本」，
产出板块级利空/利好催化剂词表补丁（写入 sector_sentiment 的用户扩展层）。

输出契约（严格遵守）：
1) 先用 ≤150 字中文简述诊断思路；
2) 然后必须给出一个 ```json 代码块，仅含：
{"patch":[{"action":"add|remove","sector":"板块名","polarity":"bull|bear",
"word":"词","evidence":"样本原句（逐字摘录）","reason":"≤40字理由","confidence":0.0~1.0}]}

规则（违反任意一条都会被代码护栏拒绝，白费 Token）：
- evidence 必须逐字取自给定样本中【同板块】的那条，系统逐条校验；
- add 的 word 会在证据文本上做子串/词边界命中校验，且加词后样本对该板块的
  净情绪必须变成你声称的方向（bull=利好 / bear=利空），否则拒绝；
- remove 只能删「当前扩展层」里列出的词（基础层词删不动）；删除后样本净情绪
  必须发生变化，否则拒绝；
- 一次最多 10 条；只允许 patch 下方列出的 flagged 板块；
- 板块专属词只收：事件/政策/价格/供需类硬词。严禁：品类词（芯片/算力/导弹/
  光模块）、涨跌措辞（上涨/下跌/涨停/大跌）、泛化动作词（发布/推出/合作/
  签约/突破/落地）、纯结果词（放量/异动）；
- 疫情类方向与直觉相反：爆发/扩散/失控=医药利好，得到控制/病例下降=医药利空，
  不要把这类再"修正"回直觉方向；
- confidence<0.6 的条目不要写；没把握就少写，写不出就给空 patch。"""


def _ctx_prompt(ctx: Dict[str, Any]) -> str:
    """把 build_context 的结构化信号渲染成 LLM user prompt。"""
    lines = ["【问题板块与触发原因】"]
    for f in ctx.get("flagged", []):
        lines.append(
            f"· {f['sector']}（n={f['n']} 偏多{f['pos']}/偏空{f['neg']}/中性{f['neu']}，"
            f"对账方向={f['sent_dir']}，修正率={f['corrected_ratio']:.0%}，"
            f"旧闻占比={f['old_ratio']:.0%}，词表 利好{f['bull_words']}/利空{f['bear_words']}词）")
        for r in f["reasons"]:
            lines.append(f"    - {r}")
    lines.append("\n【真实快讯样本（evidence 只能逐字取自这里）】")
    for s in ctx.get("samples", []):
        tag = "当前读作:" + s["tone"] + ("(被板块词修正)" if s["corrected"] else "")
        lines.append(f"[{s['sector']}] {s['text']}  （{tag}）")
    lines.append("\n【flagged 板块现有基础层词表】")
    for f in ctx.get("flagged", []):
        sec = f["sector"]
        lines.append(f"· {sec} 利好词({f['bull_words']})：{'、'.join(ss.SECTOR_BULLISH.get(sec, []))}")
        lines.append(f"· {sec} 利空词({f['bear_words']})：{'、'.join(ss.SECTOR_BEARISH.get(sec, []))}")
    lines.append("\n【当前扩展层（可 remove 的词）】")
    ex = ss.list_extra()
    if ex:
        for sec, pol in ex.items():
            lines.append(f"· {sec} 扩展利好：{'、'.join(pol.get('bull', [])) or '无'}"
                         f"；扩展利空：{'、'.join(pol.get('bear', [])) or '无'}")
    else:
        lines.append("（空）")
    return "\n".join(lines)


def _parse_patch(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """从 LLM 输出提取报告正文 + 规范化补丁列表（截到 MAX_PATCH）。

    机读块解析失败 → 空补丁（不影响报告展示）；action/polarity 统一小写，
    word/evidence 去首尾空白；confidence 非法值记 0.0（随后被护栏拒）。
    """
    m = re.search(r"```json\s*(.*?)```", text or "", re.S)
    rep = re.sub(r"```json.*?```", "", text or "", flags=re.S).strip() if m else (text or "").strip()
    patch: List[Dict[str, Any]] = []
    if m:
        try:
            obj = json.loads(m.group(1))
            raw = obj.get("patch") if isinstance(obj, dict) else obj
            if isinstance(raw, list):
                for it in raw[:MAX_PATCH]:
                    if not isinstance(it, dict):
                        continue
                    try:
                        conf = round(float(it.get("confidence")), 2)
                    except (TypeError, ValueError):
                        conf = 0.0
                    patch.append({
                        "action": str(it.get("action") or "").strip().lower(),
                        "sector": str(it.get("sector") or "").strip(),
                        "polarity": str(it.get("polarity") or "").strip().lower(),
                        "word": str(it.get("word") or "").strip(),
                        "evidence": str(it.get("evidence") or "").strip(),
                        "reason": str(it.get("reason") or "").strip()[:120],
                        "confidence": conf,
                    })
        except Exception:
            patch = []
    return rep, patch


def propose(ctx: Dict[str, Any],
            override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """调 LLM 产出补丁。返回 {"ok","report","patch","flagged"}；LLM 异常上抛。"""
    import llm_client
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": _ctx_prompt(ctx)}]
    # ponytail: chat 抛 RuntimeError 即降级，由端点转成前端可读错误
    raw = llm_client.chat(msgs, max_tokens=1800, temperature=0.3, override=override)
    report, patch = _parse_patch(raw)
    return {"ok": True, "report": report, "patch": patch,
            "flagged": [f["sector"] for f in ctx.get("flagged", [])]}


# ───────────────────────── 代码护栏（不过就不进候选，人工只勾已通过的） ─────────────────────────

def _ab_net(text: str, sector: str, bull: List[str], bear: List[str]) -> Tuple[int, List[str], List[str]]:
    """给定合并词表，算样本对某板块的专属净情绪（与 detect_catalysts 同口径）。"""
    sb = ss._match_terms(text, bull.get(sector, []))
    sd = ss._match_terms(text, bear.get(sector, []))
    return len(sb) - len(sd), sb, sd


def validate_patch(patch: List[Dict[str, Any]],
                   samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """逐条过护栏。返回 {"ok","items":[{item,ok,checks,msg}], "n_pass","n_fail"}。

    checks 五项：
      schema    —— action/sector/polarity/word/confidence/reason 形态
      evidence  —— evidence 逐字 ∈ 同板块真实样本（防编造）
      ab        —— A/B 方向翻转：add 加词后样本净情绪变为声称方向且词命中；
                   remove 词当前在证据上生效、删后净情绪确实改变
      applyable —— 能落进扩展层（remove 目标须在扩展层；add 不许与基础层同极性重复）
      selfcheck —— 单独应用该条后跑基础层 selfcheck，零回归才算过
    """
    by_sec: Dict[str, List[Dict[str, Any]]] = {}
    for s in samples or []:
        if isinstance(s, dict) and s.get("sector"):
            by_sec.setdefault(s["sector"], []).append(s)
    extra = ss.list_extra()
    results: List[Dict[str, Any]] = []
    for it in (patch or [])[:MAX_PATCH]:
        checks = {"schema": False, "evidence": False, "ab": False,
                  "applyable": False, "selfcheck": False}
        msg = ""
        act, sec = it.get("action"), it.get("sector")
        pol, word = it.get("polarity"), it.get("word") or ""
        ev, conf = it.get("evidence") or "", it.get("confidence") or 0.0
        # ── schema
        if act not in ("add", "remove"):
            msg = "action 必须是 add 或 remove"
        elif sec not in ss.SECTOR_KEYWORDS:
            msg = f"未知板块：{sec!r}"
        elif pol not in ("bull", "bear"):
            msg = "polarity 必须是 bull 或 bear"
        elif not word or len(word) > 12:
            msg = "词为空或超 12 字"
        elif not (0.0 <= conf <= 1.0) or conf < MIN_CONFIDENCE:
            msg = f"confidence 须 ≥{MIN_CONFIDENCE} 且 ≤1（防低置信瞎猜）"
        elif not it.get("reason"):
            msg = "缺少理由"
        else:
            checks["schema"] = True
        # ── evidence（逐字 ∈ 同板块样本）
        sample = None
        if checks["schema"]:
            hit = [s for s in by_sec.get(sec, []) if ev and ev in (s.get("text") or "")]
            if not hit:
                msg = "证据不在该板块样本中（须逐字取自给定样本，编造/改写/截断都不行）"
            else:
                checks["evidence"] = True
                sample = hit[0]
        # ── A/B 方向翻转（纯内存合并，不落盘）
        if sample is not None:
            before = ss.detect_catalysts(sample["text"], [sec])
            bn = before["sec_net"]
            if act == "add":
                ex = json.loads(json.dumps(extra))
                ex.setdefault(sec, {"bull": [], "bear": []})
                other = "bear" if pol == "bull" else "bull"
                ex[sec][other] = [w for w in ex[sec][other] if w != word]
                if word not in ex[sec][pol]:
                    ex[sec][pol].append(word)
                bull, bear = ss._tables(ex)
                an, sb, sd = _ab_net(sample["text"], sec, bull, bear)
                fires = (word in sb) if pol == "bull" else (word in sd)
                if not fires:
                    msg = f"「{word}」未在证据文本中命中（子串/词边界都不匹配）"
                elif pol == "bull" and an <= 0:
                    msg = f"加词后样本净情绪 {an}≤0，与声称的利好方向矛盾（或被对侧词抵消）"
                elif pol == "bear" and an >= 0:
                    msg = f"加词后样本净情绪 {an}≥0，与声称的利空方向矛盾（或被对侧词抵消）"
                else:
                    checks["ab"] = True
            else:  # remove
                cur = extra.get(sec, {})
                if not (word in (cur.get("bull") or []) or word in (cur.get("bear") or [])):
                    msg = "删除目标不在扩展层（基础层词不可删；要覆盖请用反极性 add）"
                else:
                    checks["applyable"] = True
                    fired = word in before["sec_bull"] or word in before["sec_bear"]
                    if not fired:
                        msg = "该词当前未在证据文本中生效，删除无意义"
                    else:
                        ex = json.loads(json.dumps(extra))
                        ex[sec] = {"bull": [w for w in (cur.get("bull") or []) if w != word],
                                   "bear": [w for w in (cur.get("bear") or []) if w != word]}
                        if not ex[sec]["bull"] and not ex[sec]["bear"]:
                            ex.pop(sec)
                        bull, bear = ss._tables(ex)
                        an, _, _ = _ab_net(sample["text"], sec, bull, bear)
                        if an == bn:
                            msg = f"删除后样本净情绪仍为 {bn}，该词不构成方向驱动"
                        else:
                            checks["ab"] = True
        # ── applyable + selfcheck（add 路径：试写扩展层；两条路都跑基础层自检）
        if checks["schema"] and checks["evidence"] and checks["ab"]:
            orig = ss._load_extra()
            try:
                try:
                    if act == "add":
                        ss.add_word(sec, pol, word)      # 基础层同极性重复会抛 ValueError
                    else:
                        removed, _ = ss.remove_word(sec, word)
                        if not removed:
                            raise ValueError("扩展层无此词")
                    checks["applyable"] = True
                except ValueError as e:
                    msg = f"应用被拒：{e}"
                if checks["applyable"]:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):   # 自检输出消音，别污染接口返回
                        fails = ss.selfcheck()
                    if fails:
                        msg = f"应用后基础层自检失败 {fails} 条（拒绝，防改崩底层规则）"
                    else:
                        checks["selfcheck"] = True
            finally:
                ss._save_extra(orig)                        # 护栏试写必须还原，不泄漏候选
                ss.rebuild(orig)
        results.append({"item": it, "ok": all(checks.values()),
                        "checks": checks, "msg": msg})
    n_pass = sum(1 for r in results if r["ok"])
    return {"ok": bool(results) and n_pass == len(results),
            "items": results, "n_pass": n_pass, "n_fail": len(results) - n_pass,
            "n_total": len(results),
            "truncated": len(patch or []) > MAX_PATCH}


# ───────────────────────── 应用 + 审计日志 ─────────────────────────

def _log(rec: Dict[str, Any]) -> Dict[str, Any]:
    rec = dict(rec)
    rec["ts"] = int(time.time())
    rec["time"] = _dt.datetime.now().strftime("%m-%d %H:%M")
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass      # 日志写不落不阻塞应用（词表已改，审计尽力而为）
    return rec


def list_log(n: int = 20) -> List[Dict[str, Any]]:
    """最近 n 条审计日志（新→旧）。"""
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except Exception:
        return []
    out = []
    for ln in reversed(lines[-n:]):
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def ss_list_extra() -> Dict[str, Dict[str, List[str]]]:
    """扩展层现状（门面：端点不必再 import sector_sentiment）。"""
    return ss.list_extra()


def apply_patch(selected: List[Dict[str, Any]],
                samples: List[Dict[str, Any]],
                via: str = "ui") -> Dict[str, Any]:
    """对人工勾选的子集重新过护栏 → 应用 → 审计日志。护栏不可绕过。

    via: 来源标记（ui=后台人工应用 / selfcheck=自检 roundtrip），进审计日志，
    避免自检的真实写入被误读成人工操作（日志只增不减，不删不改）。
    并发说明：add/remove 是整文件读-改-写，管理员低频操作下最后写入者获胜；
    审计日志记录全部提交项，可追溯。真要多管理员并发再上文件锁（ponytail：没必要）。
    """
    res = validate_patch(selected, samples)
    applied, failed = [], []
    for r in res["items"]:
        it = r["item"]
        if not r["ok"]:
            failed.append({"word": it.get("word"), "sector": it.get("sector"),
                           "msg": r["msg"]})
            continue
        try:
            if it["action"] == "add":
                ss.add_word(it["sector"], it["polarity"], it["word"])
            else:
                ss.remove_word(it["sector"], it["word"])
            applied.append({"action": it["action"], "sector": it["sector"],
                            "polarity": it["polarity"], "word": it["word"],
                            "reason": it.get("reason", "")})
        except ValueError as e:
            failed.append({"word": it.get("word"), "sector": it.get("sector"),
                           "msg": f"应用失败：{e}"})
    if applied:
        _log({"action": "apply", "via": via, "n": len(applied), "items": applied})
    return {"ok": bool(applied), "applied": applied, "failed": failed,
            "results": res, "log": LOG_PATH}


# ───────────────────────── P2 每日自动提议（调度方只落待审，人工勾选才应用） ─────────────────────────

PENDING_PATH = os.path.join(_ROOT, "..", "data", "sentiment_optimizer_pending.json")


def _save_pending(d: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        print(f"[sentiment-opt] 待审文件落盘失败：{e}")


def load_pending() -> Optional[Dict[str, Any]]:
    """最近一次每日自动提议（无则 None）。形状对齐 /optimize 响应，前端复用渲染。"""
    try:
        with open(PENDING_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def run_daily(hours: int = 24,
              items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """每日自动提议（P2）：build_context → LLM propose → 护栏 → 落盘待审。

    · 不应用（人工勾选才应用）——与 P1 的人工决策边界一致；
    · LLM 未启用/未配置/网络错都上抛，由调度方 try 兜底（巡检不能陪葬）；
    · items 可注入（自检用，不读真实缓存）；
    · 落盘形状对齐 /api/admin/sentiment/optimize 响应（ok/empty/report/
      ctx/patch/n_pass/n_fail + auto:True），前端一套渲染代码复用。
    """
    ctx = build_context(hours=hours, items=items)
    if not ctx.get("ok") or not ctx.get("flagged"):
        data = {"ok": True, "empty": True, "auto": True,
                "msg": ctx.get("reason") or "当前窗口没有 flagged 板块，无需优化",
                "ctx": {"flagged": [], "samples": []}, "patch": [],
                "ts": int(time.time()),
                "time": _dt.datetime.now().strftime("%m-%d %H:%M")}
        _save_pending(data)
        return data
    pr = propose(ctx)                      # LLM 异常上抛
    val = validate_patch(pr["patch"], ctx["samples"])
    data = {"ok": True, "empty": False, "auto": True,
            "report": pr.get("report", ""),
            "ctx": {"flagged": ctx["flagged"], "samples": ctx["samples"]},
            "patch": val["items"],
            "n_pass": val["n_pass"], "n_fail": val["n_fail"],
            "ts": int(time.time()),
            "time": _dt.datetime.now().strftime("%m-%d %H:%M")}
    _save_pending(data)
    return data


# ───────────────────────── 自检（离线，不调 LLM、不动真实缓存） ─────────────────────────

def selfcheck() -> int:
    """假补丁全链路验证：上下文信号 → 护栏正/负例 → apply roundtrip。"""
    fails = 0

    def ok(label, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            fails += 1

    # 1) 词表失衡诊断（#189 已知结论锁定；用相对阈值，防基础层正常演进而误报）
    imb = _lexicon_imbalance()
    ok("失衡诊断：医药 利好词显著多于利空词(#189: 69 vs 35)",
       imb["医药"][0] - imb["医药"][1] >= 20)
    ok("失衡诊断：覆盖全部板块", len(imb) == len(ss.SECTOR_KEYWORDS))

    # 统一状态：后续「前置」断言是基础层性质（如「医保」在基础层被误列利好），
    # 须在无扩展层覆盖的状态下验证；真实扩展层可能已被人工应用过（线上实测：
    # 应用「医保」反极性覆盖后本条假失败）。先清空、finally 整体还原。
    _orig = ss.list_extra()
    try:
        if _orig:
            ss._save_extra({})
            ss.rebuild({})
        _selfcheck_body(ok)
    finally:
        ss._save_extra(_orig)
        ss.rebuild(_orig)
    ok("还原：扩展层恢复原状（含人工已应用的覆盖）", ss.list_extra() == _orig)

    return fails


def _selfcheck_body(ok) -> None:
    """自检主体（在扩展层已清空的确定性状态下运行）。"""

    # 2) build_context 挑 flagged（注入假 items，不读真实缓存）
    _B = 2_000_000_000
    fake_items = [
        # 三条都命中医药且都被板块词修正方向（疫情类：词典 neg/中性 → 板块词 pos）
        {"content": "某地疫情扩散，感染人数持续上升",
         "sentiment": {"tone": "neg", "score": -2.0}, "stocks": [], "pub_ts": _B},
        {"content": "不明原因肺炎出现，排查范围扩大",
         "sentiment": {"tone": "neutral", "score": 0.0}, "stocks": [], "pub_ts": _B},
        {"content": "某地疫情反弹，新增病例激增，防控升级",
         "sentiment": {"tone": "neg", "score": -1.0}, "stocks": [], "pub_ts": _B},
    ]
    ctx = build_context(items=fake_items)
    fsec = [f["sector"] for f in ctx["flagged"]]
    ok("build_context：医药被 flagged", "医药" in fsec)
    reasons = next((f["reasons"] for f in ctx["flagged"] if f["sector"] == "医药"), [])
    ok("build_context：触发原因含修正率异常(3/3)", any("修正率" in r for r in reasons))
    ok("build_context：触发原因含词表失衡", any("失衡" in r for r in reasons))
    ok("build_context：样本为全文(供逐字证据)",
       bool(ctx["samples"]) and all(len(s["text"]) >= 8 for s in ctx["samples"]))
    ok("build_context：无快讯 → ok=False", build_context(items=[]).get("ok") is False)

    # 3) 护栏正例：「医保」反极性覆盖（#189 实证案例）
    samples = [
        {"sector": "医药", "text": "国家医保局强化医药价格管理，控费力度加大",
         "tone": "pos", "origin": "neutral", "corrected": True, "pub_ts": _B},
        {"sector": "医药", "text": "第十二批集采扩围启动，仿制药面临降价",
         "tone": "neg", "origin": "neg", "corrected": False, "pub_ts": _B},
    ]
    ok("前置：当前「医保」把控费样本读成利好(基础层bug)",
       ss.detect_catalysts(samples[0]["text"], ["医药"])["sec_net"] > 0)
    good = [{"action": "add", "sector": "医药", "polarity": "bear", "word": "医保",
             "evidence": "国家医保局强化医药价格管理，控费力度加大",
             "reason": "医保控费=医药利空(#189实证)", "confidence": 0.9}]
    r = validate_patch(good, samples)["items"][0]
    ok("护栏：医保反极性覆盖 add 五项全过", r["ok"] and all(r["checks"].values()))

    # 4) 护栏负例（每条都对应一种真实会发生的 LLM 毛病）
    ok("护栏：拒绝编造证据(样本外原句)",
       not validate_patch([dict(good[0], evidence="国家医保局召开新闻发布会，发布控费文件")],
                          samples)["items"][0]["checks"]["evidence"])
    ok("护栏：拒绝跨板块张冠李戴",
       not validate_patch([dict(good[0], evidence="多国联合军演，实战检验新型装备")],
                          samples)["items"][0]["checks"]["evidence"])
    _r = validate_patch([{"action": "add", "sector": "医药", "polarity": "bull",
                          "word": "集采扩围",
                          "evidence": "第十二批集采扩围启动，仿制药面临降价",
                          "reason": "声称利好", "confidence": 0.9}], samples)["items"][0]
    ok("护栏：拒绝方向矛盾(控费样本声称利好)", not _r["ok"] and not _r["checks"]["ab"])
    _r = validate_patch([{"action": "remove", "sector": "医药", "polarity": "bear",
                          "word": "集采", "evidence": samples[0]["text"],
                          "reason": "删基础层词", "confidence": 0.9}], samples)["items"][0]
    ok("护栏：拒绝删除基础层词", not _r["ok"] and "基础层" in _r["msg"])
    ok("护栏：拒绝坏 action", not validate_patch(
        [dict(good[0], action="delete")], samples)["items"][0]["checks"]["schema"])
    ok("护栏：拒绝超长词", not validate_patch(
        [dict(good[0], word="一二三四五六七八九十十一十二十三")],
        samples)["items"][0]["checks"]["schema"])
    ok("护栏：拒绝低置信度", not validate_patch(
        [dict(good[0], confidence=0.2)], samples)["items"][0]["checks"]["schema"])
    ok("护栏：拒绝空补丁", validate_patch([], samples)["n_total"] == 0)

    # 5) remove roundtrip + apply_patch 落盘（外层 selfcheck 统一还原扩展层）
    ss.add_word("医药", "bear", "自检临时词")
    _rm_bad = {"action": "remove", "sector": "医药", "polarity": "bear",
               "word": "自检临时词", "evidence": samples[0]["text"],
               "reason": "删个词", "confidence": 0.8}
    _r = validate_patch([_rm_bad], samples)["items"][0]
    ok("护栏：拒绝删除对证据不生效的词", not _r["ok"] and not _r["checks"]["ab"])
    _samples_rm = [{"sector": "医药", "text": "某药企自检临时词发酵，市场担忧加剧",
                    "tone": "neg", "origin": "neutral", "corrected": True,
                    "pub_ts": _B}]
    _rm_ok = dict(_rm_bad, evidence="某药企自检临时词发酵，市场担忧加剧")
    _r = validate_patch([_rm_ok], _samples_rm)["items"][0]
    ok("护栏：remove 扩展层词(删后方向变)通过", _r["ok"])
    out = apply_patch([_rm_ok], _samples_rm, via="selfcheck")
    ok("apply_patch：删除生效", out["ok"] and "自检临时词" not in
       (ss.list_extra().get("医药", {}).get("bear") or []))
    out2 = apply_patch(good, samples, via="selfcheck")
    ok("apply_patch：医保反极性覆盖落地",
       out2["ok"] and "医保" in (ss.list_extra().get("医药", {}).get("bear") or []))
    _c = ss.detect_catalysts(samples[0]["text"], ["医药"])
    ok("apply 后：控费样本读作利空", _c["sec_net"] < 0 and "医保" in _c["sec_bear"])
    ok("审计日志可读(尾部为本次 apply)",
       bool(list_log(5)) and list_log(5)[0].get("n") == 1)
    ok("apply_patch：全被护栏拒的批次不落盘",
       not apply_patch([dict(good[0], action="delete")], samples)["ok"])

    # 6) _parse_patch：单批上限 + 容错
    _many = {"patch": [{"action": "add", "sector": "医药", "polarity": "bear",
                        "word": f"词{i}", "evidence": "x", "reason": "r",
                        "confidence": 0.9} for i in range(12)]}
    _rep, _patch = _parse_patch("报告正文\n```json\n" + json.dumps(_many) + "\n```")
    ok("解析：补丁截到单批上限10", len(_patch) == MAX_PATCH and "报告正文" in _rep)
    _rep2, _patch2 = _parse_patch("无机读块的纯报告")
    ok("解析：无机读块→空补丁不崩", _patch2 == [] and _rep2 == "无机读块的纯报告")
    _rep3, _patch3 = _parse_patch("```json\n{bad json}\n```")
    ok("解析：坏 JSON→空补丁不崩", _patch3 == [] and "```" not in _rep3)

    # 7) P2 每日自动提议：run_daily 落盘 + load_pending roundtrip
    #    （假 propose 注入，不调 LLM；扩展层由外层 selfcheck 统一还原。
    #    注意 run_daily 吃的是快讯项形状(content/stocks/pub_ts)，不是样本池形状）
    _orig_propose = propose
    _canned = {"ok": True, "report": "自检假报告", "patch": [dict(good[0])],
               "flagged": ["医药"]}
    globals()["propose"] = lambda ctx, override=None: _canned
    try:
        _news = [{"content": s["text"], "sentiment": {"tone": s["tone"], "score": 0.0},
                  "stocks": [], "pub_ts": _B} for s in samples]
        d = run_daily(items=_news)
        ok("run_daily：全链路落盘(报告+补丁+样本+计数)",
           d["ok"] and not d["empty"] and d["auto"]
           and d["report"] == "自检假报告"
           and d["patch"] and d["patch"][0]["ok"]
           and d["ctx"]["samples"] and d["n_pass"] == 1)
        ok("load_pending：roundtrip 读回一致",
           (load_pending() or {}).get("report") == "自检假报告"
           and (load_pending() or {}).get("patch", [{}])[0].get("ok"))
        e = run_daily(items=[])
        ok("run_daily：无快讯→空标记落盘", e["empty"] and e["ctx"]["flagged"] == [])
        ok("run_daily：空标记也写明原因", bool(e.get("msg")))
    finally:
        globals()["propose"] = _orig_propose
        try:
            os.remove(PENDING_PATH)
        except OSError:
            pass
    ok("清理：待审文件已删", load_pending() is None)


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
