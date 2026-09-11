#!/usr/bin/env bash
#
# deploy/update.sh — bring the latest pulled code live, safely.
#
# Typical use:
#     git pull
#     bash deploy/update.sh
#
# What it does, in order:
#   1. records the current git commit (for rollback),
#   2. ensures the venv exists and installs deps IF requirements changed,
#   3. VALIDATES the new code (byte-compile + test suite) BEFORE touching any
#      service — if validation fails it aborts and the running version is left
#      untouched, so a broken pull never takes the furnace watchdog down,
#   4. restarts only the systemd services that actually exist,
#   5. health-checks them and, on failure, dumps the journal and (optionally)
#      rolls back,
#   6. writes everything to deploy/logs/update-<timestamp>.log (latest.log
#      always points at the newest), keeping the last 30.
#
# Options:
#   --pull                 run `git pull --ff-only` first (default: assume you
#                          already pulled)
#   --api                  also install requirements-api.txt (the open-data API)
#   --force-deps           reinstall deps even if requirements are unchanged
#   --rollback-on-failure  if a service won't come back, git-checkout the prior
#                          commit and restart it
#   -h | --help
#
# Service names default to: kiln-dashboard kiln-api kiln-analysis
# Override with:  KILN_SERVICES="my-dash my-api" bash deploy/update.sh
#
# Restarting kiln-dashboard briefly pauses the software watchdog (a few
# seconds). The controllers keep running their programs during that window;
# only the auto-recovery layer blinks. Avoid updating mid-recovery.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "cannot cd to repo root"; exit 1; }

# Optional persistent config, e.g. a line:  KILN_SERVICES="my-dash my-api"
# so you don't have to pass it each time.
[ -f "$ROOT/deploy/update.conf" ] && . "$ROOT/deploy/update.conf"

LOGDIR="$ROOT/deploy/logs"
mkdir -p "$LOGDIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOGDIR/update-$TS.log"
ln -sfn "update-$TS.log" "$LOGDIR/latest.log"

# mirror all output to the log file
exec > >(tee -a "$LOG") 2>&1

log()  { echo "[$(date +%H:%M:%S)] $*"; }
fail() {
  log "ERROR: $*"
  log "ABORTED — services were NOT restarted; the running version is unchanged."
  log "Full log: $LOG"
  exit 1
}

DO_PULL=0; FORCE_DEPS=0; WITH_API=0; ROLLBACK=0
for a in "$@"; do
  case "$a" in
    --pull) DO_PULL=1 ;;
    --api) WITH_API=1 ;;
    --force-deps) FORCE_DEPS=1 ;;
    --rollback-on-failure) ROLLBACK=1 ;;
    -h|--help)
      sed -n '3,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) log "unknown option: $a (try --help)" ;;
  esac
done

log "=== Kiln update $TS ==="
log "repo: $ROOT"

# ---- 1. record current commit ------------------------------------------------
if git rev-parse --git-dir >/dev/null 2>&1; then
  PREV_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
else
  PREV_COMMIT="unknown"
  log "note: not a git checkout — skipping commit tracking and --pull"
fi
log "current commit: $PREV_COMMIT"

if [ "$DO_PULL" = 1 ] && [ "$PREV_COMMIT" != "unknown" ]; then
  log "git pull --ff-only…"
  git pull --ff-only || fail "git pull failed (resolve conflicts / stash local edits, then retry)"
fi
NEW_COMMIT="$([ "$PREV_COMMIT" = unknown ] && echo unknown || git rev-parse --short HEAD)"
log "commit now:      $NEW_COMMIT"

# Your local config is gitignored (devices.json, notify.env, kiln_config.py,
# watchdog_settings.json, studio_settings.json, logs/) so a pull never touches
# it — device lists, thresholds, and history all survive updates.

# ---- 2. venv + dependencies --------------------------------------------------
VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  log "creating virtualenv (.venv)…"
  python3 -m venv "$VENV" || fail "could not create venv"
fi
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

REQ_HASH_FILE="$LOGDIR/.reqhash"
CUR_HASH="$(cat requirements.txt requirements-api.txt 2>/dev/null | sha256sum | cut -d' ' -f1)"
if [ "$FORCE_DEPS" = 1 ] || [ "$(cat "$REQ_HASH_FILE" 2>/dev/null || true)" != "$CUR_HASH" ]; then
  log "installing dependencies (requirements changed)…"
  "$PIP" install -q -r requirements.txt || fail "pip install requirements.txt failed"
  if [ "$WITH_API" = 1 ] || systemctl cat kiln-api >/dev/null 2>&1; then
    if [ -f requirements-api.txt ]; then
      "$PIP" install -q -r requirements-api.txt || fail "pip install requirements-api.txt failed"
    fi
  fi
  echo "$CUR_HASH" > "$REQ_HASH_FILE"
else
  log "dependencies unchanged — skipping (use --force-deps to reinstall)"
fi

# guard: a dependency install can pull in conflicting versions — surface it
if ! "$PY" -m pip check >/dev/null 2>&1; then
  log "WARNING: 'pip check' reports dependency conflicts in the venv:"
  "$PY" -m pip check 2>&1 | sed 's/^/      /'
  log "  (the app may fail to start — consider pinning versions in requirements.txt)"
fi

