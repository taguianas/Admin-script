#!/usr/bin/env bash
# =============================================================================
# restore.sh — Interactive restore from incremental backup snapshots
#
# Lists all available snapshots in the backup destination and lets you
# restore either a full snapshot or specific paths within it.
#
# Usage:
#   ./restore.sh [OPTIONS]
#
# Options:
#   -c, --config FILE       Path to backup_config.yaml
#                           (default: ../config/backup_config.yaml)
#   -d, --dest DIR          Backup destination root (overrides config)
#   -t, --target DIR        Where to restore files
#                           (default: original paths — restores in place)
#   -s, --snapshot NAME     Snapshot name (e.g. 2026-03-01_020000) or "latest"
#                           (skips interactive selection)
#   -p, --path PATH         Restore only this relative path within the snapshot
#                           (e.g. "home/alice" or "etc/nginx")
#   --verify                Verify SHA-256 checksums before restoring
#   --dry-run               Show what would be restored without doing it
#   -l, --log-dir DIR       Log directory (default: ../../logs)
#   -h, --help              Show this help message
#
# Examples:
#   # Interactive: pick a snapshot, restore everything to original paths
#   sudo ./restore.sh
#
#   # Restore specific snapshot to a staging directory
#   sudo ./restore.sh --snapshot 2026-03-01_020000 --target /tmp/restore-staging
#
#   # Restore only /etc/nginx from latest snapshot
#   sudo ./restore.sh --snapshot latest --path etc/nginx --target /tmp/nginx-restore
#
#   # Verify checksums before restoring
#   sudo ./restore.sh --verify
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CONFIG="${SCRIPT_DIR}/../config/backup_config.yaml"
LOG_DIR="${SCRIPT_DIR}/../../logs"
CONFIG_FILE=""
DESTINATION=""
RESTORE_TARGET=""   # empty = restore in place (to original paths)
SNAPSHOT_NAME=""
RESTORE_PATH=""     # empty = restore everything
VERIFY_CHECKSUMS=false
DRY_RUN=false

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log() {
    local level="$1"; shift
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
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
            -c|--config)    CONFIG_FILE="$2"; shift 2 ;;
            -d|--dest)      DESTINATION="$2"; shift 2 ;;
            -t|--target)    RESTORE_TARGET="$2"; shift 2 ;;
            -s|--snapshot)  SNAPSHOT_NAME="$2"; shift 2 ;;
            -p|--path)      RESTORE_PATH="$2"; shift 2 ;;
            --verify)       VERIFY_CHECKSUMS=true; shift ;;
            --dry-run)      DRY_RUN=true; shift ;;
            -l|--log-dir)   LOG_DIR="$2"; shift 2 ;;
            -h|--help)      usage ;;
            *)              log_error "Unknown option: $1"; exit 1 ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Load destination from YAML config (same helper as backup script)
# ---------------------------------------------------------------------------
load_config() {
    local cfg_file="$1"
    [[ ! -f "$cfg_file" ]] && return 0

    local dest
    dest="$(python3 -c "
import sys, yaml
with open('$cfg_file') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('backup', {}).get('destination', ''))
" 2>/dev/null || echo "")"

    [[ -z "$DESTINATION" && -n "$dest" ]] && DESTINATION="$dest"
}

# ---------------------------------------------------------------------------
# List snapshots
# ---------------------------------------------------------------------------
list_snapshots() {
    local dest="$1"
    find "$dest" -maxdepth 1 -type d -name "20*" 2>/dev/null | sort
}

count_snapshots() {
    list_snapshots "$1" | wc -l
}

