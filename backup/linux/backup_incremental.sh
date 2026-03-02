#!/usr/bin/env bash
# =============================================================================
# backup_incremental.sh — rsync-based incremental backup with hardlinks
#
# How it works:
#   Each run creates a new timestamped snapshot directory.  Unchanged files
#   are represented as hardlinks to the previous snapshot, so each directory
#   looks like a full backup while only storing the delta on disk.
#   A "latest" symlink always points to the most recent successful snapshot.
#
# Snapshot layout:
#   <destination>/
#   ├── 2026-03-01_020000/
#   │   ├── etc/
#   │   ├── home/
#   │   ├── .manifest.sha256   (SHA-256 of every backed-up file)
#   │   └── .report.txt        (duration, size, file counts)
#   ├── 2026-03-02_020000/
#   └── latest -> 2026-03-02_020000/  (symlink)
#
# Usage:
#   sudo ./backup_incremental.sh [OPTIONS]
#
# Options:
#   -c, --config FILE       Path to backup_config.yaml
#                           (default: ../config/backup_config.yaml)
#   -s, --source DIR        Add a source directory (repeatable; overrides config)
#   -d, --dest DIR          Destination root (overrides config)
#   -r, --retention DAYS    Retention days (overrides config, default: 30)
#   -e, --exclude PATTERN   Add an exclude pattern (repeatable; overrides config)
#   -l, --log-dir DIR       Log directory (default: ../../logs)
#   --no-checksum           Skip SHA-256 manifest generation
#   --dry-run               Show what rsync would do without transferring files
#   -h, --help              Show this help message
#
# Examples:
#   sudo ./backup_incremental.sh
#   sudo ./backup_incremental.sh --config /etc/backup_config.yaml
#   sudo ./backup_incremental.sh --source /home --dest /mnt/backup --retention 14
#   sudo ./backup_incremental.sh --dry-run
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CONFIG="${SCRIPT_DIR}/../config/backup_config.yaml"
LOG_DIR="${SCRIPT_DIR}/../../logs"
CONFIG_FILE=""
SOURCES=()
DESTINATION=""
RETENTION_DAYS=30
EXCLUDES=()
DRY_RUN=false
GENERATE_CHECKSUM=true
LOCK_FILE="/tmp/backup_incremental.lock"
TIMESTAMP="$(date '+%Y-%m-%d_%H%M%S')"
START_TIME="$(date +%s)"

# ---------------------------------------------------------------------------
# Logging (LOG_FILE set after LOG_DIR is resolved)
# ---------------------------------------------------------------------------
_log() {
    local level="$1"; shift
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local msg="$ts [$level] $*"
    echo "$msg" >&2
    [[ -n "${LOG_FILE:-}" ]] && echo "$msg" >> "$LOG_FILE"
}
log_info()  { _log "INFO    " "$@"; }
log_warn()  { _log "WARNING " "$@"; }
log_error() { _log "ERROR   " "$@"; }
log_dry()   { _log "DRY-RUN " "$@"; }

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    grep '^#' "$0" | grep -v '#!/' | sed 's/^# \{0,1\}//'
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c|--config)      CONFIG_FILE="$2"; shift 2 ;;
            -s|--source)      SOURCES+=("$2"); shift 2 ;;
            -d|--dest)        DESTINATION="$2"; shift 2 ;;
            -r|--retention)   RETENTION_DAYS="$2"; shift 2 ;;
            -e|--exclude)     EXCLUDES+=("$2"); shift 2 ;;
            -l|--log-dir)     LOG_DIR="$2"; shift 2 ;;
            --no-checksum)    GENERATE_CHECKSUM=false; shift ;;
            --dry-run)        DRY_RUN=true; shift ;;
            -h|--help)        usage ;;
            *)                log_error "Unknown option: $1"; exit 1 ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Load YAML config via inline Python (only if pyyaml is available)
