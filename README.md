# raceway

用 esrally 给 easysearch / elasticsearch / opensearch 集群做性能压测，并负责编排整场跑分。

支持 2~N 个节点，节点清单直接读 ibs 的 `ip.txt`，不用手抄 IP。

> 一次压测在 esrally 里叫一个 race，raceway 就是把这些 race 排成一场场比赛。
>
> 本项目的说法：被测配置叫「车」，压测用例叫「赛道」，第几轮叫「圈」，赛前检查叫「卡点」，一次完整跑分叫「一场 campaign」。

## 特性

- **三种编排方式**：单配置直接跑；多配置写在一个文件里；也可以逐场指定跑什么
- **轮内换位**：多配置对比时，每轮把配置顺序转一位，让「第一场偏冷」这类偏差摊到每个配置上
- **环境卡点**：跑之前自动检查机器是否安静、集群是否就绪、堆和绑核是否符合预期 —— 环境不干净的结果进不了结论
- **实测身份**：引擎版本与 JDK 路径由使用者指定，实际装了什么由脚本逐节点实测后落档
- **部署交给 ibs**：部署、冷启动、检查状态、卸载统一调用 [ibs](https://github.com/tlfeng/ibs) CLI
- **节点清单只有一个来源**：清单、数量、部署范围全部从 ibs `ip.txt` 推导，2~N 节点不用改代码
- **省心之处**：排程预览（`--dry-run`）、全模式断点续跑、赛道清单（`list`）、跑完发通知；计划与截止时间落档，重跑新建尝试，不覆盖已有产物
- **结果入库**：导出三张索引的载荷，完整预检通过后才写入 ES；`--drop` 也先校验后删除

## 分层

| 层 | 做什么 |
|---|---|
| 部署层 | [ibs](https://github.com/tlfeng/ibs) CLI：装集群、起冷机、检查状态、卸载 |
| 方法学层 | 本仓库 `run-bench.sh` 等：卡点 → 冷启动 → 检查集群 → 采样 → 跑一场 → 判定 → 归档 |
| 发压层 | esrally 2.12+，只做 benchmark；发压机与被测集群分开 |

## 快速开始

### 一、四步上手（在压测机上执行）

```bash
cp vm-bench.env.example vm-bench.env && vi vm-bench.env   # 填路径、凭据、阈值（真身不入库）
scp -r *.sh *.py *.conf vm-bench.env tracks/ kibana/ 压测机:/opt/raceway/   # 脚本 + 赛道定义 + 建板工具链
./doctor.sh                                             # 新环境体检：六组只读检查，失败项给出修法
./run-bench.sh --dry-run geo 3 my3.conf                    # 预览排程：秒回，不碰集群
```

**同步必须带上 `tracks/` 和 `kibana/`**，以及全部 `*.py`（对比工具依赖 `result_selection.py`、`identity.py`）。漏同步赛道定义或依赖就跑不起来。runner 从 campaign 的 `runtime/` 只读脚本快照运行，不读取同步时被覆盖的活动脚本。

节点清单从 ibs 的 `ip.txt` 推导，**不用手抄 IP**；还没规划拓扑就先跑 ibs 的一键初始化。

`vm-bench.env` 是硬前置 —— 缺了它连 `./run-bench.sh list` 都跑不动。

压测机上随时可以问这两个命令，**不用翻文档**：

```bash
./run-bench.sh --help    # 完整用法页：参数、三种编排、赛道清单、前置条件、产物在哪
./run-bench.sh list      # 可用赛道 + 每个赛道的定义目录 + 现在能不能跑 + 近期 campaign
```

### 二、我想跑什么 → 敲什么

| 我要跑的 | 命令 | 备注 |
|---|---|---|
| geonames 基座 | `./run-bench.sh geo 3` | 官方基座；语料 rally 自动下载 |
| geonames 存储压缩对比 | `./run-bench.sh codec 3` | 六个变体（压缩 × source_reuse）；ES 只跑 default / best_compression 且关闭 source_reuse 的两档 |
| 1024 维向量 | `EXPECT_CPU_CORES=8 ./run-bench.sh hv1024 3` | 先 `./cpu-affinity.sh apply 8`；语料路径见 `HV_CORPUS` |
| 1024 维四臂（量化/压缩） | `./run-bench.sh hv1024x-int8 3` | `hv1024x` / `-int8` / `-bbq` / `-codec-zstd-sr` 四臂；压缩臂仅 Easysearch |
| OpenSearch 1024 维向量 | `ENGINE_NAME=opensearch ENGINE_VERSION=3.3.2 ./run-bench.sh hv1024x-os 3` | `-os` / `-os-bbq` / `-os-disk`；搜索预算按引擎记录，不将 `ef_search` 等同于 ES `num_candidates` |
| geonames upsert / update | `./run-bench.sh upsert 3` | 自然 ID、bulk_size=500；revision 变化的真实更新，与纯 update 对照 |
| 96 维 200 万向量 | `./run-bench.sh hnsw 3` | 定义已定版 `tracks/hnsw`；语料 `TRACK_HNSW_BASE` |
| nested 文档 | `./run-bench.sh nested 3` | 官方基座 |
| 维度扫描（多档位） | `SCHEDULE_FILE=… CARS_FILE=… DS_HOURS=12 ./run-bench.sh ds` | 定义已定版 `tracks/ds-*`；语料 `CORPORA_DS` |
| 冒烟 / 补跑一场 | `./run-bench.sh geo 1` | 续跑需显式复用 `CAMP_ID` 和相同计划；成功尝试自动跳过，`FRESH=1` 分配新尝试，保留历史 |

每场时长与多配置对比的写法见 [docs/GUIDE.md §1](docs/GUIDE.md)。

### 三、三种编排方式

| 方式 | 命令 | 什么时候用 |
|---|---|---|
| 单配置 | `./run-bench.sh geo 3` | 冒烟、补跑一场；配置走命令行的环境变量 |
| 多配置文件 | `./run-bench.sh geo 3 my3.conf` | 多配置对比（引擎 × JDK） |
| 排程文件 | `SCHEDULE_FILE=… CARS_FILE=… DS_HOURS=12 ./run-bench.sh ds` | 逐场指定「哪台车 × 哪条赛道 × 第几轮」，并给整场设一个时间上限 |

配置文件每行一个配置，能用的键只有 6 个：`ENGINE_NAME` `ENGINE_VERSION` `ENGINE_BUILD` `JDK_DIR` `CAR_LABEL` `JDK_WANT`。

```
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=ez3015
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/kona25 CAR_LABEL=kona25
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
```

> 排程模式的两个文件格式、能调的旋钮、断点续跑规则，见 [docs/GUIDE.md §3](docs/GUIDE.md#3-三种编排方式)。

### 四、跑起来之后

```bash
tail -f campaigns/<campaign>/PROGRESS.txt    # 实时日志：当前场次 + 阶段
cat    campaigns/<campaign>/RACES.md          # 逐尝试判定表（VALID / 各类失败）
cat    campaigns/<campaign>/STATE.txt         # 基础 fid 的 OK / FAIL / SKIP 状态
```

所有模式都以 `PLAN.tsv`（`fid/config/track/round/recall/est` 六列）与 `PLAN.sha256` 锁定实验，`DEADLINE` 保存首次截止 epoch；同一 campaign 续跑不延长窗口。`flock RESULTS_ROOT/.runner.lock` 保证同一结果根目录下只运行一个 runner。每个实际 phase、每个节点都须有 `samples>0` 且 ENV 判定全为 `OK`；缺监控或非 `OK` 判 `ENV-FAIL`，不能进有效结论。

### 五、对比与入库

```bash
# 两个取数池显式分开；同一车代号也能跨 campaign 比较
python3 car-compare.py --a-campaign campaigns/<A> --b-campaign campaigns/<B> --a 标签A --b 标签B
python3 car-compare.py --a-campaign campaigns/<A> --b-campaign campaigns/<B> --a 同车标签 --b 同车标签 --assert campaign-thresholds.example.conf

# 入库链路：跑分产物 → rally race store → 入库载荷 → ES
python3 build-race-store.py --campaigns campaigns-local --out race-store
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns-local
python3 bench-apply.py --dir exports/<tag> --es <url> [--auth user:pass] [--insecure] [--drop]
```

`--a-campaign` / `--b-campaign` 均可重复，每侧内部不能混 build、JDK 或实验条件。对比共用 `result_selection.py` 与 `identity.py`，从 JSON 取数，不解析 Markdown 报告；每个基础场次只取最新有效尝试，失败重试不多算一圈。

`--assert` 的 CSV 格式是 `track,task,metric,>=|<=,value`，其中 track 是实际赛道变体。每条规则要求 B 侧至少 3 个有限有效测量，中位不达标、规则错误或空集合均非零退出。入库先完整预检，缺输入或校验失败不发送请求，`--drop` 只在预检通过后删除所选索引。身份分线与三张索引的口径见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)。

### 六、控制台（可选）

```bash
python3 bench-compose.py serve --ssh root@HOST --es-auth 'admin:xxx'   # 排程 / 进度 / 历史，三页合一
python3 bench-compose.py start --cars-file <f> --tracks ds-* --rounds 3 --ds-hours 12   # 不开页面直接起跑
```

## 文档

完整手册在 `docs/`：

| 文档 | 内容 |
|---|---|
| [docs/GUIDE.md](docs/GUIDE.md) | 使用手册：上篇「怎么跑」，下篇「跑起来之后」（分层、脚本清单、单场流程、赛道、控制台、规则与后果） |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | 方法学与口径：卡点链、统计口径、时延口径、三张索引的字段与词表 |
| [docs/hv1024-track.md](docs/hv1024-track.md) | hv1024 赛道的数据来源与制作方法 |
| [tracks/README.md](tracks/README.md) | 赛道定义定版目录：规矩、目录现状、压测机侧待办 |

## 环境要求

- 压测机：python3、esrally ≥2.12、能免密 ssh 到各被测节点；部署层要用 [ibs](https://github.com/tlfeng/ibs) CLI
- 被测节点（如 32 vCPU / 32G）：root 免密、systemd、能解压安装包
- 节点规划写在 ibs `ip.txt`：`esrally` 那一行是发压机，其余都是被测节点
- 赛道语料：geonames / hnsw2m / hv1024 / nested 任选，放哪见 `vm-bench.env.example`
- 结果入库用的 ES 8.1.3：只在本机汇总用，不装在压测机上

## 仓库布局

```
run-bench.sh                 唯一编排入口（配置×赛道×轮次；计划锁定 / 断点续跑 / runtime 快照）
vm-bench-lib.sh              公共函数库（被 source）
vm-bench.env.example         共享配置模板（复制成 vm-bench.env 填写；真身不入库）
doctor.sh                    新环境体检（六组只读检查）
precheck.sh / load-monitor.sh / cpu-affinity.sh      环境卡点 / 负载采样 / 引擎绑核
tracks/                       赛道定义定版目录（index.json / challenges / operations 全在 git 里）
car-compare.py / car-env-table.py / codec-compare.py  结果对比（共用 result_selection.py / identity.py）
bench-compose.py / bench-queue.py / bench-history.py  控制台与只读观测
build-race-store.py → bench-export.py → bench-apply.py  入库链路
audit_track_settings.py     赛道 settings 结构审计（codec/source_reuse 必须是顶层键；回归闸门见 tests/）
kibana/                      Kibana 7.17 建板工具链（安装器 + 起停 + 建板 + 验收）
```

## 安全

- `vm-bench.env`（含 IP 与凭据）已在 `.gitignore`，永不入库；仓库只提供占位模板
- 脚本本体不写死任何 IP 或密码，全部从 `vm-bench.env` 读

## Credits

- [esrally](https://github.com/elastic/rally)（Elasticsearch，Apache-2.0）—— 发压引擎
- [ibs](https://github.com/tlfeng/ibs) —— 集群部署与生命周期

## License

MIT
