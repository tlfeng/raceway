# kibana — Benchmark 看板工具

`bench-export.py → bench-apply.py` 的配套，使用 `benchmark-result-env`、
`benchmark-result-race`、`benchmark-result-metric` 三索引。依赖 Python 标准库；
shim 使用 Node 标准库。生成器与验收器只有 CLI 入口才连接服务，导入模块可作离线测试。

## 工具

| 文件 | 作用 |
|---|---|
| `make_dashboard.py` | 读取新导出契约、生成 saved objects，可选导入 Kibana |
| `verify-dashboard.py` | 严格验收实际 TSVB 响应，失败返回非零 |
| `panel-manifest.py` | 从 ndjson 反解面板过滤、分线、统计、单位和时间桶 |
| `shim-7.17-es250.js` | Kibana 7.17 → EasySearch 协议适配；导入模块不会启动代理 |
| `deploy-kibana.sh` | 启停本地 Kibana + shim，检查 HTTP 成功与真正 ready |
| `pull-kibana-image.sh` | 下载并解开 Kibana 镜像层，不依赖 Docker |

## 看板契约

### 身份与实验隔离

- `series_key` 仅表示产品身份：引擎、版本、build、JDK。
- `track_variant` 是**真实 runner 赛道 id**；不能从量化参数猜测。`geonames` 的 runner
  基座为 `geo`，其余基座为 canonical track 原名。`hv1024x-os-bbq`、
  `hv1024x-os-disk`、`hv1024x-bbqdisk` 是独立基座，不剥后缀。
- `comparison_key = series_key + '|' + track_variant` 是所有数值曲线的分线键。
  metric/race 的 mapping 必须同时包含 `series_key`、`track_variant`、`comparison_key`、
  `knn_index_type`（keyword）；生成器直接从 mapping 构造 index-pattern，缺字段即失败。
- 每块独立赛道板固定一个 `track_variant`。默认基座板额外使用
  `NOT codec:(zstd or best_compression) AND NOT source_reuse:true`；非默认
  int8/bbq/codec/source_reuse runner 变体另建独立板，不施加基座负条件，因此不会被隐藏。
- 独立赛道板一律保留，包括 ds 单档、仅两台车的比较。维度合并页是附加入口，不替代单档板；
  维度合并页只比较各 ds 的默认 runner 基座。所有实际 variant/task 以 composite 聚合分页发现，
  不使用 terms top-N 作为完整赛道清单。

### 可信度与聚合

- **测量面板自身强制 `trusted:true`**。看板全局默认不含 trusted，避免隐藏失败证据。
- 错误率面板不加 trusted，按 task 单独展示桶内 `max(error_rate)`；0 合法，1 表示全失败。
- 生成前预检与运行时使用同一个全局 scope + 面板过滤。无效查询/HTTP 错误直接失败，
  不冒充“有数据”。没有真实数据的指标不出数值图，另列未出图说明。
- 每个 task、每个 stat（p50/p90/p99）、每个 GC/资源/system metric 独立出图。
  不按 `stat` 或 `metric` 取代身份分线；term/phrase、cached/uncached 也不混成一个数。
  未登记业务 task 动态兜底；建索引、刷新、等待等维护 task 不出业务性能图，但仍保留其错误率证据。
- 正式跨圈汇总用中位数：TSVB metric 为
  `{"type":"percentile","field":"value","percentiles":[{"value":"50","mode":"line"}]}`。
  `stat:p50` 是**单场** Rally 统计输入，不等于跨圈已经取过中位数。
  ES 表格聚合使用 `percentiles: {field: value, percents: [50]}`，不用 stats.avg。
  system monitor 是每场×节点×相位多行摘要，健康图明确标作“桶内最坏”并用 max，
  不把节点行误当各圈样本。错误率也用最坏值；二者不是正式性能中位线。
  百分位聚合是 ES 的近似 percentile 实现，不提供精确顺序统计保证。
