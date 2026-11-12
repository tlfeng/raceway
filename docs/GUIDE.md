# raceway 使用手册（GUIDE）

> 分两层写：
> - **上篇「怎么跑」** —— 想测什么、敲什么命令、怎么看进度、跑完怎么收结果。
> - **下篇「跑起来之后」** —— 框架怎么分层、脚本各管什么、一场压测内部发生什么、哪些规则不能破。
>
> 方法学与统计口径见 [METHODOLOGY](METHODOLOGY.md)，hv1024 赛道的数据来源见 [hv1024-track](hv1024-track.md)，
> 赛道定义目录的规矩与待办见 [tracks/README.md](../tracks/README.md)。
> 门面前言与最短上手路径见仓库根 [README](../README.md)。
> 所有环境配置都放在 `vm-bench.env`（模板 `vm-bench.env.example`，真身不入库）。

## 名词速查

| 词 | 意思 |
|---|---|
| 车（配置） | 一套被测环境：引擎版本 + JDK + 标签。配置文件里一行就是一台车 |
| 赛道（track） | 一个压测用例：建哪个索引、写多少数据、查什么 |
| 圈（round） | 同一条赛道重复跑的第几遍。多跑几圈取中位数，摊掉偶然波动 |
| 场次 | 「车 × 赛道 × 圈」的基础组合，fid 形如 `kona25-geo-r2`；重试用 `-tryN` 区分实际尝试 |
| campaign | 一场完整跑分，含若干场次，计划与产物在 `campaigns/<id>/` 目录 |
| 卡点 | 开跑前的自动检查。不通过就不开跑，或直接跳过这一场 |
| sidecar | 每场附带的一份元数据 JSON：实测的 JDK 版本、堆大小、索引 settings、节点负载 |
| ENV 判定 | 逐 phase、逐节点检查采样；全部有数据且为 OK 才允许该次尝试有效（CPU steal / 外来进程 / swap） |

---

# 上篇 · 怎么跑

## 1. 入口决策表：我想测什么 → 我敲什么

命令都在压测机的 `/opt/raceway` 下跑。

| 我要测 | 命令 | 单场时长 | 整场大概 |
|---|---|---|---|
| 多配置对比（引擎 × JDK） | `./run-bench.sh geo 3 my3.conf` | ~26 分钟 / 场 | 3 配置 3 圈 ≈ 4 小时，另计换装与重试 |
| 存储压缩对比 | `./run-bench.sh codec 3 my3.conf` | ~25~30 分钟 | Easysearch 每配置 18 场 ≈ 7.5~9 小时；ES 每配置 6 场 |
| 1024 维向量（8 核环境） | `EXPECT_CPU_CORES=8 ./run-bench.sh hv1024 3` | ~17~19 分钟 | 每配置 3 场 ≈ 1 小时 |
| 96 维 hnsw / nested | `./run-bench.sh hnsw 3` / `./run-bench.sh nested 3` | ~26 分钟 | — |
| 冒烟、补跑一场 | `./run-bench.sh geo 1` | ~26 分钟 | — |
| 逐场定制（哪台车 × 哪条赛道 × 第几圈） | `SCHEDULE_FILE=… CARS_FILE=… DS_HOURS=12 ./run-bench.sh ds` | 按每行估 | 按时间上限裁剪 |

> 时长仅作排程估算；换引擎、语料、并发和重试会改变总用时，实际用时查 `RACES.md`。

> 正式数据建议在安静窗口跑。共享宿主机的噪声由赛前卡点与赛后监控共同拦截，跳过或失败的尝试不计有效圈。

## 2. 通用前提（每次都要过的 4 件事）

```bash
# ① 登录压测机（Host 别名配在 ~/.ssh/config，节点清单在 ibs 的 ip.txt）
ssh <压测机>
cd /opt/raceway

# ② 改过本地 raceway 仓库后，先同步到压测机（在本地仓库目录执行；只覆盖不删除）
scp -r *.sh *.py *.conf vm-bench.env tracks/ kibana/ <压测机>:/opt/raceway/

# ③ 长任务一律 setsid nohup 后台跑，人只轮询 PROGRESS.txt（防 SSH 断连）
# ④ 跑完把 campaign 下载回本地归档（见 §6）
```

**同步必须带 `tracks/`、`kibana/` 和全部 `*.py`**：赛道定义和 `result_selection.py` / `identity.py` 都是运行依赖。同步只覆盖不删除，清理远端多余文件须另行核对。

runner 取得 `RESULTS_ROOT/.runner.lock` 的 `flock` 后，从本次 campaign 的 `runtime/` 只读脚本快照运行；同一结果根目录下第二个 runner 会被拒绝。更新源码不会改写正在执行的脚本快照；赛道、环境配置和语料仍须在 campaign 期间保持不变。

被测引擎的包路径、堆大小、卡点阈值都在 `vm-bench.env`。JDK 不走包配置，`JDK_DIR` 直接写被测节点上的目录。

### 2.1 阈值键与路径键（`vm-bench.env`）

