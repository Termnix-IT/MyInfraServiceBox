#!/usr/bin/env bash
# サーバの初期設定。何度実行しても同じ結果になるように作っている。
# 使い方: sudo /opt/myinfraservicebox/setup/setup-server.sh
#
# 既存の UFW ルール（SSH やリモートデスクトップなど）と Minecraft の設定には触れない。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ETC_DIR=/etc/myinfraservicebox
SECRETS_DIR=$ETC_DIR/secrets
ENV_FILE=$ETC_DIR/infra.env
STATE_DIR=/var/lib/myinfraservicebox
BACKUP_DIR=/var/backups/myinfraservicebox
DAEMON_JSON=/etc/docker/daemon.json
AFTER_RULES=/etc/ufw/after.rules
GROUP=infrabox
ADMIN_USER=${SUDO_USER:-}

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'エラー: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "sudo を付けて実行してください"
# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || die "Ubuntu 以外には対応していません"

# --- 1. 環境固有の設定 -------------------------------------------------------
create_env_file() {
  local lan_if host_addr lan_subnet
  lan_if=$(ip -4 route show default | awk '{print $5; exit}')
  host_addr=$(ip -4 -o addr show dev "$lan_if" | awk '{split($4, a, "/"); print a[1]; exit}')
  lan_subnet=$(ip -4 route show dev "$lan_if" proto kernel scope link | awk '{print $1; exit}')
  install -d -m 0750 "$ETC_DIR"
  sed -e "s|^HOST_ADDR=.*|HOST_ADDR=$host_addr|" \
      -e "s|^LAN_SUBNET=.*|LAN_SUBNET=$lan_subnet|" \
      -e "s|^LAN_IF=.*|LAN_IF=$lan_if|" \
      "$REPO_ROOT/setup/infra.env.example" > "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  log "$ENV_FILE を検出した値で作りました。内容を確認してから、もう一度このスクリプトを実行してください。"
  grep -v '^#' "$ENV_FILE" | grep -v '^$'
  exit 0
}

[[ -f $ENV_FILE ]] || create_env_file
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
for name in HOST_ADDR LAN_SUBNET LAN_IF INFRA_NETWORK INFRA_SUBNET; do
  [[ -n ${!name:-} ]] || die "$ENV_FILE に $name がありません"
done
ip link show "$LAN_IF" >/dev/null 2>&1 || die "LAN_IF=$LAN_IF のインターフェースが見つかりません"

# --- 2. パッケージ -----------------------------------------------------------
log "必要なパッケージを確認しています"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 ufw >/dev/null

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  log "Docker を公式の apt repository から導入しています"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi

# --- 3. Docker デーモンの設定 ------------------------------------------------
# firewall-backend を iptables に固定するのは、将来 nftables が既定になったときに
# DOCKER-USER のルール（下の UFW 設定）が効かなくなるのを防ぐため。
log "$DAEMON_JSON を確認しています"
daemon_changed=$(python3 - "$DAEMON_JSON" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text() if path.exists() else ""
current = json.loads(text) if text.strip() else {}
wanted = {
    "firewall-backend": "iptables",
    "log-driver": "local",
    "log-opts": {"max-size": "10m", "max-file": "3"},
}
merged = {**current, **wanted}
if merged != current:
    path.parent.mkdir(parents=True, exist_ok=True)
    if text:
        path.with_name(path.name + ".bak").write_text(text)
    path.write_text(json.dumps(merged, indent=2) + "\n")
    print("yes")
PY
)
if [[ $daemon_changed == yes ]]; then
  log "Docker デーモンを再起動しています"
  systemctl restart docker
fi
systemctl enable --now docker >/dev/null

# --- 4. グループとディレクトリ ----------------------------------------------
log "グループとディレクトリを作っています"
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
if [[ -n $ADMIN_USER && $ADMIN_USER != root ]]; then
  usermod -aG "docker,$GROUP" "$ADMIN_USER"
