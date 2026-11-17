# -*- coding: utf-8 -*-
"""赛道定义必须能被真 Jinja2 渲染成合法 JSON。

tests/test_geonames_encrypted_store.py 用正则模拟 Jinja 语义来断言"开关三态"，
速度快、不依赖 jinja2，但它证明不了**渲染出来的字节**是合法 JSON —— 而 rally 用的是
真 Jinja2（`{%- ... %}` 会吃掉前导空白和换行）。少一个逗号、else 分支多一行，
正则测试照样绿，rally 加载时才炸。

这里用真 jinja2 渲染 geonames 的 index.json 与 encrypted-store / encrypted-store-snapshot
两个挑战，覆盖多种参数组合，只断言几件事：能 json.loads、加密键在开关打开时位于
settings 顶层、快照闭环的仓库 body 与异步语义渲染无漂移。
没装 jinja2 的环境（如只跑单测的开发机）skip，跑赛道的机器上会真正执行。
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEO = os.path.join(ROOT, 'tracks', 'geonames')

try:
    import jinja2
except ImportError:                                   # 没装就 skip，不当失败
    jinja2 = None

ENC = {'index.store.type': 'encrypted', 'index.encryption.profile': 'index'}

# 与 run-bench 传参对齐：p_include… 打开，分片/副本显式给值
BASE = {'p_include_non_serverless_index_settings': True,
        'number_of_shards': 3, 'number_of_replicas': 0}

CASES = [
    ({}, 'fs', None),
    ({'store_type': 'niofs'}, 'niofs', None),
    ({'encrypted_store': True}, 'encrypted', 'index'),
    ({'encrypted_store': True, 'store_type': 'niofs'}, 'encrypted', 'index'),
    ({'encrypted_store': False, 'store_type': 'niofs'}, 'niofs', None),
]


def render(relpath, params):
    with open(os.path.join(GEO, relpath), encoding='utf-8') as f:
        src = f.read()
    env = jinja2.Environment(undefined=jinja2.Undefined)
    return json.loads(env.from_string(src).render(**dict(BASE, **params)))


@unittest.skipIf(jinja2 is None, '没装 jinja2（跑赛道的机器上会执行）')
class RealRenderTest(unittest.TestCase):
    def test_index_json_renders_valid_json_in_every_switch_state(self):
        for params, store_type, profile in CASES:
            with self.subTest(params=params):
                st = render('index.json', params)['settings']
                self.assertEqual(st.get('index.store.type'), store_type)
                self.assertEqual(st.get('index.encryption.profile'), profile)

    def test_encrypted_keys_are_top_level_point_keys(self):
        """嵌进 index 对象会变成 index.index.store.type，建索引直接失败。"""
        st = render('index.json', {'encrypted_store': True})['settings']
        self.assertNotIn('store.type', st.get('index') or {})
        self.assertNotIn('encryption.profile', st.get('index') or {})

    def test_challenge_renders_and_encryption_overrides_index_settings(self):
        """index_settings 里若也写了 store.type，加密键必须压过它（update 后置）。

        不能靠"输出里键的先后"判断 —— jinja2 的 tojson 按字母序输出，
        `index.encryption.profile` 排在 `index.store.type` 前面，与 update 顺序无关。
        """
        cases = ({}, {'encrypted_store': True},
                 {'index_settings': {'index.store.type': 'niofs', 'index.codec': 'zstd'}})
        for params in cases:
            with self.subTest(params=params):
                doc = render('challenges/encrypted-store.json', params)
                creates = [t for t in doc['schedule']
                           if isinstance(t.get('operation'), dict)
                           and t['operation'].get('operation-type') == 'create-index']
                self.assertEqual(len(creates), 1)
                settings = creates[0]['operation']['settings']
                for k, v in ENC.items():
                    self.assertEqual(settings.get(k), v, '%s 没烘进去' % k)
                # 带 index_settings 时：非加密键保留，store.type 被加密压掉
                if 'index_settings' in params:
                    self.assertEqual(settings.get('index.codec'), 'zstd')

    def test_encrypted_store_snapshot_challenge_renders_valid_json(self):
        """encrypted-store-snapshot 挑战：真 Jinja2 渲染必须成合法 JSON（含 profile 开关两态）。"""
        cases = ({}, {'snapshot_repo_profile': 'snapshot'},
                 {'index_settings': {'index.store.type': 'niofs', 'index.codec': 'zstd'}})
        for params in cases:
            with self.subTest(params=params):
                doc = render('challenges/encrypted-store-snapshot.json', params)
                tasks = {t.get('name'): t for t in doc['schedule'] if t.get('name')}
                creates = [t for t in doc['schedule']
                           if isinstance(t.get('operation'), dict)
                           and t['operation'].get('operation-type') == 'create-index']
                settings = creates[0]['operation']['settings']
                for k, v in ENC.items():
                    self.assertEqual(settings.get(k), v, '%s 没烘进去' % k)
                if 'index_settings' in params:
                    self.assertEqual(settings.get('index.codec'), 'zstd')
                repo = tasks['create-repository']['operation']['body']
                self.assertEqual(repo['type'], 'encrypted')
                self.assertEqual(repo['settings']['delegate_type'], 'fs')
                self.assertEqual(repo['settings']['location'],
                                 '/tmp/raceway-snapshot-repo')
                self.assertEqual('encryption.profile' in repo['settings'],
                                 'snapshot_repo_profile' in params)
                for tname in ('create-snapshot', 'restore-snapshot'):
                    op = tasks[tname]['operation']
                    self.assertEqual(op['repository'], 'raceway-encrypted')
                    self.assertEqual(op['snapshot'], 'geonames-enc')
                    self.assertFalse(op['wait-for-completion'])
                    self.assertEqual(op['body']['indices'], 'geonames')
                    self.assertIs(op['body']['include_global_state'], False)


if __name__ == '__main__':
    unittest.main()