| 键 | 作用 |
|---|---|
| `VERDICT_STEAL_MAX` | 任一节点 steal_max 超过 → ENV 摘要 CONTAMINATED、场次 ENV-FAIL；赛前 steal_max ≥ 此值 → 卡点 FAIL |
| `VERDICT_FOREIGN_MAX` | 外来进程 CPU 峰值超过 → ENV 摘要 WARN、场次 ENV-FAIL；赛前 ≥ 此值 → 卡点 FAIL |
| `QUIET_STEAL` / `QUIET_FOREIGN_CPU` | 赛前静默卡点的同口径项，缺省**继承** `VERDICT_*`；只有赛前要单独收紧才显式写 |
| `QUIET_LOAD1` / `QUIET_BENCH_CPU` / `QUIET_FREE_GB` | 赛前卡点专属：1 分钟负载 / 被测引擎 CPU %（赛前应为 0）/ 磁盘可用 GB |
| `HOST_QUIET_PORTS` / `HOST_QUIET_AUTOSTART` | 逃生阀：置空可关掉宿主旁路检查（见 `precheck.sh`） |
| `CLUSTER_WAIT_SEC` | 集群卡点轮询超时 |
| `RALLY_PYTHON` | 赛道任务清单渲染用的 Python，默认 `python3`；须能 import esrally。Rally 安装在 venv 时指向该 venv 的 Python，不能只靠发压 wrapper 激活环境 |
| `TIMEOUT_<赛道>_W` / `_Q` | 写段 / 查段超时（秒） |
| `HV_CORPUS` / `HV_QUERIES_BZ2` | hv1024 系语料与官方查询集路径；起跑前用来软链语料、生成 `queries.json` |
| `TRACK_HNSW_BASE` / `TRACK_HNSW_S3` | 机器本地的 96 维 HNSW 赛道（定义不在库内，runner 现生成） |
| `MON_DIR` | 远端监控脚本与 CSV 目录；runner 启停、收集时统一透传为 `RACEWAY_MON_DIR` |
| `ENGINE_MARK` | 监控归因标记：cmdline 含此串的进程视为被测引擎。**换部署根必须同步改**（见 §15 第 10 条） |

## 3. 三种编排方式

```bash
./run-bench.sh <track> [rounds=3] [配置文件]     # track 清单见 list；常用 geo|codec|hnsw|hv1024|hv1024x-*|nested|upsert|both|all|ds-*
./run-bench.sh list                              # 列可用赛道、配置模板、近期跑分
./run-bench.sh --dry-run geo 3 my.conf           # 只打印排程，不执行（DRY=1 等价）
```

| 方式 | 命令 | 什么时候用 |
|---|---|---|
| 单配置 | `./run-bench.sh geo 3` | 冒烟、补跑一场；配置走命令行的环境变量 |
| 多配置文件 | `./run-bench.sh geo 3 my3.conf` | 多车对比；配置文件每行一个配置 |
| 排程文件 | `SCHEDULE_FILE=… CARS_FILE=… DS_HOURS=12 ./run-bench.sh ds` | 逐场定制，带时间窗保护 |

### 3.1 配置文件（多配置方式）

每行一个配置，`#` 开头是注释。能用的键只有 6 个：

| 键 | 说明 |
|---|---|
| `ENGINE_NAME` | `easysearch`（默认）、`elasticsearch` 或 `opensearch` |
| `ENGINE_VERSION` | 默认 2.5.0；ES 如 8.19.23，OS 如 3.3.2 |
| `ENGINE_BUILD` | 只有 easysearch 有（默认 3015）；给 ES / OS 设置会报错 |
| `JDK_DIR` | 被测节点上已解压好的 JDK 目录。只有 easysearch 能用；首次换装会自动把自带 JDK 归档起来 |
| `CAR_LABEL` | 这台车的标签，进场次名和元数据。多配置时必须互不相同；不填就取 JDK 目录名或引擎身份 |
| `JDK_WANT` | 可选，形如 `25\|OpenJDK`，记录期望 JDK 身份；实际身份由逐节点实测确定 |

配置文件按空白分隔 `KEY=value`，值中不要加 shell 引号或空格；命令行环境变量仍按 shell 规则引用。

```
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=ez3015
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/kona25 CAR_LABEL=kona25
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
```

不带配置文件时，命令行的环境变量等价于一行配置：

```bash
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ./run-bench.sh geo 3
```

**轮内换位**：多配置对比时，每圈把配置顺序转一位。3 配置 3 圈就是 r1 [A,B,C]、r2 [C,A,B]、r3 [B,C,A]。这样「闲置后第一场偏冷」这类偏差会摊到每个配置上，而不是全压在第一个配置身上。

### 3.2 排程文件（逐场定制）

用来跑维度扫描这类固定排程：每行写死「哪台车、哪条赛道、第几圈、要不要召回探针、预计几分钟」。

**车定义文件**（`CARS_FILE`）—— 每行一台车，后面跟配置行，语法和 §3.1 完全一样：

```
jdk25 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=jdk25 JDK_WANT=25|OpenJDK
jdk26 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=jdk26 JDK_DIR=/opt/jdks/jdk-26 JDK_WANT=26|OpenJDK
es819 ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
```

**排程文件**（`SCHEDULE_FILE`）—— 每行一场次：

```
jdk25 ds-g1920 1 recall 52
jdk25 ds-j1024 1 recall 35
jdk26 ds-j1024 2 norecall 42
```

四条规则：

1. 赛道写规范 id（`geo`、`hnsw`、`hv1024`、`hv1024x-*`、`nested`、`geonames-upsert`、`ds-*`、`geo-codec-*`），车必须已定义；基础 fid 不得重复。
2. 最后一个数字是该场预计分钟数，必须是非负整数。时间窗保护靠它判断「这场还塞不塞得下」。
3. `DS_HOURS=12` 首次起跑生成绝对截止 epoch，留 20 分钟余量。放不下的标 `DEADLINE-SKIP`，继续检查后面更短的场；同一 campaign 续跑沿用首次截止时间，不重新计时。
4. 排程方式不做轮内换位，文件行序就是执行顺序；展开后的实验计划与所有模式共用同一份持久状态（见 §4.5）。

> `recall` 那一列表示这一场跑完再打一次召回探针（索引还在线的时候打）。召回塌了的性能数字不作数 —— 不同 JDK 的向量代码路径并不等价，没有逐台的召回证据，吞吐和延迟不可比。

完整样例见本地留存的 `schedules/` 目录（`dimsweep-*.conf`）。该目录是内部排程计划，不随本库公开 —— 用 `--cars-file` 或 `RACEWAY_CARS_FILE` 指向它的留存副本。

## 4. 场景与完整命令

### 4.1 三个引擎对比

