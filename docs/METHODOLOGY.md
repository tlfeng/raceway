# 测试方法学与口径

## 拓扑

节点清单只有一个来源：ibs 的 `ip.txt`。主机名不以 `esrally` 开头的行是被测节点，`esrally` 那行是发压机。节点数、部署范围（`ops.deploy -n`）、卡点要循环的机器，全部从这份清单推导，所以 2~N 节点不用改代码。

分片数是性能口径，不是拓扑事实。`SHARDS` / `REPLICAS` 单独配（默认 3 / 0），写进 user-tags 和 sidecar；集群检查断言的是实际拓扑，不写死三节点。

## 数据可信度四原则

1. **冷 JVM**：每场赛前 `ibs wipe`（停服 → 确认停净 → 清 data 和 logs → 重启 → admin 自愈），各台车口径一致。停机、SSH 或端口检查失败不能当成停净，更不能删除在线 data。
2. **轮内换位**：多配置按圈右旋（3 配置 3 圈：r1 [A,B,C] / r2 [C,A,B] / r3 [B,C,A]），把首场偏冷和窗口波动摊到每个配置；显式排程按文件顺序。
3. **只认中位与同号方向**：正式比较每个配置至少 3 圈，按相同实验条件与 task 取跨圈中位，保留逐圈散点与范围。单场 Rally p50 只是跨圈聚合的输入，不等于已经取了跨圈中位。
4. **卡点与证据共同放行**：精确尝试判定、完整协议、有效指标和全节点监控缺一不可。sidecar 逐场记录实测 JDK、堆、核数、索引 settings/mappings；标签不能代替实测身份。

## 实验与测量身份

- **计划**：所有编排模式首次生成 `PLAN.tsv` 与 `PLAN.sha256`。TSV 的 tab 分隔表头为 `fid/config/track/round/recall/est`，锁定基础 fid、展开配置、赛道、轮次、召回开关、估时和顺序；同 campaign 续跑须匹配计划与摘要。
- **窗口**：`DEADLINE` 首次保存绝对截止 epoch（`0` 为无时间窗）；`DS_HOURS` 启用时留 20 分钟余量，续跑与 `FRESH=1` 不重新计时。
- **状态与尝试**：`STATE.txt` 以基础 fid 记 `OK / FAIL / SKIP`；`RACES.md` 保留逐实际尝试判定。新尝试号从已有最大值后递增，不覆盖报告、JSON、日志或监控。`FRESH=1` 新建尝试，不清空历史；重试不是新圈。
- **执行**：runner 用 `flock RESULTS_ROOT/.runner.lock` 原子互斥，从 campaign 的 `runtime/` 只读脚本快照运行。赛道、环境配置和语料仍须在 campaign 期间保持不变。
- **停机**：`RUNNER.pid` 是纯 PID，`RUNNER.meta` 为单行 tab 分隔的 `pid/start_ticks/boot_id/script绝对路径`。只停止身份验证通过的目标 runner 及其可验证后代；不按命令行子串全局扫杀。

一次完整测量是同 campaign、真实 fid/attempt 下的 `full`（phase=`wq`），或同身份的一对写/查段（phase=`w` + `q`）。不能跨 campaign、尝试或 build 拼接半场。明确声明 `measurement_protocol=write-only` 的实验按写协议检查，不能从缺查询段反推它只需要写段。

## 卡点链

