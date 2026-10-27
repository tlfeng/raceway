#!/usr/bin/env bash
# ============================================================================
# load-monitor.sh —— 虚机侧系统负载采样器（在每台虚机上运行，零依赖，纯 /proc）
# 由 run-bench.sh 通过 ssh 启停；也可手动使用：
#   ENGINE_MARK=<标记> ./load-monitor.sh start <tag>   # 周期采样 -> mon-<tag>.csv
#   ./load-monitor.sh stop  [tag]     # 停止（无 tag 停全部）
#   ./load-monitor.sh probe [seconds] # 快速探测 N 秒并输出均值（供赛前卡点，1s 采样）
#   ./load-monitor.sh summary <csv>   # 赛后判定 ENV-VERDICT
#
# 采样间隔 MON_INTERVAL（默认 5s；probe 固定 1s）。summary 只消费 avg/max，
# 5s 已够，且把采样器自身 fork 噪声降到 1/5（其进程不匹配 ENGINE_MARK，
# 本会计入 foreign——被测指标里含采样器自己）。编排按相位分 tag 采集
# （<fid>-w 写入 / -q 查询；merge 相位不采——forcemerge 的 HTTP 调用是同步的、
# 秒级完成，5s 间隔一次都赶不上，实测 -m CSV 全部只有表头），
# 分相位 summary 即"写入/查询时的系统占用"画像；整场拍成一份均值无法区分相位。
#
# ENGINE_MARK：进程归因标记。cmdline 含此串的进程计入 bench 列（被测引擎），
# 其余计入 foreign 列（外来负载）。注意 merge 写回是内核线程（kworker 无
# cmdline）→ 计入 foreign，forcemerge 相位 foreign 偏高属正常。
# 标记必须与部署根一致（ibs 模式即安装根 /infini）；错位时 bench 恒为 0、
# 引擎 CPU 被计入 foreign，"外来进程"判定失明（202610 教训：legacy
# /opt/bench 匹配不到任何进程 → bench 恒 0、判定全盲）。
#
# CSV 列:
# ts,cpu_usr,cpu_sys,cpu_steal,load1,mem_avail_gb,swpin_s,
# disk_rw_mbs,net_rx_mbs,net_tx_mbs,bench_cpu,foreign_cpu
# ============================================================================
set -u
MON_DIR=${RACEWAY_MON_DIR:-/opt/raceway/monitor}   # CSV/pid 落位（与编排侧 BENCH_BASE 一致）
INTERVAL=${MON_INTERVAL:-5}
# 赛后判定阈值（独立运行时的回退默认）。主旋钮在 vm-bench.env 的 VERDICT_*，
# runner 会经 ssh 显式传入（缺键时才落到这里的默认值）——一处改，赛前卡点同口径生效。
VERDICT_STEAL_MAX=${VERDICT_STEAL_MAX:-2.0}
VERDICT_FOREIGN_MAX=${VERDICT_FOREIGN_MAX:-5}
mkdir -p "$MON_DIR"

ENGINE_MARK=${ENGINE_MARK:-/infini}   # 默认 = ibs 安装根，引擎 java 必含此串

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

start_sampler() { # <tag>
  local tag="$1"
  local csv="$MON_DIR/mon-$tag.csv"
  [ -f "$MON_DIR/mon-$tag.pid" ] && kill "$(cat "$MON_DIR/mon-$tag.pid")" 2>/dev/null
  nohup bash "$0" _loop "$csv" "$tag" >/dev/null 2>&1 &
  echo $! > "$MON_DIR/mon-$tag.pid"
  echo "monitor started: $csv (pid $!)"
}