```bash
cat > ez-es.conf << 'EOF'
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=ez3015
ENGINE_NAME=easysearch ENGINE_VERSION=2.4.0 ENGINE_BUILD=2975 CAR_LABEL=ez240
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
EOF

# 先看排程，不执行
./run-bench.sh --dry-run both 3 ez-es.conf

# 3 配置 × 2 赛道 × 3 圈 = 18 场
setsid nohup ./run-bench.sh both 3 ez-es.conf > run-3eng-$(date +%m%d).log 2>&1 &
```

### 4.2 存储压缩对比

```bash
./run-bench.sh codec 3                     # Easysearch：压缩 × source_reuse 六个变体
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ./run-bench.sh codec 3   # 只跑 def / bc 且关闭 source_reuse，其余留痕跳过
```

压缩变体不生成目录：压缩档与 source_reuse 走 `--track-params`（`index_codec:` / `source_reuse:true`），由定版 `tracks/geonames/index.json` 的条件块写进 settings。机制见 §13.1。

### 4.3 1024 维向量（8 核环境）

```bash
# ① 绑核。写进 systemd drop-in，重启和重装之后都还在
./cpu-affinity.sh apply 8        # status 查看 / clear 还原

# ② 跑。每场都会断言实际核数
EXPECT_CPU_CORES=8 ./run-bench.sh hv1024 3    # ES 对照另跑一个 campaign
```

绑核之后如果要跑全核（32 vCPU）的矩阵，必须先 `./cpu-affinity.sh clear`。否则引擎会被限制在 8 核；正式跑分应设置对应的 `EXPECT_CPU_CORES` 逐场断言。

### 4.4 96 维 hnsw / nested

```bash
./run-bench.sh hnsw 3     # 2M×96 图构建吞吐
./run-bench.sh nested 3   # nested，分片数由 SHARDS 指定（默认 3）
```

### 4.5 冒烟与补跑

```bash
./run-bench.sh geo 1                                  # 跑一场（真跑，约 26 分钟）
./run-bench.sh geo 1 --dry-run                        # 只看排程，秒回
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ./run-bench.sh hnsw 1   # 补某配置某赛道一圈
```

补跑与原结果对比时，`CAR_LABEL` 必须对应，实测 build、JDK 和实验条件也要核对，不能只凭标签合池。

**同一 campaign 续跑**须显式给 `CAMP_ID`，并保持同一份完整计划；不设它会新建 campaign：

```bash
CAMP_ID=<原id> ./run-bench.sh geo 3 my3.conf           # 成功场次跳过，继续未完成场次
CAMP_ID=<原id> FRESH=1 ./run-bench.sh geo 3 my3.conf   # 为计划场次分配新尝试，不清空旧结果
```

- 所有模式保留 `RACES.md` 的逐尝试判定；`STATE.txt` 按**基础 fid**记 `OK / FAIL / SKIP`，不按排程物理行号绑定。基础 fid 的任一成功重试都能证明已完成，不会因 `-try2` 成功而再跑 `try1`。
- 首次生成 `PLAN.tsv`（tab 分隔，表头 `fid/config/track/round/recall/est`）与 `PLAN.sha256`，锁定展开配置、赛道、轮次、召回开关、估时及执行顺序。续跑计划或摘要不符就退出；要改实验应使用新 `CAMP_ID`。
- `DEADLINE` 保存首次截止 epoch（`0` 表示无时间窗），重启或 `FRESH=1` 都不把窗底推后。
- 新尝试号从该基础 fid 已有最大编号之后递增，报告、JSON、日志、监控都不覆盖。`FRESH=1` 是新尝试，不是原地抹历史；重试和强制重跑不额外计为新圈。

### 4.6 upsert / update 对照

```bash
./run-bench.sh upsert 3   # 等价赛道 id：geonames-upsert
```

默认 `bulk_size=500`、8 客户端，全部阶段遍历相同的自然 `geonameid`，不使用 Rally 的冲突热点模型。cold 写 `raceway_revision=0`，hot 改为 1，mixed 改为 2；update 显式 `detect_noop:false`，测真实重写而非 no-op。纯 update 对照也使用同一自然 ID 和 revision 变化，只是不带 `doc_as_upsert`。

`full` 中 upsert-flow 与 native-update 分别重建索引；赛末 store 属于最后的 native 周期，不能当作 mixed 体积。参数与负载详情见 [tracks/geonames-upsert/README.md](../tracks/geonames-upsert/README.md)。

## 5. 运行中：看进度、看判定

```bash
tail -f /opt/raceway/campaigns/<campaign-id>/PROGRESS.txt   # 逐场时间线（当前场次 + 阶段）
cat    /opt/raceway/campaigns/<campaign-id>/RACES.md        # 场次判定表
```

跑分目录的前缀随车族变化（`jdk3-*` / `vm3-*` / `ds3-*` / `es819-*`…），不要按前缀找。

**只有完整、有效的测量能进结论。** 每个实际 phase 的每个预期节点都必须 `samples>0` 且 `ENV-VERDICT: OK`；`WARN`、`CONTAMINATED`、`NO-DATA`、缺节点或采集失败统一判 `ENV-FAIL`。分节点摘要和 CSV 在 `monitors/`，不能用另一个尝试的监控或 VALID 判定补证。

### 5.1 场次状态

