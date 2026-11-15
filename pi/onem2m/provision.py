"""Create this Pi's resources on the CSE. Safe to run on every start.

    <cseBase>
    └─ HydroPi-<hash>   AE, lbl=[type:hydropi]
       ├─ acp           accessControlPolicy: this AE full access; the app read + discover only
       ├─ readings      timeSeries, one instance per 30 s cycle, missing-data detection on
       └─ vision        container, camera.py metrics

"Already exists" answers (RSC 4105 for a name, 4117 for an originator that is already
registered) count as success, so a restart or a second run changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    ONEM2M_AE_RN,
    ONEM2M_APP_ID,
    ONEM2M_LABEL,
    ONEM2M_MISSING_DATA_TIMER_MS,
    ONEM2M_READER_ORIGINATORS,
    ONEM2M_READINGS_MNI,
    ONEM2M_RVI,
    ONEM2M_VISION_MNI,
    SAMPLE_INTERVAL_S,
    SYSTEM_NAME,
)
from onem2m.client import (
    RSC_ALREADY_REGISTERED,
    RSC_CONFLICT,
    TY_ACP,
    TY_AE,
    TY_CONTAINER,
    TY_TIME_SERIES,
    CSEClient,
    Response,
)

ACP_RN = "acp"
READINGS_RN = "readings"
VISION_RN = "vision"

# accessControlOperations bit flags (TS-0004)
OP_CREATE, OP_RETRIEVE, OP_UPDATE, OP_DELETE, OP_NOTIFY, OP_DISCOVER = 1, 2, 4, 8, 16, 32
OP_ALL = OP_CREATE | OP_RETRIEVE | OP_UPDATE | OP_DELETE | OP_NOTIFY | OP_DISCOVER   # 63
OP_READ_ONLY = OP_RETRIEVE | OP_DISCOVER                                              # 34


class ProvisionError(Exception):
    pass


@dataclass
class Paths:
    ae: str
    readings: str
    vision: str


def _require(resp: Response, step: str, *also_ok: int) -> Response:
    if resp.ok or resp.rsc in also_ok:
        return resp
    raise ProvisionError(f"{step} failed: {resp.describe()}")


def _privileges(owner: str) -> dict:
    rules = [{"acor": [owner], "acop": OP_ALL}]
    if ONEM2M_READER_ORIGINATORS:
        rules.append({"acor": ONEM2M_READER_ORIGINATORS, "acop": OP_READ_ONLY})
    return {"acr": rules}


def provision(client: CSEClient) -> Paths:
    """Register the AE and make sure its access policy, readings and vision resources exist."""
    ae_path = client.path(ONEM2M_AE_RN)
    acp_path = client.path(ONEM2M_AE_RN, ACP_RN)

    _require(client.create(client.path(), TY_AE, "m2m:ae", {
        "rn": ONEM2M_AE_RN,
        "api": ONEM2M_APP_ID,
        "rr": False,                         # behind NAT: the CSE can't call us
        "srv": [ONEM2M_RVI],
        "lbl": [ONEM2M_LABEL, f"system:{SYSTEM_NAME}"],
    }), "register AE", RSC_ALREADY_REGISTERED)

    # Access policy under the AE. On a re-run, refresh it so a changed reader list takes effect.
    acp_body = {"pv": _privileges(client.originator), "pvs": {"acr": [{"acor": [client.originator], "acop": OP_ALL}]}}
    resp = client.create(ae_path, TY_ACP, "m2m:acp", {"rn": ACP_RN, **acp_body})
    if resp.rsc == RSC_CONFLICT:
        resp = _require(client.update(acp_path, "m2m:acp", {"pv": acp_body["pv"]}), "update ACP")
    acp_ri = _require(resp, "create ACP").resource("m2m:acp").get("ri")
    if not acp_ri:
        raise ProvisionError(f"ACP has no resource ID: {resp.describe()}")

    # Separate updates: the CSE requires acpi to be the only attribute in its update request
    _require(client.update(ae_path, "m2m:ae", {"acpi": [acp_ri]}), "attach ACP to AE")
    # The label carries the friendly system name, which can change between runs
    _require(client.update(ae_path, "m2m:ae", {"lbl": [ONEM2M_LABEL, f"system:{SYSTEM_NAME}"]}),
             "update AE labels")

    _require(client.create(ae_path, TY_TIME_SERIES, "m2m:ts", {
        "rn": READINGS_RN,
        "mni": ONEM2M_READINGS_MNI,
        "pei": SAMPLE_INTERVAL_S * 1000,     # expected period, ms
        "mdd": True,                         # flag cycles that never arrive
        "mdt": ONEM2M_MISSING_DATA_TIMER_MS,  # ACME requires mdt whenever mdd is on
        "mdn": 10,
        "acpi": [acp_ri],
    }), "create readings timeSeries", RSC_CONFLICT)

    _require(client.create(ae_path, TY_CONTAINER, "m2m:cnt", {
        "rn": VISION_RN, "mni": ONEM2M_VISION_MNI, "acpi": [acp_ri],
    }), "create vision container", RSC_CONFLICT)

    return Paths(ae=ae_path, readings=client.path(ONEM2M_AE_RN, READINGS_RN),
                 vision=client.path(ONEM2M_AE_RN, VISION_RN))
