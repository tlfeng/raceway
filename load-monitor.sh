#!/usr/bin/env bash
# ============================================================================
# load-monitor.sh —— 虚机侧系统负载采样器（在每台虚机上运行，零依赖，纯 /proc）
# 由 run-bench.sh 通过 ssh 启停；也可手动使用：
#   ENGINE_MARK=<标记> ./load-monitor.sh start <tag>   # 1s 采样 -> mon-<tag>.csv
#   ./load-monitor.sh stop  [tag]     # 停止（无 tag 停全部）
#   ./load-monitor.sh probe [seconds] # 快速探测 N 秒并输出均值（供赛前门禁）
#   ./load-monitor.sh summary <csv>   # 赛后判定 ENV-VERDICT
#
# ENGINE_MARK：进程归因标记。cmdline 含此串的进程计入 bench 列（被测引擎），
# 其余计入 foreign 列（外来负载）。必须与部署根一致（ibs 模式即安装根 /infini）；
# 标记错位时 bench 恒为 0、引擎 CPU 被计入 foreign，verdict 的外来进程判定失明。
#
# CSV 列:
# ts,cpu_usr,cpu_sys,cpu_iow,cpu_steal,cpu_idle,load1,mem_used_pct,
# swpin_s,disk_util_pct,disk_rw_mbs,net_rx_mbs,net_tx_mbs,bench_cpu,foreign_cpu
# ============================================================================
set -u
MON_DIR=/opt/bench/monitor
# 赛后判定阈值（与 vm-bench.env 保持一致；独立运行时的默认值）
VERDICT_STEAL_AVG=${VERDICT_STEAL_AVG:-0.5}
VERDICT_STEAL_MAX=${VERDICT_STEAL_MAX:-2.0}
VERDICT_IOWAIT_AVG=${VERDICT_IOWAIT_AVG:-5}
mkdir -p "$MON_DIR"

cpu_line() { grep '^cpu ' /proc/stat; }
diskdev() { df / --output=source 2>/dev/null | tail -1; }   # 如 /dev/sda2 -> 取 sda
load_snapshot() { # 把 <pid:cmdline是否bench> 与 utime+stime 打到临时文件
  local out="$1" p cmd b line rest u s
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      *$ENGINE_MARK*) b=1;;
      *) b=0;;
    esac
    line=$(cat "$d/stat" 2>/dev/null) || continue
    rest="${line#**) }"          # 去掉 "pid (comm) " 前缀（comm 可能含空格）
    set -- $rest                 # $1=state ... $12=utime $13=stime
    u=${12:-0}; s=${13:-0}
    echo "$p $b $((u + s))"
  done > "$out"
}

# 进程归因标记（cmdline 含此串的进程计入 bench 列，其余计入 foreign 列）。
# 默认 /infini = ibs 安装根，引擎 java（/infini/<engine>/jdk/bin/java）必含此串。
# 202610 教训：旧默认 path.home=/opt/bench 是 legacy 路径，ibs 模式匹配不到任何
# 进程 → bench 恒为 0、引擎 CPU 全计入 foreign，"外来进程"判定失明。
ENGINE_MARK=${ENGINE_MARK:-/infini}

start_sampler() { # <tag>
  local tag="$1"
  local csv="$MON_DIR/mon-$tag.csv"
  [ -f "$MON_DIR/mon-$tag.pid" ] && kill "$(cat "$MON_DIR/mon-$tag.pid")" 2>/dev/null
  nohup bash "$0" _loop "$csv" "$tag" >/dev/null 2>&1 &
  echo $! > "$MON_DIR/mon-$tag.pid"
  echo "monitor started: $csv (pid $!)"
}

