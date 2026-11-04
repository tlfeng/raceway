# kibana — Kibana 7.17 建板工具

入库链路 `bench-export.py → bench-apply.py` 的配套：生成并导入 Kibana 仪表盘（TSVB 面板），
看 `benchmark-result-env` / `benchmark-result-race` / `benchmark-result-metric` 三索引。

> **口径基准是 v2 契约**（见压测工作区 `DESIGN-三索引数据存储方案.md`）：
> 库里没有 `valid` / `status` / `quality`，只有行级布尔 `trusted`；
> 看板级默认闸门是 `trusted: true AND NOT codec:(zstd or best_compression)`。
> 改面板口径前先对一遍那份文档，别照着旧 `valid`/`quality` 写。

## 目录里有什么

| 文件 | 作用 |
|---|---|
| `make_dashboard.py` | 建板器：从 ES 实时读 mapping 与场次，生成 saved objects 并可直连导入 |
| `verify-dashboard.py` | 验收：逐面板调 Kibana 自己的 TSVB 渲染接口，确认真的出数 |
| `panel-manifest.py` | 从实际导入的 saved objects 反解出《面板清单》（地面真相） |
| `shim-7.17-es250.js` | 协议适配层：Kibana 7.17 → EasySearch 2.5.0（产品校验 + API 差异兜底） |
| `deploy-kibana.sh` | 压测机上起停 Kibana 7.17 + shim，并生成对接指标库的 `kibana.yml` |
| `pull-kibana-image.sh` | Kibana 7.17.29 的**安装**器：不装 Docker，直接走 registry API 取镜像层解开用 |

> `artifacts.elastic.co` 的下载路径在压测机网络里被封，只能走 registry API 取镜像层解开用。
> 本目录是自成一体的工具链 —— 装 Kibana（`pull-kibana-image.sh`）和起 Kibana
> （`deploy-kibana.sh`）的脚本都在这里，压测机上整个 `kibana/` 目录一起拷过去即可。

## 环境

### 压测机（对接指标库，推荐拓扑）

```
Kibana 7.17.29   127.0.0.1:5601
      │ 协议适配 shim（Node，用 Kibana 自带运行时）
      ▼
EasySearch 2.5.0 指标库   https://127.0.0.1:9237
```

```bash
# 1) 装 Kibana 7.17.29（产物 /testing/kibana-rootfs/usr/share/kibana）
bash ./pull-kibana-image.sh                      # tag=7.17.29 dest=/testing/kibana-rootfs

# 2) 起 Kibana + shim（ES_PORT/ES_USER/ES_PASS 指向指标库）
ES_PORT=9237 ES_USER=admin ES_PASS='***' \
KB_ROOT=/testing/kibana-rootfs/usr/share/kibana \
  bash deploy-kibana.sh start
bash deploy-kibana.sh status
```

**为什么必须有 shim**：Kibana 7.17 的 ES 客户端做产品校验（version / tagline / build_flavor
+ `X-elastic-product` 头），EasySearch 2.5.0 默认不满足 → Kibana 直接崩溃。
不改指标库配置去开 `api_compatibility`：指标库是入库管道的目标，不为了看板去动它；
改写放在代理侧，指标库零变更。

> 远程访问：Kibana 只听压测机回环，需要 `ssh -L 5601:127.0.0.1:5601 <user>@host` 建隧道。

### 本机（开发/自测）

工作区的 `es-kibana.sh`（**不在本仓库**）起 ES + Kibana 7.17.29 / 8.1.3，
启停干净、无需 Docker、无密码无 TLS：

