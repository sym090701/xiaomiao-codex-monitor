#!/usr/bin/env python3
"""Upgrade legacy archive JSONL rows to the schema-versioned time format."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from machw_archive import JsonlRsyncArchive  # noqa: E402

_OLD_META = {"captured_at_epoch", "captured_at_utc", "received_at_epoch",
             "received_at_utc", "received_at_local"}


def upgrade_row(row, timezone_name="Asia/Shanghai"):
    if row.get("schema_version") == 2:
        return row
    epoch = row.get("captured_at_epoch", row.get("received_at_epoch"))
    if not isinstance(epoch, (int, float)):
        raise ValueError("legacy row has no numeric capture timestamp")
    record_type = "codex_usage" if "usage" in row else "mac_hardware"
    helper = JsonlRsyncArchive(timezone_name=timezone_name)
    output = helper._metadata(epoch, record_type)
    if record_type == "codex_usage":
        output["usage"] = row["usage"]
    else:
        output.update({key: value for key, value in row.items() if key not in _OLD_META})
        output["received_at_epoch"] = epoch
        output["received_at_utc"] = output["observed_at_utc"]
        output["received_at_local"] = output["observed_at_local"]
    return output


def migrate_file(path, timezone_name="Asia/Shanghai", backup=True):
    path = Path(path)
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(upgrade_row(json.loads(line), timezone_name))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    if backup:
        backup_path = path.with_suffix(path.suffix + ".schema1.bak")
        if not backup_path.exists():
            backup_path.write_bytes(path.read_bytes())
            backup_path.chmod(0o600)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                               for row in rows), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    path.chmod(0o600)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(migrate_file(args.path, args.timezone, backup=not args.no_backup))
