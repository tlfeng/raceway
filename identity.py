#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""identity.py —— 被测配置**身份口径**的唯一来源

看板上一条线 = 一套固定配置：引擎 + 引擎版本 + 引擎 build + JDK 厂商 + JDK 版本。
系列键（series_key）补齐四段，读者不查表就能判断两条线能不能比：

    引擎@版本@build@JDK
    ez@2.4.0@2975@graalvm21      ez@2.5.0@3015@graalvm25
    ez@2.5.0@3015@kona25         es@8.19.23@—@openjdk27

身份维度在索引里各占一列（**正交**，互不重复）：

    engine        easysearch / elasticsearch      产品线
    version       2.5.0 / 2.4.0 / 8.19.23         产品版本
    engine_build  3015 / 3009 / 3003 / 2975       引擎包构建号
    jdk_vendor    GraalVM / TencentKonaJDK / OpenJDK   JDK 厂商（实测）
    jdk_version   25.0.4 / 26.0.2.1 / 21.0.9      JDK 完整版本号（实测）
    series_key    引擎@版本@build@JDK              复合分组键（看板分线 + 图例文案）

**为什么 jdk_vendor 必须单独一列**：GraalVM 25 与 TencentKonaJDK 25 的完整版本号
都是 25.0.4（都是 JDK 25.0.4 的不同发行版），只有厂商能区分。而 JDK 厂商对比正是
本项目 JDK 对比方案要回答的问题 —— 不单列就把两条线并成一条，结论直接废掉。

取值路径（只认源头证据，从强到弱）：

  ① sidecar 实测（bench-sidecar.py 落的 java -version 原文 + 节点 JVM 版本）
  ② campaign/logs/jdk-<车>.txt 的 java -version 原文
  ③ JDK 版本登记表 JDK_MEASURED_BY_CAR（车级跨批次实测背书，见下）
  ④ 车代号兜底表（CAR_FALLBACK，只在上面都没有时用）

⛔ 批次清单（PACKAGES.txt）不参与身份推导 —— 它是"这批装了哪些包"，
   不是"这个车跑的哪个包"（旧口径造成过三处真实错标，见 ENV 文档 §7.8）。
⛔ 不知道就是不知道，不猜：查不到来源的写 None（不写值），不拿车代号反推。
⛔ **不设跨场次回填池**：不再从"同车其他场次的 sidecar"借用包/JDK 身份——
   那是 campaign 作为证据池的残余语义，且池随导出范围漂移（同样的场次换个
   导出范围值就变，破坏确定性）。JDK 的跨批次事实改走 ③ 的显式登记表；
   包指纹（pkg_sha256）无本场实测就写 null，绝不借用。

术语：**车（car）= 一套固定的被测配置**（引擎 + 引擎版本 + 引擎 build + JDK + 堆）。
沿用 Rally 的原生词汇 —— Rally 官方定义：a Rally "car" is a specific configuration
of Elasticsearch that is benchmarked（如 "a configuration with a specific heap size"）。
面向人的文案一律叫「车」。

