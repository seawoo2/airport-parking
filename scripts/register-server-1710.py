"""Add one 17:10 KST D+1 passenger baseline cron job; preserve all existing jobs."""

import argparse
import json
from pathlib import Path
import subprocess

from airport_parking.sync import DEFAULT_CONFIG, config_values

REMOTE = r'''
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess

zone = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], check=True, capture_output=True, text=True).stdout.strip()
if zone != "Asia/Seoul":
    raise RuntimeError("Server cron timezone must be Asia/Seoul")
existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
if existing.returncode not in (0, 1):
    raise RuntimeError(existing.stderr)
previous = existing.stdout
marker = "# airport-parking managed D+1 17:10"
lines = previous.splitlines()
line = ("10 17 * * * cd " + shlex.quote(REMOTE_DIR) +
        " && /usr/bin/flock -w 120 .passengers.lock .venv/bin/airport-parking collect-passengers --day-offset 1 --phase baseline >> logs/passengers.log 2>&1 " + marker)
lines = [entry for entry in lines if marker not in entry]
lines.append(line)
updated = "\n".join(lines) + "\n"
backup = Path(REMOTE_DIR) / "local-work" / "cron-backups"
backup.mkdir(parents=True, exist_ok=True)
backup_file = backup / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".txt")
backup_file.write_text(previous, encoding="utf-8")
subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
installed = subprocess.run(["crontab", "-l"], check=True, capture_output=True, text=True).stdout
if installed != updated:
    raise RuntimeError("Installed cron differs from requested cron")
print(json.dumps({"status": "registered", "timezone": zone, "backup": str(backup_file), "added_job": line}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    host, key, remote_dir, _ = config_values(args.config)
    script = "REMOTE_DIR = " + repr(remote_dir) + "\n" + REMOTE
    result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "-o", "ConnectTimeout=15", "-i", str(key), host, "python3", "-"],
                            input=script.encode("utf-8"), capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    print(result.stdout.decode("utf-8"))


if __name__ == "__main__":
    main()
