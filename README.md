# System Admin : Automation Scripts

A collection of Python, Bash, and PowerShell scripts to automate system
administration tasks across Linux and Windows, covering three areas:

- **User Management** : batch create/delete users, group management, cross-platform account auditing
- **Incremental Backups** : hardlink-based snapshots (Linux rsync, Windows NTFS) with retention, integrity checks, and alerts
- **Network Service Monitoring** : ping, TCP, HTTP checks with alerting and a web dashboard *(coming soon)*

---

## Project Status

| Phase | Area | Status |
|-------|------|--------|
| 1 | Foundations : shared utilities, project skeleton | ✅ Complete |
| 2 | User Management : Linux Bash, Windows PowerShell, Python audit | ✅ Complete |
| 3 | Incremental Backups : Linux rsync, Windows NTFS hardlinks | ✅ Complete |
| 4 | Network Service Monitoring | 🔧 In progress |
| 5 | Tests, CI/CD, packaging | ⏳ Pending |

---

## Repository Structure

```
system-admin/
│
├── common/                          # Shared utilities (used by all scripts)
│   ├── logger.py                    # Rotating file + colored console logging
│   ├── config_loader.py             # YAML loader with ${ENV_VAR:-default} support
│   └── notifier.py                  # Alerts via SMTP email, Slack, Telegram
│
├── users/
│   ├── linux/
│   │   ├── create_users.sh          # Batch user creation from CSV (useradd + groups)
│   │   ├── delete_users.sh          # Archive home dir, remove crontab, delete user
│   │   ├── manage_groups.sh         # create/delete/add/remove/list/bulk subcommands
│   │   ├── audit_users.py           # Cross-platform account audit (JSON + HTML report)
│   │   ├── sample_users.csv         # Example CSV input
│   │   └── sample_group_changes.csv # Example bulk group operations input
│   └── windows/
│       ├── create_users.ps1         # Batch creation via New-LocalUser or New-ADUser
│       ├── delete_users.ps1         # Disable → archive profile → remove tasks → delete
│       └── audit_users.py           # Same audit script as linux/ (cross-platform)
│
├── backup/
│   ├── linux/
│   │   ├── backup_incremental.sh    # rsync --link-dest snapshots, rotation, manifest, alerts
│   │   └── restore.sh               # Interactive snapshot picker, full or partial restore
│   ├── windows/
│   │   ├── backup_incremental.py    # os.link() hardlink snapshots on NTFS, robocopy fallback
│   │   └── restore.py               # Snapshot listing, robocopy restore, checksum verify
│   └── config/
│       └── backup_config.yaml       # source_dirs, destination, retention_days, excludes
│
├── monitoring/                      # Phase 4 : in progress
│   ├── monitor_services.py
│   ├── alert_email.py
│   ├── dashboard.py
│   └── config/
│       └── services.yaml
│
├── tests/
│   ├── test_common.py               # 17 tests : logger, config_loader
│   ├── test_users.py                # 47 tests : audit_users parsers, reporter
│   ├── test_backup.py               # 39 tests : snapshots, hardlinks, manifest, rotation
│   └── test_monitoring.py           # Placeholder : Phase 4
│
├── requirements.txt
├── LICENSE
└── .gitignore
```

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure backups

Copy the template and set your paths:

```bash
# Edit source dirs, destination, retention, and notification settings
nano backup/config/backup_config.yaml
```

Key fields:

```yaml
backup:
  source_dirs:
    - /etc
    - /home
  destination: /mnt/backup
  retention_days: 30
  exclude:
    - "*.tmp"
    - "node_modules"
```

---

## User Management

### Linux (run as root)

```bash
# Create users from CSV  (columns: username,group,shell,password)
# Leave password blank to auto-generate; generated passwords saved to logs/
sudo bash users/linux/create_users.sh users/linux/sample_users.csv
sudo bash users/linux/create_users.sh --dry-run users/linux/sample_users.csv

# Delete users : locks account, archives home dir to /var/backups/, removes crontab
sudo bash users/linux/delete_users.sh alice bob
sudo bash users/linux/delete_users.sh --dry-run offboarded.csv

# Group management
sudo bash users/linux/manage_groups.sh create developers
sudo bash users/linux/manage_groups.sh add developers alice bob carol
sudo bash users/linux/manage_groups.sh remove developers alice
sudo bash users/linux/manage_groups.sh list developers
sudo bash users/linux/manage_groups.sh show alice
sudo bash users/linux/manage_groups.sh bulk users/linux/sample_group_changes.csv
```

### Windows (run as Administrator)

```powershell
# Local accounts
.\users\windows\create_users.ps1 -CsvFile .\users\linux\sample_users.csv

# Active Directory accounts
.\users\windows\create_users.ps1 -CsvFile .\users\linux\sample_users.csv `
    -UseAD -OUPath "OU=Staff,DC=corp,DC=example,DC=com"

# Preview only (no changes made)
.\users\windows\create_users.ps1 -CsvFile .\users\linux\sample_users.csv -DryRun

