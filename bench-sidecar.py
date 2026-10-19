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
        m = re.search(r'"document-count"\s*:\s*(\d+)', t)
        if m:
            res["corpus_docs"] = int(m.group(1))
            res["source"]["corpus_docs"] = "track.json"
    # spec 里的 jinja 默认值（未被 --track-params 覆盖时即为实际生效值）
    spec = []
    for sub in ("operations", "challenges", "."):
        d = os.path.join(track_dir, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                spec.append(read(d, fn) or "")
    blob = "\n".join(spec)
    for key, pat in (("bulk_size", r'"bulk-size"\s*:\s*\{\{\s*bulk_size\s*\|\s*default\((\d+)\)\}\}'),
                     ("clients", r'"clients"\s*:\s*\{\{\s*bulk_indexing_clients\s*\|\s*default\((\d+)\)\}\}')):
        if res[key] is None:
            m = re.search(pat, blob)
            if m:
                res[key] = int(m.group(1))
                res["source"][key] = "track spec default"
    return res


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
            "shards": tm["shards"], "replicas": tm["replicas"],
            "bulk_size": tm["bulk_size"], "clients": tm["clients"],
            "params_source": tm["source"], "track_params_passed": args.track_params or None,
            # 集群里实测生效的索引 settings（codec/source_reuse 类结论的可证明来源：
            # 赛道烘了某行 ≠ 引擎真接受了那行，ES 拒绝未知设置时会静默回落到默认编码）
            "index_settings_measured": index_settings(read(args.raw, "index-settings.txt")),
        },
        "corpus": {"name": tm["corpus"], "docs": tm["corpus_docs"]},
        "cluster_measured": measured,
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
