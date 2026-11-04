# raceway 使用手册（GUIDE）

> 分两层写：
> - **上篇「怎么跑」** —— 想测什么、敲什么命令、怎么看进度、跑完怎么收结果。
> - **下篇「跑起来之后」** —— 框架怎么分层、脚本各管什么、一场压测内部发生什么、踩过哪些坑。
>
> 方法学与统计口径见 [METHODOLOGY](METHODOLOGY.md)，hv1024 赛道的数据来源见 [hv1024-track](hv1024-track.md)。
> 门面前言与最短上手路径见仓库根 [README](../README.md)。
> 所有环境配置都放在 `vm-bench.env`（模板 `vm-bench.env.example`，真身不入库）。

## 名词速查

| 词 | 意思 |
|---|---|
| 车（配置） | 一套被测环境：引擎版本 + JDK + 标签。配置文件里一行就是一台车 |
| 赛道（track） | 一个压测用例：建哪个索引、写多少数据、查什么 |
| 圈（round） | 同一条赛道重复跑的第几遍。多跑几圈取中位数，摊掉偶然波动 |
| 场次 | 「车 × 赛道 × 圈」的一个具体组合，名字形如 `kona25-geo-r2` |
| campaign | 一场完整跑分，含若干场次，产物在 `campaigns/<id>/` 目录 |
| 卡点 | 开跑前的自动检查。不通过就不开跑，或直接跳过这一场 |
| sidecar | 每场附带的一份元数据 JSON：实测的 JDK 版本、堆大小、索引 settings、节点负载 |
| ENV 判定 | 拿采样数据判断这台机器这一场环境干不干净（CPU steal / 外来进程 / swap） |

---

# 上篇 · 怎么跑

## 1. 入口决策表：我想测什么 → 我敲什么

命令都在压测机的 `/opt/raceway` 下跑。

| 我要测 | 命令 | 单场时长 | 整场大概 |
|---|---|---|---|
| 多配置对比（引擎 × JDK） | `./run-bench.sh geo 3 my3.conf` | ~26 分钟 × 配置数 | 3 配置 3 圈 ≈ 8 小时 |
| 存储压缩对比 | `./run-bench.sh codec 3 ...` | ~25~30 分钟 | 每配置 9 场 ≈ 4 小时 |
| 1024 维向量（8 核环境） | `EXPECT_CPU_CORES=8 ./run-bench.sh hv1024 3 ...` | ~17~19 分钟 | 每配置 3 场 ≈ 1 小时 |
| 96 维 hnsw / nested | `./run-bench.sh hnsw 3 ...` / `./run-bench.sh nested 3 ...` | ~26 分钟 | — |
| 冒烟、补跑一场 | `./run-bench.sh geo 1` | ~26 分钟 | — |
| 逐场定制（哪台车 × 哪条赛道 × 第几圈） | `SCHEDULE_FILE=… CARS_FILE=… DS_HOURS=12 ./run-bench.sh ds` | 按每行估 | 按时间上限裁剪 |

> 时长是实测值，来自各次跑分的 `RACES.md`。

> ⏰ 正式数据建议夜里跑。压测机是共享环境，白天有噪声；静默卡点会自动跳过不安静的时间段。

## 2. 通用前提（每次都要过的 4 件事）

```bash
# ① 登录压测机（Host 别名配在 ~/.ssh/config，节点清单在 ibs 的 ip.txt）
ssh <压测机>
cd /opt/raceway

# ② 改过本地 raceway 仓库后，先同步到压测机（在本地仓库目录执行；只覆盖不删除）
scp *.sh *.py *.conf vm-bench.env <压测机>:/opt/raceway/

# ③ 长任务一律 setsid nohup 后台跑，人只轮询 PROGRESS.txt（防 SSH 断连）
# ④ 跑完把 campaign 下载回本地归档（见 §6）
```

被测引擎的包路径、堆大小、卡点阈值都在 `vm-bench.env`。JDK 不走包配置，`JDK_DIR` 直接写被测节点上的目录。

⚠️ scp 只覆盖不删除：压测机上删过的文件，本地删了还要 `ssh <压测机> rm` 一次。

## 3. 三种编排方式

