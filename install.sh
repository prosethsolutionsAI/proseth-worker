#!/usr/bin/env bash
#
# Proseth Worker — turn a fresh Ubuntu box into a deploy agent.
#
#   sudo ./install.sh
#
# Asks four questions, installs the tooling, registers a systemd service, and
# connects. Re-running it is safe: it updates in place and keeps the existing
# configuration unless you ask to change it.
#
# The worker dials OUT to the Supervisor. Nothing connects to this machine, so
# no inbound firewall rule is needed here and no route is needed from the
# Supervisor into this network.
set -uo pipefail

SERVICE="proseth-worker"
PREFIX="/opt/proseth-worker"
CONFIG_DIR="/etc/proseth-worker"
CONFIG="$CONFIG_DIR/config.json"
RUN_USER="proseth"
# Code lives in /opt and is root-owned so the service account cannot rewrite
# its own agent. Everything the agent WRITES - Ansible temp, terraform working
# directories, SSH control sockets - goes here instead. Ansible refuses to even
# start if it cannot write to $HOME, so this split is not optional.
STATE_DIR="/var/lib/proseth-worker"
DEFAULT_PORT=9998

BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'
YELLOW=$'\e[33m'; BLUE=$'\e[36m'; OFF=$'\e[0m'
if [ ! -t 1 ]; then BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; OFF=""; fi

say()  { echo "${BLUE}==>${OFF} $*"; }
ok()   { echo "    ${GREEN}ok${OFF}   $*"; }
warn() { echo "    ${YELLOW}!!${OFF}   $*"; }
die()  { echo "" >&2; echo "${RED}Stopped:${OFF} $*" >&2; exit 1; }

banner() {
  echo ""
  echo "${BOLD}  Proseth Worker — deploy agent setup${OFF}"
  echo "${DIM}  ─────────────────────────────────────────────────────────${OFF}"
  echo "${DIM}  This machine will connect OUT to your Supervisor and wait${OFF}"
  echo "${DIM}  for jobs. Nothing connects in, so no inbound firewall rule${OFF}"
  echo "${DIM}  is needed here.${OFF}"
  echo ""
}

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."

# --- what are we on? -------------------------------------------------------
. /etc/os-release 2>/dev/null || true
case "${ID:-}${ID_LIKE:-}" in
  *debian*|*ubuntu*) : ;;
  *) warn "This installer is written for Ubuntu/Debian. '${PRETTY_NAME:-unknown}'"
     warn "may work, but the package names are not guaranteed." ;;
esac

banner
say "This machine"
echo "    host   : $(hostname)"
echo "    distro : ${PRETTY_NAME:-unknown}"
echo "    cpus   : $(nproc 2>/dev/null || echo '?')   memory: $(awk '/MemTotal/{printf "%.1f GB", $2/1048576}' /proc/meminfo 2>/dev/null)   disk free: $(df -BG --output=avail / 2>/dev/null | tail -1 | tr -d ' G') GB"
echo ""

CPUS=$(nproc 2>/dev/null || echo 1)
MEM_GB=$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)
[ "$CPUS" -lt 2 ] && warn "2 vCPU is the practical minimum. Ansible and Terraform will be slow."
[ "$MEM_GB" -lt 4 ] && warn "${MEM_GB} GB of RAM. 4 GB is the minimum for Ansible plus Terraform."

# ===========================================================================
# The wizard
# ===========================================================================
#
# Four questions, because four things genuinely cannot be guessed. The
# Supervisor's address in particular: it is sometimes a private RFC1918 address
# reached over a site-to-site tunnel and sometimes a public one, and only the
# engineer standing there knows which applies at this customer.

EXISTING_HOST=""; EXISTING_PORT=""; EXISTING_NAME=""; EXISTING_TOKEN=""
if [ -f "$CONFIG" ]; then
  EXISTING_HOST=$(python3 -c "import json;print(json.load(open('$CONFIG')).get('supervisor_host',''))" 2>/dev/null || echo "")
  EXISTING_PORT=$(python3 -c "import json;print(json.load(open('$CONFIG')).get('supervisor_port',''))" 2>/dev/null || echo "")
  EXISTING_NAME=$(python3 -c "import json;print(json.load(open('$CONFIG')).get('worker_name',''))" 2>/dev/null || echo "")
  EXISTING_TOKEN=$(python3 -c "import json;print(json.load(open('$CONFIG')).get('token',''))" 2>/dev/null || echo "")
  say "Found an existing configuration — press Enter to keep each value."
  echo ""
fi