| 卡点 | 时机 | 内容 |
|---|---|---|
| 停机与清理 | 每场前 | 按统一 `IBS_BASE` 定位目标引擎，确认停净与 HTTP 端口空闲后才清数据 |
| 静默负载卡点 | 赛前 | load1 / CPU steal / 被测引擎与外来进程 CPU / 磁盘可用；不过则 SKIP |
| 集群卡点 | 冷启动后 | 可达 / green / 节点数 / 数据节点数 / 版本 / 节点名 / 端口属主 |
| 堆断言 | 卡点后 | 每节点运行进程实际 -Xmx == 声明；堆由 ibs 部署统一设定 |
| 绑核断言 | 卡点后 | 设置 `EXPECT_CPU_CORES=N` 时，每节点实际亲和核数 == N |
| 合并等待 | 赛道内部 | full challenge 按定义同步 force-merge 并等待完成；runner 不另设独立段数卡点 |
| 指标断言 | race 后 | 分别检查协议要求的写任务与查询任务有有限有效吞吐且零错误；维护任务不能抵充查询 |
| ENV 门禁 | 收尾 | 每个实际 phase、每个预期节点都必须 `samples>0` 且 `ENV-VERDICT: OK` |

full 挑战默认每 5 秒采样，tag 为 `<实际fid>-wq`；拆分测量逐 phase 采集。`MON_DIR` 统一透传为 `RACEWAY_MON_DIR`。缺节点、缺文件、零样本、采集失败、`WARN`、`CONTAMINATED` 或 `NO-DATA` 均判 `ENV-FAIL`，不能 VALID；细节保留在 `monitors/*-summary.txt`。不能拿另一个尝试的摘要或 VALID 补证。

## 写入口径

- **口径A** = Rally 的 `Median Throughput`（暖机后测量样本的区间中位速率），正式引用的口径。
- **口径B** = 该写任务实际处理文档数 ÷ 任务 duration（含暖机的全程平均）。两个口径分别保留，不相互替代；参数缩小语料时不能仍用全量文档数。
- **warmup**：按任务实测时长标定，约 20%，测量窗口至少留 50%；不能用固定暖机时间耗尽短任务的测量窗口。
- **upsert / update**：默认 bulk_size=500、8 客户端，所有阶段遍历相同自然 `geonameid`。cold/hot/mixed 的 `raceway_revision` 分别为 0/1/2，update 显式 `detect_noop:false`；纯 update 对照用相同 ID 与 revision，不带 `doc_as_upsert`。这是全字段合并重写，不是 no-op 或少量热点 ID 更新。各 task 独立比较，不能把多个阶段加成一个写速率。

## 查询口径

- 查询分位需要足够测量样本；常规 query-only 至少 100 次迭代，看 `service_time` 的 p50/p90/p99，不引用单圈作结论。
- KNN 将 `size` 写进最终搜索 body，与 task 的 k 一致；不能只凭 k=100 就默认返回 100 条。
- 索引类型优先取实际 mappings，声明须按本场参数渲染；Jinja 字面量不是 `knn_index_type` 的有效证据。
- OpenSearch 的 `ef_search` 是 HNSW 搜索宽度，**不等同于 ES 的每分片 `num_candidates`**。按引擎、索引方法和 task 记录预算，IVF/磁盘/重排参数另看各变体定义；相同数字或任务名不证明搜索成本一致。
- 量化与压缩正交：如 `hv1024x-int8-codec-def` 保留 `int8_hnsw`；codec/source_reuse 的声明须与 sidecar `_settings` 实测核对。
- 跨赛道只比趋势，不比绝对值；bulk 尺寸、文档大小、建图成本和搜索预算可能不同。

### 时延三兄弟：别把发压机排队当成引擎慢

Rally 的 `latency` 口径取决于 operation 是否设 `target-throughput`：

| | 闭环（无 target-throughput） | 开环（有 target-throughput） |
|---|---|---|
| `latency` | ≡ `service_time`，单次请求耗时 | 端到端，含发压机积压 |
| `service_time` | 请求从发出到响应的服务耗时 | 同左，不含计划发出前的积压 |
| `processing_time` | 包含客户端处理开销 | 同左 |

跨引擎比较优先看 `service_time`，但它仍是客户端测得的请求耗时，不等于引擎内部执行计时。`latency` 用作 SLO 时须说明开环目标负载；`queue_time=latency−service_time` 是派生的积压差值，分位差不是独立测得的排队时间分布。

开环/闭环以赛道 spec 的 `target-throughput` 为真值（sidecar `run_params.latency_modes`）；没有真值才按数据推断，并在 `manifest.latency_mode_source` 单独记录，不当作实测。

