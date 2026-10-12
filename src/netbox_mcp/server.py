#!/usr/bin/env python3
"""NetBox MCP server: typed tools over the NetBox REST API (stdio transport)."""

import hashlib
import json
import os
import sys
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

def _auth_header(token: str) -> str:
    """Authorization value: use verbatim if it already carries a scheme, else Bearer for nbt_ (v2) or Token."""
    token = token.strip()
    if token.lower().startswith(("bearer ", "token ")):
        return token
    return f"Bearer {token}" if token.startswith("nbt_") else f"Token {token}"


_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Create the HTTP client on first use from the environment (cached); tests may assign `_client` directly."""
    global _client
    if _client is None:
        url = os.environ.get("NETBOX_URL", "").strip().rstrip("/")
        token = os.environ.get("NETBOX_TOKEN", "").strip()
        verify = os.environ.get("NETBOX_VERIFY_TLS", "true").strip().lower() not in ("0", "false", "no", "off")
        _client = httpx.Client(
            base_url=f"{url}/api",
            headers={
                "Authorization": _auth_header(token),
                "Accept": "application/json",
            },
            verify=verify,
            timeout=30.0,
        )
    return _client


mcp = FastMCP("netbox")


class NetBoxError(RuntimeError):
    """NetBox answered with an HTTP error status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _request(method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
    url = f"/{path.strip('/')}/"
    try:
        resp = _get_client().request(method, url, params=params, json=body)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"NetBox request failed: {method} {url}: {type(exc).__name__}: {exc}") from None
    if resp.status_code >= 400:
        detail = resp.text.strip()[:2000]
        raise NetBoxError(f"NetBox {method} {url} -> HTTP {resp.status_code}: {detail}", resp.status_code)
    if resp.status_code == 204 or not resp.content:
        return {"status": "ok"}
    return resp.json()


def _mode() -> str:
    """"direct" only when NETBOX_DRY_RUN is explicitly 0/false/no/off; anything else is "confirm". Read per call so it can be toggled."""
    return "direct" if os.environ.get("NETBOX_DRY_RUN", "").strip().lower() in ("0", "false", "no", "off") else "confirm"


PREVIEW_TTL_S = 600
_PREVIEWS: dict[str, float] = {}  # fingerprint -> expiry (monotonic seconds); in-memory, lost on restart


def _now() -> float:
    return time.monotonic()


