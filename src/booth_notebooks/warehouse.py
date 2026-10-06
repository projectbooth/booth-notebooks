"""The s3 credential sidecar's scope: this workspace's lakehouse warehouse (ADR 0095, third amendment).

An s3 grant from booth-core's broker is scoped to a booth-storage ``{backendId, path}``: the location of
the workspace's lakehouse warehouse. Only booth-lakehouse knows it, so the hub asks once per spawn,
``GET /modules/lakehouse/api/warehouse`` through core's gateway, as the notebook itself (the same
hub-minted workload token its kernel gets from ``booth.platform_token()``). The endpoint is open to any
role and answers 404 when the workspace has no warehouse yet.

This is a runtime dependency booth-notebooks → booth-lakehouse, at spawn time (docs/decisions/0009).
It is deliberately soft: no answer means no s3 sidecar, never a failed spawn.
"""

from __future__ import annotations

import httpx

LAKEHOUSE_PATH = "/modules/lakehouse/api/warehouse"


class WarehouseUnavailable(Exception):
    """booth-lakehouse (or the gateway in front of it) couldn't give an answer. Not "no warehouse"."""


async def warehouse_scope(
    core_url: str, workspace: str, token: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 5.0
) -> dict | None:
    """The broker scope ``{"backendId", "path"}`` for ``workspace``'s warehouse, or None if it has none (404).

    Raises ``WarehouseUnavailable`` for anything else: unreachable, refused, 5xx, or a reply without
    both fields. Only the two scope fields are kept; nothing else of the reply goes into the pod.
    """
    url = core_url.rstrip("/") + LAKEHOUSE_PATH
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {token}", "X-Workspace": workspace})
    except httpx.HTTPError as e:
        raise WarehouseUnavailable(f"booth-lakehouse is unreachable: {e.__class__.__name__}") from e
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise WarehouseUnavailable(f"booth-lakehouse answered HTTP {resp.status_code}")
    try:
        data = resp.json()
        backend_id, path = data["backendId"], data["path"]
    except (ValueError, KeyError, TypeError) as e:
        raise WarehouseUnavailable("booth-lakehouse returned an unreadable warehouse") from e
    if not isinstance(backend_id, str) or not isinstance(path, str) or not backend_id or not path:
        raise WarehouseUnavailable("booth-lakehouse returned a warehouse without a backendId and path")
    return {"backendId": backend_id, "path": path}
