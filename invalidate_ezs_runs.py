#!/usr/bin/env python3
"""invalidate_ezs_runs.py —— 把 hvbbq 批次里 3 场 zstd+source_reuse 的结果作废

2026-11-04：`hv1024x-codec-zstd-sr` 变体的 index.json 把 `index.codec` /
`index.source_reuse` 嵌在 `"index"` 对象里 → 展开后是 `settings.index.index.codec`
→ EasySearch 拒收（unknown setting [index.index.codec]）→ **索引根本建不起来**。
实测这 3 场：create-index 的 error_rate=1.0，随后 knn-search-10-100 /
knn-search-100-1000 全部 error_rate=1.0（recall 探针输出 0 字节），
赛末存储体积 42.7 GB 是坏索引的残留（基座同规模只有 4.7 GB）。

结果文件已归档到 campaign 的 invalid-20261104-zstd-sr/（可恢复）。
本脚本把 RACES.md 里这 3 场的状态从 VALID 改成 INVALID —— 导出器的写入侧闸门
只放 VALID，于是这 3 场不会再入库，同时 manifest 的 dropped 段会留下审计记录。

用法: python3 invalidate_ezs_runs.py <campaign_dir> [--force]

护栏（两道，都可 --force 之外直接退出、不改文件）：
  ① campaign 目录名必须是 hvbbq*（本脚本的 fid 写死为这一批专用）
  ② 这几个 fid 必须真的在这个 campaign 里记账
"""
import os
import sys

FIDS = ("ezs-hv1024x-codec-zstd-sr-r1",
        "ezs-hv1024x-codec-zstd-sr-r2",
        "ezs-hv1024x-codec-zstd-sr-r3")

# 本脚本只服务于 hvbbq 这一批。fid 形态是「车-赛道-轮次」，别的 campaign 里
# 出现同名 fid 是**另一场合法跑分** —— 按 fid 匹配会误作废别人的数据。
# 所以默认按 campaign 目录名前缀锁定批次；要处理别的批次得显式 --force。
EXPECT_CAMP_PREFIX = "hvbbq"

NOTE = """
> **2026-11-04 作废 3 场 zstd+source_reuse**：`hv1024x-codec-zstd-sr` 变体的
> `index.json` 把 `index.codec` / `index.source_reuse` 嵌在 `"index"` 对象里，展开后是
> `settings.index.index.codec` → EasySearch 拒收（`unknown setting [index.index.codec]`），
> **索引根本建不起来**。这 3 场的 create-index error_rate=1.0，随后 knn-search 全部
> error_rate=1.0（recall 探针输出 0 字节），赛末 42.7 GB 是坏索引的残留
> （同规模基座只有 4.7 GB）。配置已修（settings 改平铺）并在虚机 smoke test 通过：
> 同样 3 万条文档，zstd+sr 0.416 GB vs 基座 0.509 GB（0.82×），检索命中正常。
> 结果文件已归档到 `invalid-20261104-zstd-sr/`，不计入有效测量。
"""


def parse_row(line):
    """markdown 表格行 → 单元格列表（非表格行返回空表）"""
    c = [x.strip() for x in line.strip().strip("|").split("|")]
    return c if len(c) >= 2 else []


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv[1:]
    if not args:
        raise SystemExit("用法: python3 invalidate_ezs_runs.py <campaign_dir> [--force]")
    camp = args[0]
    rp = os.path.join(camp, "RACES.md")
    if not os.path.isfile(rp):
        raise SystemExit("找不到 RACES.md: %s" % rp)
    lines = open(rp, encoding="utf-8").read().split("\n")

    # 第一道护栏：按 campaign 目录名锁定批次。fid（车-赛道-轮次）在别的 campaign
    # 里可能是**另一场合法跑分**，按 fid 匹配会误作废别人的数据。
    camp_id = os.path.basename(os.path.normpath(camp))
    if not force and not camp_id.startswith(EXPECT_CAMP_PREFIX):
        raise SystemExit(
            "campaign「%s」不是本脚本对应的批次（期望 %s* ），未做任何修改。\n"
            "本脚本的 fid 写死为 hvbbq 批次专用；确实要处理别的批次，加 --force。"
            % (camp_id, EXPECT_CAMP_PREFIX))

    # 第二道护栏：这些场次确实在本 campaign 里记账 —— 否则是用错了 campaign。
    present = set()
    for line in lines:
        c = parse_row(line)
        if c and c[0] in FIDS:
            present.add(c[0])
    if not present:
        raise SystemExit(
            "该 campaign 没有这几个场次（%s），未做任何修改。" % "、".join(FIDS))

    changed = []
    out = []
    for line in lines:
        c = parse_row(line)
        if len(c) >= 2 and c[0] in FIDS and c[1] == "VALID":
            changed.append(c[0])
            out.append("| %s | INVALID | 配置错误：索引建不起来（settings 嵌套） | 2026-11-04 作废并归档 |"
                       % c[0])
            continue
        out.append(line)
    text = "\n".join(out)
    if "2026-11-04 作废 3 场 zstd+source_reuse" not in text:
        text = text.rstrip("\n") + "\n" + NOTE
    open(rp, "w", encoding="utf-8").write(text)
    print("已作废 %d 场: %s" % (len(changed), "、".join(changed) if changed else "（无新增）"))
    print("结果文件归档在 %s/" % os.path.join(camp, "invalid-20261104-zstd-sr"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
