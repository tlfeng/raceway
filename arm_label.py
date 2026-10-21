#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arm_label.py —— 看板图例标签的**唯一**口径来源

看板上一条线 = 一套固定配置：引擎 + 引擎版本 + 引擎 build + JDK。
臂名（jdk25 / ez240 / kona25）是跑压时定的代号（服务器侧 ARM_LABEL），
它只说了一半：ez240 与 ez250 看不出引擎版本，jdk25 与 kona25 看不出 JDK 发行版。
图例标签补齐四段，读者不查表就能判断两条线能不能比：

    引擎@版本@build@JDK
    ez@2.4.0@2975@graalvm21      ez@2.5.0@3015@graalvm25
    ez@2.5.0@3015@kona25         es@8.19.23@—@openjdk27

两条取值路径（结果必须一致，已在 55157 条指标上逐条比对为 0 差异）：

  ① sidecar 优先（bench-export.py 用）：实测的 JDK 厂商 + 主版本，
     来自 sidecar 的 java -version 原文（vendor_detected / major_measured）——
     这是**地面真值**，某条臂换过 JDK 也不会认错。
  ② 臂表兜底（无 sidecar 的早期批次 / 已有载荷补标签时用）：
     ARM_FALLBACK 按臂代号给身份。臂代号在整个测试期对应同一份包与同一个 JDK，
     兜底值来自实测（见 ENV-perf-metric-store-单机实例.md 环境台账）。

术语：代码里的 arm 是"配置代号"（A/B 测试的实验臂），不是手臂；
面向人的文案一律叫「配置」。
"""

import re

ENGINE_SHORT = {"easysearch": "ez", "elasticsearch": "es"}

# 厂商 → 标签前缀。vendor_detected 由 bench-sidecar.py 从 java -version 原文判定。
JDK_VENDOR_PREFIX = {"GraalVM": "graalvm", "TencentKonaJDK": "kona", "OpenJDK": "openjdk"}

# 无 sidecar 的早期批次按臂代号兜底身份（实测值，见 ENV-perf-metric-store §环境台账）。
ARM_FALLBACK = {
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


def build_of_name(arm=None, arm_build=None):
    """臂代号尾部带的构建号（"jdk25-3009" → "3009"）。

    有些臂把 build 号直接写进代号（oneoff 批次），载荷里没有 engine_build，
    但代号本身就是证据 —— 比留"—"准确。只认 4 位数：ez240 / es819 / jdk25
    这类代号尾部是 2~3 位（版本号），不能当构建号。
    """
    for name in (arm_build, arm):
        if not name:
            continue
        m = re.search(r"(\d{4})$", str(name))
        if m:
            return m.group(1)
    return None


def jdk_short_of(side, arm=None):
    """实测 JDK → 标签短名（graalvm25 / kona25 / openjdk26 …）。

    优先 sidecar 的 vendor_detected + major_measured（实测值）；
    没有 sidecar 就按臂代号兜底；都没有则返回 None（标签里该段留空）。
    """
    per = ((side or {}).get("jdk") or {}).get("per_node") or []
    for n in per:
        vendor = n.get("vendor_detected")
        major = str(n.get("major_measured") or "").split(".")[0]
        if vendor and major:
            prefix = JDK_VENDOR_PREFIX.get(vendor)
            if prefix:
                return "%s%s" % (prefix, major)
            break
    fb = ARM_FALLBACK.get(arm or "")
    return (fb or {}).get("jdk")


def arm_label_of(arm, engine, version, build_no, side=None):
    """臂代号 → 图例标签：引擎@版本@build@JDK。

    engine/version/build 优先用本场实测（sidecar 包名与节点版本），
    缺失时按臂代号兜底；实在没有就留空段（不臆造 build 号）。
    """
    fb = ARM_FALLBACK.get(arm or "")
    eng = engine or (fb or {}).get("engine") or ""
    ver = str(version) if version not in (None, "") else ((fb or {}).get("version") or "")
    build = str(build_no) if build_no not in (None, "") else ""
    if not build:
        build = build_of_name(arm) or ""
    build = build or BUILD_UNKNOWN
    jdk = jdk_short_of(side, arm) or ""
    short = ENGINE_SHORT.get(eng, eng)
    return "@".join([short or "?", ver or "?", build, jdk or "?"])


def arm_label_from_doc(doc):
    """已有载荷里的文档 → 图例标签（无 sidecar 时用这条路径）。

    载荷文档本身带 engine / version / engine_build / arm_build，
    其中 arm_build（"jdk25@3015"）能补出 engine_build 缺失时的 build 号
    ——环境健康度那批 system 行只有 arm/arm_build。
    """
    doc = doc or {}
    arm = doc.get("arm") or ""
    fb = ARM_FALLBACK.get(arm) or {}
    eng = doc.get("engine") or fb.get("engine") or ""
    ver = str(doc.get("version")) if doc.get("version") not in (None, "") \
        else (fb.get("version") or "")
    build = str(doc.get("engine_build")) if doc.get("engine_build") not in (None, "") else ""
    if not build:
        ab = doc.get("arm_build") or ""
        build = ab.rsplit("@", 1)[-1] if "@" in ab else ""
    if not build:
        build = build_of_name(arm, doc.get("arm_build")) or ""
    build = build or BUILD_UNKNOWN
    jdk = fb.get("jdk") or ""
    short = ENGINE_SHORT.get(eng, eng)
    return "@".join([short or "?", ver or "?", build, jdk or "?"])
