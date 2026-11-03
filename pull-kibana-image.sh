#!/usr/bin/env bash
# ============================================================================
# pull-kibana-image.sh —— 不装 Docker，直接从 registry API 取 Kibana 镜像层解开用
#
# 背景：artifacts.elastic.co 的 /downloads/kibana/ 整条路径在当前网络环境被封
# （连 .sha512 都是 404），而 docker.elastic.co 的 registry API 是通的。
# Kibana 镜像本质上就是若干层 tar，解开就是一份可运行的 Kibana 目录。
#
# 产物：/testing/kibana-rootfs/usr/share/kibana   （含自带的 Node 运行时）
#
# 用法：./pull-kibana-image.sh [tag=7.17.29] [dest=/testing/kibana-rootfs]
# ============================================================================
set -uo pipefail

TAG="${1:-7.17.29}"
DEST="${2:-/testing/kibana-rootfs}"
REG=https://docker.elastic.co
REPO=kibana/kibana

die() { echo "[pull-kibana] ERROR: $*" >&2; exit 1; }
log() { echo "[pull-kibana] $*"; }

# ---- 1) 匿名 token ----------------------------------------------------------
H=$(curl -sI --max-time 20 "$REG/v2/" | tr -d '\r' | grep -i '^www-authenticate' || true)
REALM=$(printf '%s' "$H" | sed -n 's/.*realm="\([^"]*\)".*/\1/p')
SVC=$(printf '%s' "$H" | sed -n 's/.*service="\([^"]*\)".*/\1/p')
[ -n "$REALM" ] || die "取不到认证 realm"
TOKEN=$(curl -s --max-time 30 "${REALM}?service=${SVC}&scope=repository:${REPO}:pull" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')
[ -n "$TOKEN" ] || die "取 token 失败"
log "token ok (${#TOKEN} chars)"

AUTH=(-H "Authorization: Bearer $TOKEN")

# ---- 2) manifest list → amd64 子 manifest -----------------------------------
curl -s --max-time 30 "${AUTH[@]}" \
  -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
  "$REG/v2/$REPO/manifests/$TAG" -o /tmp/kb-list.json
DIGEST=$(python3 - <<'PY'
import json
m = json.load(open("/tmp/kb-list.json"))
for x in m.get("manifests", []):
    p = x.get("platform", {})
    if p.get("architecture") == "amd64" and p.get("os") == "linux":
        print(x["digest"]); break
PY
)
[ -n "$DIGEST" ] || die "manifest list 里没有 amd64"
log "amd64 manifest: $DIGEST"

curl -s --max-time 30 "${AUTH[@]}" \
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "$REG/v2/$REPO/manifests/$DIGEST" -o /tmp/kb-manifest.json
python3 -c 'import json; m=json.load(open("/tmp/kb-manifest.json")); print("层数:", len(m.get("layers",[])), " 总大小: %.1f MB" % (sum(l["size"] for l in m["layers"])/1048576))'

# ---- 3) 逐层下载并解开 ------------------------------------------------------
mkdir -p "$DEST"
BLD=/tmp/kb-layers; mkdir -p "$BLD"
n=0
while read -r ldigest lsize; do
  n=$((n+1))
  f="$BLD/$(echo "$ldigest" | tr ':' '_').tar.gz"
  if [ ! -s "$f" ]; then
    log "层 $n 下载中 ($(echo "scale=1; $lsize/1048576" | bc) MB) ..."
    curl -sL --max-time 900 "${AUTH[@]}" "$REG/v2/$REPO/blobs/$ldigest" -o "$f" \
      || die "层 $n 下载失败"
  fi
  log "层 $n 解开 ..."
  tar xzf "$f" -C "$DEST" --no-same-owner 2>/tmp/kb-tar.err || {
    # 白化文件（.wh.）是 Docker 删除语义，直接忽略
    cat /tmp/kb-tar.err | head -5
    log "  （层内含 whiteout，按普通 tar 继续）"
  }
done < <(python3 -c '
import json
m = json.load(open("/tmp/kb-manifest.json"))
for l in m["layers"]:
    print(l["digest"], l["size"])
')

KB="$DEST/usr/share/kibana"
[ -d "$KB" ] || die "解开后没找到 $KB"
log "解开完成：$KB"
ls -la "$KB" | head -12
echo
echo "--- Node 运行时 ---"
"$KB/node/bin/node" --version 2>&1 || die "自带 Node 不可用"
echo "--- Kibana 版本 ---"
grep -o '"version":[^,]*' "$KB/package.json" | head -1