```bash
./run-bench.sh <track> [rounds=3] [配置文件]     # track: geo|gdef|gzstd|gbc|<赛道>-codec-<def|zstd|bc>[-sr]|hnsw|hv1024|nested|codec|both|all|ds|ds-<档名>
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
| `ENGINE_NAME` | `easysearch`（默认）或 `elasticsearch` |
| `ENGINE_VERSION` | 默认 2.5.0；ES 用 8.19.23 这样 |
| `ENGINE_BUILD` | 只有 easysearch 有（默认 3015）；给 ES 设了会报错 |
| `JDK_DIR` | 被测节点上已解压好的 JDK 目录。只有 easysearch 能用；首次换装会自动把自带 JDK 归档起来 |
| `CAR_LABEL` | 这台车的标签，进场次名和元数据。多配置时必须互不相同；不填就取 JDK 目录名或引擎身份 |
| `JDK_WANT` | 可选，形如 `"25\|OpenJDK"`，是期望的 JDK 身份，进元数据并参与校验 |

> 旧名 `ARM_LABEL` 仍然认，改名前写的配置行照抄能用。

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

1. 赛道名写规范 id（`geo` `hnsw` `hv1024` `nested` `ds-*` `geo-codec-*`），不要写历史短别名。
2. 最后那个数字是该场预计耗时。时间窗保护靠它判断「这场还塞不塞得下」。
3. 设了 `DS_HOURS=12`，放不下的场次标 `DEADLINE-SKIP` 就跳过，继续跑后面更便宜的场 —— 保证高价值的数据先落袋。时间窗末尾还留 20 分钟余量。
4. 排程方式不做轮内换位，文件里的行序就是执行顺序。断点续跑认 `STATE.txt`（用 `CAMP_ID` 复用同一个跑分目录）。

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
./run-bench.sh codec 3 ...                  # ez: gdef(默认LZ4) + gzstd(ZSTD+SR) + gbc(BC)
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ./run-bench.sh gbc 3 ...  # ES 只认 default 与 best_compression
```

赛道变体由 `prepare-codec-track.sh` 自动生成，幂等。机制见 §13.1。

### 4.3 1024 维向量（8 核环境）

```bash
# ① 绑核。写进 systemd drop-in，重启和重装之后都还在
./cpu-affinity.sh apply 8        # status 查看 / clear 还原

# ② 跑。每场都会断言实际核数
EXPECT_CPU_CORES=8 ./run-bench.sh hv1024 3 ...    # ES 对照另跑一个 campaign
```

⚠️ 绑核之后如果要跑全核（32 vCPU）的矩阵，必须先 `./cpu-affinity.sh clear`。否则引擎被静默限制在 8 核，数字全废。`EXPECT_CPU_CORES` 的断言会兜住这一步。

### 4.4 96 维 hnsw / nested

```bash
./run-bench.sh hnsw 3 ...     # 2M×96 图构建吞吐
./run-bench.sh nested 3 ...   # nested 赛道 3 分片变体
```

### 4.5 冒烟与补跑

```bash
./run-bench.sh geo 1                                  # 跑一场（真跑，约 26 分钟）
./run-bench.sh geo 1 --dry-run                        # 只看排程，秒回
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ./run-bench.sh hnsw 1   # 补某配置某赛道一圈
```

补跑要能和原来的跑分对上，`CAR_LABEL` 必须一致。

断点续跑：重跑同一条命令，`RACES.md` 里已经 VALID 的场次会自动跳过。`FRESH=1` 强制全部重跑。

## 5. 运行中：看进度、看判定

```bash
tail -f /opt/raceway/campaigns/<campaign-id>/PROGRESS.txt   # 逐场时间线（当前场次 + 阶段）
cat    /opt/raceway/campaigns/<campaign-id>/RACES.md        # 场次判定表
```

跑分目录的前缀随车族变化（`jdk3-*` / `vm3-*` / `ds3-*` / `es819-*`…），不要按前缀找。

**只有 VALID 场的数据能进结论。** ENV 判定为 CONTAMINATED 的场次不可用，分阶段汇总在 `monitors/` 里。

### 5.1 场次状态

