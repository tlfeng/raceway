#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geonames encrypted-store 挑战与 encrypted_store 条件块（回归闸门）

背景：`tracks/geonames/challenges/encrypted-store.json` 是 easysearch store-encrypted
索引加密插件的读写性能探针 —— create-index 烘入 index.store.type=encrypted /
index.encryption.profile=index（operation 级 settings 合并覆盖 index.json 的
store.type，esrally track/params.py CreateIndexParamSource 语义），随后写段与查段
与 full 挑战（write-only + query-only 派生）逐项同序同预算：两者唯一差异是加密设置，
数字可直接对比。

同时 `tracks/geonames/index.json` 加了 encrypted_store 选通条件块（与 hv1024x/hnsw
同模式）：默认关（渲染出 store_type 指定的 fs），开了写 encrypted + profile=index ——
让 full/write-only/query-only 也能经 --track-params 跑加密变体。

为什么是闸门（20261112 整改同款教训）：条件块缺失或加密键被 store_type 压掉时
rally 不报错，该臂静默跑成明文基座 —— 数据看着正常、结论全废，起跑前就挡住。

跑法：
    python3 -m unittest tests.test_geonames_encrypted_store -v
     （或 python3 -m unittest discover -s tests -t . -v）
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEO = os.path.join(ROOT, 'tracks', 'geonames')
CHALLENGE = os.path.join(GEO, 'challenges', 'encrypted-store.json')
INDEX_JSON = os.path.join(GEO, 'index.json')

ENC_SETTINGS = {
    'index.store.type': 'encrypted',
    'index.encryption.profile': 'index',
}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def strip_jinja_stmt(s):
    """剥 Jinja 语句标签（{% if %} 等），保留 {{ }} 占位由 mask 覆盖。"""
    return re.sub(r'\{%.*?%\}', '', s, flags=re.S)


def json_after_masking(s):
    """把 {{ }} 占位遮成 "X" 后解析 —— 引号内占位先遮（免得变成 ""X""），再遮裸占位。"""
    s = re.sub(r'"\{\{.*?\}\}"', '"X"', s, flags=re.S)
    return json.loads(re.sub(r'\{\{.*?\}\}', '"X"', s))


def challenge_schedule_ops():
    """encrypted-store 挑战的操作序列（有效名 = 显式 name，否则 operation 引用）。"""
    doc = json_after_masking(strip_jinja_stmt(_read(CHALLENGE)))
    ops = []
    for task in doc['schedule']:
        op = task.get('operation')
        if isinstance(op, str):
            ops.append(task.get('name') or op)
        else:
            ops.append(task.get('name') or op.get('operation-type'))
    return doc, ops


def base_challenge_ops(fname, want):
    """从 write-only/query-only 取同口径操作序列（文本级扫描，与派生器同逻辑）。"""
    txt = _read(os.path.join(GEO, 'challenges', fname))
    m = re.search(r'"name"\s*:\s*"%s"' % re.escape(want), txt)
    i = txt.find('"schedule"', m.end())
    j = txt.find('[', i)
    depth, k, in_str, esc = 0, j, False, False
    while True:
        c = txt[k]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in '[{':
                depth += 1
            elif c in ']}':
                depth -= 1
                if depth == 0:
                    break
        k += 1
    inner = txt[j + 1:k]
    inner = re.sub(r'\{%.*?%\}', '', inner, flags=re.S)
    doc = json_after_masking('[' + inner + ']')
    ops = []
    for task in doc:
        op = task.get('operation')
        if isinstance(op, str):
            ops.append(task.get('name') or op)
        else:
            ops.append(task.get('name') or op.get('operation-type'))
    return ops