# The wizard is the normal path, but provisioning a rack of workers by hand is
# not. Setting these environment variables skips the questions entirely, which
# is what an automated build or a config-management run uses:
#
# This file is public, so the example uses an RFC 5737 documentation address
# rather than a real one. Put your own Supervisor's address here.
#
#   PROSETH_SUPERVISOR_HOST=203.0.113.10 \
#   PROSETH_TOKEN=psw_... \
#   PROSETH_WORKER_NAME=acme-dc-01 \
#   PROSETH_NONINTERACTIVE=1 sudo -E ./install.sh
#
# Anything not set still gets asked for, so a half-filled environment falls back
# to the wizard rather than failing.
NONINTERACTIVE="${PROSETH_NONINTERACTIVE:-}"

ask() {
  # ask <prompt> <default> <varname> [secret]
  local prompt="$1" default="$2" __var="$3" secret="${4:-}" reply=""
  local shown="$default"
  [ -n "$secret" ] && [ -n "$default" ] && shown="(unchanged)"

  if [ -n "$NONINTERACTIVE" ]; then
    printf -v "$__var" '%s' "$default"
    [ -n "$secret" ] || echo "  $prompt: $default"
    return
  fi

  if [ -n "$default" ]; then
    printf "  %s ${DIM}[%s]${OFF}: " "$prompt" "$shown"
  else
    printf "  %s: " "$prompt"
  fi
  if [ -n "$secret" ]; then
    read -r reply; echo ""
  else
    read -r reply
  fi
  printf -v "$__var" '%s' "${reply:-$default}"
}

# Environment wins over whatever was in an existing config, so re-running with a
# new token actually changes it.
EXISTING_HOST="${PROSETH_SUPERVISOR_HOST:-$EXISTING_HOST}"
EXISTING_PORT="${PROSETH_SUPERVISOR_PORT:-$EXISTING_PORT}"
EXISTING_NAME="${PROSETH_WORKER_NAME:-$EXISTING_NAME}"
EXISTING_TOKEN="${PROSETH_TOKEN:-$EXISTING_TOKEN}"

say "${BOLD}Supervisor${OFF}"
echo "${DIM}    The address this worker should connect to. It can be private or${OFF}"
echo "${DIM}    public — whatever this machine can actually reach.${OFF}"
ask "Supervisor IP or hostname" "$EXISTING_HOST" SUP_HOST
[ -n "$SUP_HOST" ] || die "The Supervisor address is required."

ask "Port" "${EXISTING_PORT:-$DEFAULT_PORT}" SUP_PORT
case "$SUP_PORT" in (''|*[!0-9]*) die "'$SUP_PORT' is not a port number." ;; esac

if [ -n "$NONINTERACTIVE" ]; then
  USE_TLS="${PROSETH_TLS:-false}"
else
  printf "  Use TLS (wss)? ${DIM}[y/N]${OFF}: "
  read -r USE_TLS
fi
case "${USE_TLS,,}" in y|yes|true) USE_TLS=true ;; *) USE_TLS=false ;; esac

echo ""
say "${BOLD}Identity${OFF}"
echo "${DIM}    Create the worker in the Supervisor first (Workers → Add worker).${OFF}"
echo "${DIM}    That gives you a token; paste it here. It is shown only once.${OFF}"
ask "Worker name (as you named it there)" "${EXISTING_NAME:-$(hostname -s)}" WORKER_NAME
ask "Token (psw_...)" "$EXISTING_TOKEN" WORKER_TOKEN secret
[ -n "$WORKER_TOKEN" ] || die "The token is required. Get it from Workers → Add worker."
case "$WORKER_TOKEN" in
  psw_*) : ;;
  *) die "That does not look like a Proseth token — they start with 'psw_'." ;;
esac

# --- prove it can get there BEFORE installing anything ---------------------
# Finding out the port is blocked after laying down a service and a user is a
# worse experience than finding out now, and this is the single most common
# thing to go wrong at a customer site.
echo ""
say "Checking this machine can reach $SUP_HOST:$SUP_PORT"
if command -v timeout >/dev/null 2>&1 && \
   timeout 8 bash -c "exec 3<>/dev/tcp/$SUP_HOST/$SUP_PORT" 2>/dev/null; then
  ok "the port answered"
else
  warn "could not open $SUP_HOST:$SUP_PORT from here"
  echo ""
  echo "    That usually means one of:"
  echo "      - a firewall between here and the Supervisor blocks outbound $SUP_PORT"
  echo "      - the Supervisor is not running, or is on a different port"
  echo "      - the address is wrong for this network"
  echo ""
  if [ -n "$NONINTERACTIVE" ]; then
    warn "carrying on anyway (non-interactive) - the agent will keep retrying"
  else
    printf "  Carry on anyway? The agent will keep retrying. ${DIM}[y/N]${OFF}: "
    read -r CARRY
    case "${CARRY,,}" in y|yes) : ;; *) die "Nothing was changed." ;; esac
  fi
fi

# ===========================================================================
# Install
# ===========================================================================

