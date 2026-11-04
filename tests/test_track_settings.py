#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛道 index.json 的 settings 结构闸门

跑法：python3 -m unittest tests.test_track_settings -v
     （或 python3 -m unittest discover -s tests -t . -v）

守的不变式：`index.codec` / `index.source_reuse` 必须是 **settings 的顶层键**
（与 "index" 对象平级），不能嵌在 "index" 对象里。

为什么是硬闸门（2026-11-04 实测）：嵌进去展开后是 `settings.index.index.codec`
→ EasySearch 直接拒收（unknown setting [index.index.codec]）→ **索引根本建不起来**。
那批 hvbbq 的 3 场 create-index error_rate=1.0、后续 knn 全废、赛末 42.7 GB 是
坏索引残留（同规模基座只有 4.7 GB）—— 整批数据作废重来。

判据的实现在 flatten_settings.py（审计/修复工具），本测试只做「不许再犯」：
扫仓库里所有赛道的 index.json（含压测机上生成的变体），结构不对就红。
"""
import glob
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

F = importlib.import_module('flatten_settings')  # noqa: E402

TRACKS = os.path.join(ROOT, 'tracks')


def index_json_files():
    """仓库里所有赛道的 index.json（静态基座 + 压测机上生成的变体）"""
    if not os.path.isdir(TRACKS):
        return []
    return sorted(glob.glob(os.path.join(TRACKS, '**', 'index.json'), recursive=True))


class TrackSettingsTest(unittest.TestCase):
    def test_track_files_found(self):
        """闸门本身得真的扫到东西 —— 空目录就静默放行等于没设闸门"""
        self.assertTrue(index_json_files(), "tracks/ 下没扫到任何 index.json")

    def test_settings_structure_ok(self):
        bad = []
        for p in index_json_files():
            status, msg = F.audit(p)
            if status in ("nested", "jinja-nested"):
                bad.append("%s → %s" % (os.path.relpath(p, ROOT), msg))
        self.assertEqual([], bad,
                         "这些赛道把 codec/source_reuse 嵌进了 index 对象（索引会建不起来）：\n  "
                         + "\n  ".join(bad))

    def test_audit_detects_nested(self):
        """闸门自己得能认出嵌套写法 —— 否则 test_settings_structure_ok 是空转"""
        import json
        import tempfile
        bad = {"settings": {"index": {"number_of_shards": 3, "index.codec": "zstd"}},
               "mappings": {}}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "index.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            status, msg = F.audit(p)
        self.assertEqual("nested", status, msg)

    def test_audit_accepts_flat(self):
        """平铺写法要判 ok —— 不能误报"""
        import json
        import tempfile
        good = {"settings": {"index": {"number_of_shards": 3}, "index.codec": "zstd"},
                "mappings": {}}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "index.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(good, f)
            status, msg = F.audit(p)
        self.assertEqual("ok", status, msg)


if __name__ == "__main__":
    unittest.main()
