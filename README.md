# raceway

用 esrally 给 easysearch / elasticsearch 集群做性能压测，并负责编排整场跑分。

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
- **省心之处**：排程预览（`--dry-run`）、断点续跑、赛道清单（`list`）、跑完发通知
- **结果入库**：一键导出三张 Elasticsearch 索引的载荷

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
scp *.sh *.py *.conf vm-bench.env 压测机:/opt/raceway/      # 脚本在压测机上跑
./doctor.sh                                                # 新环境体检：六组只读检查，✘ 的项给出修法
./run-bench.sh --dry-run geo 3 my3.conf                    # 预览排程：秒回，不碰集群
```

节点清单从 ibs 的 `ip.txt` 推导，**不用手抄 IP**；还没规划拓扑就先跑 ibs 的一键初始化。

`vm-bench.env` 是硬前置 —— 缺了它连 `./run-bench.sh list` 都跑不动。

### 二、三种编排方式

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

### 三、跑起来之后

```bash
tail -f campaigns/<campaign>/PROGRESS.txt    # 实时日志：当前场次 + 阶段
cat    campaigns/<campaign>/RACES.md          # 逐场判定表（VALID / 各类失败）
```

### 四、对比与入库

```bash
# 两个配置对比；--assert 的阈值表是 CSV：track,task,metric,>=|<=,value
python3 car-compare.py campaigns/<A> campaigns/<B> --a 标签A --b 标签B
python3 car-compare.py campaigns/<A> campaigns/<B> --a 基线 --b 候选 --assert campaign-thresholds.example.conf

# 入库链路：跑分产物 → rally race store → 入库载荷 → ES
python3 build-race-store.py --campaigns campaigns-local --out race-store
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns-local
python3 bench-apply.py --dir exports/<tag> --es <url> [--auth user:pass] [--insecure] [--drop]
```

`--assert` 只要有一条不达标就以退出码 1 结束，可以直接接进 CI 做发版回归。三张索引的口径见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)。

### 五、控制台（可选）

```bash
python3 bench-compose.py serve --ssh root@HOST --es-auth 'admin:xxx'   # 排程 / 进度 / 历史，三页合一
python3 bench-compose.py start --cars-file <f> --tracks ds-* --rounds 3 --ds-hours 12   # 不开页面直接起跑
```

## 文档

完整手册在 `docs/`：

| 文档 | 内容 |
|---|---|
| [docs/GUIDE.md](docs/GUIDE.md) | 使用手册：上篇「怎么跑」，下篇「跑起来之后」（分层、脚本清单、单场流程、赛道、控制台、踩过的坑） |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | 方法学与口径：卡点链、统计口径、时延口径、三张索引的字段与词表 |
| [docs/hv1024-track.md](docs/hv1024-track.md) | hv1024 赛道的数据来源与制作方法 |

## 环境要求

- 压测机：python3、esrally ≥2.12、能免密 ssh 到各被测节点；部署层要用 [ibs](https://github.com/tlfeng/ibs) CLI
- 被测节点（如 32 vCPU / 32G）：root 免密、systemd、能解压安装包
- 节点规划写在 ibs `ip.txt`：`esrally` 那一行是发压机，其余都是被测节点
- 赛道语料：geonames / hnsw2m / hv1024 / nested 任选，放哪见 `vm-bench.env.example`
- 结果入库用的 ES 8.1.3：只在本机汇总用，不装在压测机上

## 仓库布局

```
run-bench.sh                 唯一编排入口（配置×赛道×轮次；list / --dry-run / 断点续跑）
vm-bench-lib.sh              公共函数库（被 source）
vm-bench.env.example         共享配置模板（复制成 vm-bench.env 填写；真身不入库）
doctor.sh                    新环境体检（六组只读检查）
precheck.sh / load-monitor.sh / cpu-affinity.sh      环境卡点 / 负载采样 / 引擎绑核
prepare-*-track.sh           赛道变体生成（幂等）
car-compare.py / car-env-table.py / codec-compare.py  结果对比
bench-compose.py / bench-queue.py / bench-history.py  控制台与只读观测
build-race-store.py → bench-export.py → bench-apply.py  入库链路
```

## 安全

- `vm-bench.env`（含 IP 与凭据）已在 `.gitignore`，永不入库；仓库只提供占位模板
- 脚本本体不写死任何 IP 或密码，全部从 `vm-bench.env` 读

## Credits

- [esrally](https://github.com/elastic/rally)（Elasticsearch，Apache-2.0）—— 发压引擎
- [ibs](https://github.com/tlfeng/ibs) —— 集群部署与生命周期

## License

MIT
