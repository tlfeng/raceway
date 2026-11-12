# tracks/ · 赛道定义定版目录（收缩后的事实源）

仿照 [elastic/rally-tracks](https://github.com/elastic/rally-tracks) 的布局，赛道定义**静态定版进 git** ——
git 里是什么，跑的就是什么。回答一个问题：**跑结果用的是哪一版赛道定义** —— campaign ↔ raceway commit ↔ track 定义三者对齐。

## 规矩

1. **官方 track 用原名**：`geonames/`、`nested/` vendor 自 rally-tracks
   `28b335b5e76d869d5d8486d5607ae8e158eb5a16`；上游漂移后重新 vendor 时更新这行。
   **语料一律不进 git**：geonames/nested 语料由 rally 从 `benchmarks.elastic.co.net/corpora/`
   下载或符号链接到压测机 rally 缓存；hv1024 语料/查询集由部署时生成（见下）。
2. **challenges 与上游的差异是资产**（write-only/query-only/full 三挑战、forcemerge 单段、
   warmup 按语料规模比例、删 time-period）——已静态化进本目录。
3. **参数化**：分片数与存储设置走 track params，不烘成变体目录——
   - 分片：`number_of_shards` / `number_of_replicas` track params（geonames 默认 5、nested 默认 1、hv1024 默认 3，编排传 3/0 与历史口径对齐）
   - 存储：`index_codec`（default/ZSTD/best_compression）与 `source_reuse`（true）**条件参数**——
     不传就不写进 settings，绕开「ES 连 `index.source_reuse=false` 都拒收」的坑（Jinja 条件块 +
     标量参数无逗号，rally 2.12 解析可靠）。对应组合 = geonames + 两个 params。
   - **入库侧联动**：`track_variant` 列只作为旧契约行的回落来源，覆盖判定 key 由
     `codec`/`source_reuse` 列派生变体 id（`bench-history.variant_id_of`，旧契约行回落
     track_variant/track_raw）。**编排纪律**：只在显式传存储 params 的场次往 user-tags
     写 `codec`/`source_reuse`（基座场次不写 → codec=null → 派生回基座，不会误标成
     def 变体）；`JDK_WANT` 仍决定装哪个 JDK（§DESIGN 五·4）。
4. 消费：`esrally --track-path=<本目录>/<track> --challenge=… --track-params=…`。
   改动任何定义后先跑 `esrally info --track-path=…` 校验（服务器上执行）。

## 当前目录

| 目录 | 状态 | 说明 |
|---|---|---|
| `geonames/` | ✅ 完整 | 官方基座 + raceway 口径：`write-only`/`query-only` 由上游 `append-no-conflicts` **机械派生**（写段 = 建索引 + forcemerge 单段 + 等合并；查段 = 其余 30 个查询操作；与上游逐项同序）；`create-only` 探针在位 |
| `nested/` | ✅ 完整 | 官方基座，shards 走 params |
| `hv1024/` | ✅ 定义齐；起跑前补 `queries.json`（`bzcat $HV_QUERIES_BZ2 \| head -2000`）与语料软链 `ln -sfn $HV_CORPUS documents-1024.json`（1M×1024 维，22 739 096 214 字节，断言在 track.json） | m=32/efC=100；`_source` **整个关掉**（`enabled: false`） |
| `hv1024x/` | ✅ 完整（四臂收成一份定义 + params） | 1024 维量化/压缩四臂：**一份基座定义**，各臂差异只由两个参数维度表达 —— 量化档 `vector_index_type=int8_hnsw\|bbq_hnsw`（`index_options.type`）、压缩档 `index_codec=ZSTD + source_reuse=true`（settings，**仅 Easysearch**）。量化/压缩两侧通用性：int8/bbq 两侧通用（ES 8.19.23 与 ez 都能建），ZSTD+sr 仅 ez。`create-only` 建索引探针随基座保留。索引名 `vectors`，语料同 hv1024（VARIANT.txt 记台账）；`queries-recall.jsonl`/`truth.jsonl` 为 recall 探针数据，不进 git。结论：`REPORT-hvbbq-1024维量化压缩对比-20261104.md` |

| `ds-j{512,768,1024,1536,2048}`、`ds-g{960,1920}` | ⏳ **待从压测机回灌**（`$ROOT/tracks/ds-*` 定义文件，不含语料；生成器已不在树内，静态定版是唯一留存机会） | 语料在 `$ROOT/corpora-ds/`；起跑时由 run-bench 修链 + 自检（维度/条数/字节/查询维数） |
| `hnsw2m-2m`（→ 建议落 `hnsw/`，分片 params 化） | ⏳ 待从压测机回灌（手工搭的基座） | 96 维 200 万向量 |
| `hvtp200k-2s/3s` | 🗑 一次性，不回灌（需要时分片走 params） | |

> **为什么 `hv1024` 与 `hv1024x` 不合并成一份**：
> 两族唯一实质差异是 `_source` 策略 —— `enabled: false`（整个关掉）vs `excludes: ["emb"]`
> （保留 docid、排除向量）。这是**写入/存储口径**（影响 translog、segment 体积、写入耗时），
> 不是被测变量；而 `_source` 在 mappings 里、sidecar 只采 `_settings` → **入库数据里查不到
> 这一口径**（不像 quant/codec/sr 有实测列可反查）。
> 两族里 3 个**逐字节相同**的文件（`challenges/default.json`、`operations/default.json`、
> `track.py`）由 `tests/test_hv1024_vs_hv1024x.py` 锁住：一旦分叉就红 —— 重复文件的漂移风险
> 用测试挡住，不必用合并来挡。

## 运行时怎么用这些定义

- **直接消费**：`vm-bench-lib.sh` 的 `track_dir` 把赛道 id 映射到本目录下的子目录
  （`geo → tracks/geonames`、`hv1024 → tracks/hv1024`、`nested → tracks/nested`、
  `hv1024x*` 四臂 → `tracks/hv1024x`）。
  跑的就是 git 里这一份定义，**没有第二份运行时生成的副本**。
  编排层的 track id 仍叫 `geo`（不改名：跑分的场次名与入库口径不能漂）。
- **起跑前只补 git 收不了的产物**（`run-bench.sh` 的 `prepare_tracks`）：语料软链（GB 级）、
  hv1024 系 `queries.json`、`.offset` 断点处理。
- **处理变量走 `--track-params`**，各臂差异只由参数表达，不另存目录：
  - 压缩档与 source_reuse → `index_codec:` / `source_reuse:true`，由 `index.json` 的条件块
    写进 settings：`{% if index_codec is defined %}` / `{% if source_reuse is defined and source_reuse %}`。
  - 向量量化档 → `vector_index_type:int8_hnsw|bbq_hnsw`，由条件块写进 **mappings** 的
    `index_options.type`（`{{vector_index_type | default('hnsw')}}`）。
  这两个维度**互相独立**：量化是向量索引类型，压缩是存储设置。各臂差异只由参数表达，
  没有第二份目录可漂移（改一处忘另一处的风险从根上没了）。
- **起跑前守卫**：`vm-bench-lib.sh` 的 `track_assert_storage_params` 检查定版 `index.json`
  里有没有对应条件块——少一个条件块，该维度会**静默失效**（压缩臂跑成同一个 / 量化臂跑成
  hnsw 基座，数据看着正常、结论全废），所以起跑前就挡住。

## 搜索预算与更新对照协议

hv1024 六个目录的 ANN/exact 分支都在**最终 body** 写入 `size=k`，分别返回10/100条。
请求参数不能靠 Rally 参数字典外层的 size 表达。操作名保留 `knn-search-10-100` /
`knn-search-100-1000`，但末尾数字在不同引擎中**不表示等价工作量**：

| 赛道 / 引擎 | operation 参数 → 最终请求 | 实际口径 |
|---|---|---|
| hv1024 / hv1024x，ES/Easysearch HNSW | num-candidates → knn.num_candidates=100/1000 | 每分片近邻候选预算 |
| hv1024x-bbqdisk，ES | num-candidates → knn.num_candidates=100/1000 | bbq_disk 自有聚类/访问策略；不是 HNSW ef_search，也未固定 visit_percentage |
| hv1024x-os / os-bbq，Lucene HNSW | ef-search → query.knn.emb.method_parameters.ef_search=100/1000 | 搜索宽度 max(k,ef_search)，量化策略依引擎 |
| hv1024x-os-disk，Faiss HNSW | ef-search → method_parameters.ef_search=100/1000；oversample-factor → rescore.oversample_factor=2.0 | 显式 on_disk + 32x + m32/efC100，不是 IVF |

OS disk 协议要求 OpenSearch ≥3.3；Lucene fp32/BBQ 定义的目标版本为 ≥3.6。
ES bbq_disk 与 OS on_disk 只是驻盘策略对照，**并非同算法**。比较需固定查询集、返回 k，
并提供相近实测 recall 的证据；不能把相同预算数字/任务名称当成跨引擎可比性的保证。

`geonames-upsert` 的五个写操作统一 `bulk_size:500`、`bulk_indexing_clients:8`（都可覆盖），
共享自然 geonameid 分区与顺序。cold/index 写 `raceway_revision:0`，hot/native 写1，mixed写2；
所有 update 显式 `detect_noop:false`。native 基座先 index 同一自然 ID 全集，再逐 ID 纯 update，
不使用 conflicts 模型；hot/native 文档内容相同，仅有无 doc_as_upsert 不同。完整协议见
[geonames-upsert/README.md](geonames-upsert/README.md)。

离线运行时回归在 `tests/test_track_runtime.py`，不依赖 esrally，不读取真实 terms/语料，
以合成数据和可审计请求桩检查 body、ID、更新内容、分区及任务参数。

## 还没切完的部分（压测机侧，一次性）

1. **geonames 挑战比对**：仓库里的 `tracks/geonames/challenges/{write-only,query-only}.json`
   是从上游 `append-no-conflicts` 机械派生的；压测机基座 `$ROOT/tracks/geo/challenges/`
   里的同名文件是 raceway 自己那份。切之前逐字比对一次：
   ```bash
   diff $ROOT/tracks/geo/challenges/write-only.json $ROOT/tracks/geonames/challenges/write-only.json
   diff $ROOT/tracks/geo/challenges/query-only.json $ROOT/tracks/geonames/challenges/query-only.json
   ```
   有实质差异 → 以压测机那份为准回灌进 git，再切（别让口径在切换当天悄悄变）。
2. **ds-* 定义回灌**：`$ROOT/tracks/ds-*`（不含语料）落进 git，语料留机器。
3. **hnsw 基座回灌**：`hnsw2m-2m` 还在机器上（`$ROOT/tracks/hnsw2m-2m`），它的
   `index.json` 没有 `index_codec` 条件块 → `hnsw-codec-*` 变体会被
   `track_assert_storage_params` **直接拦下**（这是有意的：条件块缺失时压缩档会静默失效）。
   回灌时顺手把条件块补上，压缩变体才能用。
4. **旧目录清理**：压测机上的 `tracks/geo/`、`tracks/hv1024-s3/`、`tracks/nested-s3/`、
   `tracks/geo-codec-*/` 切换后不再被使用，确认无回灌需求再删；
   量化族同理（旧的 `tracks/hv1024x-int8/`、`-bbq/`、`-codec-zstd-sr/` 三份目录，
   机器上被 gitignore 的语料软链等残留还在，可手动清理）。
   ⚠️ 旧目录里的 `queries.json` / 语料软链是 gitignore 的产物，pull 不会自动清掉——
   留着不影响（runner 只认 `tracks/hv1024x`），但别被它们误导成"还在用"。
5. **编排台与看板的 `geo` 键**：编排层的 track id 保持 `geo` 不变，因此
   `bench-compose.py` 的 `'geo'` 键、RACES.md 记账、看板 `track:'geo'` 过滤**都不用改**
   （改名会让已有数据的口径漂移）；只有真要把 id 改成 `geonames` 时才需要联动。
