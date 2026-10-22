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
import importlib

# 压缩 / source_reuse 两个维度的推导与 legacy 拆分放在 bench-history（与编排台
# 同一份实现），避免两处各写一套慢慢漂移。
H = importlib.import_module('bench-history')  # 文件名带连字符

DOCS = {"track": 11396503, "hnsw2m-2m-s3": 2000000, "nested-s3": 11203029,
        "hv1024-s3": 1000000, "dsj512-s3": 1000000, "dsj768-s3": 1000000,
        "dsj1024-s3": 1000000, "dsj1536-s3": 1000000, "dsj2048-s3": 1000000,
        "dsg960-s3": 1000000, "dsg1920-s3": 1000000,
        # geonames 存储压缩 × source_reuse 正交六变体（prepare-codec-track.sh 生成，
        # 语料与基座同一份 → doc 数相同）
        "geo-codec-def": 11396503, "geo-codec-def-sr": 11396503,
        "geo-codec-zstd": 11396503, "geo-codec-zstd-sr": 11396503,
        "geo-codec-bc": 11396503, "geo-codec-bc-sr": 11396503}
TRACK_NAME = {"track": "geo", "hnsw2m-2m-s3": "hnsw", "nested-s3": "nested", "hv1024-s3": "hv1024"}


def track_name_of(raw):
    """变体目录名（track_raw）→ 基础赛道名。

    显式表（基座目录名）+ 规则（任意 `<base>-codec-*` 变体 → 它的 base）。
    规则化才能推广到全部基础赛道，新增赛道不用再改表。
    """
    if raw in TRACK_NAME:
        return TRACK_NAME[raw]
    if raw and '-codec-' in raw:
        return H.base_of(raw)
    return raw
# 变体目录 → (压缩, source_reuse) 两个**独立维度**的期望值：**按规则推导**
# （变体 id 本身就编码了压缩与 sr），不用查表 —— 新增赛道/变体不用改表。
# 压缩：default(LZ4) / ZSTD / best_compression(DEFLATE)
# source_reuse：true / false —— Easysearch 专有（ES 不认 index.source_reuse）
PHASE = {"write-only": "w", "query-only": "q", "index-only": "w",
         "nested-search-challenge": "wq", "default": "wq"}
LOWER_BETTER = ("latency", "service_time", "processing_time", "error_rate", "duration",
                "indexing_time", "merge_time", "refresh_time", "flush_time", "gc",
                "disk", "store", "system", "memory", "queue_time")
# 注意：recall 是 higher-better（召回越高越好），不能进这个列表。
# 只列当前真正会产出且方向一致的 family —— 列表里出现"还没实现"的名字，
# 将来谁走 direction_of() 就会拿到相反的方向。
# ---------------------------------------------------------------------------
# 口径契约（词表）：stat / unit 只允许出现在这两个闭集里。
#
# 为什么要闭集（2026-10-20 复审）：
#  1) rally 的原始 stat 名有 "50_0"/"90_0"/"99_0"/"99_9"/"99_99"/"100_0"/"median"…
#     旧映射漏了 "99_99"（rally 的 99.99 分位）→ 它原样漏进 stat 列，
#     于是 stat 列同时存在 p999 与 99_99 两个名字指同一类分位，画图按 stat 过滤会静默漏一半。
#  2) rally 的 "median"（吞吐块）与 "50_0"（时延块）是同一个统计量，
#     却落成 p50 与 median 两个名字 —— 同一根 stat 轴上混两种标签，不可比。
#  3) unit 同理：漏一个就多一种量纲，跨 family 求和/对比会出错。
# 归一后 stat 轴全域一致；旧名 → 新名会改 _id（见 --drop 说明），必须重建索引。
CANON_STATS = ("value", "mean", "p50", "p90", "p99", "p999", "p9999", "p100", "min", "max")
CANON_UNITS = ("s", "ms", "ops/s", "docs/s", "pages/s", "MB/s", "GB", "MB", "bytes",
               "count", "ratio", "percent", "text", "")
CANON_QUALITY = ("ok", "degraded", "unknown")
CANON_LATENCY_MODE = ("open_loop", "closed_loop", "unknown")
# 兜底别名（同一块里归一后撞名时用）：非致命，但必须在 manifest 里可见
_STAT_ALIAS_RE = re.compile(r"^(?:value|mean|p50|p90|p99|p999|p9999|p100|min|max)_raw\d*$")
STAT_MAP = {"50_0": "p50", "median": "p50", "90_0": "p90", "99_0": "p99",
            "99_9": "p999", "99_99": "p9999", "100_0": "p100",
            "min": "min", "max": "max", "mean": "mean", "value": "value"}
# 归一后仍与同块其它 key 撞名时的兜底后缀（保证 _id 唯一，撞名本身会被断言抓到）
STAT_ALIAS_SUFFIX = "_raw"
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
    # memory_segments/doc_values/terms/norms/points/stored_fields 六项不入库：
    # ES 8.19 与 ez240/250 实测恒为 0（新 Lucene 把段 reader 挪到 mmap 堆外，
    # on-heap 估算衰减为 0；HNSW 向量图本就不在统计内）。仍在真实计数的 on-heap
    # 段内存（index_writer/version_map/fixed_bit_set）由 bench-sidecar 赛末补采。
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


# system 家族（load-monitor.sh 采样）里各指标的量纲与方向。
# 百分比类一律归一到 0~1 的 ratio；吞吐类保留原单位。
_SYS_PCT = ("cpu_steal", "foreign_cpu", "cpu_usr", "cpu_sys", "bench_cpu")
_SYS_MBPS = ("disk_rw_mbs", "net_rx_mbs", "net_tx_mbs")
_SYS_GB = ("mem_avail_gb",)


def unit_of(metric):
    if metric.startswith(_SYS_PCT):
        return "ratio"
    if metric.startswith(_SYS_MBPS):
        return "MB/s"
    if metric.startswith(_SYS_GB):
        return "GB"
    if metric == "swapin":
        return "count"
    return ""


def direction_of_system(metric):
    """system 家族的方向：
    污染类（steal/外来进程/swap）与 CPU 负载类 → 越低越干净（lower-better）；
    吞吐类（磁盘/网络）与内存余量 → 越高越好（higher-better）。"""
    if metric.startswith(_SYS_MBPS) or metric.startswith(_SYS_GB):
        return "higher-better"
    return "lower-better"


def codec_dims(d, side):
    """本场**实测生效**的存储设置 → (compression, source_reuse) 两个**独立维度**。

    为什么拆两个维度：压缩（index.codec）与 source_reuse 是两个独立变量，
    糅在一个串里（旧的 "ZSTD+source_reuse"）就没法归因"是谁带来的差异"。
    拆开后任意一维都能单独分组对比。

    优先级：sidecar 的 _settings 回读（最权威，是实测值）
          → user_tags（新格式 codec/source_reuse 分开；旧格式 "ZSTD+source_reuse" 拆开）
          → 赛道变体名兜底。
    非 codec 变体场次 → (None, None)。
    """
    st = ((side.get("run_params") or {}).get("index_settings_measured") or {}) if side else {}
    for _idx, s in (st.items() if isinstance(st, dict) else []):
        c = (s or {}).get("index.codec")
        sr = str((s or {}).get("index.source_reuse", "")).lower()
        if c:
            return c, ("true" if sr == "true" else "false")
    ut = d.get("user-tags") or {}
    c = ut.get("codec")
    if c:
        return H.split_codec(c, ut.get("source_reuse"))
    tr = d.get("track")
    return H.compression_of(tr), (('true' if H.source_reuse_of(tr) else 'false')
                                  if H.source_reuse_of(tr) is not None else None)


