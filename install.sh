#!/usr/bin/env bash
#
# install.sh — one command to install OR update the Novus kiln dashboard.
#
# Fresh machine (Ubuntu/Debian with a Bluetooth adapter):
#     curl -fsSL https://raw.githubusercontent.com/alibama/novus-controller/main/install.sh | bash
#
# Inside a checkout (same thing, uses the checkout you're standing in):
#     bash install.sh
#
# Re-running it later is how you update: it pulls, reinstalls deps if needed,
# refreshes the systemd units, and hands off to deploy/update.sh, which
# validates the new code (byte-compile + tests) BEFORE restarting anything.
#
# Flags:
#   --no-services   venv + deps + secrets only (dev laptops, no systemd)
#   --no-api        skip the open-data API service (kiln-api on :8000)
#   --dir=PATH      install/update in PATH (default: $HOME/novus-controller,
#                   or the checkout you run it from)
#   -h | --help
#
# Environment overrides:
#   NOVUS_DIR      same as --dir
#   NOVUS_REPO     git URL to clone (default github.com/alibama/novus-controller)
#   NOVUS_BRANCH   branch to clone on a fresh install (default main)
#
# Safe to run repeatedly. Never overwrites an existing password, token,
# notify.env, device list, or logs.
set -euo pipefail

NOVUS_REPO="${NOVUS_REPO:-https://github.com/alibama/novus-controller.git}"
NOVUS_BRANCH="${NOVUS_BRANCH:-main}"
DIR_EXPLICIT=0
[ -n "${NOVUS_DIR:-}" ] && DIR_EXPLICIT=1
WITH_SERVICES=1
WITH_API=1

for a in "$@"; do
  case "$a" in
    --no-services) WITH_SERVICES=0 ;;
    --no-api)      WITH_API=0 ;;
    --dir=*)       NOVUS_DIR="${a#--dir=}"; DIR_EXPLICIT=1 ;;
    -h|--help)     sed -n '3,30p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (try --help)" >&2; exit 1 ;;
  esac
done

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

is_checkout() { [ -f "$1/kiln_dashboard.py" ] && [ -d "$1/deploy" ]; }