| 状态 | 什么时候出现 | 怎么处理 |
|---|---|---|
| `VALID` | 一场跑完，指标不为空 | 计入结论 |
| `SKIP` | 赛前静默卡点没过，环境被占用 | 等窗口，不重试 |
| `RACE-FAIL` | rally 那边执行失败 | 已自动重试 ≤`RETRY_MAX`（默认 3）次；仍败就看 `logs/<fid>-*.log` 尾部 |
| `METRIC-NULL-FAIL` | 测量窗口被暖机吃光，吞吐全 null | 已自动重试；查赛道的暖机时间标定（§15 第 12 条） |
| `ENGINE-FAIL` / `COLD-START-FAIL` / `CLUSTER-NOT-READY` | 部署、冷启动、集群检查失败 | 补跑这一场；仍败看 `logs/ibs-*.log`。集群检查会拦端口被顶替、版本不符、堆漂移 |
| `JDK-SWAP-FAIL` / `JDK-FIX-FAIL` / `JDK-ASSERT-FAIL` | JDK 换装、回切、身份校验失败 | 查 `JDK_DIR` 在被测节点上是否可达 |
| `HEAP-ASSERT-FAIL` | 实际 -Xmx 和声明的不一致 | 补跑这一场；复现就查配置漂移 |
| `CPU-ASSERT-FAIL` | 实际亲和核数和 `EXPECT_CPU_CORES` 不一致 | 查绑核的 drop-in 有没有残留 |

> `WRITE-FAIL` / `QUERY-FAIL` / `SEG-ASSERT-FAIL` 是旧协议时代的名字，现在不会再出现。
> `bench-queue.py` 的 `STATUS_FAILS` 已补上 `RACE-FAIL`（2026-11-02 修复），那 3 个旧名字还留着，但不会再命中。

## 6. 跑后：收结果、对比、入库

```bash
# ① 下载到本地归档
scp -r <压测机>:/opt/raceway/campaigns/<id> "campaigns-local/"

# ② 两个配置对比（读 race.json，本地和压测机都能跑）
python3 car-compare.py campaigns-local/<jdk3-A> campaigns-local/<jdk3-B> --a 标签A --b 标签B
python3 car-compare.py campaigns-local/<jdk3-A> campaigns-local/<jdk3-B> --a 基线 --b 候选 --assert campaign-thresholds.example.conf

# ③ codec 五组配置的汇总表（写入 / 赛末体积 / 查询），建议在压测机上跑，要用到 sidecar 探针
ssh <压测机> "python3 /opt/raceway/codec-compare.py /opt/raceway/campaigns/<id>"

# ④ 出入库载荷（读跑分产物，不连 ES）
python3 build-race-store.py --campaigns campaigns-local --out race-store
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns-local

# ⑤ 导入 ES
python3 bench-apply.py --dir exports/<tag> --es <url> --auth <user:pass> --insecure
```

`--assert` 的阈值表是 CSV：`track,task,metric,>=|<=,value`。断言对象是被对比那一方的逐圈中位数，任何一条不达标就以退出码 1 结束，可以直接接进 CI 做发版回归。

**怎么读数**：

- 写入看 write 报告里 `index-append` 的 `Median Throughput`（docs/s）
- 查询看 query 报告里各操作的 `Median Service Time` 和 p90/p99
- 只用多圈的中位数，只认同号方向，不引用单圈

