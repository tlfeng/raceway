#!/usr/bin/env bash
# 停止一个完整 campaign id；只认本次 runner 的 PID/starttime/boot/script 身份。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REQUESTED_ROOT="${RESULTS_ROOT:-}"   # 环境变量优先于 vm-bench.env（与 close-campaign.sh 同一契约）；
                                     # lib 会 source vm-bench.env，先取调用方的显式 env 防被覆盖
RACEWAY_ENV_OPTIONAL=1
source "$HERE/vm-bench-lib.sh" || exit 1
CAMP="${1:?用法: stop-campaign.sh <完整 campaign-id>}"
[[ "$CAMP" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "campaign id 非法"
ROOT="${REQUESTED_ROOT:-${RESULTS_ROOT:-$HERE/campaigns}}"
DIR="$ROOT/$CAMP"
[ -d "$DIR" ] || die "campaign 不存在: $CAMP（只接受完整 id）"

# 只选择此 runner 的后代；rally 同时必须携带完整 campaign tag，防前缀误匹配。
owned_children() { # <runner pid> → pid:starttime
  python3 - "$1" "$CAMP" <<'PY'
import pathlib, sys
parent, camp = int(sys.argv[1]), sys.argv[2]
rows = {}
for p in pathlib.Path('/proc').glob('[0-9]*'):
    try:
        stat = (p / 'stat').read_text().rsplit(') ', 1)[1].split()
        argv = (p / 'cmdline').read_bytes().split(b'\0')
        rows[int(p.name)] = (int(stat[1]), stat[19], argv)
    except (OSError, ValueError, IndexError):
        continue
chosen = {parent}
for _ in range(len(rows)):
    new = {pid for pid, (ppid, _, _) in rows.items() if ppid in chosen}
    if new <= chosen:
        break
    chosen |= new
for pid in sorted(chosen - {parent}, reverse=True):
    _, start, argv = rows[pid]
    # 后代全部属该 runner；额外校验 rally tag，绝不跨 campaign。
    if any(b'esrally' in a for a in argv):
        tags = [a.split(b'=', 1)[-1].decode(errors='replace') for a in argv if b'campaign:' in a]
        if not any('campaign:' + camp in t.split(',') for t in tags):
            continue
    print('%s:%s' % (pid, start))
PY
}
signal_identity() { # <pid:starttime> <signal>
  local token="$1" sig="$2" pid="${1%%:*}" start="${1#*:}"
  [ "$(proc_start "$pid")" = "$start" ] || return 0
  echo "  $sig $pid"
  kill "-$sig" "$pid" 2>/dev/null || [ ! -e "/proc/$pid" ]
}
pid=$(runner_meta_pid "$DIR") || die "无可验证的活 runner 身份，不发送信号（可用 close-campaign.sh 收尾已死 campaign）"
start=$(proc_start "$pid") || die "runner 已退出"
children=$(owned_children "$pid") || die "无法枚举目标 runner 后代"
echo "=== 停止 campaign $CAMP（runner $pid）==="
# 子进程先停，runner trap 得以收监控；仅发给刚捕获且 starttime 未变的 PID。
for token in $children; do signal_identity "$token" TERM || die "停止后代失败"; done
[ "$(runner_meta_pid "$DIR")" = "$pid" ] && signal_identity "$pid:$start" TERM
sleep 3
for token in $children; do signal_identity "$token" KILL || die "停止后代失败"; done
signal_identity "$pid:$start" KILL || die "停止 runner 失败"
left=0
for token in $children "$pid:$start"; do
  [ "$(proc_start "${token%%:*}")" = "${token#*:}" ] && left=1
done
[ "$left" = 0 ] || die "目标 campaign 仍有残留，不补结束行"
echo "  无残留"
"$HERE/close-campaign.sh" --root "$ROOT" --reason "stop-campaign.sh 停止" "$CAMP"
