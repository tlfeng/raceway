"""本地结果选择：只接受精确记账、完整且环境干净的单次测量。"""
import glob
import json
import math
import os
import re
import sys

import identity

FID_RE = re.compile(r"^(?P<base>.+)-r(?P<round>[1-9][0-9]*)(?:-try(?P<attempt>[1-9][0-9]*))?$")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        value = json.load(f, parse_constant=lambda v: (_ for _ in ()).throw(ValueError("非法数值: " + v)))
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象: %s" % path)
    def check(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON 含非有限数值: %s" % path)
        if isinstance(item, dict):
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    return value


def valid_fids(camp):
    """无判定表与零个 VALID 都不放行；冲突判定不得挑其中成功的一条。"""
    states = {}
    path = os.path.join(camp, "RACES.md")
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.strip().strip("|").split("|")]
            if line.lstrip().startswith("|") and len(parts) >= 2 and FID_RE.fullmatch(parts[0]):
                states.setdefault(parts[0], set()).add(parts[1])
    return {fid for fid, statuses in states.items() if statuses == {"VALID"}}


def monitor_summary(camp, fid, phases=None, expected_nodes=None):
    """按实际 fid/phase 匹配节点，返回最坏摘要；缺节点、缺指标也视为无数据。"""
    phases = phases or ("wq",)
    out = {"samples": 0, "steal_avg": 0.0, "steal_max": 0.0,
           "foreign_max": 0.0, "swapin": 0.0, "verdict": "OK"}
    severity = {"OK": 0, "WARN": 1, "CONTAMINATED": 2, "NO-DATA": 3}
    for phase in phases:
        prefix = "%s-%s-" % (fid, phase)
        nodes = set()
        paths = glob.glob(os.path.join(camp, "monitors", glob.escape(prefix) + "*-summary.txt"))
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            node = os.path.basename(path)[len(prefix):-len("-summary.txt")]
            values = dict(re.findall(r"\b([A-Za-z_]+)=([^\s%]+)", text))
            node = values.get("node") or node
            verdicts = re.findall(r"ENV-VERDICT:\s*(\S+)", text)
            verdict = verdicts[-1] if len(verdicts) == 1 else "NO-DATA"
            try:
                numbers = {k: float(values[k]) for k in ("samples", "steal_avg", "steal_max", "foreign_max", "swapin")}
                if not all(finite(v) and v >= 0 for v in numbers.values()) or numbers["samples"] <= 0:
                    raise ValueError("无有效监控采样")
                if numbers["samples"] != int(numbers["samples"]):
                    raise ValueError("非法采样数")
            except (KeyError, ValueError):
                verdict = "NO-DATA"
                numbers = {}
            if numbers.get("swapin", 0) > 0 and verdict == "OK":
                verdict = "WARN"
            if node in nodes or not node:
                verdict = "NO-DATA"
            nodes.add(node)
            out["samples"] += int(numbers.get("samples", 0))
            for k in ("steal_avg", "steal_max", "foreign_max"):
                out[k] = max(out[k], numbers.get(k, 0))
            out["swapin"] += numbers.get("swapin", 0)
            if severity.get(verdict, 3) > severity.get(out["verdict"], 3):
                out["verdict"] = verdict if verdict in severity else "NO-DATA"
        if not paths or expected_nodes is None or len(nodes) != expected_nodes:
            out["verdict"] = "NO-DATA"
    return out


def _identity(side, race):
    engine = side.get("engine") or {}
    pkg = os.path.basename(engine.get("pkg") or "")
    m = re.match(r"^(easysearch|elasticsearch|opensearch)-([0-9]+\.[0-9]+\.[0-9]+)(?:-([0-9]+))?-", pkg)
    if not m:
        raise ValueError("缺少明确的引擎安装包身份")
    name, version, build = m.groups()
    versions = engine.get("version_measured_nodes") or []
    if not versions or set(versions) != {version}:
        raise ValueError("引擎实测版本缺失或与包名不一致")
    nodes = (side.get("jdk") or {}).get("per_node") or []
    jdks = {(n.get("vendor_detected"), n.get("major_measured")) for n in nodes}
    if len(jdks) != 1 or not all(next(iter(jdks))):
        raise ValueError("JDK 实测身份缺失或节点不一致")
    vendor, jdk_version = next(iter(jdks))
    rp = side.get("run_params") or {}
    knn = rp.get("knn_params") or {}
    index_type = knn.get("index_type")
    if index_type and "{{" in str(index_type):
        raise ValueError("向量索引类型仍为未渲染模板")
    settings = rp.get("index_settings_measured") or {}
    storage = []
    for index, st in sorted(settings.items()):
        if isinstance(st, dict):
            storage.append((index, st.get("index.codec", "default"), str(st.get("index.source_reuse", "false")).lower()))
    budgets = knn.get("search_budget_by_task") or {}
    budget_signature = json.dumps(budgets, sort_keys=True, allow_nan=False)
    tags = race.get("user-tags") or {}
    series = identity.series_key_of(name, version, build, vendor, str(jdk_version).split(".")[0], car=tags.get("car"))
    signature = (name, version, build, vendor, jdk_version, index_type, tuple(storage), budget_signature,
                 tuple((key, json.dumps(knn.get(key), sort_keys=True, allow_nan=False))
                       for key in ("dims", "element_type", "m", "ef_construction", "engine", "encoder", "mode", "compression_level")),
                 rp.get("heap_declared_mb"), rp.get("cores_declared"), rp.get("shards"), rp.get("replicas"))
    return series, signature


