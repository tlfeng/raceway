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
  bench-apply.py      一键 bulk 导入（幂等 upsert）

时间一律 ms→s、字节→GB、吞吐保留 docs/s 与 ops/s。
error_rate 例外：rally 的 op_metrics.error_rate 本就是 0~1 的比例（实测见过 1.0 = 全量失败），
**不再按 % 折算**；折算会把 100% 打成 1%，掩盖掉 nested 查询全挂的事故场次。
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


# ---------------------------------------------------------------- 环境台账派生 (方案 §3.1)

def env_host_type(env_id, vm_nodes):
    """物理机形态：kvm-vm / bare-metal / docker —— 从 env_id 与节点清单推断，不猜"""
    eid = (env_id or "").lower()
    if "kvm" in eid or "vm" in eid:
        return "kvm-vm"
    if "docker" in eid or "container" in eid:
        return "docker"
    if vm_nodes:
        return "kvm-vm" if any(n.get("ip") for n in vm_nodes) else "bare-metal"
    return None


def env_vcpu(vm_nodes):
    """每节点 vCPU（取第一个节点）"""
    for n in (vm_nodes or []):
        v = n.get("nproc_inside") or n.get("vcpu_hypervisor")
        if v:
            return v
    return None


def env_mem_gb(vm_nodes):
    """每节点内存 GB（取第一个节点）"""
    for n in (vm_nodes or []):
        v = n.get("mem_gib_hypervisor") or n.get("mem_gb")
        if v:
            return v
    return None


def env_notes(e, host_type):
    """环境说明：由 env 数据派生，不写死资产编号/机房等内部信息"""
    nodes = e.get("vm_nodes") or []
    if not nodes:
        return "单机被测集群；rally 发压机与被测集群分离"
    unit = {"kvm-vm": "台 KVM 虚机", "docker": "个容器"}.get(host_type, "个节点")
    return "%d %s组成的被测集群；rally 发压机与被测集群分离" % (len(nodes), unit)


