"""校验「多股对比」快捷组合里的股票代码是否都真实有效。

组合定义写在 web/index.html 的 CMP_PRESETS 里。手改代码很容易写错一位数字
（写错了前端只会显示 —，不报错），所以改动后请跑一次本脚本。

跑法（需先启动服务）：
    cd server && python3 -m uvicorn app:app --port 8899 &
    python3 tests/check_cmp_presets.py

退出码：全部有效 0，有无效代码 1。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("TICK_BASE", "http://127.0.0.1:8899")
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(os.path.dirname(HERE), "web", "index.html")


def parse_presets(html):
    """从 CMP_PRESETS 数组里抠出 (分组, 组合名, 代码串)。

    不跑 JS，只做结构化匹配：形如 ["组合名", "600519,000858"] 的条目。
    """
    m = re.search(r"var CMP_PRESETS = \[(.*?)\n\];", html, re.S)
    if not m:
        raise SystemExit("未在 index.html 中找到 CMP_PRESETS 定义")
    body = m.group(1)

    out = []
    group = ""
    # 逐行扫：先记分组名 ["金融", [ ...，再收组合条目 ["组合名", "代码串"]
    for line in body.split("\n"):
        gm = re.search(r'\[\s*"([^"]+)"\s*,\s*\[\s*$', line)
        if gm:
            group = gm.group(1)
            continue
        pm = re.search(r'\[\s*"([^"]+)"\s*,\s*"([\d,]+)"\s*\]', line)
        if pm:
            out.append((group, pm.group(1), pm.group(2)))
    return out


def quote_names(codes):
    """批量取名称，返回 {code: name}。

    /api/quote 单次上限 100 只（见 app.py 的校验），这里分片请求。
    """
    uniq = sorted(set(codes))
    out = {}
    for i in range(0, len(uniq), 80):
        chunk = uniq[i:i + 80]
        url = BASE + "/api/quote?codes=" + urllib.parse.quote(",".join(chunk))
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read().decode())
            for it in (data.get("items") or []):
                out[it["code"]] = it.get("name", "")
        except Exception as e:
            print(f"  (分片请求失败 {chunk[0]}..{chunk[-1]}: {e})")
    return out


def main():
    html = open(INDEX, encoding="utf-8").read()
    presets = parse_presets(html)
    if not presets:
        raise SystemExit("解析到 0 个组合，检查 CMP_PRESETS 格式")

    all_codes = []
    for _g, _n, cs in presets:
        all_codes.extend(cs.split(","))

    names = quote_names(all_codes)

    bad = []
    dup_inside = []          # 组合内部重复的代码
    dup_group = {}
    print(f"{'分组':<12}{'组合':<14}{'只数':<5}股票")
    for g, n, cs in presets:
        codes = cs.split(",")
        dup_group.setdefault(g, []).append(n)
        # 同一组合里出现两次同一个代码 —— 几乎肯定是手误
        if len(codes) != len(set(codes)):
            seen, rep = set(), []
            for c in codes:
                if c in seen and c not in rep:
                    rep.append(c)
                seen.add(c)
            dup_inside.append((g, n, rep))
        got = []
        for c in codes:
            code = c if c[:2] in ("sh", "sz", "bj") else (
                "sh" + c if c[0] in "659" else "sz" + c)
            nm = names.get(code)
            if nm:
                got.append(f"{nm}({c})")
            else:
                got.append(f"❌{c}")
                bad.append((g, n, c))
        flag = "" if len(got) == len(codes) else "  <-- 有问题"
        print(f"{g:<12}{n:<14}{len(codes):<5}{' / '.join(got)}{flag}")

    total = sum(len(cs.split(",")) for _g, _n, cs in presets)
    print(f"\n共 {len(presets)} 组 / {total} 个引用 / {len(set(all_codes))} 个唯一代码")

    # 分组内重名检查
    for g, names_ in dup_group.items():
        if len(names_) != len(set(names_)):
            print(f"⚠️ 分组「{g}」内有重名组合")

    if dup_inside:
        print(f"\n❌ {len(dup_inside)} 个组合内部有重复代码：")
        for g, n, rep in dup_inside:
            print(f"   {g} / {n} / 重复 {','.join(rep)}")

    if bad:
        print(f"\n❌ {len(bad)} 个代码无效：")
        for g, n, c in bad:
            print(f"   {g} / {n} / {c}")

    if bad or dup_inside:
        return 1

    print("\n✅ 全部代码有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