_loop() { # <csv> <tag>
  local csv="$1" tag="$2" dev
  dev=$(diskdev); dev=${dev%[0-9]*}; dev=${dev%p}; dev=${dev##*/}
  echo "ts,cpu_usr,cpu_sys,cpu_iow,cpu_steal,cpu_idle,load1,mem_used_pct,swpin_s,disk_util_pct,disk_rw_mbs,net_rx_mbs,net_tx_mbs,bench_cpu,foreign_cpu" > "$csv"
  local c0 c1 s0 s1 d0 d1 n0 n1 p0 p1
  c0=$(cpu_line)
  s0=$(awk '/pswpin /{print $2}' /proc/vmstat)
  d0=$(grep " $dev " /proc/diskstats)
  n0=$(awk -F'[: ]+' '/:/{if($2!="lo") {rx+=$3; tx+=$11}} END{print rx, tx}' /proc/net/dev)
  p0=$(mktemp); p1=$(mktemp); load_snapshot "$p0"
  while true; do
    sleep 1
    c1=$(cpu_line)
    read -r _ u n sy id iow irq sirq st _ _ <<< "$c1"
    read -r _ u0 n0c sy0 id0 iow0 irq0 sirq0 st0 _ _ <<< "$c0"
    dt=$(( (u+n+sy+id+iow+irq+sirq+st) - (u0+n0c+sy0+id0+iow0+irq0+sirq0+st0) ))
    if [ "$dt" -le 0 ]; then c0="$c1"; continue; fi
    usr=$(( 100*(u-u0)   / dt )); sys=$(( 100*(sy-sy0) / dt ))
    iow=$(( 100*(iow-iow0)/ dt )); stl=$(( 100*(st-st0) / dt ))
    idl=$(( 100*(id-id0) / dt ))
    l1=$(cut -d' ' -f1 /proc/loadavg)
    m1=$(grep MemAvailable /proc/meminfo | awk '{print $2}'); mt=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    mem=$(( 100*(mt-m1)/mt ))
    s1=$(awk '/pswpin /{print $2}' /proc/vmstat); sw=$(( s1-s0 )); s0=$s1
    # diskstats: f6=读扇区 f10=写扇区 f13=io_ticks(ms)
    d1=$(grep " $dev " /proc/diskstats)
    read -r _ _ _ _ _ sr0 _ _ _ sw0 _ _ ti0 _ <<< "$d0"
    read -r _ _ _ _ _ sr1 _ _ _ sw1 _ _ ti1 _ <<< "$d1"
    util=$(( ti1 - ti0 )); [ "$util" -gt 1000 ] && util=1000
    rw=$(( ( (sr1 - sr0) + (sw1 - sw0) ) * 512 / 1048576 ))   # MB/s
    d0="$d1"
    n1=$(awk -F'[: ]+' '/:/{if($2!="lo") {rx+=$3; tx+=$11}} END{print rx, tx}' /proc/net/dev)
    set -- $n1; rx1=$1; tx1=$2; set -- $n0; rx0=$1; tx0=$2
    rxm=$(( (rx1-rx0)/1048576 )); txm=$(( (tx1-tx0)/1048576 )); n0="$n1"
    load_snapshot "$p1"
    bench=$(join_delta "$p0" "$p1" 1); fore=$(join_delta "$p0" "$p1" 0)
    bench=$(( 100*bench/dt )); fore=$(( 100*fore/dt ))
    rm -f "$p0"; p0="$p1"; p1=$(mktemp)
    echo "$(date +%s),$usr,$sys,$iow,$stl,$idl,$l1,$mem,$sw,$((util/10)),$rw,$rxm,$txm,$bench,$fore" >> "$csv"
    c0="$c1"
  done
}

# join_delta <f0> <f1> <bench_flag> : 同 pid 的 (utime+stime) 差值和（新 pid 计入全量）
join_delta() {
  awk -v want="$3" '
    NR==FNR { u0[$1]=$3; next }
    { d = $3 - ( $1 in u0 ? u0[$1] : 0 ); if (d>0 && $2==want) s+=d }
    END { print s+0 }
  ' "$1" "$2"
}

stop_all() {
  local f p
  for f in "$MON_DIR"/mon-*.pid; do
    [ -f "$f" ] || continue
    p=$(cat "$f"); kill "$p" 2>/dev/null; rm -f "$f"
  done
  pkill -f "load-monitor.sh _loo[p]" 2>/dev/null
  echo "monitors stopped"
}

probe() { # <seconds>
  local sec="${1:-5}" csv; csv=$(mktemp --suffix=.csv)
  bash "$0" _loop "$csv" probe & local pid=$!
  sleep $((sec + 2)); kill $pid 2>/dev/null
  summary_quiet "$csv" "$sec"
  rm -f "$csv"
}

# summary_quiet <csv> <approx_sec> — 输出均值行（供 probe/gate）
summary_quiet() {
  awk -F, -v sec="$2" 'NR>1 {
    n++; st+=$5; io+=$4; l+=$7; f+=$15; b+=$14
    if($5>stM)stM=$5; if($4>ioM)ioM=$4; if($7>lM)lM=$7; if($15>fM)fM=$15
  } END {
    if(n==0){print "probe: no data"; exit}
    printf "load1_avg=%.2f steal_avg=%.2f%% steal_max=%.2f%% iowait_avg=%.2f%% foreign_cpu_avg=%.2f%% foreign_cpu_max=%.2f%% bench_cpu_avg=%.2f%%\n",
      l/n, st/n, stM, io/n, f/n, fM, b/n
  }' "$1"
}

# summary <csv> — 赛后判定
summary() {
  local csv="$1"
  [ -s "$csv" ] || { echo "ENV-VERDICT: NO-DATA ($csv 不存在或为空)"; exit 2; }
  awk -F, -v stA="$VERDICT_STEAL_AVG" -v stM="$VERDICT_STEAL_MAX" -v ioA="$VERDICT_IOWAIT_AVG" '
    NR>1 {
      n++; st+=$5; io+=$4; l+=$7; f+=$15; sw+=$9; du+=$10
      if($5>stMx)stMx=$5; if($4>ioMx)ioMx=$4; if($7>lMx)lMx=$7; if($15>fMx)fMx=$15
      if($5>stPk)stPk=$5; if($15>foPk)foPk=$15
    } END {
      if(n==0){print "ENV-VERDICT: NO-DATA"; exit 1}
      sta=st/n; ioa=io/n; fa=f/n
      printf "samples=%d load1_max=%.2f steal_avg=%.2f%% steal_max=%.2f%% iowait_avg=%.2f%% iowait_max=%.2f%% foreign_avg=%.2f%% foreign_max=%.2f%% swapin=%d disk_util_avg=%.1f%%\n",
        n, lMx, sta, stMx, ioa, ioMx, fa, foPk, sw, du/n
      v="OK"; r=""
      if (sta>stA || stMx>stM) { v="CONTAMINATED"; r=r " steal超阈值(宿主机超卖噪声)" }
      if (ioa>ioA)              { if(v=="OK")v="WARN"; r=r " iowait偏高" }
      if (fa>5)                 { if(v=="OK")v="WARN"; r=r " 存在外来进程负载" }
      if (sw>0)                 { if(v=="OK")v="WARN"; r=r " 发生swap-in" }
      printf "ENV-VERDICT: %s%s\n", v, (r==""?"":r)
    }' "$csv"
}

case "${1:-}" in
  start)  start_sampler "${2:?tag}";;
  stop)   stop_all;;
  probe)  probe "${2:-5}";;
  summary) summary "${2:?csv}";;
  _loop)  shift; _loop "$@";;
  *) echo "用法: $0 start <tag> | stop | probe [sec] | summary <csv>"; exit 1;;
esac
