#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build-race-store.py —— 从 campaigns-local 生成 rally race store 布局（软链农场）

bench-export.py 的 --races 期望的是 rally 原生布局 `<dir>/<race-id>/race.json`，
而我们的原始层在 campaigns-local/<campaign>/results/<fid>-race.json（文件名是 fid，
不是 race-id）。本脚本按 race.json 里的 race-id 建软链，把两种布局对上：

    <out>/<race-id>/race.json  ->  campaigns-local/<campaign>/results/<fid>-race.json

只建软链不复制数据（231 场约 3 MB，复制会双份且易与源脱节）。
幂等：已存在的链接会按需重建；--rebuild 先清空再生成。

  python3 build-race-store.py --campaigns campaigns-local --out race-store
"""
import argparse
import glob
import json
import os
import shutil
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaigns", default="campaigns-local",
                    help="campaign 目录（含 <campaign>/results/*-race.json）")
    ap.add_argument("--out", default="race-store",
                    help="输出目录：<out>/<race-id>/race.json 软链")
    ap.add_argument("--rebuild", action="store_true", help="先删除输出目录再生成")
    args = ap.parse_args()

    if args.rebuild and os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)

    made, skipped, broken = 0, 0, 0
    for camp in sorted(glob.glob(os.path.join(args.campaigns, "*"))):
        if not os.path.isdir(camp):
            continue
        for path in sorted(glob.glob(os.path.join(camp, "results", "*-race.json"))):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    race = json.load(f)
            except (ValueError, OSError):
                broken += 1
                continue
            race_id = race.get("race-id")
            if not race_id:
                broken += 1
                continue
            d = os.path.join(args.out, race_id)
            link = os.path.join(d, "race.json")
            os.makedirs(d, exist_ok=True)
            target = os.path.abspath(path)
            if os.path.islink(link) and os.readlink(link) == target:
                skipped += 1
                continue
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(target, link)
            made += 1

    print("race-store → %s: 新建 %d，复用 %d，跳过/损坏 %d"
          % (args.out, made, skipped, broken))
    return 0


if __name__ == "__main__":
    sys.exit(main())
