#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hv1024x 家族：四臂一份定义 + track-params —— 渲染结果必须逐臂正确

一份基座 + 两个参数维度：

    量化档  → vector_index_type=int8_hnsw | bbq_hnsw  （index_options.type）
    压缩档  → index_codec=ZSTD + source_reuse=true     （settings）

**为什么必须有这个测试**：量化档写在 **mappings** 里（不在 settings）——
条件块缺失时 rally 不报错，但该臂会静默跑成 hnsw 基座，"int8 跑过"其实跑的是
fp32，数据看着正常、结论全废。同理压缩档缺失就是"三个臂跑成同一个"。
所以这里逐臂渲染一遍，断言 settings / mappings 真的按参数变了。

跑法：
    python3 -m unittest discover -s tests -t . -v
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACK_DIR = os.path.join(ROOT, 'tracks', 'hv1024x')
INDEX_JSON = os.path.join(TRACK_DIR, 'index.json')

# 编排 id → 该臂应有的 track-params
ARMS = {
    'hv1024x': {},
    'hv1024x-int8': {'vector_index_type': 'int8_hnsw'},
    'hv1024x-bbq': {'vector_index_type': 'bbq_hnsw'},
    'hv1024x-codec-zstd-sr': {'index_codec': 'ZSTD', 'source_reuse': 'true'},
}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def render(tmpl, params):
    """按 rally 的 Jinja 语义渲染这份 index.json（只有三种占位）。"""
    s = tmpl
    if 'index_codec' in params:
        s = re.sub(r'\{%\s*if\s+index_codec\s+is\s+defined\s*%\}(.*?)\{%\s*endif\s*%\}',
                   lambda m: m.group(1), s, flags=re.S)
    else:
        s = re.sub(r'\{%\s*if\s+index_codec\s+is\s+defined\s*%\}.*?\{%\s*endif\s*%\}',
                   '', s, flags=re.S)
    if params.get('source_reuse'):
        s = re.sub(r'\{%\s*if\s+source_reuse\s+is\s+defined\s+and\s+source_reuse\s*%\}(.*?)\{%\s*endif\s*%\}',
                   lambda m: m.group(1), s, flags=re.S)
    else:
        s = re.sub(r'\{%\s*if\s+source_reuse\s+is\s+defined\s+and\s+source_reuse\s*%\}.*?\{%\s*endif\s*%\}',
                   '', s, flags=re.S)
    s = re.sub(r'\{\{\s*vector_index_type\s*\|\s*default\(\'hnsw\'\)\s*\}\}',
               params.get('vector_index_type', 'hnsw'), s)
    s = re.sub(r'\{\{\s*index_codec\s*\}\}', params.get('index_codec', ''), s)
    return s