def _fingerprint(action: str, path: str, id: int | None, data: dict | None) -> str:
    """Hash of (action, endpoint, id, canonical JSON of the requested data); the update diff is never part of it."""
    canon = json.dumps([action, path, id, data], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def _approve(result: dict, fp: str, mode: str) -> dict:
    """Record a preview so the identical write may be confirmed, and expose its short id."""
    now = _now()
    for k in [k for k, exp in _PREVIEWS.items() if exp <= now]:
        del _PREVIEWS[k]
    _PREVIEWS[fp] = now + PREVIEW_TTL_S
    result["mode"] = mode
    result["preview_id"] = fp[:12]
    result["expires_in_s"] = PREVIEW_TTL_S
    if mode == "confirm":
        result["next_step"] = f'To apply, call the same tool again with the identical arguments plus confirm="{fp[:12]}".'
    return result


def _consume(fp: str, confirm: str) -> None:
    """Single-use gate for confirm mode: needs an unexpired preview of this exact change AND its id; the preview is then removed."""
    exp = _PREVIEWS.get(fp)
    if exp is None or exp <= _now():
        raise RuntimeError("No matching or expired preview for this exact change; call the tool without confirm first, review the preview, then repeat with confirm=<preview_id>.")
    if confirm != fp[:12]:
        raise RuntimeError("confirm does not match the preview_id of this exact change; call the tool without confirm first and use the preview_id it returns.")
    del _PREVIEWS[fp]


def _should_preview(dry_run: bool | None, confirm: str | None, fp: str) -> tuple[bool, str]:
    """(preview?, mode). Confirm mode: preview unless confirm validates (consumed here). Direct mode: preview only if dry_run is true."""
    mode = _mode()
    if dry_run:
        return True, mode
    if mode == "direct":
        return False, mode
    if not confirm:
        return True, mode
    _consume(fp, confirm)
    return False, mode


def _normalise(value: Any) -> Any:
    """Reduce a NetBox nested object ({"id",...}) to its id and a choice ({"value","label"}) to its value."""
    if isinstance(value, dict):
        if "id" in value:
            return value["id"]
        if "value" in value and "label" in value:
            return value["value"]
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _fetch(label: str, path: str, id: int) -> dict:
    """GET one object for a dry run, turning a 404 into a readable error."""
    try:
        return _request("GET", f"{path}/{id}")
    except NetBoxError as exc:
        if exc.status_code == 404:
            raise RuntimeError(f"NetBox {label} {id} not found (HTTP 404)") from None
        raise


# stem -> (plural, REST path, brief fields)
REGISTRY: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "site": ("sites", "dcim/sites", ("name", "slug", "status", "tenant", "description")),
    "prefix": ("prefixes", "ipam/prefixes", ("prefix", "status", "vrf", "vlan", "site", "role", "description")),
    "ip_address": ("ip_addresses", "ipam/ip-addresses", ("address", "status", "vrf", "dns_name", "assigned_object", "description")),
    "vlan": ("vlans", "ipam/vlans", ("vid", "name", "status", "site", "group", "description")),
    "vrf": ("vrfs", "ipam/vrfs", ("name", "rd", "tenant", "description")),
    "device": ("devices", "dcim/devices", ("name", "device_type", "role", "site", "status", "primary_ip4", "description")),
    "interface": ("interfaces", "dcim/interfaces", ("name", "device", "type", "enabled", "mac_address", "description")),
    "device_type": ("device_types", "dcim/device-types", ("model", "slug", "manufacturer", "u_height")),
    "manufacturer": ("manufacturers", "dcim/manufacturers", ("name", "slug", "description")),
    "device_role": ("device_roles", "dcim/device-roles", ("name", "slug", "color", "description")),
    "tenant": ("tenants", "tenancy/tenants", ("name", "slug", "description")),
    "service": ("services", "ipam/services", ("name", "protocol", "ports", "parent", "ipaddresses", "description")),
    "cable": ("cables", "dcim/cables", ("label", "status", "a_terminations", "b_terminations", "type")),
    "tag": ("tags", "extras/tags", ("name", "slug", "color", "description")),
    "location": ("locations", "dcim/locations", ("name", "slug", "site", "parent", "status", "description")),
    # dcim
    "rack": ("racks", "dcim/racks", ("name", "site", "location", "status", "role", "u_height", "description")),
    "rack_role": ("rack_roles", "dcim/rack-roles", ("name", "slug", "color", "description")),
    "region": ("regions", "dcim/regions", ("name", "slug", "parent", "description")),
    "site_group": ("site_groups", "dcim/site-groups", ("name", "slug", "parent", "description")),
    "platform": ("platforms", "dcim/platforms", ("name", "slug", "manufacturer", "description")),
    "front_port": ("front_ports", "dcim/front-ports", ("name", "device", "type", "rear_port", "description")),
    "rear_port": ("rear_ports", "dcim/rear-ports", ("name", "device", "type", "positions", "description")),
    "console_port": ("console_ports", "dcim/console-ports", ("name", "device", "type", "description")),
    "console_server_port": ("console_server_ports", "dcim/console-server-ports", ("name", "device", "type", "description")),
    "power_port": ("power_ports", "dcim/power-ports", ("name", "device", "type", "description")),
    "power_outlet": ("power_outlets", "dcim/power-outlets", ("name", "device", "type", "description")),
    "power_panel": ("power_panels", "dcim/power-panels", ("name", "site", "location", "description")),
    "power_feed": ("power_feeds", "dcim/power-feeds", ("name", "power_panel", "rack", "status", "type", "description")),
    "inventory_item": ("inventory_items", "dcim/inventory-items", ("name", "device", "manufacturer", "serial", "asset_tag", "description")),
    # ipam
    "aggregate": ("aggregates", "ipam/aggregates", ("prefix", "rir", "tenant", "date_added", "description")),
    "rir": ("rirs", "ipam/rirs", ("name", "slug", "is_private", "description")),
    "ipam_role": ("ipam_roles", "ipam/roles", ("name", "slug", "weight", "description")),
    "vlan_group": ("vlan_groups", "ipam/vlan-groups", ("name", "slug", "min_vid", "max_vid", "description")),
    "route_target": ("route_targets", "ipam/route-targets", ("name", "tenant", "description")),
    "fhrp_group": ("fhrp_groups", "ipam/fhrp-groups", ("name", "protocol", "group_id", "description")),
    "asn": ("asns", "ipam/asns", ("asn", "rir", "tenant", "description")),
    # circuits
    "provider": ("providers", "circuits/providers", ("name", "slug", "description")),
    "circuit_type": ("circuit_types", "circuits/circuit-types", ("name", "slug", "description")),
    "circuit": ("circuits", "circuits/circuits", ("cid", "provider", "type", "status", "tenant", "description")),
    "circuit_termination": ("circuit_terminations", "circuits/circuit-terminations", ("circuit", "term_side", "termination", "port_speed", "description")),
    # virtualization
    "cluster_type": ("cluster_types", "virtualization/cluster-types", ("name", "slug", "description")),
    "cluster_group": ("cluster_groups", "virtualization/cluster-groups", ("name", "slug", "description")),
    "cluster": ("clusters", "virtualization/clusters", ("name", "type", "group", "status", "site", "description")),
    "virtual_machine": ("virtual_machines", "virtualization/virtual-machines", ("name", "status", "site", "cluster", "role", "primary_ip4", "description")),
    "vm_interface": ("vm_interfaces", "virtualization/interfaces", ("name", "virtual_machine", "enabled", "mac_address", "description")),
    # tenancy
    "tenant_group": ("tenant_groups", "tenancy/tenant-groups", ("name", "slug", "parent", "description")),
    "contact_group": ("contact_groups", "tenancy/contact-groups", ("name", "slug", "parent", "description")),
    "contact_role": ("contact_roles", "tenancy/contact-roles", ("name", "slug", "description")),
    "contact": ("contacts", "tenancy/contacts", ("name", "email", "phone", "group", "description")),
    # extras
    "journal_entry": ("journal_entries", "extras/journal-entries", ("assigned_object_type", "assigned_object_id", "kind", "comments")),
}


