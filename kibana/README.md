# kibana — 建板工具

入库链路 `bench-export.py → bench-apply.py` 的配套：生成并导入 Kibana 仪表盘（TSVB 面板），
看 bench-env / bench-run / bench-metric 三索引。

## 环境

两种起法，任选其一：

| 方式 | 版本 | 说明 |
|---|---|---|
| 本机 `es-kibana.sh`（**推荐**） | ES 8.1.3 + Kibana 8.1.3 | 工作区脚本，启停干净、无需 Docker |
| 本目录 `docker-compose.yml` | ES/Kibana 7.10.2 OSS | 备选；无认证，Apple Silicon 走 Rosetta |

```bash
# 本机（es-kibana.sh 在压测工作区，不在本仓库）
<工作区>/es-kibana.sh start all

# 或 docker
docker compose up -d          # down -v 清数据
```

## 用法

在本目录下执行：

```bash
python3 make_dashboard.py --import \
        --es http://127.0.0.1:9200 --kibana http://127.0.0.1:5601 \
        --out kibana-objects.ndjson
```

| 参数 | 说明 |
|---|---|
| `--import` | 生成后直接导入 Kibana（overwrite，可重复跑） |
| `--out` | 产物路径，默认 `kibana/kibana-objects.ndjson` |
| `--es` / `--kibana` | 地址，默认 localhost:9200 / :5601 |

导入后打开 <http://localhost:5601/app/dashboards#/view/bench-overview>。

完整链路（含导出与入库）用压测工作区的 `bench-pipeline.sh` 一键跑。

## 产出

`kibana-objects.ndjson` —— 21 个 saved object（仅 python3 标准库）：

- 3 个 **index-pattern**：bench-metric / bench-run / bench-env（字段从 ES mapping 实时读）
- 11 个 **TSVB 面板**：写入吞吐、索引总耗时、查询吞吐、查询延迟 p90、服务时间 p99、错误率、
  KNN 检索吞吐、向量维度扫描、存储占用、段数量、Young GC 耗时
- 2 个 **markdown**：环境说明（环境台账 + 场次体检）、看图指南
- 4 个 **markdown 段标题**：① 写入 ② 查询 ③ 向量检索 ④ 资源与存储
- 1 个 **dashboard**：`bench-overview`（Benchmark 性能总览 · easysearch vs ES）

设计对照 <https://opensearch.org/benchmarks/>：每图一个指标族、series = 实验臂（向量面板按
`dims`）、x 轴 = 场次时间、底部环境说明。

> **`kibana-objects.ndjson` 不入库**（已在 `.gitignore`）。它的"环境说明"面板是从
> `bench-env` 索引现场读出来渲染的，因此产物内嵌真实环境指纹（env_id、CPU 型号、kernel）
> 与场次统计，且随每次入库数据变化 —— 属于本地生成物，按上面的命令重建即可。

## 口径与交互

- **面板过滤用 Lucene 语法**，写在 `params.filter` 里，形态必须是
  `{"language": "lucene", "query": "..."}` —— 传纯字符串会被服务端静默丢弃（面板不报错但数据混图）。
- **family 取值来自 `bench-export.py` 的 `RESULT_METRICS`**：`segment_count` / `store_size` 属
  `disk`，`total_time` 属 `indexing_time`，`young_gc_time` 属 `gc`。写错 family 面板恒空。
- **仪表盘级过滤放顶部 KQL 查询框**（`searchSourceJSON.query`，默认 `valid: true`），
  用户可直接改成 `track:geo` / `dims:1024` / `valid:false`。
- **时间窗按数据实际范围自动计算**（`data_window()`），不设 `timeRestore` 会退回"最近 15 分钟"
  导致历史场次一片空白。
- **桶粒度固定**（常规 1d、向量面板 2h）：`interval:auto` 在大跨度下会把所有场次聚成单桶，
  折线只有一个顶点画不出来。
- 索引 schema 变化后（如新增维度）重跑 `make_dashboard.py` 刷新面板过滤条件。

## es-proxy.py — 诊断工具

面板不出数据时，用它代理 Kibana→ES，看 ES 到底收到了什么查询：

```bash
python3 es-proxy.py --listen 127.0.0.1:9201 \
        --upstream http://127.0.0.1:9200 --log /tmp/es-query.log
# 改 kibana.yml: elasticsearch.hosts: ["http://127.0.0.1:9201"] → 重启 Kibana → 打开看板
# /tmp/es-query.log 里有每次查询的请求体与响应
```

排查完**务必**把 `kibana.yml` 改回 9200。代理必须补 `X-Elastic-Product: Elasticsearch`
响应头，否则 ES 8 的客户端校验会拒绝连接。

更细的踩坑记录见压测工作区的
`FLOW-benchmark结果入库ES与Kibana看板.md` 第六节。