class EncryptedStoreChallengeTest(unittest.TestCase):
    def setUp(self):
        self.doc = json_after_masking(strip_jinja_stmt(_read(CHALLENGE)))

    def test_challenge_is_parseable_and_named(self):
        self.assertEqual(self.doc['name'], 'encrypted-store')
        self.assertIn('encrypted', self.doc['description'])
        self.assertTrue(self.doc['schedule'], 'schedule 为空')

    def test_create_index_bakes_encrypted_settings(self):
        """create-index 必须烘入加密设置，且加密 update 必须最后（后置必胜，压过 index_settings）。

        settings 是 {{ p_enc_settings | tojson }} 表达式，遮罩法解析不出 —— 改为静态检查
        update 链：最后一个 update 必须恰为加密两键。
        """
        creates = [t for t in self.doc['schedule']
                   if isinstance(t.get('operation'), dict)
                   and t['operation'].get('operation-type') == 'create-index']
        self.assertEqual(len(creates), 1, 'encrypted-store 应只有一个 create-index')
        src = _read(CHALLENGE)
        updates = re.findall(r'p_enc_settings\.update\((\{.*?\})\)', src, flags=re.S)
        self.assertTrue(updates, 'create-index 里没有 p_enc_settings.update 链')
        last = json.loads(updates[-1])
        self.assertEqual(last, ENC_SETTINGS,
                         '最后一个 update 不是加密两键（加密会被 index_settings 压掉）')
        # create-index 的 settings 行必须消费 p_enc_settings
        self.assertIn('{{ p_enc_settings | tojson }}', src)

    def test_write_segment_matches_write_only(self):
        """写段与 write-only 逐项同序（仅 create-index 的 settings 不同，已单独断言）。"""
        want = base_challenge_ops('write-only.json', 'write-only')
        got = challenge_schedule_ops()[1][:len(want)]
        self.assertEqual(want, got, '写段操作序列与 write-only 漂移了')

    def test_query_segment_matches_query_only(self):
        """查段与 query-only 逐项同序同预算 —— 与 full 基线可比的前提。"""
        want = base_challenge_ops('query-only.json', 'query-only')
        got = challenge_schedule_ops()[1][len(base_challenge_ops('write-only.json', 'write-only')):]
        self.assertEqual(want, got, '查段操作序列与 query-only 漂移了')


def render_index_json(params):
    """按测试友好的 Jinja 语义渲染 index.json 的 settings 段（条件块三态）。"""
    s = _read(INDEX_JSON)
    # encrypted_store 条件块：开了保留块内文本，关了整块消失（含 else 的 fs 行保留）
    on = r'\{%-\s*if\s+encrypted_store\s+is\s+defined\s+and\s+encrypted_store\s*%\}(.*?)\{%-\s*else\s*%\}.*?\{%-\s*endif\s*%\}'
    off = r'\{%-\s*if\s+encrypted_store\s+is\s+defined\s+and\s+encrypted_store\s*%\}.*?\{%-\s*else\s*%\}(.*?)\{%-\s*endif\s*%\}'
    if params.get('encrypted_store'):
        s = re.sub(on, lambda m: m.group(1), s, flags=re.S)
    else:
        s = re.sub(off, lambda m: m.group(1), s, flags=re.S)
    s = re.sub(r'\{%\s*if\s+index_codec\s+is\s+defined\s*%\}.*?\{%\s*endif\s*%\}', '', s, flags=re.S)
    s = re.sub(r'\{%\s*if\s+source_reuse\s+is\s+defined\s+and\s+source_reuse\s*%\}.*?\{%\s*endif\s*%\}',
               '', s, flags=re.S)
    s = re.sub(r'\{%.*?%\}', '', s, flags=re.S)          # 其余语句标签（flavor 门、p_include）
    s = re.sub(r'\{#.*?#\}', '', s, flags=re.S)          # 注释
    # {{expr | default(V)}} → V（V 可能带引号）；其余占位遮成 "X"
    s = re.sub(r"\{\{[^{}]*\|\s*default\(([^)]+)\)\s*\}\}",
               lambda m: m.group(1).strip().strip("'\""), s)
    return json_after_masking(s)


class EncryptedStoreIndexJsonTest(unittest.TestCase):
    def test_default_renders_plain_fs(self):
        """encrypted_store 默认关：store.type 走 store_type 指定值，无加密键。"""
        st = render_index_json({})['settings']
        self.assertEqual(st.get('index.store.type'), 'fs')
        self.assertNotIn('index.encryption.profile', st)

    def test_encrypted_store_switch_bakes_encryption(self):
        """开了才写 store.type=encrypted + profile（store_type 在场也必须被压掉）。"""
        for params in ({'encrypted_store': 'true'},
                       {'encrypted_store': 'true', 'store_type': 'niofs'}):
            st = render_index_json(dict(params, number_of_shards=3))['settings']
            for k, v in ENC_SETTINGS.items():
                self.assertEqual(st.get(k), v,
                                 '%s: 加密键 %s 没写进 settings' % (params, k))

    def test_encrypted_keys_are_settings_top_level(self):
        """加密键必须是 settings 顶层点键（嵌进 index 对象 = index.index.store.type，会建不起来）。"""
        st = render_index_json({'encrypted_store': 'true'})['settings']
        self.assertNotIn('store.type', st.get('index', {}), 'store.type 不能嵌在 index 对象里')
        self.assertNotIn('encryption.profile', st.get('index', {}), 'encryption.profile 不能嵌在 index 对象里')


if __name__ == '__main__':
    unittest.main()
