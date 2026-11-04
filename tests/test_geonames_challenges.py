#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geonames 的 write-only / query-only 必须与上游定版定义同源（回归闸门）

背景：`tracks/geonames/challenges/{write-only,query-only}.json` 是从上游定版
`challenges/default.json` 的 `append-no-conflicts` 挑战**机械派生**出来的
（写段 = 建索引 + forcemerge 单段 + 等合并；查段 = 其余查询操作）。
机械派生 = 可复现 = 能被测试盯住：上游定义一改，这里就红，红了就得重新派生。

派生规则（与 tracks/nested/challenges/ 同构）：

· 写/查分界 = `wait-until-merges-finish`（最后一个写段操作）

· 唯一口径改动 = force-merge 的 max-num-segments 由上游的可选参数改为
  `{{max_num_segments | default(1)}}`（raceway 口径 = 强制单段）

· Jinja 控制标签（`{% if %}`）剥掉 → 各自文件里是静态定义；唯独 `post_ingest_sleep`
  保留条件块（它是默认关的参数开关，固化就变成"每次都睡 30s"）

· 逗号归属：普通 op 的逗号在 op 之后；条件块的逗号在块内
  （条件为假时整块消失且不留悬空逗号）

跑法：
    python3 -m unittest discover -s tests -t . -v
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH = os.path.join(ROOT, 'tracks', 'geonames', 'challenges')
BASE_CHALLENGE = 'append-no-conflicts'   # 上游 geonames 的写+查主挑战


# ---- 上游文本解析（与 vm-bench-lib.sh 的 derive_full_challenge 同套扫描逻辑）----
def arr_inner(text, j):
    """返回 '[' 之后到配对 ']' 之前的内层文本。"""
    depth, in_str, esc, i = 0, False, False, j
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    return text[j + 1:i]
        i += 1
    return None


def named_schedule(text, want):
    m = re.search(r'"name"\s*:\s*"%s"' % re.escape(want), text)
    if not m:
        return None
    i = text.find('"schedule"', m.end())
    if i < 0:
        return None
    j = text.find('[', i)
    if j < 0:
        return None
    return arr_inner(text, j)