入库口径（三张索引、字段词表、`series_key`、幂等和 `--drop`）见 [METHODOLOGY](METHODOLOGY.md)。

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
| `cpu-affinity.sh` | 给三台节点的引擎绑核（systemd CPUAffinity drop-in），模拟 8 核的小规格环境 | `./cpu-affinity.sh apply 8` / `status` / `clear` |
| `doctor.sh` | 新环境体检：六组只读检查（工具、env、拓扑、连通、语料、指标库） | `./doctor.sh` |
| `prepare-codec-track.sh` | 生成存储压缩变体（幂等，带 `esrally info` 自检） | 选 codec 时 run-bench 会自动调 |
| `prepare-hv1024-track.sh` | 生成 1024 维赛道变体（幂等），见 §13.2 | 选 hv1024 时自动调 |
| `prepare-nested-track.sh` | 生成 nested 赛道 3 分片变体（幂等） | 选 nested 时自动调 |
| `prepare-dimsweep-track.sh` / `prepare-dimsweep-jdk.sh` | ds-* 赛道修复与自检 / 把 OpenJDK 26、27 装到三台被测节点（幂等） | 选 ds-* 时自动调 |
| `reset-env.sh` | 环境还原 | `./reset-env.sh <easysearch\|elasticsearch\|all>`，可加 `--no-wipe` / `--purge` |
| `stop-campaign.sh` / `close-campaign.sh` | 停掉某个跑分的进程 / 给中途停掉的跑分补「结束行」 | `./stop-campaign.sh <标签>`；`./close-campaign.sh [--dry-run]` |
| `identity.py` | 被测配置身份口径的唯一来源 | 被 sidecar 和导出器 import |
| `flatten_settings.py` | 赛道 index.json 的 settings 结构审计/修复（codec/source_reuse 必须是顶层键） | `python3 flatten_settings.py --check tracks/`；回归由 `tests/test_track_settings.py` 守着 |
| `bench-sidecar.py` | 每场的元数据 sidecar：实测 JDK 版本、索引 settings、核数、堆 | 由 run-bench 每场调用 |
| `car-compare.py` | 两个配置对比 + `--assert` 阈值断言（不达标退出码 1） | `python3 car-compare.py <跑分A> <跑分B> --a 标签A --b 标签B` |
| `car-env-table.py` | 三配置 × 多赛道的「性能 × 环境」联合表 | `python3 car-env-table.py <跑分目录> [--detail]` |
| `codec-compare.py` | codec 五组配置的三张表：写入、赛末落盘体积、查询 | `python3 codec-compare.py <跑分目录> [--detail]` |
| `bench-queue.py` / `bench-history.py` / `bench-compose.py` | 队列观测台 / 已入库覆盖表 / 控制台，见 §14 | 见 §14 |
| `build-race-store.py` → `bench-export.py` → `bench-apply.py` | 跑分产物 → rally race store → 入库载荷 → 导入 ES | `python3 bench-export.py --out exports/<tag>` |

> 旧名 `arm-compare.py` 和 `arm-env-table.py`（2026-10-21 改名前的副本）已删除，现在的版本是 `car-*`。
> Kibana 建板工具链自成一套，在 `kibana/` 目录里（见 [kibana/README.md](../kibana/README.md)）。

## 10. 一场压测内部发生了什么

```
装好配置 → 只停集群（不清数据） → 赛前负载卡点 → 让 ibs 把引擎就位（版本不符就自动重装）
→ ibs wipe 冷启动 → 集群卡点 → 堆断言 → 绑核断言 → 开始采样
→ 跑一场（写 + 合并 + 查，一次 rally 调用跑完） → 指标断言 → 判定
→ （排程里带 recall 的场次再打召回探针） → sidecar 与采样归档
```

几个关键取舍：

- **单挑战协议**（2026-10-28 起）：编排侧不再分成 write 和 query 两次 race，也不再单独做段数检查。合并由赛道自带的 full challenge 完成（force-merge 同步接口 + 等合并结束）。代价是「合并结果没有再单独验证」，这是接受的简化。
- **采样**：一整场共用一份采样，tag 是 `<场次名>-wq`，5 秒一条 CSV，ENV 判定写在 `monitors/` 里。
- **堆**：由 ibs 在部署时统一设成物理内存的一半（16g）。easysearch 写进 `jvm.options`，ES 写进 `jvm.options.d/heap.options`。
- **JDK**：按 `JDK_DIR` 幂等换装。第一次换装会把引擎自带的 JDK 归档成 `jdk.bundled`；没指定 `JDK_DIR` 的配置会自动切回自带的。
- **身份**：`CAR_LABEL` 会进场次名和 user-tags。sidecar 另外逐节点实测 `java -version` 和索引 settings 落档。

## 11. 产物结构

跑分目录 `campaigns/<campaign-id>/`：