# ---- 3. VALIDATE before touching services ------------------------------------
log "byte-compiling (syntax check)…"
# shellcheck disable=SC2035
"$PY" -m compileall -q *.py pages/*.py tools/*.py 2>&1 \
  || fail "syntax error in the new code — refusing to restart"

if "$PY" -c "import pytest" >/dev/null 2>&1 && [ -d tests ]; then
  log "running test suite (safety gate)…"
  "$PY" -m pytest tests/ -q \
    || fail "tests failed on the new code — NOT restarting; the running version stays up"
  log "tests passed."
else
  log "pytest not installed in venv — skipping the test gate."
  log "  (recommend: $PIP install pytest fastapi httpx  — so future updates are gated)"
fi

# ---- 4. restart services that exist ------------------------------------------
CANDIDATES="${KILN_SERVICES:-kiln-dashboard kiln-api kiln-analysis}"
RESTARTED=()
for svc in $CANDIDATES; do
  if systemctl cat "$svc" >/dev/null 2>&1; then
    log "restarting ${svc}…"
    if sudo systemctl restart "$svc"; then
      RESTARTED+=("$svc")
    else
      log "WARN: 'systemctl restart ${svc}' returned non-zero"
    fi
  fi
done

# If none of the known names matched, DISCOVER the unit that actually runs this
# app — any service whose ExecStart references kiln_dashboard.py, uvicorn api,
# or this repo directory.
if [ "${#RESTARTED[@]}" -eq 0 ]; then
  log "no known service names matched — searching systemd for the app's unit…"
  while read -r unit; do
    [ -z "$unit" ] && continue
    es="$(systemctl show -p ExecStart --value "$unit" 2>/dev/null || true)"
    if printf '%s' "$es" | grep -qiE "kiln_dashboard\.py|uvicorn +api:app|${ROOT}"; then
      log "discovered service: ${unit} — restarting…"
      if sudo systemctl restart "$unit"; then
        RESTARTED+=("${unit%.service}")
      fi
    fi
  done < <(systemctl list-units --type=service --all --no-legend 2>/dev/null | awk '{print $1}')
  if [ "${#RESTARTED[@]}" -gt 0 ]; then
    log "TIP: set KILN_SERVICES=\"${RESTARTED[*]}\" (or in deploy/update.conf) to "
    log "     skip discovery next time."
  fi
fi

if [ "${#RESTARTED[@]}" -eq 0 ]; then
  # Nothing was restarted. Be honest about whether the app is up on OLD code or
  # down entirely — never report a bland success.
  if pgrep -af "streamlit run|uvicorn +api:app" >/dev/null 2>&1; then
    log "!! The app is RUNNING but under no unit I could restart, so it is still"
    log "!! on the OLD code. Restart it yourself so this update takes effect:"
    log "     sudo systemctl restart <your-service-name>   # then set KILN_SERVICES"
    log "   or, if you run it by hand, stop it and relaunch:"
    log "     $VENV/bin/streamlit run kiln_dashboard.py --server.port 8501 --server.address 127.0.0.1"
  else
    log "!! The app does NOT appear to be running. Start it now:"
    log "     $VENV/bin/streamlit run kiln_dashboard.py --server.port 8501 --server.address 127.0.0.1"
    log "   (or install and enable deploy/kiln-dashboard.service for a managed service.)"
  fi
  log "=== Update finished: code is updated & validated, but NO service was "
  log "    restarted (see above). Log: $LOG ==="
  exit 0
fi

# ---- 5. health check ---------------------------------------------------------
sleep 3
HEALTHY=1
for svc in "${RESTARTED[@]}"; do
  if systemctl is-active --quiet "$svc"; then
    log "✓ ${svc} is active"
  else
    HEALTHY=0
    log "✗ ${svc} is NOT active — last 25 journal lines:"
    sudo journalctl -u "$svc" -n 25 --no-pager 2>&1 | sed 's/^/      /'
  fi
done
# best-effort API ping (non-fatal)
if printf '%s\n' "${RESTARTED[@]}" | grep -q '^kiln-api$' && command -v curl >/dev/null; then
  if curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    log "✓ kiln-api /health responding"
  else
    log "note: kiln-api /health did not respond (may still be starting)"
  fi
fi

# ---- 6. rollback on failure (optional) ---------------------------------------
if [ "$HEALTHY" != 1 ]; then
  if [ "$ROLLBACK" = 1 ] && [ "$NEW_COMMIT" != "$PREV_COMMIT" ] && [ "$PREV_COMMIT" != unknown ]; then
    log "ROLLING BACK to $PREV_COMMIT…"
    if git checkout -q "$PREV_COMMIT"; then
      for svc in "${RESTARTED[@]}"; do sudo systemctl restart "$svc" || true; done
      log "rolled back to $PREV_COMMIT and restarted."
    else
      log "rollback checkout failed — manual intervention needed."
    fi
  fi
  log "One or more services are unhealthy. To roll back manually:"
  log "    git checkout $PREV_COMMIT && bash deploy/update.sh"
  fail "update did not come up cleanly (see journal output above)."
fi

# ---- 7. rotate logs (keep newest 30) -----------------------------------------
ls -1t "$LOGDIR"/update-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

log "=== Update complete: ${PREV_COMMIT} → ${NEW_COMMIT}. Healthy: ${RESTARTED[*]} ==="
log "Log saved to $LOG"