# stem -> dependent-object counts for the delete preview: (label, REST path, {filter param: field of the object to filter by}).
# Only filters NetBox supports are used; a param whose source field is empty on the object is omitted.
IMPACT: dict[str, tuple[tuple[str, str, dict[str, str]], ...]] = {
    "site": (
        ("devices", "dcim/devices", {"site_id": "id"}),
        ("racks", "dcim/racks", {"site_id": "id"}),
        ("prefixes", "ipam/prefixes", {"site_id": "id"}),
        ("vlans", "ipam/vlans", {"site_id": "id"}),
    ),
    "vlan": (
        ("prefixes", "ipam/prefixes", {"vlan_id": "id"}),
        ("interfaces", "dcim/interfaces", {"vlan_id": "id"}),
    ),
    "prefix": (
        ("child_prefixes", "ipam/prefixes", {"within": "prefix", "vrf_id": "vrf"}),
        ("ip_addresses", "ipam/ip-addresses", {"parent": "prefix", "vrf_id": "vrf"}),
    ),
    "vrf": (
        ("prefixes", "ipam/prefixes", {"vrf_id": "id"}),
        ("ip_addresses", "ipam/ip-addresses", {"vrf_id": "id"}),
    ),
    "device": (
        ("interfaces", "dcim/interfaces", {"device_id": "id"}),
        ("ip_addresses", "ipam/ip-addresses", {"device_id": "id"}),
    ),
    "interface": (
        ("ip_addresses", "ipam/ip-addresses", {"interface_id": "id"}),
    ),
    "location": (
        ("devices", "dcim/devices", {"location_id": "id"}),
        ("racks", "dcim/racks", {"location_id": "id"}),
        ("child_locations", "dcim/locations", {"parent_id": "id"}),
    ),
    "rack": (("devices", "dcim/devices", {"rack_id": "id"}),),
    "region": (
        ("sites", "dcim/sites", {"region_id": "id"}),
        ("child_regions", "dcim/regions", {"parent_id": "id"}),
    ),
    "site_group": (
        ("sites", "dcim/sites", {"group_id": "id"}),
        ("child_site_groups", "dcim/site-groups", {"parent_id": "id"}),
    ),
    "platform": (
        ("devices", "dcim/devices", {"platform_id": "id"}),
        ("virtual_machines", "virtualization/virtual-machines", {"platform_id": "id"}),
    ),
    "vlan_group": (("vlans", "ipam/vlans", {"group_id": "id"}),),
    "rir": (("aggregates", "ipam/aggregates", {"rir_id": "id"}),),
    "ipam_role": (
        ("prefixes", "ipam/prefixes", {"role_id": "id"}),
        ("vlans", "ipam/vlans", {"role_id": "id"}),
    ),
    "provider": (("circuits", "circuits/circuits", {"provider_id": "id"}),),
    "circuit_type": (("circuits", "circuits/circuits", {"type_id": "id"}),),
    "cluster_type": (("clusters", "virtualization/clusters", {"type_id": "id"}),),
    "cluster_group": (("clusters", "virtualization/clusters", {"group_id": "id"}),),
    "cluster": (
        ("virtual_machines", "virtualization/virtual-machines", {"cluster_id": "id"}),
        ("devices", "dcim/devices", {"cluster_id": "id"}),
    ),
    "virtual_machine": (("interfaces", "virtualization/interfaces", {"virtual_machine_id": "id"}),),
    "tenant_group": (
        ("tenants", "tenancy/tenants", {"group_id": "id"}),
        ("child_tenant_groups", "tenancy/tenant-groups", {"parent_id": "id"}),
    ),
}
# stem -> fields of the fetched object whose presence counts as 1 dependent (no extra request)
IMPACT_PRESENT = {"interface": ("cable",)}
# (stem, label) counts that must be DISTINCT ids: NetBox joins the tagged-VLAN table for vlan_id on interfaces,
# so `count` can include duplicate rows (e.g. 39 rows for 21 interfaces).
IMPACT_DISTINCT = {("vlan", "interfaces")}
DISTINCT_LIMIT = 1000


