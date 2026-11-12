#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 campaign 原始 JSON 生成 Rally race-store 软链；先完整预检再改输出。"""
import argparse
import glob
import json
import math
import os
import re
import shutil
import sys
import tempfile


def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("race 包含 NaN/Infinity")
    if isinstance(value, dict):
        for item in value.values():
            finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            finite_tree(item)


def collect_sources(campaigns):
    if not os.path.isdir(campaigns):
        raise ValueError("campaign 目录不存在: %s" % campaigns)
    sources = {}
    for path in sorted(glob.glob(os.path.join(campaigns, "*", "results", "*-race.json"))):
        with open(path, encoding="utf-8") as f:
            race = json.load(f)
        finite_tree(race)
        rid = race.get("race-id") if isinstance(race, dict) else None
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", rid) or rid in (".", ".."):
            raise ValueError("%s 的 race-id 不是安全目录名" % path)
        if rid in sources:
            raise ValueError("重复 race-id %s: %s / %s" % (rid, sources[rid], path))
        sources[rid] = os.path.abspath(path)
    if not sources:
        raise ValueError("没有可构建的 race.json，拒绝清空/重建")
    return sources


def build_store(campaigns, output, rebuild=False):
    sources = collect_sources(campaigns)
    source_root, output = os.path.realpath(campaigns), os.path.abspath(output)
    # 输出不能包含原始层，也不能写进原始层；--rebuild 只授权删除独立生成目录。
    if os.path.commonpath((os.path.normcase(source_root), os.path.normcase(output))) in (
            os.path.normcase(source_root), os.path.normcase(output)):
        raise ValueError("输出目录与 campaign 原始层不能相互包含")
    if os.path.islink(output):
        raise ValueError("输出目录不能是软链接")
    if os.path.lexists(output) and not os.path.isdir(output):
        raise ValueError("输出路径不是目录")
    # 不删除用户文件；输出内只允许已生成的 <id>/race.json 链接布局。
    if os.path.isdir(output):
        for name in os.listdir(output):
            directory = os.path.join(output, name)
            if (os.path.islink(directory) or not os.path.isdir(directory)
                    or os.listdir(directory) != ["race.json"]
                    or not os.path.islink(os.path.join(directory, "race.json"))):
                raise ValueError("输出含非生成文件，拒绝覆盖/删除: %s" % directory)
    parent = os.path.dirname(output)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".race-store-", dir=parent)
    backup = None
    try:
        if not rebuild and os.path.isdir(output):
            for name in os.listdir(output):
                if name in sources:
                    continue
                old = os.path.join(output, name, "race.json")
                os.makedirs(os.path.join(staging, name))
                os.symlink(os.readlink(old), os.path.join(staging, name, "race.json"))
        for rid, target in sources.items():
            directory = os.path.join(staging, rid)
            os.makedirs(directory)
            os.symlink(target, os.path.join(directory, "race.json"))
        if os.path.isdir(output):
            backup = tempfile.mkdtemp(prefix=".race-store-old-", dir=parent)
            os.rmdir(backup)
            os.replace(output, backup)
        try:
            os.replace(staging, output)
            staging = None
        except OSError:
            if backup:
                os.replace(backup, output)
                backup = None
            raise
        if backup:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging)
    return len(sources)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaigns", default="campaigns-local")
    ap.add_argument("--out", default="race-store")
    ap.add_argument("--rebuild", action="store_true", help="以完整预检后的原始层替换生成目录")
    args = ap.parse_args()
    try:
        count = build_store(args.campaigns, args.out, args.rebuild)
    except (OSError, ValueError) as exc:
        print("race-store 构建失败: %s" % exc, file=sys.stderr)
        return 2
    print("race-store → %s: %d 场" % (args.out, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