```bash
<工作区>/es-kibana.sh start all --version 7.17      # 或 --version 8.1.3
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

`kibana-objects.ndjson`（仅 python3 标准库）：

- **3 个 index-pattern**：`benchmark-result-metric` / `benchmark-result-race` / `benchmark-result-env`
  （字段从 ES mapping 实时读，不硬编码）
- **dashboard**：总览 `bench-overview` + 每个 esrally track 一块 `bench-<track>`
  + 维度扫描合并板 `bench-dimsweep`（维度轴）/ `bench-dimsweep-car`（车轴）
- **visualization**：TSVB 时序图 + markdown 文本面板（段标题、维度对照表、图例说明）

面板数量随赛道与 workload 集变化，**以实际导入为准**，不要在文档里写死数字
（历史口径散落在多处，已经漂过好几轮）。

TSVB 时序图按「赛道 × workload」动态生成，分四段：
**① 写入**（吞吐、索引总耗时、Merge/Refresh/Flush 耗时、限流、活动次数）、
**② 查询**（每 workload 一组吞吐 + 延迟分位 + 处理时间分位，外加错误率）、
**③ 资源与存储**（存储体积、段数、GC 耗时与次数）、
**④ 环境健康度**（仅带 monitor 采集的赛道：CPU steal、外来进程 CPU、换入次数等）。

### 哪些赛道不进看板

赛道清单**从 ES 实算**（不硬编码），但有两类例外：

| 例外 | 规则 | 原因 |
|---|---|---|
| 维度扫描档 `ds-*` | `DS_TRACK_RE` 匹配即跳过 | 已合并进 `bench-dimsweep` / `bench-dimsweep-car`，不再一页一档 |
| 一次性对照场次 | `ONE_OFF_TRACKS` 显式列出 | 不是可复跑的赛道，是一次决策实验（详见 `make_dashboard.py` 里的注释） |

`ONE_OFF_TRACKS` 当前是 `('hvtp200k-2s', 'hvtp200k-3s')` —— 2026-10-15「分片布局复测」的
对照组（20 万条 × 1024 维，只测"2 主分片 vs 3 主分片"一个变量），结论已定稿进
《向量场景控标技术指标-20261015.md》§五，**结论走文档不走看板**。
不放的理由写在 `make_dashboard.py` 的 `ONE_OFF_TRACKS` 注释里（无 RACES.md 记账 → 导出期
就不写库；`shards`/`clients`/`bulk_size`/`corpus`/`dims` 因无 sidecar 全为 null，
读者无法验证两场同口径）。

### 面板清单（机读生成）

`panel-manifest.py` 从**实际导入 Kibana 的 saved objects** 反解出《面板清单》
（每个图的标题 / saved object id / 过滤串 / 分线字段 / 聚合 / 数字格式 / 桶粒度）——
清单是看板的地面真相，手写一次就漂一次：

```bash
python3 panel-manifest.py --in kibana-objects.ndjson --out 看板面板清单.md \
        --source-note "结果入库/exports/<tag>/kibana-objects.ndjson"
```

只读 ndjson、**不连 Kibana** —— 产物即"这次导入到底建了什么"。改看板后要重新生成。

### 为什么按赛道拆看板

设计对照 <https://opensearch.org/benchmarks/>：那边 HTTP logs / NYC taxis / PMC / Big5 /
Vectorsearch-* 各自独立页面，**不同 track 不混在一张图里**。我们同理：

- **赛道之间不可比** —— 语料、维度、task 集都不同，量纲不同，横向比较没有意义。
- **赛道内还要按 workload 分图** —— 同一赛道内不同 task 的量纲也不同：
  `term` 检索 703 ops/s vs `country` 聚合 35 ops/s vs `decay_geo_gauss` 1.5 ops/s。
- 赛道清单**不硬编码**：从 ES 实算。新增一场新赛道的压测后重跑脚本就自动多一个看板。

**进各赛道看板**：总览页的导航表给出路径（`/app/dashboards#/view/bench-<track>`），
或左侧栏 ☰ → Analytics → **Dashboard**（`/app/dashboards/list`）点标题。

> Kibana 8.1 的 markdown 面板里 `[文字](url)` 点击后**不会**让父页面跳转（链接渲染在
> 面板自己的渲染上下文里），绝对 URL 会被安全策略降级成纯文本，而 `link` 可视化类型在
> 8.1 已不支持 —— 三条路都实测过，所以导航靠路径 + Kibana 自己的看板列表。