def _impact(stem: str, current: dict) -> dict:
    """Dependent-object counts via read-only GETs (limit=1, read `count`); "unknown" where a count can't be obtained."""
    out: dict[str, Any] = {}
    for label, path, params in IMPACT.get(stem, ()):
        query = {param: _normalise(current.get(src)) for param, src in params.items()}
        query = {k: v for k, v in query.items() if v is not None}
        try:
            if (stem, label) in IMPACT_DISTINCT:
                data = _request("GET", path, params={**query, "brief": 1, "limit": DISTINCT_LIMIT})
                results = data["results"]
                if data["count"] > len(results) and len(results) >= DISTINCT_LIMIT:
                    raise KeyError("truncated")  # more rows than one page: a distinct count would be wrong
                out[label] = len({r["id"] for r in results})
            else:
                out[label] = _request("GET", path, params={**query, "limit": 1})["count"]
        except (RuntimeError, KeyError, TypeError):
            out[label] = "unknown"
    for field in IMPACT_PRESENT.get(stem, ()):
        out[field] = 1 if current.get(field) else 0
    if stem == "ip_address":
        out["devices_using_as_primary_ip"] = _ip_primary_count(current)
    return out


def _ip_primary_count(ip: dict) -> int | str:
    """NetBox has no primary-ip filter on devices: follow assigned_object to its device and compare primary_ip4/6 ids."""
    assigned = ip.get("assigned_object")
    if not assigned:
        return 0
    try:
        if ip.get("assigned_object_type") != "dcim.interface":
            return 0
        device_id = (assigned.get("device") or {}).get("id")
        if device_id is None:
            return "unknown"
        device = _request("GET", f"dcim/devices/{device_id}")
        return int(any(_normalise(device.get(f)) == ip.get("id") for f in ("primary_ip4", "primary_ip6")))
    except (RuntimeError, AttributeError):
        return "unknown"


def _brief(item: dict, fields: tuple[str, ...]) -> dict:
    out = {"id": item.get("id"), "display": item.get("display")}
    for f in fields:
        if f in item:
            out[f] = item[f]
    return out