```
PROGRESS.txt        逐场时间线，tail -f 看进度
RACES.md            场次判定汇总，也是断点续跑的状态来源
STATE.txt           排程方式的断点续跑状态（OK / FAIL / SKIP 加序号）
PACKAGES.txt        配置清单 + 安装包 sha256
CODEC-VARIANTS.txt  codec 赛道的变体 settings 台账
results/            rally 报告 + race.json + sidecar.json
logs/               rally 与 ibs 日志、环境指纹、JDK 实测版本
monitors/           三台被测节点的采样 CSV + summary + ENV-SUMMARY.csv
```

## 12. 两条纪律

- **开跑前先校验，错了就停**。逐行检查配置：包在不在、ES 有没有误带 `JDK_DIR`、`CAR_LABEL` 有没有重复。有一行不合法就直接退出，**不会建跑分目录**，避免留下半截产物。
- **对比只用实测数据**。`car-compare.py <跑分目录...> --a 标签A --b 标签B [--assert thresholds.conf]`。断言对象是被对比那一方的中位数，不达标退出码 1，可以直接接 CI 或发版回归。

## 13. 赛道说明

### 13.1 codec 赛道（geonames 存储压缩）

`./run-bench.sh codec 3` 一次跑六个变体，也可以单独跑：`./run-bench.sh geo-codec-def 3`。

变体的命名规则是 `<基础赛道>-codec-<def|zstd|bc>[-sr]`：

```
geo-codec-def      geo-codec-def-sr
geo-codec-zstd     geo-codec-zstd-sr
geo-codec-bc       geo-codec-bc-sr
hnsw-codec-zstd    ds-j1024-codec-bc-sr   …（任意基础赛道都能这么派生）
```

**为什么要拆成两个维度**：早先三个变体把压缩和 source_reuse 混在一个名字里（`geo-codec-zstd-sr` 既是 ZSTD 又是 source_reuse），这样「ZSTD 比 default 快还是慢」就没法归因了。拆开之后，任意两档之间的差异只来自一个变量：

| 对比 | 差异来自 |
|---|---|
| `geo-codec-def` ↔ `geo-codec-zstd` | 压缩方式（source_reuse 都关） |
| `geo-codec-def-sr` ↔ `geo-codec-zstd-sr` | 压缩方式（source_reuse 都开） |
| `geo-codec-def` ↔ `geo-codec-def-sr` | source_reuse（压缩都是 default） |
| `geo-codec-zstd` ↔ `geo-codec-zstd-sr` | source_reuse（压缩都是 ZSTD） |

**引擎兼容**（依据 Easysearch 源码 `EngineConfig.INDEX_CODEC_SETTING` 和实测）：

- `index.codec` 的合法值：`default` / `best_compression` / `lucene_default` / ZSTD 系列。ZSTD 系列是 Easysearch 专有，ES 只认 default 和 best_compression。
- `index.source_reuse` 也是 Easysearch 专有，ES 不认这个设置。

所以两边都能跑的是「压缩 default 或 best_compression，且 source_reuse 关」；其余组合只有 Easysearch 能跑。编排台和 runner 都会在开跑前拦下不兼容的组合，不用等建索引那步才炸。

几个实现细节：

- 基础赛道 `$TRACK_GEO` 一点没改。`prepare-codec-track.sh` 只复制一份副本，在副本的 settings 里加一行，幂等。
- **分片数不烘在变体里，靠 track-params 传**。geonames 的 `index.json` 是 Jinja 模板，分片位置写的是 `{{number_of_shards | default(5)}}` —— 不传就会落到模板默认的 **5 分片**。runner 按**基座赛道**统一传 `SHARDS`/`REPLICAS`（默认 3/0），所以 canonical 变体名（`geo-codec-*`）和 legacy 别名（`gdef`/`gzstd`/`gbc`）拿到的是同一份口径。变体目录里看不到分片设置，不代表分片数就是模板默认值。
- 历史短名 `gdef` / `gzstd` / `gbc` 保留为别名（`gzstd` 实际是 `geo-codec-zstd-sr`）。不改名是为了让旧跑分的场次名（`…-gzstd-r1`）语义不漂移。
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
| 赛道生成 | `prepare-hv1024-track.sh` 幂等生成到 `$ROOT/tracks/hv1024-s3` |