> **`kibana-objects.ndjson` 不入库**（已在 `.gitignore`）。它的"环境说明"面板是从
> `benchmark-result-env` 索引现场读出来渲染的，因此产物内嵌真实环境指纹（env_id、CPU 型号、
> kernel）与场次统计，且随每次入库数据变化 —— 属于本地生成物，按上面的命令重建即可。

## 口径与交互

- **面板过滤用 Lucene 语法**，写在 `params.filter` 里，形态必须是
  `{"language": "lucene", "query": "..."}` —— 传纯字符串会被服务端静默丢弃（面板不报错但数据混图）。
- **每个面板都带 `track:<赛道>` 约束**，外加该 workload 的 task 列表：
  `track:geo AND family:throughput AND unit:"ops/s" AND stat:p50 AND (task:term OR task:phrase)`。
- **family 取值来自 `bench-export.py` 的 `RESULT_METRICS`**，且**不是按名字归一的**：
  `merge_count` 属 `merge`、`merge_time` 属 `merge_time`、`merge_throttle_time` /
  `refresh_time` / `flush_time` 各是独立 family。写错 family 面板恒空。
- **看板级过滤放顶部 KQL 查询框**（`searchSourceJSON.query`，默认
  `trusted: true AND NOT codec:(zstd or best_compression)`），用户可直接改成
  `series_key:es@*` / `trusted:false` / `campaign:ds3-20261015-233223`
  （前缀匹配**不要加引号**：带引号是精确短语，实测匹配 0 条）。
  - `trusted` 是行级布尔：这一行的数字能不能用。`trusted=false` 的行量的是"失败响应有多快"，
    混进延迟曲线会把引擎显得比实际更快，所以测量类面板一律只取 `trusted:true`。
    **错误率面板是唯一的例外**，它刻意不带这道闸门 —— 它要显示的就是失败场次。
  - 存储变体负条件：跨批聚合默认只看默认压缩，zstd / best_compression 各带一套存储设置，
    混进同一条线绝对值不可比；要单独看它们去「存储变体对比」页。
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
python3 verify-dashboard.py --kibana http://127.0.0.1:5601
python3 verify-dashboard.py --kibana http://127.0.0.1:5601 --dash bench-geo
```

它直接调 Kibana 自己的 TSVB 渲染接口 `POST /api/metrics/vis/data`（7.x / 8.x 同路径），
把看板级 KQL 并进面板 filter、用看板存的时间窗发真实请求，要求返回的 series 里 bucket 非空。

> 踩坑：Kibana 7.11+ 的 dashboard 迁移会把 `panelsJSON` 里面板的 `id` 抹掉，改成
> `panelRefName` + `references`（`references` 的 `name` 是 panelIndex，面板侧是
> `panelRefName`，两边名字对不上）—— 校验脚本按 panelIndex 关联才对得上。

## 排查：面板不出数

shim 自己会落盘每次转发的请求体与响应（`/tmp/es_shim.log`，各 60 KB / 3 KB 截断），
直接看它就知道 ES 收到了什么查询：

```bash
tail -f /tmp/es_shim.log
```

排查完不必还原任何配置 —— shim 是常驻代理，不像改 `kibana.yml` 那种要改回来。

更细的踩坑记录见压测工作区的 `FLOW-benchmark结果入库ES与Kibana看板.md` 第六节。

## 历史

2026-11-02 上游 `ebd5c86` 以「移除 Kibana 看板链路与 HTML 报告工具」为名删掉了本目录与
`bench-report.py`。2026-11-03 恢复**仅建板链路**（本目录 5 个文件 +
`pull-kibana-image.sh`，安装器后来也从仓库根目录归位到本目录），
并按 v2 契约改了面板口径；未恢复的是与建板无关的部分：
HTML 报告工具链（`bench-report.py` + `echarts.min.js`）、
7.10.2 的 `docker-compose.yml`（不是 7.17，且与工作区 `es-kibana.sh` 冲突）、
`es-proxy.py`（shim 已落请求体，功能被覆盖）。