| 状态 | 什么时候出现 | 怎么处理 |
|---|---|---|
| `VALID` | 协议跑完、必需写/查任务吞吐有限有效且零错误，全 phase 全节点监控有数据并为 OK | 按精确尝试身份进入后续质量筛选 |
| `SKIP` | 赛前静默卡点没过，或组合不兼容 | 不计有效圈；确认环境和计划后再续跑 |
| `DEADLINE-SKIP` | 预计用时超出持久化截止时间 | 不计有效圈；同 campaign 不延长窗口 |
| `ENV-FAIL` | 缺监控、缺节点、samples=0、采集失败，或 ENV 为 WARN / CONTAMINATED / NO-DATA | 不可用；查 `monitors/*-summary.txt`，解决后新尝试 |
| `RACE-FAIL` | rally 那边执行失败 | 已自动重试 ≤`RETRY_MAX`（默认 3）次；仍败就看 `logs/<fid>-*.log` 尾部 |
| `METRIC-NULL-FAIL` | 必需写/查任务缺失、吞吐无效或错误率非零；维护任务不能抵充 | 已自动重试；查任务、错误与暖机时间（§15 第 12 条） |
| `STOP-FAIL` / `CLEAN-FAIL` | 停净或清理未确认成功 | 不继续压测；核对目标进程、端口、安装根与日志 |
| `TASKS-FAIL` | 无法从赛道建立必需任务清单 | 检查赛道定义与参数；不猜测任务集合 |
| `ENGINE-FAIL` / `COLD-START-FAIL` / `CLUSTER-NOT-READY` | 部署、冷启动、集群检查失败 | 补跑这一场；仍败看 `logs/ibs-*.log`。集群检查会拦端口被顶替、版本不符、堆漂移 |
| `JDK-SWAP-FAIL` / `JDK-FIX-FAIL` / `JDK-ASSERT-FAIL` | JDK 换装、回切、身份校验失败 | 查 `JDK_DIR` 在被测节点上是否可达 |
| `HEAP-ASSERT-FAIL` | 实际 -Xmx 和声明的不一致 | 补跑这一场；复现就查配置漂移 |
| `CPU-ASSERT-FAIL` | 实际亲和核数和 `EXPECT_CPU_CORES` 不一致 | 查绑核的 drop-in 有没有残留 |

## 6. 跑后：收结果、对比、入库

```bash
# ① 下载到本地归档
scp -r <压测机>:/opt/raceway/campaigns/<id> "campaigns-local/"

# ② 两个取数池对比（读机读 JSON，不解析 Markdown 报告）
python3 car-compare.py --a-campaign campaigns-local/<A> --b-campaign campaigns-local/<B> --a 标签A --b 标签B

# 同车代号可跨 campaign 对比；各侧可重复传 --a-campaign / --b-campaign 合池
python3 car-compare.py --a-campaign campaigns-local/<A> --b-campaign campaigns-local/<B> --a jdk25 --b jdk25 --assert campaign-thresholds.example.conf

# ③ codec 实测组合汇总（写入 / 赛末体积 / 查询）；归档须带齐 logs/fp 与 sidecar
python3 codec-compare.py campaigns-local/<id> --detail

# ④ 出入库载荷（读跑分产物，不连 ES）
python3 build-race-store.py --campaigns campaigns-local --out race-store
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns-local

# ⑤ 导入 ES
python3 bench-apply.py --dir exports/<tag> --es <url> --auth <user:pass> --insecure
```

`--a-campaign` / `--b-campaign` 各定义独立取数池，可重复；同一车代号可在不同池比较，每侧内部则不能混 build、JDK、索引类型或实验条件。两侧须有相同的完整有效赛道与任务集合；空集合、身份冲突都非零退出。

三个对比工具共用 `result_selection.py`（依赖 `identity.py`）：精确匹配实际 fid 的 VALID、JSON 与 sidecar，要求协议完整、所有任务错误率为零且全节点监控 OK；同一基础 fid 只取最新有效尝试，不把失败尝试或重试计为额外轮次。`RACES.md` 只提供判定，性能值一律读机读 JSON，不从展示用 Markdown 表格取数。

`--assert` 的 CSV 格式为 `track,task,metric,>=|<=,value`，track 写实际变体 id。每条规则检查 **B 侧至少 3 个有限有效测量的中位数**；未知赛道/任务/指标、格式错误、非有限阈值、空规则集、样本不足或不达标均以退出码 1 结束。A 侧展示散点与中位供比较，不替代 B 的样本数要求。

**怎么读数**：

- 写入看 `race.json` 中写任务的 `throughput.median`（docs/s）；upsert 各阶段分开比较
- 查询看各任务 `service_time` 的 p50/p90/p99；工具展示 ms，入库归一为 s
- 正式数值取跨圈中位，同时保留逐圈散点和范围；单场 p50 不是跨圈中位
- 看板以 `comparison_key=series_key+'|'+track_variant` 分线，variant 独立显示；错误率面板不受全局 `trusted:true` 过滤

`bench-apply.py` 在任何请求前预检所选模板、全部 JSONL、字段类型、有限数、唯一 ID 与 race 引用；缺文件、总载荷为空或校验失败非零退出。`--drop` 仅在预检通过后删除明确列出的所选索引，不会先删后找输入。

三张索引、稳定测量键和统计口径见 [METHODOLOGY](METHODOLOGY.md)。

## 7. 收尾与环境复位

```bash
./reset-env.sh all --purge          # 彻底还原：卸安装、清数据、回滚监控和系统参数
./reset-env.sh easysearch --no-wipe # 只停不清
./cpu-affinity.sh clear             # 跑完全核矩阵前必做（如果之前 apply 过）
```

**压测期间要停掉指标库。** 压测机就是三台被测节点的宿主机，指标库（9237）和适配层（9202）跟被测引擎抢同一批物理 CPU，压测窗口里必须停着：

```bash
systemctl stop easysearch-metric-store.service
```

赛前卡点会查这两个端口，命中就跳过该场（`HOST_QUIET_PORTS=""` 可以关掉这个检查）。

想手动看某台被测节点的负载，ssh 过去执行 `/opt/raceway/monitor/load-monitor.sh probe 6`（1 秒采一次）。

中途被停掉的跑分怎么收尾，见 §14.1。

---

# 下篇 · 跑起来之后

## 8. 框架分层

