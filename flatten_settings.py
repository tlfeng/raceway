#!/usr/bin/env python3
"""flatten_settings.py —— 审计/修复赛道 index.json 里的 settings 结构

`index.codec` / `index.source_reuse` 必须是 **settings 的顶层键**（与 "index" 对象平级）。
塞进 "index" 对象里会展开成 settings.index.index.codec → EasySearch 拒收
（unknown setting [index.index.codec]）→ **索引根本建不起来**。

用法:
  python3 flatten_settings.py --check  <track_dir>...   # 只报告，不改
  python3 flatten_settings.py --fix    <track_dir>...   # 修（纯 JSON 的直接改写；
                                                      #  Jinja 模板只报告，需人工处理）
"""
import json
import os
import re
import sys

CODEC_KEYS = ("index.codec", "index.source_reuse")


def audit(path):
    """→ (状态, 说明)。状态 ∈ ok / nested / jinja-nested / unreadable"""
    try:
        s = open(path, encoding="utf-8").read()
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
        return ("jinja-nested", "index 对象里有 %s（需人工把条件块挪到 settings 顶层）" % bad) if bad \
            else ("ok", "结构正确")
    st = d.get("settings")
    if not isinstance(st, dict):
        return "ok", "settings 不是对象"
    inner = st.get("index")
    if not isinstance(inner, dict):
        return "ok", "settings 里没有 index 对象"
    bad = [c for c in CODEC_KEYS if c in inner]
    return ("nested", "index 对象里有 %s" % bad) if bad else ("ok", "结构正确")


def fix_json(path):
    d = json.load(open(path, encoding="utf-8"))
    st = d.get("settings") or {}
    inner = st.get("index") or {}
    moved = []
    for k in CODEC_KEYS:
        if k in inner:
            st[k] = inner.pop(k)
            moved.append(k)
    if moved:
        d["settings"] = st
        open(path, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2))
    return moved


def main():
    args = sys.argv[1:]
    mode = "check"
    if args and args[0] in ("--check", "--fix"):
        mode = args.pop(0)[2:]
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
                if mode == "fix" and status == "nested":
                    moved = fix_json(p)
                    print("  [已修] %-58s 移动 %s" % (p, moved))
                    continue
                print("  [%s] %-58s %s" % ("修" if status == "nested" else "人工",
                                          p, msg))
            elif status == "unreadable":
                print("  [跳过] %s" % p)
    print("\n%s：%d 个赛道 index.json 的 settings 结构有问题"
          % ("仍需处理" if n_bad else "全部通过", n_bad))
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
