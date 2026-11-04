# 测试方法学与口径

## 拓扑

节点清单只有一个来源：ibs 的 `ip.txt`。主机名不以 `esrally` 开头的行是被测节点，`esrally` 那行是发压机。节点数、部署范围（`ops.deploy -n`）、卡点要循环的机器，全部从这份清单推导，所以 2~N 节点不用改代码。

分片数是性能口径，不是拓扑事实。`SHARDS` / `REPLICAS` 两个环境变量单独配（默认 3 / 0），会写进 user-tags 和 sidecar。

## 数据可信度四原则

1. **冷 JVM**：每场赛前 `ibs wipe`（停服 → 清 data 和 logs → 重启 → admin 自愈），各台车口径一致。
2. **轮内换位**：多配置按圈右旋轮换（3 配置 3 圈：r1 [A,B,C] / r2 [C,A,B] / r3 [B,C,A]），把首场偏冷和窗口波动摊到每个配置上。
3. **只认中位与同号方向**：每个配置至少跑 3 圈取中位；跨跑分对比要看各圈散点带是否重叠，单圈差异不作证据。
4. **卡点拦截脏数据**：环境噪声（CPU steal、外来进程、swap-in）进不了 VALID；sidecar 逐场记录实测身份（JDK 版本、堆、核数、索引 settings），事后可审计。

## 卡点链

| 卡点 | 时机 | 内容 |
|---|---|---|
| 静默负载卡点 | 赛前 | load1 / cpu steal / 外来进程 CPU / 磁盘可用（不过 → SKIP） |
| 集群卡点 | 冷启动后 | 可达 / green / 节点数 / 数据节点数 / 版本 / 节点名 / **端口属主**（9200 监听进程必须在安装根下，拦手工集群顶替） |
| 堆断言 | 卡点后 | 运行进程实际 -Xmx == 声明（堆由 ibs 部署统一设定为内存/2） |
| 绑核断言 | 卡点后 | EXPECT_CPU_CORES=N 时进程实际亲和核数 == N |
| 段数卡点 | write 后 | forcemerge 到每主分片 1 段 |
| 指标断言 | query 后 | throughput 非 null（防测量窗口被暖机吃掉） |
| ENV 判定 | 收尾 | 整场一份采样（5s 一次）的 steal / 外来进程 / swap-in 汇总；CONTAMINATED 场不可进结论 |

## 写入口径

- **口径A** = rally 的 `Median Throughput`（区间中位速率），正式引用的口径
- **口径B** = 语料条数 ÷ 任务 duration（全程平均）。两者的差异来自写入的慢尾，不是窗口长短；工具两个都会输出
- **warmup**：按「任务时长的 20%、窗口不小于 50%」标定。写死一个常量会在短任务上把测量窗口吃光（`throughput=null` 而返回码还是 0）

## 查询口径

- query-only 全操作 ≥100 次迭代（p99 可报）；看 `service time` 的 p50/p90/p99，不引用单轮
- codec 类对比注意：烘焙进赛道的 settings 必须与 sidecar 抓到的 `_settings` 实测值核对
- 跨赛道只比趋势不比绝对值（bulk 尺寸/文档大小/建图成本不同）

### 时延三兄弟：别把发压机排队当成引擎慢

rally 的 `latency` 有两种口径，取决于该 operation 有没有设 `target-throughput`：

| | 闭环（无 target-throughput） | 开环（有 target-throughput） |
|---|---|---|
| `latency` | ≡ `service_time`（单次请求耗时） | **端到端，含发压机积压** |
| `service_time` | 请求的服务端耗时 | 同左 |
| `processing_time` | ≈ `service_time`（差 0.1%） | 同左 |

开环 task 饱和时，积压会把引擎耗时放大一个数量级 —— nested 赛道实测 90 分位 `latency`
1.477s，而 `service_time` 只有 36.7ms，**差的 40 倍全是发压机积压**。

因此：

- **跨引擎比较看 `service_time`**（引擎侧执行时间）
- `latency` 只作 SLO 指标看，且必须知道该 task 是不是开环的
- `queue_time`（= latency − service_time，导出时派生）单独看，用来分清
  "引擎慢" 还是 "发压机排队"
- 逐 task 的开环/闭环以赛道 spec 的 `target-throughput` 为**地面真值**
  （sidecar `run_params.latency_modes`）；没有真值时才按数据反推，
  反推结果单独计数上报（`manifest.latency_mode_source`），不当作实测

## 口径契约（导出自检）

`bench-export.py` 是纯函数、产出可 diff 的载荷快照 —— 口径缺陷必须在**写出前**
被拦住，而不是等它在看板上变成一个"看起来合理但不可比"的数字。

**致命（导出非零退出，载荷不写出）**

| 检查 | 抓的是什么 |
|---|---|
| `stat` 在闭集内 | rally 的 `99_99` 分位原样漏进 stat 列，与 `p999` 并存 |
| `unit` 在闭集内 | 量纲越界（跨 family 求和/对比出错） |
| `_id` 不重复 | 两行共用一个 `_id` → 后写的静默覆盖先写的 |
| 同一 (场次, task) 的 `trusted` 一致 | 失败 task 的行与成功 task 的行混在一起 |
| `error_rate>0` 的 task 整块 `trusted=false` | 失败 task 的"漂亮时延"混进延迟曲线 |
| `closed_loop` 不出现积压 | 口径声明与实测自相矛盾 |
| 场次 `trusted` 与 `degraded_tasks` 一致 | 场次级标记与行级标记打架 |