和 96 维 `hnsw` 的定位差别：hnsw 测「2M×96 的建图吞吐」，hv1024 测「高维的建图与遍历成本」。跨赛道只看趋势，不看绝对值。8 核环境的做法见 §4.3。

## 14. 控制台（可选）

`bench-compose.py serve` 起一个页面，三块内容合一：

```bash
python3 bench-compose.py serve --ssh root@HOST --es-auth 'admin:xxx'
# --ssh 缺省读环境变量 RACEWAY_SSH，--es-auth 缺省读 RACEWAY_ES_AUTH
# 默认 http://127.0.0.1:8802，起完自动开浏览器（--no-open 关掉）
```

**① 启动排程** —— 勾选矩阵（车 × 赛道 × 圈数），自动剔除指标库里已经跑过的格子，生成排程，dry-run 预览，互斥预检，起跑。生成时会显示预计总耗时；超出 `DS_HOURS` 会当场警告，放不下的场次标 `DEADLINE-SKIP`。勾选状态记在 localStorage。起跑成功后自动跳到进度页。

**② 运行进度** —— 自动选中最近一个没收尾的跑分：进度条、总场次/有效/失败/待跑卡片、剩余分钟数和预计完成时刻、当前场次与阶段、已跑多久。页面标题当状态栏（`▶ 场次 · 阶段`），切走也能瞄。`PROGRESS.txt` 的实时日志直接贴在页面上。可以一键停掉整个跑分，会二次确认，停完自动补结束行。

**③ 历史与结果** —— 所有跑分的清单（一次远端调用取回摘要）+ 关键字筛选。任一场可以「看进度」复看逐场明细，也可以「按此重排」把这场用的车 × 赛道 × 圈数预填进启动页。还能选两场对比（远端跑 `car-compare`：逐圈实测值、中位差值、圈间极差，车代号留空会自动取）。

不开页面的话，五个子命令是同一套能力的程序化入口：

```bash
python3 bench-compose.py cars    --cars-file <f>                   # 列出车定义文件里的车
python3 bench-compose.py gen     --cars-file <f> --tracks ds-*     # 生成排程文件
python3 bench-compose.py preview --cars-file <f> --tracks ds-*     # 预览：跑 runner 自己的 --dry-run
python3 bench-compose.py start   --cars-file <f> --tracks ds-* --rounds 3 --ds-hours 12
python3 bench-compose.py serve   --port 8802                       # Web 控制台
```

`start` 会把排程和车定义传到远端，先做互斥预检（远端已经有在跑的 runner 就拒绝），再 `setsid nohup` 后台起跑。

另外两个只读工具：

- `bench-queue.py` —— 看队列。list / status / plan 三个子命令，把跑分目录里的 `PROGRESS.txt`、`RACES.md`、`STATE.txt`、排程文件推成一张队列视图，并报四类问题：判定矛盾、STATE 行号错位、排程在运行期间被改、窗底漂移。（网页版在上面的 `bench-compose.py serve` 里，不另开一个服务）
- `bench-history.py` —— 看已入库的历史。把指标库 `benchmark-result-race` 读成「车 × 赛道 × 圈数」的覆盖表，回答「哪些格子跑过了、判定是什么、用的哪个 build」。

进度页的推导就是来自 `bench-queue.build_campaign`。

几个使用前提：

- 指标库没起（压测窗口里是常态）时编排照常，只是没法自动剔除已跑过的格子。
- 控制台里的赛道清单是**全部能起跑的赛道**：`ds-*` 维度扫描 7 档、`geo` 加 `gdef/gzstd/gbc` 三个 codec 变体、`hnsw`、`hv1024`、`nested`。每档带一个实测的预计分钟数（来自各次跑分的 `RACES.md`）。`hvtp200k-2s/3s` 只有历史数据，当前 runner 没有对应分支，**能看不能排**。
- 覆盖判定守三条口径（见 `bench-history.py` 顶部注释）：相位判定（拆分协议一场次两行 phase=w/q，两个相位都 VALID 才算跑过；单挑战协议一行 phase=wq）、build 隔离（`jdk25@3009` 跑过不等于 `jdk25@3015` 跑过）、指标库不可用不等于什么都没跑过。

### 14.1 中途停掉的跑分怎么收尾