def _measurement(camp, fid, paths):
    m = FID_RE.fullmatch(fid)
    phases = set(paths)
    if not m or phases not in ({"wq"}, {"w", "q"}):
        raise ValueError("测量不完整，要求 full 或配对写/查段")
    races, sides, identities = {}, {}, []
    expected = None
    tags0 = None
    for phase, path in paths.items():
        race = read_json(path)
        tags = race.get("user-tags") or {}
        if not tags.get("car") or not tags.get("track") or str(tags.get("round")) != m.group("round"):
            raise ValueError("车、赛道或轮次元数据缺失/错位")
        car, variant = tags["car"], tags["track"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", car) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", variant):
            raise ValueError("非法车或赛道身份")
        base_fid = "%s-%s-r%s" % (car, variant, tags["round"])
        if re.sub(r"-try[0-9]+$", "", fid) != base_fid:
            raise ValueError("文件名与场次身份不一致")
        if tags0 and any(tags.get(k) != tags0.get(k) for k in ("campaign", "car", "track", "round", "nnodes")):
            raise ValueError("写查段元数据不一致")
        tags0 = tags
        try:
            count = int(tags["nnodes"])
            if count <= 0:
                raise ValueError("节点数必须为正")
        except (KeyError, TypeError, ValueError):
            raise ValueError("缺少可靠的 nnodes") from None
        expected = count
        side = read_json(path[:-len("-race.json")] + "-sidecar.json")
        run = side.get("run") or {}
        if run.get("fid") != fid or run.get("phase") != phase or run.get("race_id") != race.get("race-id"):
            raise ValueError("sidecar 与实际 race 不一致")
        if phase == "wq" and race.get("challenge") != "full":
            raise ValueError("单段测量必须是 full challenge")
        if phase in ("w", "q") and race.get("challenge") != {"w": "write-only", "q": "query-only"}[phase]:
            raise ValueError("拆分相位 challenge 不一致")
        ops = (race.get("results") or {}).get("op_metrics") or []
        if not ops:
            raise ValueError("无任务测量")
        bulk = [op for op in ops if (op.get("throughput") or {}).get("unit") == "docs/s"]
        queries = [op for op in ops if not str(op.get("task") or "").startswith(
            ("index-append", "index-update", "upsert-", "refresh", "force-merge", "wait-", "delete-", "create-", "check-cluster"))]
        if phase in ("w", "wq") and not bulk:
            raise ValueError("缺少真实写入任务")
        if phase in ("q", "wq") and not queries:
            raise ValueError("缺少真实查询或末尾统计任务")
        for op in bulk + queries:
            throughput = (op.get("throughput") or {}).get("median")
            if not finite(throughput) or throughput <= 0:
                raise ValueError("必需任务没有有效吞吐")
        for op in ops:
            error = op.get("error_rate")
            if not finite(error) or error != 0:
                raise ValueError("任务错误率非零或缺失")
        races[phase], sides[phase] = race, side
        identities.append(_identity(side, race))
    if any(i != identities[0] for i in identities[1:]):
        raise ValueError("写查段实测身份或实验条件不一致")
    env = monitor_summary(camp, fid, sorted(phases), expected)
    if env["verdict"] != "OK":
        raise ValueError("监控缺失、不完整或受污染: %s" % env["verdict"])
    series, signature = identities[0]
    return {"fid": fid, "base_fid": re.sub(r"-try[0-9]+$", "", fid),
            "car": tags0["car"], "track": tags0["track"], "round": int(m.group("round")),
            "attempt": int(m.group("attempt") or 1), "camp": os.path.basename(os.path.normpath(camp)),
            "camp_dir": camp, "series_key": series, "signature": signature,
            "races": races, "sides": sides, "paths": paths, "env": env}


def select_measurements(camp, warn=None):
    """每个逻辑场次只取最新有效尝试，不把重试计作额外轮次。"""
    warn = warn or (lambda message: print("跳过: " + message, file=sys.stderr))
    keep = valid_fids(camp)
    if not keep:
        warn("%s 没有精确 VALID 场次" % camp)
        return []
    contaminated = set()
    path = os.path.join(camp, "CONTAMINATED.txt")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            contaminated = {line.strip().split()[0] for line in f if line.strip() and not line.lstrip().startswith("#")}
    groups = {}
    for path in sorted(glob.glob(os.path.join(camp, "results", "*-race.json"))):
        name = os.path.basename(path)
        match = re.fullmatch(r"(.+)-(wq|w|q)-race\.json", name)
        if match:
            groups.setdefault(match[1], {})[match[2]] = path
    selected = {}
    for fid, paths in groups.items():
        if fid not in keep or fid in contaminated:
            continue
        try:
            row = _measurement(camp, fid, paths)
        except (ValueError, OSError, TypeError) as e:
            warn("%s/%s: %s" % (camp, fid, e))
            continue
        prev = selected.get(row["base_fid"])
        if prev is None or row["attempt"] > prev["attempt"]:
            selected[row["base_fid"]] = row
    return sorted(selected.values(), key=lambda r: (r["track"], r["round"], r["car"]))


def metrics_of(race):
    """Rally 机读指标投影成对比工具的 (指标名, task) 格，不解析展示用 markdown。"""
    out = {}
    for op in (race.get("results") or {}).get("op_metrics") or []:
        task = op.get("task")
        if not task:
            continue
        tp = op.get("throughput") or {}
        for key, label in (("median", "Median Throughput"), ("max", "Max Throughput")):
            if finite(tp.get(key)):
                out[(label, task)] = tp[key]
        st = op.get("service_time") or {}
        factor = {"ms": 1, "s": 1000}.get(st.get("unit", "ms"))
        for stat, label in (("50_0", "50th"), ("90_0", "90th"), ("99_0", "99th"), ("100_0", "100th")):
            if finite(st.get(stat)) and factor is not None:
                out[(label + " percentile service time", task)] = st[stat] * factor
    return out
