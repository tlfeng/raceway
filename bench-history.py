#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-history.py —— 已入库历史覆盖（指标库 benchmark-result-race 的只读视图）

为什么需要它
------------
编排台（bench-compose）只会排新活：矩阵勾什么就排什么，**不知道哪些格已经跑过**。
不查历史就只能凭记忆避开已做过的组合 —— 结果要么重复跑（白烧机器，还把轮间时序
摊薄的前提搞脏），要么漏掉没跑的那一格。

本模块把指标库 benchmark-result-race 索引读成一张**覆盖表**：

    一格 = (车 car, 赛道 track, 轮次 round)

每格记着：跑过几遍、什么判定、哪个 build、来自哪个 campaign、什么时候。

三个必须守住的口径
------------------
1. **build 隔离**：jdk25@3009 跑过 ≠ jdk25@3015 跑过。跨 build 混线会让结论被
   误读（make_dashboard 就是为此按 series_key 分线的）。覆盖判定必须带 build，
   否则"已跑过"会把不同引擎版本的数据当成同一格。
2. **相位判定**：覆盖必须来自同 campaign、真实 fid/attempt 的一次完整测量；
   不跨测量拼写赛/查赛。当前可排赛道均要求写+查，write-only 只说明写段存在，
   不证明整场完整；单挑战 full 则同时证明写查两段。
3. **只读**：不写 ES、不碰集群、不碰被测引擎。指标库没跑（压测窗口内这是常态）
   时返回 available=False，由调用方降级 —— 不拿空数据当“没跑过”。

凭据
----
basic auth 的口令走 **stdin**（curl --config -），不出现在任何进程的 argv 里；
查询体（不含机密）走 argv。ssh 模式在服务器上 curl，本机模式直连 urllib。

用法
----
    from bench_history import fetch_coverage, plan_cells
    cov = fetch_coverage(ssh='root@203.0.113.10', auth='admin:xxx')
    cov.cells[('jdk25', 'ds-j1024', 1)].verdict     # 'VALID'
    plan_cells(cov, cars, tracks, rounds)           # 编排矩阵里哪些格该排
