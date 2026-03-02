# System Admin : Automation Scripts

A collection of Python, Bash, and PowerShell scripts to automate system administration tasks across Linux and Windows, covering three areas:

- **User Management** : batch create/delete users, group management, account auditing
- **Incremental Backups** : rsync-based (Linux) and robocopy/shutil-based (Windows) with retention and integrity checks *(Phase 3)*
- **Network Service Monitoring** : ping, TCP, HTTP checks with alerting and a web dashboard *(Phase 4)*

## Project Status

| Phase | Area | Status |
|-------|------|--------|
| 1 | Foundations : shared utilities, project skeleton | ✅ Complete |
| 2 | User Management : Linux Bash, Windows PowerShell, Python audit | ✅ Complete |
| 3 | Incremental Backups | 🔧 In progress |
| 4 | Network Service Monitoring | ⏳ Pending |
| 5 | Tests, CI/CD, packaging | ⏳ Pending |

## Repository Structure

```
system-admin/
│
├── common/                     # Shared utilities (used by all scripts)
│   ├── logger.py               # Rotating file + colored console logging
│   ├── config_loader.py        # YAML loader with ${ENV_VAR:-default} support
│   └── notifier.py             # Alerts via SMTP email, Slack, Telegram
│
├── users/
│   ├── linux/
│   │   ├── create_users.sh     # Batch user creation from CSV (useradd + groups)
│   │   ├── delete_users.sh     # Archive home dir, remove crontab, delete user
│   │   ├── manage_groups.sh    # create/delete/add/remove/list/bulk subcommands
│   │   ├── audit_users.py      # Cross-platform account audit (JSON + HTML report)
│   │   ├── sample_users.csv    # Example CSV input
│   │   └── sample_group_changes.csv
│   └── windows/
│       ├── create_users.ps1    # Batch creation via New-LocalUser or New-ADUser
│       ├── delete_users.ps1    # Disable → archive profile → remove tasks → delete
│       └── audit_users.py      # Same audit script as linux/ (cross-platform)
│
├── backup/                     # Phase 3 : coming soon
│   ├── linux/
│   │   ├── backup_incremental.sh
│   │   └── restore.sh
│   ├── windows/
│   │   ├── backup_incremental.py
│   │   └── restore.py
│   └── config/
│       └── backup_config.yaml
│
├── monitoring/                 # Phase 4 : coming soon
│   ├── monitor_services.py
│   ├── alert_email.py
│   ├── dashboard.py
│   └── config/
│       └── services.yaml
│
├── tests/
│   ├── test_common.py          # 17 tests : logger, config_loader
│   ├── test_users.py           # 47 tests : audit_users parsers, reporter
│   ├── test_backup.py          # Placeholder : Phase 3
│   └── test_monitoring.py      # Placeholder : Phase 4
│
├── requirements.txt
├── LICENSE
└── .gitignore
```

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. User Management (Linux)

**Create users from CSV:**
```bash
# CSV format: username,group,shell,password  (password blank = auto-generated)
sudo bash users/linux/create_users.sh users/linux/sample_users.csv

# Preview without making changes
sudo bash users/linux/create_users.sh --dry-run users/linux/sample_users.csv
```

**Delete users (archives home directory first):**
```bash
sudo bash users/linux/delete_users.sh alice bob
sudo bash users/linux/delete_users.sh --dry-run offboarded.csv
```

**Manage groups:**
```bash
sudo bash users/linux/manage_groups.sh create developers
sudo bash users/linux/manage_groups.sh add developers alice bob carol
sudo bash users/linux/manage_groups.sh list developers
sudo bash users/linux/manage_groups.sh bulk users/linux/sample_group_changes.csv
```

### 3. User Management (Windows : run as Administrator)

```powershell
# Local accounts
.\users\windows\create_users.ps1 -CsvFile .\users\linux\sample_users.csv

# Active Directory
.\users\windows\create_users.ps1 -CsvFile .\users\linux\sample_users.csv `
    -UseAD -OUPath "OU=Staff,DC=corp,DC=example,DC=com"

# Preview only
.\users\windows\create_users.ps1 -CsvFile .\users\linux\sample_users.csv -DryRun

# Delete users
.\users\windows\delete_users.ps1 -Users alice, bob
.\users\windows\delete_users.ps1 -CsvFile .\offboarded.csv -DryRun
```

### 4. Audit User Accounts

Produces a JSON and/or HTML report covering last login, inactive accounts,
missing passwords, and admin privileges.

```bash
# Linux (root required for /etc/shadow)
sudo python users/linux/audit_users.py
sudo python users/linux/audit_users.py --format html --output report.html
sudo python users/linux/audit_users.py --format json,html --output-dir /tmp/audit
sudo python users/linux/audit_users.py --inactive-days 60

# Windows (run as Administrator)
python users\windows\audit_users.py --format html --output report.html
```

Exit code `2` when flagged accounts are found, making it CI-friendly.

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
cfg = load_config("monitoring/config/services.yaml", required_keys=["services"])
host = get_nested(cfg, "services", 0, "host", default="localhost")
```

Supports `${VAR}` and `${VAR:-default}` placeholders in YAML values.

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

## Requirements

| Requirement | Details |
|-------------|---------|
| Python | 3.10+ |
| Linux scripts | bash, `useradd`/`userdel`, `rsync`, `lastlog` |
| Windows scripts | PowerShell 5+, `robocopy` (built-in) |
| AD support | RSAT ActiveDirectory module (`-UseAD` flag) |

All Python dependencies:

```bash
pip install -r requirements.txt
# pyyaml, psutil, paramiko, jinja2, flask, pytest, flake8
```

## Running Tests

```bash
# All tests
pytest tests/ -v

# Specific module
pytest tests/test_users.py -v

# With coverage
pytest tests/ --cov=common --cov=users -v
```

Current test count: **65 passing**

## License

MIT : see [LICENSE](LICENSE)
