#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-ops.py —— 控制台「写操作 + 目录列举」的远端辅助程序（Go/Python 共用）。

设计：只执行这一份固定脚本，参数与文件内容经 JSON 从 stdin 传入，不拼进 shell argv。
Go 后端（serve-go）与 Python 后端（bench-compose.py serve）都通过 exec 调它，
因此两条路径的写操作语义完全一致；runner 的 flock 仍是最终互斥边界。

用法（由后端 exec）：
  python3 bench-ops.py <mode> < payload.json

支持的 mode：
  track-list / track-read / track-write / track-delete
  export-gen / close

安全：所有路径限制在仓库根 tracks/ 内、拒绝 .. 与符号链接、写操作用临时文件 +
fsync + os.replace 原子发布，带 sha256 期望值防并发覆盖。
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

root = pathlib.Path.cwd().resolve()
results = pathlib.Path(os.environ.get('RESULTS_ROOT') or (root / 'campaigns'))
if not results.is_absolute():
    results = root / results
results = results.resolve()


def fail(message):
    raise RuntimeError(message)


def safe_track_path(rel):
    if not isinstance(rel, str) or not rel or len(rel) > 200:
        fail('非法赛道路径')
    if '\\' in rel or rel.startswith('/') or '\0' in rel:
        fail('非法赛道路径')
    parts = rel.split('/')
    if any(x in ('', '.', '..') for x in parts):
        fail('赛道路径不能含空段、. 或 ..')
    if any(not re.fullmatch(r'[A-Za-z0-9._-]+', x) for x in parts):
        fail('赛道路径只允许字母数字与 ._-')
    tracks = (root / 'tracks').resolve()
    fp = (tracks / rel).resolve()
    if fp != tracks and tracks not in fp.parents:
        fail('赛道路径越界')
    if fp.is_symlink():
        fail('赛道文件不允许符号链接')
    return fp


