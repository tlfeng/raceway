#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-tools.py —— 扩展功能的命令行入口（与 Web / Go 后端共用同一套逻辑）。

raceway 的基础能力一直是命令行的：run-bench.sh 编排、car-compare 对比、
build-race-store→bench-export→bench-apply 入库。控制台新增的能力也不该锁在页面里 ——
本脚本直接复用 bench-compose.py 的 api_* 函数，让每个新能力都能从 shell 使用，
本地与 --ssh 远端都支持。三通道（CLI / Python serve / Go 后端）语义一致。

用法：
  # 只读（本地或 --ssh 远端，输出 JSON，可管道给 jq）
  python3 bench-tools.py sidecar  <cid> <fid>
  python3 bench-tools.py detail   <cid> <fid>
  python3 bench-tools.py monitor  <cid> <fid>
  python3 bench-tools.py log      <cid> <相对路径> [--n N] [--q 关键字]
  python3 bench-tools.py report   <cid> [--fmt md|html] [-o 文件]
  python3 bench-tools.py est      <track>
  python3 bench-tools.py track-ls

  # 写操作（track 改动只影响仓库文件；--ssh 时作用于远端仓库）
  python3 bench-tools.py track-read  <相对路径> [-o 文件]
  python3 bench-tools.py track-write <相对路径> --content 文本 | --file 文件 [--expect sha256]
  python3 bench-tools.py track-del   <相对路径> [--expect sha256]
  python3 bench-tools.py export-gen  --tag TAG <cid> [<cid> ...]
  python3 bench-tools.py close       <cid> [--run] [--reason 文本]   # 默认 dry-run，--run 才真正执行
  python3 bench-tools.py result      <cid>          # 场内速览：逐场次判定 + 逐任务吞吐/延迟
  python3 bench-tools.py mutex                      # 集群占用预检（.runner.lock，只读）
  python3 bench-tools.py stop        <cid> --yes    # 停止远端 runner/esrally（必须显式 --yes）
  python3 bench-tools.py cars-validate [--cars-file F]   # 车定义 runner 同口径校验（不写盘）

全局参数：--root DIR  --ssh user@host  --results-root DIR  --raw（文本原样输出）
说明：
  - report / track-read 默认原样输出文本（便于重定向）；其余输出 JSON。加 --raw 强制原样。
  - close 默认 dry-run，必须显式 --run 才补结束行（与页面二次确认同口径）。
  - stop 是控制台唯一杀进程的操作，必须显式 --yes（对应页面上的二次确认）。
  - 赛道文件写入带 sha256 并发校验：传 --expect 时若文件已被改动会拒绝，避免覆盖他人修改。