print_snapshot_table() {
    local dest="$1"
    local -a snapshots
    mapfile -t snapshots < <(list_snapshots "$dest")

    if [[ ${#snapshots[@]} -eq 0 ]]; then
        echo "No snapshots found in: $dest"
        return 1
    fi

    echo ""
    printf "  %-4s  %-22s  %-8s  %-20s\n" "#" "Snapshot" "Size" "Report"
    printf "  %-4s  %-22s  %-8s  %-20s\n" "----" "----------------------" "--------" "--------------------"

    local i=1
    for snap in "${snapshots[@]}"; do
        local name size report_note
        name="$(basename "$snap")"
        size="$(du -sh "$snap" 2>/dev/null | cut -f1 || echo "?")"

        if [[ -f "${snap}/.report.txt" ]]; then
            report_note="report available"
        else
            report_note="-"
        fi

        # Highlight the latest symlink target
        local latest_target
        latest_target="$(readlink -f "${dest}/latest" 2>/dev/null || echo "")"
        if [[ "$snap" == "$latest_target" ]]; then
            name="${name} (latest)"
        fi

        printf "  %-4s  %-22s  %-8s  %-20s\n" "$i" "$name" "$size" "$report_note"
        (( i++ )) || true
    done
    echo ""
}

# ---------------------------------------------------------------------------
# Interactive snapshot selection
# ---------------------------------------------------------------------------
select_snapshot() {
    local dest="$1"
    local -a snapshots
    mapfile -t snapshots < <(list_snapshots "$dest")

    print_snapshot_table "$dest"

    while true; do
        read -r -p "  Enter snapshot number (or 'q' to quit): " choice
        case "$choice" in
            q|Q) log_info "Restore cancelled."; exit 0 ;;
            ''|*[!0-9]*) echo "  Please enter a valid number." ;;
            *)
                if (( choice >= 1 && choice <= ${#snapshots[@]} )); then
                    echo "${snapshots[$((choice - 1))]}"
                    return 0
                else
                    echo "  Number out of range (1–${#snapshots[@]})."
                fi
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------
verify_checksums() {
    local snapshot_dir="$1"
    local manifest="${snapshot_dir}/.manifest.sha256"

    if [[ ! -f "$manifest" ]]; then
        log_warn "No checksum manifest found at $manifest — skipping verification"
        return 0
    fi

    log_info "Verifying checksums from: $manifest"
    local failed=0

    (
        cd "$snapshot_dir"
        while IFS= read -r line; do
            local expected_hash file_path
            expected_hash="${line%% *}"
            file_path="${line##* }"
            if [[ -f "$file_path" ]]; then
                local actual_hash
                actual_hash="$(sha256sum "$file_path" | awk '{print $1}')"
                if [[ "$actual_hash" != "$expected_hash" ]]; then
                    log_error "Checksum mismatch: $file_path"
                    (( failed++ )) || true
                fi
            else
                log_warn "File missing from snapshot: $file_path"
            fi
        done < "$manifest"
    )

    if [[ $failed -gt 0 ]]; then
        log_error "Checksum verification failed: $failed file(s) corrupted"
        return 1
    fi

    log_info "All checksums verified successfully"
    return 0
}

# ---------------------------------------------------------------------------
# Show snapshot report
# ---------------------------------------------------------------------------
show_report() {
    local snapshot_dir="$1"
    local report="${snapshot_dir}/.report.txt"
    if [[ -f "$report" ]]; then
        echo ""
        echo "  --- Snapshot Report ---"
        sed 's/^/  /' "$report"
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# Perform restore
# ---------------------------------------------------------------------------
do_restore() {
    local snapshot_dir="$1"
    local restore_path="$2"   # relative path within snapshot, or empty for all
    local target="$3"         # destination, or empty for in-place

    local source_root="${snapshot_dir}"
    [[ -n "$restore_path" ]] && source_root="${snapshot_dir}/${restore_path}"

    if [[ ! -d "$source_root" ]]; then
        log_error "Path not found in snapshot: $source_root"
        exit 1
    fi

    # Determine rsync destination
    local rsync_dest
    if [[ -n "$target" ]]; then
        rsync_dest="${target}/"
        [[ "$DRY_RUN" == false ]] && mkdir -p "$target"
    else
        # In-place: restore to original paths
        # The snapshot stores files under their original path relative to /
        # e.g. snapshot/etc/nginx → /etc/nginx
        rsync_dest="/"
    fi

    log_info "Restoring from : $source_root"
    log_info "Restoring to   : ${rsync_dest%/} ($([ -n "$target" ] && echo 'staging' || echo 'in-place'))"

    local -a rsync_cmd=(
        rsync
        --archive
        --hard-links
        --human-readable
        --stats
    )
    [[ "$DRY_RUN" == true ]] && rsync_cmd+=("--dry-run")
    rsync_cmd+=("${source_root}/" "${rsync_dest}")

    log_info "rsync command: ${rsync_cmd[*]}"

    if "${rsync_cmd[@]}" 2>&1 | tee -a "${LOG_FILE:-/dev/null}"; then
        log_info "Restore completed successfully"
    else
        log_error "Restore failed — check log for details"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    # Resolve config
    [[ -z "$CONFIG_FILE" ]] && CONFIG_FILE="$DEFAULT_CONFIG"
    load_config "$CONFIG_FILE"

    if [[ -z "$DESTINATION" ]]; then
        log_error "No backup destination configured. Use --dest or set backup.destination in config."
        exit 1
    fi

    if [[ ! -d "$DESTINATION" ]]; then
        log_error "Backup destination not found: $DESTINATION"
        exit 1
    fi

    # Set up logging
    mkdir -p "$LOG_DIR"
    LOG_FILE="${LOG_DIR}/restore.log"
    touch "$LOG_FILE"
    log_info "=== restore.sh started ==="

    # Resolve snapshot
    local snapshot_dir=""
    if [[ -n "$SNAPSHOT_NAME" ]]; then
        if [[ "$SNAPSHOT_NAME" == "latest" ]]; then
            snapshot_dir="$(readlink -f "${DESTINATION}/latest" 2>/dev/null || true)"
            if [[ -z "$snapshot_dir" || ! -d "$snapshot_dir" ]]; then
                log_error "'latest' symlink not found or invalid in: $DESTINATION"
                exit 1
            fi
        else
            snapshot_dir="${DESTINATION}/${SNAPSHOT_NAME}"
            if [[ ! -d "$snapshot_dir" ]]; then
                log_error "Snapshot not found: $snapshot_dir"
                exit 1
            fi
        fi
        log_info "Using snapshot: $snapshot_dir"
    else
        echo ""
        echo "Available snapshots in: $DESTINATION"
        snapshot_dir="$(select_snapshot "$DESTINATION")"
        show_report "$snapshot_dir"
    fi

    # Confirm before restoring in-place
    if [[ -z "$RESTORE_TARGET" && "$DRY_RUN" == false ]]; then
        echo ""
        log_warn "IN-PLACE RESTORE: files will be overwritten at their original system paths."
        read -r -p "  Type 'yes' to confirm: " confirm
        if [[ "$confirm" != "yes" ]]; then
            log_info "Restore cancelled."
            exit 0
        fi
    fi

    # Verify checksums if requested
    if [[ "$VERIFY_CHECKSUMS" == true ]]; then
        verify_checksums "$snapshot_dir" || exit 1
    fi

    do_restore "$snapshot_dir" "$RESTORE_PATH" "$RESTORE_TARGET"
    log_info "=== restore.sh finished ==="
}

main "$@"