# ---- 0. where are we, and who are we ---------------------------------------
[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux (BlueZ). On macOS/Windows use the Docker path or run the app by hand (see README)."

if [ "$DIR_EXPLICIT" = 0 ]; then
  if is_checkout "$PWD"; then
    NOVUS_DIR="$PWD"
  elif [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
       && is_checkout "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; then
    NOVUS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  else
    NOVUS_DIR="$HOME/novus-controller"
  fi
fi

SUDO=""
HAVE_SUDO=1
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  warn "running as root: the services will run as root and files land in $NOVUS_DIR."
  warn "(Running as your normal user is recommended: curl ... | bash, no sudo.)"
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  HAVE_SUDO=0
fi

say "Novus kiln dashboard installer"
note "directory: $NOVUS_DIR"

# ---- 1. OS packages ----------------------------------------------------------
say "Checking OS packages (git, python3, venv, pip, bluez)"
PKGS="git python3 python3-venv python3-pip bluez"
if command -v apt-get >/dev/null 2>&1; then
  MISSING=""
  for p in $PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
  done
  if [ -z "$MISSING" ]; then
    note "all present."
  elif [ "$HAVE_SUDO" = 1 ]; then
    note "installing:$MISSING"
    # shellcheck disable=SC2086
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $MISSING \
      || warn "apt-get failed; install these by hand and re-run:$MISSING"
  else
    warn "no sudo available. Please install:  apt-get install$MISSING"
  fi
else
  note "no apt-get here; make sure these are installed: $PKGS"
fi
command -v git >/dev/null 2>&1     || die "git is not installed."
command -v python3 >/dev/null 2>&1 || die "python3 is not installed."

# ---- 2. get / refresh the code ----------------------------------------------
PULLED=0
if [ -d "$NOVUS_DIR/.git" ]; then
  say "Updating checkout in $NOVUS_DIR"
  cd "$NOVUS_DIR"
  BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
  if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    warn "you have local edits in tracked files, so I am NOT pulling."
    warn "commit or stash them, then re-run to update. Continuing with what's here."
  elif ! git remote get-url origin >/dev/null 2>&1; then
    warn "no 'origin' remote; skipping pull."
  else
    if git fetch --prune --quiet && git pull --ff-only --quiet; then
      AFTER="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
      if [ "$BEFORE" != "$AFTER" ]; then
        note "updated $BEFORE -> $AFTER"; PULLED=1
      else
        note "already up to date ($AFTER)."
      fi
    else
      warn "git pull failed (offline, or the branch has diverged). Continuing with the current code."
    fi
  fi
elif is_checkout "$NOVUS_DIR"; then
  say "Using existing (non-git) copy in $NOVUS_DIR"
  cd "$NOVUS_DIR"
  warn "this is not a git checkout, so the one-liner can't update it later."
else
  say "Cloning $NOVUS_REPO ($NOVUS_BRANCH) into $NOVUS_DIR"
  git clone --quiet --branch "$NOVUS_BRANCH" "$NOVUS_REPO" "$NOVUS_DIR"
  cd "$NOVUS_DIR"
  note "cloned $(git rev-parse --short HEAD)."
fi
is_checkout "$NOVUS_DIR" || die "$NOVUS_DIR does not look like a novus-controller checkout."

# ---- 3. Python venv + dependencies ------------------------------------------
say "Python environment (.venv)"
VENV="$NOVUS_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  note "creating virtualenv..."
  python3 -m venv "$VENV" || die "python3 -m venv failed. On Debian/Ubuntu:  sudo apt-get install python3-venv"
fi
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
note "installing dependencies (first run takes a minute or two)..."
"$PIP" install -q --upgrade pip >/dev/null 2>&1 || true
# pytest + httpx so deploy/update.sh can run the test gate before restarts.
"$PIP" install -q -r requirements.txt -r requirements-api.txt pytest httpx \
  || die "pip install failed. Check your network and re-run."
# Record the requirements hash the way update.sh does, so it won't reinstall.
mkdir -p deploy/logs
cat requirements.txt requirements-api.txt | sha256sum | cut -d' ' -f1 > deploy/logs/.reqhash
note "ok."

# ---- 4. secrets (never overwritten) ------------------------------------------
say "Secrets"
NEW_PASSWORD=""
NEW_TOKEN=0
mkdir -p .streamlit
if [ ! -f .streamlit/secrets.toml ]; then
  NEW_PASSWORD="$("$PY" -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(12)))')"
  ( umask 077; printf 'dashboard_password = "%s"\n' "$NEW_PASSWORD" > .streamlit/secrets.toml )
  note "created .streamlit/secrets.toml with a new dashboard password."
else
  note ".streamlit/secrets.toml exists; keeping your password."
fi
chmod 600 .streamlit/secrets.toml 2>/dev/null || true

if [ ! -f api.env ]; then
  TOKEN="$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
  ( umask 077; {
      echo "# Generated by install.sh. See api.env.example for what each setting does."
      echo "# Bearer token required for the API's POST (write) endpoints."
      echo "API_WRITE_TOKEN=$TOKEN"
      echo "# Public https URL the API is reachable at (e.g. a Tailscale Funnel URL)."
      echo "# API_PUBLIC_URL="
      echo "# 1 = token holders may queue run/stop/setpoint commands; 0 = read-only."
      echo "# API_ALLOW_CONTROL=1"
    } > api.env )
  NEW_TOKEN=1
  note "created api.env with a new API_WRITE_TOKEN."
else
  note "api.env exists; keeping it."
fi
chmod 600 api.env 2>/dev/null || true
[ -f notify.env ] || note "no notify.env yet (optional): copy notify.env.example to notify.env for Telegram/ntfy/SMS alerts."

# ---- 5. Bluetooth group ------------------------------------------------------
say "Bluetooth access"
ME="$(id -un)"
if [ "$(id -u)" -eq 0 ]; then
  note "root has BLE access already."
elif ! getent group bluetooth >/dev/null 2>&1; then
  note "no 'bluetooth' group on this system; skipping."
elif id -nG "$ME" | tr ' ' '\n' | grep -qx bluetooth; then
  note "$ME is already in the bluetooth group."
elif [ "$HAVE_SUDO" = 1 ]; then
  if $SUDO usermod -aG bluetooth "$ME" 2>/dev/null; then
    note "added $ME to the bluetooth group (takes effect for new logins; services are unaffected)."
  else
    warn "could not add $ME to the bluetooth group (sudo usermod -aG bluetooth $ME)."
  fi
else
  warn "run this once as root:  usermod -aG bluetooth $ME"
fi

# ---- 6. systemd services -----------------------------------------------------
SERVICES_STATE="skipped"
UPDATE_LOG=""
DASH_UNIT=/etc/systemd/system/kiln-dashboard.service
if [ "$WITH_SERVICES" = 1 ]; then
  say "Services (systemd)"
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found; skipping services. Run by hand: $VENV/bin/python run_dashboard.py"
  elif [ "$HAVE_SUDO" = 0 ] && [ "$(id -u)" -ne 0 ]; then
    warn "no sudo; skipping services. As root, run:  bash deploy/install-services.sh --with-api --enable"
  else
    SVC_ARGS=()
    [ "$WITH_API" = 1 ] && SVC_ARGS+=(--with-api)
    if [ -f "$DASH_UNIT" ]; then
      # Existing install: refresh the unit files (in case the templates changed),
      # make sure they're enabled, then let update.sh do a VALIDATED restart.
      note "services already installed; refreshing unit files..."
      $SUDO bash deploy/install-services.sh ${SVC_ARGS[@]+"${SVC_ARGS[@]}"} >/dev/null \
        || warn "install-services.sh failed; the old unit files stay in place."
      $SUDO systemctl enable -q kiln-dashboard 2>/dev/null || true
      [ "$WITH_API" = 1 ] && { $SUDO systemctl enable -q kiln-api 2>/dev/null || true; }
      note "validating + restarting via deploy/update.sh..."
      UPD_ARGS=()
      [ "$WITH_API" = 1 ] && UPD_ARGS+=(--api)
      if bash deploy/update.sh ${UPD_ARGS[@]+"${UPD_ARGS[@]}"}; then
        SERVICES_STATE="restarted"
      else
        SERVICES_STATE="update.sh FAILED (services were left as they were)"
      fi
      UPDATE_LOG="$NOVUS_DIR/deploy/logs/latest.log"
    else
      note "installing and starting services..."
      if $SUDO bash deploy/install-services.sh ${SVC_ARGS[@]+"${SVC_ARGS[@]}"} --enable; then
        SERVICES_STATE="installed"
      else
        SERVICES_STATE="install-services.sh FAILED"
      fi
    fi
  fi
fi

# ---- 7. summary --------------------------------------------------------------
HOST="$(hostname 2>/dev/null || echo localhost)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"

echo
echo "================================================================"
echo "  Novus kiln dashboard: done."
echo "================================================================"
echo "  Dashboard:   http://$HOST:8501"
[ -n "$LAN_IP" ] && echo "               http://$LAN_IP:8501"
if [ "$WITH_API" = 1 ] && [ "$WITH_SERVICES" = 1 ]; then
  echo "  Open-data API: http://$HOST:8000   (docs at /docs)"
fi
echo
if [ -n "$NEW_PASSWORD" ]; then
  echo "  Dashboard password:  $NEW_PASSWORD"
  echo "    (shown once; it lives in $NOVUS_DIR/.streamlit/secrets.toml - edit to change)"
else
  echo "  Dashboard password:  unchanged, in $NOVUS_DIR/.streamlit/secrets.toml"
fi
if [ "$NEW_TOKEN" = 1 ]; then
  echo "  API write token:     new, in $NOVUS_DIR/api.env"
else
  echo "  API write token:     unchanged, in $NOVUS_DIR/api.env"
fi
echo
if [ "$WITH_SERVICES" = 1 ]; then
  case "$SERVICES_STATE" in
    installed) echo "  Services:  kiln-dashboard$( [ "$WITH_API" = 1 ] && echo ' + kiln-api') installed, enabled at boot, running." ;;
    restarted) echo "  Services:  validated and restarted.  Log: $UPDATE_LOG" ;;
    skipped)   echo "  Services:  not set up (see warnings above)." ;;
    *)         echo "  Services:  $SERVICES_STATE"; [ -n "$UPDATE_LOG" ] && echo "             Log: $UPDATE_LOG" ;;
  esac
  echo "  Logs:      journalctl -u kiln-dashboard -f"
  echo "  Status:    systemctl status kiln-dashboard"
else
  echo "  Run it:    cd $NOVUS_DIR && .venv/bin/python run_dashboard.py"
  echo "  API:       cd $NOVUS_DIR && .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000"
fi
echo
echo "  Update:    re-run the same command any time:"
echo "             curl -fsSL https://raw.githubusercontent.com/alibama/novus-controller/main/install.sh | bash"
echo "             (or: cd $NOVUS_DIR && bash install.sh)"
echo
echo "  First launch: open the dashboard, go to Settings -> Find controllers,"
echo "  scan, add your devices, set each one's role."
echo "================================================================"