"""
import argparse
import importlib
import json
import os
import posixpath
import sys
import threading
import types

C = importlib.import_module('bench-compose')
Q = importlib.import_module('bench-queue')


def build_state(args):
    """构造 api_* 需要的最小 state：不读车定义、不碰指标库。"""
    root = args.root
    if args.ssh and ('\\' in root or not posixpath.isabs(root)):
        raise SystemExit('--ssh 模式下 --root 必须是远端绝对 POSIX 路径')
    # results_root：显式 > 远端 inspect > 本地 <root>/campaigns
    if args.results_root:
        results_root = args.results_root.rstrip('/')
    elif args.ssh:
        results_root = C.remote_operation(args, 'inspect')['results_root']
    else:
        results_root = os.path.join(root, 'campaigns')
    st = types.SimpleNamespace(
        args=args,
        src=Q.Source(root=results_root, ssh=args.ssh),
        results_root=results_root,
        lock=threading.Lock(),       # 与 serve 同口径：写/远端操作串行
        long_lock=threading.Lock(),  # export-gen 等长操作
    )
    return st


def emit(obj, raw=False, raw_text=None):
    if raw and raw_text is not None:
        sys.stdout.write(raw_text if raw_text.endswith('\n') or raw_text == '' else raw_text + '\n')
        return
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def maybe_write(text, out):
    if out:
        with open(out, 'w', encoding='utf-8') as f:
            f.write(text)
        print('已写入 ' + out, file=sys.stderr)
        return False   # 已落盘，不再原样打印
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=[
        'sidecar', 'detail', 'monitor', 'log', 'report', 'est', 'track-ls',
        'track-read', 'track-write', 'track-del', 'export-gen', 'close',
        'result', 'mutex', 'stop', 'cars-validate',
    ])
    ap.add_argument('pos', nargs='*', help='位置参数（各动作不同，见 --help）')
    ap.add_argument('--root', default=None)
    ap.add_argument('--ssh', default=os.environ.get('RACEWAY_SSH'))
    ap.add_argument('--results-root', default=None)
    ap.add_argument('--n', type=int, default=200)
    ap.add_argument('--q', default='')
    ap.add_argument('--fmt', default='md', choices=['md', 'html'])
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--content', default=None)
    ap.add_argument('--file', default=None)
    ap.add_argument('--expect', default=None)
    ap.add_argument('--tag', default=None)
    ap.add_argument('--cars-file', default=None,
                    help='cars-validate 用：车定义文件路径（缺省同 compose 的三级回退）')
    ap.add_argument('--yes', action='store_true', help='stop 真正执行（缺省拒绝，杀进程必须显式确认）')
    ap.add_argument('--run', action='store_true', help='close 真正执行（缺省 dry-run）')
    ap.add_argument('--reason', default='命令行收尾')
    ap.add_argument('--raw', action='store_true', help='文本原样输出')
    args = ap.parse_args(argv)

    if args.root is None:
        here = os.path.dirname(os.path.abspath(__file__))
        args.root = here if os.path.isfile(os.path.join(here, 'run-bench.sh')) else C.DEFAULT_ROOT

    st = build_state(args)
    pos = args.pos
    a = args.action
    exit_code = None   # cars-validate 有 error 时置 1，在 except 块之外统一退出

    try:
        if a == 'sidecar':
            need(pos, 2, 'sidecar <cid> <fid>')
            code, payload = C.api_sidecar(st, pos[0], pos[1])
            check(code, payload)

            def render():
                if not payload.get('sidecars'):
                    return '该场次没有 sidecar 落档。\n'
                out = []
                for s in payload['sidecars']:
                    out.append('== 相位 %s (%s) ==' % (s['phase'], s['file']))
                    out.append(json.dumps(s['data'], ensure_ascii=False, indent=2))
                return '\n'.join(out) + '\n'
            text = render()
            if args.raw or args.out:
                show = maybe_write(text, args.out)
                if show:
                    sys.stdout.write(text)
            else:
                emit(payload)

        elif a == 'detail':
            need(pos, 2, 'detail <cid> <fid>')
            code, payload = C.api_race_detail(st, pos[0], pos[1])
            check(code, payload)
            if args.raw or args.out:
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                show = maybe_write(text, args.out)
                if show:
                    print(text)
            else:
                emit(payload)

        elif a == 'monitor':
            need(pos, 2, 'monitor <cid> <fid>')
            code, payload = C.api_monitor(st, pos[0], pos[1])
            check(code, payload)
            if not payload.get('series'):
                emit(payload)   # 无数据也如实输出
                return
            # 命令行最有用的是把每节点时序导出成 CSV；--out 写文件，否则打印表头+首行预览
            series = payload['series']
            header = ['node'] + list(series[0]['header'])
            rows = []
            for s in series:
                for r in s['rows']:
                    rows.append([s['node']] + [r.get(k, '') for k in series[0]['header']])
            csv_text = ','.join(header) + '\n' + '\n'.join(','.join(str(x) for x in row) for row in rows) + '\n'
            if args.out:
                maybe_write(csv_text, args.out)
            elif args.raw:
                sys.stdout.write(csv_text)
            else:
                emit({'cid': payload['cid'], 'fid': payload['fid'], 'interval': payload['interval'],
                      'nodes': [s['node'] for s in series], 'samples': len(rows),
                      'preview': rows[:3], 'header': header})

        elif a == 'log':
            need(pos, 2, 'log <cid> <相对路径>')
            code, payload = C.api_logfile(st, pos[0], pos[1], args.n, args.q)
            check(code, payload)
            text = '\n'.join(payload['lines']) + ('\n' if payload['lines'] else '')
            if args.raw or args.out:
                show = maybe_write(text, args.out)
                if show:
                    sys.stdout.write(text)
            else:
                emit(payload)

        elif a == 'report':
            need(pos, 1, 'report <cid>')
            code, payload = C.api_report(st, pos[0], args.fmt)
            check(code, payload)
            text = payload['text']
            show = maybe_write(text, args.out) if (args.raw or args.out) else False
            if show or (not args.out and not args.raw):
                # 默认原样打印报告（命令行本意：可管道/重定向）；--json 语义见 --raw 反向
                if not args.out:
                    sys.stdout.write(text if text.endswith('\n') else text + '\n')

        elif a == 'est':
            need(pos, 1, 'est <track>')
            code, payload = C.api_est_suggest(st, pos[0])
            check(code, payload)
            if args.raw or args.out:
                val = '' if payload.get('suggest') is None else str(payload['suggest'])
                show = maybe_write(val + '\n' if val else '', args.out)
                if show and val:
                    print(val)
            else:
                emit(payload)

        elif a == 'track-ls':
            reply = C.api_track_list(st)
            if args.raw or args.out:
                text = '\n'.join(f['path'] for f in reply.get('files', []))
                show = maybe_write(text + '\n' if text else '', args.out)
                if show and text:
                    print(text)
            else:
                emit(reply)

        elif a == 'track-read':
            need(pos, 1, 'track-read <相对路径>')
            reply = C.api_track_read(st, {'path': pos[0]})
            text = reply.get('content', '')
            show = maybe_write(text, args.out) if (args.raw or args.out) else False
            if show or not args.out:
                if not args.out:
                    sys.stdout.write(text)

        elif a == 'track-write':
            need(pos, 1, 'track-write <相对路径> --content 文本|--file 文件')
            if args.content is not None:
                content = args.content
            elif args.file:
                with open(args.file, encoding='utf-8') as f:
                    content = f.read()
            else:
                raise SystemExit('track-write 需要 --content 或 --file')
            payload = {'path': pos[0], 'content': content}
            if args.expect:
                payload['expect'] = args.expect
            emit(C.api_track_write(st, payload))

        elif a == 'track-del':
            need(pos, 1, 'track-del <相对路径>')
            payload = {'path': pos[0]}
            if args.expect:
                payload['expect'] = args.expect
            emit(C.api_track_delete(st, payload))

        elif a == 'export-gen':
            if not args.tag:
                raise SystemExit('export-gen 需要 --tag')
            if not pos:
                raise SystemExit('export-gen 至少需要一个 <cid>')
            emit(C.api_export_gen(st, {'tag': args.tag, 'campaigns': list(pos)}))

        elif a == 'close':
            need(pos, 1, 'close <cid> [--run]')
            emit(C.api_close(st, {'cid': pos[0], 'dry_run': not args.run,
                                  'reason': args.reason}))

        elif a == 'result':
            need(pos, 1, 'result <cid>')
            code, payload = C.api_campaign_result(st, pos[0])
            check(code, payload)
            if args.raw or args.out:
                out = []
                for r in payload.get('rows', []):
                    out.append('== %s (%s)' % (r.get('fid'), r.get('verdict')))
                    for m in r.get('metrics', []):
                        lat = ''
                        if m.get('lat_p99') is not None:
                            lat = ' p99=%s%s' % (m['lat_p99'], m.get('lat_unit') or '')
                        out.append('  %s %s %s %s%s' % (m.get('phase'), m.get('task'),
                                                        m.get('throughput'), m.get('tput_unit') or '', lat))
                text = '\n'.join(out) + ('\n' if out else '')
                show = maybe_write(text, args.out)
                if show:
                    sys.stdout.write(text)
            else:
                emit(payload)

        elif a == 'mutex':
            code, payload = C.api_mutex(st)
            check(code, payload)
            if args.raw:
                sys.stdout.write(('free\n' if payload.get('free')
                                  else 'busy: %s\n' % (payload.get('why') or '')))
            else:
                emit(payload)

        elif a == 'stop':
            need(pos, 1, 'stop <cid> --yes')
            if not args.yes:
                raise SystemExit('stop 会杀掉远端 runner 与 esrally 进程 —— 确认无误请加 --yes'
                                 '（与页面二次确认同口径）')
            code, payload = C.api_stop(st, {'cid': pos[0]})
            check(code, payload)
            if args.raw:
                sys.stdout.write((payload.get('result') or '') + '\n')
            else:
                emit(payload)

        elif a == 'cars-validate':
            # 复用 compose 的车文件定位（env → 仓库默认 → 报错），只读不写
            cargs = argparse.Namespace(**vars(args))
            if cargs.cars_file is None:
                cargs.cars_file = (os.environ.get('RACEWAY_CARS_FILE')
                                   or os.path.join(cargs.root, 'schedules', 'dimsweep-cars.conf'))
                if not os.path.isfile(cargs.cars_file) and not cargs.ssh:
                    _leg = cargs.cars_file.replace('cars.conf', 'arms.conf')
                    if os.path.isfile(_leg):
                        cargs.cars_file = _leg
            if not cargs.ssh and not os.path.isfile(cargs.cars_file):
                raise SystemExit('找不到车定义文件: %s\n用 --cars-file 指定路径，'
                                 '或设环境变量 RACEWAY_CARS_FILE 指向它。' % cargs.cars_file)
            content = C.read_cars_file(cargs)
            pkg_dir = None
            try:
                with open(os.path.join(cargs.root, 'vm-bench.env'), encoding='utf-8') as f:
                    for line in f:
                        s = line.strip()
                        if s.startswith('PKG_DIR='):
                            pkg_dir = s.partition('=')[2].strip().strip('"').strip("'")
                            pkg_dir = pkg_dir.replace('${ROOT}', cargs.root).replace('$ROOT', cargs.root)
                            break
            except OSError:
                pass
            cars, diagnostics = C.parse_cars_strict(content, pkg_dir=pkg_dir)
            errors = [d for d in diagnostics if d.get('severity') == 'error']
            reply = {'path': cargs.cars_file, 'cars': len(cars), 'ok': not errors,
                     'diagnostics': diagnostics}
            if args.raw:
                for d in diagnostics:
                    sys.stdout.write('%s 行%s: %s\n' % (d.get('severity', '?'),
                                                       d.get('line') or '?', d.get('message')))
                if not diagnostics:
                    sys.stdout.write('OK: %d 车，无诊断\n' % len(cars))
            else:
                emit(reply)
            if errors:
                exit_code = 1
    except SystemExit as e:
        # 参数解析（argparse）自身的退出码 0/2 原样放行；业务错误统一加"错误:"前缀，
        # 且不重复抛给顶层（否则消息打印两遍）。
        if e.code in (0, 2) or e.code is None:
            raise
        if str(e):
            print('错误: ' + str(e), file=sys.stderr)
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        print('错误: ' + str(e), file=sys.stderr)
        sys.exit(1)
    if exit_code:
        sys.exit(exit_code)


def need(pos, n, usage):
    if len(pos) < n:
        raise SystemExit('参数不足，用法: ' + usage)


def check(code, payload):
    if code != 200:
        raise SystemExit(payload.get('error') or ('HTTP ' + str(code)))


if __name__ == '__main__':
    main()