注意与 Rally 自己的 car 区分：race.json 顶层的 "car"（值形如 ["external"]）是 Rally 的
**car plugin**（发压端驱动插件），跟本项目的「车」（被测集群配置）不是一回事。
Rally 只在自己起集群时才应用 car config；我们用 --pipeline=benchmark-only 打自建集群，
Rally 从不管我们的堆和 JDK —— 手工管的那套配置正是 car config 的等价物。
所以本项目的车身份写进 user-tags.car，Rally 的插件列表原样保留在 race.json 顶层
（入库时独立为 rally_car）。
"""

import re

ENGINE_SHORT = {"easysearch": "ez", "elasticsearch": "es"}

# 厂商 → 标签前缀。vendor_detected 由 bench-sidecar.py 从 java -version 原文判定。
JDK_VENDOR_PREFIX = {"GraalVM": "graalvm", "TencentKonaJDK": "kona", "OpenJDK": "openjdk"}

# 标签前缀 → 厂商名（兜底表反解用：CAR_FALLBACK 里存的是 graalvm25 这种短名）
VENDOR_BY_PREFIX = {v: k for k, v in JDK_VENDOR_PREFIX.items()}

# 无 sidecar 的早期批次按车代号兜底身份（实测值，见 ENV-perf-metric-store §环境台账）。
CAR_FALLBACK = {
    "ez240": {"engine": "easysearch", "version": "2.4.0", "jdk": "graalvm21"},
    "ez250": {"engine": "easysearch", "version": "2.5.0", "jdk": "graalvm25"},
    "es819": {"engine": "elasticsearch", "version": "8.19.23", "jdk": "openjdk27"},
    "jdk25": {"engine": "easysearch", "version": "2.5.0", "jdk": "graalvm25"},
    "jdk25-3009": {"engine": "easysearch", "version": "2.5.0", "jdk": "graalvm25"},
    "jdk26": {"engine": "easysearch", "version": "2.5.0", "jdk": "openjdk26"},
    "jdk27": {"engine": "easysearch", "version": "2.5.0", "jdk": "openjdk27"},
    "kona25": {"engine": "easysearch", "version": "2.5.0", "jdk": "kona25"},
}

BUILD_UNKNOWN = "—"     # 无 build 号的配置（ez250 发行包 / ES 8.19）用占位符，段数保持一致
#
# build 号只认**源头**：sidecar 的包名 / version_from_pkg、部署日志
# （ibs-deploy-<车>.log 的 "Deploying Easysearch 2.5.0-3015"）、revision 指纹。
# 源头没有就是没有 → 显示 "—"（如 ES 8.19.23、ez250 发行包），
# 不从车代号里抠数字猜 —— 车代号是人起的，不是包的事实。

# ---------------------------------------------------------------------------
# JDK 版本登记表：车代号 → (厂商, 主版本, 完整版本号)
#
# 这是**补充的车级原始数据**，替代已删除的跨场次回填池：每个条目都有 sidecar
# 的 java -version 实测背书（多个批次的场次一致），且只利用了一条不对称性——
#   **JDK 与车代号绑定，跨部署期不变；引擎 build 才会跨批次更换。**
#   （实测：jdk25 车在 2.5.0-3009 与 2.5.0-3015 两个部署期的 sidecar 实测
#    都是 GraalVM 25.0.4 —— 所以 JDK 版本可以按车登记，包指纹不可以。）
# 引擎版本 / build / 包 sha256 **不进**本表：那些随部署更换，逐场查源头。
# 没有实测背书的车（ez250 / jdk21）不登记 —— 不知道就是不知道。
#
# 实测背书明细（DESIGN §2.5.2 有完整计数）：es819×26、ez240×18、jdk25×62、
# jdk26×6、jdk27×6、kona25×42（本地 402 场 store 口径；服务器全量只多不少）。
JDK_MEASURED_BY_CAR = {
    "es819":      ("OpenJDK", "27", "27"),
    "ez240":      ("GraalVM", "21", "21.0.9"),
    "jdk25":      ("GraalVM", "25", "25.0.4"),
    # jdk25-3009 是 jdk25 车在 3009 部署期的代号，JDK 同 jdk25（25.0.4 跨
    # 3009/3015 实测一致）；本车代号自身无独立 java -version 记录。
    "jdk25-3009": ("GraalVM", "25", "25.0.4"),
    "jdk26":      ("OpenJDK", "26", "26.0.2.1"),
    "jdk27":      ("OpenJDK", "27", "27"),
    "kona25":     ("TencentKonaJDK", "25", "25.0.4"),
}

# java -version 原文里的完整版本号（openjdk version "25.0.4" 2025-01-21）
_VERSION_RE = re.compile(r'(?:java version|openjdk version)\s+"([0-9][0-9.]*)')


def jdk_vm_of(side_jdk, jdk_file=None):
    """JDK 身份 (厂商, 主版本) —— 取实测证据，不猜。

    side_jdk  : sidecar 的 jdk 段（vendor_detected / major_measured，赛末实测）
    jdk_file  : logs/jdk-<车>.txt 的 java -version 实测（vendor, major）
    两处都没有就返回 None —— 交给车兜底表。
    """
    for n in ((side_jdk or {}).get("per_node") or []):
        vendor = n.get("vendor_detected")
        major = str(n.get("major_measured") or "").split(".")[0]
        if vendor and major:
            return (vendor, major)
    if jdk_file:
        if isinstance(jdk_file, dict):
            vendor, major = jdk_file.get("vendor"), jdk_file.get("major")
        else:
            vendor, major = jdk_file
        if vendor and major:
            return (vendor, str(major).split(".")[0])
    return None


def jdk_version_of(side, jdk_file=None, car=None):
    """JDK **完整**版本号（25.0.4 / 26.0.2.1 / 21.0.9 / 27）—— 只认实测。

    ① sidecar 的节点 JVM 版本（jdk.measured_nodes）
    ② sidecar 落的 java -version 原文（jdk.java_version_raw_per_node）
    ③ logs/jdk-<车>.txt 的 java -version 原文
    ④ JDK 版本登记表（JDK_MEASURED_BY_CAR，车级跨批次实测背书）

    查不到就返回 None：车代号推不出完整版本号（jdk25 可能是 25.0.4 也可能是 25.0.5），
    **不知道就是不知道，不猜**。
    """
    sj = (side or {}).get("jdk") or {}
    vals = sorted({str(x) for x in (sj.get("measured_nodes") or []) if x})
    if vals:
        return ",".join(vals)
    for n in (sj.get("java_version_raw_per_node") or []):
        m = _VERSION_RE.search(" ".join(n.get("java_version") or []))
        if m:
            return m.group(1)
    if jdk_file:
        v = jdk_file.get("version") if isinstance(jdk_file, dict) else None
        if v:
            return str(v)
    return jdk_version_fallback(car)


def jdk_version_fallback(car):
    """JDK 版本登记表反查：车代号 → 实测背书的完整版本号（无背书返回 None）。"""
    e = JDK_MEASURED_BY_CAR.get(car or "")
    return e[2] if e else None


def _fallback_jdk_vm(car):
    """车代号兜底表里的 JDK 短名（graalvm25）→ (厂商, 主版本)。"""
    short = (CAR_FALLBACK.get(car or "") or {}).get("jdk") or ""
    for pref, vendor in VENDOR_BY_PREFIX.items():
        if short.startswith(pref):
            return (vendor, short[len(pref):] or None)
    return (None, None)


def jdk_vendor_of(side=None, car=None, jdk_vm=None, jdk_file=None):
    """JDK 厂商（GraalVM / TencentKonaJDK / OpenJDK）—— 实测优先，无实测按车代号兜底。"""
    vm = jdk_vm or jdk_vm_of((side or {}).get("jdk"), jdk_file)
    if vm and vm[0]:
        return vm[0]
    return _fallback_jdk_vm(car)[0]


def jdk_major_of(side=None, car=None, jdk_vm=None, jdk_file=None):
    """JDK 主版本（25 / 26 / 27）—— 实测优先，无实测按车代号兜底。

    只用于 series_key 的 JDK 段（可比性身份粒度）；精确版本看 jdk_version。
    """
    vm = jdk_vm or jdk_vm_of((side or {}).get("jdk"), jdk_file)
    if vm and vm[1]:
        return str(vm[1])
    return _fallback_jdk_vm(car)[1]


def jdk_short_of(jdk_vendor, jdk_major):
    """厂商 + 主版本 → 标签短名（graalvm25 / kona25 / openjdk27）。"""
    prefix = JDK_VENDOR_PREFIX.get(jdk_vendor or "")
    return ("%s%s" % (prefix, jdk_major)) if (prefix and jdk_major) else None


def series_key_of(engine, version, build_no, jdk_vendor=None, jdk_major=None, car=None):
    """看板分线键 + 图例文案：引擎@版本@build@JDK。

    引擎/版本/build 由调用方按源头证据解析好（bench-export.py 的身份推导段），
    这里只负责拼。build 查不到写 "—"（保持段数一致），不臆造。
    JDK 段查不到时：知道车代号写 unk-<车代号>，完全未知才写 "?" ——
    "?" 会让**所有未实测且不在车表里的车**塌成同一个键（同一赛道里两辆车
    混成一条线，结论直接作废）；unk- 前缀明确标出"未实测"，车代号保证不塌线。
    """
    short = ENGINE_SHORT.get(engine or "", engine or "")
    ver = str(version) if version not in (None, "") else "?"
    build = str(build_no) if build_no not in (None, "") else BUILD_UNKNOWN
    jdk = jdk_short_of(jdk_vendor, jdk_major) or ("unk-%s" % car if car else "?")
    return "@".join([short or "?", ver, build, jdk])


def car_of(doc):
    """文档里的**车代号**（配置代号，如 jdk25 / ez240 / es819）。

    索引里不再存顶层 car 字段 —— 车代号只作为 user_tags.car（编排时定的名字）留着，
    身份靠 engine/version/engine_build/jdk_vendor/jdk_version/series_key 表达。
    旧文档（车代号在顶层 car/arm）仍能读出来。
    """
    doc = doc or {}
    c = doc.get("car")
    if isinstance(c, (list, tuple)):
        c = None                       # Rally 自己的 car 插件列表，不是车代号
    if not c:
        c = doc.get("arm")
    if not c:
        c = (doc.get("user_tags") or {}).get("car") or (doc.get("user_tags") or {}).get("arm")
    return c or ""


def series_key_from_doc(doc):
    """已有载荷里的文档 → 系列键（补标签路径用，无 sidecar 时用这条）。

    载荷文档带 engine / version / engine_build，可直接拼；JDK 段从 series_key
    旧值反解，或按车代号兜底表补。
    """
    doc = doc or {}
    eng = doc.get("engine") or ""
    ver = str(doc.get("version")) if doc.get("version") not in (None, "") else ""
    build = str(doc.get("engine_build")) if doc.get("engine_build") not in (None, "") else ""
    vendor = doc.get("jdk_vendor") or None
    major = None
    jv = doc.get("jdk_version")
    if jv:
        major = str(jv).split(".")[0]
    if not vendor or not major:
        fb_vm = _fallback_jdk_vm(car_of(doc))
        vendor = vendor or fb_vm[0]
        major = major or fb_vm[1]
    return series_key_of(eng, ver, build, vendor, major, car=car_of(doc))