echo ""
say "Installing packages (this is the slow part)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
# python3-venv is separate from python3 on Ubuntu and its absence is a confusing
# failure three steps later. openssh-client is what paramiko shells out to for
# some key formats. The rest is what the jobs need.
if ! apt-get install -y -qq python3 python3-venv python3-pip openssh-client \
     ca-certificates curl unzip git >/dev/null 2>&1; then
  die "Package installation failed. Check this machine can reach the Ubuntu archives."
fi
ok "base packages"

say "Installing Ansible"
if command -v ansible-playbook >/dev/null 2>&1; then
  ok "already present: $(ansible-playbook --version 2>/dev/null | head -1)"
else
  # The distro package rather than pip: it is what the customer's own team will
  # recognise, it is patched by unattended-upgrades, and it does not fight the
  # agent's venv.
  if apt-get install -y -qq ansible >/dev/null 2>&1; then
    ok "$(ansible-playbook --version 2>/dev/null | head -1)"
  else
    warn "Ansible could not be installed from apt. Playbook jobs will fail"
    warn "until it is. Try: sudo apt install ansible"
  fi
fi

say "Installing Terraform"
if command -v terraform >/dev/null 2>&1; then
  ok "already present: $(terraform version 2>/dev/null | head -1)"
else
  TF_VER="1.9.8"
  ARCH=$(dpkg --print-architecture 2>/dev/null || echo amd64)
  case "$ARCH" in amd64|arm64) : ;; *) ARCH="amd64" ;; esac
  TMP=$(mktemp -d)
  if curl -fsSL -o "$TMP/tf.zip" \
       "https://releases.hashicorp.com/terraform/${TF_VER}/terraform_${TF_VER}_linux_${ARCH}.zip" \
     && unzip -qo "$TMP/tf.zip" -d /usr/local/bin/ 2>/dev/null; then
    chmod 755 /usr/local/bin/terraform
    ok "$(terraform version 2>/dev/null | head -1)"
  else
    warn "Terraform could not be downloaded. Cloud jobs will fail until it is"
    warn "installed. This machine may have no internet route."
  fi
  rm -rf "$TMP"
fi

# A worker that builds a Kubernetes cluster is also the only machine that can
# afterwards REACH it: the API endpoint is a private address inside this
# network. So kubectl and helm belong here, not on the supervisor - the console
# in the platform runs them through this agent.
#
# The upstream Google apt repository rather than snap: snapd is absent or
# deliberately disabled on plenty of server builds, and a `snap install` that
# fails on a minimal image is a confusing way to lose kubectl.
say "Installing kubectl and helm"
if command -v kubectl >/dev/null 2>&1; then
  ok "already present: $(kubectl version --client 2>/dev/null | head -1)"