- terms 按 `_key` 排序，不能直接按多值 percentile metric 排序。
- x 轴是 `run_start`（批次锚点），默认 1d 桶、`now-30d → now`；同桶同配置同 variant
  的各圈形成一个中位数点，**不宣称每场一个点**。精确限定批次用 `campaign` 筛选。
- 时延一律秒 `s`，大小 `GB`，CPU 比例 `ratio`。跨引擎比较看 `service_time`；
  `latency` 包含开环发压机积压。维度静态表同样用秒，不再与图差 1000 倍。
- 维度表是建板时全部批次可信行快照，不随顶部时间、查询更新；页面内明确标注此边界。

顶部可筛选 `series_key:ez@*` 或 `campaign:xxx`。通配符不要加引号；精确值可加引号。
改全局查询不能取消测量面板的 `trusted:true`；`trusted:false` 仅用于观察错误率证据。

## 运行

以下命令会连接服务，只应在明确要生成、验收或部署时执行。

```bash
python3 make_dashboard.py --import --es http://127.0.0.1:9200 \
  --kibana http://127.0.0.1:5601 --out kibana-objects.ndjson
python3 verify-dashboard.py --kibana http://127.0.0.1:5601 --dash bench-geonames --json
python3 panel-manifest.py --in kibana-objects.ndjson --out 看板面板清单.md
```

生成器支持 `--es-auth user:pass`、`--insecure`（仅 ES 自签证书）、
`--kibana-version 7.17.29`（否则询问 Kibana）。导入失败或不完整返回非零。
导入前先完整分页收集旧 bench 对象，再删除不再生成的对象，避免边遍历边删除漏页。

产物 `kibana-objects.ndjson` 不入库，可能包含真实环境台账。导航使用
`/app/dashboards#/view/bench-<track>`，变体入口与维度合并入口以总览表实际输出为准。

### 严格验收边界

验收会保留 dashboard KQL 与 panel Lucene 各自的语言，经 TSVB 请求的 `query` / `filters`
叠加，不把 KQL 字符串硬拼成 Lucene。必须满足：

- 至少匹配一个 dashboard，且整个验收至少检查一个 TSVB 面板；总览 markdown 可以跳过。
- 引用必须存在、`panelsJSON` / `visState` / filter / params 必须合法。
- 响应必须命中请求的精确 panel id，不能借用另一单个响应。
- 至少一个有限 y 值才算出数；0 合法。空数组、全 null、全 NaN/Infinity、仅时间戳/桶计数均失败。
- HTTP 错误、错误体、全未验收、缺引用和损坏状态不能静默跳过并返回成功。

离线回归（不接服务，不启动代理）：

```bash
python3 -B -m unittest tests.test_dashboard_contract tests.test_dashboard_verifier -v
```

### 部署与协议适配

```bash
ES_PORT=9237 ES_USER=admin ES_PASS='***' \
  KB_ROOT=/testing/kibana-rootfs/usr/share/kibana bash deploy-kibana.sh start
bash deploy-kibana.sh status
```

Kibana、shim 默认只监听本机回环；远程查看用 SSH 隧道。ready 检查要求同一次
`curl -f` 请求 HTTP 成功、JSON 可解析且 `status.overall.state=green`（7.x）或
`status.overall.level=available`（8.x）；如果两个字段同时存在，必须都 ready。
red/yellow/degraded/unavailable、畸形 JSON 或 HTTP 503 都不算成功。

PIT 适配只翻译 HTTP 2xx 且合法的成功响应：close 必须有非空 pits，所有项具有
`pit_id` 与 `successful:true`，才补 `succeeded:true/num_freed`；open 必须有合法 pit_id。
上游 401/403/404/500、错误体、失败项或损坏 JSON 均保留上游 status/body，不伪造成功。
`require()` shim 只导出纯函数；只有直接运行脚本才监听端口。

离线测试只能保证生成契约与模拟响应处理，不能代替真实 Kibana 版本的导入、TSVB 渲染、
EasySearch API 行为或实际部署验收。
