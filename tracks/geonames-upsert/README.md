# geonames-upsert track

基于官方 geonames 语料的 **upsert / 纯 update 对照**。索引为 `geonames-upsert`；
语料为 11,396,503 条地理信息文档，每条的自然 `geonameid` 作为 `_id`。
不使用 Rally conflicts 模型，也不生成另一套顺序 ID。

## 负载协议

所有写任务由同一个 `upsert-source` 参数源生成。每个客户端负责语料的连续、不重叠切片，
每阶段按相同顺序访问一次切片内全部 ID；尾数归最后一个客户端。选择相同
`ingest_percentage` / `bulk_indexing_clients` 时，两个对照访问的原始文档和 ID 完全一致。

| operation / task | bulk action | 文档内容 | 路径 |
|---|---|---|---|
| `upsert-insert` / `upsert-insert-cold` | update + doc_as_upsert | 原文 + `raceway_revision:0` | 空索引全部插入 |
| `upsert-update` / `upsert-update-hot` | update + doc_as_upsert | 原文 + `raceway_revision:1` | 全部真实更新 |
| `upsert-mixed` | update + doc_as_upsert | 原文 + `raceway_revision:2` | 默认25%偏移 ID 插入、75%原 ID 更新 |
| `index-append-ids` / `index-append-with-ids` | index | 原文 + `raceway_revision:0` | 同一自然 ID 全集插入 |
| `index-update-native` | update（**无** doc_as_upsert） | 原文 + `raceway_revision:1` | 同一自然 ID 全集真实更新 |

`raceway_revision` 是映射中声明的 integer 字段，不改变业务字段。hot/native 的
目标文档内容相同，只差有无 `doc_as_upsert:true`。所有 update body 显式携带
`detect_noop:false`，第一次 hot 把 revision 0 改为1，mixed 把1改为2；单独重跑同一
阶段仍强制重写，不能把默认 no-op 吞吐冒充真实更新。这里测的是**全字段 doc 合并重写**，
不是只改一个业务字段的轻量更新；客户端 JSON 解码/组装成本在所有对照中一致。

mixed 使用固定种子逐文档抽样，`new_doc_ratio` 是**期望比例**，不是强制精确配额。
新 ID=`geonameid + new_id_offset`，原文中的 geonameid 不变；offset 必须大于语料最大
geonameid。相同种子和客户端数产生相同抽样。重复运行 mixed 会命中已插入的偏移 ID，
**不再保持新文档比例**；要比较 insert/update 混合路径，必须从干净 cold/hot 基座开始。

## Challenges

| challenge | 说明 | 前置条件 |
|---|---|---|
| `upsert-flow`（默认） | cold → hot → mixed | 自动删除并重建索引 |
| `upsert-cold-insert` | 只测 cold | 自动删除并重建索引 |
| `upsert-hot-update` | 只测 hot，不删索引 | 已以相同语料、ingest_percentage、客户端切片跑完 cold |
| `upsert-mixed` | 只测 mixed，不删索引 | 已建好相同自然 ID 全集，且未使用同一 offset 跑过 mixed |
| `native-update` | 自然 ID index → 纯 update | 自动删除并重建索引 |
| `full` | upsert-flow，然后重建索引运行 native-update | raceway 单挑战入口 |

`full` 的两个周期先后执行、各自重建索引；结束的索引和赛级 store/segment 快照属于
**native 周期**，不能拿这一份快照代表 mixed 结束的体积。比较吞吐使用各自 task 指标。
hot 单独执行没有基座检查：缺文档的 upsert 会走插入路径；纯 update 缺文档则失败。

## 参数（--track-params）

| 参数 | 默认 | 说明 |
|---|---|---|
| `bulk_size` | 500 | **全部五个写任务共用**同一 bulk 大小，确保对照一致 |
| `bulk_indexing_clients` | 8 | 全部阶段共用并发客户端数 |
| `ingest_percentage` | 100 | (0,100]，每阶段处理相同的语料前缀，至少1条 |
| `new_doc_ratio` | 25 | mixed 新 ID 的期望百分比，[0,100] |
| `new_id_offset` | 1000000000 | mixed 新 ID 偏移，必须保证与原 ID 集不相交 |
| `upsert_seed` | 42 | mixed 随机种子；改变客户端数会改变抽样 |
| `corpus_file` | 自动定位 | 显式指定解压后的 documents-2.json；透传到所有五个任务 |
| `number_of_shards` / `number_of_replicas` | 5 / 0 | 索引设置 |
| `refresh_interval` | 1s | 刷新间隔 |
| `index_settings` | {} | 建索引时追加覆盖设置 |
| `include_force_merge` | false | flow/native/full 末尾是否合并；不插入 hot 测量之前 |
| `warmup_time_period` | 30 | 各 bulk 阶段预热秒数 |
| `cluster_health` | green | 等待的健康状态 |
| `error_level` | non-fatal | bulk 响应错误处理，与 Rally 语义一致 |

预热请求会消耗语料但不计入测量，小比例冒烟必须设 `warmup_time_period:0`。
若要测压实基座的更新，需在 cold 后自行显式合并，再单独跑 hot；仅设置
`include_force_merge:true` 不会把 flow 中 hot 的前置状态改为压实。

## 运行

```bash
# 单场包含两个周期，对照 bulk/clients 完全一致
esrally race --track-path=./geonames-upsert --challenge=full \
  --pipeline=benchmark-only --target-hosts=<host>:9200 \
  --track-params="bulk_size:500,bulk_indexing_clients:8"

# 小语料冒烟，不让预热耗尽测量数据
esrally race --track-path=./geonames-upsert --challenge=upsert-flow \
  --pipeline=benchmark-only --target-hosts=<host>:9200 \
  --track-params="ingest_percentage:5,warmup_time_period:0,bulk_size:200,bulk_indexing_clients:4,number_of_shards:3"
```

## 语料与验证

Rally 根据 track.json 下载 `https://rally-tracks.elastic.co/geonames/documents-2.json.bz2`。
也可用 `corpus_file` 指定解压语料。映射为 dynamic=strict，语料需符合官方 geonames schema，
geonameid 必须是非负整数、全局唯一；参数源对无效行或提前结束报错，不静默漏文档。

`tests/test_track_runtime.py` 用可审计的内存 bulk 桩验证最终 body、自然 ID 全集、
分区覆盖、revision 变化、mixed 路径和纯 update 语义，无需安装 esrally 或连接集群。