def codec_of(d, side):
    """兼容旧调用点：只取压缩维度（source_reuse 另有 source_reuse_of）。"""
    return codec_dims(d, side)[0]


def source_reuse_of(d, side):
    """本场是否开了 index.source_reuse（Easysearch 专有设置）。"""
    return codec_dims(d, side)[1]


def knn_params_of(side):
    """KNN 索引参数（k / ef_search / num_candidates / M / ef_construction / index type / 量化）。

    没有实测来源就返回 None —— 不臆造。KNN 赛道只比吞吐不看召回、不记索引参数，
    "快"是拿不同候选集换来的，比值没有意义。
    """
    p = ((side or {}).get("run_params") or {}).get("knn_params")
    return p if isinstance(p, dict) and p else None


def fid_of(d):
    ut = d.get("user-tags") or {}
    # 改名兼容：改名前写的是 user-tags.arm，改名后写 user-tags.car。两者都读，
    # 老的 race.json（改名前跑的那批）照样能解析出车的身份。
    car = ut.get("car") or ut.get("arm") or ut.get("engine") or "?"
    tr = track_name_of(d.get("track"))
    return car, tr, ut


# ------------------------------------------------------------------ 图例标签派生
#
# 看板图例只显示车名（ez250 / jdk25@3015 / kona25@3015）时，读者看不出三件事：
# 引擎版本（ez240 与 ez250 都是 EasySearch，版本 2.4.0 / 2.5.0）、
# 引擎 build（3015 / 3009 / 2975，同版本不同 build 的结论不能混）、
# JDK 发行版（jdk25 车是 Oracle GraalVM 25，kona25 车是 Tencent Kona 25，
# 两者主版本号都是 25，车名区分得开但看不出是两种发行版）。
#
# 系列键口径（2026-10-21 sam 确认）：引擎名@版本号@build号@JDK名
#   ez@2.4.0@2975@graalvm21     ez@2.5.0@3015@graalvm25
#   ez@2.5.0@3015@kona25        es@8.19.23@—@openjdk27
# 口径代码在 identity.py（唯一来源），这里只做引用 —— 别在这里另写一份。
#
# 身份维度正交拆列（2026-10-21 sam 确认）：engine / version / engine_build /
# jdk_vendor / jdk_version / series_key。索引里不再存 car / car_build / car_label /
# arm* —— 那些都是「车代号」的重复编码，车代号只留在 user_tags.car。
# jdk_vendor 必须单列：GraalVM 25 与 Kona 25 的完整版本号都是 25.0.4，
# 只有厂商能区分，而 JDK 厂商对比正是本项目要回答的问题。

try:
    identity_mod = importlib.import_module('identity')
except ImportError:                                                    # 从别处 import 本文件时
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    identity_mod = importlib.import_module('identity')
CAR_FALLBACK = identity_mod.CAR_FALLBACK
BUILD_UNKNOWN = identity_mod.BUILD_UNKNOWN
ENGINE_SHORT = identity_mod.ENGINE_SHORT
JDK_VENDOR_PREFIX = identity_mod.JDK_VENDOR_PREFIX
series_key_of = identity_mod.series_key_of
jdk_short_of = identity_mod.jdk_short_of
jdk_vm_of = identity_mod.jdk_vm_of
jdk_vendor_of = identity_mod.jdk_vendor_of
jdk_major_of = identity_mod.jdk_major_of
jdk_version_of = identity_mod.jdk_version_of


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


# ------------------------------------------------------------------ 源头证据
#
# 引擎身份（版本 + build）与 JDK 身份都有**实测/记录级**的源头，
# 比"从车代号猜"可靠得多。三条源头，按可信度从高到低：
#
#   ① sidecar（赛末实测）：engine.pkg 包名 + jdk.per_node 的 vendor/major
#   ② 部署日志（部署脚本自己写的）：logs/ibs-deploy-<车>.log 里的
#      "Deploying Easysearch 2.4.0-2975" / "Deploying Elasticsearch 8.19.21"
#      logs/jdk-<车>.txt 里的 java -version 原文
#   ③ revision 指纹（race.json 的 cluster.revision）：引擎源码提交号，
#      与包名一一对应（见 REVISION_IDENTITY，由①②实测归纳）
#
# 为什么不用 campaign 的 PACKAGES.txt：那是"这一批装了哪些包"的清单，
# 不是"这个车实际跑的是哪个包" —— ez240 车在 jdk3-20261016-* 批次里跑的是
# 2.4.0-2975，PACKAGES.txt 里写的却是该批默认包 2.5.0-3015，
# 按它推会把 ez240 标成 "ez240@3015"（引擎版本还是 2.4.0，自相矛盾）。
#
# revision → 身份对照表（每条都有部署日志或 sidecar 实测背书）：
#   d8d3be54… → EasySearch 2.5.0-3015（部署日志 ×6 + sidecar 实测）
#   fab83e9f… → EasySearch 2.5.0-3009（部署日志 ×3 + sidecar 实测）
#   79c5d197… → EasySearch 2.4.0-2975（部署日志 ×5 + sidecar 实测）
#   bf8501f2… → EasySearch 2.5.0-3003（部署日志 ×2）
#   b21f3974… → EasySearch 2.5.0（构建号无记录 —— 这批只有 car-es819.log，
#                没有部署日志；不猜）
#   4fe44c25… → Elasticsearch 8.19.21（部署日志 ×2）
#   6940e499… → Elasticsearch 8.19.23（部署日志 ×3 + sidecar 实测）
REVISION_IDENTITY = {
    "d8d3be542d807ad57d4becbd0d4e95b560107453": ("easysearch", "2.5.0", "3015"),
    "fab83e9f78f32c50dcd3793ff9e5a2d8c19210a9": ("easysearch", "2.5.0", "3009"),
    "79c5d197a9ba7b28ec89b149c9eb320c52c5a784": ("easysearch", "2.4.0", "2975"),
    "bf8501f2c621318bc031a859615402379ca9d7c1": ("easysearch", "2.5.0", "3003"),
    "b21f39745344f63923d66df18875f1ddf487c5c6": ("easysearch", "2.5.0", None),
    "4fe44c255c3d0da06779b921e132cdc555ed9aff": ("elasticsearch", "8.19.21", None),
    "6940e499f8a38841809b8e295a20c6421c95d9e4": ("elasticsearch", "8.19.23", None),
}

_DEPLOY_RE = re.compile(r"Deploying\s+(Easysearch|Elasticsearch)\s+([0-9][0-9.]*)(?:-(\d+))?")