_GATE = (
        "Two-step in the default \"confirm\" mode: a call without `confirm` is always a preview (only GETs; any dry_run value) "
        "and returns `mode`, `preview_id`, `expires_in_s` (600) and `next_step`. Review it, then repeat the identical call with "
        "confirm=<preview_id> to apply; the preview is single-use and refused if missing, expired, for different arguments or "
        "if confirm does not match. dry_run=true always previews and never applies, even with confirm. If the server env "
        "NETBOX_DRY_RUN is 0/false/no/off the mode is \"direct\": no gate, dry_run unset/false writes immediately, dry_run=true "
        "previews, confirm is ignored."
    )


def _register(stem: str, plural: str, path: str, fields: tuple[str, ...]) -> None:
    label = stem.replace("_", " ")

    def list_objects(filters: dict | None = None, limit: int = 50, offset: int = 0, brief: bool = True) -> dict:
        params = {**(filters or {}), "limit": limit, "offset": offset}
        data = _request("GET", path, params=params)
        results = data.get("results", [])
        if brief:
            results = [_brief(r, fields) for r in results]
        return {"count": data.get("count"), "results": results}

    def get_object(id: int) -> dict:
        return _request("GET", f"{path}/{id}")

    def create_object(data: dict, dry_run: bool | None = None, confirm: str | None = None) -> dict:
        fp = _fingerprint("create", path, None, data)
        preview, mode = _should_preview(dry_run, confirm, fp)
        if preview:
            return _approve({"dry_run": True, "action": "create", "method": "POST", "path": f"/{path}/", "body": data}, fp, mode)
        return _request("POST", path, body=data)

    def update_object(id: int, data: dict, dry_run: bool | None = None, confirm: str | None = None) -> dict:
        fp = _fingerprint("update", path, id, data)
        preview, mode = _should_preview(dry_run, confirm, fp)
        if preview:
            current = _fetch(label, path, id)
            changes: dict[str, dict] = {}
            unchanged: list[str] = []
            for field, new in data.items():
                old = _normalise(current.get(field))
                if old == new:
                    unchanged.append(field)
                else:
                    changes[field] = {"from": old, "to": new}
            result = {"dry_run": True, "action": "update", "id": id, "current_display": current.get("display"), "changes": changes, "unchanged": unchanged}
            if stem == "device" and any(f in data for f in ("primary_ip4", "primary_ip6")):
                result["touches_primary_ip"] = True
            return _approve(result, fp, mode)
        return _request("PATCH", f"{path}/{id}", body=data)

    def delete_object(id: int, dry_run: bool | None = None, confirm: str | None = None) -> dict:
        fp = _fingerprint("delete", path, id, None)
        preview, mode = _should_preview(dry_run, confirm, fp)
        if preview:
            current = _fetch(label, path, id)
            result = {"dry_run": True, "action": "delete", "id": id, "object": _brief(current, fields), "impact": _impact(stem, current), "warning": "Deletion is irreversible and may cascade"}
            return _approve(result, fp, mode)
        return _request("DELETE", f"{path}/{id}")

    list_objects.__doc__ = (
        f"List NetBox {plural.replace('_', ' ')} ({path}). `filters` maps NetBox filter names to values "
        f"(e.g. {{\"q\": \"text\"}}, {{\"site\": \"slug\"}}). Returns {{count, results}}; "
        f"set brief=false for full objects."
    )
    get_object.__doc__ = f"Get one NetBox {label} by numeric id (full object)."
    gate = _GATE
    create_object.__doc__ = f"Create a NetBox {label}. `data` is the JSON body per the NetBox API. Dry-run returns the POST that would be made. {gate}"
    update_object.__doc__ = (
        f"Partially update (PATCH) a NetBox {label} by id. `data` holds only changed fields. Dry-run does a GET and returns a "
        f"from/to diff; the preview matches the requested `data`, so apply with the same data. {gate}"
    )
    delete_object.__doc__ = (
        f"Delete a NetBox {label} by id. DESTRUCTIVE and irreversible when real; may cascade. Dry-run does a GET and returns "
        f"the object plus an `impact` dict of dependent-object counts (\"unknown\" where a count is unavailable). {gate}"
    )

    mcp.tool(name=f"list_{plural}", annotations=ToolAnnotations(readOnlyHint=True))(list_objects)
    mcp.tool(name=f"get_{stem}", annotations=ToolAnnotations(readOnlyHint=True))(get_object)
    mcp.tool(name=f"create_{stem}", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))(create_object)
    mcp.tool(name=f"update_{stem}", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))(update_object)
    mcp.tool(name=f"delete_{stem}", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))(delete_object)


