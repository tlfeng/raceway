#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台 API 契约：前端 ↔ mock ↔ bench-compose 三方一致。

web/ 前端是手写 JS，没有构建期类型检查；这里用最朴素的文本扫描把三方对齐：
  1. 前端引用的每个 /api/* 都在 bench-compose.py 的路由层存在（防前端调了已删/改名接口）
  2. 前端引用的每个 /api/* 都被 tests/mock_console.py stub（防 mock 缺口导致演示/截图时 404）
  3. mock 不发明 bench-compose.py 没有的接口（mock 的响应形状才有人维护）

扫描的是路由字面量（`'/api/...'`），与 serve 的 _route_get_core/_post_core 的
`path == '...'` 判等写法对齐；接口的返回形状另有 test_dashboard_contract 等把守。
"""
import os
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

FRONTEND_FILES = ('web/app.js', 'web/extras.js', 'web/legacy.html')


def api_refs(relpath):
    text = (ROOT / relpath).read_text(encoding='utf-8')
    return set(re.findall(r'/api/[A-Za-z0-9_-]+', text))


def frontend_apis():
    out = set()
    for name in FRONTEND_FILES:
        out |= api_refs(name)
    return out


class TestWebApiContract(unittest.TestCase):
    def test_frontend_routes_exist_in_backend(self):
        missing = sorted(frontend_apis() - api_refs('bench-compose.py'))
        self.assertFalse(missing, '前端引用了 bench-compose.py 路由层没有的接口: %s' % missing)

    def test_mock_covers_frontend_routes(self):
        missing = sorted(frontend_apis() - api_refs('tests/mock_console.py'))
        self.assertFalse(missing, 'mock_console.py 缺前端要用到的接口（演示/截图会 404）: %s' % missing)

    def test_mock_invents_nothing(self):
        extra = sorted(api_refs('tests/mock_console.py') - api_refs('bench-compose.py'))
        self.assertFalse(extra, 'mock 实现了 bench-compose.py 没有的接口: %s' % extra)


if __name__ == '__main__':
    unittest.main()
