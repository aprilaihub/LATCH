#!/usr/bin/env bash
# ============================================================================
# AWS Probe Experiment Watchdog
# ============================================================================
#
# Monitors the resource-probe experiment running on the AWS contest instance.
# Every CHECK_INTERVAL seconds it tries to SSH in and check if the experiment
# has completed.  Once complete, it pulls results back, stops the instance,
# and exits.  If the experiment runs longer than MAX_RUNTIME_HOURS, the
# instance is force-stopped and a local alert file is written.
#
# Usage (run in background / tmux):
#   bash scripts/aws_watchdog_probe.sh &
#
# Configuration via env vars:
#   CHECK_INTERVAL      seconds between checks  (default: 300 = 5 min)
#   MAX_RUNTIME_HOURS   force-kill after N hours (default: 10)
#   EXPERIMENT_PID      remote PID to watch
#   RESULTS_DIR         remote results path
#   LOCAL_RESULTS       local path for pulled data
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── Configuration ─────────────────────────────────────────────────────────────

CHECK_INTERVAL="${CHECK_INTERVAL:-300}"        # 5 minutes
MAX_RUNTIME_HOURS="${MAX_RUNTIME_HOURS:-10}"
EXPERIMENT_PID="${EXPERIMENT_PID:-11126}"
RESULTS_DIR="${RESULTS_DIR:-experiments/aws_resource_probe_20260706_134953}"
LOCAL_RESULTS="${LOCAL_RESULTS:-$REPO_ROOT/experiments/aws_resource_probe_latest}"
SSH_KEY="${SSH_KEY:-$REPO_ROOT/fpl26contest-key.pem}"
SSH_HOST="${SSH_HOST:-54.162.220.32}"
SSH_USER="${SSH_USER:-ubuntu}"
export FPL26CONTEST_TOKEN="${FPL26CONTEST_TOKEN:?Set FPL26CONTEST_TOKEN in the environment}"
ALERT_FILE="${ALERT_FILE:-$REPO_ROOT/experiments/AWS_FORCE_KILLED.md}"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=5 -o ServerAliveCountMax=2"
SSH_CMD="ssh $SSH_OPTS -i $SSH_KEY ${SSH_USER}@${SSH_HOST}"

START_TS="$(date +%s)"

# ── Helpers ───────────────────────────────────────────────────────────────────

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

ssh_check() {
  $SSH_CMD "echo ok" 2>/dev/null
}

check_experiment_done() {
  # Returns 0 if experiment process has exited, 1 if still running, 2 if SSH fails
  local result
  result="$($SSH_CMD "ps -p $EXPERIMENT_PID -o pid --no-headers 2>/dev/null || echo EXITED" 2>/dev/null)"
  if [[ -z "$result" ]]; then
    return 2  # SSH failed
  elif [[ "$result" == "EXITED" ]]; then
    return 0  # Done
  else
    return 1  # Still running
  fi
}

count_completed_runs() {
  $SSH_CMD "ls ~/beta_infra/${RESULTS_DIR}/{probe,validate}_*/probe_metadata.json 2>/dev/null | wc -l" 2>/dev/null || echo "0"
}

pull_results() {
  log "Pulling results from $RESULTS_DIR to $LOCAL_RESULTS ..."
  mkdir -p "$LOCAL_RESULTS"
  scp $SSH_OPTS -i "$SSH_KEY" -r "${SSH_USER}@${SSH_HOST}:~/beta_infra/${RESULTS_DIR}/" "$LOCAL_RESULTS/" 2>&1 || {
    log "WARNING: scp failed, trying fpl26contest scp ..."
    cd "$REPO_ROOT"
    FPL26CONTEST_TOKEN="$FPL26CONTEST_TOKEN" ./fpl26contest scp ":${RESULTS_DIR}" "$LOCAL_RESULTS/" 2>&1 || {
      log "ERROR: both scp methods failed"
      return 1
    }
  }
  log "Results pulled to $LOCAL_RESULTS"
  return 0
}

stop_instance() {
  log "Stopping instance ..."
  cd "$REPO_ROOT"
  FPL26CONTEST_TOKEN="$FPL26CONTEST_TOKEN" ./fpl26contest stop <<< "y" 2>&1 || true
}

force_kill() {
  local reason="$1"
  log "FORCE KILLING: $reason"
  stop_instance

  cat > "$ALERT_FILE" <<EOF
# AWS Instance Force-Killed

- **Time:** $(date '+%Y-%m-%d %H:%M:%S')
- **Reason:** $reason
- **Instance:** ${SSH_USER}@${SSH_HOST}
- **Results dir:** ${RESULTS_DIR}
- **Local results:** ${LOCAL_RESULTS}
- **Experiment PID:** ${EXPERIMENT_PID}

The watchdog force-stopped the instance. Data may be incomplete.
Check \`${LOCAL_RESULTS}\` for any results that were pulled before the kill.
EOF
  log "Alert written to $ALERT_FILE"
}

# ── Main loop ─────────────────────────────────────────────────────────────────

log "Watchdog started. PID=$$"
log "  Check interval: ${CHECK_INTERVAL}s"
log "  Max runtime:    ${MAX_RUNTIME_HOURS}h"
log "  Remote PID:     ${EXPERIMENT_PID}"
log "  Results dir:    ${RESULTS_DIR}"
log "  Local dir:      ${LOCAL_RESULTS}"
log ""

CONSECUTIVE_SSH_FAILS=0

while true; do
  NOW="$(date +%s)"
  ELAPSED_SEC=$(( NOW - START_TS ))
  ELAPSED_H=$(( ELAPSED_SEC / 3600 ))

  # ── Timeout check ───────────────────────────────────────────────────────
  if (( ELAPSED_H >= MAX_RUNTIME_HOURS )); then
    log "MAX_RUNTIME exceeded (${ELAPSED_H}h >= ${MAX_RUNTIME_HOURS}h). Force-killing."
    force_kill "Experiment exceeded ${MAX_RUNTIME_HOURS}h runtime limit"
    exit 1
  fi

  # ── SSH check ───────────────────────────────────────────────────────────
  if ! ssh_check > /dev/null 2>&1; then
    CONSECUTIVE_SSH_FAILS=$(( CONSECUTIVE_SSH_FAILS + 1 ))
    log "SSH unreachable (fail #${CONSECUTIVE_SSH_FAILS}, elapsed ${ELAPSED_H}h)"
    sleep "$CHECK_INTERVAL"
    continue
  fi
  CONSECUTIVE_SSH_FAILS=0

  # ── Experiment status ───────────────────────────────────────────────────
  check_experiment_done
  STATUS=$?

  if [[ $STATUS -eq 0 ]]; then
    log "████ Experiment COMPLETED! (elapsed ${ELAPSED_H}h) ████"

    # Give a few seconds for last files to flush
    sleep 10

    # Pull results
    if pull_results; then
      log "Results successfully pulled."
    else
      log "WARNING: could not pull all results. Will try again."
      sleep 60
      pull_results || log "Giving up on pull."
    fi

    # Stop instance
    stop_instance
    log "████ Watchdog done. Instance stopped. ████"
    exit 0
  elif [[ $STATUS -eq 1 ]]; then
    COMPLETED="$(count_completed_runs)"
    log "Still running (elapsed ${ELAPSED_H}h, $COMPLETED/10 runs done, SSH OK)"
  else
    log "SSH OK but status check confused — will retry"
  fi

  sleep "$CHECK_INTERVAL"
done
