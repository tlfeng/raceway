# raceway

搜索引擎性能编排（easysearch / elasticsearch × esrally，2~N 节点，拓扑取自 ibs ip.txt）：**配置文件排程、轮内配对交错、卡点链、sidecar 元数据、ES 入库**。一次跑分在 esrally 语境里叫 a race——本工具就是排这些 race 的赛场。

> 命名与生态：配置是车，轮次是圈速，卡点是赛会检查，campaign 是一场大奖赛。

## 特性

- **配置文件排程**：每行一个被测配置（引擎三元组 + JDK 目录 + 标签），轮内右旋配对交错摊薄时序效应
- **卡点链**：赛前静默负载卡点 → 集群卡点（可达/green/节点数/版本/节点名/**端口属主**）→ 堆断言 → 绑核断言 → 段数卡点 → 指标非空断言；环境脏数据进不了结论
- **无车机制**：变体用户自管（`ENGINE_NAME/ENGINE_VERSION/ENGINE_BUILD` + `JDK_DIR`），实测身份（逐节点 java -version、索引 settings）由 sidecar 自动落档
- **运维全托管**：部署/冷启动/卡点/卸载统一走 [ibs](https://github.com/tlfeng/ibs) CLI；堆由部署统一设定（内存/2）
- **拓扑单事实源**：节点列表/数量/部署范围全部从 ibs `ip.txt` 派生（集群节点=主机名非 esrally 行），2~N 节点免改代码
- **省心件**：`--dry-run` 排程预览 · 断点续跑（已 VALID 场自动跳过）· `list` 子命令 · 收尾 webhook 通知
- **结果入库**：一键导出 `bench-env / bench-run / bench-metric` 三索引载荷（单位归一、幂等 upsert），Kibana/Grafana 直接建板

## 架构

| 层 | 内容 |
|---|---|
| 部署层 | [ibs](https://github.com/tlfeng/ibs) CLI：三节点集群部署（systemd+TLS+admin 初始化）、冷启动、卡点、卸载 |
| 方法学层 | 本仓库 `run-bench.sh` 等：静默卡点 → 冷启动 → 集群卡点 → 监控 → write race → 段数卡点 → query race → ENV 判定 → 归档 |
| 发压层 | esrally 2.12+，benchmark-only pipeline，驱动器与集群分离 |

## 仓库布局

```
run-bench.sh                 唯一编排入口（配置×赛道×轮次；list/--dry-run/断点续跑）
doctor.sh                    新环境体检（六组只读检查：工具/env/拓扑/连通/语料/指标库）
vm-bench-lib.sh              公共函数库（被 source）
vm-bench.env.example         共享配置模板（复制为 vm-bench.env 填写；真身不入库）
reset-env.sh                 环境还原（停/清/卸载）
precheck.sh                  赛前静默负载卡点
load-monitor.sh              虚机侧零依赖采样器（纯 /proc，5s；按 w/m/q 相位分 tag）
cpu-affinity.sh              引擎绑核（模拟小规格招标环境）
prepare-{codec,hv1024,nested}-track.sh   赛道变体生成（幂等）
car-compare.py               两配置对比 + --assert 阈值断言（FAIL 退出码 1）
car-env-table.py             三车×多赛道"性能×环境"联合表（race.json+sidecar+monitors）
codec-compare.py             codec 五组合车三件套（写入/赛末体积/查询）
bench-sidecar.py             每场环境/身份元数据 sidecar 生成
bench-export.py / bench-apply.py / bench-report.py   入库载荷 → ES 导入 → 单文件 HTML 报告
build-race-store.py          campaign 目录 → rally race store 布局（软链，可重建）
campaign-configs.example.conf      配置文件模板
campaign-thresholds.example.conf   断言阈值模板
echarts.min.js               报告内联用（Apache-2.0，见credits）
kibana/                      Kibana 建板（make_dashboard.py 生成 saved objects：每个 esrally track
                              一个独立看板 + 总览导航页；es-proxy.py 诊断代理；
                              docker-compose.yml 是备用的 7.10 OSS 环境，本机已有 ES/Kibana 时不需要）
```

## 环境要求

- 驱动宿主机：python3、esrally ≥2.12、ssh 免密到三台被测虚机；部署层依赖 [ibs](https://github.com/tlfeng/ibs) CLI
- N 台被测虚机（如 32 vCPU/32G）：root 免密、systemd、可解压 bundle；驱动器与集群节点规划在 ibs `ip.txt`（esrally 行=驱动器，其余=集群节点）
- 赛道语料：geonames / hnsw2m / hv1024（jina 1M×1024d）/ nested 任选，放置路径见 `vm-bench.env.example`
- 结果入库与看板：ES 8.1.3 + Kibana 8.1.3（仅本地汇总用，不在压测机上）

## 入库

把 rally race 结果灌进 ES（三索引）并在 Kibana 建看板：

```bash
python3 build-race-store.py --campaigns campaigns --out race-store   # 软链农场（幂等）
python3 bench-export.py --out exports/<tag> --races race-store --campaigns campaigns
python3 bench-apply.py --dir exports/<tag> --es <url> [--auth user:pass] [--insecure] [--drop]
python3 kibana/make_dashboard.py --import --es <url> --kibana <url>
```

> `bench-export.py` 带**口径契约自检**：stat/unit 词表越界、`_id` 撞号、
> 失败 task 的行与成功 task 混流、closed_loop 任务出现积压等一律**非零退出、
> 载荷不写出**（排查用 `--allow-fail` 放行）。覆盖率与一致性提示写进
> `manifest.json` 的 `assertions` / `coverage` 段。
> 完整口径文档（stat/unit 词表、自检规则、指标语义）属内部资料，未随本公共库公开；
> 规则本身可直接看 `bench-export.py` 顶部的 `CANON_STATS` / `CANON_UNITS`
> 与导出产物 `manifest.json` 的 `assertions` 段。
>
> **stat 归一后 `_id` 会变**（rally 的 `median`→`p50`、`99_99`→`p9999`），
> 旧索引里的同一条会变成两条 —— 改词表后必须 `bench-apply.py --drop` 重建。

三索引：`bench-env`（环境台账）/ `bench-run`（一场一条主维度表）/ `bench-metric`(一指标一行长表，单位归一)。

口径要点：

- **error_rate 按 rally 语义 0~1 原样入库**（`1.0` = 该 task 请求全失败），不按百分比再折算
- 实验变量 `dims` / `lineage` 从 user-tags 提升为 typed 列，可 range/排序
- **bench-metric 带 `track` 列**（归一化赛道名）：按赛道拆看板的前提 —— 指标行没有
  track 就没法按赛道过滤
- **bench-metric 只带一个身份字段 `series_key`**（引擎@版本@build@JDK，如
  `ez@2.5.0@3015@graalvm25`）：run 维度（engine/version/jdk/dims/segment_form/
  env_id/round/phase/车代号…）在指标行上与 bench-run 完全重复，既白付体积又多一份漂移面，
  全部回 bench-run 查（方案 §1 修订）。看板按 `series_key` 分线，TSVB 的 split-by 只吃一个字段；
  按引擎过滤写 `series_key:ez@*`（**不加引号** —— 带引号是精确短语，实测匹配 0 条）
- **身份只认实测**：`jdk_vendor` / `jdk_version` 取 sidecar 的 `java -version` 原文与节点 JVM 版本，
  查不到就空 —— 车代号推不出 patch 号（jdk25 可能是 25.0.4 也可能是 25.0.5），不知道就不猜；
  厂商必须单列（GraalVM 25.0.4 与 TencentKonaJDK 25.0.4 的版本号一模一样，只有厂商能区分）
- **monitors 补的 system 行按场次回填 `@timestamp` / `valid` / `series_key` / `race_id`**：这行是按场次
  采集的摘要，本身没有时间戳也没有身份 —— 不回填的话 CPU steal 这类数据可信度指标
  既画不出折线（x 轴无值），也会被 dashboard 的 `valid: true` 整体过滤掉
- 引擎身份 `engine`（easysearch/elasticsearch）从**包名**推导，不信 sidecar 的 `name` 字段
- 幂等：文档 `_id` 固定（`race_id` / `race_id|task|family|stat`），重复执行是覆盖不是翻倍
- 改了 index template 必须 `--drop` 重建索引，旧 mapping 不会自动更新
- **bench-metric 的索引模板是 `dynamic: strict`**：字段集 = 导出器的白名单，白名单外的字段
  （run 维度回流）在 ES 侧直接拒收 —— 精简靠机制保证，不靠口头约定

看板对照 [opensearch.org/benchmarks](https://opensearch.org/benchmarks/) 的分页口径：
**每个 esrally track 一个独立看板**（`bench-geo` / `bench-hnsw` / `bench-nested` /
`bench-ds-*`），不同赛道的指标不混在一张图里 —— 赛道之间语料、维度、task 集都不同，
量纲不可比。赛道内再按 workload（term 检索 / country 聚合 / sort / KNN k 值…）分图，
组内才是同量纲可比的。总览页 `bench-overview` 是导航入口（环境说明 + 赛道导航表）。

每个赛道看板四段：**① 写入**（吞吐/总耗时/Merge·Refresh·Flush 分解/限流/活动次数）、
**② 查询**（每 workload 的吞吐 + 延迟分位 p50·p90·p99·p999 + 处理时间分位）、
**③ 资源与存储**（存储/段数/GC 耗时与次数）、**④ 环境健康度**（CPU steal / 外来进程 CPU / swap —— 数据可信度，只有带 monitor 采集的赛道有）。

赛道清单不硬编码，从 ES 实算；新增一场新赛道的压测后重跑 `make_dashboard.py` 就自动
多一个看板。入口：左侧栏 ☰ → Analytics → Dashboard（`/app/dashboards/list`）。

面板出问题时用 `kibana/es-proxy.py` 代理 Kibana→ES 看 ES 实际收到的查询（需改 kibana.yml
的 elasticsearch.hosts，排查完记得改回）。

单文件 HTML 报告（不依赖 ES）：

```bash
python3 bench-report.py --export exports/<tag> --echarts echarts.min.js --out 报告.html
```


## 存储压缩 × source_reuse 变体

`index.codec`（存储压缩）与 `index.source_reuse`（_source 复用）是**索引级设置**，
对任何索引都成立。raceway 把它们做成**两个独立维度**，按需派生出赛道变体：

```
变体 id = <基础赛道>-codec-<def|zstd|bc>[-sr]      （与变体目录名一致）
  geo-codec-def      geo-codec-def-sr
  geo-codec-zstd     geo-codec-zstd-sr
  geo-codec-bc       geo-codec-bc-sr
  hnsw-codec-zstd    ds-j1024-codec-bc-sr   …（任意基础赛道都适用）
```

**为什么要正交展开**：早先三个变体把两个维度糅在赛道名里
（`geo-codec-zstd-sr` = ZSTD **且** source_reuse），于是"ZSTD 那档比 default 快/慢"
**无法归因**。正交后任意两档的差异只归因到一个变量：

| 对比 | 隔离出的变量 |
|---|---|
| `geo-codec-def` ↔ `geo-codec-zstd` | 压缩（source_reuse 都关） |
| `geo-codec-def-sr` ↔ `geo-codec-zstd-sr` | 压缩（source_reuse 都开） |
| `geo-codec-def` ↔ `geo-codec-def-sr` | source_reuse（压缩都是 default） |
| `geo-codec-zstd` ↔ `geo-codec-zstd-sr` | source_reuse（压缩都是 ZSTD） |

**引擎兼容**（据 Easysearch 源码 `EngineConfig.INDEX_CODEC_SETTING` 与实测）：
`index.codec` 合法值 = `default` / `best_compression` / `lucene_default` / ZSTD 系列
（ZSTD 系列是 Easysearch 专有；ES 的 `index.codec` 只有 default/best_compression）；
`index.source_reuse` 也是 Easysearch 专有（ES 不认这个设置）。
→ 两侧通用：压缩 default/best_compression 且 sr 关；其余**仅 Easysearch**。
编排台与 runner 都会在起跑前拦下不兼容的组合（别等建索引那步才炸）。

```bash
# 按需生成变体（语料只软链不复制，变体目录 4MB 级）
./prepare-codec-track.sh                      # 默认：geonames 一族 6 个
./prepare-codec-track.sh hnsw-codec-zstd      # 任意基础赛道的变体
```

历史短 id `gdef` / `gzstd` / `gbc` 保留为别名（`gzstd` 实为 `geo-codec-zstd-sr`）——
不改名是为了让旧 campaign 的场次名（`…-gzstd-r1`）语义不漂移。
变体的推导是**规则**不是查表（`vm-bench-lib.sh` 的 `track_*` 与 `bench-history.py`
的同名函数），`tests/test_codec_variants.py` 直接执行 bash 那套与 python 侧逐项比对。


## 控制台：启动 · 进度 · 历史（一个页面搞定）

```bash
python3 bench-compose.py serve --ssh root@HOST --es-auth 'admin:xxx'
#   --ssh 缺省读环境变量 RACEWAY_SSH；--es-auth 缺省读 RACEWAY_ES_AUTH
#   起来后自动开浏览器（--no-open 关掉），默认 http://127.0.0.1:8802
```

`bench-compose.py serve` 是**三合一控制台**（原"排程编排台"扩容）：

| 标签 | 干什么 |
|---|---|
| ① 启动排程 | 矩阵勾选 → 自动剔除指标库已跑过的格 → 生成排程 → dry-run 预览 → 互斥预检后起跑；**生成时显示预计总耗时，超出 DS_HOURS 窗口当场预警**（放不下的场次会被 DEADLINE-SKIP）；勾选记 localStorage；起跑成功自动跳到进度页 |
| ② 运行进度 | 自动选中最新没收尾的 campaign：进度条、总/有效/失败/待跑卡片、剩余分钟与预计完成时刻；**当前场次 + 相位（write/query/recall）+ 已进行时长**；PROGRESS.txt 实时日志尾；标签页标题当状态栏（`▶ 场次 · 相位`，切走也能瞄）；完成时可弹系统通知；进度文件长期无更新会提示"runner 可能已停止"；可一键停止（远端 stop-campaign.sh，二次确认） |
| ③ 历史与结果 | 全部 campaign 清单（一次远端调用取摘要）+ 关键字筛选；任一场「看进度」复看逐场明细；「按此重排」把该场的 车×赛道×轮次 预填进启动排程；**两场对比**（远端跑 car-compare：A 车 vs B 车逐轮实测、中位差值 Δ、轮间极差——车代号留空自动取）；外链 Kibana 看板（Kibana 只听远端 127.0.0.1 时先 `ssh -L 5601:127.0.0.1:5601 …`） |

配套的只读 CLI 仍在：`bench-queue.py`（list / status / plan / html / serve，campaign 队列观测）、
`bench-history.py`（指标库覆盖表）。进度页的推导就来自 `bench-queue.build_campaign`。

历史短排程复用一例：历史页挑一场满意的 campaign →「按此重排」→ 启动页自动勾好
车/赛道/轮次并填上 tag 前缀 → 调整 → 生成排程 → dry-run → 起跑，全程不碰 ssh 命令行。

三个只读/半读的本地工具，把"队列"从散落的文件变成能看能排的东西：

| 工具 | 干什么 |
|---|---|
| `bench-queue.py` | **队列观测台**（纯读）：把 campaign 目录里的 `PROGRESS.txt` / `RACES.md` / `STATE.txt` / 排程文件推导成队列视图，实时页 + 四类告警（判定矛盾 / STATE 行号错位 / 排程运行期被改 / 窗底漂移） |
| `bench-history.py` | **已入库历史覆盖**（纯读）：把指标库 `bench-run` 索引读成「车×赛道×轮次」覆盖表，回答"哪些格已经跑过、什么判定、哪个 build" |
| `bench-compose.py` | **控制台**：① 启动排程（矩阵勾选 → 生成排程 → dry-run 预览 → 互斥预检后起跑，**默认自动剔除指标库里已跑过的格**）② 运行进度（当前场次/相位/耗时、进度条、实时日志、停止）③ 历史与结果（campaign 清单、按此重排、Kibana 外链）——见上节 |

```bash
# 看队列（远端读 campaigns 目录）
python3 bench-queue.py list --root /opt/raceway/campaigns --ssh root@HOST
python3 bench-queue.py serve <campaign> --root /opt/raceway/campaigns --ssh root@HOST   # 实时页

# 控制台（详见上节；口令经 stdin 传给远端 curl，不出现在 argv）
python3 bench-compose.py serve --port 8802 --ssh root@HOST --es-auth 'admin:xxx'
#   指标库没起（压测窗口内是常态）时编排照常，只是无法自动剔除已跑过的格
```

`bench-compose.py` 的赛道清单是**全部可起跑的赛道**（不是只有 ds-*）：
`ds-*` 维度扫描 7 档、`geo` + `gdef/gzstd/gbc` codec 变体、`hnsw`、`hv1024`、`nested`，
每档带实测 est（分钟，来自各 campaign 的 `RACES.md`）。
`hvtp200k-2s/3s` 只有历史数据（当前 runner 无对应分支），**能看不能排**。

覆盖判定守住三条口径（见 `bench-history.py` 顶部注释）：
**一场次两行**（写赛+查赛都 VALID 才算跑过）、**build 隔离**
（`jdk25@3009` 跑过 ≠ `jdk25@3015` 跑过）、**指标库不可用 ≠ 什么都没跑过**。


## 快速开始

```bash
cp vm-bench.env.example vm-bench.env && vi vm-bench.env   # 填 IP/凭据/路径（真身不入库）
scp *.sh *.py vm-bench.env 驱动宿主机:/opt/raceway/       # 脚本在驱动宿主机上运行
# 拓扑在 ibs ip.txt 里规划（如 1 台 esrally 驱动 + N 台集群节点）

./doctor.sh                                               # 新环境体检：六组检查告诉你离能起跑
                                                          # 还差什么（工具/env/拓扑/连通/语料/指标库），
                                                          # 失败项带修法提示；也可在控制台点「🩺 环境体检」

./run-bench.sh list                                       # 看赛道/近期 campaign
./run-bench.sh --dry-run geo 3 my3.conf                   # 排程预览（不执行，秒回）
./run-bench.sh geo 1                                      # 单场冒烟（会真跑 ~26 分钟）
setsid nohup ./run-bench.sh all 3 > run.log 2>&1 &        # 全量（夜间窗口）
./run-bench.sh geo 3 my3.conf                             # 多配置：轮内右旋配对交错
```

新环境最小上手路径：装 python3 + esrally(≥2.12) + ibs → 配好到虚机的 root 免密 →
`cp vm-bench.env.example vm-bench.env` 按注释填写（拓扑从 ibs ip.txt 派生，不用手抄 IP）→
`./doctor.sh` 把 ✘ 修完 → `./run-bench.sh geo 1` 冒烟。卡点阈值默认值即开即用；
要调只改一处：vm-bench.env 的 `VERDICT_STEAL_MAX` / `VERDICT_FOREIGN_MAX`
（赛后 CONTAMINATED 判定与赛前静默卡点同口径继承，赛前专属项另有 `QUIET_LOAD1` 等）。

多配置文件每行一个配置（模板见 `campaign-configs.example.conf`）：

```
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=ez3015
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/kona25 CAR_LABEL=kona25
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
```

3 配置 × 3 轮 = 9 场，场序 r1 [A,B,C] / r2 [C,A,B] / r3 [B,C,A]。

## 结果对比与断言

```bash
./car-compare.py campaigns/<A> campaigns/<B> --a 标签A --b 标签B
./car-compare.py campaigns/<A> campaigns/<B> --a 基线 --b 候选 --assert campaign-thresholds.example.conf
```

`--assert`：CSV 阈值（`track,task,metric,>=|<=,value`），对象为 B 配置的逐轮中位，任一 FAIL 退出码 1——可直接接 CI 做发版回归。

## 安全

- `vm-bench.env`（含 IP/凭据）已在 `.gitignore`，**永不入库**；仓库只提供占位模板
- 脚本本体不硬编码任何 IP/密码——全部经 `vm-bench.env` 注入

## Credits

- [esrally](https://github.com/elastic/rally)（Elasticsearch，Apache-2.0）——发压引擎
- [ECharts](https://github.com/apache/echarts)（Apache-2.0）——报告图表（`echarts.min.js` 内联）
- [ibs](https://github.com/tlfeng/ibs)——集群部署与生命周期

## License

MIT
