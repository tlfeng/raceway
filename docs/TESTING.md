# 测试怎么跑

本仓库的测试是**离线**的：不连 SSH、不连 ES、不启真实压测，全部用临时夹具与命令替身。
所以它能在任何一台开发机上跑，也适合放进 CI。

## 一条命令

```bash
python3 -m unittest discover -s tests -v
```

只要 Python 3 标准库，无需 pip 装依赖。

单个文件 / 单个用例：

```bash
python3 -m unittest tests.test_queue -v
python3 -m unittest tests.test_compose_safety.TestRemoteHelper.test_upload_same_content_is_idempotent_conflict_never_overwrites
```

## 宿主环境要求

| 要求 | 为什么 |
|---|---|
| Linux / WSL（真跑 bash 的用例） | 用例会执行 runner 的真实函数片段，并按 `/proc` 判进程身份 |
| **bash ≥ 4** | runner 用了关联数组（`declare -A`）等 bash 4 特性。macOS 自带 bash 3.2，空数组在 `set -u` 下会直接报 `unbound variable` —— 相关用例会自动 skip 并说明原因 |
| Linux 常用工具 | `awk` / `sed` / `grep` / `stat -c` / `ss`；macOS 的 BSD 版本行为不同（例如 `stat -c` 不存在） |
| 可选：`node` | 只有"控制台 JS 语法"那一条用例需要，缺了自动 skip |

macOS 上跑会看到一批 skip（bash 3.2 / `/proc` / BSD 工具），这是预期的，不是测试坏了。

## 用例在守什么

按"改坏了会静默出错"的地方分组：

| 文件 | 守的契约 |
|---|---|
| `test_runner_safety.py` / `test_close_campaign.py` | 停机只杀自己的进程、进程身份校验、campaign 收尾 |
| `test_queue.py` | 状态词表与 runner 文案一致、按基础 fid 去重计场次、连续失败告警、窗底预算 |
| `test_compose_safety.py` | 同源守卫 / CSRF、长操作不占短锁、入库走 stdin 信封（口令不进 argv）、趋势页取数路径 |
| `test_export_contract.py` / `test_apply_safety.py` | 三张索引的字段契约、先预检后发请求、`--drop` 顺序 |
| `test_comparison_safety.py` | 有效尝试筛选、`--assert` fail closed、`--json` 输出纯净 |
| `test_track_settings.py` / `test_track_runtime.py` / `test_hv1024x_family.py` | 赛道定义的存储参数、变体家族、引擎兼容性 |
| `test_dashboard_contract.py` / `test_dashboard_verifier.py` | 建板字段契约、验收不放行空数据 |
| `test_web_api_contract.py` | 控制台前端引用的每个 `/api/*` 在 `bench-compose.py` 路由层存在、被 `tests/mock_console.py` stub、mock 不发明后端没有的接口 |

## 加新用例时的两条规矩

1. **不许连网**。需要远端数据就造夹具（参考 `tests/test_queue.py` 的 `write()` 与 `_camp()`）。
2. **契约变化要同时改测试**。测试是唯一的跨文件闸门（例如 `run-bench.sh` 写进 RACES 的状态词表、
   指标字段白名单）——只改一边会让另一边的断言变成假绿。

## 控制台 UI 离线迭代（mock 控制台）

改 `web/` 前端时不必连压测机：

```bash
python3 tests/mock_console.py          # http://127.0.0.1:8899 （端口可作第一个参数改）
```

stub 全部 `/api/*` + 静态 `web/`。三方契约（`test_web_api_contract.py`）只把守**接口名**，
不把守返回字段 —— mock 的数据形状是手工对齐的，后端改响应字段时 mock 静默过期；
对 mock 开发完要上真机验一遍，漂移多了就把真实响应（脱敏后）固化成夹具。monitor 是
随机游走的活数据方便看曲线交互。只用于开发/演示/截图，**放在 tests/ 是刻意的**——
README 的同步命令拷顶层 `*.py` 与 `web/`，tests/ 不上压测机。

## 已知的环境噪音

| 看到什么 | 原因 |
|---|---|
| `_pos[@]: unbound variable` | 宿主 bash 3.2；用例应已 skip，若报错说明 skip 判据被改坏了 |
| `UnicodeDecodeError` 读子进程输出 | 宿主 locale 非 UTF-8；读到的是脚本里的中文注释 |
| 上传/落盘用例报 `EEXIST` | 个别 Python 构建的 `Path.mkdir(exist_ok=True)` 对已存在目录仍抛错；仓库已改为先判存在 |
