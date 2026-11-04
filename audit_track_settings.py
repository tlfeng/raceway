#!/usr/bin/env python3
"""audit_track_settings.py —— 审计赛道 index.json 里的 settings 结构

`index.codec` / `index.source_reuse` 必须是 settings 的**顶层键**（与 "index" 对象平级）：
塞进 "index" 对象里会展开成 settings.index.index.codec → 引擎拒收 → 索引建不起来。

用法:
  python3 audit_track_settings.py [--check] <track_dir>...   # 只报告

只审计不修复：条件块的位置要人工挪（连带逗号与缩进），自动改写容易改出新坑。
判据被 tests/test_track_settings.py 当回归闸门用。
"""
import json
import os
import re
import sys

CODEC_KEYS = ("index.codec", "index.source_reuse")


def audit(path):
    """→ (状态, 说明)。状态 ∈ ok / nested / jinja-nested / unreadable"""
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read()
    except OSError:
        return "unreadable", "读不了"
    try:
        d = json.loads(s)
    except ValueError:
        # Jinja 模板：按文本找 "index" 对象的范围
        m = re.search(r'"settings"\s*:\s*\{', s)
        if not m:
            return "ok", "不是 settings 模板"
        i = s.find('"index"', m.end())
        if i < 0:
            return "ok", "settings 里没有 index 对象"
        j = s.find("{", i)
        depth = 0
        k = j
        while k < len(s):
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        inner = s[j:k + 1]
        bad = [c for c in CODEC_KEYS if c in inner]
        return ("jinja-nested", "index 对象里有 %s → 条件块位置不对，手工改这个 index.json"
                "（把条件块挪到 settings 顶层，与 \"index\" 对象平级）" % bad) if bad \
            else ("ok", "结构正确")
    st = d.get("settings")
    if not isinstance(st, dict):
        return "ok", "settings 不是对象"
    inner = st.get("index")
    if not isinstance(inner, dict):
        return "ok", "settings 里没有 index 对象"
    bad = [c for c in CODEC_KEYS if c in inner]
    return ("nested", "index 对象里有 %s → 把这两个键挪到 settings 顶层（与 \"index\" 平级）" % bad) \
        if bad else ("ok", "结构正确")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--help", "-h"):
        print(__doc__)
        return 0
    if args and args[0] == "--check":
        args.pop(0)
    # 未知参数必须报错：把 --fix 当成目录名去 walk，会得到一句"全部通过"的假 OK
    for a in args:
        if a.startswith("-"):
            print("未知参数: %s（只接受 --check；本工具只审计不修复）" % a, file=sys.stderr)
            return 2
    dirs = args or ["."]
    n_bad = 0
    for root in dirs:
        for dirpath, _dn, fn in os.walk(root):
            if "index.json" not in fn:
                continue
            p = os.path.join(dirpath, "index.json")
            status, msg = audit(p)
            if status in ("nested", "jinja-nested"):
                n_bad += 1
                print("  [错] %-58s %s" % (p, msg))
            elif status == "unreadable":
                print("  [跳过] %s" % p)
    print("%s：%d 个赛道 index.json 的 settings 结构有问题"
          % ("仍需处理" if n_bad else "全部通过", n_bad))
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
