#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-apply.py —— 把 bench-export.py 的产物灌进任意 ES/Easysearch 集群（纯标准库，无需 client）

  python3 bench-apply.py --dir exports/<tag> \
      --es https://127.0.0.1:9200 --auth admin:CHANGE_ME [--insecure] [--only benchmark-result-race,benchmark-result-metric]
  python3 bench-apply.py --dir exports/<tag> --es http://localhost:9200        # 无鉴权集群
  python3 bench-apply.py --dir exports/<tag> --es http://localhost:9200 --drop # 先删索引再重建

幂等：每条文档带 _id（race_id / race_id|task|family|stat），重复执行是覆盖不是翻倍。
只做三件事：① 用 templates.json 建三张索引模板；② 按 jsonl 逐行走 _bulk；③ refresh。
--drop 会先删掉三张索引再重建 —— mapping 变更（如新增 typed 字段）必须走这条路，
旧索引的 mapping 不会因为换了 template 而自动更新。

载荷快照原样入 ES/EasySearch，不因目标集群差异改写数据内容 —— 唯一的例外是
flattened 字段的显式 null（见 flatten_fields），那是被目标集群的 mapper 逼出来的
必要处理，对 ES 语义等价。

退出码：全成功 → 0；bulk 有任何一条 per-item 被拒收 → 1（理由见 flush）。
refresh 失败只算警告，不改退出码 —— 它不影响数据正确性，只是搜索可见性延迟。
"""
import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import math
import datetime
import re

IDX = ("benchmark-result-env", "benchmark-result-race", "benchmark-result-metric")


def flatten_fields(template):
    """从 index template 里取出映射为 flattened 的字段名。

    EasySearch 的 flattened 不认 null_value，且显式 null 会报
    "tried to parse field [x] as object, but got EOF"（ES 的 parse 对 null 早返回）
    → 值为 null 时只能把字段整个省掉（对 ES 语义等价）。
    """
    props = ((template or {}).get("template", {}).get("mappings", {}) or {}).get("properties", {}) or {}
    return {name for name, spec in props.items()
            if isinstance(spec, dict) and spec.get("type") == "flattened"}


def drop_null_flattened(doc, names):
    """删掉值为 null 的 flattened 字段（原地修改 doc，返回删掉的字段数）。"""
    dropped = 0
    for name in names:
        if name in doc and doc[name] is None:
            del doc[name]
            dropped += 1
    return dropped


def post(url, body, auth, insecure, ctype="application/x-ndjson", method="POST"):
    req = urllib.request.Request(url, data=body.encode("utf-8") if isinstance(body, str) else body,
                                 method=method, headers={"Content-Type": ctype})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def finite_tree(value, path):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("%s 含非有限数字" % path)
    if isinstance(value, dict):
        for key, item in value.items():
            finite_tree(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            finite_tree(item, "%s[%d]" % (path, i))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象重复字段: %s" % key)
        result[key] = value
    return result


def read_json(text, path):
    value = json.loads(text, object_pairs_hook=unique_object)
    finite_tree(value, path)
    if not isinstance(value, dict):
        raise ValueError("%s 必须是 JSON 对象" % path)
    return value


def validate_field(value, spec, path):
    """预检当前三索引的显式 mapping；不依靠 ES 在删库后发现类型错误。"""
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            validate_field(item, spec, path)
        return
    kind = spec.get("type")
    if kind in ("keyword", "text"):
        valid = isinstance(value, str)
    elif kind == "boolean":
        valid = isinstance(value, bool)
    elif kind in ("integer", "long", "short", "byte"):
        limits = {"integer": 2**31, "long": 2**63, "short": 2**15, "byte": 2**7}
        valid = isinstance(value, int) and not isinstance(value, bool) and -limits[kind] <= value < limits[kind]
    elif kind in ("double", "float", "half_float", "scaled_float"):
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    elif kind == "date":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(value, str):
            try:
                dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                valid = dt.tzinfo is not None
            except ValueError:
                valid = False
    elif kind in ("flattened", "object") or "properties" in spec:
        valid = isinstance(value, dict)
        if valid and "properties" in spec:
            validate_document(value, spec, path)
    else:
        raise ValueError("%s 不支持的 mapping 类型: %r" % (path, kind))
    if not valid:
        raise ValueError("%s 与 mapping %s 类型不符: %r" % (path, kind, value))


def validate_document(doc, mappings, path):
    props = mappings.get("properties")
    if not isinstance(props, dict):
        raise ValueError("%s 缺少 properties" % path)
    for key, value in doc.items():
        if key not in props:
            raise ValueError("%s 出现模板外字段: %s" % (path, key))
        validate_field(value, props[key], path + "." + key)


def validate_template(template, idx):
    if not isinstance(template, dict) or template.get("index_patterns") != [idx]:
        raise ValueError("%s 模板必须精确匹配所选索引" % idx)
    if not isinstance(template.get("template"), dict):
        raise ValueError("%s template 必须为对象" % idx)
    settings = template["template"].get("settings", {})
    if not isinstance(settings, dict) or set(settings) - {"number_of_shards", "number_of_replicas"}:
        raise ValueError("%s settings 只允许显式分片/副本整数" % idx)
    for name, value in settings.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < (1 if name == "number_of_shards" else 0):
            raise ValueError("%s settings.%s 无效" % (idx, name))
    mappings = template["template"].get("mappings")
    if not isinstance(mappings, dict) or mappings.get("dynamic") != "strict":
        raise ValueError("%s 模板必须声明 dynamic:strict" % idx)
    if set(template) - {"index_patterns", "template"} or set(template["template"]) - {"mappings", "settings"}:
        raise ValueError("%s 模板包含非本契约定义的设置" % idx)
    if set(mappings) - {"dynamic", "properties"}:
        raise ValueError("%s mappings 包含非本契约定义的设置" % idx)
    props = mappings.get("properties")
    if not isinstance(props, dict) or not props:
        raise ValueError("%s 模板 properties 为空" % idx)
    supported = {"keyword", "text", "boolean", "integer", "long", "short", "byte", "double", "float",
                 "half_float", "scaled_float", "date", "flattened", "object"}
    def check(fields):
        for key, spec in fields.items():
            if not isinstance(spec, dict) or spec.get("type", "object" if "properties" in spec else "") not in supported:
                raise ValueError("%s.%s 模板字段定义非法" % (idx, key))
            kind = spec.get("type", "object")
            if set(spec) - {"type", "properties", "format"} or ("format" in spec and kind != "date"):
                raise ValueError("%s.%s 模板含未知字段选项" % (idx, key))
            if "format" in spec and spec["format"] != "strict_date_optional_time||epoch_millis":
                raise ValueError("%s.%s date format 不符合导出契约" % (idx, key))
            if "properties" in spec:
                if not isinstance(spec["properties"], dict):
                    raise ValueError("%s.%s properties 非对象" % (idx, key))
                check(spec["properties"])
    check(props)
    return mappings


def preflight(directory, want):
    """删除前全量校验并保留内存快照；后续发送不再重读可变文件。"""
    if not os.path.isdir(directory):
        raise ValueError("输入目录不存在: %s" % directory)
    tp = os.path.join(directory, "templates.json")
    with open(tp, encoding="utf-8") as f:
        templates = read_json(f.read(), tp)
    unknown = set(templates) - set(IDX)
    if unknown:
        raise ValueError("模板含未知索引: %s" % sorted(unknown))
    manifest = None
    manifest_path = os.path.join(directory, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = read_json(f.read(), manifest_path)
        if (manifest.get("assertions") or {}).get("status") != "pass":
            raise ValueError("manifest 的导出断言未通过")
    snapshots = {}
    for idx in want:
        if idx not in templates:
            raise ValueError("缺少所选索引模板: %s" % idx)
        mappings = validate_template(templates[idx], idx)
        path = os.path.join(directory, idx + ".jsonl")
        docs, seen = [], set()
        with open(path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                label = "%s:%d" % (path, line_num)
                doc = read_json(line, label)
                rid = doc.pop("_id", None)
                if not isinstance(rid, str) or not rid.strip() or len(rid.encode("utf-8")) > 512:
                    raise ValueError("%s _id 非法" % label)
                if rid in seen:
                    raise ValueError("%s 重复 _id: %s" % (label, rid))
                seen.add(rid)
                if doc.pop("_index", None) != idx:
                    raise ValueError("%s _index 与所选文件不符" % label)
                if idx == "benchmark-result-race" and doc.get("race_id") != rid:
                    raise ValueError("%s race_id 与 _id 不符" % label)
                if idx == "benchmark-result-env" and doc.get("env_id") != rid:
                    raise ValueError("%s env_id 与 _id 不符" % label)
                if idx != "benchmark-result-env":
                    if not isinstance(doc.get("race_id"), str) or not doc["race_id"]:
                        raise ValueError("%s 缺少 race_id" % label)
                    if doc.get("comparison_key") != "%s|%s" % (doc.get("series_key"), doc.get("track_variant")) or not doc.get("track_variant") or not doc.get("series_key"):
                        raise ValueError("%s comparison_key 与身份/赛道不符" % label)
                if doc.get("family") == "error_rate":
                    value = doc.get("value")
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                        raise ValueError("%s error_rate 非法" % label)
                validate_document(doc, mappings, label)
                drop_null_flattened(doc, flatten_fields(templates[idx]))
                docs.append((rid, doc))
        snapshots[idx] = docs
        if manifest is not None:
            count_key = {"benchmark-result-env": "bench_env", "benchmark-result-race": "bench_run",
                         "benchmark-result-metric": "bench_metric"}[idx]
            count = manifest.get(count_key)
            if isinstance(count, bool) or not isinstance(count, int) or count != len(docs):
                raise ValueError("%s 行数与 manifest.%s 不符: %s / %r" % (idx, count_key, len(docs), count))
    if not any(snapshots.values()):
        raise ValueError("所选索引载荷总计 0 条，拒绝空输入/删除")
    if "benchmark-result-race" in snapshots and "benchmark-result-metric" in snapshots:
        if not snapshots["benchmark-result-race"] or not snapshots["benchmark-result-metric"]:
            raise ValueError("同时选择 race/metric 时两者必须都有载荷，拒绝清空半份输入")
        ids = {rid for rid, _doc in snapshots["benchmark-result-race"]}
        if any(doc["race_id"] not in ids for _rid, doc in snapshots["benchmark-result-metric"]):
            raise ValueError("metric 引用的 race_id 不在所选 race 载荷中")
    return {idx: templates[idx] for idx in want}, snapshots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--es", required=True)
    ap.add_argument("--auth", default=None, help="user:pass；无鉴权集群留空")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--only", default=",".join(IDX))
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--drop", action="store_true", help="先删除三张索引（mapping 变更时必需）")
    args = ap.parse_args()

    want = [i.strip() for i in args.only.split(",")]
    if args.batch <= 0 or not want or any(i not in IDX for i in want) or len(set(want)) != len(want):
        print("参数错误：--only 必须是不重复的已知索引，--batch 必须大于 0", file=sys.stderr)
        return 2
    if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", args.es):
        print("参数错误：--es 必须是 HTTP(S) URL", file=sys.stderr)
        return 2
    args.es = args.es.rstrip("/")
    try:
        templates, snapshots = preflight(args.dir, want)
    except (OSError, ValueError, TypeError) as exc:
        print("入库预检失败（未发送任何请求）: %s" % exc, file=sys.stderr)
        return 2
    print("预检通过: %s" % {idx: len(snapshots[idx]) for idx in want})
    if args.drop:
        print("删除所选索引: %s" % ", ".join(want))
        for idx in want:
            try:
                response = post("%s/%s" % (args.es, idx), b"", args.auth, args.insecure, "application/json", "DELETE")
                if not isinstance(response, dict) or response.get("acknowledged") is not True:
                    raise ValueError("DELETE 未确认成功: %r" % response)
                print("已删除索引 %s" % idx)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    print("索引 %s 不存在，跳过删除" % idx)
                else:
                    print("删除 %s 失败，终止: %s" % (idx, exc), file=sys.stderr)
                    return 1
            except (OSError, ValueError) as exc:
                print("删除 %s 失败，终止: %s" % (idx, exc), file=sys.stderr)
                return 1
    for idx in want:
        response = post("%s/_index_template/%s" % (args.es, idx),
                        json.dumps(templates[idx], allow_nan=False), args.auth, args.insecure,
                        "application/json", "PUT")
        if not isinstance(response, dict) or response.get("acknowledged") is not True:
            print("模板 %s 未确认成功，终止" % idx, file=sys.stderr)
            return 1
        print("模板 %s → %s" % (idx, response))

    total = {}
    samples_by_idx = {}
    for idx in want:
        sent = errs = 0
        samples = []
        for offset in range(0, len(snapshots[idx]), args.batch):
            chunk = snapshots[idx][offset:offset + args.batch]
            buf = []
            for rid, doc in chunk:
                buf.append(json.dumps({"index": {"_index": idx, "_id": rid}}, allow_nan=False))
                buf.append(json.dumps(doc, ensure_ascii=False, allow_nan=False))
            bad, smp = flush(args, buf, sent)
            errs += bad
            samples.extend(smp[:max(0, 3 - len(samples))])
            sent += len(chunk)
        total[idx] = {"sent": sent, "errors": errs}
        samples_by_idx[idx] = samples
        print("%s: 提交 %d 条，失败 %d" % (idx, sent, errs))
    for idx in want:
        try:
            r = post("%s/%s/_refresh" % (args.es, idx), b"", args.auth, args.insecure)
        except Exception as e:                      # noqa: BLE001
            # refresh 失败只是搜索可见性延迟，数据本身已经写进去了 ——
            # 降级成警告，不和 bulk 的失败混在一起，也不改退出码
            print("警告: refresh %s 失败: %s（不影响数据正确性，仅搜索可见性延迟）" % (idx, e),
                  file=sys.stderr)
    failed = [i for i in want if total.get(i, {}).get("errors")]
    if failed:
        # 失败摘要走 stderr 并返回 1：strict 拒收是静默的，
        # 只看"完成"两个字会以为灌成功了，索引里其实少了一大片数据
        lines = ["==== 入库失败汇总（bulk 有条目被 ES 拒收）===="]
        t_sent = t_err = 0
        for idx in want:
            info = total.get(idx)
            if not info:
                continue
            t_sent += info["sent"]
            t_err += info["errors"]
            lines.append("%s: 提交 %d 条，成功 %d 条，失败 %d 条"
                         % (idx, info["sent"], info["sent"] - info["errors"], info["errors"]))
            for i, s in enumerate(samples_by_idx.get(idx, []), 1):
                lines.append("  失败样例 %d: %s" % (i, s))
        lines.append("合计: 提交 %d 条，失败 %d 条" % (t_sent, t_err))
        lines.append("结论: 有文档没进索引，退出码 1（strict 拒收是静默的，别当灌成功）")
        print("\n".join(lines), file=sys.stderr)
        return 1
    print("完成:", json.dumps(total, ensure_ascii=False))
    return 0


def caused_text(cb):
    """把 error.caused_by 展开成一行文本（ES 的 caused_by 可能层层嵌套）。"""
    if not isinstance(cb, dict):
        return str(cb)
    txt = cb.get("type") or ""
    if cb.get("reason"):
        txt = ("%s: %s" % (txt, cb["reason"])) if txt else cb["reason"]
    inner = cb.get("caused_by")
    if isinstance(inner, dict):
        txt = "%s <- %s" % (txt, caused_text(inner))
    return txt


def fmt_fail(action):
    """把 bulk 里的单条失败压成一行可读摘要（保留 ES 给的 reason / caused_by）。

    整条 action JSON 动辄几百字符；定位问题的是 status / error.type /
    error.reason / error.caused_by / _id，其余部分不打印。
    """
    err = action.get("error")
    if isinstance(err, str):
        return "reason=%s" % err
    err = err if isinstance(err, dict) else {}
    parts = []
    if action.get("status") is not None:
        parts.append("status=%s" % action["status"])
    if err.get("type"):
        parts.append("type=%s" % err["type"])
    if err.get("reason"):
        parts.append("reason=%s" % err["reason"])
    if err.get("caused_by"):
        parts.append("caused_by=%s" % caused_text(err["caused_by"]))
    if action.get("_id"):
        parts.append("_id=%s" % action["_id"])
    return " ".join(parts) or json.dumps(action, ensure_ascii=False)[:300]


def flush(args, buf, sent):
    """提交一批 bulk，返回 (失败条数, 本批失败样例列表)。

    strict 拒收是静默的：ES 会整条丢掉类型冲突的文档，而 bulk 仍返回 200 ——
    不看 per-item 的 status 就会以为灌成功了。失败条数要一路带回 main，
    最后以非 0 退出码结束，而不是打印一句"完成"就当重灌成功。
    """
    try:
        r = post("%s/_bulk?refresh=false" % args.es, "\n".join(buf) + "\n", args.auth, args.insecure)
    except urllib.error.HTTPError as e:
        # 请求级失败：这一批一条都没写进去，整批计失败
        try:
            raw = e.read()[:400]
        except Exception:                                  # noqa: BLE001
            raw = b""                                      # 有些响应体读不出来，别让报错路径自己崩
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        detail = "bulk 请求失败: HTTP %s %s" % (e.code, raw)
        print("  " + detail, file=sys.stderr)
        return len(buf) // 2, [detail]
    except (OSError, ValueError) as exc:
        detail = "bulk 请求失败: %s" % exc
        print("  " + detail, file=sys.stderr)
        return len(buf) // 2, [detail]
    bad = 0
    samples = []
    if not isinstance(r, dict) or not isinstance(r.get("items"), list):
        return len(buf) // 2, ["bulk 响应缺少 items 数组"]
    items = r["items"]
    expect = len(buf) // 2
    if len(items) < expect:
        # 明细比提交的还少：少的那几条结果未知，不能当成功算
        miss = expect - len(items)
        bad += miss
        samples.append("bulk 只返回 %d 条明细（提交 %d 条），%d 条结果未知，按失败计"
                       % (len(items), expect, miss))
    if len(items) > expect:
        return expect, ["bulk 返回明细多于提交数，响应不可验证"]
    for item in items:
        act = item.get("index") if isinstance(item, dict) else None
        act = act if isinstance(act, dict) else {}
        if act.get("status") not in (200, 201):
            bad += 1
            if len(samples) < 3:
                samples.append(fmt_fail(act))
    if bad:
        print("  本批 %d 条有问题（bulk 请求本身仍是 200）" % bad, file=sys.stderr)
    return bad, samples


if __name__ == "__main__":
    sys.exit(main())