for _stem, (_plural, _path, _fields) in REGISTRY.items():
    _register(_stem, _plural, _path, _fields)


# Allocation helpers: NetBox's "next available" sub-endpoints on prefixes (ipam/prefixes/{id}/available-ips|available-prefixes).
_AVAILABLE = {"ips": "available-ips", "prefixes": "available-prefixes"}


def _register_available(kind: str, noun: str) -> None:
    sub = _AVAILABLE[kind]

    def list_available(prefix_id: int, limit: int = 50) -> Any:
        return _request("GET", f"ipam/prefixes/{prefix_id}/{sub}", params={"limit": limit})

    def create_available(prefix_id: int, data: dict | None = None, dry_run: bool | None = None, confirm: str | None = None) -> Any:
        endpoint = f"ipam/prefixes/{prefix_id}/{sub}"
        body = data if data is not None else ({} if kind == "ips" else None)
        if kind == "prefixes" and not (body and body.get("prefix_length")):
            raise RuntimeError("data must include prefix_length (e.g. {\"prefix_length\": 28}) to allocate a child prefix")
        fp = _fingerprint("create", endpoint, None, body)
        preview, mode = _should_preview(dry_run, confirm, fp)
        if preview:
            available = _request("GET", endpoint, params={"limit": 1})
            result = {"dry_run": True, "action": "create", "method": "POST", "path": f"/{endpoint}/", "body": body, "first_available": available[0] if isinstance(available, list) and available else None}
            return _approve(result, fp, mode)
        return _request("POST", endpoint, body=body)

    list_available.__doc__ = f"List available (unallocated) {noun} inside the prefix with this numeric id. Read-only."
    create_available.__doc__ = (
        f"Allocate the next available {noun[:-1] if noun.endswith('s') else noun} inside a prefix (POST {sub}). `data` holds the new object's fields"
        + (" (empty allocates with defaults)." if kind == "ips" else " and must include prefix_length.")
        + " Dry-run shows the POST and the first free candidate (GET only). " + _GATE
    )
    mcp.tool(name=f"list_available_{kind}", annotations=ToolAnnotations(readOnlyHint=True))(list_available)
    mcp.tool(name=f"create_available_{kind[:-1] if kind == 'ips' else 'prefix'}", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))(create_available)


_register_available("ips", "IP addresses")
_register_available("prefixes", "child prefixes")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def netbox_status() -> dict:
    """Return NetBox /api/status/ (version, plugins, installed apps)."""
    return _request("GET", "status")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def netbox_search(q: str, limit: int = 10) -> dict:
    """Free-text search (?q=) across sites, prefixes, devices, IP addresses and VLANs. Returns brief results per type."""
    out: dict[str, Any] = {}
    for stem in ("site", "prefix", "device", "ip_address", "vlan"):
        plural, path, fields = REGISTRY[stem]
        try:
            data = _request("GET", path, params={"q": q, "limit": limit})
            out[plural] = {"count": data.get("count"), "results": [_brief(r, fields) for r in data.get("results", [])]}
        except RuntimeError as exc:
            out[plural] = {"error": str(exc)}
    return out


def main() -> None:
    """Validate the environment, then serve MCP over stdio."""
    if not os.environ.get("NETBOX_TOKEN", "").strip():
        print("netbox-mcp: NETBOX_TOKEN is not set; export it before starting the server", file=sys.stderr)
        sys.exit(1)
    url = os.environ.get("NETBOX_URL", "").strip()
    if not url.startswith(("http://", "https://")):
        print("netbox-mcp: NETBOX_URL is required and must start with http:// or https:// (e.g. https://netbox.example.com)", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