| 层 | 内容 | 位置 |
|---|---|---|
| 部署层 | **ibs** CLI（`python3 -m ops.deploy/status/clean`）：装集群（systemd + TLS + admin 初始化）、切引擎版本、集群健康检查、冷重启 | 压测机 `/opt/raceway/ibs/` |
| 方法学层 | **raceway**（本仓库）：静默卡点 → 冷重启 → 集群卡点 → 采样 → 跑一场 → ENV 判定 → 归档 | 压测机 `/opt/raceway/` |
| 发压层 | esrally 2.12+，只做 benchmark。发压机和被测集群分开，节点清单取自 ibs `ip.txt`（`esrally` 那行是发压机） | 压测机 |

脚本在压测机上以 root 运行。对被测节点的操作全部通过 ssh 免密下发，节点列表就是 `ip.txt` 里的私网地址。

## 9. 脚本清单

`*.sh` 都在压测机上跑。集群的部署和生命周期只有一条路：ibs CLI。`*.py` 工具在任何有 python3 的机器上都能跑。

| 脚本 | 干什么 | 怎么用 |
|---|---|---|
| `vm-bench.env` | 共享配置：拓扑、端口、堆、版本号、包路径、赛道路径、卡点阈值、超时 | 各脚本 source 它，**不要直接执行**。从 `vm-bench.env.example` 复制填写 |
| `vm-bench-lib.sh` | 公共函数库：远程执行、拓扑推导、引擎目录映射、赛道名推导、堆断言 | 被 source，**不要直接执行** |
| `run-bench.sh` | **唯一的编排入口**：三种编排方式、卡点链、断点续跑、`list` 子命令 | `./run-bench.sh <赛道> [圈数] [配置文件]` |
| `precheck.sh` | 赛前静默卡点：负载、steal、bench 进程、外来进程、磁盘 | `./precheck.sh quiet` |
| `load-monitor.sh` | 被测节点侧采样器，只读 `/proc`，5 秒一采 | run-bench 自动启停；手动可 `start` / `stop` / `probe` / `summary` |
| `cpu-affinity.sh` | 从 ibs 拓扑逐节点写入 systemd CPUAffinity drop-in，模拟 8 核环境；空拓扑或节点失败不报成功 | `./cpu-affinity.sh apply 8` / `status` / `clear` |
| `doctor.sh` | 新环境体检：六组只读检查（工具、env、拓扑、连通、语料、指标库） | `./doctor.sh` |
| `tracks/` | 赛道定义定版目录（index.json / challenges / operations / track.json 全在 git 里） | runner 直接消费；语料软链、queries.json 等 git 收不了的产物由 run-bench 起跑前补 |
| `reset-env.sh` | 环境还原 | `./reset-env.sh <easysearch\|elasticsearch\|all>`，可加 `--no-wipe` / `--purge` |
| `stop-campaign.sh` / `close-campaign.sh` | 精确停止一个 campaign 的进程 / 给已停止的 campaign 补结束行 | `./stop-campaign.sh <完整campaign-id>`；`./close-campaign.sh [--dry-run]` |
| `identity.py` / `result_selection.py` | 实测身份与有效测量选择的公共口径 | 对比工具共用；须与调用方一起同步 |
| `audit_track_settings.py` | 赛道 index.json 的 settings 结构审计（codec/source_reuse 必须是顶层键；只报告不修复） | `python3 audit_track_settings.py --check tracks/`；回归由 `tests/test_track_settings.py` 守着 |
| `bench-sidecar.py` | 每场的元数据 sidecar：实测 JDK 版本、索引 settings、核数、堆 | 由 run-bench 每场调用 |
| `car-compare.py` | 显式 A/B 取数池对比 + B 侧中位阈值断言 | `python3 car-compare.py --a-campaign <A> --b-campaign <B> --a 标签A --b 标签B` |
| `car-env-table.py` | 按实测身份 × 赛道变体 × 写任务汇总性能与环境 | `python3 car-env-table.py <跑分目录...> [--detail]` |
| `codec-compare.py` | 按身份与变体分组：写入、赛末体积、查询 | `python3 codec-compare.py <跑分目录...> [--detail]` |
| `bench-queue.py` / `bench-history.py` / `bench-compose.py` | 队列观测台 / 已入库覆盖表 / 控制台，见 §14 | 见 §14 |
| `build-race-store.py` → `bench-export.py` → `bench-apply.py` | 跑分产物 → rally race store → 入库载荷 → 导入 ES | `python3 bench-export.py --out exports/<tag>` |

> Kibana 建板工具链自成一套，在 `kibana/` 目录里（见 [kibana/README.md](../kibana/README.md)）。

## 10. 一场压测内部发生了什么

```
确认目标引擎全部停净、端口空闲 → 按 IBS_BASE 清理数据 → 赛前负载卡点
→ ibs 引擎就位（版本不符则重装） → JDK 就位 → ibs wipe 冷启动
→ 集群卡点 → 堆断言 → 绑核断言 → 开始采样
→ full race（写 + 合并 + 查，一次 rally 调用） → 写/查指标断言
→ （需要时打召回探针） → 停采样并收集 → 全节点 ENV 门禁 → sidecar 与产物归档
```

几个关键取舍：

- **单挑战协议**：编排侧一次 rally 调用跑完（写 + 合并 + 查），不单独做段数检查。合并由赛道自带的 full challenge 完成（force-merge 同步接口 + 等合并结束）。代价是「合并结果没有再单独验证」，这是接受的简化。
- **采样**：full 挑战全程一份采样，tag 为 `<实际fid>-wq`，默认 5 秒一条 CSV；每个实际 phase、每个预期节点都必须有 `samples>0` 且 ENV 为 OK，缺失即失败。手动 stop 必须给 tag，只停对应采样器。
- **先停后清**：按统一 `IBS_BASE` 和引擎清单停净并确认端口空闲，才允许清数据；SSH 失败或归属不明不能当作停净。
- **堆**：由 ibs 在部署时统一设成物理内存的一半（16g）。easysearch 写进 `jvm.options`，ES 写进 `jvm.options.d/heap.options`。
- **JDK**：按 `JDK_DIR` 幂等换装。第一次换装会把引擎自带的 JDK 归档成 `jdk.bundled`；没指定 `JDK_DIR` 的配置会自动切回自带的。
- **身份**：`CAR_LABEL` 会进场次名和 user-tags。sidecar 另外逐节点实测 `java -version` 和索引 settings 落档。