## 口径契约（导出自检）

`bench-export.py` 从原始产物生成确定性载荷。精确未记账、非 VALID、人工污染、协议不完整或监控不合格的测量不导出，原因写入 manifest；段间身份或实验条件不一致则直接失败。

**致命（导出非零退出，不写载荷）**：

| 检查 | 约束 |
|---|---|
| 数值有效 | 不接受 NaN / Infinity；task 的 error_rate 必须存在且为 [0,1] 有限数 |
| `stat` / `unit` 闭集 | 分位与单位先归一，不能漏入未知词表 |
| `_id` 唯一 | 不允许两行静默覆盖 |
| 身份与变体 | `comparison_key=series_key+'\|'+track_variant`，变体非空，索引类型不得是未渲染模板 |
| 段间一致 | 引擎、build、JDK、量化、压缩、预算、维度、堆、核数、分片等条件必须一致 |
| task 可信度 | 同一测量、同一 task 的所有行 trusted 一致；`error_rate>0` 则整 task 为 false |
| 场次可信度 | 场次 trusted 取各 task/段的最差，与 degraded_tasks 一致 |
| 时延语义 | 闭环声明与积压检查不得自相矛盾 |

字段覆盖率、归一别名及其他非致命提示写进 manifest，不能把提示当作实测证据。

### 词表

- `stat` ∈ `value | mean | p50 | p90 | p99 | p999 | p9999 | p100 | min | max`；`median`→`p50`、`99_9`→`p999`、`99_99`→`p9999`。
- `unit` ∈ `s | ms | ops/s | docs/s | pages/s | MB/s | GB | MB | bytes | count | ratio | percent | text | ""`；导出时延统一为 s。
- `trusted` ∈ `true | false`，裸布尔，适用于指标行与场次行。
- `latency_mode` ∈ `open_loop | closed_loop | unknown`。

### 可信度与展示

精确实际 fid 的 `RACES.md` 判定 → 完整协议与全 phase 全节点 ENV 检查 → task 质量 → 场次 trusted。`trusted=true` 表示通过相应数据质量门槛，不保证实验设计、样本量或不同配置之间天然可比。

`error_rate>0` 的 task 全部指标行（含错误率证据）均 `trusted=false`，场次也为 false；若精确判定为 VALID、协议与监控完整，错误证据仍可落库。赛级指标（store/segment/GC/merge 等）按写阶段质量判，不因单个查询 task 失败一并降级。

看板不使用全局 `trusted:true`：吞吐、时延、GC、资源等测量面板各自过滤可信行；**错误率面板及其预检均不加 trusted 过滤**，显示桶内最坏错误率。默认基座视图排除非默认压缩和 source_reuse，每个实际 variant 单独显示。固定分位后按 `comparison_key` 分线，正式数值跨圈取中位，不按身份混平均。时延图表统一标 s，不能把秒值直接标成 ms。

## 结果入库（三索引）

```bash
python3 build-race-store.py --campaigns campaigns-local --out race-store
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns-local
python3 bench-apply.py --dir exports/<tag> --es <url> [--auth user:pass] [--insecure] [--drop]
```

| 索引 | 一行代表 | 用途 |
|---|---|---|
| `benchmark-result-env` | 一条环境台账 | 环境、身份与配置维度 |
| `benchmark-result-race` | 一次完整测量 | run 主维度；可由同次测量的写/查段合并 |
| `benchmark-result-metric` | 一个指标 | 长表，一指标一行，单位归一 |

口径要点：