_loop() { # <csv> <tag>
  local csv="$1" tag="$2" dev mb10
  dev=$(diskdev); dev=${dev%[0-9]*}; dev=${dev%p}; dev=${dev##*/}
  mb10=$(( 1048576 * INTERVAL / 10 ))   # 0.1MB/s 分辨率的除数（防整除截断吞掉小流量）
  echo "ts,cpu_usr,cpu_sys,cpu_steal,load1,mem_avail_gb,swpin_s,disk_rw_mbs,net_rx_mbs,net_tx_mbs,bench_cpu,foreign_cpu" > "$csv"
  local c0 c1 s0 s1 d0 d1 n0 n1 p0 p1
  c0=$(cpu_line)
  s0=$(awk '/pswpin /{print $2}' /proc/vmstat)
  d0=$(grep " $dev " /proc/diskstats)
  n0=$(awk -F'[: ]+' '/:/{if($2!="lo") {rx+=$3; tx+=$11}} END{print rx, tx}' /proc/net/dev)
  p0=$(mktemp); p1=$(mktemp); load_snapshot "$p0"
  while true; do
    sleep "$INTERVAL"
    c1=$(cpu_line)
    read -r _ u n sy id iow irq sirq st _ _ <<< "$c1"
    read -r _ u0 n0c sy0 id0 iow0 irq0 sirq0 st0 _ _ <<< "$c0"
    dt=$(( (u+n+sy+id+iow+irq+sirq+st) - (u0+n0c+sy0+id0+iow0+irq0+sirq0+st0) ))
    if [ "$dt" -le 0 ]; then c0="$c1"; continue; fi
    # dt 是全核 jiffies 和（100Hz），bench/foreign 的进程 jiffies 差按它归一成 %
    usr=$(( 100*(u-u0)   / dt )); sys=$(( 100*(sy-sy0) / dt ))
    stl=$(( 100*(st-st0) / dt ))
    l1=$(cut -d' ' -f1 /proc/loadavg)
    m1=$(awk '/^MemAvailable/{print $2}' /proc/meminfo)
    agb=$(( m1 / 1048576 ))   # 页缓存余量：查询性能与其强相关；事件级内存压力看 swpin
    s1=$(awk '/pswpin /{print $2}' /proc/vmstat); sw=$(( s1-s0 )); s0=$s1
    # diskstats: f6=读扇区 f10=写扇区
    d1=$(grep " $dev " /proc/diskstats)
    read -r _ _ _ _ _ sr0 _ _ _ sw0 _ <<< "$d0"
    read -r _ _ _ _ _ sr1 _ _ _ sw1 _ <<< "$d1"
    rw=$(( ( (sr1 - sr0) + (sw1 - sw0) ) * 512 / mb10 ))   # 0.1MB/s
    d0="$d1"
    n1=$(awk -F'[: ]+' '/:/{if($2!="lo") {rx+=$3; tx+=$11}} END{print rx, tx}' /proc/net/dev)
    set -- $n1; rx1=$1; tx1=$2; set -- $n0; rx0=$1; tx0=$2
    rx=$(( (rx1-rx0) / mb10 )); tx=$(( (tx1-tx0) / mb10 )); n0="$n1"
    load_snapshot "$p1"
    bench=$(join_delta "$p0" "$p1" 1); fore=$(join_delta "$p0" "$p1" 0)
    bench=$(( 100*bench/dt )); fore=$(( 100*fore/dt ))
    rm -f "$p0"; p0="$p1"; p1=$(mktemp)
    printf '%s,%d,%d,%d,%s,%d,%d,%d.%d,%d.%d,%d.%d,%d,%d\n' \
      "$(date +%s)" "$usr" "$sys" "$stl" "$l1" "$agb" "$sw" \
      $((rw/10)) $((rw%10)) $((rx/10)) $((rx%10)) $((tx/10)) $((tx%10)) \
      "$bench" "$fore" >> "$csv"
    c0="$c1"
  done
}

# join_delta <f0> <f1> <bench_flag> : 同 pid 的 (utime+stime) 差值和（新 pid 计入全量）。
# 已退出 pid 的 CPU 不计——对外来进程检测即"抓常驻、漏突发"，抓常驻是主要威胁模型
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
  local sec="${1:-6}" csv; csv=$(mktemp --suffix=.csv)
  MON_INTERVAL=1 bash "$0" _loop "$csv" probe & local pid=$!
  sleep $((sec + 2)); kill $pid 2>/dev/null
  summary_quiet "$csv"
  rm -f "$csv"
}

