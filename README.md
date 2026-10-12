# netbox-mcp

Small stdio MCP server (official `mcp` SDK, FastMCP) wrapping the NetBox REST API so an MCP client such as Claude Code can read and edit a NetBox instance.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .          # add '.[dev]' for the test suite
export NETBOX_URL=https://netbox.example.com
export NETBOX_TOKEN=...             # NetBox UI -> Admin -> API Tokens
```

The console script is `.venv/bin/netbox-mcp` (also `python -m netbox_mcp`). Both `NETBOX_URL` and `NETBOX_TOKEN` are validated at startup; the server prints a message to stderr and exits 1 if either is missing or `NETBOX_URL` does not start with `http://` or `https://`.

A read-only token is enough for the list/get tools; create/update/delete need a write-enabled token.

Run the tests with `.venv/bin/python -m pytest -q` (offline, mocked HTTP).

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `NETBOX_TOKEN` | required | API token, with or without the scheme prefix. `Bearer ...` / `Token ...` (case-insensitive) is sent verbatim; bare `nbt_...` (v2) is sent as `Bearer`; any other bare value as `Token` |
| `NETBOX_URL` | required | NetBox base URL, must start with `http://` or `https://` |
| `NETBOX_VERIFY_TLS` | `true` | Set `false` to skip TLS verification |
| `NETBOX_DRY_RUN` | unset (confirm mode) | `0`/`false`/`no`/`off` (case-insensitive) selects direct mode; unset, empty or any other value selects confirm mode (see Dry run) |

## Registration (Claude Code)

Copy `.mcp.json.example` to your project's `.mcp.json` and set the absolute path of the installed script:

```json
{
  "mcpServers": {
    "netbox": {
      "command": "/absolute/path/to/netbox-mcp/.venv/bin/netbox-mcp",
      "env": {
        "NETBOX_TOKEN": "${NETBOX_TOKEN}",
        "NETBOX_URL": "${NETBOX_URL}",
        "NETBOX_DRY_RUN": "${NETBOX_DRY_RUN:-1}"
      }
    }
  }
}
```

Export `NETBOX_TOKEN` and `NETBOX_URL` in your shell before launching Claude Code, then check with `claude mcp list`.

## Safety model

Confirm mode (the default) is a preview-before-apply gate for the same caller: it makes the caller look at exactly what will change before it changes. It is not an authorization boundary, because the caller that requests the preview also supplies the `confirm` value. Human approval still comes from the MCP client's tool-permission prompts (for example Claude Code asking before running `update_device`); keep those enabled for the write tools.

## Tools

For each of `site`, `prefix`, `ip_address`, `vlan`, `vrf`, `device`, `interface`, `device_type`, `manufacturer`, `device_role`, `tenant`, `service`, `cable`, `tag`, `location`:
`list_<plural>`, `get_<name>`, `create_<name>`, `update_<name>` (PATCH), `delete_<name>` (marked destructive).

Plus `netbox_status` and `netbox_search`. `list_*` accepts `filters` (NetBox filter names), `limit`, `offset`, and `brief` (default true).

Create, update and delete change live NetBox data as described under Dry run. Errors include the NetBox response body; the token is never logged.

## Dry run

`create_*`, `update_*` and `delete_*` accept `dry_run` (bool, default unset) and `confirm` (string, default unset). NetBox has no native dry-run, so previews are done client-side and only ever issue GETs. `NETBOX_DRY_RUN` is read on every call and selects one of two modes; every preview reports it as `"mode"`.

| Mode | Selected by | Call without `confirm` | Call with `confirm=<preview_id>` | `dry_run=true` |
|------|-------------|------------------------|----------------------------------|----------------|
| `confirm` (default) | `NETBOX_DRY_RUN` unset, empty, or anything except `0`/`false`/`no`/`off` | Always a preview, even with `dry_run=false` | Real write, only if it matches an unexpired, unused preview of the identical call | Preview only; `confirm` is ignored |
| `direct` | `NETBOX_DRY_RUN` is `0`/`false`/`no`/`off` (case-insensitive, whitespace stripped) | Real write immediately | `confirm` is ignored (real write) | Preview (still returns `preview_id`, nothing requires it) |

Confirm mode: every preview records a fingerprint of (action, endpoint, id, canonical JSON of the requested `data`) and returns `"preview_id"` (short hash), `"expires_in_s"` (600) and a `"next_step"` hint. The apply call must repeat the same arguments plus `confirm="<preview_id>"`. It is refused, with no mutating request sent, if there is no matching or expired preview, if the data, id, endpoint or action differ, or if `confirm` does not equal that preview's id. A preview is single-use and is consumed even if NetBox then returns an error, so a retry needs a fresh preview. Previews live in server memory (lost on restart). There is no bypass argument. For updates the fingerprint covers the requested data, not the diff, so apply with the same `data`.

Direct mode has no gate: use it for bulk seeding where the two-step flow is noise. A real write response is the plain NetBox response (no `mode` field). To switch, export `NETBOX_DRY_RUN=0` in your shell before launching Claude Code (the example `.mcp.json` passes it through, defaulting to `1`, i.e. confirm mode).

- create: returns the request that would be sent (`method`, `path`, `body`) without any network call.
- update: GETs the object and returns `changes` (`from`/`to`) and `unchanged`. Nested objects compare by id and choice fields by `value`. For devices, `"touches_primary_ip": true` is added when `primary_ip4`/`primary_ip6` is in the data.
- delete: GETs the object and returns its brief view, an irreversibility warning, and an `impact` dict of dependent-object counts from read-only `limit=1` GETs (`unknown` where a count cannot be obtained; the vlan interfaces count instead fetches brief ids with `limit=1000` and counts distinct ids, because NetBox can return duplicate rows when joining tagged VLANs): site (devices, racks, prefixes, vlans), location (devices, racks, child_locations), vlan (prefixes, interfaces), prefix (child_prefixes, ip_addresses), vrf (prefixes, ip_addresses), device (interfaces, ip_addresses), interface (ip_addresses, cable 0/1), ip_address (`devices_using_as_primary_ip`, found by following the assigned interface to its device). The per-type map is the `IMPACT` table in `src/netbox_mcp/server.py`.

```text
# confirm mode (default)
update_device(id=7, data={"site": 1, "status": "offline"})
-> {"dry_run": true, "action": "update", "id": 7, "current_display": "router-1",
    "changes": {"status": {"from": "active", "to": "offline"}}, "unchanged": ["site"],
    "mode": "confirm", "preview_id": "3f9a1c0b7d2e", "expires_in_s": 600,
    "next_step": "To apply, call the same tool again with the identical arguments plus confirm=\"3f9a1c0b7d2e\"."}
update_device(id=7, data={"site": 1, "status": "offline"}, confirm="3f9a1c0b7d2e")   # applies, consumes the preview

# direct mode (export NETBOX_DRY_RUN=0 before launching Claude Code)
update_device(id=7, data={"status": "offline"})                  # PATCH sent immediately
update_device(id=7, data={"status": "offline"}, dry_run=true)    # preview only
```
