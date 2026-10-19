#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-export.py —— 把 rally race store + campaign 产物导出成 ES 入库载荷（离线，不需要 ES 集群）

用法（在压测机上）：
  python3 bench-export.py --out exports/<tag> \
      [--races ~/.rally/benchmarks/races] [--campaigns campaigns]

产出（对齐《20261016-benchmark结果入库ES方案》§3）：
  templates.json      bench-env / bench-run / bench-metric 三张索引的 index template（strict + snake_case）
  bench-env.jsonl     一环境一行（host/vm 台账，来自 sidecar）
  bench-run.jsonl     一场 race 一行（含实测 JDK/heap/核数、valid、口径B 速率、fingerprint）
  bench-metric.jsonl  race × task × metric × stat 一行（单位归一 + family + direction）
  manifest.json       计数与来源，供核对
  apply.sh            之后有 store 集群时一键 bulk 导入（curl，可重复执行=幂等）

时间一律 ms→s、字节→GB、百分比→0~1、吞吐保留 docs/s 与 ops/s。
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import sys

DOCS = {"track": 11396503, "hnsw2m-2m-s3": 2000000, "nested-s3": 11203029,
        "hv1024-s3": 1000000, "dsj512-s3": 1000000, "dsj768-s3": 1000000,
        "dsj1024-s3": 1000000, "dsj1536-s3": 1000000, "dsj2048-s3": 1000000,
        "dsg960-s3": 1000000, "dsg1920-s3": 1000000,
        # geonames 存储压缩对比三变体（prepare-codec-track.sh 生成，语料与基座同一份）
        "geo-codec-def": 11396503, "geo-codec-zstd-sr": 11396503, "geo-codec-bc": 11396503}
TRACK_NAME = {"track": "geo", "hnsw2m-2m-s3": "hnsw", "nested-s3": "nested", "hv1024-s3": "hv1024",
              "geo-codec-def": "geo", "geo-codec-zstd-sr": "geo", "geo-codec-bc": "geo"}
# 变体名 → 期望 codec（实测值优先取 sidecar 的 index_settings_measured，这里只兜底）
TRACK_CODEC = {"geo-codec-def": "default", "geo-codec-zstd-sr": "ZSTD+source_reuse",
               "geo-codec-bc": "best_compression"}
PHASE = {"write-only": "w", "query-only": "q", "index-only": "w",
         "nested-search-challenge": "wq", "default": "wq"}
LOWER_BETTER = ("latency", "service_time", "processing_time", "error_rate", "duration",
                "indexing_time", "merge_time", "refresh_time", "flush_time", "gc",
                "disk", "store", "system", "memory")
STAT_MAP = {"50_0": "p50", "90_0": "p90", "99_0": "p99", "99_9": "p999", "100_0": "p100",
            "min": "min", "max": "max", "mean": "mean", "median": "median"}
RESULT_METRICS = {
    "total_time": ("indexing_time", "ms"), "merge_time": ("merge_time", "ms"),
    "merge_count": ("merge", "count"), "refresh_time": ("refresh_time", "ms"),
    "refresh_count": ("refresh", "count"), "flush_time": ("flush_time", "ms"),
    "flush_count": ("flush", "count"), "merge_throttle_time": ("merge_throttle_time", "ms"),
    "indexing_throttle_time": ("indexing_throttle_time", "ms"),
    "young_gc_time": ("gc", "ms"), "young_gc_count": ("gc", "count"),
    "old_gc_time": ("gc", "ms"), "old_gc_count": ("gc", "count"),
    "store_size": ("disk", "bytes"), "translog_size": ("disk", "bytes"),
    "dataset_size": ("disk", "bytes"), "segment_count": ("disk", "count"),
    "memory_segments": ("memory", "bytes"), "memory_doc_values": ("memory", "bytes"),
    "memory_terms": ("memory", "bytes"), "memory_norms": ("memory", "bytes"),
    "memory_points": ("memory", "bytes"), "memory_stored_fields": ("memory", "bytes"),
}


def norm_value(v, raw_unit):
    """→ (归一值, 归一单位)"""
    if v is None:
        return None, None
    if raw_unit in ("ms",):
        return v / 1000.0, "s"
    if raw_unit == "bytes":
        return v / 1e9, "GB"
    if raw_unit == "%":
        return v / 100.0, "ratio"
    return v, raw_unit or ""


