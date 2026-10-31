#!/bin/bash
set -Eeuo pipefail

# bastion の Compute（bastion.tf。#2670）の cloud-init の user_data。Oracle Linux 9。
# root の filesystem を広げ、GNOME と TigerVNC（:1 = TCP 5901）を入れて firewalld で開け、Google Chrome を入れる。
# VNC のパスワードは instance metadata の bastion_vnc_password_b64（stack の入力 bastion_vnc_password）から読み、
# このファイルには書かない。cloud-init は "#!" で始まる user_data を shell script として実行するため、
# 1 行目の shebang の前に空行・BOM を入れない（LF の改行のまま）。

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

exec > >(tee -a /var/log/bastion-init.log) 2>&1

VNC_USER="opc"
VNC_DISPLAY="1"
VNC_PORT="$((5900 + VNC_DISPLAY))"
VNC_GEOMETRY="1280x1024"
METADATA_URL="http://169.254.169.254/opc/v2/instance/metadata"

log() {
  echo "[$(date -Is)] $*"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    log "ERROR: this script must run as root"
    exit 1
  fi
}

grow_root_filesystem() {
  # Oracle Linux 9 の image の root は boot volume を大きくしても約 30 GB のまま（#1984 と同じ）。
  if [ ! -x /usr/libexec/oci-growfs ]; then
    dnf -y install oci-utils || true
  fi
  if [ -x /usr/libexec/oci-growfs ]; then
    /usr/libexec/oci-growfs -y || true
  fi
  df -hT / || true
}

read_vnc_password() {
  local encoded=""
  for _ in $(seq 1 30); do
    if encoded="$(curl -fsS -H "Authorization: Bearer Oracle" "${METADATA_URL}/bastion_vnc_password_b64")"; then
      break
    fi
    sleep 2
  done
  if [ -z "${encoded}" ]; then
    log "ERROR: instance metadata bastion_vnc_password_b64 was not found"
    exit 1
  fi
  VNC_PASS="$(printf '%s' "${encoded}" | base64 -d)"
  if [ "${#VNC_PASS}" -ne 8 ]; then
    log "ERROR: the VNC password must be exactly 8 characters"
    exit 1
  fi
}

install_packages() {
  log "Installing GNOME and TigerVNC"

  dnf -y makecache || true

  if ! dnf -y groupinstall "Server with GUI"; then
    log "WARN: dnf groupinstall failed, trying alternate syntax"
    dnf -y group install "Server with GUI" || true
  fi

  dnf -y install \
    tigervnc-server \
    tigervnc-server-module \
    firewalld \
    policycoreutils \
    policycoreutils-python-utils || {
      log "ERROR: required package installation failed"
      exit 1
    }

  # repository によっては無い package。
  dnf -y install tigervnc-selinux || log "WARN: optional package tigervnc-selinux was not installed"
  # 3 製品の画面（日本語）を Chrome で表示するための font。
  dnf -y install google-noto-sans-cjk-ttc-fonts || log "WARN: optional package google-noto-sans-cjk-ttc-fonts was not installed"
}

install_chrome() {
  log "Installing Google Chrome"

  cat > /etc/yum.repos.d/google-chrome.repo <<'REPOEOF'
[google-chrome]
name=google-chrome
baseurl=https://dl.google.com/linux/chrome/rpm/stable/x86_64
enabled=1
gpgcheck=1
gpgkey=https://dl.google.com/linux/linux_signing_key.pub
REPOEOF

  rpm --import https://dl.google.com/linux/linux_signing_key.pub || log "WARN: failed to import the Google signing key"
  if dnf -y install google-chrome-stable; then
    log "OK: $(google-chrome --version 2>/dev/null || echo google-chrome-stable installed)"
  else
    log "WARN: google-chrome-stable was not installed (check the egress to dl.google.com)"
  fi
}

configure_gdm() {
  log "Configuring GDM to disable Wayland"
  mkdir -p /etc/gdm

  if [ ! -f /etc/gdm/custom.conf ]; then
    cat > /etc/gdm/custom.conf <<'GDMEOF'
[daemon]
WaylandEnable=false
GDMEOF
    return 0
  fi

  if grep -q '^\[daemon\]' /etc/gdm/custom.conf; then
    if grep -q '^#\?WaylandEnable=' /etc/gdm/custom.conf; then
      sed -i 's/^#\?WaylandEnable=.*/WaylandEnable=false/' /etc/gdm/custom.conf
    else
      sed -i '/^\[daemon\]/a WaylandEnable=false' /etc/gdm/custom.conf
    fi
  else
    cat >> /etc/gdm/custom.conf <<'GDMEOF'

[daemon]
WaylandEnable=false
GDMEOF
  fi
}

configure_vnc_password() {
  log "Configuring VNC password of ${VNC_USER}"

  local vnc_home
  vnc_home="$(getent passwd "${VNC_USER}" | cut -d: -f6)"
  if [ -z "${vnc_home}" ] || [ ! -d "${vnc_home}" ]; then
    log "ERROR: home directory for ${VNC_USER} was not found"
    exit 1
  fi

  install -d -m 700 -o "${VNC_USER}" -g "${VNC_USER}" "${vnc_home}/.vnc"
  # TigerVNC は先頭の 8 文字だけを使う。
  printf '%s\n' "${VNC_PASS}" | vncpasswd -f > "${vnc_home}/.vnc/passwd"
  chown "${VNC_USER}:${VNC_USER}" "${vnc_home}/.vnc/passwd"
  chmod 600 "${vnc_home}/.vnc/passwd"
  restorecon -RF "${vnc_home}/.vnc" || true
}