def parse_deploy_logs(camp_dir):
    """campaign/logs 里的部署日志 → {车或引擎名: (engine, version, build)}。

    文件名两种写法都见过：ibs-deploy-<车>.log（ibs 部署脚本）与 deploy-<车>.log。
    车名取文件名（ez240 / jdk25 / es819…），也可能是引擎名（easysearch /
    elasticsearch）——那是"该批默认引擎"的部署记录，对该批所有对应引擎的车生效。
    """
    out = {}
    for p in glob.glob(os.path.join(camp_dir, "logs", "*deploy-*.log")):
        base = os.path.basename(p)
        mm = re.match(r"^(?:ibs-)?deploy-(.+)\.log$", base)
        if not mm:
            continue
        key = mm.group(1)
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = _DEPLOY_RE.search(txt)
        if not m:
            continue
        eng = "elasticsearch" if m.group(1) == "Elasticsearch" else "easysearch"
        out[key] = (eng, m.group(2), m.group(3))
    return out


def parse_jdk_files(camp_dir):
    """campaign/logs/jdk-<车>.txt 里的 java -version → {车: {vendor, major, version}}。

    没有 sidecar 的批次靠它拿 JDK 身份（实测原文，不是从车名猜）。
    version 是**完整**版本号（25.0.4 / 26.0.2.1 / 21.0.9），major 只取第一段。
    """
    out = {}
    for p in glob.glob(os.path.join(camp_dir, "logs", "jdk-*.txt")):
        car = os.path.basename(p)[len("jdk-"):-len(".txt")]
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        vendor = None
        if "TencentKonaJDK" in txt:
            vendor = "TencentKonaJDK"
        elif "GraalVM" in txt:
            vendor = "GraalVM"
        elif "OpenJDK" in txt or "openjdk" in txt:
            vendor = "OpenJDK"
        mm = re.search(r'(?:java version|openjdk version)\s+"([0-9][0-9.]*)', txt)
        version = mm.group(1) if mm else None
        major = version.split(".")[0] if version else None
        if vendor and major:
            out[car] = {"vendor": vendor, "major": major, "version": version}
    return out


def load_campaign_meta(camp_dir):
    """RACES.md 状态、PACKAGES 指纹、sidecar、monitors 摘要"""
    m = {"status": {}, "packages": "", "sidecar": {}, "sidecar_by_race": {},
         "post_merge": {}, "env": {}, "env_id": None}
    m["deploys"] = parse_deploy_logs(camp_dir)
    m["jdks"] = parse_jdk_files(camp_dir)
    rp = os.path.join(camp_dir, "RACES.md")
    if os.path.isfile(rp):
        for line in open(rp, encoding="utf-8", errors="replace"):
            c = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(c) >= 2 and re.match(r"^[a-z0-9]+-", c[0]):
                m["status"][c[0]] = c[1]
    pp = os.path.join(camp_dir, "PACKAGES.txt")
    if os.path.isfile(pp):
        m["packages"] = open(pp, encoding="utf-8", errors="replace").read()
    # 本 campaign 自证的引擎包名（从 PACKAGES.txt 的 sha256+包名行解析）：
    #   没有 sidecar 的历史 campaign（如 ds3-20261015）只能靠它，
    #   否则会套用别的 campaign 实测到的车身份，把 3009 的场次标成 3015。
    for _mm in re.finditer(r"(easysearch|elasticsearch)-[0-9][^\s]*\.tar\.gz", m.get("packages") or ""):
        m.setdefault("pkg_own", {}).setdefault(_mm.group(1), _mm.group(0))
    # 受污染场次清单（每行一个 fid）：给该场 bench-run 文档打 caliber_warnings 标注。
    # 为什么单独存文件而不是写进 RACES.md：RACES.md 的状态列直接决定 valid，
    # 写成 CONTAMINATED 会让 valid=false，看板闸门 valid:true 会把这一场整个滤掉
    # （ds-j2048 只有 jdk25 车有数据，一滤就空面板）。状态保持 VALID，
    # 污染事实走这个清单 + 文档 caliber_warnings 双通道留痕。
    cf = os.path.join(camp_dir, "CONTAMINATED.txt")
    if os.path.isfile(cf):
        m["contaminated"] = [x.strip() for x in open(cf, encoding="utf-8", errors="replace")
                             if x.strip() and not x.strip().startswith("#")]
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
            "foreign_cpu_max": g("foreign_max"), "swapin": g("swapin"),
            # load-monitor.sh 每 5 秒就采到、但摘要里一直只吐 5 个键的其余量 ——
            # 现在补进摘要，这里一并接住（无需新增探针、无需改采样节奏）：
            "cpu_usr_avg": g("cpu_usr_avg"), "cpu_usr_max": g("cpu_usr_max"),
            "cpu_sys_avg": g("cpu_sys_avg"), "cpu_sys_max": g("cpu_sys_max"),
            "bench_cpu_avg": g("bench_cpu_avg"), "bench_cpu_max": g("bench_cpu_max"),
            "disk_rw_mbs_avg": g("disk_rw_mbs_avg"), "disk_rw_mbs_max": g("disk_rw_mbs_max"),
            "net_rx_mbs_avg": g("net_rx_mbs_avg"), "net_tx_mbs_avg": g("net_tx_mbs_avg"),
            "mem_avail_gb_min": g("mem_avail_gb_min"),
            "env_verdict": (re.search(r"ENV-VERDICT: (\S+)", txt).group(1)
                            if re.search(r"ENV-VERDICT: (\S+)", txt) else None)}
    return m


# 写入阶段的 degraded 阈值（2026-10-21 定）
#
# degraded 的原始动机：error_rate=1.0（该 task 全部请求失败）时，
# rally 吐出的时延量的是"失败响应有多快"（实测 nested p99 只有 0.22s），
# 混进延迟曲线会把引擎显得比实际更快 —— 所以这类行不能进图。
#
# 但"error_rate > 0"一刀切过严：实测 GIST 系向量赛道（ds-g1920 / ds-g960）
# 的 index-append error_rate 稳定在 0.0017~0.0023（约 0.2%，
# 100 万条里丢 2000 条），ds3 与本轮复测（2.5.0-3015）都复现 ——
# 是**可复现的血统特性**，不是测量事故。这种量级的 bulk 偶发失败不影响
# 吞吐/时延的量级（rally 按成功处理量计吞吐），检索质量也没塌
# （实测 recall k=10 0.9643 / k=100 0.996），却会让这两个赛道的写入数据
# 整批被 quality 闸门挡在看板外。
#
# 所以：写入阶段 error_rate > 1% 才降级；查询侧维持 > 0 即降级
# ——查询 task 全失败的时延污染才是主要风险，查询侧不放松。
WRITE_DEGRADED_ERROR_RATE = 0.01


def is_write_task(task):
    """写阶段 task（index-*）。与场次级 write_degraded 判定同口径。"""
    return str(task or "").startswith("index")


def task_quality(om, task=None):
    """单个 operation 的可信度。

    ok       = error_rate 在阈值以下，该 task 的时延/吞吐是成功请求的
    degraded = 超过阈值（写阶段 > 1%，查询阶段 > 0；
                rally 记的是 0~1 比例，1.0 = 该 task 全部请求失败）
    unknown  = rally 没给 error_rate
    """
    er = om.get("error_rate")
    if er is None:
        return "unknown"
    try:
        er = float(er)
    except (TypeError, ValueError):
        return "unknown"
    thr = WRITE_DEGRADED_ERROR_RATE if is_write_task(task) else 0.0
    return "degraded" if er > thr else "ok"


