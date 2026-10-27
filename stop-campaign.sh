#!/bin/bash
# 停掉指定 campaign 的运行进程（按 campaign 标签精确定位，不误伤自身）
# 用法: stop-campaign.sh <campaign标签>
set +e
CAMP="${1:?需要 campaign 标签，如 ds2c}"
SELF=$$
HERE="$(cd "$(dirname "$0")" && pwd)"

# runner = 有 shell 进程在执行 run-bench.sh。pgrep -f 匹配整条命令行，vim/tail/less/ssh
# 命令行里带这个文件名的也会命中 —— 按 comm 过滤只留 shell，别把人家的编辑器杀了。
runner_pids() {
  local p
  for p in $(pgrep -f 'run-bench[.]sh' 2>/dev/null); do
    [ "$p" = "$SELF" ] && continue
    case "$(ps -p "$p" -o comm= 2>/dev/null)" in
      bash|sh|dash|zsh|*/bash|*/sh|*/dash|*/zsh) echo "$p";;
    esac
  done
}
rally_pids() { pgrep -f "esrally .*campaign:${CAMP}" 2>/dev/null | grep -vx "$SELF"; }

echo "=== [1] 停调度器 ==="
for p in $(runner_pids); do echo "  TERM $p"; kill -TERM "$p" 2>/dev/null; done
sleep 3

echo "=== [2] 停 esrally（含该 campaign 标签的）==="
for p in $(rally_pids); do echo "  TERM $p"; kill -TERM "$p" 2>/dev/null; done
sleep 4

echo "=== [3] 兜底 KILL ==="
for p in $(rally_pids); do echo "  KILL $p"; kill -KILL "$p" 2>/dev/null; done
for p in $(runner_pids); do echo "  KILL $p"; kill -KILL "$p" 2>/dev/null; done
sleep 2

echo "=== [4] 残留检查 ==="
left=""
for p in $(runner_pids) $(rally_pids); do left="$left$(ps -p "$p" -o pid=,command= 2>/dev/null)
"; done
left=$(printf '%s' "$left" | sed '/^[[:space:]]*$/d')
if [ -n "$left" ]; then echo "$left"; else echo "  无残留 ✓"; fi

# 被 KILL 的 runner 写不了"campaign 结束"行（TERM 能由 run-bench.sh 的 trap 自己补），
# 进度台会一直显示运行中 —— 停干净后兜底补写。参数既可能是全 id（控制台传的），
# 也可能是前缀标签（手敲 ds2c）：有目录按 id，否则按前缀扫。
echo "=== [5] 补结束行 ==="
CLOSE="$HERE/close-campaign.sh"
if [ -n "$left" ]; then
  echo "  有残留进程，不补结束行；清干净后手动: $CLOSE ${CAMP}"
elif [ ! -x "$CLOSE" ]; then
  echo "  缺 $CLOSE，跳过"
else
  ROOT=$("$CLOSE" --print-root)
  if [ -f "$ROOT/$CAMP/PROGRESS.txt" ]; then
    "$CLOSE" --reason "stop-campaign.sh 停止" "$CAMP"
  else
    "$CLOSE" --reason "stop-campaign.sh 停止" --prefix "${CAMP}-"
  fi
fi