configure_tigervnc() {
  log "Configuring TigerVNC display :${VNC_DISPLAY} for ${VNC_USER}"

  install -d -m 755 /etc/tigervnc
  touch /etc/tigervnc/vncserver.users
  sed -i "/^:${VNC_DISPLAY}=/d" /etc/tigervnc/vncserver.users
  echo ":${VNC_DISPLAY}=${VNC_USER}" >> /etc/tigervnc/vncserver.users

  cat > /etc/tigervnc/vncserver-config-defaults <<VNCEOF
session=gnome
geometry=${VNC_GEOMETRY}
localhost=no
alwaysshared
VNCEOF

  mkdir -p /etc/systemd/system/vncserver@.service.d
  cat > /etc/systemd/system/vncserver@.service.d/10-restart.conf <<'SYSTEMDEOF'
[Service]
Restart=on-failure
RestartSec=5s
SYSTEMDEOF
}

write_ensure_script() {
  log "Writing the VNC health-check helper"

  # 再起動のたびに VNC の起動と firewalld の 5901 を確かめる（ensure-vnc.service）。
  cat > /usr/local/sbin/ensure-vnc.sh <<'ENSUREEOF'
#!/bin/bash
set -Eeuo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

VNC_DISPLAY="${1:-1}"
VNC_PORT="$((5900 + VNC_DISPLAY))"

log() {
  echo "[ensure-vnc $(date -Is)] $*"
}

run_timeout() {
  local seconds="$1"
  shift
  timeout "${seconds}" "$@" || {
    local rc=$?
    log "WARN: command failed or timed out rc=${rc}: $*"
    return 0
  }
}

log "Ensuring TigerVNC display :${VNC_DISPLAY} and TCP ${VNC_PORT}"

systemctl daemon-reload || true
systemctl enable "vncserver@:${VNC_DISPLAY}.service" >/dev/null 2>&1 || true
systemctl restart "vncserver@:${VNC_DISPLAY}.service" >/dev/null 2>&1 || true

if command -v firewall-cmd >/dev/null 2>&1; then
  systemctl enable --now firewalld >/dev/null 2>&1 || true

  for _ in $(seq 1 30); do
    if timeout 3 firewall-cmd --state >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  run_timeout 15 firewall-cmd --permanent --add-port="${VNC_PORT}/tcp"
  run_timeout 15 firewall-cmd --reload

  if systemctl is-active --quiet firewalld; then
    if ! timeout 5 firewall-cmd --query-port="${VNC_PORT}/tcp" >/dev/null 2>&1; then
      log "ERROR: firewalld is active but ${VNC_PORT}/tcp is not open"
      firewall-cmd --list-all || true
      exit 1
    fi
  fi
fi

for _ in $(seq 1 30); do
  if ss -ltn | grep -q ":${VNC_PORT}\b"; then
    log "OK: VNC is listening on TCP ${VNC_PORT}"
    exit 0
  fi
  sleep 2
done

log "ERROR: VNC is not listening on TCP ${VNC_PORT}"
systemctl status "vncserver@:${VNC_DISPLAY}.service" --no-pager -l || true
exit 1
ENSUREEOF

  chmod 755 /usr/local/sbin/ensure-vnc.sh
}

write_ensure_service() {
  log "Writing ensure-vnc.service"

  cat > /etc/systemd/system/ensure-vnc.service <<SERVICEEOF
[Unit]
Description=Ensure TigerVNC display :${VNC_DISPLAY} and TCP ${VNC_PORT} are available
After=network-online.target firewalld.service vncserver@:${VNC_DISPLAY}.service
Wants=network-online.target
Requires=vncserver@:${VNC_DISPLAY}.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ensure-vnc.sh ${VNC_DISPLAY}
RemainAfterExit=yes
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
SERVICEEOF
}

final_checks() {
  log "Final checks"
  systemctl status "vncserver@:${VNC_DISPLAY}.service" --no-pager -l || true
  systemctl status ensure-vnc.service --no-pager -l || true

  if ! ss -ltn | grep -q ":${VNC_PORT}\b"; then
    log "ERROR: ${VNC_PORT} is not listening"
    exit 1
  fi

  if systemctl is-active --quiet firewalld; then
    firewall-cmd --list-all || true
    if ! firewall-cmd --query-port="${VNC_PORT}/tcp" >/dev/null 2>&1; then
      log "ERROR: firewalld does not allow ${VNC_PORT}/tcp"
      exit 1
    fi
  fi
}

main() {
  require_root
  log "Bastion setup started"

  grow_root_filesystem
  read_vnc_password
  install_packages
  install_chrome
  systemctl set-default graphical.target || true
  configure_gdm
  configure_vnc_password
  configure_tigervnc
  write_ensure_script
  write_ensure_service

  systemctl daemon-reload
  systemctl enable "vncserver@:${VNC_DISPLAY}.service"
  systemctl enable ensure-vnc.service
  systemctl restart ensure-vnc.service

  final_checks
  log "Bastion setup completed"
}

main "$@"