"""
import base64
import json
import shlex
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass, field, asdict

import importlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    identity_mod = importlib.import_module('identity')
except ImportError:                                                    # 从别处 import 本文件时
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    identity_mod = importlib.import_module('identity')
car_of = identity_mod.car_of      # car_code 与源头 car 标签共用身份规则
runner_track_id = identity_mod.runner_track_id

INDEX = 'benchmark-result-race'
DEFAULT_ES_URL = 'https://127.0.0.1:9237'

# 一次读走的行数上限（不够就再翻页）
PAGE_SIZE = 2000

# 覆盖与测量身份所需字段。track_variant 是 runner 的真实赛道 id，不由 codec 猜。
# fid/attempt 隔离尝试；合并 race 行的 race_id 是缺少源头 fid 时的独立测量键。
_SOURCE_FIELDS = ['campaign', 'car_code', 'engine', 'version', 'engine_build',
                  'track', 'track_variant', 'track_raw', 'round',
                  '@timestamp', 'dims', 'pkg', 'race_id', 'fid', 'attempt', 'source',
                  'challenge', 'codec', 'source_reuse', 'knn_index_type',
                  'series_key', 'comparison_key', 'jdk_vendor', 'jdk_version', 'trusted',
                  'status', 'quality', 'phase', 'user_tags']


def row_build_key(r):
    """一行的 build 标识：`车@build`（无 build 号的车就是车名本身，如 es819）。

    索引不存 car_build（那是「车代号」的重复编码），这里用车代号 + engine_build
    合成 —— 与旧的 car_build 字段等价。车代号取顶层 car_code，旧库的行仍从
    user_tags.car 取。
    """
    car = car_of(r)
    b = r.get('engine_build')
    return ('%s@%s' % (car, b)) if (car and b) else (car or '')

# 判定失败的状态（与 bench-queue 的 STATUS_FAILS 对齐，只取库里真会出现的）
FAIL_STATUSES = (
    'ENGINE-FAIL', 'JDK-SWAP-FAIL', 'JDK-FIX-FAIL', 'JDK-ASSERT-FAIL',
    'COLD-START-FAIL', 'CLUSTER-NOT-READY', 'HEAP-ASSERT-FAIL',
    'CPU-ASSERT-FAIL', 'METRIC-NULL-FAIL', 'WRITE-FAIL', 'QUERY-FAIL',
    'SEG-ASSERT-FAIL', 'ENV-FAIL', 'RACE-FAIL',
)
SKIP_STATUSES = ('SKIP', 'DEADLINE-SKIP')

# 独立测量的判定；Cell 优先展示可用的完整测量，不跨测量拼接阶段。
VERDICT_VALID = 'VALID'
VERDICT_DEGRADED = 'DEGRADED'
VERDICT_HALF = 'HALF'      # 只有一个相位跑完
VERDICT_SKIP = 'SKIP'
VERDICT_FAIL = 'FAIL'
VERDICT_UNKNOWN = 'UNKNOWN'


class HistoryUnavailable(Exception):
    """指标库不可用（没起 / 连不上 / 没权限 / 返回不是 JSON）。

    调用方**必须**降级处理：界面要显示"指标库未运行"，而不是把空结果当成
    "什么都没跑过" —— 后者会让编排台把已跑过的场次重排一遍。
    """


# ------------------------------------------------------------------ 变体推导
# 变体 id = <基础赛道>-codec-<def|zstd|bc>[-sr]（与变体目录名一致），按规则推导
# （与压测机 vm-bench-lib.sh 的 track_canonical / track_base 等同一套，
# tests/test_codec_variants.py 会比对两边推导结果）。
#
# 历史短 id（只作兼容输入）：gdef / gzstd(=geo-codec-zstd-sr) / gbc
#
# 覆盖判定必须带变体：库里 track 字段是**基础赛道**（"geo"/"hnsw"/…），
# 压缩与 source_reuse 是另外两个维度。若覆盖键只用 (车, 赛道, 轮次)，6 个变体的
# 历史会混成一格 —— "ZSTD+sr 跑过"会被当成"default 也跑过"，把没跑的格误判成已覆盖。
CODEC_TOKEN = {'default': 'def', 'ZSTD': 'zstd', 'zstd': 'zstd', 'best_compression': 'bc'}
# ↑ 'zstd' 小写别名：入库 codec 列是归一小写（CANON_CODEC），variant_id_of 从列值
#   派生变体 id 时要能命中；'ZSTD' 大写键保留给变体 id 反推路径（compression_of）。
TOKEN_CODEC = {'def': 'default', 'zstd': 'ZSTD', 'bc': 'best_compression'}
CANONICAL_ALIAS = {'gdef': 'geo-codec-def', 'gzstd': 'geo-codec-zstd-sr',
                   'gbc': 'geo-codec-bc'}

# ---- 向量量化变体 --------------------------------------------
#
# hv1024x 家族：hv1024x / hv1024x-int8 / hv1024x-bbq / hv1024x-codec-zstd-sr。
# 变体台账（各变体目录的 VARIANT.txt）写明「基础赛道: hv1024」，但**赛道定义本身**
# 与 hv1024-s3 不同（_source excludes vs enabled），所以库里的 track 存
# **hv1024x**（家族基座，与变体目录名一致），不并进 hv1024 —— 同一规范名下混两套
# 赛道定义比多一个名字更坏（§2.0「归一不出来就不写库」的同一条原则）。
#
# 量化档不是压缩档：它是**向量索引类型**（index_options.type），由 race 行的
# knn_index_type 列承载（hnsw / int8_hnsw / bbq_hnsw）。压缩档仍由 codec /
# source_reuse 承载（§2.6 同一套处理变量口径）。
#
# 覆盖判定必须带这一维，否则基座 / int8 / bbq 三格会并成一格 —— "int8 跑过"会被
# 当成"基座也跑过"，编排台把没跑的格误判成已覆盖（与压缩变体同一类事故）。
QUANT_BY_INDEX_TYPE = {'int8_hnsw': 'int8', 'bbq_hnsw': 'bbq'}
# 变体 id 里的量化后缀 → base_of 剥掉它（hv1024x-int8 → hv1024x）
QUANT_SUFFIXES = ('-int8', '-bbq')
# 独立目录定义的家族成员：-os / -bbq / -disk 是**名字的一部分**（mappings 各不相同，
# 差异表达不成 track-params），不是变体后缀 —— base_of 不得剥（否则 hv1024x-os-bbq
# 被错剥成 hv1024x-os，canon_track 归一不出来，整场被 track_unknown 闸门挡掉）。
DIR_FAMILY_BASES = ('hv1024x-os', 'hv1024x-bbqdisk')


def canonical_id(tid):
    """历史短 id → 规范 id。"""
    return CANONICAL_ALIAS.get(tid, tid)


def is_variant(tid):
    return '-codec-' in canonical_id(tid)


def base_of(tid):
    """变体 id → 基础赛道 id。

    剥两层后缀：压缩档（-codec-...）与向量量化档（-int8 / -bbq）。
    量化档不是压缩档的子集，所以要单独剥 —— 否则 hv1024x-int8 归一不出来，
    整场会被 track_unknown 闸门挡掉。
    独立目录家族（os 族 / bbqdisk）的后缀是名字的一部分，先于量化剥离判断。
    """
    c = canonical_id(tid)
    if '-codec-' in c:
        c = c.split('-codec-', 1)[0]
    for b in DIR_FAMILY_BASES:
        if c == b or c.startswith(b + '-'):
            return c
    for suf in QUANT_SUFFIXES:
        if c.endswith(suf):
            c = c[:-len(suf)]
            break
    return c


def token_of(tid):
    """变体 id → 压缩 token（def/zstd/bc）；非变体返回空。"""
    c = canonical_id(tid)
    if '-codec-' not in c:
        return ''
    tok = c.split('-codec-', 1)[1]
    return tok[:-3] if tok.endswith('-sr') else tok


def compression_of(tid):
    """变体 id → index.codec 取值；非变体返回 None。"""
    return TOKEN_CODEC.get(token_of(tid))


def source_reuse_of(tid):
    """变体 id → True/False；非变体返回 None。"""
    c = canonical_id(tid)
    if '-codec-' not in c:
        return None
    return c.split('-codec-', 1)[1].endswith('-sr')


def engines_of(tid):
    """变体 id → 允许的引擎（None = 两侧通用）。

    ZSTD 系列压缩与 index.source_reuse 都是 Easysearch 专有（ES 的 index.codec
    只有 default/best_compression，也不认 index.source_reuse）。
    """
    if not is_variant(tid):
        return None
    if source_reuse_of(tid) or compression_of(tid) == 'ZSTD':
        return 'easysearch'
    return None


def variant_of(track, codec, sr):
    """(基础赛道, 压缩, source_reuse) → 变体 id；没有压缩就是基础赛道本身。"""
    if not codec:
        return track
    token = CODEC_TOKEN.get(codec)
    if not token:
        return track
    return '%s-codec-%s%s' % (track, token, '-sr' if sr == 'true' else '')


def variant_id_of(row):
    """覆盖键取真实 runner 赛道 id，不能从相同的实测 codec 猜基座/def 变体。

    track_variant 是新契约的权威身份；无此列时仅保留明确的源头 track_raw/track。
    geo 与入库展示名 geonames 在所有路径上统一回 runner 的 geo。
    """
    raw = row.get('track_variant') or row.get('track_raw')
    if raw and raw != 'track':
        return runner_track_id(canonical_id(raw))
    return runner_track_id(canonical_id(row.get('track') or ''))


def split_codec(codec, sr=None):
    """(codec 字段, source_reuse 字段) → (压缩, source_reuse)。

    兼容旧格式：老的 user_tags.codec 是糅合串 "ZSTD+source_reuse"（两个维度
    挤在一起）→ 拆开，让新旧数据落到同一套干净维度上，可比。
    """
    if not codec:
        return None, None
    if '+' in codec:
        comp, _, tail = codec.partition('+')
        return comp, ('true' if 'source_reuse' in tail else 'false')
    if sr is None:
        return codec, None
    return codec, ('true' if str(sr).lower() == 'true' else 'false')


# ------------------------------------------------------------------ 查指标库
def es_query(ssh, body, auth=None, es_url=DEFAULT_ES_URL, path=None, timeout=90,
             method='POST'):
    """查指标库，返回解析后的 JSON。

    ssh 模式：在服务器上 curl，口令经 stdin 的 curl 配置传入（不出现在 argv）。
    本机模式：直连 urllib（自签证书 → 不校验）。
    """
    payload = json.dumps(body, ensure_ascii=False)
    url = '%s/%s' % (es_url.rstrip('/'), path or (INDEX + '/_search'))

    if ssh:
        cmd = ("curl -fsSk --max-time %d --config - -X %s "
               "-H 'Content-Type: application/json' -d %s %s"
               % (int(timeout), shlex.quote(method), shlex.quote(payload), shlex.quote(url)))
        stdin = ''
        if auth:
            # curl 配置中的控制字符不能改变配置结构；机密只经 stdin 传递。
            safe = (auth.replace('\\', '\\\\').replace('"', '\\"')
                    .replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t'))
            stdin = 'user = "%s"\n' % safe
        full = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
        full += ssh.split()
        full += [cmd]
        try:
            p = subprocess.run(full, input=stdin, capture_output=True, text=True,
                               timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HistoryUnavailable('ssh 查询指标库失败: %s' % e)
        if p.returncode != 0:
            raise HistoryUnavailable('ssh/curl 查询指标库返回码 %d'
                                     % p.returncode)
        raw = p.stdout
    else:
        req = urllib.request.Request(url, data=payload.encode('utf-8'), method=method,
                                     headers={'Content-Type': 'application/json'})
        if auth:
            tok = base64.b64encode(auth.encode('utf-8')).decode('ascii')
            req.add_header('Authorization', 'Basic ' + tok)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                raw = r.read().decode('utf-8', 'replace')
        except Exception as e:                                    # noqa: BLE001
            raise HistoryUnavailable('连不上指标库 %s: %s' % (url, e))

    try:
        resp = json.loads(raw)
    except (ValueError, TypeError):
        raise HistoryUnavailable('指标库返回非 JSON')
    if method == 'DELETE':
        if not isinstance(resp, dict) or resp.get('error') or resp.get('succeeded') is not True:
            raise HistoryUnavailable('指标库 scroll 清理失败')
    else:
        _validate_response(resp)
    return resp


def _validate_response(resp):
    """传输成功不等于查询成功：错误、超时及部分 shard 结果都不能当完整历史。"""
    if not isinstance(resp, dict) or resp.get('error'):
        raise HistoryUnavailable('指标库返回错误或无效响应结构')
    status = resp.get('status')
    if status is not None and (not isinstance(status, int) or status >= 400):
        raise HistoryUnavailable('指标库返回 HTTP 错误状态')
    if resp.get('timed_out') is not False:
        raise HistoryUnavailable('指标库查询超时或缺少 timed_out 结果')
    shards = resp.get('_shards')
    if not isinstance(shards, dict):
        raise HistoryUnavailable('指标库缺少 shard 完整性信息')
    counts = [shards.get(k) for k in ('total', 'successful', 'failed')]
    if any(type(x) is not int or x < 0 for x in counts):
        raise HistoryUnavailable('指标库 shard 完整性信息无效')
    total, successful, failed = counts
    if failed or successful != total:
        raise HistoryUnavailable('指标库只返回部分 shard 结果')


def _search_page(resp):
    _validate_response(resp)
    hits = resp.get('hits')
    if not isinstance(hits, dict) or not isinstance(hits.get('hits'), list):
        raise HistoryUnavailable('指标库缺少 hits 结果')
    total = hits.get('total')
    if (not isinstance(total, dict) or total.get('relation') != 'eq'
            or type(total.get('value')) is not int or total['value'] < 0):
        raise HistoryUnavailable('指标库未返回精确 total（必须 track_total_hits=true）')
    for hit in hits['hits']:
        if (not isinstance(hit, dict) or not isinstance(hit.get('_source'), dict)
                or not isinstance(hit.get('_id'), str) or not hit['_id']):
            raise HistoryUnavailable('指标库命中文档缺少 _id/_source')
        source = hit['_source']
        if (any(not isinstance(source.get(k), str) or not source[k]
                for k in ('car_code', 'track', 'track_variant', 'campaign', 'race_id'))
                or type(source.get('round')) is not int or source['round'] < 1
                or type(source.get('trusted')) is not bool
                or not isinstance(source.get('challenge'), list)
                or any(not isinstance(c, str) or not c for c in source['challenge'])):
            raise HistoryUnavailable('指标库文档不符合身份/覆盖字段契约')
    return hits['hits'], total['value']


def fetch_rows(ssh=None, auth=None, es_url=DEFAULT_ES_URL, index=INDEX,
               timeout=90, max_rows=20000):
    """用真实 scroll 完整读取；总数、分片、文档去重任一不符就降级，绝不截断。"""
    body = {'size': PAGE_SIZE, 'query': {'match_all': {}}, '_source': _SOURCE_FIELDS,
            'sort': ['_doc'], 'track_total_hits': True}
    rows, seen = [], set()
    scroll_id = None
    try:
        resp = es_query(ssh, body, auth=auth, es_url=es_url,
                        path='%s/_search?scroll=2m' % index, timeout=timeout)
        scroll_id = resp.get('_scroll_id') if isinstance(resp, dict) else None
        got, total = _search_page(resp)
        if total > max_rows:
            raise HistoryUnavailable('指标库有 %d 行，超过上限 %d' % (total, max_rows))
        while True:
            if scroll_id is not None and (not isinstance(scroll_id, str) or not scroll_id):
                raise HistoryUnavailable('指标库 scroll_id 无效')
            for hit in got:
                key = (hit.get('_index', index), hit['_id'])
                if key in seen:
                    raise HistoryUnavailable('指标库分页出现重复文档')
                seen.add(key)
                rows.append(hit['_source'])
            if len(rows) > total:
                raise HistoryUnavailable('指标库行数超过声明 total')
            if len(rows) == total:
                return rows, total
            if not got or not scroll_id:
                raise HistoryUnavailable('指标库只读到 %d/%d 行（翻页中断）' % (len(rows), total))
            resp = es_query(ssh, {'scroll': '2m', 'scroll_id': scroll_id},
                            auth=auth, es_url=es_url, path='_search/scroll', timeout=timeout)
            got, page_total = _search_page(resp)
            if page_total != total:
                raise HistoryUnavailable('指标库分页 total 变化')
            scroll_id = resp.get('_scroll_id') or scroll_id
    finally:
        if isinstance(scroll_id, str) and scroll_id:
            try:
                es_query(ssh, {'scroll_id': [scroll_id]}, auth=auth, es_url=es_url,
                         path='_search/scroll', timeout=timeout, method='DELETE')
            except HistoryUnavailable:
                pass      # 清理失败不改变已核实的读取结果；scroll 自身有 2m TTL


# ------------------------------------------------------------------ 覆盖模型
def _challenges_of(row):
    """一次测量的 challenge 集合。race 行里是**多值**（写赛+查赛合并成一行）。"""
    ch = row.get('challenge')
    if isinstance(ch, str):
        ch = [ch]
    return {str(c).strip() for c in (ch or []) if str(c).strip()}


def _phases_of(row):
    """已存在的阶段只证明对应阶段，不从 write-only 推断整场完整。"""
    phases = set()
    for challenge in _challenges_of(row):
        if challenge in ('write-only', 'index-only'):
            phases.add('w')
        elif challenge == 'query-only':
            phases.add('q')
        elif challenge in ('full', 'nested-search-challenge', 'default'):
            phases.update(('w', 'q'))
    phase = row.get('phase')
    if phase == 'wq':
        phases.update(('w', 'q'))
    elif phase in ('w', 'q'):
        phases.add(phase)
    return phases


def _expected_phases(row):
    """当前 runner 的可排赛道一律要求写查两段，与已落档的 challenge 无关。"""
    return {'w', 'q'}


def _measurement_key(row, ordinal):
    """只允许同 campaign 的真实 fid/attempt 配相位；缺身份的行不得互相补齐。"""
    campaign = row.get('campaign') or ''
    fid = row.get('fid') or ''
    # 源头 sidecar 路径含真实 fid；不能从 car/track/round 重新合成尝试身份。
    if not fid:
        sources = row.get('source') or []
        if isinstance(sources, str):
            sources = [sources]
        for source in sources:
            if isinstance(source, str) and source.startswith('sidecar:'):
                match = re.fullmatch(r'sidecar:(.+)-(?:wq|w|q)', source)
                if match:
                    fid = match.group(1)
                    break
    if campaign and fid:
        match = re.search(r'-try(\d+)$', fid)
        suffix_attempt = int(match.group(1)) if match else 1
        try:
            attempt = int(row.get('attempt') or suffix_attempt)
        except (TypeError, ValueError):
            return ('unidentified', ordinal)
        if attempt < 1 or (match and attempt != suffix_attempt):
            return ('unidentified', ordinal)
        return (campaign, 'fid', re.sub(r'-try\d+$', '', fid), attempt)
    if campaign and row.get('race_id'):
        return (campaign, 'race_id', row['race_id'])
    return ('unidentified', ordinal)


def _measurement_verdict(rows):
    statuses = [r.get('status') or '' for r in rows]
    if any(s in FAIL_STATUSES for s in statuses):
        return VERDICT_FAIL
    if any(s in SKIP_STATUSES for s in statuses):
        return VERDICT_SKIP
    if any(s and s != 'VALID' for s in statuses):
        return VERDICT_UNKNOWN
    phases = set().union(*(_phases_of(r) for r in rows))
    if not {'w', 'q'}.issubset(phases):
        return VERDICT_HALF
    if any(r.get('trusted') is False or r.get('quality') == 'degraded' for r in rows):
        return VERDICT_DEGRADED
    return VERDICT_VALID


def _verdict_of(rows):
    """先逐测量判完整性，再聚合；任一完整可信测量即可覆盖，失败不抹掉成功。"""
    groups = {}
    for ordinal, row in enumerate(rows):
        key = (row_build_key(row), row.get('series_key') or '',
               _measurement_key(row, ordinal))
        groups.setdefault(key, []).append(row)
    verdicts = [_measurement_verdict(group) for group in groups.values()]
    # 不同测量从不拼相位。显示最可用的一次，只有 VALID 才能自动剔除。
    for verdict in (VERDICT_VALID, VERDICT_DEGRADED, VERDICT_HALF,
                    VERDICT_FAIL, VERDICT_SKIP):
        if verdict in verdicts:
            return verdict
    return VERDICT_UNKNOWN


@dataclass
class Cell:
    """一个 (车, **变体**, 轮次) 格的覆盖情况。

    变体 id = <基础赛道>-codec-<压缩>[-sr]；没有压缩的场次就是基础赛道本身。
    （库里 track 字段是基础赛道，压缩与 source_reuse 是另外两个维度 ——
    不合成变体 id 的话，6 个变体的历史会混成一格。）

    注意：同一格可能有**多个 build** 的记录（一个 build 跑失败、另一个 build 重跑成功）。
    所以判定必须**按 build 分开**：对重跑那个 build 而言这一格是"跑过"，对另一个
    build 而言是"没跑过"。把两个 build 的行混在一起取最坏判定，会把后来成功的
    重跑也一起抹掉。
    """
    car: str
    track: str
    round: int
    rows: list = field(default_factory=list)

    def add(self, row):
        self.rows.append(row)

    @property
    def compression(self):
        """这一格用到的压缩：优先实测值，缺失时按变体 id 的定义回落。

        （变体目录本身就是"烘了什么设置"的 ground truth —— 变体没烘
        source_reuse 就是没烘，引擎默认 false。）
        """
        for r in self.rows:
            c, _ = split_codec(r.get('codec'), r.get('source_reuse'))
            if c:
                return c
        return compression_of(self.track)

    @property
    def source_reuse(self):
        """这一格是否开了 source_reuse：优先实测值，缺失时按变体 id 的定义回落。"""
        for r in self.rows:
            _, s = split_codec(r.get('codec'), r.get('source_reuse'))
            if s is not None:
                return s == 'true'
        return source_reuse_of(self.track)

    @property
    def key(self):
        return (self.car, self.track, self.round)

    @property
    def base(self):
        """基础赛道 id（变体剥掉 -codec-... 后缀）。"""
        return base_of(self.track)

    @property
    def builds(self):
        return sorted({row_build_key(r) for r in self.rows})

    def rows_for(self, car_build):
        """某个 build 上的行。没有 build 号的车按车名匹配。"""
        return [r for r in self.rows if row_build_key(r) == car_build]

    def verdict_for(self, car_build):
        """该 build 至少有一次独立完整测量的判定。"""
        rows = self.rows_for(car_build)
        if not rows:
            return VERDICT_UNKNOWN
        return _verdict_of(rows)

    def covered_for(self, car_build):
        """该 build 上能不能算"已经跑过、结论可用"。"""
        return self.verdict_for(car_build) == VERDICT_VALID

    @property
    def verdict(self):
        """整格最可用的一次独立测量（仅展示；自动剔除仍必须按 build）。"""
        return _verdict_of(self.rows)

    @property
    def covered(self):
        """整格是否至少有一个 build 跑成过。覆盖判定请用 covered_for()。"""
        return self.verdict == VERDICT_VALID

    @property
    def campaigns(self):
        return sorted({(r.get('campaign') or '') for r in self.rows})

    @property
    def phases(self):
        return sorted(set().union(*(_phases_of(r) for r in self.rows)))

    @property
    def last_ts(self):
        ts = [r.get('@timestamp') or '' for r in self.rows if r.get('@timestamp')]
        return max(ts) if ts else ''

    @property
    def warnings(self):
        """这一格有什么值得提示的。

        索引行里不再有 caliber_warnings（警告改走 manifest），
        剩下的行级信号只有 trusted 与 track_variant。
        """
        out = []
        for r in self.rows:
            if r.get('trusted') is False:
                out.append("该测量有 trusted=false 的行（面板只取 trusted=true）")
            v = variant_id_of(r)
            if v and v != r.get('track'):
                out.append("赛道变体：%s（基座 %s）" % (v, r.get('track')))
        return out

    def per_build(self):
        """每个 build 上的判定 + campaign，供界面展示。"""
        out = {}
        for b in self.builds:
            rows = self.rows_for(b)
            out[b] = {
                'verdict': _verdict_of(rows),
                'runs': len(rows),
                'phases': sorted(set().union(*(_phases_of(r) for r in rows))),
                'campaigns': sorted({(r.get('campaign') or '') for r in rows}),
                'quality': sorted({(r.get('quality') or '') for r in rows}),
            }
        return out

    def to_dict(self):
        d = asdict(self)
        d.update({
            'verdict': self.verdict,
            'covered': self.covered,
            'builds': self.builds,
            'campaigns': self.campaigns,
            'phases': self.phases,
            'last_ts': self.last_ts,
            'warnings': self.warnings,
            'runs': len(self.rows),
            'per_build': self.per_build(),
        })
        d.pop('rows', None)
        return d


@dataclass
class Coverage:
    """覆盖表。"""
    cells: dict = field(default_factory=dict)       # (car, track, round) -> Cell
    total_rows: int = 0
    campaigns: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    cars: list = field(default_factory=list)
    available: bool = True
    error: str = ''

    def get(self, car, track, rnd):
        return self.cells.get((car, runner_track_id(canonical_id(track)), rnd))

    def summary(self):
        by_verdict = {}
        for c in self.cells.values():
            by_verdict[c.verdict] = by_verdict.get(c.verdict, 0) + 1
        return {
            'rows': self.total_rows,
            'races': len(self.cells),
            'campaigns': len(self.campaigns),
            'tracks': len(self.tracks),
            'cars': len(self.cars),
            'by_verdict': by_verdict,
        }

    def to_dict(self):
        return {
            'available': self.available,
            'error': self.error,
            'summary': self.summary(),
            'campaigns': self.campaigns,
            'tracks': self.tracks,
            'cars': self.cars,
            'cells': {'|'.join(str(x) for x in k): c.to_dict()
                      for k, c in self.cells.items()},
        }


def build_coverage(rows, total=None):
    """把 benchmark-result-race 的行折叠成覆盖表。"""
    cov = Coverage(total_rows=total if total is not None else len(rows))
    camps, tracks, cars = set(), set(), set()
    for r in rows:
        car = car_of(r)
        track = r.get('track') or ''
        rnd = r.get('round')
        if rnd is None:
            rnd = 0
        try:
            rnd = int(rnd)
        except (TypeError, ValueError):
            rnd = 0
        if not car or not track:
            continue
        # 变体身份来自 runner id；实测 codec 是另一维，不用于猜目录。
        vid = variant_id_of(r)
        key = (car, vid, rnd)
        cell = cov.cells.get(key)
        if cell is None:
            cell = Cell(car=car, track=vid, round=rnd)
            cov.cells[key] = cell
        cell.add(r)
        camps.add(r.get('campaign') or '')
        tracks.add(vid)
        cars.add(car)
    cov.campaigns = sorted(c for c in camps if c)
    cov.tracks = sorted(tracks)
    cov.cars = sorted(cars)
    return cov


def fetch_coverage(ssh=None, auth=None, es_url=DEFAULT_ES_URL, index=INDEX,
                   timeout=90):
    """读指标库 → 覆盖表。指标库不可用时返回 available=False 的空表（不抛）。

    返回空表而不是抛异常，是因为编排台必须在指标库停着时照常工作
    （压测窗口内指标库本来就是停的）—— 但界面要明确显示"指标库未运行"。
    """
    try:
        rows, total = fetch_rows(ssh=ssh, auth=auth, es_url=es_url,
                                  index=index, timeout=timeout)
    except HistoryUnavailable as e:
        cov = Coverage(available=False, error=str(e))
        return cov
    cov = build_coverage(rows, total)
    return cov


# ------------------------------------------------------------------ 编排用
def expected_car_build(car, build):
    """该车在当前 build 下的 build 标识（与 row_build_key 同一口径）。

    easysearch 车是 `car@build`（如 jdk25@3015）；
    elasticsearch 车没有 build 号，就是车名本身（如 es819）。
    """
    if not build:
        return car
    return '%s@%s' % (car, build)


def plan_cells(coverage, cars, tracks, rounds, build_of=None, include_covered=False):
    """算出编排矩阵里每一格该不该排。

    cars: [{'name':..., 'build':...}, ...] 或 [(name, build), ...]
    build_of: {车名: build}，缺省视为不区分 build
    include_covered=True 时不做任何剔除（用户明确要重跑）

    返回 {'cells': [...], 'to_run': [...], 'skipped': [...], 'notes': [...]}
    每格：{car, track, round, covered, same_build, verdict, builds, campaigns,
           reason}
    """
    build_of = build_of or {}
    cells = []
    for car_spec in cars:
        # 车的描述有三种形态：dict、Car 命名元组、(name, build) 二元组。
        # 注意别把命名元组当二元组解包 —— 第二个字段是 label，不是 build。
        if isinstance(car_spec, dict):
            aname = car_spec.get('name') or ''
            build = car_spec.get('build') or ''
        elif hasattr(car_spec, 'build'):
            aname = getattr(car_spec, 'name', '')
            build = getattr(car_spec, 'build', '') or ''
        else:
            aname = car_spec[0]
            build = car_spec[1] if len(car_spec) > 1 else ''
        build = build or build_of.get(aname) or ''
        want = expected_car_build(aname, build)
        for tk in tracks:
            for rnd in range(1, int(rounds) + 1):
                cell = coverage.get(aname, tk, rnd) if coverage.available else None
                info = {
                    'car': aname, 'track': tk, 'round': rnd,
                    'covered': False, 'same_build': False,
                    'verdict': '', 'verdict_same_build': '',
                    'builds': [], 'campaigns': [], 'runs': 0,
                    'reason': '',
                }
                if cell is not None:
                    same = want in cell.builds
                    info.update({
                        'verdict': cell.verdict,               # 跨 build 展示用
                        'verdict_same_build': cell.verdict_for(want),
                        'builds': cell.builds,
                        'campaigns': cell.campaigns,
                        'runs': len(cell.rows),
                        'same_build': same,
                        # 覆盖判定按 build 隔离：别的 build 上跑过不算这一格跑过
                        'covered': cell.covered_for(want),
                    })
                if include_covered:
                    info['reason'] = '（含已跑：按要求全部重跑）'
                    info['to_run'] = True
                elif info['covered']:
                    camps = info['campaigns']
                    shown = ','.join(camps[:3])
                    if len(camps) > 3:
                        shown += '…共%d个campaign' % len(camps)
                    info['reason'] = '已跑过（同 build %s，%s）' % (want, shown or '?')
                    info['to_run'] = False
                elif cell is not None and not info['same_build']:
                    per = cell.per_build()
                    detail = '，'.join('%s=%s' % (b, per[b]['verdict'])
                                       for b in info['builds'])
                    if not build:
                        # 车没带 build 信息 → 无从判断是否同 build。这种情况
                        # 必须说出来，不能当成"已跑过"也不能当成"没跑过"。
                        info['reason'] = ('有记录（%s）但该车没有 build 信息，'
                                          '无法判定是否同一 build → 需人工确认' % detail)
                    else:
                        info['reason'] = '只有别的 build 跑过（%s），当前 build %s 仍需跑' % (
                            detail, want)
                    info['to_run'] = True
                elif cell is not None:
                    info['reason'] = '有记录但判定 %s，不算跑过，需重跑' % (
                        info['verdict_same_build'] or info['verdict'] or '?')
                    info['to_run'] = True
                else:
                    info['reason'] = '未跑过'
                    info['to_run'] = True
                cells.append(info)
    return {
        'cells': cells,
        'to_run': [c for c in cells if c['to_run']],
        'skipped': [c for c in cells if not c['to_run']],
    }


def filter_rows(plan, rows):
    """按编排计划剔除已覆盖的格。rows 是 (车, 赛道, 轮次) 序列。"""
    skip = set((c['car'], c['track'], c['round']) for c in plan['skipped'])
    return [r for r in rows if (r[0], r[1], r[2]) not in skip]


def if_empty(coverage):
    """指标库不可用时的占位覆盖表（界面据此显示"未连接"而不是"全空"）。"""
    return Coverage(available=False, error=coverage.error if coverage else '未配置指标库')