def _blk_mean(blk, stats=("mean", "p50", "median", "50_0")):
    """取块里的一个代表值（优先 mean，其次 p50/median/50_0 —— 原始块里是 rally 的
    "50_0" 这种 key，不是归一后的 "p50"）"""
    for s in stats:
        v = blk.get(s)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def latency_semantics(lat_blk, svc_blk, gt_mode):
    """判定一个 task 的 latency 口径。

    rally 的 latency 有两种语义，取决于该 operation 是否设了 target-throughput（开环节流）：
      * 闭环（无 target-throughput）：latency ≡ service_time，量的是单次请求耗时
      * 开环（有 target-throughput）：latency = 从计划发出到响应返回的端到端时间，
        **包含发压机侧的积压延迟**；service_time 才是请求本身的服务端耗时
    实测：nested 赛道 6 个 operation 全带 target-throughput，其中 5 个实际饱和
    （target 20 ops/s×2 clients，achieved 仅 19.86），90 分位 latency 1.477s 而
    service_time 只有 36.7ms —— 差的 40 倍全是发压机积压，不是引擎慢。

    返回 (mode, backlog_ratio, source)：
      mode   ∈ {"open_loop", "closed_loop", "unknown"}
      backlog_ratio = (latency_mean - service_time_mean) / latency_mean ∈ [0,1]
      source ∈ {"groundtruth", "inferred", "unknown"}
    gt_mode 来自 track spec 实参（sidecar.run_params.latency_modes），是地面真值；
    没有真值时按数据推断（推断不可证伪，单独计数上报，不当成实测）。
    """
    lm, sm = _blk_mean(lat_blk), _blk_mean(svc_blk)
    ratio = None
    if lm is not None and sm is not None and lm > 0:
        ratio = max(0.0, min(1.0, (lm - sm) / lm))
    if gt_mode in ("open_loop", "closed_loop"):
        return gt_mode, ratio, "groundtruth"
    if ratio is None:
        return "unknown", None, "unknown"
    return ("open_loop" if ratio > 0.01 else "closed_loop"), ratio, "inferred"


def emit_metrics(d, run_id, ts, common, out, side=None, quality_of=None, counters=None):
    """race.json → bench-metric 行"""
    res = d.get("results") or {}
    gt_modes = ((side or {}).get("run_params") or {}).get("latency_modes") or {}
    for om in res.get("op_metrics") or []:
        task = om.get("task") or om.get("operation") or "?"
        q = quality_of(om) if quality_of is not None else task_quality(om, task)
        lat_blk = om.get("latency") or {}
        svc_blk = om.get("service_time") or {}
        mode, backlog, lsrc = latency_semantics(lat_blk, svc_blk, gt_modes.get(task))
        if counters is not None:
            counters["latency_mode_" + lsrc] = counters.get("latency_mode_" + lsrc, 0) + 1
        row_common = dict(common)
        row_common["quality"] = q
        row_common["latency_mode"] = mode
        if backlog is not None:
            row_common["backlog_ratio"] = backlog
        for key in ("throughput", "latency", "service_time", "processing_time"):
            blk = om.get(key) or {}
            unit = blk.get("unit") or ({"throughput": "ops/s", "latency": "ms",
                                        "service_time": "ms", "processing_time": "ms"}[key])
            family = key
            # 撞名判定必须**按 family 独立**：_id 里带 family，latency 的 p50 与
            # service_time 的 p50 是两行数据，不算撞名。只有同一个块里既有 50_0
            # 又有 median 这种才需要兜底后缀。
            seen_stat = {}
            for k, v in blk.items():
                if k == "unit" or v is None:
                    continue
                stat = STAT_MAP.get(k, k)
                base, n = stat, 1
                while stat in seen_stat:
                    stat = "%s%s%d" % (base, STAT_ALIAS_SUFFIX, n)
                    n += 1
                seen_stat[stat] = True
                nv, nu = norm_value(v, unit)
                out.append({"_index": "bench-metric", "_id": "%s|%s|%s|%s" % (run_id, task, family, stat),
                            "@timestamp": ts, "task": task, "operation": om.get("operation"),
                            "family": family, "metric": family, "stat": stat,
                            "value": nv, "unit": nu, "value_original": v, "unit_original": unit,
                            "direction": direction_of(family), **row_common})
        # ---- 派生：发压机积压 = latency - service_time（开环 task 才会有）----
        lat_u, svc_u = lat_blk.get("unit"), svc_blk.get("unit")
        if lat_u and svc_u and lat_u == svc_u:
            qseen = {}
            for k in sorted(set(lat_blk) & set(svc_blk)):
                if k == "unit":
                    continue
                lv, sv = lat_blk.get(k), svc_blk.get(k)
                if lv is None or sv is None:
                    continue
                try:
                    diff = float(lv) - float(sv)
                except (TypeError, ValueError):
                    continue
                if diff <= 0:
                    continue
                stat = STAT_MAP.get(k, k)
                base, n = stat, 1
                while stat in qseen:
                    stat = "%s%s%d" % (base, STAT_ALIAS_SUFFIX, n)
                    n += 1
                qseen[stat] = True
                nv, nu = norm_value(diff, lat_u)
                out.append({"_index": "bench-metric", "_id": "%s|%s|queue_time|%s" % (run_id, task, stat),
                            "@timestamp": ts, "task": task, "operation": om.get("operation"),
                            "family": "queue_time", "metric": "queue_time", "stat": stat,
                            "value": nv, "unit": nu, "value_original": diff, "unit_original": lat_u,
                            "direction": direction_of("queue_time"), **row_common})
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
                        "direction": direction_of(fam), **row_common})
    # ---- 赛级指标（results 块）----
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
    # ---- 召回率（ground truth 由 campaign 离线算好写进 sidecar）----
    for task, m in ((side or {}).get("recall") or {}).items():
        if not isinstance(m, dict):
            continue
        for label, v in m.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            stat = STAT_MAP.get(label, label)
            out.append({"_index": "bench-metric", "_id": "%s|%s|recall|%s" % (run_id, task, stat),
                        "@timestamp": ts, "task": task, "operation": None,
                        "family": "recall", "metric": "recall", "stat": stat,
                        "value": fv, "unit": "ratio", "value_original": fv, "unit_original": "ratio",
                        "direction": "higher-better", **common})


