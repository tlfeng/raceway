# -*- coding: utf-8 -*-
"""段数口径回归：只数 open 且非 "." 系统索引的业务索引。

背景见 docs/FINDING-ez-forcemerge-segment-count-20261113.md —— rally 赛末采的
`_all.primaries.segments.count` 把 .security 的系统索引碎片一并计入（EZ 恒 +8 段），
跨引擎比段数会被这层噪声支配。这里锁住两处实现：
  bench-sidecar.segments_open_nondot()  采样侧重算，写进 sidecar 的 segment_stats
  bench-export._open_nondot_segments()  导出侧从探针文件重算（赛末快照口径）
口径必须一致；探针缺失时都返回 None（缺探针不猜值）。
"""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
S = importlib.import_module('bench-sidecar')
E = importlib.import_module('bench-export')


def stats_payload(per_index):
    """{"索引名": 段数} → _all/_stats/segments 的形状。"""
    return {"_shards": {"total": len(per_index), "successful": len(per_index), "failed": 0},
            "indices": {name: {"primaries": {"segments": {"count": n}},
                               "total": {"segments": {"count": n}}}
                        for name, n in per_index.items()}}


def cat_indices(states):
    """{"索引名": status} → _cat/indices?format=json 的形状。"""
    return [{"index": name, "status": st} for name, st in states.items()]


class SidecarCaliberTest(unittest.TestCase):
    def test_counts_only_open_business_indices(self):
        out = S.segments_open_nondot(
            json.dumps(stats_payload({"vectors": 3, ".security": 8, "old": 5})),
            json.dumps(cat_indices({"vectors": "open", ".security": "open", "old": "close"})))
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["per_index"], {"vectors": 3})
        # 被排除项要留痕，差值才能审计
        self.assertEqual(sorted(out["excluded"]), [".security", "old"])
        self.assertEqual(out["excluded"][".security"], {"count": 8, "status": "open"})
        self.assertEqual(out["excluded"]["old"]["status"], "close")

    def test_missing_probe_returns_none(self):
        listing = json.dumps(cat_indices({"vectors": "open"}))
        stats = json.dumps(stats_payload({"vectors": 3}))
        self.assertIsNone(S.segments_open_nondot(None, listing))
        self.assertIsNone(S.segments_open_nondot(stats, ""))
        self.assertIsNone(S.segments_open_nondot("", ""))

    def test_unparsable_probe_returns_none(self):
        listing = json.dumps(cat_indices({"vectors": "open"}))
        stats = json.dumps(stats_payload({"vectors": 3}))
        self.assertIsNone(S.segments_open_nondot("<html>502</html>", listing))
        self.assertIsNone(S.segments_open_nondot(stats, "<html>502</html>"))

    def test_curl_error_prefix_is_tolerated(self):
        """curl 失败时探针文件里可能带错误前缀，取第一个 { 之后的 JSON。"""
        text = 'curl: (7) Failed to connect\n' + json.dumps(stats_payload({"vectors": 3}))
        out = S.segments_open_nondot(text, json.dumps(cat_indices({"vectors": "open"})))
        self.assertEqual(out["count"], 3)

    def test_all_excluded_still_reports_probe_was_read(self):
        """只剩系统索引时：probe 读到了（不是 None），但 per_index 为空 —— 两者要能区分。"""
        out = S.segments_open_nondot(json.dumps(stats_payload({".security": 8})),
                                     json.dumps(cat_indices({".security": "open"})))
        self.assertIsNotNone(out)
        self.assertIsNone(out["per_index"])
        self.assertEqual(out["count"], 0)

    def test_malformed_segment_count_is_skipped(self):
        payload = {"indices": {"vectors": {"primaries": {"segments": {"count": True}}},
                               "geo": {"primaries": {"segments": {"count": 2}}}}}
        out = S.segments_open_nondot(json.dumps(payload),
                                     json.dumps(cat_indices({"vectors": "open", "geo": "open"})))
        self.assertEqual(out["per_index"], {"geo": 2})     # bool 不算段数

    def test_export_reads_the_same_number(self):
        """同一份探针，采样侧与导出侧必须给同一个数（口径漂移的守卫）。"""
        stats = json.dumps(stats_payload({"vectors": 3, ".security": 8}))
        listing = json.dumps(cat_indices({"vectors": "open", ".security": "open"}))
        side = S.segments_open_nondot(stats, listing)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp, 'fp', 'fid1-q')
            d.mkdir(parents=True)
            (d / 'indices-segments.txt').write_text(stats, encoding='utf-8')
            (d / 'indices-list.txt').write_text(listing, encoding='utf-8')
            self.assertEqual(E._open_nondot_segments(str(d)), side["count"])


class ExportCaliberTest(unittest.TestCase):
    def _fp(self, tmp, segments=None, listing=None):
        d = Path(tmp, 'logs', 'fp', 'fid1-q')
        d.mkdir(parents=True, exist_ok=True)
        if segments is not None:
            (d / 'indices-segments.txt').write_text(
                segments if isinstance(segments, str) else json.dumps(stats_payload(segments)),
                encoding='utf-8')
        if listing is not None:
            (d / 'indices-list.txt').write_text(
                listing if isinstance(listing, str) else json.dumps(cat_indices(listing)),
                encoding='utf-8')
        return str(d)

    def test_reads_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._fp(tmp, {"vectors": 3, ".security": 8},
                         {"vectors": "open", ".security": "open"})
            self.assertEqual(E._open_nondot_segments(d), 3)

    def test_missing_probe_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._fp(tmp, {"vectors": 3}, None)
            self.assertIsNone(E._open_nondot_segments(d))

    def test_closed_index_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._fp(tmp, {"vectors": 3, "frozen": 9},
                         {"vectors": "open", "frozen": "close"})
            self.assertEqual(E._open_nondot_segments(d), 3)


class PostMergeCaliberLabelTest(unittest.TestCase):
    """load_campaign_meta 要让下游能看出这个段数是什么口径。"""

    def _camp(self, tmp, with_probes):
        camp = Path(tmp, 'camp')
        fp = camp / 'logs' / 'fp' / 'fid1-q'
        fp.mkdir(parents=True)
        # 两个节点：节点统计和 = 3 + 4 = 7（混合口径，含系统索引）
        nodes = {"nodes": {"n1": {"indices": {"segments": {"count": 3},
                                            "store": {"size_in_bytes": 1 << 30}}},
                           "n2": {"indices": {"segments": {"count": 4},
                                            "store": {"size_in_bytes": 1 << 30}}},
                           "_nodes": {}}}
        (fp / 'nodes-stats.txt').write_text(json.dumps(nodes), encoding='utf-8')
        if with_probes:
            (fp / 'indices-segments.txt').write_text(
                json.dumps(stats_payload({"vectors": 3, ".security": 8})), encoding='utf-8')
            (fp / 'indices-list.txt').write_text(
                json.dumps(cat_indices({"vectors": "open", ".security": "open"})), encoding='utf-8')
        return str(camp)

    def test_probe_present_uses_clean_caliber(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = E.load_campaign_meta(self._camp(tmp, True))
        pm = meta["post_merge"]["fid1"]
        self.assertEqual(pm["segments"], 3)                   # 不是节点和 7
        self.assertEqual(pm["segments_caliber"], "open_nondot")

    def test_probe_absent_falls_back_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = E.load_campaign_meta(self._camp(tmp, False))
        pm = meta["post_merge"]["fid1"]
        self.assertEqual(pm["segments"], 7)                   # 老 campaign 回退节点和
        self.assertEqual(pm["segments_caliber"], "node_stats_all")


if __name__ == '__main__':
    unittest.main()