## 11. 产物结构

跑分目录 `campaigns/<campaign-id>/`：

```
PROGRESS.txt        逐场时间线，tail -f 看进度
RACES.md            逐实际尝试判定，所有模式续跑均保留
STATE.txt           基础 fid 的 OK / FAIL / SKIP 状态
PLAN.tsv            锁定计划；表头 fid/config/track/round/recall/est（tab 分隔）
PLAN.sha256         计划摘要；续跑必须匹配
DEADLINE            首次截止 epoch；0 表示无时间窗
RUNNER.pid          活 runner 的纯 PID
RUNNER.meta         pid/start_ticks/boot_id/script 绝对路径（单行 tab 分隔）
SCRIPT_SNAPSHOT     本次运行脚本快照路径
runtime/            每次启动的只读脚本快照
PACKAGES.txt        配置清单 + 安装包 sha256
CODEC-VARIANTS.txt  codec 赛道的变体 settings 台账
results/            rally 报告 + race.json + sidecar.json；尝试间不覆盖
logs/               rally 与 ibs 日志、环境指纹、JDK 实测版本
monitors/           各被测节点的采样 CSV + summary + ENV-SUMMARY.csv
```

## 12. 两条纪律

- **开跑前先校验，错了就停**。逐行检查配置：包在不在、ES 有没有误带 `JDK_DIR`、`CAR_LABEL` 有没有重复。有一行不合法就直接退出，**不会建跑分目录**，避免留下半截产物。
- **对比只用实测数据**。取数池显式分 A/B，每侧保持身份与实验条件一致；JSON、sidecar、精确判定及逐节点监控共同决定有效性。`--assert` 每规则至少 3 个 B 侧有限有效测量，中位不达标或规则有误均非零退出。

## 13. 赛道说明

赛道定义的目录结构、规矩与待办见 [tracks/README.md](../tracks/README.md)；这里只讲编排层怎么用。

### 13.1 codec 赛道（geonames 存储压缩）

`./run-bench.sh codec 3` 一次跑六个变体，也可以单独跑：`./run-bench.sh geo-codec-def 3`。

变体的命名规则是 `<基础赛道>-codec-<def|zstd|bc>[-sr]`：

```
geo-codec-def      geo-codec-def-sr
geo-codec-zstd     geo-codec-zstd-sr
geo-codec-bc       geo-codec-bc-sr
hnsw-codec-zstd    ds-j1024-codec-bc-sr   …（任意基础赛道都能这么派生）
```

**压缩与 source_reuse 是两个独立维度**；以下配对每次只改变一个变量：

| 对比 | 差异来自 |
|---|---|
| `geo-codec-def` ↔ `geo-codec-zstd` | 压缩方式（source_reuse 都关） |
| `geo-codec-def-sr` ↔ `geo-codec-zstd-sr` | 压缩方式（source_reuse 都开） |
| `geo-codec-def` ↔ `geo-codec-def-sr` | source_reuse（压缩都是 default） |
| `geo-codec-zstd` ↔ `geo-codec-zstd-sr` | source_reuse（压缩都是 ZSTD） |

**引擎兼容**（依据 Easysearch 源码 `EngineConfig.INDEX_CODEC_SETTING` 和实测）：

- `index.codec` 的合法值：`default` / `best_compression` / `lucene_default` / ZSTD 系列。ZSTD 系列是 Easysearch 专有，ES 只认 default 和 best_compression。
- `index.source_reuse` 也是 Easysearch 专有，ES 不认这个设置。

所以 Easysearch / ES 都能跑的是「压缩 default 或 best_compression，且 source_reuse 关」。显式排程含不兼容组合会在起跑前拒绝；`codec` 自动矩阵则留痕跳过不适配项，不等建索引时才失败。

几个实现细节：

- 赛道定义定版在 `tracks/geonames/`（git 里就是跑的那份）。压缩档与 source_reuse 走 `--track-params`（`index_codec:` / `source_reuse:true`），由 `index.json` 的条件块写入 settings —— 没有第二份运行时生成的赛道定义。
- **分片数不烘在变体里，靠 track-params 传**。geonames 的 `index.json` 是 Jinja 模板，分片位置是 `{{number_of_shards | default(5)}}` —— 不传就落到模板默认的 **5 分片**。runner 按**基座赛道**统一传 `SHARDS`/`REPLICAS`（默认 3/0），canonical 变体名（`geo-codec-*`）与别名（`gdef`/`gzstd`/`gbc`）拿到同一份口径。变体目录里看不到分片设置，不代表分片数就是模板默认值。
- **短名**：`gdef` = `geo-codec-def`、`gzstd` = `geo-codec-zstd-sr`、`gbc` = `geo-codec-bc`；排程建议用规范 id。
- **量化独立解析**：`hv1024x-int8-codec-def` 仍为 `int8_hnsw`，`hv1024x-bbq-codec-*` 仍为 `bbq_hnsw`；不能剥掉压缩后把量化臂归回 hnsw。
- 可证明性：sidecar 每场抓一次 `GET /<索引>/_settings?flat_settings=true` 的实测值 —— 烘进赛道不等于集群里真是这个值。
- 汇总用 `codec-compare.py <跑分目录> [--detail]`，出写入两口径、赛末体积、逐操作查询三张表。

### 13.2 hv1024 赛道（1024 维 float HNSW）

