# raceway

三节点搜索引擎性能编排（easysearch / elasticsearch × esrally）：**配置文件排程、轮内配对交错、门禁链、sidecar 元数据、ES 入库**。一次跑分在 esrally 语境里叫 a race——本工具就是排这些 race 的赛场。

> 命名与生态：配置是车手，轮次是圈速，门禁是赛会检查，campaign 是一场大奖赛。

## 特性

- **配置文件排程**：每行一个被测配置（引擎三元组 + JDK 目录 + 标签），轮内右旋配对交错摊薄时序效应
- **门禁链**：赛前静默负载门禁 → 集群门禁（可达/green/节点数/版本/节点名/**端口属主**）→ 堆断言 → 绑核断言 → 段数门禁 → 指标非空断言；环境脏数据进不了结论
- **无臂机制**：变体用户自管（`ENGINE_NAME/ENGINE_VERSION/ENGINE_BUILD` + `JDK_DIR`），实测身份（逐节点 java -version、索引 settings）由 sidecar 自动落档
- **运维全托管**：部署/冷启动/门禁/卸载统一走 [ibs](https://github.com/tlfeng/ibs) CLI；堆由部署统一设定（内存/2）
- **省心件**：`--dry-run` 排程预览 · 断点续跑（已 VALID 场自动跳过）· `list` 子命令 · 收尾 webhook 通知
- **结果入库**：一键导出 `bench-env / bench-run / bench-metric` 三索引载荷（单位归一、幂等 upsert），Kibana/Grafana 直接建板

## 架构

| 层 | 内容 |
|---|---|
| 部署层 | [ibs](https://github.com/tlfeng/ibs) CLI：三节点集群部署（systemd+TLS+admin 初始化）、冷启动、门禁、卸载 |
| 方法学层 | 本仓库 `run-bench.sh` 等：静默门禁 → 冷启动 → 集群门禁 → 监控 → write race → 段数门禁 → query race → ENV 判定 → 归档 |
| 发压层 | esrally 2.12+，benchmark-only pipeline，驱动器与集群分离 |

## 仓库布局

```
run-bench.sh                 唯一编排入口（配置×赛道×轮次；list/--dry-run/断点续跑）
vm-bench-lib.sh              公共函数库（被 source）
vm-bench.env.example         共享配置模板（复制为 vm-bench.env 填写；真身不入库）
reset-env.sh                 环境还原（停/清/卸载）
precheck-gate.sh             赛前静默负载门禁
load-monitor.sh              虚机侧零依赖逐秒采样器（纯 /proc）
cpu-affinity.sh              引擎绑核（模拟小规格招标环境）
prepare-{codec,hv1024,nested}-track.sh   赛道变体生成（幂等）
arm-compare.py               两配置对比 + --assert 阈值断言（FAIL 退出码 1）
codec-compare.py             codec 五组合臂三件套（写入/赛末体积/查询）
bench-sidecar.py             每场环境/身份元数据 sidecar 生成
bench-export.py / bench-apply.py / bench-report.py   入库载荷 → ES 导入 → 单文件 HTML 报告
campaign-configs.example.conf      配置文件模板
campaign-thresholds.example.conf   断言阈值模板
echarts.min.js               报告内联用（Apache-2.0，见credits）
docs/METHODOLOGY.md          测试方法学与口径
```

## 环境要求

- 驱动宿主机：python3、esrally ≥2.12、ssh 免密到三台被测虚机；部署层依赖 [ibs](https://github.com/tlfeng/ibs) CLI
- 三台被测虚机（如 32 vCPU/32G）：root 免密、systemd、可解压 bundle
- 赛道语料：geonames / hnsw2m / hv1024（jina 1M×1024d）/ nested 任选，放置路径见 `vm-bench.env.example`

## 快速开始

```bash
cp vm-bench.env.example vm-bench.env && vi vm-bench.env   # 填 IP/凭据/路径（真身不入库）
scp *.sh *.py vm-bench.env 驱动宿主机:/opt/raceway/       # 脚本在驱动宿主机上运行

./run-bench.sh list                                       # 看赛道/近期 campaign
./run-bench.sh --dry-run geo 3 my3.conf                   # 排程预览（不执行，秒回）
./run-bench.sh geo 1                                      # 单场冒烟（会真跑 ~26 分钟）
setsid nohup ./run-bench.sh all 3 > run.log 2>&1 &        # 全量（夜间窗口）
./run-bench.sh geo 3 my3.conf                             # 多配置：轮内右旋配对交错
```

多配置文件每行一个配置（模板见 `campaign-configs.example.conf`）：

```
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 ARM_LABEL=ez3015
ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/kona25 ARM_LABEL=kona25
ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ARM_LABEL=es819
```

3 配置 × 3 轮 = 9 场，场序 r1 [A,B,C] / r2 [C,A,B] / r3 [B,C,A]。

## 结果对比与断言

```bash
./arm-compare.py campaigns/<A> campaigns/<B> --a 标签A --b 标签B
./arm-compare.py campaigns/<A> campaigns/<B> --a 基线 --b 候选 --assert campaign-thresholds.example.conf
```

`--assert`：CSV 阈值（`track,task,metric,>=|<=,value`），对象为 B 配置的逐轮中位，任一 FAIL 退出码 1——可直接接 CI 做发版回归。

## 入库

```bash
python3 bench-export.py --out exports/<tag>        # race store + campaign → 三索引载荷
python3 bench-apply.py --dir exports/<tag> --es <url> --auth user:pass --insecure
python3 bench-report.py --export exports/<tag> --echarts echarts.min.js --out 报告.html
```

三索引：`bench-env`（环境台账）/ `bench-run`（一场一条主维度表）/ `bench-metric`(一指标一行长表，单位归一)。

## 安全

- `vm-bench.env`（含 IP/凭据）已在 `.gitignore`，**永不入库**；仓库只提供占位模板
- 脚本本体不硬编码任何 IP/密码——全部经 `vm-bench.env` 注入

## Credits

- [esrally](https://github.com/elastic/rally)（Elasticsearch，Apache-2.0）——发压引擎
- [ECharts](https://github.com/apache/echarts)（Apache-2.0）——报告图表（`echarts.min.js` 内联）
- [ibs](https://github.com/tlfeng/ibs)——集群部署与生命周期

## License

MIT
