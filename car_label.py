#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""car_label.py —— 看板图例标签的**唯一**口径来源

看板上一条线 = 一套固定配置：引擎 + 引擎版本 + 引擎 build + JDK。
车名（jdk25 / ez240 / kona25）是跑压时定的代号（服务器侧 CAR_LABEL），
它只说了一半：ez240 与 ez250 看不出引擎版本，jdk25 与 kona25 看不出 JDK 发行版。
图例标签补齐四段，读者不查表就能判断两条线能不能比：

    引擎@版本@build@JDK
    ez@2.4.0@2975@graalvm21      ez@2.5.0@3015@graalvm25
    ez@2.5.0@3015@kona25         es@8.19.23@—@openjdk27

两条取值路径（结果必须一致，已在 55157 条指标上逐条比对为 0 差异）：

  ① sidecar 优先（bench-export.py 用）：实测的 JDK 厂商 + 主版本，
     来自 sidecar 的 java -version 原文（vendor_detected / major_measured）——
     这是**地面真值**，某条车换过 JDK 也不会认错。
  ② 车表兜底（无 sidecar 的早期批次 / 已有载荷补标签时用）：
     CAR_FALLBACK 按车代号给身份。车代号在整个测试期对应同一份包与同一个 JDK，
     兜底值来自实测（见 ENV-perf-metric-store-单机实例.md 环境台账）。

术语：代码里的 car 是"配置代号"（A/B 测试的实验车），不是手车；
面向人的文案一律叫「配置」。
"""

ENGINE_SHORT = {"easysearch": "ez", "elasticsearch": "es"}

# 厂商 → 标签前缀。vendor_detected 由 bench-sidecar.py 从 java -version 原文判定。
JDK_VENDOR_PREFIX = {"GraalVM": "graalvm", "TencentKonaJDK": "kona", "OpenJDK": "openjdk"}

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
# build 号只认**源头**：sidecar 的包名 / version_from_pkg、race.json 的
# user_tags.engine_ver、或 campaign 自己的 PACKAGES.txt。
# 源头没有就是没有 → 显示 "—"（如 ES 8.19.23、ez250 发行包），
# 不从车代号里抠数字猜 —— 车代号是人起的，不是包的事实。
# （oneoff-hvtp200k 那批代号叫 "jdk25-3009" 的场次没有 RACES.md 记账，
#   valid=null，被看板的 valid:true 闸门挡在门外，标签取不到也不影响。）


def jdk_vm_of(side_jdk, jdk_file=None):
    """JDK 身份（厂商, 主版本）—— 取实测证据，不猜。

    side_jdk  : sidecar 的 jdk 段（vendor_detected / major_measured，赛末实测）
    jdk_file  : logs/jdk-<车>.txt 里的 java -version 实测（vendor, major）
    两处都没有就返回 None —— 交给车兜底表。
    """
    for n in ((side_jdk or {}).get("per_node") or []):
        vendor = n.get("vendor_detected")
        major = str(n.get("major_measured") or "").split(".")[0]
        if vendor and major:
            return (vendor, major)
    if jdk_file:
        vendor, major = jdk_file
        if vendor and major:
            return (vendor, str(major).split(".")[0])
    return None


def jdk_short_of(side, car=None, jdk_vm=None):
    """实测 JDK → 标签短名（graalvm25 / kona25 / openjdk26 …）。

    优先实测（sidecar per_node 或 jdk-<车>.txt）；
    没有实测就按车代号兜底；都没有则返回 None（标签里该段留空）。
    """
    vm = jdk_vm or jdk_vm_of((side or {}).get("jdk"))
    if vm:
        prefix = JDK_VENDOR_PREFIX.get(vm[0])
        if prefix:
            return "%s%s" % (prefix, vm[1])
    fb = CAR_FALLBACK.get(car or "")
    return (fb or {}).get("jdk")


def car_label_of(car, engine, version, build_no, side=None, jdk_vm=None):
    """车代号 → 图例标签：引擎@版本@build@JDK。

    engine/version/build 优先用本场实测（sidecar 包名与节点版本、部署日志、
    revision 指纹），缺失时按车代号兜底；实在没有就留空段（不臆造 build 号）。
    """
    fb = CAR_FALLBACK.get(car or "")
    eng = engine or (fb or {}).get("engine") or ""
    ver = str(version) if version not in (None, "") else ((fb or {}).get("version") or "")
    build = str(build_no) if build_no not in (None, "") else BUILD_UNKNOWN
    jdk = jdk_short_of(side, car, jdk_vm=jdk_vm) or ""
    short = ENGINE_SHORT.get(eng, eng)
    return "@".join([short or "?", ver or "?", build, jdk or "?"])


def car_of(doc):
    """文档里的**车身份**（配置代号，如 jdk25 / ez240 / es819）。

    改名兼容 —— 改名前后的文档字段含义不同：
      改名前：``car`` 存的是 Rally 自己的 car 插件列表（["external"]，指发压客户端），
              车身份在 ``arm`` 字段；
      改名后：``car`` = 车身份，Rally 的插件列表挪到 ``rally_car``。
    所以这里先看 car 是不是列表（旧文档），是就当没有，回落到 arm。
    """
    doc = doc or {}
    c = doc.get("car")
    if isinstance(c, (list, tuple)):
        c = None
    return c or doc.get("arm") or ""


def car_label_from_doc(doc):
    """已有载荷里的文档 → 图例标签（无 sidecar 时用这条路径）。

    载荷文档本身带 engine / version / engine_build / car_build，
    其中 car_build（"jdk25@3015"）能补出 engine_build 缺失时的 build 号
    ——环境健康度那批 system 行只有 car/car_build。
    """
    doc = doc or {}
    car = car_of(doc)
    fb = CAR_FALLBACK.get(car) or {}
    eng = doc.get("engine") or fb.get("engine") or ""
    ver = str(doc.get("version")) if doc.get("version") not in (None, "") \
        else (fb.get("version") or "")
    build = str(doc.get("engine_build")) if doc.get("engine_build") not in (None, "") else ""
    if not build:
        ab = doc.get("car_build") or ""
        build = ab.rsplit("@", 1)[-1] if "@" in ab else ""
    build = build or BUILD_UNKNOWN
    jdk = fb.get("jdk") or ""
    short = ENGINE_SHORT.get(eng, eng)
    return "@".join([short or "?", ver or "?", build, jdk or "?"])