# ---------------------------------------------------------------------------
# 导出期口径契约断言（2026-10-20 复审新增）
#
# 导出是纯函数、产出可 diff 的载荷快照 —— 架构上完全有能力在写出前发现口径缺陷。
# 上一版没有这一步，于是下面三类问题全部静默进了 ES：
#   1) stat 词表越界：rally 的 99_99 分位原样漏进 stat 列，与 p999 并存
#   2) 失败 task 的时延行与成功 task 混在同一张延迟曲线里（15 场 nested，valid=true）
#   3) 同一 (family, stat) 在不同 task 下量着不同的东西（开环/闭环的 latency）
# 现在这三条是**致命**的：导出直接非零退出，不让脏数据进库。
# 覆盖率与一致性提示是**非致命**的：写进 manifest 供核对，不阻断流水线。
# ---------------------------------------------------------------------------
def validate_payload(runs, metrics):
    """返回 (problems, warnings, coverage)"""
    problems, warnings = [], []
    ids = set()
    stat_count, unit_count, aliases = {}, {}, {}
    for m in metrics:
        i = m.get("_id")
        if i in ids:
            problems.append("重复 _id（后写的会静默覆盖先写的）: %s" % i)
        ids.add(i)
        st = m.get("stat")
        if st not in CANON_STATS:
            if st and _STAT_ALIAS_RE.match(st):
                aliases[st] = aliases.get(st, 0) + 1
            else:
                problems.append("stat 越界（不在词表内）: %r  %s" % (st, i))
        stat_count[st] = stat_count.get(st, 0) + 1
        u = m.get("unit")
        if u not in CANON_UNITS:
            problems.append("unit 越界（不在词表内）: %r  %s" % (u, i))
        unit_count[u] = unit_count.get(u, 0) + 1
        qv = m.get("quality")
        if qv is not None and qv not in CANON_QUALITY:
            problems.append("quality 越界（不在词表内）: %r  %s" % (qv, i))
        lm = m.get("latency_mode")
        if lm is not None and lm not in CANON_LATENCY_MODE:
            problems.append("latency_mode 越界（不在词表内）: %r  %s" % (lm, i))
    if aliases:
        warnings.append("stat 归一后撞名，已用兜底别名区分（%d 行）: %s"
                        % (sum(aliases.values()), sorted(aliases)))
    # 同一 (场次, task) 的所有行 quality 必须一致
    # 注意必须用复合键：task 名在不同场次之间会重名（index-append 每个场次都有），
    # 只按 task 分组会把不同场次的质量标记混在一起比。
    tq = {}
    for m in metrics:
        t = m.get("task")
        if not t:
            continue
        k = (m.get("race_id"), t)
        q = m.get("quality")
        if k in tq and tq[k] != q:
            problems.append("同一 (场次, task) 的行 quality 不一致: %s/%s (%s vs %s)"
                            % (k[0], t, tq[k], q))
        tq.setdefault(k, q)
    # error_rate **超阈值**的 task 整块必须 degraded
    # （写阶段 1% 阈值、查询侧 > 0 —— 与 task_quality 同口径，见 WRITE_DEGRADED_ERROR_RATE）
    for m in metrics:
        if m.get("family") == "error_rate" and (m.get("value") or 0) > 0:
            k = (m.get("race_id"), m.get("task"))
            thr = WRITE_DEGRADED_ERROR_RATE if is_write_task(m.get("task")) else 0.0
            if (m.get("value") or 0) > thr and tq.get(k) != "degraded":
                problems.append("error_rate 超阈值的 (场次, task) 未标 degraded: %s/%s" % k)
    # closed_loop 的 task 不应出现积压（口径与实测矛盾）
    for m in metrics:
        if m.get("family") == "latency" and m.get("latency_mode") == "closed_loop":
            br = m.get("backlog_ratio")
            if br is not None and br > 0.01:
                problems.append("closed_loop 任务出现积压（口径矛盾）: %s br=%.3f"
                                % (m.get("task"), br))
    # 场次级：quality 必须是各 task 的最差
    for r in runs:
        dt = r.get("degraded_tasks") or []
        q = r.get("quality")
        if dt and q != "degraded":
            problems.append("场次有 degraded task 但 quality 不是 degraded: %s" % r.get("race_id"))
        if not dt and q == "degraded":
            problems.append("场次标 degraded 但没有 degraded task: %s" % r.get("race_id"))
    # 覆盖率（非致命）
    n = len(runs) or 1
    def cov(fn):
        return round(100.0 * sum(1 for r in runs if fn(r)) / n, 1)
    cover = {
        "env_id": cov(lambda r: bool(r.get("env_id"))),
        "dims": cov(lambda r: r.get("dims") is not None),
        "segment_form": cov(lambda r: bool(r.get("segment_form"))),
        "codec": cov(lambda r: bool(r.get("codec"))),
        "source_reuse": cov(lambda r: r.get("source_reuse") is not None),
        "sidecar": cov(lambda r: not any(str(w).startswith("no-sidecar")
                                         for w in (r.get("caliber_warnings") or []))),
        "quality_ok": cov(lambda r: r.get("quality") == "ok"),
        "degraded_races": sum(1 for r in runs if r.get("quality") == "degraded"),
    }
    for k, v in cover.items():
        if k != "degraded_races" and v < 50.0:
            warnings.append("字段覆盖率偏低: %s = %.1f%%（过滤条件用该字段会漏掉大半场次）" % (k, v))
    return problems, warnings, {"fields": cover, "stat_histogram": stat_count,
                                "unit_histogram": unit_count, "stat_aliases": aliases}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--races", default="/root/.rally/benchmarks/races")
    ap.add_argument("--campaigns", default="campaigns")
    ap.add_argument("--allow-fail", action="store_true",
                    help="口径契约断言失败也照样写出载荷（仅用于排查，默认失败即非零退出）")
    ap.add_argument("--require-campaign", action="store_true",
                    help="只导有 campaign 元数据（RACES.md 状态 + sidecar）的场次。"
                         "rally race store 里混着大量早期 one-off 场次（无 campaign 目录、"
                         "track 名未归一、多数都叫 track），把它们灌进可视层会造出"
                         "一个上千场次的'杂赛道'看板，且这些行 valid 为空 —— "
                         "默认看板闸门会滤掉，但索引里全是噪音。默认不过滤。")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    metas = {}
    for c in sorted(glob.glob(os.path.join(args.campaigns, "*"))):
        if os.path.isdir(c):
            metas[os.path.basename(c)] = load_campaign_meta(c)

    # 预扫 sidecar：学出 car -> 引擎身份（实测包名/版本/JDK）。
    # 有些场次没有 sidecar（早期 campaign），但同一实验车的包是同一份 ——
    # 用别的场次实测到的身份回填，比留空或按车名猜要准。
    car_identity = {}
    for meta in metas.values():
        seen_side = list(meta.get("sidecar", {}).values()) + list(meta.get("sidecar_by_race", {}).values())
        for s in seen_side:
            car = (s.get("run") or {}).get("car")
            if not car:
                continue
            se = s.get("engine") or {}
            base_pkg = os.path.basename(se.get("pkg") or "")
            eng = ("elasticsearch" if base_pkg.startswith("elasticsearch")
                   else "easysearch" if base_pkg.startswith("easysearch") else None)
            vm = se.get("version_measured_nodes") or []
            jdk_nodes = (s.get("jdk") or {}).get("measured_nodes") or []
            # 只取**实测**厂商（不走车代号兜底）—— 这一份是"同车其他场次实测"的凭据
            _vm = jdk_vm_of(s.get("jdk") or {})
            if eng:
                ident = {
                    "engine": eng, "pkg": se.get("pkg"),
                    "pkg_sha256": se.get("pkg_sha256"),
                    "version": vm[0] if vm else se.get("version_from_pkg"),
                    "build": re.sub(r"-linux.*$", "", se.get("pkg") or "") or None,
                    "jdk_vendor": _vm[0] if _vm else None,
                    "jdk_version": ",".join(sorted({str(x) for x in jdk_nodes})) or None,
                }
                # 车代号在整个测试期对应同一份包与同一个 JDK —— 但先到的那份 sidecar
                # 可能没带 JDK 实测（早期批次）。已有实测身份时不要用后来的空值覆盖。
                if car not in car_identity:
                    car_identity[car] = ident
                else:
                    old = car_identity[car]
                    for _k in ("jdk_vendor", "jdk_version"):
                        if not old.get(_k) and ident.get(_k):
                            old[_k] = ident[_k]

    envs, runs, metrics = {}, [], []
    counters = {}          # 导出期统计（口径来源分布等），进 manifest
    fid_track = {}          # fid → 归一化赛道名；monitors 补的 system 行靠它回填 track
    fid_ts = {}             # fid → 场次时间；system 行靠它回填 @timestamp（画折线的前提）
    fid_valid = {}          # fid → 场次有效性；system 行靠它回填 valid（否则被 KQL 过滤）
    fid_quality = {}        # fid → 场次级 quality；system 行同样要带 quality，
                            #   否则看板上 quality:ok 的闸门会把整个环境健康度段落过滤空
    fid_series_key = {}       # fid → 系列键（引擎@版本@build@JDK）；system 行也要回填，
                            #   否则按 series_key 分线的 system 类面板（外来进程 CPU /
                            #   换入次数…）分不出桶 → 空面板
    seen = set()
    unmatched = 0
    skipped_no_campaign = 0
    for d in load_store(args.races):
        ut = d.get("user-tags") or {}
        camp = ut.get("campaign") or "unknown"
        car, tr, _ = fid_of(d)
        phase = PHASE.get(d.get("challenge"), "wq")
        rnd = ut.get("round")
        fid = "%s-%s-r%s" % (car, tr, rnd)
        meta = metas.get(camp, {})
        race_id = d.get("race-id") or os.path.basename(os.path.dirname(d["_path"]))
        if race_id in seen:
            continue
        if args.require_campaign and camp not in metas:
            skipped_no_campaign += 1
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
            fid = real_fid   # sidecar 的 fid 才是本场唯一身份（含变体名与 -tryN），比 car-track-rN 拼出来的准
        dur = None
        for om in ((d.get("results") or {}).get("op_metrics") or []):
            if om.get("task") == "index-append" and om.get("duration"):
                dur = om["duration"] / 1000.0
        rate_b = (DOCS.get(d.get("track"), 0) / dur) if dur and DOCS.get(d.get("track")) else None
        warn = []
        eng_tag = ut.get("engine") or ""
        for _cf in (meta.get("contaminated") or []):
            if _cf == fid:
                warn.append("受宿主旁路负载污染：该场写赛窗口内落过宿主侧全量导出，吞吐可能偏低，结论以重跑为准")
                break
        if camp.startswith("ds3-") and eng_tag != "es819":
            warn.append("heap 声明不实：user_tags heap:16g 但 ds3 campaign 的 easysearch 车实测 -Xmx1g（2026-10-16 事故），绝对值不可与他批比较")
        if not side:
            warn.append("no-sidecar：JDK/heap/clients/bulk 无实测值，仅有 user_tags 声明")
        if status is None:
            warn.append("no-RACES.md：valid 未知（campaign 目录不在或该场未记账）")

        # ---- 实测身份（方案 §3.2「引擎(实测)」+ §3.2「实验变量 typed promote」）----
        se = side.get("engine") or {}
        sj = side.get("jdk") or {}
        srp = side.get("run_params") or {}
        ident = car_identity.get(car) or {}
        # 身份来源按可信度排队：sidecar 实测 > 部署日志 > revision 指纹 > 批次清单 > 车名兜底。
        # 批次清单（PACKAGES.txt）排最后：它是"这批装了哪些包"，不是"这个车跑的哪个包"。
        deploys = meta.get("deploys") or {}
        deploy = deploys.get(car)                       # 车级部署记录
        rev = (d.get("cluster") or {}).get("revision")   # 引擎源码指纹
        rev_id = REVISION_IDENTITY.get(rev) if rev else None
        pkg = se.get("pkg")                             # 车实际用的包（sidecar 实测），没有就是没有
        base_pkg = os.path.basename(pkg or "")
        engine = ("elasticsearch" if base_pkg.startswith("elasticsearch")
                  else "easysearch" if base_pkg.startswith("easysearch") else None)
        if not engine and rev_id:
            engine = rev_id[0]
        if not engine:
            # 没有 sidecar 的早期 campaign（如 ds3 维度扫描）：rally 记的 distribution-flavor
            # 足以区分产品线 —— easysearch=oss，elasticsearch=default
            flavor = (d.get("cluster") or {}).get("distribution-flavor")
            engine = {"oss": "easysearch", "default": "elasticsearch"}.get(flavor)
        if not engine and deploy:
            engine = deploy[0]
        if not engine:
            engine = ("easysearch" if (car or "").startswith("ez")
                      else "elasticsearch" if (car or "").startswith("es") else None)
        engine = engine or ident.get("engine")
        # 引擎级部署记录（ibs-deploy-easysearch.log / deploy-elasticsearch.log）：
        # 该批所有同引擎的车共用一份部署
        deploy_eng = deploys.get(engine) if engine else None
        vm = se.get("version_measured_nodes") or []
        version = ((vm[0] if vm else None) or se.get("version_from_pkg")
                   or (deploy[1] if deploy else None)
                   or (deploy_eng[1] if deploy_eng else None)
                   or (rev_id[1] if rev_id else None)
                   or ident.get("version")
                   or (d.get("cluster") or {}).get("distribution-version"))
        build = re.sub(r"-linux.*$", "", pkg or "") or None
        # 构建号（3015 / 3009 …）：version 字段只有主次版本（如 2.5.0），无法区分
        # 跨 build 的两批数据。ds3 首轮是 2.5.0-3009、ds2 是 2.5.0-3015，
        # 车标签相同（jdk25/jdk26/jdk27）时看板上会混成一条线，必须能按 build 过滤。
        vfp = se.get("version_from_pkg") or ""
        _mb = re.search(r"-(\d+)-linux", pkg or "")
        build_no = _mb.group(1) if _mb else None
        if not build_no and vfp:
            build_no = vfp.rsplit("-", 1)[-1] if "-" in vfp else None
        if not build_no and deploy:
            build_no = deploy[2]
        if not build_no and rev_id:
            build_no = rev_id[2]
        if not build_no and deploy_eng:
            build_no = deploy_eng[2]
        if not build_no:
            _uv = ut.get("engine_ver") or ""
            build_no = (_uv.rsplit("-", 1)[-1] if "-" in _uv else None) or None
        # ---- JDK 身份：实测优先，逐级降级（identity.py 口径）----
        # ① sidecar 本场实测（vendor_detected + measured_nodes）
        # ② campaign/logs/jdk-<车>.txt 的 java -version 原文
        # ③ 同一辆车的其他场次实测（ident）
        # ④ 车代号兜底表（CAR_FALLBACK）
        # jdk_vendor 与 jdk_version 分开存：GraalVM 25 与 Kona 25 的完整版本号
        # 都是 25.0.4，只有厂商能分 —— 而 JDK 厂商对比正是本项目要回答的问题。
        jdk_file = (meta.get("jdks") or {}).get(car)
        jdk_vm = jdk_vm_of(sj, jdk_file)
        jdk_vendor = jdk_vendor_of(side, car, jdk_vm=jdk_vm, jdk_file=jdk_file)
        jdk_major = jdk_major_of(side, car, jdk_vm=jdk_vm, jdk_file=jdk_file)
        jdk_version = jdk_version_of(side, jdk_file=jdk_file)
        if not jdk_vendor:
            jdk_vendor = ident.get("jdk_vendor")
        if not jdk_version:
            jdk_version = ident.get("jdk_version")
        if not jdk_major:
            jdk_major = (str(jdk_version).split(".")[0] if jdk_version else None)
        # 系列键（看板分线键 + 图例文案）：引擎@版本@build@JDK
        # JDK 段查不到时按车代号写 unk-<车>，避免未实测的车塌成同一条 "?"
        series_key = series_key_of(engine, version, build_no, jdk_vendor, jdk_major, car=car)
        heap_max = srp.get("heap_max_measured_mb") or []
        heap_gb = round(max(heap_max) / 1024, 2) if heap_max else None
        tp = d.get("track-params") or {}
        # 实验变量：维度/血统/段形态 —— 从 user-tags 与 track params 提升为 typed 列（方案 §3.2）
        dims = ut.get("dim") or tp.get("dim")
        lineage = ut.get("lineage")
        segment_form = ut.get("segment_form") or tp.get("segment_form")

        # ---- 场次可信度（2026-10-20 复审新增）----
        # RACES.md 的 status 只反映"这场跑完了没有"，不反映"结果可不可用"。
        # 实测 15 场 nested 的某个 operation error_rate=1.0（该 task 全部请求失败），
        # status 却是 VALID → valid:true → 这些失败 task 的"漂亮延迟"（失败响应很快，
        # p99 只有 0.22s）被混进查询延迟曲线，把引擎显得比实际更快。
        # 这里按 task 逐个判 error_rate，场次级取最差；行级 quality 由 emit_metrics 落。
        op_metrics = (d.get("results") or {}).get("op_metrics") or []
        task_quality_map = {}
        for om in op_metrics:
            t = om.get("task") or om.get("operation") or "?"
            task_quality_map[t] = task_quality(om, t)
        degraded_tasks = sorted(t for t, q in task_quality_map.items() if q == "degraded")
        if degraded_tasks:
            race_quality = "degraded"
        elif status is None:
            race_quality = "unknown"
        else:
            race_quality = "ok"
        # 赛级指标（store_size / segment_count / GC / merge…）量的是**写阶段**的状态，
        # 一个查询 task 失败不该把它们一起降级 —— 否则 15 场 nested 的磁盘与 GC 数据
        # 会因为一个查询 task 挂了而从看板上消失。只有写阶段自己失败才降级。
        write_degraded = any(is_write_task(t) for t in degraded_tasks)
        race_row_quality = "degraded" if write_degraded else "ok"

        # ---- common = 场次级 + 指标行共用的维度 ----
        # 身份维度只在 bench-run 存全套（engine/version/engine_build/jdk_vendor/
        # jdk_version/series_key）；bench-metric 只带 series_key（看板分线的唯一依赖）。
        # 车代号（car/arm/car_build/car_label）不再入库 —— 那是「车代号」的重复编码，
        # 车代号只留在 user_tags.car，身份靠上面六个正交字段表达。
        common = {"race_id": race_id, "campaign": camp,
                  # series_key：看板分线键 + 图例文案（引擎@版本@build@JDK）
                  "series_key": series_key,
                  # 实验变量：这一场测的是什么（向量维度 / codec / 段形态）
                  "dims": dims, "lineage": lineage, "segment_form": segment_form,
                  "codec": codec_of(d, side),
                  "source_reuse": source_reuse_of(d, side),
                  "quality": race_row_quality,
                  "knn_params": knn_params_of(side),
                  # track 必须进 common：bench-metric 的指标行没有它就没法按赛道过滤，
                  # 而按赛道拆看板要求每个指标行都带归一化后的赛道名
                  "track": tr, "track_raw": d.get("track"), "phase": phase, "round": rnd,
                  "challenge": d.get("challenge"), "env_id": side.get("env", {}).get("env_id"),
                  "valid": (status == "VALID") if status else None, "status": status}
        fid_track[fid] = tr
        # fid → 场次时间：monitors 补的 system 行本身没有 @timestamp（它是按场次
        # 采集的摘要，不是时间序列），不回填时间戳的话 CPU steal 这类
        # 数据可信度指标在 Kibana 上画不出折线（x 轴无值）。
        fid_ts.setdefault(fid, ts)
        # fid → 场次有效性：同理，system 行缺 valid 会被 dashboard 的 `valid: true`
        # 查询框整体过滤掉，CPU steal 这类面板就全空。
        fid_valid.setdefault(fid, (status == "VALID") if status else None)
        fid_quality.setdefault(fid, race_row_quality)
        fid_series_key.setdefault(fid, series_key)
        runs.append({"_index": "bench-run", "_id": race_id, "race_id": race_id, "@timestamp": ts,
                     "campaign": camp, "fid": fid,
                     # ---- 身份维度（正交，唯一事实源在本索引）----
                     "engine": engine,
                     "version": str(version) if version is not None else None, "build": build,
                     "engine_build": build_no,
                     "jdk_vendor": jdk_vendor, "jdk_version": jdk_version,
                     # series_key：看板分线键 + 图例文案（引擎@版本@build@JDK）
                     "series_key": series_key,
                     "heap_gb": heap_gb,
                     "dims": dims, "lineage": lineage, "segment_form": segment_form,
                     "track": tr, "track_raw": d.get("track"), "challenge": d.get("challenge"),
                     "phase": phase, "round": rnd, "nnodes": ut.get("nnodes"),
                     "cores_tag": ut.get("cores"), "heap_tag": ut.get("heap"),
                     "pipeline": d.get("pipeline"), "rally_version": d.get("rally-version"),
                     "environment": d.get("environment"), "user_tags": ut,
                     "track_params": d.get("track-params"),
                     # rally_car = Rally 自己的 car（客户端插件列表，如 ["external"]）——
                     # 跟本项目的「车」（一套被测配置）不是一回事，单独存。
                     "rally_car": d.get("car"),
                     "cluster": d.get("cluster"),
                     "version_from_pkg": (side.get("engine") or {}).get("version_from_pkg"),
                     "pkg": pkg,
                     # 该批次的默认包（PACKAGES.txt 清单）—— 只是"这批装了哪些包"，
                     # 不等于本车跑的包（ez240 车在 jdk3-20261016-* 跑的是 2.4.0-2975，
                     # 那批清单里写的却是默认包 2.5.0-3015）。
                     "pkg_inventory": (meta.get("pkg_own") or {}).get(engine) or None,
                     "pkg_sha256": (side.get("engine") or {}).get("pkg_sha256") or ident.get("pkg_sha256"),
                     "jdk_want": (side.get("jdk") or {}).get("want"),
                     "jdk_measured": (side.get("jdk") or {}).get("measured_nodes"),
                     "jdk_match_want": (side.get("jdk") or {}).get("match_want"),
                     "heap_declared": (side.get("run_params") or {}).get("heap_declared"),
                     "heap_max_measured_mb": (side.get("run_params") or {}).get("heap_max_measured_mb"),
                     "cores_declared": (side.get("run_params") or {}).get("cores_declared"),
                     "available_processors_measured": (side.get("run_params") or {}).get("available_processors_measured"),
                     "index_writer_memory_measured_mb": (side.get("run_params") or {}).get("index_writer_memory_measured_mb"),
                     "version_map_memory_measured_mb": (side.get("run_params") or {}).get("version_map_memory_measured_mb"),
                     "fixed_bit_set_memory_measured_mb": (side.get("run_params") or {}).get("fixed_bit_set_memory_measured_mb"),
                     "clients": (side.get("run_params") or {}).get("clients"),
                     "bulk_size": (side.get("run_params") or {}).get("bulk_size"),
                     "shards": (side.get("run_params") or {}).get("shards"),
                     "replicas": (side.get("run_params") or {}).get("replicas"),
                     "corpus": side.get("corpus") or ({"docs": DOCS.get(d.get("track"))} if DOCS.get(d.get("track")) else None),
                     "env_id": (side.get("env") or {}).get("env_id"),
                     "codec": codec_of(d, side),
                     "source_reuse": source_reuse_of(d, side),
                     "store_size_gb_post_merge": (meta.get("post_merge", {}).get(real_fid or fid) or {}).get("store_gb"),
                     "segments_post_merge": (meta.get("post_merge", {}).get(real_fid or fid) or {}).get("segments"),
                     "write_task_duration_s": dur, "throughput_docs_per_s_whole": rate_b,
                     "status": status, "valid": (status == "VALID") if status else None,
                     "quality": race_quality, "degraded_tasks": degraded_tasks,
                     "knn_params": knn_params_of(side),
                     "caliber_warnings": warn,
                     "source": [d["_path"], "campaign:" + camp] + (["sidecar:" + fid + "-" + phase] if side else []),
                     "fingerprint": hashlib.sha1(("%s|%s|%s" % (race_id, status, d["_path"])).encode()).hexdigest()})
        emit_metrics(d, race_id, ts, common, metrics, side=side, counters=counters)
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
                                    "valid": fid_valid.get(fid),
                                    "series_key": fid_series_key.get(fid),
                                    "quality": fid_quality.get(fid) or "ok",
                                    "phase": ph, "track": fid_track.get(fid),
                                    "family": "system", "metric": mk, "stat": "value",
                                    "value": None, "value_text": mv, "unit": "text",
                                    "value_original": None, "direction": "lower-better"})
                    continue
                sysrows += 1
                metrics.append({"_index": "bench-metric", "_id": "env|%s|%s|%s" % (camp, key, mk),
                                "@timestamp": fid_ts.get(fid), "campaign": camp, "fid": fid, "node": node,
                                "valid": fid_valid.get(fid),
                                "series_key": fid_series_key.get(fid),
                                "quality": fid_quality.get(fid) or "ok",
                                "phase": ph, "track": fid_track.get(fid),
                                "family": "system", "metric": mk, "stat": "value",
                                "value": (mv / 100.0 if unit_of(mk) == "ratio" else mv),
                                "unit": unit_of(mk),
                                "value_original": mv, "direction": direction_of_system(mk)})

    # ---- 口径契约断言：先验后写，脏载荷不进库 ----
    problems, warnings, cover = validate_payload(runs, metrics)
    if problems:
        print("!! 口径契约断言失败（%d 项）—— 载荷未写出：" % len(problems))
        for p in problems[:30]:
            print("   - %s" % p)
        if len(problems) > 30:
            print("   ... 其余 %d 项见 manifest" % (len(problems) - 30))
        if not args.allow_fail:
            with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"generated_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "assertions": {"status": "fail", "problems": problems, "warnings": warnings},
                           "coverage": cover}, f, ensure_ascii=False, indent=1)
            return 2
    for w in warnings:
        print("   提示: %s" % w)

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
                       # ---- 身份维度（正交，2026-10-21 修订）----
                       "engine": {"type": "keyword"}, "version": {"type": "keyword"},
                       "engine_build": {"type": "keyword"},
                       # jdk_vendor：JDK 厂商（GraalVM/TencentKonaJDK/OpenJDK）——
                       #   必须单列：GraalVM 25 与 Kona 25 的完整版本号都是 25.0.4，
                       #   只有厂商能分，而 JDK 厂商对比正是本项目的核心测试目标。
                       "jdk_vendor": {"type": "keyword"},
                       # jdk_version：JDK 完整版本号（25.0.4 / 26.0.2.1），只认实测
                       "jdk_version": {"type": "keyword"},
                       # series_key：看板分线键 + 图例文案（引擎@版本@build@JDK）
                       "series_key": {"type": "keyword"},
                       # rally_car：Rally 自己的 car（客户端插件列表，如 ["external"]），
                       # 与本项目的「车」（一套被测配置）不是一回事，单独一个字段
                       "rally_car": {"type": "keyword"},
                       "build": {"type": "keyword"},
                       "env_id": {"type": "keyword"},
                       # 存储设置的两个独立维度：压缩 与 source_reuse
                       "codec": {"type": "keyword"}, "source_reuse": {"type": "keyword"},
                       "track": {"type": "keyword"}, "campaign": {"type": "keyword"},
                       "family": {"type": "keyword"}, "metric": {"type": "keyword"},
                       "stat": {"type": "keyword"}, "task": {"type": "keyword"},
                       "unit": {"type": "keyword"}, "valid": {"type": "boolean"},
                       # ---- 口径契约（2026-10-20 复审新增）----
                       # quality: 这行数据可不可信（ok / degraded / unknown）
                       #   degraded = 该 task 的 error_rate 超阈值（写阶段 > 1%，查询侧 > 0），
                       #   其时延量的是"失败响应有多快"，混进延迟曲线会把引擎显得更快
                       #   —— 看板上必须按 quality 过滤。
                       # latency_mode: latency 的口径（open_loop 含发压机积压 / closed_loop / unknown）
                       # backlog_ratio: (latency_mean - service_time_mean)/latency_mean，发压机积压占比
                       "quality": {"type": "keyword"},
                       "latency_mode": {"type": "keyword"},
                       "backlog_ratio": {"type": "double"},
                       "degraded_tasks": {"type": "keyword"},
                       "knn_params": {"type": "flattened"},
                       "corpus": {"type": "object", "properties": {
                           "name": {"type": "keyword"}, "docs": {"type": "long"},
                           "spec_hash": {"type": "keyword"}}},
                       "disk_type": {"type": "keyword"}, "disk_path": {"type": "keyword"}}}}}
    templates = {idx: json.loads(json.dumps(TPL).replace("%s", idx))
                 for idx in ("bench-env", "bench-run", "bench-metric")}
    with open(os.path.join(args.out, "templates.json"), "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=1)

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "races_scanned": len(seen), "bench_run": len(runs), "bench_metric": len(metrics),
                   "bench_env": len(envs), "system_rows": sysrows,
                   "campaigns_with_dir": sorted(metas), "races_without_campaign_dir": unmatched,
                   "skipped_no_campaign": skipped_no_campaign,
                   "require_campaign": bool(args.require_campaign),
                   "valid_true": sum(1 for r in runs if r.get("valid") is True),
                   "valid_null": sum(1 for r in runs if r.get("valid") is None),
                   "assertions": {"status": "pass" if not problems else "fail",
                                  "problems": problems, "warnings": warnings},
                   "coverage": cover,
                   "latency_mode_source": {k: v for k, v in (counters or {}).items()
                                           if k.startswith("latency_mode_")}},
                  f, ensure_ascii=False, indent=1)
    shutil.copy2(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench-apply.py"),
                 os.path.join(args.out, "bench-apply.py"))
    print("已导出 → %s" % args.out)
    print(json.dumps(json.load(open(os.path.join(args.out, "manifest.json"))), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
