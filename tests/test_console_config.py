#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""console.conf 解析契约：控制台默认值（外链清单 / 入库参数）从配置文件读取。

守三件事：
  1. extlink.<名称> 解析：名称可含中文/空格，全取第一个点之后的部分
  2. apply.* 只认白名单键，别的命名空间忽略；auth 存在即视为"已配置"，
     且 console_config_defaults 绝不把 auth 回传给前端（不下发浏览器）
  3. 没有配置文件 → 空结构 + file=False，前端落内置默认（控制台照常可用）
"""
import importlib
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

C = importlib.import_module('bench-compose')  # noqa: E402


class TestConsoleConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='raceway-console-conf-')

    def _conf(self, text):
        with open(os.path.join(self.tmp, 'console.conf'), 'w', encoding='utf-8') as f:
            f.write(text)

    def test_parse_extlinks_names_with_spaces_and_cjk(self):
        self._conf(
            '# 注释行\n'
            'extlink.Kibana = http://k:5601\n'
            'extlink.LogPilot=http://lp:8802\n'
            'extlink.趋势大屏 A = http://grafana:3000/d/r\n'
            '\n'
            'extlink. = http://无名称被忽略\n'
            'extlink.空URL =\n')
        cc = C.load_console_config(self.tmp)
        self.assertTrue(cc['file'])
        self.assertEqual([(x['name'], x['url']) for x in cc['ext_links']], [
            ('Kibana', 'http://k:5601'),
            ('LogPilot', 'http://lp:8802'),
            ('趋势大屏 A', 'http://grafana:3000/d/r'),
        ])

    def test_parse_apply_whitelist_and_secret_not_echoed(self):
        self._conf(
            'apply.es = https://127.0.0.1:9237\n'
            'apply.auth = admin:secret\n'
            'apply.insecure = 1\n'
            'apply.drop = 0\n'
            'apply.unknown = x\n'
            'other.key = y\n')
        cc = C.load_console_config(self.tmp)
        self.assertEqual(cc['apply'], {'es': 'https://127.0.0.1:9237',
                                       'auth': 'admin:secret',
                                       'insecure': '1', 'drop': '0'})
        pub = C.console_config_defaults(cc)
        # 口令绝不进 bootstrap 载荷
        self.assertNotIn('auth', pub['apply_defaults'])
        self.assertTrue(pub['apply_auth_configured'])
        self.assertEqual(pub['apply_defaults'], {'es': 'https://127.0.0.1:9237',
                                                 'insecure': '1', 'drop': '0'})

    def test_missing_file_is_empty_and_usable(self):
        cc = C.load_console_config(self.tmp)
        self.assertEqual(cc, {'ext_links': [], 'apply': {}, 'file': False})
        pub = C.console_config_defaults(cc)
        self.assertEqual(pub['ext_links'], [])
        self.assertEqual(pub['apply_defaults'], {})
        self.assertFalse(pub['apply_auth_configured'])
        self.assertFalse(pub['console_conf'])

    def test_example_template_parses_clean(self):
        """console.conf.example 的示例键（取消注释后）都必须被解析器接受（防模板漂移）。"""
        example = pathlib.Path(__file__).resolve().parents[1] / 'console.conf.example'
        # 模板默认全注释；把每个示例行（含 extlink./apply. 前缀的）取消注释后校验
        lines = []
        for l in example.read_text(encoding='utf-8').splitlines():
            l = l.strip()
            if l.startswith('#'):
                l = l[1:].strip()
            if l.startswith(('extlink.', 'apply.')):
                lines.append(l)
        self.assertTrue(lines, '模板里应有 extlink./apply. 示例键行')
        for l in lines:
            k = l.partition('=')[0].strip()
            if k.startswith('apply.'):
                self.assertIn(k.partition('.')[2], C._CONSOLE_CONF_KEYS,
                              '模板键不在白名单: %r' % k)


if __name__ == '__main__':
    unittest.main()
