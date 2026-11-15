"""Upload stored cycles to the oneM2M CSE, and publish camera metrics.

Runs as its own service (hydro-onem2m.service, via onem2m_sync.py), separate from the sensor
loop, so a slow or stopped CSE can never delay sampling. SQLite stays the source of truth: a row
is marked synced_onem2m only after the CSE has accepted it, so an ACME restart loses nothing.

Each cycle becomes one timeSeriesInstance whose content is config.remote_record(row), the same
record the Supabase sync and the BLE server send, and whose dataGenerationTime is the cycle
time. The CSE refuses a second instance with the same dataGenerationTime (RSC 4105), which makes
uploads safe to repeat: a duplicate means "already there".
"""

from __future__ import annotations

import time

import db
from config import (
    BASIN_IDS,
    ONEM2M_CSE_RN,
    ONEM2M_CSE_URL,
    ONEM2M_ORIGINATOR,
    ONEM2M_RVI,
    ONEM2M_TIMEOUT_S,
    ONEM2M_TOKEN,
    ONEM2M_UPLOAD_BATCH,
    ONEM2M_UPLOAD_INTERVAL_S,
    REMOTE_COLUMNS,
    remote_record,
)
from onem2m.client import (
    RSC_CONFLICT,
    RSC_NOT_FOUND,
    TY_CONTENT_INSTANCE,
    TY_TIME_SERIES_INSTANCE,
    CSEClient,
    CSEUnreachable,
)
from onem2m.provision import Paths, ProvisionError, provision

SYNC_COLUMN = "synced_onem2m"
_MAX_BACKOFF_S = 60


class ResourcesMissing(Exception):
    """The CSE answered 404 for our resources, e.g. after its database was reset."""


def make_client() -> CSEClient:
    return CSEClient(ONEM2M_CSE_URL, ONEM2M_CSE_RN, ONEM2M_ORIGINATOR, ONEM2M_TOKEN,
                     rvi=ONEM2M_RVI, timeout_s=ONEM2M_TIMEOUT_S)


def data_generation_time(ts: float) -> str:
    """oneM2M absolute timestamp (UTC), e.g. 20261115T062455. ACME's dgt needs absolute time."""
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(ts))


def upload_pending(conn, client: CSEClient, paths: Paths) -> int:
    """Upload one batch of unsynced cycles, oldest first. Returns how many rows were settled.

    Rows with no sensor values at all are settled without uploading, like the Supabase sync,
    so they can't sit at the front of the queue forever.
    """
    rows = db.get_unsynced_readings(conn, ONEM2M_UPLOAD_BATCH, column=SYNC_COLUMN)
    settled: list[int] = []
    try:
        for row in rows:
            record = remote_record(row)
            if all(record[remote] is None for remote in REMOTE_COLUMNS.values()):
                settled.append(row["id"])
                continue
            resp = client.create(paths.readings, TY_TIME_SERIES_INSTANCE, "m2m:tsi", {
                "dgt": data_generation_time(row["ts"]),
                "con": record,
            })
            if resp.ok or resp.rsc == RSC_CONFLICT:
                settled.append(row["id"])
            elif resp.rsc == RSC_NOT_FOUND:
                raise ResourcesMissing(resp.describe())
            else:
                print(f"[onem2m] upload refused, will retry: {resp.describe()}")
                break
    finally:
        db.mark_synced(conn, settled, column=SYNC_COLUMN)  # keep progress even if we bail out
    return len(settled)


def publish_vision(metrics: dict) -> bool:
    """Store one set of camera metrics in the vision container. For camera.py.

    Provisions first (cheap, and safe to repeat) so it works even before the uploader has run.
    Returns False instead of raising, so a CSE problem never stops the camera or refill loop.
    """
    client = make_client()
    try:
        paths = provision(client)
        resp = client.create(paths.vision, TY_CONTENT_INSTANCE, "m2m:cin", {"con": metrics})
    except (CSEUnreachable, ProvisionError) as exc:
        print(f"[onem2m] vision upload failed: {exc}")
        return False
    if not resp.ok:
        print(f"[onem2m] vision upload refused: {resp.describe()}")
    return resp.ok


def run() -> None:
    """Provision, then keep the CSE up to date with SQLite. Never exits on CSE trouble."""
    if len(BASIN_IDS) > 1:
        # Basins in one cycle share a timestamp, and the CSE allows one instance per
        # dataGenerationTime, so a second basin's row would be dropped as a duplicate.
        raise SystemExit("oneM2M mode supports one basin; give each basin its own TimeSeries first")

    conn = db.connect()
    client = make_client()
    paths: Paths | None = None
    backoff = ONEM2M_UPLOAD_INTERVAL_S
    print(f"[onem2m] uploading to {ONEM2M_CSE_URL}/{ONEM2M_CSE_RN} as {ONEM2M_ORIGINATOR}")

    while True:
        try:
            if paths is None:
                paths = provision(client)
                print(f"[onem2m] provisioned {paths.ae}")
            settled = upload_pending(conn, client, paths)
            if settled:
                print(f"[onem2m] uploaded {settled} cycle(s)")
            backoff = ONEM2M_UPLOAD_INTERVAL_S
            # A full batch means a backlog: carry straight on with the next one
            time.sleep(0 if settled >= ONEM2M_UPLOAD_BATCH else ONEM2M_UPLOAD_INTERVAL_S)
        except ResourcesMissing as exc:
            print(f"[onem2m] resources missing ({exc}); provisioning again")
            paths = None
        except (CSEUnreachable, ProvisionError) as exc:
            print(f"[onem2m] {exc}; retrying in {backoff} s")
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
