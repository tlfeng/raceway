#!/usr/bin/env bash
# ============================================================================
# prepare-dimsweep-jdk.sh —— 把 OpenJDK 26/27 装到三台虚机（幂等）
#
# 背景：JDK 26/27 臂的 JDK 属于"用户自管变体"——框架只负责换装（ensure_jdk）与
# 身份断言（jdk_gate），装在哪、什么版本由这里决定。ds3 首轮跑完后虚机上已无
# jdk-26/27 目录（campaign 收尾清理掉了），复测前必须重装。
#
# 装到 /opt/jdks/jdk-<ver>（虚机本地路径，ensure_jdk 的 JDK_DIR 指向它）。
# 幂等：已装且主版本对得上就跳过。
#
# 用法：./prepare-dimsweep-jdk.sh [26] [27]
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

VERS=("$@")
[ ${#VERS[@]} -eq 0 ] && VERS=(26 27)

for ver in "${VERS[@]}"; do
  pkg="$PKG_DIR/openjdk-${ver}_linux-x64_bin.tar.gz"
  [ -s "$pkg" ] || { echo "[prepare-dimsweep-jdk] ERROR: 缺安装包: $pkg" >&2; exit 1; }
  echo "[prepare-dimsweep-jdk] openjdk-$ver 包: $pkg ($(stat -c%s "$pkg") B)"
  for ip in "${VM_IPS[@]}"; do
    if vm "$ip" "[ -x /opt/jdks/jdk-$ver/bin/java ] && /opt/jdks/jdk-$ver/bin/java -version 2>&1 | head -1 | grep -q \"\\\"$ver\"" 2>/dev/null; then
      echo "  $ip: jdk-$ver 已装且版本正确，跳过"
      continue
    fi
    echo "  $ip: 推送 openjdk-$ver（约 $(($(stat -c%s "$pkg")/1048576)) MB）..."
    vmPut "$ip" "$pkg" "/tmp/openjdk-$ver.tar.gz" >/dev/null || { echo "  $ip: 推送失败" >&2; exit 1; }
    vm "$ip" "set -e
      rm -rf /opt/jdks/jdk-$ver.tmp
      mkdir -p /opt/jdks/jdk-$ver.tmp
      tar xzf /tmp/openjdk-$ver.tar.gz -C /opt/jdks/jdk-$ver.tmp --strip-components=1
      rm -rf /opt/jdks/jdk-$ver
      mv /opt/jdks/jdk-$ver.tmp /opt/jdks/jdk-$ver
      rm -f /tmp/openjdk-$ver.tar.gz
      /opt/jdks/jdk-$ver/bin/java -version 2>&1 | head -1" || { echo "  $ip: 解压失败" >&2; exit 1; }
    echo "  $ip: jdk-$ver 就绪"
  done
done
echo "[prepare-dimsweep-jdk] 完成"
