#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-apply.py —— 把 bench-export.py 的产物灌进任意 ES/Easysearch 集群（纯标准库，无需 client）

  python3 bench-apply.py --dir exports/<tag> \
      --es https://127.0.0.1:9200 --auth admin:CHANGE_ME [--insecure] [--only bench-run,bench-metric]

幂等：每条文档带 _id（race_id / race_id|task|family|stat），重复执行是覆盖不是翻倍。
只做两件事：① 用 templates.json 建三张索引模板；② 按 jsonl 逐行走 _bulk。
"""
import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

IDX = ("bench-env", "bench-run", "bench-metric")


def post(url, body, auth, insecure, ctype="application/x-ndjson", method="POST"):
    req = urllib.request.Request(url, data=body.encode("utf-8") if isinstance(body, str) else body,
                                 method=method, headers={"Content-Type": ctype})
    req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--es", required=True)
    ap.add_argument("--auth", required=True)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--only", default=",".join(IDX))
    ap.add_argument("--batch", type=int, default=1000)
    args = ap.parse_args()

    want = [i for i in args.only.split(",") if i in IDX]
    tp = os.path.join(args.dir, "templates.json")
    if os.path.isfile(tp):
        templates = json.load(open(tp, encoding="utf-8"))
        for idx in want:
            if idx in templates:
                r = post("%s/_index_template/%s" % (args.es, idx),
                         json.dumps(templates[idx]), args.auth, args.insecure,
                         "application/json", "PUT")
                print("模板 %s → %s" % (idx, r))

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
