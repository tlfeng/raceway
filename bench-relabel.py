#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-relabel.py —— 给既有载荷补系列键（series_key）与 JDK 身份

  python3 bench-relabel.py --dir exports/<tag> [--out exports/<tag>-relabel]

做什么：把 benchmark-result-metric.jsonl / benchmark-result-race.jsonl 里**缺** series_key 的文档补一个
（引擎@版本@build@JDK，见 identity.py），其余字段原样保留。

为什么需要它：完整载荷（402 场）依赖本机的 rally race store 副本才能导全；
要补键又不能重导，就给既有载荷打补丁 —— 键的口径与 bench-export.py 完全同一份
（identity.py）。

幂等：**缺才补、有不动**。已带 series_key 的文档原样保留 —— 那个键是
bench-export 按源头真值（sidecar 实测 / 包名 / user-tags）算出来的；本工具
拿到的指标行不带身份字段，重算只会得到 "?@?@—@?"，把好键覆盖掉，入库后
看板全部分线塌成一桶（2026-10-22 复现实锤）。所以只给缺键的文档补。
只改本地文件，不连 ES —— 入库用 bench-apply.py 另跑。
"""
import argparse
import importlib
import json
import os
import shutil
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="载荷目录（含 benchmark-result-metric.jsonl / benchmark-result-race.jsonl）")
    ap.add_argument("--out", default=None, help="输出目录；缺省 = <dir>-relabel")
    ap.add_argument("--only", default="benchmark-result-metric,benchmark-result-race")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ID = importlib.import_module('identity')

    src = os.path.abspath(args.dir)
    dst = os.path.abspath(args.out) if args.out else src + "-relabel"
    want = args.only.split(",")

    if os.path.abspath(dst) == src:
        print("输出目录不能与输入相同（就地改会毁掉原始载荷）")
        return 1
    os.makedirs(dst, exist_ok=True)

    rewritten = {idx + ".jsonl" for idx in want}
    for name in os.listdir(src):
        p = os.path.join(src, name)
        if os.path.isfile(p) and name not in rewritten:
            shutil.copy2(p, os.path.join(dst, name))

    # templates.json 要跟上：series_key 是分线键，得在模板里显式声明成 keyword，
    # 否则目标索引靠 dynamic_templates 兜底（能跑，但映射口径不显式）。
    tpl_path = os.path.join(dst, "templates.json")
    if os.path.isfile(tpl_path):
        with open(tpl_path, encoding="utf-8") as f:
            tpl = json.load(f)
        touched = []
        for name, t in tpl.items():
            props = ((t.get("template") or {}).get("mappings") or {}).get("properties")
            if not isinstance(props, dict):
                continue
            for fld, typ in (("series_key", "keyword"), ("jdk_vendor", "keyword"),
                             ("jdk_version", "keyword")):
                if fld not in props:
                    props[fld] = {"type": typ}
                    touched.append("%s.%s" % (name, fld))
        if touched:
            with open(tpl_path, "w", encoding="utf-8") as f:
                json.dump(tpl, f, ensure_ascii=False, indent=1)
        print("templates.json: 声明补齐 → %s" % (", ".join(touched) or "无需改动"))

    for idx in want:
        path = os.path.join(src, idx + ".jsonl")
        if not os.path.isfile(path):
            print("跳过（无此文件）: %s" % path)
            continue
        n = changed = 0
        out_path = os.path.join(dst, idx + ".jsonl")
        with open(path, encoding="utf-8") as f, open(out_path, "w", encoding="utf-8") as g:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                doc = json.loads(line)
                if not doc.get("series_key"):
                    doc["series_key"] = ID.series_key_from_doc(doc)
                    changed += 1
                g.write(json.dumps(doc, ensure_ascii=False) + "\n")
                n += 1
        print("%s: %d 条，系列键补缺 %d 条 → %s" % (idx, n, changed, out_path))

    # 键分布：给人看一眼，确认没有 ? / 空段（--only 不含 benchmark-result-metric 时跳过）
    path = os.path.join(dst, "benchmark-result-metric.jsonl")
    if os.path.isfile(path):
        dist = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                k = d.get("series_key") or "(缺)"
                dist[k] = dist.get(k, 0) + 1
        print("系列键分布:")
        for k, v in sorted(dist.items(), key=lambda x: -x[1]):
            print("  %-32s %d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
