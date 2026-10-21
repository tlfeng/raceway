#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-relabel.py —— 给既有载荷补看板图例标签（car_label）

  python3 bench-relabel.py --dir exports/<tag> [--out exports/<tag>-relabel]

做什么：把 bench-metric.jsonl / bench-run.jsonl 每条文档补一个 car_label
（引擎@版本@build@JDK，见 car_label.py），其余字段原样保留。

为什么需要它：完整载荷（402 场）是在服务器侧导出的，本机的 campaigns-local
只是部分镜像（36/47 个 campaign），本机重跑 bench-export.py 只能导出一半场次。
要补标签又不能重导，就给既有载荷打补丁 —— 标签口径与 bench-export.py 完全同一份
（car_label.py），两条路径已在 55157 条指标上逐条比对为 0 差异。

幂等：已带 car_label 的文档会按同一口径重算（值相同则原样保留）。
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
    ap.add_argument("--dir", required=True, help="载荷目录（含 bench-metric.jsonl / bench-run.jsonl）")
    ap.add_argument("--out", default=None, help="输出目录；缺省 = <dir>-relabel")
    ap.add_argument("--only", default="bench-metric,bench-run")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    AL = importlib.import_module('car_label')

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

    # templates.json 要跟上：car_label 是新列，得在模板里显式声明成 keyword，
    # 否则目标索引靠 dynamic_templates 兜底（能跑，但映射口径不显式）。
    tpl_path = os.path.join(dst, "templates.json")
    if os.path.isfile(tpl_path):
        with open(tpl_path, encoding="utf-8") as f:
            tpl = json.load(f)
        touched = []
        for name, t in tpl.items():
            props = ((t.get("template") or {}).get("mappings") or {}).get("properties")
            if isinstance(props, dict) and "car_label" not in props:
                props["car_label"] = {"type": "keyword"}
                touched.append(name)
        if touched:
            with open(tpl_path, "w", encoding="utf-8") as f:
                json.dump(tpl, f, ensure_ascii=False, indent=1)
        print("templates.json: car_label 声明补齐 → %s" % (", ".join(touched) or "无需改动"))

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
                label = AL.car_label_from_doc(doc)
                if doc.get("car_label") != label:
                    doc["car_label"] = label
                    changed += 1
                g.write(json.dumps(doc, ensure_ascii=False) + "\n")
                n += 1
        print("%s: %d 条，标签更新 %d 条 → %s" % (idx, n, changed, out_path))

    # 标签分布：给人看一眼，确认没有 ? / 空段
    path = os.path.join(dst, "bench-metric.jsonl")
    dist = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            k = d.get("car_label") or "(缺)"
            dist[k] = dist.get(k, 0) + 1
    print("标签分布:")
    for k, v in sorted(dist.items(), key=lambda x: -x[1]):
        print("  %-32s %d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
