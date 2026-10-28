#!/usr/bin/env bash
#
# deploy/install-services.sh — install systemd units from the deploy/*.service
# TEMPLATES, substituting THIS machine's real repo path and service user.
#
# The templates ship with placeholders (`youruser`,
# `/home/youruser/novus-n20k48-ble`) so they stay generic in the public repo.
# Copying them verbatim fails with status=217/USER — this script fills in the
# real values instead. Run it as root:
#
#     sudo bash deploy/install-services.sh                 # dashboard only
#     sudo bash deploy/install-services.sh --with-api      # + open-data API
#     sudo bash deploy/install-services.sh --root          # run services as root
#     sudo bash deploy/install-services.sh --enable        # enable + start now
#
# Service user defaults to the OWNER of the repo directory (so files stay
# owned correctly). Use --user=NAME to override, or --root to run as root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SVC_USER=""
RUN_AS_ROOT=0
ENABLE=0
SERVICES=("kiln-dashboard")
for a in "$@"; do
  case "$a" in
    --user=*) SVC_USER="${a#--user=}" ;;
    --root) RUN_AS_ROOT=1 ;;
    --enable) ENABLE=1 ;;
    --with-api) SERVICES+=("kiln-api") ;;
    --with-analysis) SERVICES+=("kiln-analysis") ;;
    -h|--help)
      sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (try --help)"; exit 1 ;;
  esac
done

if [ "$RUN_AS_ROOT" = 0 ] && [ -z "$SVC_USER" ]; then
  SVC_USER="$(stat -c '%U' "$ROOT")"
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo bash deploy/install-services.sh …)"; exit 1
fi

for name in "${SERVICES[@]}"; do
  tmpl="$ROOT/deploy/${name}.service"
  if [ ! -f "$tmpl" ]; then
    echo "skip: no template $tmpl"; continue
  fi
  dst="/etc/systemd/system/${name}.service"
  tmp="$(mktemp)"
  # strip the placeholder-warning header, then substitute path + user
  grep -v '^# !!! DO NOT copy\|^# .youruser\|^# as-is fails\|^# machine.s real path' "$tmpl" \
    | sed -e "s#/home/youruser/novus-n20k48-ble#${ROOT}#g" > "$tmp"
  if [ "$RUN_AS_ROOT" = 1 ]; then
    sed -i '/^User=/d; /^Group=/d' "$tmp"          # no User= => runs as root
    who="root"
  else
    sed -i "s/^User=.*/User=${SVC_USER}/; s/^Group=.*/Group=${SVC_USER}/" "$tmp"
    who="$SVC_USER"
  fi
  cp "$tmp" "$dst"; rm -f "$tmp"
  echo "installed ${dst}  (dir=${ROOT}, user=${who})"
done

systemctl daemon-reload
echo "systemctl daemon-reload done."

if [ "$ENABLE" = 1 ]; then
  for name in "${SERVICES[@]}"; do
    [ -f "/etc/systemd/system/${name}.service" ] || continue
    if systemctl enable --now "$name"; then
      echo "enabled + started ${name}"
    else
      echo "WARN: could not enable ${name} — check: systemctl status ${name}"
    fi
  done
fi

echo
echo "Done. Verify with:  systemctl status ${SERVICES[0]} --no-pager"
echo "If a service fails to start, check its user has BLE access (bluetooth"
echo "group) or re-run with --root."
