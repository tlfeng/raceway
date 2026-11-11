# geonames-upsert track

基于官方 [geonames track](https://github.com/elastic/rally-tracks/tree/master/geonames) 改造的 **update / upsert 性能测试 track**。

- 语料与官方 geonames 完全相同：1139 万条地理信息文档（压缩 265MB，解压约 3.5GB），扁平 schema。
- 每条文档自带 `geonameid` 字段，作为 `_id`（天然业务主键）。
- upsert 通过 bulk API 执行，body 为 `{"update": {"_id": ...}}` + `{"doc": ...,"doc_as_upsert":true}`，覆盖 insert 与 update 两条内部路径。
- 另附 `native-update` challenge，用 Rally 内置的 `on-conflict: update` 路径（普通部分文档更新，无 upsert 语义）作对照。

## Challenges

| challenge | 说明 | 前置条件 |
|---|---|---|
| `upsert-flow`（默认） | 三个连续阶段：① 空索引上全量 upsert（全部走 insert 路径）② 全量 upsert（全部走 update 路径）③ 混合负载（默认 25% 新 id + 75% 已存在 id） | 无，自动建索引 |
| `upsert-cold-insert` | 只做阶段①：空索引纯 upsert-insert 吞吐 | 无 |
| `upsert-hot-update` | 只做阶段②：对已有数据纯 upsert-update 吞吐 | 需先跑过 `upsert-cold-insert` 或 `upsert-flow`（**不删索引**） |
| `upsert-mixed` | 只做阶段③ | 同上 |
| `native-update` | 先用 Rally 生成的顺序 id 全量索引，再用 `on-conflict: update` 全量更新（Rally 原生 update 路径） | 无，自动建索引 |

## 运行

```bash
# 完整 upsert 生命周期（cold insert -> hot update -> mixed）
esrally race --track-path=/path/to/geonames-upsert \
  --pipeline=benchmark-only --target-hosts=<host>:9200

# 只测冷启动 upsert-insert
esrally race --track-path=/path/to/geonames-upsert --challenge=upsert-cold-insert ...

# 热更新（先跑过上面任一建数据的 challenge）
esrally race --track-path=/path/to/geonames-upsert --challenge=upsert-hot-update ...

# Rally 原生 update 对照
esrally race --track-path=/path/to/geonames-upsert --challenge=native-update ...
```

对照建议：同一集群、相同参数下分别跑 `upsert-flow` 与 `native-update`，对比三个指标：
1. `upsert-insert-cold`（upsert 插入）vs `index-append-with-ids`（普通 index 插入，指定 id）；
2. `upsert-update-hot`（doc_as_upsert 更新）vs `index-update-native`（Rally 原生 update）；
3. 混合负载 `upsert-mixed` 的吞吐与错误率。

## 参数（--track-params）

| 参数 | 默认 | 说明 |
|---|---|---|
| `bulk_size` | 5000 | Rally 原生 bulk 操作（append/native-update）的 bulk 大小 |
| `upsert_bulk_size` | 500 | upsert bulk 大小。upsert body 每条比 index 多一层数据，且服务端要先查后写，建议从 500 起步 |
| `bulk_indexing_clients` | 8 | 并发 bulk 客户端数 |
| `ingest_percentage` | 100 | 每个阶段处理的语料百分比（冒烟测试可先设 1~5） |
| `new_doc_ratio` | 25 | 混合阶段中新文档（insert 路径）占比，0~100 |
| `new_id_offset` | 1000000000 | 混合阶段新 id 的偏移量，需远大于语料最大 geonameid 以保证命中新 id |
| `upsert-seed` | 42 | 混合阶段 id 抽样的随机种子。客户端数不变时结果可复现；改变 `bulk_indexing_clients` 会改变各客户端抽样序列 |
| `number_of_shards` / `number_of_replicas` | 5 / 0 | 索引设置 |
| `refresh_interval` | 1s | 可调大（如 30s）以观察 refresh 策略对 update 的影响 |
| `index_settings` | {} | 追加覆盖任意索引设置 |
| `include_force_merge` | false | 结束后是否 force-merge |
| `warmup_time_period` | 30 | 各 bulk 阶段预热秒数 |
| `cluster_health` | green | 等待的集群健康状态 |
| `error_level` | non-fatal | bulk 出现响应错误（如 429 限流拒绝）时是否中止 race，与官方 track 语义一致 |
| `corpus-file` | （自动） | 显式指定解压后的语料文件路径（默认从 Rally 数据目录自动定位） |

示例：

```bash
# 冒烟：5% 数据量。warmup_time_period 必须为 0，否则预热期可能把小语料整个消耗掉，测量阶段无数据
esrally race --track-path=./geonames-upsert --challenge=upsert-flow \
  --pipeline=benchmark-only --target-hosts=10.0.0.1:9200 \
  --track-params="ingest_percentage:5,warmup_time_period:0,bulk_indexing_clients:4,upsert_bulk_size:200,number_of_shards:3"
```

## 语料下载

首次运行时 Rally 自动从 `https://rally-tracks.elastic.co/geonames` 下载并解压到
`~/.rally/benchmarks/data/geonames-upsert/geonames/`。若 VM 无法访问外网，可提前在有网的机器下载：

```
https://rally-tracks.elastic.co/geonames/documents-2.json.bz2
```

放到上述目录（Rally 会自动解压），或解压后通过 `corpus-file` 参数指定路径。

## 注意事项

- `upsert-hot-update` / `upsert-mixed` 不删除索引，**必须先跑** `upsert-cold-insert` 或 `upsert-flow`，否则所有 upsert 都会落到 insert 路径，结果与 challenge 名称含义不符（不会报错，务必按顺序执行）。
- **预热期的消耗**：各 bulk 阶段先执行 `warmup_time_period`（默认 30s）预热，预热期间的请求不计入统计。全量跑（100%）时这只是一小部分；但 `ingest_percentage` 很小时预热可能耗尽全部语料，导致正式测量阶段无数据——此时必须设 `warmup_time_period: 0`。
- **阶段间的 segment 状态继承**：`upsert-flow` 中 update / mixed 阶段承接 insert 阶段刚写完的 segment 布局（默认不做 force-merge），模拟的是"持续更新"的真实场景；`native-update` challenge 的结构相同，因此 `upsert-flow` 与 `native-update` 的横向对比是公平的。若要测"索引已压实后的更新性能"，可设 `include_force_merge: true` 并分别单独跑 cold/hot challenge。
- 每次 race 的 mixed 阶段都会写入新 id 文档（geonameid + offset），重复跑 `upsert-mixed` 会在这些 id 上反复 upsert，不影响统计，但会缓慢增加索引体积；需要干净环境时用 `upsert-flow` 或 `upsert-cold-insert` 重建索引。
- 结果中每个 bulk 阶段的 `index`/`update` 计数在 `detailed-results` 开启时更直观，但该选项开销极大（Rally 官方标注可达数千倍开销），只建议小规模冒烟时开启。
- 需要 Rally ≥ 2.x（custom param source API）。easysearch 需支持 bulk update / `doc_as_upsert`（Elasticsearch 兼容语义）。
