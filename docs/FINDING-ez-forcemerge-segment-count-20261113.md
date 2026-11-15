# FINDING · easysearch force-merge 后「每分片 3-4 段」问题调查结论（2026-11-13）

> 起因：jdk3-20261111 / jdk3c-20261112 两个 campaign 里，track 显式 `max_num_segments=1`，
> rally 报告却显示 EZ 11 段 vs ES 3 段，疑似「EZ 不合并到 1 段」。
> 调查工具：/tmp/segprobe.py（本机留存副本：raceway docs/ 同目录不再复制，脚本核心=建索引→5万docs→blocking FM→数段→2000 次 knn→每 500 次数段）。
> 本地完整分析见工作区 REPORT-ez3000-20261112-vs-es956-hv1024x-20261113.md §六。

## 结论（一句话）

**easysearch 的 force-merge 是好的，vectors 真实收敛到 1 段/分片；「11 段」是 rally 统计口径污染——
EZ 的 `.security` 系统索引把 8 个文档写成 8 个 segment 且永不合并，被 rally 赛末采的
`_all.primaries.segments.count` 一并计入。**

## 证据链

1. **race 内闸门诚实通过**：track 的 full challenge 在 FM 后有两个收敛闸门——
   `wait-until-merges-finish`（merges.current==0）与 `wait-for-single-segment`
   （index=vectors，`_all.total.segments.count`==3，retry-until-success=true **无限重试**，见
   rally runner.py Retry 类：retry-until-success=true 时 retries 参数被忽略、直到条件满足）。
   18 场全 VALID → FM 后 vectors 确实是 3 总段（1/分片），查询跑在单段上。
2. **口径污染实锤**：rally 在 on_benchmark_stop 采的是 `_all.primaries.segments.count`（`_all` = 所有索引，
   不止 vectors）。EZ 3.0.0 的 `.security` 索引 8 docs/24 段（每副本 8 段=每文档一段，compound=true，
   永不合并；1P+2R 共 24 段），主分片贡献 8 段 → vectors(3) + .security(8) = 11。
   ES 9.5.6 系统索引紧凑（合并过、无碎片）→ 干净的 3。
   sidecar 的 node-stats segments.count（EZ 8/8/9 vs ES 3/4/6）同样是「vectors+系统索引」的每节点混合口径。
3. **live probe 复核**（3 节点、50k docs、3 分片 0 副本、hnsw fp32）：
   - blocking `_forcemerge?max_num_segments=1` 返回时（4.2s）vectors 还有 **6 段/分片**（API 先返回、merge 后台继续）；
   - ~几十秒后收敛 **1 段/分片**，store 289.5MB → 198.9MB（向量文件真实融合）；
   - 2000 次 knn 查询全程段数稳定 1 段/分片——**查询不产生段**。
4. EZ 源码核对（~/gitea/easysearch）：RestForceMergeAction / TransportForceMergeAction 为标准 ES 实现，
   maxNumSegments 逐层透传，无 knn 特殊分支。

## 影响与处置

- **报告修正**：REPORT-ez3000-20261112-vs-es956-hv1024x-20261113.md §六 的段数行已改写；
  REPORT-…-20261111.md 的「EZ 留 3~4 段/分片」结论同样作废（口径污染，非引擎行为）。
- **指标库（已治本，2026-11-13 实装）**：`segment_count`（family=disk）改成「open 且索引名不以 `.`
  开头的业务索引 primaries 段数」。数据源是 sidecar 增采的两路原始探针
  （`logs/fp/<fid>-<phase>/indices-segments.txt` = `_all/_stats/segments`、
  `indices-list.txt` = `_cat/indices?h=index,status`），由 `bench-sidecar.segments_open_nondot()`
  重算并写进 sidecar 的 `segment_stats`；导出端 `emit_metrics` 用它替换 rally 的
  `_all.primaries` 口径，rally 原值另存 `segment_count_rally_all` —— 两行差值就是被排除的
  系统索引段数，可审计。
  探针缺席（老 campaign）时回退旧口径，并在 race 索引的 `segments_caliber_post_merge` 标注
  `node_stats_all`；有探针时为 `open_nondot`。混口径比数之前先看这个字段。
- **顺带的新发现（比段数更重要）**：probe 在 1 段/分片的干净状态下 2000 次 knn 出现
  **p99 416~619ms 的尾部（~1% 请求 >600ms）**，与 jdk3c campaign 里 fp32 knn-10 三轮恒定 ~594ms 同款——
  **EZ 3.0.0 knn 查询自身存在 ~600ms 级尾部毛刺，与段数/合并/负载无关**（bbq 赛道本轮却无此尾部），
  建议反馈 INFINI 排查（历史上 10-15 起已见同款毛刺，当时嫌疑 https+security）。
- probe 还发现两个 EZ 行为差异（不影响 benchmark，记录备查）：
  `_bulk?refresh=false` 会 400（"index is missing"，ES 允许该参数）；
  `DELETE /_all` 被 security 禁（403，合理）。

## 后续动作状态（2026-11-13 07:36）

- **jdk3d-20261113-073636 已点火**：EZ 3.0.0-20261112 单车重跑，3 赛道×3 轮=9 场（排程
  schedules/hv3k-ez-only.sched，bbqdisk 先行），验证项：①干净 EZ 数字 ②fp32 ~594ms 尾部是否复现
  ③vm_retry 补丁下 monitor 0 失败。预计 ~2.5h 跑完。
- ⚠️ 若 185 中途关机/虚机重启导致 campaign 死掉：断点续跑命令（RACES.md 已 VALID 的场自动跳过）：
  ```
  cd /testing/github/raceway && set -a && source ./vm-bench.env; set +a
  SCHEDULE_FILE=/testing/github/raceway/schedules/hv3k-ez-only.sched \
  CARS_FILE=/testing/github/raceway/schedules/cars-ez3000-es956-20261112.conf \
  DS_HOURS=5 CAMP_ID=jdk3d-20261113-073636 setsid nohup ./run-bench.sh all \
    >> campaigns/launch-jdk3d-20261113.log 2>&1 < /dev/null &
  ```
- 调查期间探针遗留：节点上 vectors 索引（5 万 docs）已由 jdk3d 第 1 场的 delete-index 清掉；
  /tmp/segprobe.py 可删可留。
