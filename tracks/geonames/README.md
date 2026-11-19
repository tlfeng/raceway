## Geonames track

This track is based on a [geonames](http://www.geonames.org/) dump of the file [allCountries.zip](http://download.geonames.org/export/dump/allCountries.zip) retrieved as of April 27, 2017. 

For further details about the semantics of individual fields, please see the [geonames dump README](http://download.geonames.org/export/dump/readme.txt).

Modifications:

* The original CSV data have been converted to JSON.
* We combine the original `longitude` and `latitude` fields to a new `location` field of type [geo_point](https://www.elastic.co/guide/en/elasticsearch/reference/current/geo-point.html).

### Example Document

```json
{
  "geonameid": 2986043,
  "name": "Pic de Font Blanca",
  "asciiname": "Pic de Font Blanca",
  "alternatenames": "Pic de Font Blanca,Pic du Port",
  "feature_class": "T",
  "feature_code": "PK",
  "country_code": "AD",
  "admin1_code": "00",
  "population": 0,
  "dem": "2860",
  "timezone": "Europe/Andorra",
  "location": [
    1.53335,
    42.64991
  ]
}
```

### Parameters

This track allows to overwrite the following parameters with Rally 0.8.0+ using `--track-params`:

* `bulk_size` (default: 5000)
* `bulk_indexing_clients` (default: 8): Number of clients that issue bulk indexing requests.
* `ingest_percentage` (default: 100): A number between 0 and 100 that defines how much of the document corpus should be ingested.
* `conflicts` (default: "random"): Type of id conflicts to simulate. Valid values are: 'sequential' (A document id is replaced with a document id with a sequentially increasing id), 'random' (A document id is replaced with a document id with a random other id).
* `conflict_probability` (default: 25): A number between 0 and 100 that defines the probability of id conflicts. This requires to run the respective challenge. Combining ``conflicts=sequential`` and ``conflict-probability=0`` makes Rally generate index ids by itself, instead of relying on Elasticsearch's `automatic id generation <https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-index_.html#_automatic_id_generation>`_.
* `on_conflict` (default: "index"): Whether to use an "index" or an "update" action when simulating an id conflict.
* `recency` (default: 0): A number between 0 and 1 that defines whether to bias towards more recent ids when simulating conflicts. See the [Rally docs](http://esrally.readthedocs.io/en/latest/track.html#bulk) for the full definition of this parameter. This requires to run the respective challenge.
* `number_of_replicas` (default: 0)
* `number_of_shards` (default: 5)
* `max_num_segments`: The maximum number of segments to force-merge to.
* `source_enabled` (default: true): A boolean defining whether the `_source` field is stored in the index. 
* `index_settings`: A list of index settings. Index settings defined elsewhere (e.g. `number_of_replicas`) need to be overridden explicitly.
* `cluster_health` (default: "green"): The minimum required cluster health.
* `error_level` (default: "non-fatal"): Available for bulk operations only to specify ignore-response-error-level.
* `post_ingest_sleep` (default: false): Whether to pause after ingest and prior to subsequent operations.
* `post_ingest_sleep_duration` (default: 30): Sleep duration in seconds.
* `include_non_serverless_index_settings` (default: true for non-serverless clusters, false for serverless clusters): Whether to include non-serverless index settings.
* `include_force_merge` (default: true for non-serverless clusters, false for serverless clusters): Whether to include force merge operation.
* `include_target_throughput` (default: true for non-serverless clusters, false for serverless clusters): Whether to apply target throughput.

仅 `lite` / `lite-encrypted` 缩量探针使用（`full` 等定版挑战不读这几个参数）：

* `lite_ingest_percentage` (default: 5): 缩量探针的入库比例，0 到 100。5 ≈ 57 万条；首跑后按实测写吞吐调，别调回 100。
* `lite_bulk_size` (default: 2000): 缩量探针的 bulk 大小。
* `lite_max_num_segments` (default: 1): 缩量探针 force-merge 的目标段数。

### 缩量探针（`lite` / `lite-encrypted`）

`encrypted-store` 与 `full` 的查询段按 `target-throughput` 定死（合计 ≈ 78 分钟，**与数据量无关**），
所以"只砍数据量"救不了耗时。缩量探针同时砍两端：`lite_ingest_percentage` 控制入库量，
查询只留 7 个代表操作并把 `iterations` / `target-throughput` 调小，合计落在 **5-10 分钟**。

两个挑战由 `challenges/lite.json` 的**同一份 schedule** 经开关 `_enc` 生成，逐任务同序同预算，
唯一差异是 create-index 的 store 设置：

| 挑战 | store | 用途 |
| --- | --- | --- |
| `lite` | `index.store.type` = `store_type`（默认 `fs`） | 常规基线 |
| `lite-encrypted` | `encrypted` + `index.encryption.profile=index` | 索引加密臂（仅 Easysearch） |

两臂各自把 `store.type` 钉在 operation 级 settings 里（覆盖 `index.json` 的 body），
所以残留的 `encrypted_store:true` 不会把 `lite` 偷偷跑成加密臂。

```bash
# 常规臂
esrally race --pipeline=benchmark-only --target-hosts=<hosts> \
  --track-path=tracks/geonames --challenge=lite --track-params="number_of_shards:5,number_of_replicas:0"

# 加密臂（其余参数逐项相同）
esrally race --pipeline=benchmark-only --target-hosts=<hosts> \
  --track-path=tracks/geonames --challenge=lite-encrypted \
  --track-params="number_of_shards:5,number_of_replicas:0"

# 首跑偏慢时缩量（default 5）
... --track-params="number_of_shards:5,number_of_replicas:0,lite_ingest_percentage:2"
```

口径与禁忌：

* 时长构成 = 写段（随机器浮动）+ force-merge（随机器浮动）+ 查询段（**定死 162 s**）。首跑若超 10 分钟，降 `lite_ingest_percentage`。
* 缩量后 `store` 体积、`force-merge` 耗时、查询延迟**只能与另一臂对比**，不能与 `full` 台账的数字放在一起比。
* bulk 任务**不设** `warmup-time-period`：上游的 70 s 预热在 5% 语料下会把整段吞进 warmup，测量样本为零。
* 刻意不含 `*_with_after_*` 系列：它们的 `search_after` 烘死 `[1000000]` / `[5000000]`，缩量后命中为空，延迟没有意义。
* 走 runner 的 campaign 一律用 `full`，别把 `lite` 塞进台账（会判 `METRIC-NULL-FAIL`）。

### License

We use the same license for the data as the original data from Geonames:

```
This work is licensed under a Creative Commons Attribution 3.0 License,
see http://creativecommons.org/licenses/by/3.0/
The Data is provided "as is" without warranty or any representation of accuracy, timeliness or completeness.
```
