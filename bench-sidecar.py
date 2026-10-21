#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-sidecar.py —— 每场 race 的 sidecar 组装器（方案 §4）

用法（由 run-bench-jdk.sh 每场调用一次）:
  bench-sidecar.py --raw DIR --out FILE --campaign C --fid F --phase w|q --race-id ID \
      --arm A --track TK --challenge CH --round N --attempt K --track-dir D --track-params "..." \
      --heap 16g --cores 32 --pkg PATH --jdk-want "25|TencentKonaJDK" --env-id bench-3node-32c32g

raw 目录里是编排抓好的原始探针（缺失项记 null，不猜值）:
  nodes-info.txt  curl "_nodes/_all/jvm,os,process,settings"（版本/JDK/heap_max/核数）
  nodes-stats.txt curl "_nodes/_all/stats/jvm,os,process,indices"（heap_used/GC/docs 实测）
  jdk-<ip>.txt    该节点 /infini/easysearch/jdk/bin/java -version 原文
  host.txt        发压机指纹 key=value
  vm-<ip>.txt     虚机指纹 key=value（nproc/mem/disk/kernel/thp/max_map_count/swap）
  virsh.txt       宿主机侧 `name|vcpu|mem_kib|vcpupin数` 每行一节点
  packages.txt    campaign PACKAGES.txt 内容
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys


def read(raw, name):
    p = os.path.join(raw, name)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def kv(text):
    """key=value 文本 → dict（忽略空行与非 kv 行）"""
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def num(v, default=None):
    try:
        return int(v) if re.fullmatch(r"-?\d+", str(v)) else float(v)
    except (TypeError, ValueError):
        return default


def heap_mb(declared):
    m = re.fullmatch(r"(\d+)([gm])", str(declared or "").lower())
    if not m:
        return num(declared)
    return int(m.group(1)) * (1024 if m.group(2) == "g" else 1)


# ---------- 赛道 spec：外部集群运行参数与语料规模（rally race.json 不记这些） ----------
# operation 级参数（target-throughput / clients / iterations）决定了 rally 的 latency 口径：
#   设了 target-throughput → 开环节流 → latency 是端到端（含发压机积压）
#   没设 → 闭环 → latency ≡ service_time
# 这个信息只在赛道 spec 里，race.json 不记，必须在这里解析出来落进 sidecar，
# 否则下游只能靠"latency 与 service_time 是否相等"反推（反推在没饱和时不可靠）。
_OP_RE = re.compile(r'"operation"\s*:\s*"([a-z0-9_-]+)"(?P<rest>[^{}]*?)\}', re.S)


def parse_operations(blob):
    """赛道 spec 文本 → {operation 名: {target_throughput, clients, iterations, warmup_iterations}}"""
    out = {}
    for m in _OP_RE.finditer(blob or ""):
        name, rest = m.group(1), m.group("rest")
        op = {}
        for key, field in (("target-throughput", "target_throughput"),
                           ("clients", "clients"),
                           ("iterations", "iterations"),
                           ("warmup-iterations", "warmup_iterations")):
            mm = re.search(r'"%s"\s*:\s*([0-9.]+)' % re.escape(key), rest)
            if mm:
                try:
                    op[field] = float(mm.group(1)) if "." in mm.group(1) else int(mm.group(1))
                except ValueError:
                    pass
        if op:
            out[name] = op
    return out


def latency_modes_of(operations):
    """operation → latency 口径（地面真值，不靠反推）"""
    modes = {}
    for name, op in (operations or {}).items():
        modes[name] = "open_loop" if op.get("target_throughput") else "closed_loop"
    return modes


