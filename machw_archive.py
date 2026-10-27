"""Durable Mac hardware-metric spool and optional NAS synchronizer."""
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import re


_SAFE_HOST = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_host(name):
    value = _SAFE_HOST.sub("_", name).strip("._-")
    return (value or "mac")[:80]


class MacHardwareArchive:
    """Append validated metrics locally, then rsync the spool to a NAS.

    The local spool is the source of truth. A NAS outage only delays sync and
    never blocks the quota or WebSocket request path.
    """

    def __init__(self, local_dir=None, remote=None, ssh_key=None,
                 known_hosts=None, interval=60):
        self.local_dir = Path(local_dir) if local_dir else None
        self.remote = remote
        self.ssh_key = ssh_key
        self.known_hosts = known_hosts
        self.interval = max(30, int(interval))
        if self.local_dir:
            self.local_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.local_dir, 0o700)

    @classmethod
    def from_env(cls):
        local_dir = os.environ.get("MACHWLOGS_LOCAL_DIR")
        remote = os.environ.get("MACHWLOGS_REMOTE")
        if not local_dir:
            return cls()
        return cls(local_dir, remote=remote,
                   ssh_key=os.environ.get("MACHWLOGS_SSH_KEY"),
                   known_hosts=os.environ.get("MACHWLOGS_KNOWN_HOSTS"),
                   interval=int(os.environ.get("MACHWLOGS_SYNC_INTERVAL", "60")))

    @property
    def enabled(self):
        return self.local_dir is not None

    def record(self, metrics, received_epoch=None):
        if not self.enabled:
            return
        received_epoch = received_epoch or dt.datetime.now(dt.timezone.utc).timestamp()
        stamp = dt.datetime.fromtimestamp(received_epoch, dt.timezone.utc)
        host = _safe_host(metrics.get("host_name", "mac"))
        directory = self.local_dir / host
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / (stamp.strftime("%Y-%m-%d") + ".ndjson")
        row = dict(metrics)
        row["received_at_epoch"] = received_epoch
        row["received_at_utc"] = stamp.isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)

    async def sync_once(self):
        if not self.enabled or not self.remote:
            return False
        if not self.ssh_key or not self.known_hosts:
            raise RuntimeError("NAS sync requires MACHWLOGS_SSH_KEY and MACHWLOGS_KNOWN_HOSTS")
        ssh = ("ssh", "-i", self.ssh_key, "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o",
               "UserKnownHostsFile=" + self.known_hosts)
        rsync = ("rsync", "-a", "--partial", "--chmod=F600,D700", "-e",
                 " ".join(ssh), str(self.local_dir) + "/", self.remote.rstrip("/") + "/")
        process = await asyncio.create_subprocess_exec(*rsync,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()[-300:]
            raise RuntimeError("NAS sync failed" + (": " + detail if detail else ""))
        return True

    async def sync_loop(self):
        while True:
            try:
                await self.sync_once()
            except (OSError, asyncio.TimeoutError, RuntimeError):
                pass
            await asyncio.sleep(self.interval)
