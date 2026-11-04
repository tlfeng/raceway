# tracks/ · 赛道定义定版目录（收缩后的事实源）

仿照 [elastic/rally-tracks](https://github.com/elastic/rally-tracks) 的布局，赛道定义**静态定版进 git**，
不再由 prepare-*.sh 运行时生成——git 里是什么，跑的就是什么。
回答一个问题：**跑结果用的是哪一版赛道定义** —— campaign ↔ raceway commit ↔ track 定义三者对齐。

## 规矩

1. **官方 track 用原名**：`geonames/`、`nested/` vendor 自 rally-tracks
   `28b335b5e76d869d5d8486d5607ae8e158eb5a16`（2026-07-27）；上游漂移后重新 vendor 时更新这行。
   **语料一律不进 git**：geonames/nested 语料由 rally 从 `benchmarks.elastic.co.net/corpora/`
   下载或符号链接到压测机 rally 缓存；hv1024 语料/查询集由部署时生成（见下）。
2. **challenges 与上游的差异是资产**（write-only/query-only/full 三挑战、forcemerge 单段、
   warmup 按语料规模比例、删 time-period）——已从 prepare-*.sh 的 heredoc **原样静态化**进本目录。
3. **参数化收缩（已执行）**：分片数与存储设置不再烘成变体目录——
   - 分片：`number_of_shards` / `number_of_replicas` track params（geonames 默认 5、nested 默认 1、hv1024 默认 3，编排传 3/0 与历史口径对齐）
   - 存储：`index_codec`（default/ZSTD/best_compression）与 `source_reuse`（true）**条件参数**——
     不传就不写进 settings，绕开「ES 连 `index.source_reuse=false` 都拒收」的坑（Jinja 条件块 +
     标量参数无逗号，rally 2.12 解析可靠）。旧 `geo-codec-*` 六个变体目录退役，
     对应组合 = geonames + 两个 params。
   - **入库侧联动（已执行）**：`track_variant` 列退役；覆盖判定 key 由
     `codec`/`source_reuse` 列派生变体 id（`bench-history.variant_id_of`，旧契约行回落
     track_variant/track_raw）。**编排纪律**：只在显式传存储 params 的场次往 user-tags
     写 `codec`/`source_reuse`（基座场次不写 → codec=null → 派生回基座，不会误标成
     def 变体）；`JDK_WANT` 仍决定装哪个 JDK（§DESIGN 五·4）。
4. 消费：`esrally --track-path=<本目录>/<track> --challenge=… --track-params=…`。
   改动任何定义后先跑 `esrally info --track-path=…` 校验（服务器上执行）。

## 当前目录

| 目录 | 状态 | 说明 |
|---|---|---|
| `geonames/` | ✅ 定义齐；**write-only/query-only 待从压测机回灌**（`$ROOT/tracks/geo/challenges/`） | 官方基座 + raceway 口径；`create-only` 探针已就位 |
| `nested/` | ✅ 完整（挑战自 prepare-nested-track.sh heredoc 静态化） | 官方基座，shards 走 params |
| `hv1024/` | ✅ 定义齐；`queries.json` 部署时生成：`bzcat $HV_QUERIES_BZ2 \| head -2000 > queries.json`，语料 `ln -sfn $HV_CORPUS documents-1024.json`（1M×1024 维，22 739 096 214 字节，断言在 track.json） | 原 hv1024-s3 收缩；m=32/efC=100 |
| `hv1024x{,-int8,-bbq,-codec-zstd-sr}/` | ✅ 完整（2026-11-04 自压测机定版入库，生成器 `setup-hv1024bbq.sh` 退役） | 1024 维量化/压缩四变体（2026-11-03 hvbbq campaign 15/15 VALID 用此版）：四目录仅 `index.json` 不同——基座 `hnsw`、`int8_hnsw`、`bbq_hnsw`、`hnsw+ZSTD+source_reuse`；其余定义（track.json/track.py/operations/challenges）逐字节同基座。`create-only` 探针仅 codec-zstd-sr 有（复现该臂故障用）。索引名 `vectors`，语料同 hv1024（VARIANT.txt 记台账）；`queries-recall.jsonl`/`truth.jsonl` 为 recall 探针数据，不进 git。结论：`REPORT-hvbbq-1024维量化压缩对比-20261104.md` |
| `ds-j{512,768,1024,1536,2048}`、`ds-g{960,1920}` | ⏳ **待从压测机回灌**（`$ROOT/tracks/ds-*` 定义文件，不含语料；生成器 dimsweep-*.py 已不在树内，静态定版是唯一留存机会） | 语料在 `$ROOT/corpora-ds/` |
| `hnsw2m-2m`（→ 建议落 `hnsw/`，分片 params 化） | ⏳ 待从压测机回灌（手工搭的基座） | 96 维 200 万向量 |
| `hvtp200k-2s/3s` | 🗑 一次性，不回灌（oneoff 批次已定案；需要时分片走 params） | |

## 服务器侧切换清单（压测机上执行并验证，一轮 campaign 走通后再删脚本）

编排层 track id `geo → geonames` 的改名与 RACES.md/coverage 记账联动（§6.1 断裂类），
**必须连编排台一起验证**。精确改动点：

1. `vm-bench-lib.sh`：`track_dir` 的 `geo) echo $TRACK_GEO` → `geonames) echo $ROOT/tracks/geonames`；
   `track_params`/`track_index`/超时 case 的 `geo` 键同步改 `geonames`；
   `TIMEOUT_GEO_W/Q`（vm-bench.env*）改名 `TIMEOUT_GEONAMES_W/Q`。
2. 压缩变体起跑：`--track-path=$ROOT/tracks/<变体id>` 改为
   `--track-path=$ROOT/tracks/geonames --track-params="number_of_shards:${SHARDS:-3},number_of_replicas:${REPLICAS:-0},index_codec:<档>,source_reuse:<true|不传>"`；
   `track_compression`/`track_source_reuse` 两个 helper 保留（变体 id → params 的映射照用）。
3. `run-bench.sh`：`WHAT_ARGS`/usage/case 的 `geo` → `geonames`（`gdef/gzstd/gbc` 别名保留）；
   `codec) TRACKS=(六变体)` → `TRACKS=(geonames-codec-def geonames-codec-zstd geonames-codec-bc …-sr)`（id 仅作编排记账，目录不再存在）；
   `both)/all)` 与 `track_tmo_w/q` 的 geo 键同步。
4. `bench-compose.py`：199/212/214/238 行的 `'geo'` 键与 `BASE_GROUP` 同步改 `geonames`。
5. `prepare-codec-track.sh` / `prepare-nested-track.sh` / `prepare-hv1024-track.sh` 退役
   （tracks/ 已含其产物）；`prepare-dimsweep-track.sh` 保留（修链+自检职责仍需要）。
6. 看板/分析侧：`kibana/make_dashboard.py:319` 的 `'geo'` 标签键改 `'geonames'`；
   workspace `analysis/*.py` 里 `track:'geo'` 过滤在下次对重灌数据使用前改 `geonames`。
