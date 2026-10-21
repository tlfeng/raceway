# kibana — 建板工具

入库链路 `bench-export.py → bench-apply.py` 的配套：生成并导入 Kibana 仪表盘（TSVB 面板），
看 bench-env / bench-run / bench-metric 三索引。

## 环境

两种起法，任选其一：

| 方式 | 版本 | 说明 |
|---|---|---|
| 本机 `es-kibana.sh`（**推荐**） | ES + Kibana **7.17.29 / 8.1.3** | 工作区脚本，启停干净、无需 Docker、无密码无 TLS |
| 本目录 `docker-compose.yml` | ES/Kibana 7.10.2 OSS | 备选；无认证，Apple Silicon 走 Rosetta |

```bash
# 本机（es-kibana.sh 在压测工作区，不在本仓库）
<工作区>/es-kibana.sh start all --version 7.17    # 或 --version 8.1.3

# 或 docker
docker compose up -d          # down -v 清数据
```

### 7.x 与 8.x 的兼容点（都实测过）

`make_dashboard.py` 会先问 Kibana 自己是什么版本（`GET /api/status` → `version.number`），
再按版本生成 saved object。差异集中在三处：

| 点 | Kibana 7.17 | Kibana 8.1 |
|---|---|---|
| `visualization.attributes.version` | 映射是 **integer**，写字符串整批导入失败 → **不写**（Kibana 自带样例里也没这字段，不是必填） | 同样不写（历史上写过非 semver 值让 8.x 迁移函数抛 `Invalid Version: 1`） |
| `kibanaSavedObjectMeta.searchSourceJSON` | 按 7.17 自带样例写 `{"query":{"query":"","language":"kuery"},"filter":[]}` | `{"filter":[]}` |
| `index-pattern.attributes.timeFieldName` | 必填字符串，取不到就兜底第一个 date 字段 | 同 |

> 7.17 的 saved object 索引是 `.kibana_7.17.29_001`（8.x 是 `.kibana_1`），
> 两个版本的看板各自独立，互不覆盖。

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

`kibana-objects.ndjson` —— **270 个 saved object**（仅 python3 标准库）：

- 3 个 **index-pattern**：bench-metric / bench-run / bench-env（字段从 ES mapping 实时读）
- **10 个 dashboard**：
  - `bench-overview` 总览（环境说明 + 赛道导航 + 看图指南）
  - `bench-<track>` **每个 esrally track 一个独立看板**，当前 9 个：
    `geo`（geonames 文本检索）/ `hnsw`（HNSW 96 维）/ `nested`（嵌套文档）/
    `ds-j512` `ds-j768` `ds-g960` `ds-j1024` `ds-g1920` `ds-j2048`（向量维度扫描）
- **218 个 TSVB 面板**：按「赛道 × workload」动态生成，分四段：
  - **① 写入**：写入吞吐、索引总耗时、Merge/Refresh/Flush 耗时、索引限流时长、索引活动次数
  - **② 查询**：每 workload 一组 —— 吞吐 + **延迟分位 p50/p90/p99/p999** + **处理时间分位 p50/p90/p99**；外加错误率
  - **③ 资源与存储**：存储体积、段数、GC 耗时与次数（young/old）
  - **④ 环境健康度**（仅带 monitor 采集的赛道）：CPU steal、外来进程 CPU、换入次数
- 3 个 **markdown**：环境说明（环境台账 + 场次体检）、**赛道导航**、看图指南
- 36 个 **markdown 段标题**：每赛道 4 个（① 写入 ② 查询 ③ 资源与存储 ④ 环境健康度）

### 为什么按赛道拆看板

设计对照 <https://opensearch.org/benchmarks/>：那边 HTTP logs / NYC taxis / PMC / Big5 /
Vectorsearch-* 各自独立页面，**不同 track 不混在一张图里**。我们同理：

- **赛道之间不可比** —— 语料、维度、task 集都不同，量纲不同，横向比较没有意义。
  之前把所有赛道的 `family:throughput` 画在一张图上，ge 检索的 700 ops/s 和
  KNN 的 1.5 ops/s 混成一条线，是误读。
- **赛道内还要按 workload 分图** —— 同一赛道内不同 task 的量纲也不同：
  `term` 检索 703 ops/s vs `country` 聚合 35 ops/s vs `decay_geo_gauss` 1.5 ops/s。
  所以每个 workload（term/agg/sort/decay/function_score/large-terms/painless/scroll/
  nested-*/KNN k 值）单独出图，组内才是同量纲可比的。
- 赛道清单**不硬编码**：从 ES 实算。新增一场新赛道的压测后重跑脚本就自动多一个看板。