def direction_of(family):
    return "lower-better" if any(k in family for k in LOWER_BETTER) else "higher-better"


def codec_of(d, side):
    """本场**实测生效**的存储编码（codec 对比结论的分组键）：
    优先 sidecar 的 _settings 回读 → user_tags.codec → 赛道变体名期望值；都没有则 None（非 codec 场）。"""
    st = ((side.get("run_params") or {}).get("index_settings_measured") or {}) if side else {}
    for _idx, s in (st.items() if isinstance(st, dict) else []):
        c = (s or {}).get("index.codec")
        sr = str((s or {}).get("index.source_reuse", "")).lower()
        if c and sr == "true":
            return c + "+source_reuse"
        if c:
            return c
    ut = d.get("user-tags") or {}
    if ut.get("codec"):
        return ut["codec"]
    return TRACK_CODEC.get(d.get("track"))


def fid_of(d):
    ut = d.get("user-tags") or {}
    arm = ut.get("arm") or ut.get("engine") or "?"
    tr = TRACK_NAME.get(d.get("track"), d.get("track"))
    return arm, tr, ut


def load_store(races_dir):
    for p in sorted(glob.glob(os.path.join(races_dir, "*", "race.json"))):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (ValueError, OSError):
            continue
        if d.get("user-tags"):
            d["_path"] = p
            yield d


def load_campaign_meta(camp_dir):
    """RACES.md 状态、PACKAGES 指纹、sidecar、monitors 摘要"""
    m = {"status": {}, "packages": "", "sidecar": {}, "sidecar_by_race": {},
         "post_merge": {}, "env": {}, "env_id": None}
    rp = os.path.join(camp_dir, "RACES.md")
    if os.path.isfile(rp):
        for line in open(rp, encoding="utf-8", errors="replace"):
            c = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(c) >= 2 and re.match(r"^[a-z0-9]+-", c[0]):
                m["status"][c[0]] = c[1]
    pp = os.path.join(camp_dir, "PACKAGES.txt")
    if os.path.isfile(pp):
        m["packages"] = open(pp, encoding="utf-8", errors="replace").read()
    for p in glob.glob(os.path.join(camp_dir, "results", "*-sidecar.json")):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        m["sidecar"][s["run"]["fid"] + "-" + s["run"]["phase"]] = s
        # 重试场的 fid 带 -tryN，而 race.json 里没有轮试号——只有 sidecar 同时握有 race_id 与真实 fid，
        # 用它做连接键，否则重试场会丢 sidecar（实测 JDK/heap 全 null）并套错 RACES.md 状态
        rid = (s.get("run") or {}).get("race_id")
        if rid:
            m["sidecar_by_race"][rid] = s
        m["env_id"] = s.get("env", {}).get("env_id") or m["env_id"]
        m["env"] = s.get("env") or m["env"]
    # 赛末（forcemerge 之后）落盘体积：q 阶段的节点统计原始探针，三节点 store 之和
    for p in glob.glob(os.path.join(camp_dir, "logs", "fp", "*-q", "nodes-stats.txt")):
        fid = os.path.basename(os.path.dirname(p))[:-2]
        try:
            nd = json.load(open(p, encoding="utf-8")).get("nodes") or {}
        except (ValueError, OSError):
            continue
        if not nd:
            continue
        store = sum(((n.get("indices") or {}).get("store") or {}).get("size_in_bytes", 0) for n in nd.values())
        segs = sum(((n.get("indices") or {}).get("segments") or {}).get("count", 0) for n in nd.values())
        m["post_merge"][fid] = {"store_gb": round(store / 2**30, 4) if store else None,
                                "segments": segs, "nodes": len(nd)}
    for p in glob.glob(os.path.join(camp_dir, "monitors", "*-summary.txt")):
        base = os.path.basename(p)[:-len("-summary.txt")]
        txt = open(p, encoding="utf-8", errors="replace").read()
        g = lambda k: (float(re.search(k + r"=([0-9.]+)", txt).group(1))
                       if re.search(k + r"=([0-9.]+)", txt) else None)
        m.setdefault("mon", {})[base] = {
            "cpu_steal_avg": g("steal_avg"), "cpu_steal_max": g("steal_max"),
            "cpu_iowait_avg": g("iowait_avg"), "cpu_load1_max": g("load1_max"),
            "foreign_cpu_max": g("foreign_max"), "swapin": g("swapin"),
            "disk_util_avg": g("disk_util_avg"),
            "env_verdict": (re.search(r"ENV-VERDICT: (\S+)", txt).group(1)
                            if re.search(r"ENV-VERDICT: (\S+)", txt) else None)}
    return m