# Delete users : disables account, archives profile to C:\UserArchives\, removes scheduled tasks
.\users\windows\delete_users.ps1 -Users alice, bob
.\users\windows\delete_users.ps1 -CsvFile .\offboarded.csv -DryRun
```

### Account Audit (cross-platform)

Produces JSON and HTML reports covering last login, inactive accounts (> 90 days),
accounts without passwords, and admin/sudo privileges.

```bash
# Linux (root required for /etc/shadow password data)
sudo python users/linux/audit_users.py
sudo python users/linux/audit_users.py --format html --output report.html
sudo python users/linux/audit_users.py --format json,html --output-dir /tmp/audit
sudo python users/linux/audit_users.py --inactive-days 60

# Windows (run as Administrator)
python users\windows\audit_users.py --format html --output report.html
```

Exit code `2` when flagged accounts are found : CI-friendly.

---

## Incremental Backups

Each run creates a timestamped snapshot directory. Unchanged files are stored
as hardlinks to the previous snapshot, so every snapshot looks like a full
backup while only new or modified files consume extra disk space.

```
/mnt/backup/
├── 2026-03-01_020000/
│   ├── etc/
│   ├── home/
│   ├── .manifest.sha256    ← SHA-256 hash of every backed-up file
│   └── .report.txt         ← duration, size, file counts
├── 2026-03-02_020000/
└── latest -> 2026-03-02_020000/   (Linux symlink / Windows latest.txt)
```

### Linux

```bash
# Run backup (reads backup/config/backup_config.yaml)
sudo bash backup/linux/backup_incremental.sh

# Override settings on the fly
sudo bash backup/linux/backup_incremental.sh \
    --source /etc --source /home \
    --dest /mnt/backup --retention 14

# Preview what rsync would transfer (no changes made)
sudo bash backup/linux/backup_incremental.sh --dry-run

# Restore : interactive: pick a snapshot from a numbered list
sudo bash backup/linux/restore.sh

# Restore specific snapshot to a staging directory
sudo bash backup/linux/restore.sh \
    --snapshot 2026-03-01_020000 --target /tmp/restore-staging

# Restore only /etc/nginx from latest snapshot, verify checksums first
sudo bash backup/linux/restore.sh \
    --snapshot latest --path etc/nginx \
    --target /tmp/nginx-restore --verify
```

### Windows

```powershell
# Run backup
python backup\windows\backup_incremental.py

# Override settings on the fly
python backup\windows\backup_incremental.py `
    --source C:\Users --dest D:\Backups --retention 14

# Dry run (no files copied)
python backup\windows\backup_incremental.py --dry-run

# Restore : interactive snapshot selection
python backup\windows\restore.py

# Restore to a staging directory
python backup\windows\restore.py `
    --snapshot 2026-03-01_020000 --target D:\Restore

# Restore a single user's profile with checksum verification
python backup\windows\restore.py `
    --snapshot latest --path "C\Users\alice" --target D:\Restore --verify
```

---

## Common Utilities

### `common/logger.py`

Drop-in logger with rotating files (10 MB, 5 backups) and colored console output.

```python
from common.logger import get_logger
logger = get_logger(__name__)
logger.info("Starting backup...")
```

Set level via environment variable: `LOG_LEVEL=DEBUG python script.py`

### `common/config_loader.py`

YAML loader with environment variable interpolation.

```python
from common.config_loader import load_config, get_nested

cfg  = load_config("backup/config/backup_config.yaml", required_keys=["backup"])
dest = get_nested(cfg, "backup", "destination", default="/mnt/backup")
```

Supports `${VAR}` and `${VAR:-default}` placeholders in any YAML string value.

### `common/notifier.py`

Multi-channel alert sender. Configure via YAML or environment variables.

```python
from common.notifier import Notifier
notifier = Notifier(cfg.get("notifications", {}))
notifier.send("Backup failed on web-01", body="rsync exited with code 23")
```

| Channel | Environment variables |
|---------|----------------------|
| Email (SMTP) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO` |
| Slack | `SLACK_WEBHOOK_URL` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

All channels are optional : unconfigured channels are silently skipped.

---

## Requirements

| Requirement | Details |
|-------------|---------|
| Python | 3.10+ |
| Linux scripts | bash, `useradd`/`userdel`, `rsync`, `lastlog` |
| Windows scripts | PowerShell 5+, `robocopy` (built-in on Windows 7+) |
| AD support | RSAT ActiveDirectory module (pass `-UseAD` flag) |

Python dependencies:

```bash
pip install -r requirements.txt
# pyyaml  psutil  paramiko  jinja2  flask  pytest  flake8
```

---

## Running Tests

```bash
# Full suite
pytest tests/ -v

# Single module
pytest tests/test_backup.py -v

# With coverage report
pytest tests/ --cov=common --cov-report=term-missing -v
```

**Current: 103 tests passing**

| File | Tests | Covers |
|------|-------|--------|
| `test_common.py` | 17 | logger, config_loader |
| `test_users.py` | 47 | audit parsers, flag logic, JSON/HTML reporter |
| `test_backup.py` | 39 | snapshots, hardlinks, manifest, rotation, reports |
| `test_monitoring.py` | : | Phase 4 |

---

## License

MIT : see [LICENSE](LICENSE)
