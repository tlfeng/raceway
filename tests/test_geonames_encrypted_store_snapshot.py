#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geonames encrypted-store-snapshot 挑战（索引加密 + 快照加密全链路）回归闸门

背景：`tracks/geonames/challenges/encrypted-store-snapshot.json` 在 encrypted-store
（索引加密读写探针）之上加**加密仓库快照闭环**：delete-snapshot-repository 预清场
（幂等重跑）→ 注册 type=encrypted 仓库（委托 fs）→ create-snapshot（异步）→
wait-for-snapshot-create（快照字节数/吞吐）→ 删原索引 → restore（异步）→
wait-for-recovery（恢复字节数/吞吐）→ 健康检查 → 查段跑在恢复出的索引上。

为什么是闸门（与 test_geonames_encrypted_store 同款教训）：rally 对快照族操作
（create-snapshot-repository / create-snapshot / wait-for-snapshot-create /
restore-snapshot / wait-for-recovery，esrally 2.13 builtin）的参数校验极松 ——
repository/snapshot 名字抄错、wait-for-completion 忘了写 false、删索引不在恢复前，
都会静默跑偏（比如 restore 因索引同名而失败、快照吞吐测不到），这里在起跑前挡住。

跑法：
    python3 -m unittest tests.test_geonames_encrypted_store_snapshot -v
     （或 python3 -m unittest discover -s tests -t . -v）
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEO = os.path.join(ROOT, 'tracks', 'geonames')
CHALLENGE = os.path.join(GEO, 'challenges', 'encrypted-store-snapshot.json')

ENC_SETTINGS = {
    'index.store.type': 'encrypted',
    'index.encryption.profile': 'index',
}

SNAP_PHASE = [
    'delete-snapshot-repository',
    'create-repository',
    'create-snapshot',
    'wait-for-snapshot-create',
    'delete-index-before-restore',
    'restore-snapshot',
    'wait-for-recovery',
    'check-cluster-health-after-restore',
]


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
    """挑战的操作序列（有效名 = 显式 name，否则 operation 引用）。"""
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


def task_by_name(doc, name):
    return [t for t in doc['schedule'] if t.get('name') == name]