def run(payload):
    mode = sys.argv[1]
    if mode == 'track-list':
        base = root / 'tracks'
        if not base.is_dir():
            fail('仓库下没有 tracks/ 目录')
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fn in filenames:
                if fn.startswith('.'):
                    continue
                fp = pathlib.Path(dirpath) / fn
                if fp.is_symlink():
                    continue
                st = fp.stat()
                out.append({'path': fp.relative_to(base).as_posix(),
                            'size': st.st_size, 'mtime': int(st.st_mtime)})
        out.sort(key=lambda x: x['path'])
        return {'files': out}

    if mode == 'track-read':
        fp = safe_track_path(payload['path'])
        if not fp.is_file():
            fail('赛道文件不存在')
        return {'path': payload['path'],
                'content': fp.read_text(encoding='utf-8', errors='replace'),
                'sha256': hashlib.sha256(fp.read_bytes()).hexdigest()}

    if mode == 'track-write':
        fp = safe_track_path(payload['path'])
        if fp.exists() and not fp.is_file():
            fail('目标不是普通文件')
        content = payload['content']
        if not isinstance(content, str):
            fail('文件内容必须是文本')
        old = fp.read_text(encoding='utf-8', errors='replace') if fp.is_file() else None
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if old == content:
            return {'path': payload['path'], 'sha256': digest, 'unchanged': True}
        expect = payload.get('expect')
        if expect and expect != (hashlib.sha256(old.encode('utf-8')).hexdigest()
                                 if old is not None else None):
            fail('文件内容已被他人修改，请刷新后重试（期望摘要不匹配）')
        fp.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.track-', dir=str(fp.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, fp)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return {'path': payload['path'], 'sha256': digest, 'unchanged': False}

    if mode == 'track-delete':
        fp = safe_track_path(payload['path'])
        if not fp.is_file():
            fail('赛道文件不存在')
        old = fp.read_text(encoding='utf-8', errors='replace')
        expect = payload.get('expect')
        if expect and expect != hashlib.sha256(old.encode('utf-8')).hexdigest():
            fail('文件内容已变更，请刷新后重试（期望摘要不匹配）')
        fp.unlink()
        return {'path': payload['path'], 'deleted': True}

    if mode == 'export-gen':
        import shutil
        tag = payload['tag']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', tag):
            fail('非法导出 tag')
        cids = payload.get('campaigns') or []
        if not isinstance(cids, list) or not cids:
            fail('请至少选择一个 campaign')
        for cid in cids:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', cid):
                fail('非法 campaign 名')
            if not (results / cid).is_dir():
                fail('campaign 不存在: %s' % cid)
        export_root = root / 'exports' / tag
        if export_root.exists():
            fail('导出 tag 已存在: %s —— 换一个 tag，已归档导出不覆盖' % tag)
        staging = root / 'exports' / ('.gen-' + tag)
        if staging.exists():
            fail('上一次生成残留: %s —— 请先清理' % staging)
        store = staging / 'race-store'
        camps = staging / 'campaigns'
        try:
            staging.mkdir(parents=True)
            camps.mkdir()
            for cid in cids:
                os.symlink(results / cid, camps / cid)
            env = dict(os.environ, RESULTS_ROOT=str(results))
            r1 = subprocess.run([sys.executable, str(root / 'build-race-store.py'),
                                 '--campaigns', str(camps), '--out', str(store)],
                                cwd=str(root), env=env, capture_output=True, text=True, timeout=600)
            if r1.returncode != 0:
                fail('build-race-store 失败(rc=%d): %s' % (r1.returncode, (r1.stderr or r1.stdout)[-1200:]))
            r2 = subprocess.run([sys.executable, str(root / 'bench-export.py'),
                                 '--out', str(staging / 'out'), '--races', str(store),
                                 '--campaigns', str(camps)],
                                cwd=str(root), env=env, capture_output=True, text=True, timeout=600)
            if r2.returncode != 0:
                fail('bench-export 失败(rc=%d): %s' % (r2.returncode, (r2.stderr or r2.stdout)[-1200:]))
            if not (staging / 'out' / 'manifest.json').is_file():
                fail('导出产物缺 manifest.json，拒绝发布')
            os.replace(staging / 'out', export_root)
            shutil.rmtree(staging, ignore_errors=True)
            # manifest 摘要给控制台直接展示关键统计（数字埋在 log 里没人看）
            summary = {}
            try:
                with open(export_root / 'manifest.json', encoding='utf-8') as f:
                    m = json.load(f)
                for k in ('generated_utc', 'races_scanned', 'segments_scanned',
                          'bench_run', 'bench_metric', 'bench_env', 'system_rows'):
                    if m.get(k) is not None:
                        summary[k] = m[k]
                orphans = m.get('monitors_orphan_fids') or {}
                if orphans:
                    summary['monitors_orphan_cids'] = len(orphans)
                    summary['monitors_orphan_fids'] = sum(len(v) for v in orphans.values())
            except (OSError, ValueError):
                pass
            return {'tag': tag, 'campaigns': cids, 'summary': summary,
                    'log': (r1.stdout + r2.stdout).strip()[-2000:]}
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    if mode == 'close':
        cid = payload.get('cid') or ''
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', cid):
            fail('非法 campaign 名')
        dry = payload.get('dry_run', True)
        cmd = ['bash', str(root / 'close-campaign.sh')]
        if dry:
            cmd.append('--dry-run')
        cmd += ['--reason', payload.get('reason') or '控制台收尾', cid]
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=120)
        return {'result': (r.stdout or '') + (r.stderr or ''), 'rc': r.returncode,
                'dry_run': bool(dry)}

    fail('未知 mode: %s' % mode)


try:
    data = json.load(sys.stdin)
    print(json.dumps(run(data), ensure_ascii=False))
except Exception as e:
    print(json.dumps({'error': '%s: %s' % (type(e).__name__, e)}, ensure_ascii=False))
    sys.exit(1)