| 项 | 值 |
|---|---|
| 数据 | msmarco passage × jina-embeddings-v3（官方 `msmarco-jina-10m-vector` 的 1M 子集），来源见 [hv1024-track](hv1024-track.md) |
| 规模 | 100 万条 × 1024 维 float，`dot_product`，HNSW m=32 / ef_construction=100 |
| 字段 | `docid` keyword + `emb` dense_vector（关 `_source`，`dynamic: false`） |
| 索引 | `vectors`，3 分片 0 副本，写完 forcemerge 到每分片 1 段 |
| 查询 | `knn-search-10-100`（k10/ef100）与 `knn-search-100-1000`（k100/ef1000），warmup 100，测 1000 次迭代 |
| 写入口径 | bulk-size 500 × 8 客户端。1024 维单条约 22KB，和 96 维 hnsw 的 5000×1 不能直接比 |
| 赛道定义 | 定版在 `tracks/hv1024/`（git）；起跑前只补语料软链与 queries.json |

和 96 维 `hnsw` 的定位差别：hnsw 测「2M×96 的建图吞吐」，hv1024 测「高维的建图与遍历成本」。跨赛道只看趋势，不看绝对值。8 核环境的做法见 §4.3。

### 13.3 向量请求与索引身份

KNN 的 `size` 写入实际搜索 body，与 task 的 k 一致；k=100 的任务明确请求返回 100 条，不依赖搜索默认 size。索引类型优先取 sidecar 回读的实际 mappings；声明值须按本场参数渲染，不能把 Jinja 字面量当实测。

OpenSearch 的 HNSW 搜索预算使用 `ef_search`，不等同于 ES 的每分片 `num_candidates`；磁盘/IVF 变体按各自方法与重排参数记录，不能因任务名和数字相同就声称候选预算一致。看对比时同时核对 `track_variant`、`knn_index_type` 和 sidecar 的逐任务预算；赛道细节见 [hv1024-track](hv1024-track.md)。

## 14. 控制台（可选）

`bench-compose.py serve` 起一个页面，三块内容合一：

```bash
python3 bench-compose.py serve --ssh root@HOST --es-auth 'admin:xxx'
# --ssh 缺省读环境变量 RACEWAY_SSH，--es-auth 缺省读 RACEWAY_ES_AUTH
# 默认 http://127.0.0.1:8802，起完自动开浏览器（--no-open 关掉）
```

**① 启动排程** —— 勾选矩阵（车 × 赛道 × 圈数），按可用历史覆盖剔除已完成格子，生成排程、dry-run 预览、起跑。空选车或未知车名直接拒绝。每次请求生成独立不可变快照，车定义、排程、参数与摘要绑定；同 tag 只能复用相同内容，不覆盖另一个排程。起跑成功须确认 PID、campaign 和摘要，不以后台命令已提交当作成功。预计总耗时超出 `DS_HOURS` 会警告，实际截止由 runner 首次持久化。勾选状态记在 localStorage，成功后跳进度页。

**② 运行进度** —— 自动选中最近一个没收尾的跑分：进度条、总场次/有效/失败/待跑卡片、剩余分钟数和预计完成时刻、当前场次与阶段、已跑多久。页面标题当状态栏（`▶ 场次 · 阶段`），切走也能瞄。`PROGRESS.txt` 的实时日志直接贴在页面上。可以一键停掉整个跑分，会二次确认，停完自动补结束行。

**③ 历史与结果** —— 跑分清单、关键字筛选、复看进度与「按此重排」。两场对比显式传各自 campaign 到 A/B 取数池，支持相同车代号跨池比较；远端执行本机对比程序及 `result_selection.py` / `identity.py` 依赖，输出有效逐圈值、中位差值与极差，不解析 Markdown 报告。车代号留空会自动选择，正式比较建议明确填写。

不开页面的话，五个子命令是同一套能力的程序化入口：

```bash
python3 bench-compose.py cars    --cars-file <f>                   # 列出车定义文件里的车
python3 bench-compose.py gen     --cars-file <f> --tracks ds-*     # 生成排程文件
python3 bench-compose.py preview --cars-file <f> --tracks ds-*     # 预览：跑 runner 自己的 --dry-run
python3 bench-compose.py start   --cars-file <f> --tracks ds-* --rounds 3 --ds-hours 12
python3 bench-compose.py serve   --port 8802                       # Web 控制台
```

`start` 将同一摘要对应的车定义与排程上传、dry-run、再后台启动；tag 仅可用安全文件名，不能越出 `schedules/`，已有不同内容不覆盖。控制台预检不能代替 runner 的 `flock` 实例锁。

另外两个只读工具：

- `bench-queue.py` —— 看队列。list / status / plan 三个子命令，结合 `PLAN.tsv`、`DEADLINE`、`STATE.txt` 和实际进度/判定展示计划与执行，提示计划或状态矛盾；不能可靠确认的部分标未知，不猜成待跑或成功。`PLAN.sha256` 由 runner 校验。（网页版在 `bench-compose.py serve` 中）
- `bench-history.py` —— 看已入库的历史。把指标库 `benchmark-result-race` 读成「车 × 赛道 × 圈数」的覆盖表，回答「哪些格子跑过了、判定是什么、用的哪个 build」。

进度页的推导就是来自 `bench-queue.build_campaign`。

几个使用前提：

- 指标库不可用时不会冒充“没有历史”：认证、HTTP、响应结构或分页错误都会显示不可用，编排不据此自动剔除格子。
- 可排赛道以 runner `list` 与定义可用性为准，目录/语料缺失或没有 runner 分支的项目不能排。预计分钟数只是估算，不是性能结论。
- 覆盖按车、build、真实 `track_variant` 与圈判定；一次完整测量须来自同 campaign、真实 fid/attempt，拆分协议要写查齐全，不能跨 campaign 或尝试拼半场。`car_code`、`knn_index_type` 等身份列参与查询投影，不将 int8 / BBQ 格子归到基座。

### 14.1 中途停掉的跑分怎么收尾

进度台判「已结束」，只看 `PROGRESS.txt` 里最后一次 `启动:` 之后有没有 `campaign 结束: N/M 场有效` 这一行。

停止活跑分只给完整 campaign id：

```bash
./stop-campaign.sh <完整campaign-id>
```