def emit_metrics(d, run_id, ts, common, out):
    """race.json → bench-metric 行"""
    res = d.get("results") or {}
    for om in res.get("op_metrics") or []:
        task = om.get("task") or om.get("operation") or "?"
        for key in ("throughput", "latency", "service_time", "processing_time"):
            blk = om.get(key) or {}
            unit = blk.get("unit") or ({"throughput": "ops/s", "latency": "ms",
                                        "service_time": "ms", "processing_time": "ms"}[key])
            family = key
            for k, v in blk.items():
                if k == "unit" or v is None:
                    continue
                stat = STAT_MAP.get(k, k)
                nv, nu = norm_value(v, unit)
                out.append({"_index": "bench-metric", "_id": "%s|%s|%s|%s" % (run_id, task, family, stat),
                            "@timestamp": ts, "task": task, "operation": om.get("operation"),
                            "family": family, "metric": family, "stat": stat,
                            "value": nv, "unit": nu, "value_original": v, "unit_original": unit,
                            "direction": direction_of(family), **common})
        for key in ("error_rate", "duration"):
            v = om.get(key)
            if v is None:
                continue
            fam = "error_rate" if key == "error_rate" else "duration"
            nv, nu = norm_value(v, "%" if key == "error_rate" else "ms")
            out.append({"_index": "bench-metric", "_id": "%s|%s|%s|value" % (run_id, task, fam),
                        "@timestamp": ts, "task": task, "operation": om.get("operation"),
                        "family": fam, "metric": key, "stat": "value", "value": nv, "unit": nu,
                        "value_original": v, "unit_original": "%" if key == "error_rate" else "ms",
                        "direction": direction_of(fam), **common})
    for key, (fam, unit) in RESULT_METRICS.items():
        v = res.get(key)
        if v is None:
            continue
        if isinstance(v, dict):          # *_per_shard：取 median
            v = v.get("median")
            if v is None:
                continue
            stat = "median_per_shard"
        else:
            stat = "value"
        nv, nu = norm_value(v, unit)
        out.append({"_index": "bench-metric", "_id": "%s||%s|%s" % (run_id, fam + "_" + key, stat),
                    "@timestamp": ts, "task": None, "operation": None,
                    "family": fam, "metric": key, "stat": stat, "value": nv, "unit": nu,
                    "value_original": v, "unit_original": unit,
                    "direction": direction_of(fam), **common})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--races", default="/root/.rally/benchmarks/races")
    ap.add_argument("--campaigns", default="campaigns")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    metas = {}
    for c in sorted(glob.glob(os.path.join(args.campaigns, "*"))):
        if os.path.isdir(c):
            metas[os.path.basename(c)] = load_campaign_meta(c)

    envs, runs, metrics = {}, [], []
    seen = set()
    unmatched = 0
    for d in load_store(args.races):
        ut = d.get("user-tags") or {}
        camp = ut.get("campaign") or "unknown"
        arm, tr, _ = fid_of(d)
        phase = PHASE.get(d.get("challenge"), "wq")
        rnd = ut.get("round")
        fid = "%s-%s-r%s" % (arm, tr, rnd)
        meta = metas.get(camp, {})
        race_id = d.get("race-id") or os.path.basename(os.path.dirname(d["_path"]))
        if race_id in seen:
            continue
        seen.add(race_id)
        ts = d.get("race-timestamp")
        ts = (datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
              if ts else None)
        side = meta.get("sidecar_by_race", {}).get(race_id) or meta.get("sidecar", {}).get(fid + "-" + phase, {})
        # 重试场的 fid 带 -tryN（race.json 里没有轮试号）：以 sidecar 的 fid 为准回查 RACES.md，
        # 否则 10-18 的 es819-gbc-r1-try3（VALID）会被套上 r1 首 attempt 的 QUERY-FAIL
        real_fid = (side.get("run") or {}).get("fid") if side else None
        status = (meta.get("status", {}).get(real_fid) if real_fid else None) \
            or meta.get("status", {}).get(fid) or meta.get("status", {}).get(fid + "-try2") \
            or meta.get("status", {}).get(fid + "-try3")
        if real_fid:
            fid = real_fid   # sidecar 的 fid 才是本场唯一身份（含变体名与 -tryN），比 arm-track-rN 拼出来的准
        dur = None
        for om in ((d.get("results") or {}).get("op_metrics") or []):
            if om.get("task") == "index-append" and om.get("duration"):
                dur = om["duration"] / 1000.0
        rate_b = (DOCS.get(d.get("track"), 0) / dur) if dur and DOCS.get(d.get("track")) else None
        warn = []
        eng_tag = ut.get("engine") or ""
        if camp.startswith("ds3-") and eng_tag != "es819":
            warn.append("heap 声明不实：user_tags heap:16g 但 ds3 campaign 的 easysearch 臂实测 -Xmx1g（2026-10-16 事故），绝对值不可与他批比较")
        if not side:
            warn.append("no-sidecar：JDK/heap/clients/bulk 无实测值，仅有 user_tags 声明")
        if status is None:
            warn.append("no-RACES.md：valid 未知（campaign 目录不在或该场未记账）")
        common = {"race_id": race_id, "campaign": camp, "arm": arm, "track": tr,
                  "codec": codec_of(d, side),
                  "track_raw": d.get("track"), "phase": phase, "round": rnd,
                  "challenge": d.get("challenge"), "env_id": side.get("env", {}).get("env_id"),
                  "valid": (status == "VALID") if status else None, "status": status}
        runs.append({"_index": "bench-run", "_id": race_id, "race_id": race_id, "@timestamp": ts,
                     "campaign": camp, "fid": fid, "arm": arm, "engine": ut.get("engine"),
                     "track": tr, "track_raw": d.get("track"), "challenge": d.get("challenge"),
                     "phase": phase, "round": rnd, "nnodes": ut.get("nnodes"),
                     "cores_tag": ut.get("cores"), "heap_tag": ut.get("heap"),
                     "pipeline": d.get("pipeline"), "rally_version": d.get("rally-version"),
                     "environment": d.get("environment"), "user_tags": ut,
                     "track_params": d.get("track-params"), "car": d.get("car"),
                     "cluster": d.get("cluster"),
                     "version_from_pkg": (side.get("engine") or {}).get("version_from_pkg"),
                     "pkg": (side.get("engine") or {}).get("pkg"),
                     "pkg_sha256": (side.get("engine") or {}).get("pkg_sha256"),
                     "jdk_want": (side.get("jdk") or {}).get("want"),
                     "jdk_measured": (side.get("jdk") or {}).get("measured_nodes"),
                     "jdk_match_want": (side.get("jdk") or {}).get("match_want"),
                     "heap_declared": (side.get("run_params") or {}).get("heap_declared"),
                     "heap_max_measured_mb": (side.get("run_params") or {}).get("heap_max_measured_mb"),
                     "cores_declared": (side.get("run_params") or {}).get("cores_declared"),
                     "available_processors_measured": (side.get("run_params") or {}).get("available_processors_measured"),
                     "clients": (side.get("run_params") or {}).get("clients"),
                     "bulk_size": (side.get("run_params") or {}).get("bulk_size"),
                     "shards": (side.get("run_params") or {}).get("shards"),
                     "replicas": (side.get("run_params") or {}).get("replicas"),
                     "corpus": side.get("corpus") or ({"docs": DOCS.get(d.get("track"))} if DOCS.get(d.get("track")) else None),
                     "env_id": (side.get("env") or {}).get("env_id"),
                     "codec": codec_of(d, side),
                     "store_size_gb_post_merge": (meta.get("post_merge", {}).get(real_fid or fid) or {}).get("store_gb"),
                     "segments_post_merge": (meta.get("post_merge", {}).get(real_fid or fid) or {}).get("segments"),
                     "write_task_duration_s": dur, "throughput_docs_per_s_whole": rate_b,
                     "status": status, "valid": (status == "VALID") if status else None,
                     "caliber_warnings": warn,
                     "source": [d["_path"], "campaign:" + camp] + (["sidecar:" + fid + "-" + phase] if side else []),
                     "fingerprint": hashlib.sha1(("%s|%s|%s" % (race_id, status, d["_path"])).encode()).hexdigest()})
        emit_metrics(d, race_id, ts, common, metrics)
        if side:
            e = side.get("env") or {}
            eid = e.get("env_id")
            if eid and eid not in envs:
                envs[eid] = {"_index": "bench-env", "_id": eid, "env_id": eid,
                             "host": e.get("host"), "vm_nodes": e.get("vm_nodes"),
                             "topology": {"node_count": len(e.get("vm_nodes") or []),
                                          "heap_gb": 16, "gc": "G1",
                                          "cpu_pinning": "none" if all(
                                              str(n.get("cpu_pinning_hypervisor")) in ("0", "None", "")
                                              for n in (e.get("vm_nodes") or [{}])) else "pinning"},
                             "os": "Ubuntu 24.04", "kernel": (e.get("host") or {}).get("kernel"),
                             "notes": "压测机 宿主 + 三台 KVM 虚机；rally 发压机与被测集群分离",
                             "created_at": ts, "active": True,
                             "source": side.get("source")}
        elif camp not in metas:
            unmatched += 1

    # monitors → system 指标（bench-metric 补一行/场/节点/项）
    sysrows = 0
    for camp, meta in metas.items():
        for key, vals in (meta.get("mon") or {}).items():
            # 摘要文件名是 <fid>-<虚机名>-summary.txt，而 fid 自己就带 "-"（es819-gbc-r1-try3），
            # 必须按尾部的 -test-worker-N 切，否则 fid 被截成 "es819"，环境行与场次对不上
            m = re.match(r"^(.*)-(test-worker-\d+)$", key)
            fid, node = (m.group(1), m.group(2)) if m else (key.partition("-")[0], key.partition("-")[2])
            for mk, mv in (vals or {}).items():
                if mv is None:
                    continue
                sysrows += 1
                metrics.append({"_index": "bench-metric", "_id": "env|%s|%s|%s" % (camp, key, mk),
                                "@timestamp": None, "campaign": camp, "fid": fid, "node": node,
                                "family": "system", "metric": mk, "stat": "value",
                                "value": (mv / 100.0 if mk.startswith(("cpu_steal", "cpu_iowait", "foreign", "disk_util")) else mv),
                                "unit": ("ratio" if mk.startswith(("cpu_steal", "cpu_iowait", "foreign", "disk_util"))
                                         else ("count" if mk == "swapin" else "")),
                                "value_original": mv, "direction": "lower-better"})

    with open(os.path.join(args.out, "bench-run.jsonl"), "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(args.out, "bench-metric.jsonl"), "w", encoding="utf-8") as f:
        for r in metrics:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(args.out, "bench-env.jsonl"), "w", encoding="utf-8") as f:
        for r in envs.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    TPL = {"index_patterns": ["%s"], "template": {"settings": {"number_of_shards": 1,
             "number_of_replicas": 0}, "mappings": {
        "dynamic_templates": [
            {"strings_as_keyword": {"match": "*", "match_mapping_type": "string",
                                    "mapping": {"type": "keyword"}}},
            {"numbers_as_double": {"match": "*", "match_mapping_type": "long",
                                   "mapping": {"type": "double"}}}],
        "properties": {"@timestamp": {"type": "date",
                                      "format": "strict_date_optional_time||epoch_millis"},
                       "value": {"type": "double"}, "value_original": {"type": "double"},
                       "user_tags": {"type": "flattened"}, "track_params": {"type": "flattened"},
                       "cluster": {"type": "flattened"}}}}}
    templates = {idx: json.loads(json.dumps(TPL).replace("%s", idx))
                 for idx in ("bench-env", "bench-run", "bench-metric")}
    with open(os.path.join(args.out, "templates.json"), "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=1)

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "races_scanned": len(seen), "bench_run": len(runs), "bench_metric": len(metrics),
                   "bench_env": len(envs), "system_rows": sysrows,
                   "campaigns_with_dir": sorted(metas), "races_without_campaign_dir": unmatched,
                   "valid_true": sum(1 for r in runs if r.get("valid") is True),
                   "valid_null": sum(1 for r in runs if r.get("valid") is None)},
                  f, ensure_ascii=False, indent=1)
    shutil.copy2(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench-apply.py"),
                 os.path.join(args.out, "bench-apply.py"))
    print("已导出 → %s" % args.out)
    print(json.dumps(json.load(open(os.path.join(args.out, "manifest.json"))), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