**非致命（写进 manifest 供核对）**：字段覆盖率（`env_id` / `dims` /
`segment_form` / `codec` / sidecar）、`stat` 归一后的兜底别名、提示列表。

### 词表

- `stat` ∈ `value | mean | p50 | p90 | p99 | p999 | p9999 | p100 | min | max`
  （rally 的 `median` 归一为 `p50`；`99_9`→`p999`；`99_99`→`p9999`）
- `unit` ∈ `s | ms | ops/s | docs/s | pages/s | MB/s | GB | MB | bytes | count | ratio | percent | text | ""`
- `trusted` ∈ `true | false`（行级布尔；也是场次级布尔）
  （导出内部按 task 判 `ok / degraded / unknown`，落库时投影成布尔：degraded → `false`。
  **这是看板的默认闸门**，`true` 只说明"跑完了"，不说明"结果可用"）
- `latency_mode` ∈ `open_loop | closed_loop | unknown`

### 可信度三层

RACES.md 判定（跑没跑完）→ 场次级 `trusted`（这一场能不能用，取各 task 的最差）→ 行级
`trusted`（这一行可不可信，按所属 task 判）。看板默认查询框 =
`trusted: true AND NOT codec:(zstd or best_compression)`。

**注意**：赛级指标（`store_size` / `segment_count` / GC / merge…）量的是**写阶段**
状态，一个查询 task 失败不会把它们降级 —— 只有写阶段自己失败才降级。


## 结果入库（三索引）

把 rally race 结果灌进 ES：三索引各自独立 strict 模板 + 导出期字段白名单。

```bash
python3 build-race-store.py --campaigns campaigns-local --out race-store   # 软链农场（幂等）
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns-local
python3 bench-apply.py --dir exports/<tag> --es <url> [--auth user:pass] [--insecure] [--drop]
```

| 索引 | 一行代表 | 用途 |
|---|---|---|
| `benchmark-result-env` | 一条环境台账 | 环境/身份/配置维度 |
| `benchmark-result-race` | 一场 race | 主维度表（run 维度全在这张表） |
| `benchmark-result-metric` | 一个指标 | 长表，一指标一行，单位归一 |

口径要点：

- **error_rate 按 rally 语义 0~1 原样入库**（`1.0` 表示该 task 的请求全失败），不按百分比折算
- 实验变量 `dims` 从 user-tags 提升成 typed 列，可以 range 和排序
- **指标表带 `track` 列**（归一化赛道名）。按赛道拆看板的前提就是它 —— 指标行没有 track就没法按赛道过滤
- **指标表只带一个身份字段 `series_key`**，形如 `ez@2.5.0@3015@graalvm25`（引擎@版本@build@JDK）。运行维度（engine、version、jdk、dims、segment_form、env_id、round、phase、车代号…）在指标行上与 `benchmark-result-race` 完全重复，留着只是白付体积、平白多一份漂移面，要查就去主维度表查。看板按 `series_key` 分线，TSVB 的 split-by 只能吃一个字段。按引擎过滤写 `series_key:ez@*`，**不要加引号** —— 带引号是精确短语，实测匹配 0 条
- **身份只认实测**：`jdk_vendor` / `jdk_version` 取 sidecar 的 `java -version` 原文和节点上的 JVM 版本，查不到就留空。车代号推不出 patch 号（jdk25 可能是 25.0.4，也可能是 25.0.5），不知道就不猜。厂商必须单列 —— GraalVM 25.0.4 和 TencentKonaJDK 25.0.4 的版本号一模一样，只有厂商能区分
- **monitors 补的 system 行按场次回填 `@timestamp` / `run_start` / `trusted` / `series_key` / `race_id`**。这行是按场次采的摘要，本身没有时间戳也没有身份，不回填的话 CPU steal 这类可信度指标画不出折线（x 轴没值），也会被 `trusted: true` 整个过滤掉
- 引擎身份 `engine`（easysearch / elasticsearch）从**包名**推导，不信 sidecar 里的 `name` 字段
- **幂等**：文档 `_id` 是固定的（场次行 `race_id`；指标行 `race_id|phase|task|family|stat`），重复执行是覆盖，不会翻倍
- **stat 归一后 `_id` 会变**（rally 的 `median` → `p50`、`99_99` → `p9999`），旧索引里的同一条会变成两条。改过词表必须 `bench-apply.py --drop` 重建
- **改了 index template 也必须 `--drop` 重建索引**，旧 mapping 不会自动更新
- **三张索引的模板都是 `dynamic: strict`**：能进索引的字段就是导出器的白名单（metric / race / env 各一份），白名单外的字段（运行维度回流）在 ES 侧直接拒收。字段精简靠机制保证，不靠口头约定

## 存储口径

比落盘体积用 **forcemerge 之后**三台节点的 store 之和（sidecar 的原始探针：拆分协议落 `logs/fp/<场次名>-q/`，单挑战协议落 `-wq/`）。不要用 race.json 里的快照，那还是合并前的多段状态。

## 已知边界

- 每个配置跑 3 圈，单场会有 ±5%~10% 的波动；尾部指标（p99、p100）样本少，只采信方向一致的项
- 白天共享宿主机有噪声，正式数据跑夜里（静默卡点会自动跳过不安静的时段）
- 跨引擎轮换时，每场有 5~8 分钟的 ibs 重装开销 —— 这正是要做轮内换位的原因
