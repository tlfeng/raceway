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
"""
import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

IDX = ("benchmark-result-env", "benchmark-result-race", "benchmark-result-metric")


def flatten_fields(template):
    """从 index template 里取出映射为 flattened 的字段名。

    flattened 是「对象」型 mapper，但 EasySearch 2.5.0 的实现比 ES 8.x 少一层
    保护：ES 的 FlattenedFieldMapper.parse 对 null 早返回，EasySearch 没有，
    于是文档里出现显式 null 时报
        "tried to parse field [x] as object, but got EOF"
    flattened 也不认 null_value 参数（实测 unsupported parameters），
    所以唯一可行的处理是：值为 null 时把字段整个省掉。
    对 ES 语义等价 —— null 与缺失同样不进倒排、不参与聚合与 exists 查询，
    只是 _source 里少一个键。
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

    want = [i for i in args.only.split(",") if i in IDX]

    if args.drop:
        for idx in want:
            try:
                post("%s/%s" % (args.es, idx), b"", args.auth, args.insecure, "application/json", "DELETE")
                print("已删除索引 %s" % idx)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print("删除 %s 失败: %s" % (idx, e))
                else:
                    print("索引 %s 不存在，跳过删除" % idx)

    tp = os.path.join(args.dir, "templates.json")
    flat = {}
    if os.path.isfile(tp):
        templates = json.load(open(tp, encoding="utf-8"))
        for idx in want:
            if idx in templates:
                r = post("%s/_index_template/%s" % (args.es, idx),
                         json.dumps(templates[idx]), args.auth, args.insecure,
                         "application/json", "PUT")
                print("模板 %s → %s" % (idx, r))
            flat[idx] = flatten_fields(templates.get(idx))
    else:
        for idx in want:
            flat[idx] = set()

    total = {}
    for idx in want:
        path = os.path.join(args.dir, idx + ".jsonl")
        if not os.path.isfile(path):
            print("跳过（无此文件）: %s" % path)
            continue
        sent = errs = 0
        buf = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                doc = json.loads(line)
                rid = doc.pop("_id")
                doc.pop("_index", None)
                drop_null_flattened(doc, flat[idx])
                buf.append(json.dumps({"index": {"_index": idx, "_id": rid}}))
                buf.append(json.dumps(doc, ensure_ascii=False))
                if len(buf) >= args.batch * 2:
                    errs += flush(args, buf, sent)
                    sent += args.batch
                    buf = []
            if buf:
                errs += flush(args, buf, sent)
                sent += len(buf) // 2
        total[idx] = {"sent": sent, "errors": errs}
        print("%s: 提交 %d 条，失败 %d" % (idx, sent, errs))
    for idx in want:
        try:
            r = post("%s/%s/_refresh" % (args.es, idx), b"", args.auth, args.insecure)
        except Exception as e:                      # noqa: BLE001
            print("refresh %s 失败: %s" % (idx, e))
    print("完成:", json.dumps(total, ensure_ascii=False))
    return 0


def flush(args, buf, sent):
    try:
        r = post("%s/_bulk?refresh=false" % args.es, "\n".join(buf) + "\n", args.auth, args.insecure)
    except urllib.error.HTTPError as e:
        print("  bulk 失败: %s %s" % (e.code, e.read()[:400]))
        return 1
    bad = 0
    for item in r.get("items", []):
        act = item.get("index") or {}
        if act.get("status") not in (200, 201):
            bad += 1
            if bad <= 3:
                print("  写失败样例:", json.dumps(act, ensure_ascii=False)[:300])
    return bad


if __name__ == "__main__":
    sys.exit(main())
