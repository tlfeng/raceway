#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hv1024 与 hv1024x 两族：共享文件必须逐字节相同，差异只允许在白名单里

**为什么要有这个测试**：`tracks/hv1024` 与 `tracks/hv1024x` 是**两条不同的赛道**
（不是同一条赛道的两个档），唯一的实质差异是 `_source` 策略 ——

    hv1024   : "_source": { "enabled": false }            整个 _source 关掉
    hv1024x  : "_source": { "excludes": ["emb"] }          保留 docid、排除向量

这是**写入/存储口径**（影响 translog、segment 体积、写入耗时），不是被测变量，
所以两族分开定版、各自一份目录。

`_source` 是 mappings 里的东西，sidecar 只采 `_settings`（index settings）→
**入库数据里查不到这一口径**，合并成一个目录后就只剩"track id + 参数台账"能反推，
比 quant/codec 弱一档。

两份目录里有 3 个文件是**逐字节相同**的（challenges/default.json、
operations/default.json、track.py）—— 同一份定义存两处，就是"改一处忘另一处"的
漂移源。这个测试就是那道闸门：共享文件一旦分叉就红。

跑法：
    python3 -m unittest discover -s tests -t . -v
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS = os.path.join(ROOT, 'tracks')
A = os.path.join(TRACKS, 'hv1024')       # _source 整个关掉
B = os.path.join(TRACKS, 'hv1024x')      # _source 排除 emb

# 两族之间**必须**逐字节相同的文件
SHARED_FILES = ['challenges/default.json', 'operations/default.json', 'track.py']


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def render(tmpl, params):
    """按 rally 的 Jinja 语义渲染这两份 index.json（只有这几种占位）。"""
    s = tmpl
    # index_codec 条件块
    if 'index_codec' in params:
        s = re.sub(r'\{%\s*if\s+index_codec\s+is\s+defined\s*%\}(.*?)\{%\s*endif\s*%\}',
                   lambda m: m.group(1), s, flags=re.S)
    else:
        s = re.sub(r'\{%\s*if\s+index_codec\s+is\s+defined\s*%\}.*?\{%\s*endif\s*%\}',
                   '', s, flags=re.S)
    # source_reuse 条件块
    if params.get('source_reuse'):
        s = re.sub(r'\{%\s*if\s+source_reuse\s+is\s+defined\s+and\s+source_reuse\s*%\}(.*?)\{%\s*endif\s*%\}',
                   lambda m: m.group(1), s, flags=re.S)
    else:
        s = re.sub(r'\{%\s*if\s+source_reuse\s+is\s+defined\s+and\s+source_reuse\s*%\}.*?\{%\s*endif\s*%\}',
                   '', s, flags=re.S)
    # 标量占位
    s = re.sub(r'\{\{\s*number_of_shards\s*\|\s*default\(3\)\s*\}\}', '3', s)
    s = re.sub(r'\{\{\s*number_of_replicas\s*\|\s*default\(0\)\s*\}\}', '0', s)
    s = re.sub(r'\{\{\s*vector_index_type\s*\|\s*default\(\'hnsw\'\)\s*\}\}',
               params.get('vector_index_type', 'hnsw'), s)
    s = re.sub(r'\{\{\s*index_codec\s*\}\}', params.get('index_codec', ''), s)
    # 残留占位（未识别的）一律报错，别让半渲染的 JSON 混过去
    left = re.findall(r'\{\{.*?\}\}|\{%.*?%\}', s)
    if left:
        raise AssertionError('模板里有未处理的占位: %s' % left[:3])
    return s


class TestHv1024FamilySeparation(unittest.TestCase):
    """两族是两条赛道：定义各自定版，但共享文件不许分叉。"""

    def test_shared_files_are_byte_identical(self):
        for f in SHARED_FILES:
            pa, pb = os.path.join(A, f), os.path.join(B, f)
            self.assertTrue(os.path.isfile(pa), '缺文件: %s' % pa)
            self.assertTrue(os.path.isfile(pb), '缺文件: %s' % pb)
            with open(pa, 'rb') as x, open(pb, 'rb') as y:
                self.assertEqual(x.read(), y.read(),
                                 '%s 两族不一致 —— 同一份定义存两处，改一处忘另一处就会漂移' % f)

    def test_source_policy_is_pinned_per_family(self):
        """_source 口径是这两条赛道的分水岭，必须各自钉死，不能被统一掉。"""
        a = json.loads(render(_read(os.path.join(A, 'index.json')), {}))
        b = json.loads(render(_read(os.path.join(B, 'index.json')), {}))
        self.assertEqual(a['mappings']['_source'], {'enabled': False},
                         'hv1024 的 _source 口径变了（应为整个关掉）')
        self.assertEqual(b['mappings']['_source'], {'excludes': ['emb']},
                         'hv1024x 的 _source 口径变了（应为保留 docid、排除 emb）')

    def test_both_families_render_valid_json(self):
        for d, params in ((A, {}), (B, {'vector_index_type': 'int8_hnsw'})):
            out = render(_read(os.path.join(d, 'index.json')), params)
            try:
                doc = json.loads(out)
            except Exception as e:
                self.fail('%s 渲染失败: %s\n%s' % (d, e, out))
            self.assertEqual(doc['mappings']['properties']['emb']['dims'], 1024)
            self.assertEqual(doc['mappings']['properties']['emb']['similarity'], 'dot_product')

    def test_only_documented_differences_exist(self):
        """index.json / track.json 的差异必须是"已登记在案"的那几条，不能多出别的。"""
        # index.json：除 _source / element_type / shards 写法 / index_options.type 外不应有别处不同
        a = json.loads(render(_read(os.path.join(A, 'index.json')), {}))
        b = json.loads(render(_read(os.path.join(B, 'index.json')), {}))
        self.assertEqual(a['mappings']['properties']['emb']['index_options'],
                         b['mappings']['properties']['emb']['index_options'],
                         'index_options 出现未登记的差异')
        self.assertEqual(a['mappings']['properties']['docid'], b['mappings']['properties']['docid'])
        self.assertEqual(a['mappings']['dynamic'], b['mappings']['dynamic'])
        self.assertEqual(a['settings']['index'], b['settings']['index'],
                         'settings.index 出现未登记的差异')
        # track.json：只允许 description 不同
        ta = json.loads(_read(os.path.join(A, 'track.json')).replace(
            '{% import "rally.helpers" as rally with context %}', '').replace(
            '{{ rally.collect(parts="operations/*.json") }}', '[]').replace(
            '{{ rally.collect(parts="challenges/*.json") }}', '[]'))
        tb = json.loads(_read(os.path.join(B, 'track.json')).replace(
            '{% import "rally.helpers" as rally with context %}', '').replace(
            '{{ rally.collect(parts="operations/*.json") }}', '[]').replace(
            '{{ rally.collect(parts="challenges/*.json") }}', '[]'))
        self.assertEqual(ta['indices'], tb['indices'], '索引定义出现未登记的差异')
        self.assertEqual(ta['corpora'], tb['corpora'], '语料声明出现未登记的差异')
        self.assertNotEqual(ta['description'], tb['description'],
                            'description 变了（无关紧要，但登记在案的两族文案不该再变）')

    def test_family_dirs_both_exist(self):
        """两族各有自己的目录（刻意不合并：_source 是口径，不是被测变量）。"""
        for d in (A, B):
            self.assertTrue(os.path.isdir(d), '缺赛道目录: %s' % d)


if __name__ == '__main__':
    unittest.main()
