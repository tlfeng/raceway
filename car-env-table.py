#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""car-env-table.py —— 完整有效测量的性能与环境联合表。

python3 car-env-table.py <campaign 目录...> [--detail]
按实测引擎身份、赛道变体、任务隔离，不按可复用的车代号混合 build。
"""
import argparse
import statistics

from result_selection import finite, monitor_summary, select_measurements


def summary_of(camp_dir, fid, phases=None, expected_nodes=None):
    return monitor_summary(camp_dir, fid, phases, expected_nodes)


def collect(camps):
    rows = []
    for camp in dict.fromkeys(camps):
        for measurement in select_measurements(camp):
            phase = "wq" if "wq" in measurement["races"] else "w"
            d = measurement["races"][phase]
            side = measurement["sides"][phase]
            docs = (side.get("corpus") or {}).get("docs")
            for op in (d.get("results") or {}).get("op_metrics") or []:
                tp = op.get("throughput") or {}
                if tp.get("unit") != "docs/s":
                    continue
                duration = op.get("duration")
                dur = duration / 1000 if finite(duration) and duration > 0 else None
                svc = op.get("service_time") or {}
                scale = {"ms": 1, "s": 1000}.get(svc.get("unit", "ms"))
                def percentile(key):
                    return svc[key] * scale if scale is not None and finite(svc.get(key)) else None
                rows.append({"fid": measurement["fid"], "car": measurement["series_key"],
                             "track": measurement["track"], "task": op.get("task"),
                             "round": measurement["round"], "camp": measurement["camp"],
                             "a": tp.get("median"),
                             "b": docs / dur if finite(docs) and dur else None,
                             "dur": dur, "env": measurement["env"],
                             "signature": measurement["signature"],
                             "p99": percentile("99_0"), "p100": percentile("100_0")})
    return rows


def shown(value, digits=1):
    return ("%.*f" % (digits, value)) if finite(value) else "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaigns", nargs="+")
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()
    rows = collect(args.campaigns)
    if not rows:
        print("没有完整有效的写入测量")
        return 1
    if args.detail:
        print("| 场 | 身份 | 赛道/任务 | 圈 | 口径A docs/s | 口径B docs/s | 任务 s | steal峰 % | foreign峰 % | 判定 |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            e = r["env"]
            print("| %s/%s | %s | %s/%s | %s | %s | %s | %s | %s | %s | %s |" % (
                r["camp"], r["fid"], r["car"], r["track"], r["task"], r["round"],
                shown(r["a"]), shown(r["b"]), shown(r["dur"]), shown(e["steal_max"]),
                shown(e["foreign_max"]), e["verdict"]))
    groups = {}
    for row in rows:
        groups.setdefault((row["car"], row["track"], row["task"]), []).append(row)
    print("\n| 身份 | 赛道 | 任务 | n | 口径B中位 docs/s | 极差 % | bulk p99 ms | p100 ms | steal峰 % | foreign峰 % |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for (car, track, task), group in sorted(groups.items()):
        if any(r["signature"] != group[0]["signature"] for r in group[1:]):
            print("%s/%s 实测条件不一致，拒绝合池" % (car, track))
            return 1
        def median(key):
            values = [r[key] for r in group if finite(r[key])]
            return statistics.median(values) if values else None
        rates = [r["b"] for r in group if finite(r["b"])]
        spread = (max(rates) / min(rates) - 1) * 100 if rates and min(rates) > 0 else None
        print("| %s | %s | %s | %d | %s | %s | %s | %s | %s | %s |" % (
            car, track, task, len(group), shown(median("b")), shown(spread),
            shown(median("p99")), shown(median("p100")),
            shown(max(r["env"]["steal_max"] for r in group)),
            shown(max(r["env"]["foreign_max"] for r in group))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