class TestHv1024xFamily(unittest.TestCase):
    """一份基座定义 + 参数 = 四臂。"""

    def setUp(self):
        self.tmpl = _read(INDEX_JSON)

    def test_every_arm_renders_valid_json(self):
        for arm, params in ARMS.items():
            out = render(self.tmpl, params)
            try:
                doc = json.loads(out)
            except Exception as e:
                self.fail('%s 渲染不是合法 JSON: %s\n%s' % (arm, e, out))
            self.assertIn('settings', doc, '%s 缺 settings' % arm)
            self.assertIn('mappings', doc, '%s 缺 mappings' % arm)

    def test_quant_arm_sets_index_options_type(self):
        """量化档只该改 index_options.type，其余映射逐字相同。"""
        base = json.loads(render(self.tmpl, {}))
        base_type = base['mappings']['properties']['emb']['index_options']
        for arm, want in (('hv1024x-int8', 'int8_hnsw'), ('hv1024x-bbq', 'bbq_hnsw')):
            doc = json.loads(render(self.tmpl, ARMS[arm]))
            io = doc['mappings']['properties']['emb']['index_options']
            self.assertEqual(io['type'], want, '%s 的 index_options.type 不对' % arm)
            # 其余字段必须与基座一致（m / ef_construction）
            for k in ('m', 'ef_construction'):
                self.assertEqual(io.get(k), base_type.get(k),
                                 '%s 的 index_options.%s 与基座不一致' % (arm, k))
            # mappings 只允许 index_options.type 这一处差异
            self.assertEqual(doc['mappings']['properties']['emb']['dims'], 1024)
            self.assertEqual(doc['mappings']['properties']['emb']['similarity'], 'dot_product')

    def test_base_arm_is_plain_hnsw(self):
        """基座臂不带任何量化/压缩设置。"""
        doc = json.loads(render(self.tmpl, {}))
        self.assertEqual(doc['mappings']['properties']['emb']['index_options']['type'], 'hnsw')
        st = doc['settings']
        self.assertNotIn('index.codec', st, '基座臂不该带 index.codec')
        self.assertNotIn('index.source_reuse', st, '基座臂不该带 index.source_reuse')

    def test_codec_arm_sets_codec_and_source_reuse(self):
        """压缩臂：settings 里出现 index.codec 与 index.source_reuse（顶层键）。"""
        doc = json.loads(render(self.tmpl, ARMS['hv1024x-codec-zstd-sr']))
        st = doc['settings']
        self.assertEqual(st.get('index.codec'), 'ZSTD')
        self.assertIs(st.get('index.source_reuse'), True)
        # codec / source_reuse 必须在 settings 顶层（不能塞进 "index" 对象里）
        self.assertNotIn('index.codec', st.get('index', {}), 'codec 不能嵌在 index 对象里')
        self.assertNotIn('index.source_reuse', st.get('index', {}), 'source_reuse 不能嵌在 index 对象里')
        # 量化档仍是基座的 hnsw（压缩臂不叠加量化）
        self.assertEqual(doc['mappings']['properties']['emb']['index_options']['type'], 'hnsw')

    def test_template_has_all_condition_blocks(self):
        """条件块必须在位（缺一个 = 该维度静默失效，见模块注释）。"""
        for needle in ('index_codec is defined',
                       'source_reuse is defined and source_reuse',
                       'vector_index_type'):
            self.assertIn(needle, self.tmpl, '定版 index.json 缺条件块: %s' % needle)

    # 允许存在的 hv1024x- 目录 = 家族的**独立目录定义**成员。
    # 它们与基座的 mappings 不同（os 族的 knn_vector+method、bbq_disk 的
    # index_options.type 是另一种结构），差异表达不成 track-params，只能各立目录；
    # 这与量化档（int8/bbq，只差 index_options.type 一行，走 params）相反。
    # 出现名单之外的 hv1024x-* 目录 = 又有人把 params 能表达的东西建成了目录 → 红。
    FAMILY_DIRS = ('hv1024x-bbqdisk', 'hv1024x-os', 'hv1024x-os-bbq', 'hv1024x-os-disk')

    def test_no_duplicate_arm_directories(self):
        """量化族不许再出现第二份目录（差异应由 track-params 表达）；
        独立目录成员以 FAMILY_DIRS 名单为准，名单之外的目录一律红。"""
        tracks = os.path.join(ROOT, 'tracks')
        leftovers = [d for d in os.listdir(tracks)
                     if d.startswith('hv1024x-') and os.path.isdir(os.path.join(tracks, d))]
        self.assertEqual(sorted(leftovers), sorted(self.FAMILY_DIRS),
                         'hv1024x 家族目录与名单不符（量化档应走 track-params；'
                         '独立目录成员名单见 FAMILY_DIRS）: %s' % leftovers)

    def test_create_only_probe_still_available(self):
        """建索引探针必须随基座保留（用于复现建索引类故障）。"""
        p = os.path.join(TRACK_DIR, 'challenges', 'create-only.json')
        self.assertTrue(os.path.isfile(p), '缺建索引探针: %s' % p)
        self.assertIn('"name": "create-only"', _read(p))

    def test_family_has_write_and_query_challenges(self):
        """full 挑战由 write-only + query-only 派生，两者缺一不可。"""
        txt = _read(os.path.join(TRACK_DIR, 'challenges', 'default.json'))
        self.assertIn('"name": "write-only"', txt)
        self.assertIn('"name": "query-only"', txt)


if __name__ == '__main__':
    unittest.main()
