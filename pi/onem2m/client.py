"""Minimal oneM2M HTTP-binding client (TS-0009), standard library only.

Only what the AE needs: create, retrieve and update, with the oneM2M request headers, a bearer
token, and the response status code (RSC) surfaced so callers can decide what counts as success.
Behaviour was checked against ACME 2026.5.3; see docs/onem2m.md.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

# Resource types (ty) used here
TY_ACP = 1
TY_AE = 2
TY_CONTAINER = 3
TY_CONTENT_INSTANCE = 4
TY_TIME_SERIES = 29
TY_TIME_SERIES_INSTANCE = 30

# Response status codes (RSC) the AE acts on
RSC_OK = 2000
RSC_CREATED = 2001
RSC_DELETED = 2002
RSC_UPDATED = 2004
RSC_NOT_FOUND = 4004
RSC_ORIGINATOR_NO_PRIVILEGE = 4103
RSC_CONFLICT = 4105              # a resource with that name (or TSI with that dgt) exists
RSC_ALREADY_REGISTERED = 4117    # this originator already has an AE


@dataclass
class Response:
    status: int                   # HTTP status
    rsc: int | None               # oneM2M response status code (X-M2M-RSC)
    content: Any                  # parsed JSON body, or text if not JSON

    @property
    def ok(self) -> bool:
        return self.rsc in (RSC_OK, RSC_CREATED, RSC_DELETED, RSC_UPDATED)

    def resource(self, key: str) -> dict:
        """The resource body, e.g. resource("m2m:ts")."""
        if isinstance(self.content, dict) and isinstance(self.content.get(key), dict):
            return self.content[key]
        return {}

    def describe(self) -> str:
        if isinstance(self.content, dict) and "m2m:dbg" in self.content:
            return f"{self.status}/{self.rsc}: {self.content['m2m:dbg']}"
        return f"{self.status}/{self.rsc}"


class CSEUnreachable(Exception):
    """The CSE could not be contacted at all (refused, timed out, DNS)."""


class CSEClient:
    def __init__(self, base_url: str, cse_rn: str, originator: str, token: str = "",
                 rvi: str = "3", timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.cse_rn = cse_rn
        self.originator = originator
        self.token = token
        self.rvi = rvi
        self.timeout_s = timeout_s

    def path(self, *parts: str) -> str:
        """Structured, CSE-relative path, e.g. path("HydroPi-1", "readings")."""
        return "/".join((self.cse_rn, *parts))

    def request(self, method: str, path: str, ty: int | None = None,
                body: dict | None = None, query: str = "") -> Response:
        headers = {
            "X-M2M-Origin": self.originator,
            "X-M2M-RI": uuid.uuid4().hex,
            "X-M2M-RVI": self.rvi,
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json" + (f";ty={ty}" if ty is not None else "")
        url = f"{self.base_url}/{path}{('?' + query) if query else ''}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return _response(resp.status, resp.headers.get("X-M2M-RSC"), resp.read())
        except urllib.error.HTTPError as exc:
            return _response(exc.code, exc.headers.get("X-M2M-RSC"), exc.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise CSEUnreachable(f"{method} {url}: {exc}") from exc

    def create(self, parent: str, ty: int, key: str, attributes: dict) -> Response:
        return self.request("POST", parent, ty=ty, body={key: attributes})

    def retrieve(self, path: str, query: str = "") -> Response:
        return self.request("GET", path, query=query)

    def update(self, path: str, key: str, attributes: dict) -> Response:
        return self.request("PUT", path, body={key: attributes})


def _response(status: int, rsc: str | None, raw: bytes) -> Response:
    try:
        content: Any = json.loads(raw) if raw else None
    except ValueError:
        content = raw.decode(errors="replace")
    try:
        code = int(rsc) if rsc else None
    except ValueError:
        code = None
    return Response(status=status, rsc=code, content=content)
