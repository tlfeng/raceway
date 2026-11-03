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
    samples_by_idx = {}
    for idx in want:
        path = os.path.join(args.dir, idx + ".jsonl")
        if not os.path.isfile(path):
            print("跳过（无此文件）: %s" % path)
            continue
        sent = errs = 0
        samples = []
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
                    bad, smp = flush(args, buf, sent)
                    errs += bad
                    if len(samples) < 3:
                        samples.extend(smp[:3 - len(samples)])
                    sent += args.batch
                    buf = []
            if buf:
                bad, smp = flush(args, buf, sent)
                errs += bad
                if len(samples) < 3:
                    samples.extend(smp[:3 - len(samples)])
                sent += len(buf) // 2
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

    整条 action JSON 动辄几百字符，真正定位问题的是 status / error.type /
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

    strict 拒收是静默的：ES 会整条丢掉类型冲突的文档，而 bulk 仍然返回 200 ——
    不看 per-item 的 status 就会以为灌成功了。所以失败条数要一路带回 main，
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
    bad = 0
    samples = []
    items = r.get("items") or []
    expect = len(buf) // 2
    if len(items) < expect:
        # 明细比提交的还少：少的那几条结果未知，不能当成功算
        miss = expect - len(items)
        bad += miss
        samples.append("bulk 只返回 %d 条明细（提交 %d 条），%d 条结果未知，按失败计"
                       % (len(items), expect, miss))
    for item in items:
        act = item.get("index") or {}
        if act.get("status") not in (200, 201):
            bad += 1
            if len(samples) < 3:
                samples.append(fmt_fail(act))
    if bad:
        print("  本批 %d 条有问题（bulk 请求本身仍是 200）" % bad, file=sys.stderr)
    return bad, samples


if __name__ == "__main__":
    sys.exit(main())