进度台判「已结束」，只看 `PROGRESS.txt` 里最后一次 `启动:` 之后有没有 `campaign 结束: N/M 场有效` 这一行。

runner 被 TERM/INT/HUP 打断，或者自己 `die` 掉时，会补上这一行（括号里标「中止」，历史页显示「已中止」）。遇到 `kill -9`、被测节点重启这类拦不住的情况，由 `stop-campaign.sh` 停完之后兜底补写，也可以手动跑：

```bash
./close-campaign.sh --dry-run          # 预演：列出所有没收尾的（还活着的 runner 自动跳过）
./close-campaign.sh                    # 全部补结束行，场次数取 RACES.md 的实测值，文件 mtime 不动
./close-campaign.sh ds2-20261021-021151 --reason "宿主机重启"   # 只收尾指定的
```

补的只是一行文本，`RACES.md` 和 `STATE.txt` 都不动，`CAMP_ID=<id>` 的断点续跑照常。

## 15. 踩过的坑

1. **报告文件名必须全场唯一**。rally 的 markdown 报告是追加不覆盖的，重试的场次用 `-tryN` 后缀，免得串写。
2. **ES 冷重启的口径由 ibs wipe 承担**。ES 的 admin 用户存在 data 目录的 `.security` 索引里。`ops.clean wipe` 先停服务再清空 data 和 logs，重启后 `_restore_admin` 用 keystore 的 bootstrap.password 重放一遍 admin 创建（幂等）。这样冷态干净，三台车口径一致。
3. **easysearch 的 wipe 不用特殊处理**。admin 由 `config/security/user.yml` 承载，data 可以整个删，和 ES 走同一条 ibs wipe 命令。
4. **pkill 会匹配到自己**。停进程时用 `[x]` 这种字符类打断字面匹配，避免 pkill 杀掉自己的命令行。
5. **hnsw 的 3 分片变体必须幂等生成，而且绝不能带 `.offset` 断点文件** —— rally 会续传，写入计量就失真了。
6. **段数要用带后缀的接口采**（`_cat/segments` 按 prirep 过滤）。rally 报告里的段数是 `_all` 口径，会把 `.security` 也算进去。
7. **长任务要防 SSH 断连**。整个跑分 `setsid nohup` 后台跑，人只轮询 PROGRESS.txt 文件。
8. **0 副本的时候单节点也是 green**。集群检查必须显式断言 `number_of_nodes=3` 和节点名，不能只看 green。
9. **残留进程会串台**。每场之前要停干净、断言 9200 端口空闲、再用 `GET /` 断言版本，防旧引擎占着端口静默串台。静默卡点还会查 `bench_cpu_avg`：残留引擎的命令行里有 ENGINE_MARK，会被算成 bench 而不是外来进程，只有这一条能抓到它。
10. **监控的归因标记要跟着部署根走**。load-monitor 判断 bench 还是外来进程，靠的是命令行里的 ENGINE_MARK（默认 `/infini`，也就是 ibs 的安装根，run-bench 每场都会精确指到被测引擎目录）。标记错位时 bench 那列恒为 0，引擎的 CPU 被算成外来进程，「外来进程」这个判定就瞎了。**换部署根必须同步改 ENGINE_MARK**。
11. **绑核的 drop-in 是持久生效的**。`cpu-affinity.sh apply 8` 写进 `/etc/systemd/system/{easysearch,elasticsearch}.service.d/zz-cpu-affinity.conf`，跨 ibs 部署、wipe、重启一直都在。**后面要跑全核（32 vCPU）矩阵，必须先 `./cpu-affinity.sh clear`**，否则引擎被静默限制在 8 核，数字全废。`EXPECT_CPU_CORES` 每场都会兜底断言。
12. **写赛道的暖机时间必须按本集群实测的写入时长重新标，不能沿用赛道的原值**。rally 的规矩是：`warmup-time-period` 里的样本全标 `sample-type=warmup`，不进结果。而 bulk 写入按官方文档**不要设 `time-period`** —— rally 会灌完整个语料，暖机之后的每个样本都是测量样本；报告里的 `duration` 是**含暖机**的墙钟时长。官方示例给 bulk 写死 120s 预跑，隐含前提是语料要灌好几分钟。本集群整段写入只有 geo 71~98s、nested 110~135s、hnsw 422~449s，120s 或 70s 这种常量会变成 72%~109%，测量窗口被吃光，`throughput=null` 而返回码还是 0，照样判 VALID（nested 变体曾因此丢了 2/6 场）。**取值规则：按上游比例带取暖机，约等于任务时长的 20%，而且窗口必须 ≥ 50%** —— geo `write-only` 20s、nested-s3 25s（并删掉 time-period:3600）、hnsw 40s。
13. **口径A 和口径B 不一样，两个都要留**。口径A 是 rally 的 `Median Throughput`（区间中位速率），口径B 是语料条数 ÷ `duration`（全程平均）。差别来自写入有没有慢尾，不是窗口长短：nested 窗口只有 1.7~14.4s 时两者差 ±0.8%，hnsw 窗口 382~404s 时口径A 反而系统性高 3.5%（建图慢尾）。`car-compare.py` 两个口径都会输出，还会给出每场的任务秒数。race.json 里的 `segment_count` 和 `store_size` 是 `_all` 全集群统计，而段数检查查的是具体索引，别拿它当 forcemerge 失败的证据。
14. **ES 8.19 出厂的 `config/jvm.options` 里没有 `-Xms/-Xmx`**，堆走 JVM 默认值，也就是物理内存的 1/4，32G 的被测节点上只有 8g，和 ez 那边显式设的 16g 不是一个基线。现在由 ibs 统一治本：`configure_jvm_heap` 遇到没有堆行的配置，就写一份 `config/jvm.options.d/heap.options`（内存的一半 = 16g，幂等覆写）。ES 和 easysearch 两边部署完就是同一个基线，编排侧的 `heap_assert` 也会硬断言每一台车。
15. **比 codec 的落盘体积别用 race.json 的 `store_size`**。race.json 里的 `store_size` 和 `segment_count` 是 rally 落档的 `_all` 全集群口径（把 `.security` 这些非测试索引也算进去了），和「赛末三台节点的 `indices.store` 之和」不是一回事，codec 之间的差异显现不出来。真实体积取赛末 sidecar 的原始探针 `logs/fp/<场次名>-q/nodes-stats.txt`（拆分协议）或 `<场次名>-wq/nodes-stats.txt`（单挑战协议），把三台节点的 `indices.store.size_in_bytes` 加起来。`codec-compare.py` 默认用后者，只有探针缺失时才回退到 race.json。
16. **重试的场次会让「按 车-赛道-圈数 拼出来的场次名」在下游串档**。RACES.md 里同一场可能出现 `x-r1`、`x-r1-try2`、`x-r1-try3` 三行，而 race.json 里**没有 attempt 号**。只有 sidecar 同时握着 `race_id` 和带 `-tryN` 的真实场次名，所以 `bench-export.py` 用 `sidecar_by_race` 做连接键（实测 JDK、堆、状态都对得上）；`codec-compare.py` 则先按 RACES.md 过滤掉 VALID 再统计，否则作废场次的写入数据会混进样本。同类的坑：`env_summary_csv` 早期用 `partition("-")` 切场次名，把 `es819-gbc-r1` 截成了 `es819`，后来改成按 `-<节点名>$` 重建。
17. **对比必须先把赛道钉死，车只在同赛道内可比**。以前那种一个 track 只取到第一辆车的对比图，等于拿 A 车的尾部去和另一个赛道的数比。正确的做法是按 (车, 赛道) 分车画线，每个赛道一张图。跨赛道只能看趋势，不能看绝对值 —— bulk 尺寸、文档大小、建图成本都不一样。
18. **排程输入必须挂在独立的文件描述符上**。排程循环体里的子命令（ssh、ibs、rally、tar）都会继承 stdin，而 `while read … done < <(schedule_lines)` 的 stdin 就是排程管道。任何一个子命令读一下 stdin，后面还没跑的排程行就被吃干净了 —— 实测 24 场只跑了第 1 场。修法是排程从 **FD 3** 读（`exec 3< <(schedule_lines)` 配 `read -u 3`），子命令的 stdin 和排程彻底隔开。