def track_meta(track_dir, track_params):
    tp = dict(kv_track_params(track_params))
    res = {
        "shards": num(tp.get("number_of_shards")),
        "replicas": num(tp.get("number_of_replicas")),
        "bulk_size": num(tp.get("bulk_size")),
        "clients": num(tp.get("bulk_indexing_clients")),
        "overrides": tp,
        "corpus": None,
        "corpus_docs": None,
        "corpus_source": None,
        "operations": {},
        "latency_modes": {},
        "source": {"bulk_size": None, "clients": None, "corpus_docs": None},
    }
    if not track_dir or not os.path.isdir(track_dir):
        return res
    tj = os.path.join(track_dir, "track.json")
    if os.path.isfile(tj):
        t = read(track_dir, "track.json")
        m = re.search(r'"corpora"\s*:\s*\[\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', t, re.S)
        if m:
            res["corpus"] = m.group(1)
        m = re.search(r'"source-path"\s*:\s*"([^"]+)"', t)
        if m:
            res["corpus_source"] = m.group(1)
        m = re.search(r'"document-count"\s*:\s*(\d+)', t)
        if m:
            res["corpus_docs"] = int(m.group(1))
            res["source"]["corpus_docs"] = "track.json"
    # spec 里的 jinja 默认值（未被 --track-params 覆盖时即为实际生效值）
    spec = []
    op_files = {}
    py_blob = []
    for sub in ("operations", "challenges", "."):
        d = os.path.join(track_dir, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                t = read(d, fn) or ""
                spec.append(t)
                op_files[fn] = t
            elif fn.endswith(".py"):
                py_blob.append(read(d, fn) or "")
    blob = "\n".join(spec)
    for key, pat in (("bulk_size", r'"bulk-size"\s*:\s*\{\{\s*bulk_size\s*\|\s*default\((\d+)\)\}\}'),
                     ("clients", r'"clients"\s*:\s*\{\{\s*bulk_indexing_clients\s*\|\s*default\((\d+)\)\}\}')):
        if res[key] is None:
            m = re.search(pat, blob)
            if m:
                res[key] = int(m.group(1))
                res["source"][key] = "track spec default"
    res["operations"] = parse_operations(blob)
    res["latency_modes"] = latency_modes_of(res["operations"])
    res["knn_params"] = parse_knn_params(blob, op_files, "\n".join(py_blob))
    return res


def parse_knn_params(spec_blob, op_files, py_blob=""):
    """KNN 索引/查询参数 —— 没有就不臆造。

    KNN 赛道只比吞吐不看召回、不记索引参数，"快"就无从判断是检索算法更优
    还是候选集/近似程度不同。这里把 dims / 索引类型 / m / ef_construction /
    num_candidates / 每个 task 的 k 一起落库。
    """
    p = {}
    m = re.search(r'"dims"\s*:\s*(\d+)', spec_blob or "")
    if m:
        p["dims"] = int(m.group(1))
    m = re.search(r'"element_type"\s*:\s*"([^"]+)"', spec_blob or "")
    if m:
        p["element_type"] = m.group(1)
    m = re.search(r'"index_options"\s*:\s*\{[^}]*?"type"\s*:\s*"([^"]+)"', spec_blob or "", re.S)
    if m:
        p["index_type"] = m.group(1)
    for key, field in (("m", "m"), ("ef_construction", "ef_construction")):
        mm = re.search(r'"index_options"\s*:\s*\{[^}]*?"%s"\s*:\s*(\d+)' % key, spec_blob or "", re.S)
        if mm:
            p[field] = int(mm.group(1))
    # num_candidates 在 param-source 的 .py 里（默认 100），spec JSON 里也可能出现
    for blob in (spec_blob or "", py_blob or ""):
        mm = re.search(r'"num[-_]candidates"\s*:\s*(\d+)', blob) or \
             re.search(r'"num-candidates"\s*,\s*(\d+)', blob)
        if mm:
            p["num_candidates"] = int(mm.group(1))
            break
    kmap = {}
    for fname, text in (op_files or {}).items():
        try:
            d = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        k = d.get("k")
        if name and k is not None:
            try:
                kmap[name] = int(k)
            except (TypeError, ValueError):
                pass
    if kmap:
        p["k_by_task"] = kmap
    return p or None


def node_extras(full_stats_text):
    """从完整 _nodes/stats 探针里取"引擎压力与健康"指标。

    只取拿得到就落、拿不到就不落 —— 旧探针（_nodes/stats/jvm,os,process,indices）
    不含这些字段。缺它们的后果：压测中"是不是把线程池打爆了""缓存命中多少"
    "引擎侧累计查询耗时"全部无法回答，饱和与限流在数据上不可区分。
    """
    nodes = load_nodes(full_stats_text)
    out = {}
    for nid, n in nodes.items():
        e = {}
        tp = n.get("thread_pool") or {}
        for pool in ("search", "write", "get", "force_merge"):
            p = tp.get(pool) or {}
            if p:
                e["thread_pool_%s" % pool] = {
                    "rejected": p.get("rejected"), "queue": p.get("queue"),
                    "active": p.get("active"), "completed": p.get("completed"),
                }
        br = n.get("breaker") or {}
        if br:
            e["breaker_tripped"] = sum((v or {}).get("tripped", 0) for v in br.values())
            e["breaker_tripped_count"] = {k: (v or {}).get("tripped") for k, v in br.items()}
        qc = ((n.get("indices") or {}).get("query_cache") or {})
        if qc:
            e["query_cache"] = {"hit_count": qc.get("hit_count"),
                                "miss_count": qc.get("miss_count"),
                                "memory_size_in_bytes": qc.get("memory_size_in_bytes")}
        idx = n.get("indices") or {}
        s = idx.get("search") or {}
        if s:
            e["engine_query_time_ms"] = s.get("query_time_in_millis")
            e["engine_query_total"] = s.get("query_total")
        i = idx.get("indexing") or {}
        if i:
            e["engine_index_time_ms"] = i.get("index_time_in_millis")
            e["engine_index_total"] = i.get("index_total")
        seg = idx.get("segments") or {}
        if seg:
            e["segment_count"] = seg.get("count")
        fs = n.get("fs") or {}
        if fs:
            tot = fs.get("total") or {}
            e["disk_total_gb"] = round((tot.get("total_in_bytes") or 0) / 1e9, 2) or None
            e["disk_free_gb"] = round((tot.get("free_in_bytes") or 0) / 1e9, 2) or None
        pr = n.get("process") or {}
        if pr.get("cpu"):
            e["process_cpu_percent"] = (pr.get("cpu") or {}).get("percent")
        if e:
            out[nid] = e
    return out or None


def kv_track_params(s):
    out = []
    for part in (s or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out.append((k.strip(), v.strip()))
    return out


# ---------- rally _nodes 实测（Easysearch 只认 info/stats 两路，fs/filesystem/mem 等 metric 名会 400） ----------
def load_nodes(text):
    if not text:
        return {}
    try:
        return json.loads(text[text.index("{"):]).get("nodes") or {}
    except (ValueError, json.JSONDecodeError):
        return {}


def mb(v):
    return int(v / 1048576) if isinstance(v, (int, float)) else None


def mbf(v):
    """KB~MB 量级的小计数保留两位小数：int 截断会把 0.8MB 抹成 0，分不清"没报"和"很小\""""
    return round(v / 1048576, 2) if isinstance(v, (int, float)) else None


def gc_of(n):
    col = ((n.get("jvm") or {}).get("mem") or {}).get("gc", {}).get("collectors", {}) \
        or ((n.get("jvm") or {}).get("gc", {}) or {}).get("collectors", {})
    out = {}
    for gen in ("young", "old"):
        c = col.get(gen) or {}
        out[f"{gen}_count"] = c.get("collection_count")
        out[f"{gen}_time_ms"] = c.get("collection_time_in_millis")
    return out


def nodes_measured(info_text, stats_text):
    info, stats = load_nodes(info_text), load_nodes(stats_text)
    out = []
    for nid in sorted(set(info) | set(stats)):
        n = dict(info.get(nid) or {})
        s = stats.get(nid) or {}
        jvm_i, os_i = n.get("jvm") or {}, n.get("os") or {}
        jvm_s, os_s = s.get("jvm") or {}, s.get("os") or {}
        idx = (s.get("indices") or {}).get("docs") or {}
        # 段内存只记仍在真实计数的 on-heap 三项：rally 经 index stats 采的 terms/norms/
        # doc_values/points 等六项在新 Lucene（ES 8.19、ez240/250 实测）已衰减为恒 0
        # （reader 挪到 mmap 堆外），index_writer/version_map/fixed_bit_set 仍是实值
        seg = (s.get("indices") or {}).get("segments") or {}
        rec = {
            "name": n.get("name") or s.get("name"),
            "ip": n.get("host") or n.get("ip") or (s.get("host") or s.get("ip")),
            "version_measured": n.get("version"),
            "roles": ",".join(sorted(n.get("roles", []))) or None,
            "jdk_version_measured": jvm_i.get("version"),
            "jvm_vm_version_measured": jvm_i.get("vm_version"),
            "jvm_heap_flags": [x for x in (jvm_i.get("input_arguments") or []) if "Xm" in x] or None,
            "heap_max_mb": mb((jvm_i.get("mem") or {}).get("heap_max_in_bytes")),
            "available_processors_measured": os_i.get("available_processors"),
            "kernel_measured": os_i.get("kernel") or os_s.get("name"),
            "uptime_ms": jvm_s.get("uptime_in_millis") or jvm_i.get("uptime_in_millis"),
            "heap_used_mb": mb(((jvm_s.get("mem") or {}).get("heap_used_in_bytes"))),
            "cpu_load_average_1m": (os_s.get("cpu") or {}).get("load_average"),
            "docs_total_measured": idx.get("count"),
            "index_writer_memory_mb": mbf(seg.get("index_writer_memory_in_bytes")),
            "version_map_memory_mb": mbf(seg.get("version_map_memory_in_bytes")),
            "fixed_bit_set_memory_mb": mbf(seg.get("fixed_bit_set_memory_in_bytes")),
        }
        rec.update({k: v for k, v in gc_of(s).items() if v is not None})
        out.append(rec)
    return out


def jdk_per_node(raw, ips):
    out = []
    for ip in ips:
        t = read(raw, f"jdk-{ip}.txt")
        lines = [l.strip() for l in (t or "").splitlines() if l.strip()][:3]
        out.append({"ip": ip, "java_version": lines or None})
    return out


def main():
    a = argparse.ArgumentParser()
    for k in ("raw", "out", "campaign", "fid", "phase", "race_id", "arm", "track", "challenge",
              "heap", "cores", "pkg", "jdk_want", "env_id", "track_dir"):
        a.add_argument("--" + k.replace("_", "-"), required=k in ("raw", "out", "campaign", "fid"))
    a.add_argument("--round", type=int, default=None)
    a.add_argument("--attempt", type=int, default=1)
    a.add_argument("--track-params", default="")
    args = a.parse_args()

    ips = sorted({re.fullmatch(r"vm-([0-9.]+)\.txt", f).group(1)
                  for f in os.listdir(args.raw) if re.fullmatch(r"vm-[0-9.]+\.txt", f)})

    virsh = {}
    for line in (read(args.raw, "virsh.txt") or "").splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            virsh[parts[0]] = {"vcpu": num(parts[1]), "mem_gib": num(float(parts[2]) / 1048576) if parts[2] else None,
                               "vcpupin": (parts[3] if len(parts) > 3 else "0")}

    vm_fp = {}
    for ip in ips:
        d = kv(read(args.raw, f"vm-{ip}.txt"))
        thp_raw = d.get("thp") or ""
        m = re.search(r"\[(\w+)\]", thp_raw)
        vm_fp[ip] = {
            "ip": ip, "hostname": d.get("hostname"), "name": d.get("name"),
            "nproc_inside": num(d.get("nproc")),
            "mem_gb": round(num(d.get("mem_mb"), 0) / 1024.0, 1) if d.get("mem_mb") else None,
            "disk_avail_gb": num((d.get("disk_avail_gb") or "").strip("G")), "kernel": d.get("kernel"),
            # 磁盘类型/设备：写入性能的一阶因子，换 NVMe→SATA 的结论没有它做不出来
            "disk_path": d.get("disk_path"), "disk_type": d.get("disk_type"),
            "disk_rota": d.get("disk_rota"),
            "thp": m.group(1) if m else (thp_raw or None), "thp_raw": thp_raw or None,
            "max_map_count": num(d.get("max_map_count")),
            "swap_lines": num(d.get("swap_lines")), "cpu_model": d.get("cpu_model"),
            "vcpu_hypervisor": virsh.get(d.get("name", ""), {}).get("vcpu"),
            "mem_gib_hypervisor": virsh.get(d.get("name", ""), {}).get("mem_gib"),
            "cpu_pinning_hypervisor": virsh.get(d.get("name", ""), {}).get("vcpupin"),
        }

    host = kv(read(args.raw, "host.txt"))
    tm = track_meta(args.track_dir, args.track_params)
    pkg = read(args.raw, "packages.txt") or ""
    sha = None
    if args.pkg:
        m = re.search(r"([0-9a-f]{64})\s+\S*" + re.escape(os.path.basename(args.pkg)), pkg)
        sha = m.group(1) if m else None
    jdk_want_major, _, jdk_want_vendor = (args.jdk_want or "").partition("|")
    measured = nodes_measured(read(args.raw, "nodes-info.txt") or read(args.raw, "nodes.txt"),
                              read(args.raw, "nodes-stats.txt"))
    jdk_raw = jdk_per_node(args.raw, ips)

    def detect_vendor(text):
        if "TencentKonaJDK" in text:
            return "TencentKonaJDK"
        if "GraalVM" in text:
            return "GraalVM"
        if "OpenJDK" in text or "openjdk" in text:
            return "OpenJDK"
        return None

    raw_by_ip = {r["ip"]: " ".join(r["java_version"] or []) for r in jdk_raw}
    per_node = []
    for m in measured:
        vd = detect_vendor(raw_by_ip.get(m["ip"], "")) or detect_vendor(m["jvm_vm_version_measured"] or "")
        per_node.append({"ip": m["ip"], "name": m["name"],
                         "major_measured": m["jdk_version_measured"], "vendor_detected": vd})
    match_want = None
    if jdk_want_major and jdk_want_vendor and per_node:
        match_want = all(str(n["major_measured"] or "").split(".")[0] == jdk_want_major
                         and n["vendor_detected"] == jdk_want_vendor for n in per_node)

    doc = {
        "schema": "sidecar-v1",
        "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "run": {
            "campaign": args.campaign, "fid": args.fid, "phase": args.phase,
            "race_id": args.race_id or None, "arm": args.arm, "track": args.track,
            "challenge": args.challenge, "round": args.round, "attempt": args.attempt,
        },
        "engine": {
            # 从包名推导，不能写死 easysearch：es819 臂的 sidecar 曾经 name=easysearch 而 pkg/version=8.19.23 自相矛盾
            "name": engine_name(args.pkg),
            "pkg": os.path.basename(args.pkg) if args.pkg else None,
            "pkg_sha256": sha,
            "version_from_pkg": version_from_pkg(args.pkg),
            "version_measured_nodes": sorted({m["version_measured"] for m in measured if m["version_measured"]}) or None,
        },
        "jdk": {
            "declared_arm": args.arm,
            "want": {"major": jdk_want_major or None, "vendor": jdk_want_vendor or None},
            "measured_nodes": [m["jdk_version_measured"] for m in measured] or None,
            "measured_vm_versions": [m["jvm_vm_version_measured"] for m in measured] or None,
            "java_version_raw_per_node": jdk_raw,
            "per_node": per_node or None,
            "match_want": match_want,
        },
        "run_params": {
            "heap_declared": args.heap, "heap_declared_mb": heap_mb(args.heap),
            "heap_max_measured_mb": [m["heap_max_mb"] for m in measured] or None,
            "cores_declared": num(args.cores),
            "available_processors_measured": [m["available_processors_measured"] for m in measured] or None,
            "index_writer_memory_measured_mb": [m["index_writer_memory_mb"] for m in measured] or None,
            "version_map_memory_measured_mb": [m["version_map_memory_mb"] for m in measured] or None,
            "fixed_bit_set_memory_measured_mb": [m["fixed_bit_set_memory_mb"] for m in measured] or None,
            "shards": tm["shards"], "replicas": tm["replicas"],
            "bulk_size": tm["bulk_size"], "clients": tm["clients"],
            "params_source": tm["source"], "track_params_passed": args.track_params or None,
            # operation 级参数与 latency 口径（地面真值）：rally 的 latency 在开环 task
            # （设了 target-throughput）里含发压机积压，闭环 task 里 ≡ service_time。
            # 没有这一项，下游只能靠"latency 是否等于 service_time"反推 —— 反推在
            # 没饱和时会把开环 task 误判成闭环。
            "operations": tm["operations"] or None,
            "latency_modes": tm["latency_modes"] or None,
            # KNN 索引/查询参数：不记就只能比吞吐，无法判断"快"是算法更优还是候选集不同
            "knn_params": tm["knn_params"],
            # 集群里实测生效的索引 settings（codec/source_reuse 类结论的可证明来源：
            # 赛道烘了某行 ≠ 引擎真接受了那行，ES 拒绝未知设置时会静默回落到默认编码）
            "index_settings_measured": index_settings(read(args.raw, "index-settings.txt")),
        },
        "corpus": {"name": tm["corpus"], "docs": tm["corpus_docs"],
                   "source_path": tm["corpus_source"],
                   "spec_hash": corpus_spec_hash(tm)},
        "cluster_measured": measured,
        "cluster_extras": node_extras(read(args.raw, "nodes-stats-full.txt")),
        "env": {
            "env_id": args.env_id,
            "host": {"hostname": host.get("hostname"), "cpu_model": host.get("cpu_model"),
                     "logical_cores": num(host.get("nproc")), "mem_gb": num(host.get("mem_gb")),
                     "kernel": host.get("kernel"), "role": "rally 发压机/控制机"},
            "vm_nodes": list(vm_fp.values()),
        },
        "source": ["race.json(rally FileRaceStore)",
                   "_nodes/_all/jvm,os,process,settings", "_nodes/_all/stats/jvm,os,process,indices",
                   "pkg java -version", "procfs/lscpu/virsh", "track spec", "PACKAGES.txt"],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"sidecar → {os.path.basename(args.out)}（测量节点 {len(measured)} 台，指纹 {len(vm_fp)} 台）")


def corpus_spec_hash(tm):
    """语料规格指纹（name + docs + source-path 的稳定摘要）。

    不做内容级哈希（语料是 GB 级，每场重算会把压测机拖垮），而是把"这份语料是哪个
    spec 的第几版"钉住 —— 同名语料在不同场次是不是同一份，靠它判断。
    """
    key = "|".join(str(x) for x in (tm.get("corpus"), tm.get("corpus_docs"), tm.get("corpus_source")))
    if not any((tm.get("corpus"), tm.get("corpus_docs"), tm.get("corpus_source"))):
        return None
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def index_settings(text):
    """GET /<index>/_settings?flat_settings=true 原文 → {索引名: 扁平 settings}；解析不了返回 None"""
    if not text:
        return None
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or not d:
        return None
    return {k: v.get("settings", v) for k, v in d.items() if isinstance(v, dict)}


def engine_name(pkg):
    """包名 → 引擎名（es819 臂不能再记成 easysearch）"""
    b = os.path.basename(pkg or "")
    if b.startswith("elasticsearch"):
        return "elasticsearch"
    if b.startswith("easysearch"):
        return "easysearch"
    return None


def version_from_pkg(pkg):
    if not pkg:
        return None
    m = re.search(r"^(?:easysearch|elasticsearch)-([0-9][^-]*(?:-[0-9]+)?)-", os.path.basename(pkg))
    return m.group(1) if m else None


if __name__ == "__main__":
    sys.exit(main())