else
  ARCH=$(dpkg --print-architecture 2>/dev/null || echo amd64)
  case "$ARCH" in amd64|arm64) : ;; *) ARCH="amd64" ;; esac
  # Track the latest stable release rather than pinning: kubectl is supported
  # one minor version either side of the server, so a pin here would be wrong
  # for some cluster within a year, whereas stable is right for almost all.
  KVER=$(curl -fsSL https://dl.k8s.io/release/stable.txt 2>/dev/null || true)
  if [ -n "$KVER" ] && curl -fsSL -o /usr/local/bin/kubectl \
       "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl"; then
    chmod 755 /usr/local/bin/kubectl
    ok "$(kubectl version --client 2>/dev/null | head -1)"
  else
    rm -f /usr/local/bin/kubectl
    warn "kubectl could not be downloaded. The cluster console will say so"
    warn "until it is installed. This machine may have no internet route."
  fi
fi
if command -v helm >/dev/null 2>&1; then
  ok "already present: $(helm version --short 2>/dev/null | head -1)"
else
  # Helm's own installer, which picks the right architecture itself. Piping a
  # script from the internet into bash is exactly what the platform's risk
  # scanner flags, and it is noted here deliberately: this is Helm's documented
  # install path, it runs once at install time under an operator who is already
  # root, and the alternative is a version pin that goes stale.
  if curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
       | bash >/dev/null 2>&1; then
    ok "$(helm version --short 2>/dev/null | head -1)"
  else
    warn "helm could not be installed. Add-on jobs that use it will fail."
  fi
fi

# --- the agent -------------------------------------------------------------
say "Installing the agent"
id -u "$RUN_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$RUN_USER"
# An existing install may have the old home; move it so Ansible works.
usermod --home "$STATE_DIR" "$RUN_USER" >/dev/null 2>&1 || true
install -d -m 755 "$PREFIX"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR/.ssh"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR/.ansible"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR/.ansible/tmp"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR/.proseth"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -rf "$PREFIX/proseth_worker"
cp -r "$SRC/proseth_worker" "$PREFIX/proseth_worker"
chown -R root:root "$PREFIX/proseth_worker"

if [ ! -x "$PREFIX/venv/bin/python" ]; then
  python3 -m venv "$PREFIX/venv" || die "Could not create the Python venv."
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
if ! "$PREFIX/venv/bin/pip" install --quiet \
     "websockets>=12" "paramiko>=3" "netmiko>=4.3" >/dev/null 2>&1; then
  die "Could not install the Python dependencies. Check this machine can reach PyPI."
fi
ok "agent and its dependencies"

# --- configuration ---------------------------------------------------------
install -d -m 750 -o root -g "$RUN_USER" "$CONFIG_DIR"
cat > "$CONFIG" <<JSON
{
  "worker_name": "$WORKER_NAME",
  "supervisor_host": "$SUP_HOST",
  "supervisor_port": $SUP_PORT,
  "tls": $USE_TLS,
  "verify_tls": true,
  "token": "$WORKER_TOKEN"
}
JSON
# The token is a credential for this whole worker. Readable by the service
# account and nobody else.
chown root:"$RUN_USER" "$CONFIG"
chmod 640 "$CONFIG"
ok "configuration written to $CONFIG"

# --- launcher and service --------------------------------------------------
cat > /usr/local/bin/proseth-worker <<LAUNCH
#!/bin/sh
exec $PREFIX/venv/bin/python -m proseth_worker.agent "\$@"
LAUNCH
chmod 755 /usr/local/bin/proseth-worker

cat > /usr/local/bin/proseth-worker-setup <<SETUP
#!/bin/sh
# Re-run the wizard to change the Supervisor address or the token.
exec $PREFIX/install.sh "\$@"
SETUP
chmod 755 /usr/local/bin/proseth-worker-setup
cp "$SRC/install.sh" "$PREFIX/install.sh"
chmod 755 "$PREFIX/install.sh"
[ -d "$SRC/proseth_worker" ] && cp -r "$SRC/proseth_worker" "$PREFIX/" 2>/dev/null

cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Proseth Worker - deploy agent
Documentation=https://github.com/proseth/proseth-worker
After=network-online.target
Wants=network-online.target
# In [Unit], not [Service]. systemd moved it years ago and logs
# "Unknown key 'StartLimitIntervalSec' in section [Service], ignoring" if it is
# in the wrong one - which means the limit silently is not applied.
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$STATE_DIR
Environment=PYTHONPATH=$PREFIX
Environment=PYTHONUNBUFFERED=1
Environment=HOME=$STATE_DIR
Environment=PROSETH_STATE_DIR=$STATE_DIR
ExecStart=$PREFIX/venv/bin/python -m proseth_worker.agent
# Always come back. A worker that gives up is one somebody has to drive to a
# customer site to restart.
Restart=always
RestartSec=5
# The agent closes its socket on SIGTERM and exits in under a second. Without
# that it sat in its receive loop until systemd's 90s stop timeout and was
# SIGKILLed on every restart.
TimeoutStopSec=20

# It runs jobs against the customer's network, so it gets no more of this
# machine than it needs. NoNewPrivileges is off deliberately: some jobs use
# sudo on THIS host, which it would block.
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$STATE_DIR /tmp

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
ok "systemd service installed and started"

# --- did it work? ----------------------------------------------------------
echo ""
say "Waiting for it to connect"
CONNECTED=0
for _ in $(seq 1 15); do
  sleep 1
  if journalctl -u "$SERVICE" --since "-60s" --no-pager 2>/dev/null \
     | grep -q "Connected as"; then
    CONNECTED=1
    break
  fi
  if journalctl -u "$SERVICE" --since "-60s" --no-pager 2>/dev/null \
     | grep -q "refused this worker"; then
    break
  fi
done

echo ""
if [ "$CONNECTED" = "1" ]; then
  echo "${GREEN}${BOLD}  Connected.${OFF}"
  echo ""
  echo "  '$WORKER_NAME' should now show as ${GREEN}online${OFF} on the Workers page."
  echo "  It will reconnect by itself after a reboot or a network outage."
else
  echo "${YELLOW}${BOLD}  Installed, but it has not connected yet.${OFF}"
  echo ""
  echo "  The agent keeps retrying, so this may simply be slow. To look:"
  echo "    ${DIM}journalctl -u $SERVICE -f${OFF}"
fi

echo ""
echo "${DIM}  Useful commands${OFF}"
echo "    systemctl status $SERVICE     ${DIM}is it running${OFF}"
echo "    journalctl -u $SERVICE -f     ${DIM}watch what it is doing${OFF}"
echo "    proseth-worker --check        ${DIM}what this worker is${OFF}"
echo "    sudo proseth-worker-setup     ${DIM}change the address or token${OFF}"
echo ""