# ---------------------------------------------------------------------------
load_config() {
    local cfg_file="$1"
    [[ ! -f "$cfg_file" ]] && { log_warn "Config not found: $cfg_file — using CLI args / defaults"; return 0; }

    local py_out
    py_out="$(python3 - "$cfg_file" <<'PYEOF'
import sys, yaml

try:
    with open(sys.argv[1]) as f:
        raw = yaml.safe_load(f)
except Exception as e:
    print(f"CONFIG_ERROR={e}", file=sys.stderr)
    sys.exit(1)

b = raw.get("backup", {})

dest = b.get("destination", "")
ret  = b.get("retention_days", 30)
srcs = b.get("source_dirs", [])
excl = b.get("exclude", [])

print(f"DEST_CFG={dest}")
print(f"RETENTION_CFG={ret}")
# Emit arrays as newline-delimited values prefixed with SOURCE: and EXCLUDE:
for s in srcs:
    print(f"SOURCE:{s}")
for e in excl:
    print(f"EXCLUDE:{e}")
PYEOF
)" || { log_warn "Could not parse config YAML — using CLI args / defaults"; return 0; }

    while IFS= read -r line; do
        case "$line" in
            DEST_CFG=*)
                [[ -z "$DESTINATION" ]] && DESTINATION="${line#DEST_CFG=}" ;;
            RETENTION_CFG=*)
                [[ "$RETENTION_DAYS" -eq 30 ]] && RETENTION_DAYS="${line#RETENTION_CFG=}" ;;
            SOURCE:*)
                [[ ${#SOURCES[@]} -eq 0 ]] && SOURCES+=("${line#SOURCE:}") ;;
            EXCLUDE:*)
                [[ ${#EXCLUDES[@]} -eq 0 ]] && EXCLUDES+=("${line#EXCLUDE:}") ;;
        esac
    done <<< "$py_out"
}

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script should be run as root to back up system directories."
        log_warn  "Continuing as non-root — some files may be skipped due to permissions."
    fi
}

check_commands() {
    local missing=()
    for cmd in rsync python3; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Required commands not found: ${missing[*]}"
        exit 1
    fi
}

acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local pid
        pid="$(cat "$LOCK_FILE" 2>/dev/null || echo "")"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log_error "Backup already running (PID $pid). Exiting."
            exit 1
        else
            log_warn "Stale lock file found — removing it"
            rm -f "$LOCK_FILE"
        fi
    fi
    echo $$ > "$LOCK_FILE"
}

release_lock() {
    rm -f "$LOCK_FILE"
}

# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

find_latest_snapshot() {
    local dest="$1"
    local link="${dest}/latest"
    if [[ -L "$link" ]]; then
        readlink -f "$link"
    else
        # Fall back to most recently modified snapshot dir
        find "$dest" -maxdepth 1 -type d -name "20*" -printf '%T@ %p\n' 2>/dev/null \
            | sort -n | tail -1 | awk '{print $2}'
    fi
}

build_exclude_args() {
    local -a args=()
    for pattern in "${EXCLUDES[@]}"; do
        args+=("--exclude=${pattern}")
    done
    echo "${args[@]+"${args[@]}"}"
}

# ---------------------------------------------------------------------------
# SHA-256 manifest
# ---------------------------------------------------------------------------
generate_manifest() {
    local snapshot_dir="$1"
    local manifest="${snapshot_dir}/.manifest.sha256"
    log_info "Generating SHA-256 manifest..."

    # find all regular files, compute checksums, store relative paths
    (
        cd "$snapshot_dir"
        find . -type f ! -name ".manifest.sha256" ! -name ".report.txt" \
            -exec sha256sum {} \; 2>/dev/null
    ) > "$manifest"

    local count
    count="$(wc -l < "$manifest")"
    log_info "Manifest written: $manifest ($count files)"
}

# ---------------------------------------------------------------------------
# Backup report
# ---------------------------------------------------------------------------
write_report() {
    local snapshot_dir="$1"
    local -i files_transferred="$2"
    local -i files_total="$3"
    local rsync_log="$4"
    local report="${snapshot_dir}/.report.txt"

    local end_time
    end_time="$(date +%s)"
    local duration=$(( end_time - START_TIME ))
    local dur_fmt
    dur_fmt="$(printf '%02d:%02d:%02d' $(( duration/3600 )) $(( (duration%3600)/60 )) $(( duration%60 )))"

    local size
    size="$(du -sh "$snapshot_dir" 2>/dev/null | cut -f1 || echo "unknown")"

    cat > "$report" << EOF
Backup Report
=============
Timestamp  : $TIMESTAMP
Snapshot   : $snapshot_dir
Duration   : $dur_fmt
Total size : $size
Sources    : ${SOURCES[*]}
Destination: $DESTINATION

File counts
-----------
Files transferred (new/changed): $files_transferred
Total files in snapshot        : $files_total

Configuration
-------------
Retention  : $RETENTION_DAYS days
Dry run    : $DRY_RUN
Checksums  : $GENERATE_CHECKSUM
EOF

    log_info "Report written: $report"
}

# ---------------------------------------------------------------------------
# Rotation — delete snapshots older than retention_days
# ---------------------------------------------------------------------------
rotate_old_snapshots() {
    local dest="$1"
    log_info "Rotating snapshots older than ${RETENTION_DAYS} days..."

    local count=0
    while IFS= read -r snapshot; do
        [[ -z "$snapshot" ]] && continue
        if [[ "$DRY_RUN" == true ]]; then
            log_dry "Would remove old snapshot: $snapshot"
        else
            rm -rf "$snapshot"
            log_info "Removed old snapshot: $snapshot"
        fi
        (( count++ )) || true
    done < <(
        find "$dest" -maxdepth 1 -type d -name "20*" \
            -mtime "+${RETENTION_DAYS}" 2>/dev/null
    )

    [[ $count -eq 0 ]] && log_info "No snapshots to rotate"
}

# ---------------------------------------------------------------------------
# Notification helper (calls Python notifier if available)
# ---------------------------------------------------------------------------
send_notification() {
    local subject="$1"
    local body="$2"
    local config="${CONFIG_FILE:-$DEFAULT_CONFIG}"

    python3 - "$config" "$subject" "$body" <<'PYEOF' 2>/dev/null || true
import sys
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[3].__str__() if hasattr(__file__, '__str__') else '.')

# Resolve project root relative to this script
import os
script_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv[0] != '-' else os.getcwd()
root = os.path.abspath(os.path.join(script_dir, '..', '..'))
sys.path.insert(0, root)

try:
    import yaml
    from common.notifier import Notifier
    from common.config_loader import load_config
    cfg = load_config(sys.argv[1])
    notifier = Notifier(cfg.get('notifications', {}))
    notifier.send(sys.argv[2], body=sys.argv[3])
except Exception as e:
    print(f"Notification skipped: {e}", file=sys.stderr)
PYEOF
}

# ---------------------------------------------------------------------------
# Main backup loop
# ---------------------------------------------------------------------------
run_backup() {
    local dest="$DESTINATION"
    local snapshot_dir="${dest}/${TIMESTAMP}"
    local latest_link="${dest}/latest"

    # Find previous snapshot for --link-dest
    local prev_snapshot
    prev_snapshot="$(find_latest_snapshot "$dest")"
    if [[ -n "$prev_snapshot" ]]; then
        log_info "Previous snapshot: $prev_snapshot"
    else
        log_info "No previous snapshot found — performing full backup"
    fi

    log_info "New snapshot: $snapshot_dir"

    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$snapshot_dir"
    fi

    local -a exclude_args
    read -ra exclude_args <<< "$(build_exclude_args)"
    local -i total_transferred=0
    local -i total_files=0
    local rsync_log="${LOG_DIR}/rsync_${TIMESTAMP}.log"

    for source in "${SOURCES[@]}"; do
        if [[ ! -d "$source" ]]; then
            log_warn "Source directory not found: $source — skipping"
            continue
        fi

        # Derive target subdir name from source path
        # e.g. /home -> <snapshot>/home , /var/www -> <snapshot>/var/www
        local rel_path
        rel_path="${source#/}"   # strip leading slash
        local target="${snapshot_dir}/${rel_path}"

        log_info "Backing up: $source → $target"

        local -a rsync_cmd=(
            rsync
            --archive            # -rlptgoD: recursive, links, perms, times, group, owner, devices
            --hard-links         # preserve existing hardlinks in source
            --human-readable
            --stats
            --delete             # remove files at destination that are gone from source
            "${exclude_args[@]+"${exclude_args[@]}"}"
        )

        # Add --link-dest if we have a previous snapshot
        if [[ -n "$prev_snapshot" ]]; then
            local prev_target="${prev_snapshot}/${rel_path}"
            [[ -d "$prev_target" ]] && rsync_cmd+=("--link-dest=${prev_target}")
        fi

        [[ "$DRY_RUN" == true ]] && rsync_cmd+=("--dry-run")

        rsync_cmd+=("${source}/" "${target}/")

        log_info "rsync command: ${rsync_cmd[*]}"

        local rsync_output
        if rsync_output="$("${rsync_cmd[@]}" 2>&1)"; then
            echo "$rsync_output" >> "$rsync_log"

            # Parse transferred file count from rsync --stats output
            local transferred
            transferred="$(echo "$rsync_output" \
                | grep -oP 'Number of regular files transferred: \K[0-9,]+' \
                | tr -d ',' || echo 0)"
            total_transferred=$(( total_transferred + ${transferred:-0} ))

            local all_files
            all_files="$(echo "$rsync_output" \
                | grep -oP 'Number of files: [0-9,]+' \
                | grep -oP '[0-9,]+' | tr -d ',' || echo 0)"
            total_files=$(( total_files + ${all_files:-0} ))

            log_info "Source '$source': $transferred files transferred"
        else
            local exit_code=$?
            log_error "rsync failed for '$source' (exit code $exit_code)"
            log_error "See $rsync_log for details"
            return $exit_code
        fi
    done

    # Generate SHA-256 manifest
    if [[ "$GENERATE_CHECKSUM" == true && "$DRY_RUN" == false ]]; then
        generate_manifest "$snapshot_dir"
    fi

    # Write backup report
    if [[ "$DRY_RUN" == false ]]; then
        write_report "$snapshot_dir" "$total_transferred" "$total_files" "$rsync_log"
    fi

    # Update latest symlink
    if [[ "$DRY_RUN" == false ]]; then
        ln -snf "$snapshot_dir" "$latest_link"
        log_info "Updated 'latest' symlink: $latest_link → $snapshot_dir"
    fi

    log_info "Backup complete: $total_transferred files transferred, $total_files total in snapshot"
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    # Resolve config
    [[ -z "$CONFIG_FILE" ]] && CONFIG_FILE="$DEFAULT_CONFIG"
    load_config "$CONFIG_FILE"

    # Validate required settings
    if [[ ${#SOURCES[@]} -eq 0 ]]; then
        log_error "No source directories configured. Use --source or set backup.source_dirs in config."
        exit 1
    fi
    if [[ -z "$DESTINATION" ]]; then
        log_error "No destination configured. Use --dest or set backup.destination in config."
        exit 1
    fi

    # Set up logging
    mkdir -p "$LOG_DIR"
    LOG_FILE="${LOG_DIR}/backup_incremental.log"
    touch "$LOG_FILE"

    log_info "=== backup_incremental.sh started ==="
    log_info "Config     : $CONFIG_FILE"
    log_info "Sources    : ${SOURCES[*]}"
    log_info "Destination: $DESTINATION"
    log_info "Retention  : ${RETENTION_DAYS} days"
    log_info "Dry run    : $DRY_RUN"
    log_info "Checksums  : $GENERATE_CHECKSUM"

    check_root
    check_commands

    # Create destination if it doesn't exist
    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$DESTINATION"
    fi

    acquire_lock
    trap 'release_lock' EXIT

    # Run backup
    if run_backup; then
        rotate_old_snapshots "$DESTINATION"
        log_info "=== Backup finished successfully ==="
        send_notification \
            "Backup succeeded: $(hostname) $(date '+%Y-%m-%d')" \
            "Snapshot: ${DESTINATION}/${TIMESTAMP}"
    else
        local rc=$?
        log_error "=== Backup FAILED ==="
        send_notification \
            "BACKUP FAILED: $(hostname) $(date '+%Y-%m-%d')" \
            "Check log: $LOG_FILE"
        exit $rc
    fi
}

main "$@"
