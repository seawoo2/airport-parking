"""Deploy collection code and register context cron jobs over configured SSH."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

from airport_parking.sync import ROOT, DEFAULT_CONFIG, config_values

FILES = ("src/airport_parking/cli.py", "src/airport_parking/context_storage.py",
         "src/airport_parking/collectors/context_api.py")
REMOTE = r'''
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

root = Path(REMOTE_DIR).resolve()
zone = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True, text=True, check=True).stdout.strip()
if zone != "Asia/Seoul":
    raise RuntimeError("Server cron timezone must be Asia/Seoul")
dirty = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
backup = root / "local-work" / "context-deploy" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
backup.mkdir(parents=True)
hashes = {}
for relative, encoded in FILES.items():
    target = root / relative
    if relative in dirty and target.read_bytes() != base64.b64decode(encoded):
        raise RuntimeError("Refusing to overwrite modified server file: " + relative)
for relative, encoded in FILES.items():
    target = root / relative
    payload = base64.b64decode(encoded)
    if relative in dirty and target.read_bytes() != payload:
        raise RuntimeError("Refusing to overwrite modified server file: " + relative)
    if target.exists():
        archived = backup / relative
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(target.read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".deploy")
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    hashes[relative] = hashlib.sha256(payload).hexdigest()
    if hashlib.sha256(target.read_bytes()).hexdigest() != hashes[relative]:
        raise RuntimeError("Deployment checksum mismatch")
subprocess.run([str(root / ".venv/bin/airport-parking"), "init-context-db"], cwd=root, check=True)
existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
if existing.returncode not in (0, 1):
    raise RuntimeError("Cannot read crontab")
previous = existing.stdout
(backup / "crontab.txt").write_text(previous, encoding="utf-8")
marker = "# airport-parking managed context "
lines = [line for line in previous.splitlines() if marker not in line]
jobs = []
for schedule, command, lock, logfile in (
    ("5 9 * * *", "collect-flights --days 2", ".flights.lock", "flights.log"),
    ("10 17 * * *", "collect-flights --days 2", ".flights.lock", "flights.log"),
    ("10 23 * * *", "collect-flights --days 2", ".flights.lock", "flights.log"),
    ("40 16 * * 1", "collect-holidays", ".holidays.lock", "holidays.log"),
):
    jobs.append(schedule + " cd " + shlex.quote(str(root)) + " && /usr/bin/flock -w 120 " + lock +
                " .venv/bin/airport-parking " + command + " >> logs/" + logfile + " 2>&1 " + marker + command)
(root / "logs").mkdir(exist_ok=True)
updated = "\n".join(lines + jobs) + "\n"
subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
installed = subprocess.run(["crontab", "-l"], check=True, capture_output=True, text=True).stdout
if installed != updated:
    raise RuntimeError("Cron registration verification failed")
print(json.dumps({"status":"deployed", "backup":str(backup), "timezone":zone, "jobs":jobs, "sha256":hashes}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    host, key, remote_dir, _ = config_values(args.config)
    files = {name: base64.b64encode((ROOT / name).read_bytes()).decode("ascii") for name in FILES}
    script = "REMOTE_DIR = " + repr(remote_dir) + "\nFILES = " + repr(files) + "\n" + REMOTE
    result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "-o", "ConnectTimeout=15", "-i", str(key), host, "python3", "-"],
                            input=script.encode("utf-8"), capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    print(result.stdout.decode("utf-8"))


if __name__ == "__main__":
    main()