def split_top_level_ops(content):
    """按顶层 { } 切出每个操作对象。"""
    ops, i, n = [], 0, len(content)
    while i < n:
        c = content[i]
        if c in ' \t\r\n,':
            i += 1
            continue
        if c != '{':
            i += 1
            continue
        start = i
        depth, in_str, esc = 0, False, False
        while i < n:
            ch = content[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in '[{':
                    depth += 1
                elif ch in ']}':
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
            i += 1
        ops.append(content[start:i])
    return ops


def clean(op):
    """去掉 Jinja 控制标签与注释；max_num_segments 默认 1（强制单段）。"""
    op = op.replace('{{max_num_segments}}', '{{max_num_segments | default(1)}}')
    op = re.sub(r'\{%-?.*?-?%\}', '', op, flags=re.S)
    op = re.sub(r'\{#.*?#\}', '', op, flags=re.S)
    op = re.sub(r'[ \t]+\n', '\n', op)
    op = re.sub(r'\n\s*\n', '\n', op)
    return op.strip()


def _read(name):
    """读文件并及时关闭（到处 open().read() 会刷 ResourceWarning）。"""
    with open(os.path.join(CH, name), encoding='utf-8') as f:
        return f.read()


def derive():
    """从上游定义派生 (write-only 文本, query-only 文本)。"""
    txt = _read('default.json')
    content = named_schedule(txt, BASE_CHALLENGE)
    assert content is not None, '上游定义里找不到挑战 %s' % BASE_CHALLENGE
    raw_ops = split_top_level_ops(content)

    cut = None
    for k, op in enumerate(raw_ops):
        if 'wait-until-merges-finish' in op:
            cut = k
            break
    assert cut is not None, '找不到 wait-until-merges-finish，无法定写/查分界'

    def keep(ops):
        out = []
        for o in ops:
            c = clean(o)
            if not c:
                assert '"' not in o, '操作清空但含 JSON 内容，说明清理过头：%s' % o[:200]
                continue
            out.append(c)
        return out

    write_ops = keep(raw_ops[:cut + 1])
    query_ops = keep(raw_ops[cut + 1:])

    # post_ingest_sleep 是上游的服务端less调参开关（默认关，runner 从不传）
    # → 必须保留条件块，不能固化成"每次都睡 30s"
    for i, o in enumerate(query_ops):
        if 'post-ingest-sleep' in o:
            query_ops[i] = ('cond', o)
            break

    def emit(name, description, ops):
        parts = []
        for i, o in enumerate(ops):
            last = (i == len(ops) - 1)
            if isinstance(o, tuple):
                assert not last, '条件块不能是最后一个操作（逗号无处安放）'
                parts.append('        {%% if post_ingest_sleep|default(false) %%}\n'
                             '        %s,\n'
                             '        {%% endif %%}' % o[1])
            else:
                parts.append('        ' + o + ('' if last else ','))
        body = '\n'.join(parts)
        return ('    {\n'
                '      "name": "%s",\n'
                '      "description": "%s",\n'
                '      "schedule": [\n%s\n      ]\n'
                '    }\n' % (name, description, body))

    w = emit('write-only',
             'Indexes the full geonames corpus and force-merges to one segment.',
             write_ops)
    q = emit('query-only',
             'Runs the geonames query set against an already indexed corpus.',
             query_ops)
    return w, q


def render(text, cond):
    """模拟 rally 的 Jinja 渲染：条件块按 cond 取/舍，占位符换 X。"""
    if cond:
        text = re.sub(r'\{%-?\s*if\s+post_ingest_sleep[^\}]*?-?%\}(.*?)\{%-?\s*endif\s*-?%\}',
                      lambda m: m.group(1), text, flags=re.S)
    else:
        text = re.sub(r'\{%-?\s*if\s+post_ingest_sleep.*?\{%-?\s*endif\s*-?%\}',
                      '', text, flags=re.S)
    text = re.sub(r'"\{\{.*?\}\}"', '"X"', text, flags=re.S)
    text = re.sub(r'\{\{.*?\}\}', '"X"', text, flags=re.S)
    return text


class TestGeonamesChallenges(unittest.TestCase):
    """派生结果必须与仓库里的定版文件一致，且与上游同序。"""

    def test_committed_files_match_derivation(self):
        """库里的 write-only / query-only 必须等于从上游机械派生的结果。"""
        w, q = derive()
        for name, want in (('write-only.json', w), ('query-only.json', q)):
            p = os.path.join(CH, name)
            self.assertTrue(os.path.isfile(p), '缺少定版挑战文件: %s' % p)
            got = _read(name)
            self.assertEqual(got, want,
                             '%s 与上游机械派生的结果不一致 —— 上游定义变了？'
                             '重新派生后再提交' % name)

    def test_schedule_covers_upstream_ops_in_order(self):
        """写段 + 查段 = 上游 append-no-conflicts 的操作序列（逐项同序）。"""
        up = named_schedule(_read('default.json'), BASE_CHALLENGE)
        w = named_schedule(_read('write-only.json'), 'write-only')
        q = named_schedule(_read('query-only.json'), 'query-only')

        def ops_of(arr_text):
            return re.findall(r'"operation-type"\s*:\s*"([^"]+)"|"operation"\s*:\s*"([^"]+)"',
                              arr_text)

        up_ops = [a or b for a, b in ops_of(render(up, False))]
        der_ops = [a or b for a, b in ops_of(render(w, False) + ',' + render(q, False))]
        self.assertEqual(len(up_ops), len(der_ops), '操作数对不上')
        self.assertEqual(up_ops, der_ops, '派生序列与上游不一致')

    def test_both_render_to_valid_json(self):
        """两种条件状态下，两份挑战都要能渲染成合法 JSON（rally 才吃得下）。"""
        for name, nm in (('write-only.json', 'write-only'), ('query-only.json', 'query-only')):
            raw = named_schedule(_read(name), nm)
            for cond in (False, True):
                try:
                    arr = json.loads('[%s]' % render(raw, cond))
                except Exception as e:
                    self.fail('%s 在 cond=%s 时渲染失败: %s' % (name, cond, e))
                self.assertTrue(arr, '%s 渲染出空 schedule' % name)

    def test_force_merge_is_single_segment(self):
        """raceway 口径 = force-merge 到 1 段（上游的可选参数在这里默认 1）。"""
        w = _read('write-only.json')
        self.assertIn('"max-num-segments": {{max_num_segments | default(1)}}', w)
        self.assertIn('"operation-type": "force-merge"', w)
        self.assertIn('wait-until-merges-finish', w)

    def test_post_ingest_sleep_stays_conditional(self):
        """post_ingest_sleep 必须仍是条件块（默认关），不能固化成每次都睡。"""
        q = _read('query-only.json')
        self.assertIn('{% if post_ingest_sleep|default(false) %}', q)
        self.assertIn('post-ingest-sleep', q)


if __name__ == '__main__':
    unittest.main()