def env_os(vm_nodes):
    """从节点内核推断发行版：6.8.x-generic → Ubuntu 24.04"""
    for n in (vm_nodes or []):
        k = n.get("kernel") or ""
        if k.startswith("6.8."):
            return "Ubuntu 24.04"
        if k.startswith("6.5.") or k.startswith("5.15."):
            return "Ubuntu 22.04"
        if k:
            return k
    return None


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
            fam = key
            # error_rate: rally 给的就是 0~1 比例（实测见过 1.0 = 该 task 全部请求失败），
            #   当成百分比再 /100 会把 100% 打成 1%，把事故场次洗成"几乎无错"。
            # duration: rally 给毫秒，归一到秒。
            raw_unit = "ratio" if key == "error_rate" else "ms"
            nv, nu = norm_value(v, raw_unit)
            out.append({"_index": "bench-metric", "_id": "%s|%s|%s|value" % (run_id, task, fam),
                        "@timestamp": ts, "task": task, "operation": om.get("operation"),
                        "family": fam, "metric": key, "stat": "value", "value": nv, "unit": nu,
                        "value_original": v, "unit_original": raw_unit,
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

    # 预扫 sidecar：学出 arm -> 引擎身份（实测包名/版本/JDK）。
    # 有些场次没有 sidecar（早期 campaign），但同一实验臂的包是同一份 ——
    # 用别的场次实测到的身份回填，比留空或按臂名猜要准。
    arm_identity = {}
    for meta in metas.values():
        seen_side = list(meta.get("sidecar", {}).values()) + list(meta.get("sidecar_by_race", {}).values())
        for s in seen_side:
            arm = (s.get("run") or {}).get("arm")
            if not arm or arm in arm_identity:
                continue
            se = s.get("engine") or {}
            base_pkg = os.path.basename(se.get("pkg") or "")
            eng = ("elasticsearch" if base_pkg.startswith("elasticsearch")
                   else "easysearch" if base_pkg.startswith("easysearch") else None)
            vm = se.get("version_measured_nodes") or []
            jdk_nodes = (s.get("jdk") or {}).get("measured_nodes") or []
            if eng:
                arm_identity[arm] = {
                    "engine": eng, "pkg": se.get("pkg"),
                    "pkg_sha256": se.get("pkg_sha256"),
                    "version": vm[0] if vm else se.get("version_from_pkg"),
                    "build": re.sub(r"-linux.*$", "", se.get("pkg") or "") or None,
                    "jdk": ",".join(sorted({str(x) for x in jdk_nodes})) or None,
                }

    envs, runs, metrics = {}, [], []
    fid_track = {}          # fid → 归一化赛道名；monitors 补的 system 行靠它回填 track
    fid_ts = {}             # fid → 场次时间；system 行靠它回填 @timestamp（画折线的前提）
    fid_valid = {}          # fid → 场次有效性；system 行靠它回填 valid（否则被 KQL 过滤）
    fid_arm = {}            # fid → 实验臂；system 行靠它回填 arm（否则按 arm 分线的面板拿不到 series）
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

        # ---- 实测身份（方案 §3.2「引擎(实测)」+ §3.2「实验变量 typed promote」）----
        se = side.get("engine") or {}
        sj = side.get("jdk") or {}
        srp = side.get("run_params") or {}
        ident = arm_identity.get(arm) or {}
        pkg = se.get("pkg") or ident.get("pkg")
        # 引擎名从包名推导（bench-sidecar.py 同规则）：旧 sidecar 曾把 es819 臂写成 name=easysearch
        base_pkg = os.path.basename(pkg or "")
        engine = ("elasticsearch" if base_pkg.startswith("elasticsearch")
                  else "easysearch" if base_pkg.startswith("easysearch") else None)
        if not engine:
            engine = ("easysearch" if (arm or "").startswith("ez")
                      else "elasticsearch" if (arm or "").startswith("es") else None)
        if not engine:
            # 没有 sidecar 的早期 campaign（如 ds3 维度扫描）：rally 记的 distribution-flavor
            # 足以区分产品线 —— easysearch=oss，elasticsearch=default
            flavor = (d.get("cluster") or {}).get("distribution-flavor")
            engine = {"oss": "easysearch", "default": "elasticsearch"}.get(flavor)
        engine = engine or ident.get("engine")
        vm = se.get("version_measured_nodes") or []
        version = (vm[0] if vm else None) or se.get("version_from_pkg") \
            or ident.get("version") or (d.get("cluster") or {}).get("distribution-version")
        build = re.sub(r"-linux.*$", "", pkg or "") or None
        jdk_nodes = sj.get("measured_nodes") or []
        jdk = ",".join(sorted({str(x) for x in jdk_nodes})) or ut.get("jdk") or ident.get("jdk")
        heap_max = srp.get("heap_max_measured_mb") or []
        heap_gb = round(max(heap_max) / 1024, 2) if heap_max else None
        tp = d.get("track-params") or {}
        # 实验变量：维度/血统/段形态 —— 从 user-tags 与 track params 提升为 typed 列（方案 §3.2）
        dims = ut.get("dim") or tp.get("dim")
        lineage = ut.get("lineage")
        segment_form = ut.get("segment_form") or tp.get("segment_form")

        common = {"race_id": race_id, "campaign": camp, "arm": arm, "engine": engine,
                  "version": str(version) if version is not None else None,
                  "jdk": jdk, "heap_gb": heap_gb,
                  "dims": dims, "lineage": lineage, "segment_form": segment_form,
                  "codec": codec_of(d, side),
                  # track 必须进 common：bench-metric 的指标行没有它就没法按赛道过滤，
                  # 而按赛道拆看板要求每个指标行都带归一化后的赛道名
                  "track": tr, "track_raw": d.get("track"), "phase": phase, "round": rnd,
                  "challenge": d.get("challenge"), "env_id": side.get("env", {}).get("env_id"),
                  "valid": (status == "VALID") if status else None, "status": status}
        fid_track[fid] = tr
        # fid → 场次时间：monitors 补的 system 行本身没有 @timestamp（它是按场次
        # 采集的摘要，不是时间序列），不回填时间戳的话 CPU steal / iowait 这类
        # 数据可信度指标在 Kibana 上画不出折线（x 轴无值）。
        fid_ts.setdefault(fid, ts)
        # fid → 场次有效性：同理，system 行缺 valid 会被 dashboard 的 `valid: true`
        # 查询框整体过滤掉，CPU steal 这类面板就全空。
        fid_valid.setdefault(fid, (status == "VALID") if status else None)
        fid_arm.setdefault(fid, arm)
        runs.append({"_index": "bench-run", "_id": race_id, "race_id": race_id, "@timestamp": ts,
                     "campaign": camp, "fid": fid, "arm": arm, "engine": engine,
                     "version": str(version) if version is not None else None, "build": build,
                     "jdk": jdk, "heap_gb": heap_gb,
                     "dims": dims, "lineage": lineage, "segment_form": segment_form,
                     "track": tr, "track_raw": d.get("track"), "challenge": d.get("challenge"),
                     "phase": phase, "round": rnd, "nnodes": ut.get("nnodes"),
                     "cores_tag": ut.get("cores"), "heap_tag": ut.get("heap"),
                     "pipeline": d.get("pipeline"), "rally_version": d.get("rally-version"),
                     "environment": d.get("environment"), "user_tags": ut,
                     "track_params": d.get("track-params"), "car": d.get("car"),
                     "cluster": d.get("cluster"),
                     "version_from_pkg": (side.get("engine") or {}).get("version_from_pkg"),
                     "pkg": pkg,
                     "pkg_sha256": (side.get("engine") or {}).get("pkg_sha256") or ident.get("pkg_sha256"),
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
                                          "heap_gb": heap_gb, "gc": "G1",
                                          "cpu_pinning": "none" if all(
                                              str(n.get("cpu_pinning_hypervisor")) in ("0", "None", "")
                                              for n in (e.get("vm_nodes") or [{}])) else "pinning"},
                             "host_type": env_host_type(eid, e.get("vm_nodes")),
                             "vcpu": env_vcpu(e.get("vm_nodes")),
                             "mem_gb": env_mem_gb(e.get("vm_nodes")),
                             "cpu_model": (e.get("host") or {}).get("cpu_model"),
                             "disk_type": None, "disk_path": None,
                             "os": env_os(e.get("vm_nodes")),
                             "kernel": (e.get("host") or {}).get("kernel"),
                             "notes": env_notes(e, env_host_type(eid, e.get("vm_nodes"))),
                             "created_at": ts, "active": True,
                             "source": side.get("source")}
            elif eid:
                # 同一环境跨 campaign 复用：堆取各场实测最大值，拓扑以信息更全的一侧为准
                cur = envs[eid]
                t = cur.setdefault("topology", {})
                if heap_gb and (t.get("heap_gb") is None or heap_gb > t["heap_gb"]):
                    t["heap_gb"] = heap_gb
                if not cur.get("kernel"):
                    cur["kernel"] = (e.get("host") or {}).get("kernel")
        elif camp not in metas:
            unmatched += 1

    # monitors → system 指标（bench-metric 补一行/场/节点/相位/项）
    sysrows = 0
    for camp, meta in metas.items():
        for key, vals in (meta.get("mon") or {}).items():
            # 摘要文件名是 <fid>-<虚机名>-summary.txt，而 fid 自己就带 "-"（es819-gbc-r1-try3），
            # 必须按尾部的 -test-worker-N 切，否则 fid 被截成 "es819"，环境行与场次对不上
            m = re.match(r"^(.*)-(test-worker-\d+)$", key)
            fid, node = (m.group(1), m.group(2)) if m else (key.partition("-")[0], key.partition("-")[2])
            # 相位后缀 -w/-m/-q（分相位采集，与 race 行 phase=w/q 同词汇）；剥掉后缀让
            # 环境行与 race 行同 fid 对上，相位记进 phase 字段。报告侧按 fid 聚合取最坏
            # 即自动跨相位；历史 campaign 无后缀 → phase=""
            ph = ""
            if fid.endswith(("-w", "-m", "-q")):
                ph, fid = fid[-1], fid[:-2]
            for mk, mv in (vals or {}).items():
                if mv is None:
                    continue
                # env_verdict 是 OK/FAIL 这类**文字**判定，不是数值 —— 塞进 double 的 value 列
                # 会被 ES 拒收（mapper_parsing_exception），整行静默丢失。改走 value_text 文字列。
                if isinstance(mv, str):
                    sysrows += 1
                    metrics.append({"_index": "bench-metric", "_id": "env|%s|%s|%s" % (camp, key, mk),
                                    "@timestamp": fid_ts.get(fid), "campaign": camp, "fid": fid, "node": node,
                                    "valid": fid_valid.get(fid), "arm": fid_arm.get(fid),
                                    "phase": ph, "track": fid_track.get(fid),
                                    "family": "system", "metric": mk, "stat": "value",
                                    "value": None, "value_text": mv, "unit": "text",
                                    "value_original": None, "direction": "lower-better"})
                    continue
                sysrows += 1
                metrics.append({"_index": "bench-metric", "_id": "env|%s|%s|%s" % (camp, key, mk),
                                "@timestamp": fid_ts.get(fid), "campaign": camp, "fid": fid, "node": node,
                                    "valid": fid_valid.get(fid), "arm": fid_arm.get(fid),
                                "phase": ph, "track": fid_track.get(fid),
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
        # 显式声明实验变量与计数列为 typed —— dims 要能 range/排序，round/attempt 要能当整数分组，
        # 不能落到 dynamic_templates 的 double 上（方案 §3.2「typed promote」）
        "properties": {"@timestamp": {"type": "date",
                                      "format": "strict_date_optional_time||epoch_millis"},
                       "value": {"type": "double"}, "value_original": {"type": "double"},
                       "value_text": {"type": "keyword"},
                       "user_tags": {"type": "flattened"}, "track_params": {"type": "flattened"},
                       "cluster": {"type": "flattened"},
                       "dims": {"type": "integer"}, "round": {"type": "integer"},
                       "attempt": {"type": "integer"}, "shards": {"type": "integer"},
                       "replicas": {"type": "integer"}, "clients": {"type": "integer"},
                       "bulk_size": {"type": "integer"}, "nnodes": {"type": "integer"},
                       "heap_gb": {"type": "double"}, "vcpu": {"type": "integer"},
                       "mem_gb": {"type": "double"},
                       "lineage": {"type": "keyword"}, "segment_form": {"type": "keyword"},
                       "engine": {"type": "keyword"}, "version": {"type": "keyword"},
                       "build": {"type": "keyword"}, "jdk": {"type": "keyword"},
                       "arm": {"type": "keyword"}, "env_id": {"type": "keyword"},
                       "track": {"type": "keyword"}, "campaign": {"type": "keyword"},
                       "family": {"type": "keyword"}, "metric": {"type": "keyword"},
                       "stat": {"type": "keyword"}, "task": {"type": "keyword"},
                       "unit": {"type": "keyword"}, "valid": {"type": "boolean"}}}}}
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
