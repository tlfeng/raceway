# kibana — 本地建板环境（可选）

入库链路 `bench-export.py → bench-apply.py` 的配套：本地单机 ES+Kibana（7.10.2 OSS，无认证，Apple Silicon 走 Rosetta），导入载荷后用 TSVB 面板看 bench-env / bench-run / bench-metric 三索引。

```bash
docker compose up -d                       # 起 ES :9200 + Kibana :5601（down -v 清数据）
python3 bench-apply.py --dir exports/<tag> --es http://localhost:9200 --insecure
python3 make_dashboard.py --import         # 生成并导入 TSVB saved objects（overwrite 幂等）
# 打开 http://localhost:5601 → Dashboard
```

- `make_dashboard.py`：生成 `kibana-objects.ndjson`（每指标族一图，series=臂，x=场次时间；顶部 Markdown 环境台账）。仅 python3 标准库，面板过滤用 Lucene 语法
- 索引 schema 变化后（如新增 phase 维度）重跑 `make_dashboard.py` 刷新面板过滤条件