fi
install -d -m 0750 -o root -g "$GROUP" "$ETC_DIR"
install -d -m 2770 -o root -g "$GROUP" "$SECRETS_DIR"
chgrp "$GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
# ポータルのコンテナ（root でも infrabox でもないユーザー）が履歴を読めるように、others に r-x を残す
install -d -m 2775 -o root -g "$GROUP" "$STATE_DIR" "$STATE_DIR/services" "$STATE_DIR/locks"
install -d -m 2770 -o root -g "$GROUP" "$BACKUP_DIR" "$BACKUP_DIR/postgres"
ln -sfn "$REPO_ROOT/bin/infra" /usr/local/bin/infra

if [[ ! -f $SECRETS_DIR/postgres.env ]]; then
  log "PostgreSQL の管理者パスワードを生成しています"
  (
    umask 027
    printf 'POSTGRES_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
      > "$SECRETS_DIR/postgres.env"
  )
  chgrp "$GROUP" "$SECRETS_DIR/postgres.env"
  chmod 0640 "$SECRETS_DIR/postgres.env"
fi

# --- 5. 共通 Docker ネットワーク ---------------------------------------------
if docker network inspect "$INFRA_NETWORK" >/dev/null 2>&1; then
  actual=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$INFRA_NETWORK")
  [[ " $actual " == *" $INFRA_SUBNET "* ]] \
    || die "$INFRA_NETWORK ネットワークのアドレス帯（$actual）が INFRA_SUBNET=$INFRA_SUBNET と違います"
else
  log "$INFRA_NETWORK ネットワークを作っています"
  docker network create --driver bridge --subnet "$INFRA_SUBNET" "$INFRA_NETWORK" >/dev/null
fi

# --- 6. UFW ------------------------------------------------------------------
log "UFW のルールを追加しています"
if ! ufw status | grep -q '^Status: active'; then
  printf '注意: UFW が有効になっていません。SSH を許可してから sudo ufw enable を実行してください。\n' >&2
fi
ufw allow from "$LAN_SUBNET" to any port 80 proto tcp comment 'infrabox portal' >/dev/null
ufw allow from "$LAN_SUBNET" to any port 8100:8199 proto tcp comment 'infrabox services' >/dev/null
# ポータルのコンテナからの死活確認（ホストの公開ポートへの接続が INPUT を通る場合がある）
ufw allow from "$INFRA_SUBNET" to any port 8100:8199 proto tcp comment 'infrabox portal health check' >/dev/null
ufw allow from "$INFRA_SUBNET" to any port 25565 proto tcp comment 'infrabox portal health check' >/dev/null

# Docker が公開したポートへの通信は UFW の INPUT を通らないため、DOCKER-USER チェーンで
# LAN 側インターフェースから来た LAN 外の送信元を捨てる。after.rules に書くと UFW の起動・reload のたびに再設定される。
[[ -f $AFTER_RULES.infrabox.bak ]] || cp -a "$AFTER_RULES" "$AFTER_RULES.infrabox.bak"
python3 - "$AFTER_RULES" "$LAN_IF" "$LAN_SUBNET" <<'PY'
import pathlib, re, sys
path, lan_if, lan_subnet = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
begin, end = "# BEGIN MYINFRASERVICEBOX", "# END MYINFRASERVICEBOX"
block = f"""{begin}
# setup/setup-server.sh が生成する。手で編集しないこと。
*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A DOCKER-USER -i {lan_if} -s {lan_subnet} -j RETURN
-A DOCKER-USER -i {lan_if} -j DROP
-A DOCKER-USER -j RETURN
COMMIT
{end}
"""
text = path.read_text()
pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
new = pattern.sub(lambda _: block, text) if pattern.search(text) else text.rstrip("\n") + "\n\n" + block
if new != text:
    path.write_text(new)
PY
ufw reload >/dev/null

# --- 7. バックアップの timer -------------------------------------------------
log "バックアップの systemd timer を登録しています"
for unit in infrabox-backup.service infrabox-backup.timer; do
  sed "s|@REPO_ROOT@|$REPO_ROOT|g" "$REPO_ROOT/setup/systemd/$unit" > "/etc/systemd/system/$unit"
  chmod 0644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now infrabox-backup.timer >/dev/null

log "完了しました"
cat <<EOF
次の手順:
  1. グループ（docker, $GROUP）を反映するため、一度ログアウトしてログインし直す
  2. infra core up      # PostgreSQL・ソケットプロキシ・ポータルを起動する
  3. README.md の「サーバでの確認」を実施する
EOF