# summary_quiet <csv> — 输出均值行（供 probe/卡点）
summary_quiet() {
  awk -F, 'NR>1 {
    n++; st+=$4; l+=$5; f+=$12; b+=$11
    if($4>stM)stM=$4; if($12>fM)fM=$12
  } END {
    if(n==0){print "probe: no data"; exit}
    printf "load1_avg=%.2f steal_avg=%.2f%% steal_max=%.2f%% foreign_cpu_avg=%.2f%% foreign_cpu_max=%.2f%% bench_cpu_avg=%.2f%%\n",
      l/n, st/n, stM, f/n, fM, b/n
  }' "$1"
}

# summary <csv> — 赛后判定（按相位各跑一份；写入/查询相位才是可引用口径）
summary() {
  local csv="$1"
  [ -s "$csv" ] || { echo "ENV-VERDICT: NO-DATA ($csv 不存在或为空)"; exit 2; }
  awk -F, -v stM="$VERDICT_STEAL_MAX" -v fM="$VERDICT_FOREIGN_MAX" '
    NR>1 {
      n++; st+=$4; f+=$12; sw+=$7
      us+=$2; sy+=$3; bc+=$11; dr+=$8; rx+=$9; tx+=$10
      if($4>stMx)stMx=$4; if($12>fMx)fMx=$12
      if($2>usMx)usMx=$2; if($3>syMx)syMx=$3; if($11>bcMx)bcMx=$11
      if($8>drMx)drMx=$8
      if(mn==""||$6+0<mn+0)mn=$6
    } END {
      if(n==0){print "ENV-VERDICT: NO-DATA"; exit 1}
      # 这些量**每 5 秒都在采**，只是摘要里一直只吐 steal/foreign/swapin 三个，
      # 其余全被扔掉了。补进摘要后 bench-export 直接就能落库 —— 不用新增探针，
      # 也不用改采样节奏。补的是"引擎到底吃了多少 CPU/IO"这类真口径：
      #   bench_cpu = 被测引擎进程的 CPU 占用（跨引擎直接可比）
      #   cpu_usr/cpu_sys = 整机 CPU 占用（负载占比）
      #   disk_rw_mbs = 磁盘读写吞吐（IO 瓶颈与否）
      printf "cpu_usr_avg=%.2f cpu_usr_max=%.2f cpu_sys_avg=%.2f cpu_sys_max=%.2f bench_cpu_avg=%.2f bench_cpu_max=%.2f disk_rw_mbs_avg=%.2f disk_rw_mbs_max=%.2f net_rx_mbs_avg=%.2f net_tx_mbs_avg=%.2f mem_avail_gb_min=%.2f\n",
        us/n, usMx, sy/n, syMx, bc/n, bcMx, dr/n, drMx, rx/n, tx/n, mn
      printf "samples=%d steal_avg=%.2f%% steal_max=%.2f%% foreign_avg=%.2f%% foreign_max=%.2f%% swapin=%d\n",
        n, st/n, stMx, f/n, fMx, sw
      v="OK"; r=""
      if (stMx>stM) { v="CONTAMINATED"; r=r " steal超阈值(宿主机超卖噪声,与赛前卡点同口径)" }
      if (fMx>fM)   { if(v=="OK")v="WARN"; r=r " 存在外来进程负载" }
      if (sw>0)     { if(v=="OK")v="WARN"; r=r " 发生swap-in" }
      printf "ENV-VERDICT: %s%s\n", v, (r==""?"":r)
    }' "$csv"
}

case "${1:-}" in
  start)  start_sampler "${2:?tag}";;
  stop)   stop_all;;
  probe)  probe "${2:-6}";;
  summary) summary "${2:?csv}";;
  _loop)  shift; _loop "$@";;
  *) echo "用法: $0 start <tag> | stop | probe [sec] | summary <csv>"; exit 1;;
esac