**进各赛道看板**：总览页的导航表给出路径（`/app/dashboards#/view/bench-<track>`），
或左侧栏 ☰ → Analytics → **Dashboard**（`/app/dashboards/list`）点标题。

> Kibana 8.1 的 markdown 面板里 `[文字](url)` 点击后**不会**让父页面跳转（链接渲染在
> 面板自己的渲染上下文里），绝对 URL 会被安全策略降级成纯文本，而 `link` 可视化类型在
> 8.1 已不支持 —— 三条路都实测过，所以导航靠路径 + Kibana 自己的看板列表。

> **`kibana-objects.ndjson` 不入库**（已在 `.gitignore`）。它的"环境说明"面板是从
> `bench-env` 索引现场读出来渲染的，因此产物内嵌真实环境指纹（env_id、CPU 型号、kernel）
> 与场次统计，且随每次入库数据变化 —— 属于本地生成物，按上面的命令重建即可。

## 口径与交互

- **面板过滤用 Lucene 语法**，写在 `params.filter` 里，形态必须是
  `{"language": "lucene", "query": "..."}` —— 传纯字符串会被服务端静默丢弃（面板不报错但数据混图）。
- **每个面板都带 `track:<赛道>` 约束**，外加该 workload 的 task 列表：
  `track:geo AND family:throughput AND unit:"ops/s" AND stat:median AND (task:term OR task:phrase)`。
- **family 取值来自 `bench-export.py` 的 `RESULT_METRICS`**，且**不是按名字归一的**：
  `merge_count` 属 `merge`、`merge_time` 属 `merge_time`、`merge_throttle_time` /
  `refresh_time` / `flush_time` 各是独立 family。写错 family 面板恒空。
- **仪表盘级过滤放顶部 KQL 查询框**（`searchSourceJSON.query`，默认 `valid: true`），
  用户可直接改成 `series_key:es@*` / `valid:false` / `campaign:ds3-20261015-233223`
  （前缀匹配**不要加引号**：带引号是精确短语，实测匹配 0 条）。
  注意 system 指标行的 `valid` 是**按场次回填**的（见下），否则会被这个过滤整体清空。
- **时间窗按数据实际范围自动计算**（`data_window(track)`），不设 `timeRestore` 会退回
  "最近 15 分钟"导致历史场次一片空白。每个看板各按自己赛道的批次走。
- **桶粒度按该赛道的跨度定**（`pick_interval()`：跨天用 1d、半天内用 2h）：
  固定 1d 会让只跑了几小时的赛道（向量维度扫描整场 campaign 只跨 12 小时）全部数据
  落进一个桶 = 一个数据点，折线只有一个顶点画不出来。
- **没有数据的指标不出图**（`group_has_metric()` / `system_metric_exists()`）：例如
  `scroll` 是分页操作，只有 latency 没有 throughput，硬出图就是一个恒空的
  "No results found" 面板。同理 `memory_*` 六个分量实测全为 0（rally 报告未填充）、
  `translog_size` 赛末已清空（≈0），都不出图。
- **`service_time` 不单独出图**：无排队等待时它与 `latency` 逐点相等（rally 的
  `latency = service_time + queue`），单独画一图全是与延迟重复的线。
- **导入前会清理旧对象**（`prune_stale()`）：Kibana 的导入只覆盖同名对象、不会删旧文件，
  改了面板结构后旧面板会变成孤儿留在 Kibana 里。
- 索引 schema 变化后（如新增维度）重跑 `make_dashboard.py` 刷新面板过滤条件。

### 校验看板有没有真出数

光看"文档数 > 0"不够 —— **terms 分组拿不到 series 时前端是空图，但 count 仍大于 0**。
校验要同时带上 dashboard 级 KQL 和面板 filter，并确认分组聚合的 buckets 非空：

```bash
# 每个面板: (dashboard 的 KQL) AND (面板 filter) → terms 分组 → buckets 非空
```

工作区自带的 `verify-dashboard.py` 就是干这个的 —— 它直接调 Kibana 自己的 TSVB 渲染接口
`POST /api/metrics/vis/data`（7.x / 8.x 同路径），把看板级 KQL 并进面板 filter、
用看板存的时间窗发真实请求，要求返回的 series 里 bucket 非空：

```bash
python3 <工作区>/verify-dashboard.py --kibana http://127.0.0.1:5601
python3 <工作区>/verify-dashboard.py --kibana http://127.0.0.1:5601 --dash bench-geo
```

> 踩坑：Kibana 7.11+ 的 dashboard 迁移会把 `panelsJSON` 里面板的 `id` 抹掉，改成
> `panelRefName` + `references`（references 的 `name` 是 panelIndex，面板侧是
> `panelRefName`）—— 校验脚本按 panelIndex 关联才对得上。

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