class EncryptedStoreSnapshotChallengeTest(unittest.TestCase):
    def setUp(self):
        self.doc = json_after_masking(strip_jinja_stmt(_read(CHALLENGE)))
        self.ops = challenge_schedule_ops()[1]

    def test_challenge_is_parseable_and_named(self):
        self.assertEqual(self.doc['name'], 'encrypted-store-snapshot')
        self.assertIn('snapshot', self.doc['description'])
        self.assertTrue(self.doc['schedule'], 'schedule 为空')

    def test_create_index_bakes_encrypted_settings(self):
        """create-index 必须烘入加密设置，且加密 update 必须最后（后置必胜，压过 index_settings）。"""
        creates = [t for t in self.doc['schedule']
                   if isinstance(t.get('operation'), dict)
                   and t['operation'].get('operation-type') == 'create-index']
        self.assertEqual(len(creates), 1, '应只有一个 create-index')
        src = _read(CHALLENGE)
        updates = re.findall(r'p_enc_settings\.update\((\{.*?\})\)', src, flags=re.S)
        self.assertTrue(updates, 'create-index 里没有 p_enc_settings.update 链')
        last = json.loads(updates[-1])
        self.assertEqual(last, ENC_SETTINGS,
                         '最后一个 update 不是加密两键（加密会被 index_settings 压掉）')
        self.assertIn('{{ p_enc_settings | tojson }}', src)

    def test_write_segment_matches_write_only(self):
        """写段与 write-only 逐项同序（仅 create-index 的 settings 不同，已单独断言）。"""
        want = base_challenge_ops('write-only.json', 'write-only')
        got = self.ops[:len(want)]
        self.assertEqual(want, got, '写段操作序列与 write-only 漂移了')

    def test_query_segment_matches_query_only(self):
        """查段与 query-only 逐项同序同预算，且必须是挑战的收尾（跑在恢复出的索引上）。"""
        want = base_challenge_ops('query-only.json', 'query-only')
        got = self.ops[-len(want):]
        self.assertEqual(want, got, '查段操作序列与 query-only 漂移了')

    def test_snapshot_phase_order(self):
        """快照闭环的 8 个任务必须按预设顺序出现在写段与查段之间。"""
        i0 = self.ops.index(SNAP_PHASE[0])
        got = self.ops[i0:i0 + len(SNAP_PHASE)]
        self.assertEqual(SNAP_PHASE, got,
                         '快照闭环任务顺序漂移了（删原索引必须在 restore 之前）')

    def test_repository_and_snapshot_names_are_consistent(self):
        """5 个快照族任务的 repository 引用必须同源；create/restore 的 snapshot 名必须同源。"""
        src = _read(CHALLENGE)
        repo_expr = '{{snapshot_repository | default(\'raceway-encrypted\')}}'
        self.assertEqual(src.count(repo_expr), 5,
                         'repository 占位不是 5 处（4 个任务 + body 不引用 repo）')
        snap_expr = '{{snapshot_name | default(\'geonames-enc\')}}'
        self.assertEqual(src.count(snap_expr), 2,
                         'snapshot 名占位必须是 create-snapshot / restore-snapshot 各 1 处')

    def test_create_repository_body(self):
        """注册的是 type=encrypted 仓库：委托类型/location 走参数（遮罩后为 X）。"""
        t = task_by_name(self.doc, 'create-repository')[0]['operation']
        self.assertEqual(t['body']['type'], 'encrypted', '仓库 type 必须是 encrypted（快照加密）')
        st = t['body']['settings']
        self.assertEqual(st.get('delegate_type'), 'X')
        self.assertEqual(st.get('location'), 'X')

    def test_profile_is_optional_conditional(self):
        """encryption.profile 只在显式传 snapshot_repo_profile 时渲染（2.5.1 硬化版需要）。"""
        src = _read(CHALLENGE)
        hits = re.findall(r'snapshot_repo_profile', src)
        self.assertEqual(len(hits), 3,
                         'snapshot_repo_profile 应恰好出现 3 次（if 守卫 2 次 + 值 1 次）')
        self.assertIn('{%- if snapshot_repo_profile is defined and snapshot_repo_profile %}', src)
        self.assertIn('"encryption.profile": "{{snapshot_repo_profile}}"', src)

    def test_snapshot_and_restore_are_async_with_followup_waits(self):
        """快照/恢复都异步发（wait-for-completion=false）+ 显式等待任务收口（吞吐才有数据）。"""
        for name, op_type, wait in (('create-snapshot', 'create-snapshot',
                                     'wait-for-snapshot-create'),
                                    ('restore-snapshot', 'restore-snapshot',
                                     'wait-for-recovery')):
            t = task_by_name(self.doc, name)[0]['operation']
            self.assertEqual(t['operation-type'], op_type)
            self.assertFalse(t.get('wait-for-completion', True),
                             '%s 必须异步（wait-for-completion: false），否则吞吐测不到' % name)
            self.assertEqual(t.get('request-timeout'), 7200)
            waits = task_by_name(self.doc, wait)
            self.assertEqual(len(waits), 1, '缺显式等待任务 %s' % wait)

    def test_pre_clean_before_register(self):
        """delete-snapshot-repository 必须先于 create-repository（重跑幂等；其自身 ignore 404）。"""
        self.assertLess(self.ops.index('delete-snapshot-repository'),
                        self.ops.index('create-repository'))

    def test_delete_index_before_restore(self):
        """原索引必须先删再恢复（恢复同名索引会被拒）。"""
        self.assertLess(self.ops.index('delete-index-before-restore'),
                        self.ops.index('restore-snapshot'))

    def test_snapshot_body_excludes_global_state(self):
        """create/restore 的 body 必须 include_global_state=false（恢复全局状态会撞系统索引）。"""
        for name in ('create-snapshot', 'restore-snapshot'):
            body = task_by_name(self.doc, name)[0]['operation']['body']
            self.assertEqual(body.get('indices'), 'geonames')
            self.assertIs(body.get('include_global_state'), False)


if __name__ == '__main__':
    unittest.main()