工具同时校验 `RUNNER.pid` 与 `RUNNER.meta` 的 `pid/start_ticks/boot_id/script`，仅向本 runner 及可验证后代发信号；rally 还须精确匹配完整 campaign tag。不按命令行子串扫杀所有 runner，也不凭旧 PID 发信号；身份缺失或不符就拒绝停止，不能用旧 campaign 页停掉当前另一个 campaign。

runner 的 TERM/INT/HUP 与异常退出会补中止结束行；`kill -9`、宿主机重启等情况可在确认已停止后补写：

```bash
./close-campaign.sh --dry-run          # 预演；活 runner 或实例锁忙则跳过
./close-campaign.sh                    # 对确认已停止的 campaign 补结束行，保留原 mtime
./close-campaign.sh <完整campaign-id> --reason "宿主机重启"
```

收尾只补进度文本，不改 `RACES.md`、`STATE.txt`、`PLAN.tsv` 或截止时间，也不把失败尝试改成有效；`CAMP_ID=<id>` 仍可按原计划续跑。

## 15. 规则与后果（不能破的规矩）

| # | 规则 | 后果 |
|---|---|---|
| 1 | 报告文件名必须全场唯一（重试场次用 `-tryN` 后缀） | rally 的 markdown 报告是追加不覆盖的，不加后缀会串写 |
| 2 | ES 冷重启的口径由 ibs wipe 承担：`ops.clean wipe` 先停服务再清空 data 和 logs，重启后 `_restore_admin` 用 keystore 的 bootstrap.password 重放一遍 admin 创建（幂等） | 不做就带着上一场的状态进场，三台车冷态口径不一致 |
| 3 | easysearch 的 wipe 不用特殊处理：admin 由 `config/security/user.yml` 承载，data 可整个删 | 和 ES 走同一条 ibs wipe 命令，不必分支 |
| 4 | 停机仅操作经过 PID 起始身份与归属验证的目标；campaign 用 RUNNER.meta，采样器用 tag 对应 PID 身份 | 不得按 `run-bench.sh` / `java` 子串全局 pkill，避免 PID 复用与误杀无关进程 |
| 5 | 语料目录必须幂等生成，且绝不能带 `.offset` 断点文件 | rally 会续传，写入计量失真 |
| 6 | 段数要用带后缀的接口采（`_cat/segments` 按 prirep 过滤） | rally 报告里的段数是 `_all` 口径，会把 `.security` 也算进去 |
| 7 | 整个跑分用 `setsid nohup` 后台跑，人只轮询 PROGRESS.txt | SSH 断连会带走整个跑分 |
| 8 | 集群检查显式断言 ibs 拓扑推导的节点数、数据节点数和节点名，不能只看 green | 0 副本时单节点也可能 green，拓扑不符不可起跑 |
| 9 | 清数据前必须停净所有目标引擎并确认 HTTP 端口空闲，冷启后再断言版本与端口属主；按统一 IBS_BASE 定位 | 停机、SSH 或端口检查失败不能当成功，更不能删在线 data 或静默串台 |
| 10 | 监控归因标记 ENGINE_MARK 要跟着部署根走（默认 `/infini` 即 ibs 安装根，run-bench 每场精确指到被测引擎目录）；**换部署根必须同步改** | 标记错位时 bench 那列恒为 0，引擎 CPU 被算成外来进程，「外来进程」这个判定就瞎了 |
| 11 | `cpu-affinity.sh apply 8` 写的 drop-in 是持久生效的（跨 ibs 部署、wipe、重启）；跑全核（32 vCPU）矩阵前必须先 `./cpu-affinity.sh clear` | 引擎被静默限制在 8 核，数字全废。`EXPECT_CPU_CORES` 每场都会兜底断言 |
| 12 | 写任务暖机按实测时长标定，约为任务的 20%，测量窗口至少留 50%；bulk 不用固定 time-period 截断语料 | 窗口被暖机吃光会产生 null；runner 按必需写/查任务分别校验，返回码 0 也不能自动 VALID |
| 13 | 口径A（rally `Median Throughput`）与口径B（语料条数 ÷ `duration`）都要留；`car-compare.py` 两个口径都会输出 | 差别来自写入慢尾不是窗口长短：nested 窗口 1.7~14.4s 时两者差 ±0.8%，hnsw 窗口 382~404s 时口径A 反而系统性高 3.5% |
| 14 | 堆由 ibs 在部署时统一设成物理内存的一半（16g）；ES 配置里没有堆行时写一份 `config/jvm.options.d/heap.options`（幂等覆写）。编排侧 `heap_assert` 硬断言每一台车 | ES 8.19 出厂 `config/jvm.options` 里没有 `-Xms/-Xmx`，堆走 JVM 默认值（物理内存 1/4，32G 节点上只有 8g），和 ez 显式设的 16g 不是同一个基线 |
| 15 | 比落盘体积用赛末 sidecar 原始探针：`logs/fp/<实际fid>-wq/nodes-stats.txt`；拆分协议为 `-q/`，对全部被测节点求和 | 缺探针就显示缺失，不用 race.json 快照冒充赛末体积；同时核对索引范围 |
| 16 | JSON、sidecar、判定和监控必须精确匹配真实 fid/attempt/phase；对比共用 result_selection.py 与 identity.py | try2 不得借基础 fid 的 VALID，失败尝试不计轮；入库 race_id 是稳定测量键，不取临时主段 UUID |
| 17 | 看板固定真实 track_variant，按 comparison_key 分线；时延固定分位，正式值跨圈中位，错误率面板独立于 trusted 过滤 | 仅按 track/stat 或 series_key 聚合会混引擎、量化、压缩与重试；图表时延统一 s，错误证据不能隐藏 |
| 18 | 主循环消费已锁定的计划，子命令 stdin 与排程输入隔离 | ssh、ibs、rally、tar 等不得吞掉后续排程，也不能边跑边重读变化中的排程文件 |
