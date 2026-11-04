# hv1024 赛道：1024 维向量数据集与制作记录

> 记录 hv1024 赛道（定义定版在 `tracks/hv1024/`）的数据集来源与制作方法。数据集血统为 **esrally 官方赛道 `msmarco-jina-10m-vector`**（MS MARCO passage 语料 × jina-embeddings-v3，1024 维 float32）的 100 万条子集；赛道本身为自定义测试程序。赛道参数与口径速览见 [GUIDE §13.2](GUIDE.md)。

## 一、数据集来源

- **官方赛道**：`msmarco-jina-10m-vector`（elastic/rally-tracks），语料由 `https://rally-tracks.elastic.co` 分发，共 4 个分片文件 `jina-documents-01..04.json.zst`（各 2.5M 条，解压 56.8GB/个）。
- **官方行格式**：`{"docid":"...","emb":[1024 个 float]}`——官方本就是 `docid`+`emb` 两字段，**无需任何字段映射**。
- **查询集**：官方 `queries.json.bz2`（67,961,127 字节，10,000 条 × 1024 维）。

## 二、语料制作链（`<语料制作目录>/hv-bench/`）

| 步骤 | 脚本 | 做了什么 |
|---|---|---|
| 1. 前缀下载 | `hv-prefix.sh` | 从官方 CDN 下载 `jina-documents-01.json.zst` 的**前 8,160,437,862 字节**（完整文件 17,370,657,342 字节；6 并发 Range 分片 + 断点续传；zstd 流前缀足够解码前 100 万条，截断致尾部 premature end 属预期）。前缀文件 sha256 已记录于 `corpora/CHECKSUMS.txt` |
| 2. 解码+截取 | `hv-prep.sh jina` → `hv-transcode.py` | `zstd -dc` 流式解码管道到 python，jina 模式**原样透传**官方行，取前 **1,000,000** 条 → `corpora/documents-jina-float.json`（22,739,096,214 字节） |
| 3. 回填校验 | `hv-backfill.py` | 将实际行数/字节数回填 track.json；校验 `emb` 维度=1024 |

产物：`corpora/documents-jina-float.json`（1,000,000 行，每行 `{"docid","emb":[1024 floats]}`，~22.7KB/行）。

## 三、赛道构造（`tracks/hv1024/`，本仓库定版）

赛道定义定版在 `tracks/hv1024/`（index.json / track.json / operations / challenges / track.py 全在 git 里）。起跑前由 run-bench 补两样 git 收不了的东西：语料软链 `documents-1024.json`、查询集 `queries.json`（官方 bz2 前 2000 条），并自检语料字节数与语料/查询向量维度。语料与查询集的来源路径由 `vm-bench.env` 的 `HV_CORPUS` / `HV_QUERIES_BZ2` 指定：

| 组件 | 内容 | 与官方差异 |
|---|---|---|
| 语料 | `documents-1024.json` → 符号链接至上述 1M 文件 | 子集化（10M → 1M） |
| index.json | `vectors`，**3 主分片** 0 副本，`_source` 关闭，`dynamic:false`；`docid` keyword + `emb` dense_vector（float/1024/dot_product/HNSW m=32/ef_construction=100） | 官方为 10M 条不同分片布局 |
| operations | `knn-search-10-100`（k=10/ef=100）、`knn-search-100-1000`（k=100/ef=1000），param-source 从 `queries.json` 轮询 | 同官方家族模板（`dense_vector`/`so_vector` 同款骨架） |
| challenges | `write-only`：bulk 500×8 客户端 → force-merge 每分片 1 段；`query-only`：两档各 warmup 100 / 1000 迭代，单客户端 | 读写分离 + 单段卡点为自定义（官方为混合挑战） |
| queries.json | 官方 `queries.json.bz2` 解压后**前 2,000 条** | 条数与 96 维 s3 赛道对齐 |

## 四、20 万条派生赛道

- 语料：`corpora/documents-hvfloat-sub200000.json`（200,000 行，4,548,045,134 字节，同链切取的前 20 万条）。
- 派生赛道 `tracks/hvtp200k-2s`（2 分片）与 `hvtp200k-3s`（3 分片，最终采用），query-only 含单客户端与 32 客户端稳态（time-period 90s）两任务，用于 32 并发吞吐与分片布局对比。结论：2 分片使 3 节点 1/3 算力闲置，最终采用 3 主分片。

## 五、复现要点

1. 下载/转码脚本在 `<语料制作目录>/hv-bench/`；赛道定义在本仓库 `tracks/hv1024/`（语料软链与 queries.json 由 run-bench 起跑前生成）。
2. 语料文件**勿删勿改**：`documents-jina-float.json` 与 `documents-hvfloat-sub200000.json` 被正式赛道以符号链接引用；重新生成只需重跑上表 1→2（需外网可达官方 CDN）。
3. 本数据集与官方赛道的关系口径：「官方 esrally 赛道 msmarco-jina-10m-vector 公开语料的 100 万条子集，测试程序由本规范定义」。
