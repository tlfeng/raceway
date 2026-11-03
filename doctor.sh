#!/usr/bin/env bash
# ============================================================================
# doctor.sh —— 新环境体检：一条命令回答"这台机器离能起跑还差什么"
#   ./doctor.sh        # 在驱动宿主机上运行
# 纯只读：不碰集群、不改状态、不依赖 vm-bench.env（它正是体检对象之一）。
# 六组检查：基础工具 / env 配置 / ibs 拓扑 / 虚机连通 / 语料与安装包 / 指标库，
# 末尾给出阈值生效值。FAIL>0 时退出码 1 —— 修完 FAIL 再起跑。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"

PASS=0; WARN=0; FAIL=0
ok()   { echo "  ✔ $*"; PASS=$((PASS+1)); }
warn() { echo "  ⚠ $*"; WARN=$((WARN+1)); }
bad()  { echo "  ✘ $*"; FAIL=$((FAIL+1)); }
note() { echo "  · $*"; }
section() { echo; echo "== $* =="; }

# ---- env（可选加载：没有 env 也能体检，只是依赖项标跳过） ---------------------
ENV_LOADED=0
if [ -f vm-bench.env ]; then
  # shellcheck disable=SC1091
  source ./vm-bench.env 2>/dev/null && ENV_LOADED=1
fi

# ---- 1/6 驱动宿主机基础工具 ---------------------------------------------------
section "1/6 驱动宿主机基础工具"
for t in python3 ssh scp curl; do
  command -v "$t" >/dev/null 2>&1 && ok "$t" || bad "缺 $t（apt/yum 安装）"
done
RALLY_BIN="${RALLY:-esrally}"
if command -v "$RALLY_BIN" >/dev/null 2>&1; then
  ver=$("$RALLY_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
  case "$ver" in
    *.*) if [ "${ver%%.*}" -gt 2 ] || { [ "${ver%%.*}" -eq 2 ] && [ "${ver#*.}" -ge 12 ]; }
         then ok "esrally $ver"; else warn "esrally $ver < 2.12（benchmark-only pipeline 需要 ≥2.12）"; fi;;
    '' ) warn "esrally 存在但取不到版本（$RALLY_BIN --version 无输出）";;
  esac
else
  bad "缺 esrally（pip3 install esrally；$RALLY_BIN 不在 PATH）"
fi

# ---- 2/6 vm-bench.env ---------------------------------------------------------
section "2/6 vm-bench.env 配置"
if [ "$ENV_LOADED" = 0 ]; then
  bad "vm-bench.env 不存在或不可解析 —— cp vm-bench.env.example vm-bench.env 后按环境填写"
else
  ok "存在且可解析"
  # 必填键：等号左边非注释行即视为已配置（值对不对后面逐项看）
  for k in IBS_BASE IBS_ROOT MON_DIR HTTP_PORT HEAP ADMIN_USER ADMIN_PW \
           EZ_RUN_USER ES_RUN_USER ROOT PKG_DIR RESULTS_ROOT RALLY ENV_ID \
           VM_SSH_OPTS VM_NODES_EZ VM_NODES_ES; do
    grep -qE "^[[:space:]]*$k=" vm-bench.env && ok "$k 已配置" \
      || bad "缺 $k（vm-bench.env 里补上）"
  done
  grep -q "CHANGE_ME" vm-bench.env && warn "ADMIN_PW 还是模板占位 CHANGE_ME（initialize.sh 要求 ≥9 位）"
fi

# ---- 3/6 ibs 拓扑（单事实源 = ip.txt）-----------------------------------------
section "3/6 ibs 拓扑（ip.txt）"
VM_IPS=(); VM_NODES=()
if [ "$ENV_LOADED" = 0 ]; then
  bad "没有 vm-bench.env，不知道 IBS_ROOT 在哪 —— 先完成第 2 组"
elif [ ! -s "${IBS_ROOT:-}/configs/ip.txt" ]; then
  bad "ibs ip.txt 不存在: ${IBS_ROOT:-?}/configs/ip.txt（拓扑单事实源；用 ibs 规划拓扑生成）"
else
  while read -r _pub priv host _ver; do
    [ -n "${host:-}" ] || continue
    case "$host" in esrally-*) continue;; esac
    VM_IPS+=("$priv"); VM_NODES+=("$host")
  done < "$IBS_ROOT/configs/ip.txt"
  if [ "${#VM_IPS[@]}" -ge 1 ]; then
    ok "集群节点 ${#VM_IPS[@]} 台: ${VM_NODES[*]}"
    [ -d "$IBS_ROOT/ops" ] && ok "ibs 仓库就位（$IBS_ROOT/ops）" \
      || bad "缺 ibs 仓库 $IBS_ROOT/ops（部署层依赖 python3 -m ops.deploy）"
  else
    bad "ip.txt 里没有集群角色节点（esrally 行是驱动器，其余是集群节点）"
  fi