- **error_rate 原样按 0~1 入库**，`1.0` 表示该 task 请求全失败，不再次除以 100。
- `track` 是归一基础赛道；race 与 metric 同时保留真实 `track_variant`、已解析 `knn_index_type` 和 `comparison_key`，不能把 int8 / BBQ / codec 归到一条线。
- **`series_key` 只表达引擎配置身份**，如 `ez@2.5.0@3015@graalvm25`（引擎@版本@build@JDK厂商主版本）；完整 JDK 版本另存 `jdk_version`。**实验分线用 `comparison_key=series_key+'|'+track_variant`**，不把处理变量塞入引擎身份。按引擎筛选可写 `series_key:ez@*`，通配符不要加引号。
- 引擎明确区分 Easysearch、Elasticsearch、OpenSearch；不从通用 `oss` flavor 猜产品。ES / OS 无独立 build，不虚构 Easysearch 构建号。JDK 厂商与完整版本取源头证据，不凭车标签猜 patch 号。
- `dims` 等实验变量为 typed 列；metric 物化分组必需的 track/variant/索引类型/比较键，不依赖 ES 自动 join race。
- monitors 的 system 行按精确测量回填 `@timestamp`、`run_start`、`trusted`、`race_id` 和身份/变体键；无法匹配的摘要不借别场身份。
- **稳定测量键**：race 的 `_id` / `race_id` 为 `campaign|真实fid|a<attempt>`，不取当前可见主段的 Rally UUID。普通指标键为 `race_id|phase|task|family|stat`；段 UUID 保留溯源，不控制测量身份。相同输入重复入库覆盖同键，不翻倍。
- 三张模板均为 `dynamic: strict`，导出器各有字段白名单；模板外字段或错误类型不能静默入库。

### 入库预检与 --drop

`bench-apply.py` 在任何网络请求、特别是任何 DELETE 之前，完整检查输入目录、所选索引模板、全部 JSONL、字段类型、有限数、唯一 ID、comparison_key 和所选 race/metric 引用；已提供 manifest 时须断言通过。读取后使用同一内存快照，不在删除后重读变化中的文件。

缺文件、未知或重复 `--only`、非法模板/载荷、所选总载荷为 0 均非零退出，不发送请求。`--drop` 只在预检通过后展示并删除所选索引，再建模板和入库；删除未确认成功即停止。bulk 按每条响应计失败，缺响应明细也不算成功；有失败则非零退出。

## 有效结果对比

`car-compare.py`、`codec-compare.py`、`car-env-table.py` 共用 `result_selection.py` 与 `identity.py`。指标只读机读 JSON，不从 Markdown 报告取数；判定表只用于精确尝试筛选。要求完整协议、sidecar 身份一致、任务零错误、监控全 OK。同一 campaign 的同一基础 fid 只取最新有效尝试，失败或无判定重试不计圈。

```bash
python3 car-compare.py --a-campaign campaigns-local/<A> --b-campaign campaigns-local/<B> \
  --a 同车标签 --b 同车标签 --assert campaign-thresholds.example.conf
```

A/B 目录选项均可重复，同车可跨池比较；每侧内部不得混 build、JDK、量化、存储设置或运行条件。两侧赛道与任务集合须一致，空有效集合非零退出。阈值 CSV 为 `track,task,metric,>=|<=,value`，track 是实际变体；每规则要求 B 侧至少 3 个有限有效测量，检查其中位数。规则错误、空规则集、未知指标、样本不足或不达标均非零退出，不能跳过坏规则而放行 CI。

## 存储口径

落盘体积用赛末、合并之后全部被测节点的 store 之和，原始探针为 `logs/fp/<实际fid>-wq/nodes-stats.txt`，拆分协议取 `-q/`。探针缺失显示缺失，不用 race.json 的其他时点快照冒充。核对探针索引范围，节点级总量可能包含系统索引。

upsert 的 full 含两次重建，赛末 store 属于最后的 native-update 周期，不能代表 mixed 阶段体积。

## 已知边界

- 单场可能有明显波动，尾部分位样本有限；正式结论保留散点、样本数和同号方向，不用一条中位线代替分布。
- 白天共享宿主机有噪声，优先选安静窗口；任何时段都不能省略监控。
- 跨引擎轮换包含重装开销，时长估算不是性能指标；重试耗时也不能充作新增有效圈。
