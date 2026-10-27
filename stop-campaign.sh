#!/bin/bash
# 停掉指定 campaign 的运行进程（按 campaign 标签精确定位，不误伤自身）
# 用法: stop-campaign.sh <campaign标签>
set +e
CAMP="${1:?需要 campaign 标签，如 ds2c}"
SELF=$$

echo "=== [1] 停调度器 ==="
for p in $(pgrep -f "run-bench[.]sh"); do
  [ "$p" = "$SELF" ] && continue
  echo "  TERM $p"; kill -TERM "$p" 2>/dev/null
done
sleep 3

echo "=== [2] 停 esrally（含该 campaign 标签的）==="
for p in $(pgrep -f "esrally .*campaign:${CAMP}"); do
  [ "$p" = "$SELF" ] && continue
  echo "  TERM $p"; kill -TERM "$p" 2>/dev/null
done
sleep 4

echo "=== [3] 兜底 KILL ==="
for p in $(pgrep -f "esrally .*campaign:${CAMP}"); do
  [ "$p" = "$SELF" ] && continue
  echo "  KILL $p"; kill -KILL "$p" 2>/dev/null
done
for p in $(pgrep -f "run-bench[.]sh"); do
  [ "$p" = "$SELF" ] && continue
  echo "  KILL $p"; kill -KILL "$p" 2>/dev/null
done
sleep 2

echo "=== [4] 残留检查 ==="
left=$(pgrep -af "run-bench[.]sh|esrally .*campaign:${CAMP}" | grep -v "$$")
if [ -n "$left" ]; then echo "$left"; else echo "  无残留 ✓"; fi