fi

# ---- 4/6 虚机连通 -------------------------------------------------------------
section "4/6 虚机连通（root 免密）"
if [ "${#VM_IPS[@]}" -eq 0 ]; then
  warn "跳过（拓扑不可用）"
else
  for i in "${!VM_IPS[@]}"; do
    ip="${VM_IPS[$i]}"; nm="${VM_NODES[$i]}"
    hn=$(ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no "root@$ip" hostname 2>/dev/null)
    if [ "$hn" = "$nm" ]; then
      ok "$nm ($ip) 免密可达，主机名一致"
    elif [ -n "$hn" ]; then
      warn "$nm ($ip) 可达但主机名是 $hn（ibs 部署时会改写，可忽略；对不上会误导排障）"
    else
      bad "$nm ($ip) 免密连不上（ssh-copy-id root@$ip；BatchMode 下带密码会直接失败）"
    fi
  done
fi

# ---- 5/6 语料与安装包 ---------------------------------------------------------
section "5/6 语料赛道与安装包"
if [ "$ENV_LOADED" = 0 ]; then
  warn "跳过（依赖 vm-bench.env）"
else
  miss_var=0
  # TRACK_* 的值是 env 里的 $ROOT/... 引用 —— 从文件 grep 出**名字**，取 source 后
  # 的变量值（间接展开），不做 eval（env 是用户自己的文件，也不值得开这个口子）
  while IFS= read -r k; do
    k=$(echo "$k" | tr -d '[:space:]')
    case "$k" in TRACK_*) ;; *) continue;; esac
    v="${!k:-}"
    [ -n "$v" ] || continue
    if [ -d "$v" ]; then
      if [ -f "$v/track.json" ]; then ok "$k → $v"
      else warn "$k → $v 目录在但没有 track.json（不是 rally 赛道目录？路径填错或语料没铺全）"
      fi; continue; fi
    case "$v" in
      *codec*) warn "$k → $v 不存在（prepare-codec-track.sh 按需生成，用到哪条跑哪条）"; miss_var=$((miss_var+1));;
      *) bad "$k → $v 不存在（基座赛道缺语料，起跑必炸）";;
    esac
  done < <(grep -oE '^[[:space:]]*TRACK_[A-Z0-9_]+' vm-bench.env)
  # 安装包：任一 bundle 即可部署（版本由配置推导）
  if [ -n "${PKG_DIR:-}" ] && ls "$PKG_DIR"/*.tar.gz >/dev/null 2>&1; then
    ok "安装包就位: $(ls "$PKG_DIR"/*.tar.gz 2>/dev/null | wc -l) 个（$PKG_DIR）"
  else
    bad "PKG_DIR=${PKG_DIR:-?} 里没有 *.tar.gz（easysearch/elasticsearch bundle，部署必需）"
  fi
fi

# ---- 6/6 指标库与看板（信息性：压测窗口内本来就该停着） ------------------------
section "6/6 指标库 9237 / Kibana 5601（压测窗口内应为停用状态）"
port_up() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }
if port_up 9237; then note "指标库 9237 在运行（入库/看板可用；压测窗口内记得停）"; else note "指标库 9237 未运行（压测窗口内是常态；入库前再启动）"; fi
if port_up 5601; then note "Kibana 5601 在运行"; else note "Kibana 5601 未运行"; fi
if command -v systemctl >/dev/null 2>&1; then
  st=$(systemctl is-enabled easysearch-metric-store.service 2>/dev/null || true)
  [ "$st" = "enabled" ] && warn "指标库开机自启是 enabled（窗口期一重启就被拉回来抢 CPU；systemctl disable 关掉）" \
                       || note "指标库开机自启未启用"
fi

# ---- 阈值生效值（主旋钮 VERDICT_*，QUIET_* 继承它） ---------------------------
section "卡点阈值生效值（主旋钮 vm-bench.env 的 VERDICT_*）"
echo "  VERDICT_STEAL_MAX=${VERDICT_STEAL_MAX:-2.0}（赛后 CONTAMINATED / 赛前卡点同口径）"
echo "  VERDICT_FOREIGN_MAX=${VERDICT_FOREIGN_MAX:-5}（外来进程 CPU）"
echo "  QUIET_LOAD1=${QUIET_LOAD1:-3}  QUIET_BENCH_CPU=${QUIET_BENCH_CPU:-1}  QUIET_FREE_GB=${QUIET_FREE_GB:-20}"

echo
echo "体检完成: 通过 $PASS / 警告 $WARN / 失败 $FAIL"
[ "$FAIL" -eq 0 ] || { echo "先修完上面的 ✘ 再起跑（⚠ 不阻塞，但会误导排障）"; exit 1; }
exit 0
