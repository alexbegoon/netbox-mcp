# netbox-mcp

A small [MCP](https://modelcontextprotocol.io/) server that lets an AI client like [Claude Code](https://claude.com/claude-code) read and edit your [NetBox](https://netboxlabs.com/docs/netbox/) instance through its [REST API](https://netboxlabs.com/docs/netbox/integrations/rest-api/). It talks stdio and is built on the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) ([FastMCP](https://gofastmcp.com/getting-started/quickstart)).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
export NETBOX_URL=https://netbox.example.com
export NETBOX_TOKEN=your-api-token   # NetBox UI -> Admin -> API Tokens
```

That's it. The server is now at `.venv/bin/netbox-mcp` (or run `python -m netbox_mcp`).

A read-only token is fine if you only want to look things up. You need a write-enabled token to create, update or delete.

## Use it with [Claude Code](https://claude.com/claude-code)

Copy `.mcp.json.example` into your project as `.mcp.json` and point `command` at the script:

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

Export `NETBOX_URL` and `NETBOX_TOKEN` before starting [Claude Code](https://claude.com/claude-code), then run `claude mcp list` to check it's connected.

## Configuration

| Variable | Default | What it does |
|----------|---------|--------------|
| `NETBOX_URL` | required | Base URL of your [NetBox](https://netboxlabs.com/docs/netbox/), starting with `http://` or `https://` |
| `NETBOX_TOKEN` | required | API token. A `Bearer ...` or `Token ...` prefix is used as is; a bare `nbt_...` token is sent as `Bearer`, anything else as `Token` |
| `NETBOX_VERIFY_TLS` | `true` | Set to `false` to skip TLS verification |
| `NETBOX_DRY_RUN` | confirm mode | Set to `0`, `false`, `no` or `off` to write directly without the preview step |

## What it can do

Every object type below gets `list_`, `get_`, `create_`, `update_` (PATCH) and `delete_` tools. For example, `list_devices` or `update_device`.

| Area | Object types |
|------|--------------|
| DCIM | `site`, `location`, `region`, `site_group`, `rack`, `rack_role`, `device`, `device_type`, `device_role`, `manufacturer`, `platform`, `interface`, `front_port`, `rear_port`, `console_port`, `console_server_port`, `power_port`, `power_outlet`, `power_panel`, `power_feed`, `inventory_item`, `cable` |
| IPAM | `prefix`, `ip_address`, `vlan`, `vlan_group`, `vrf`, `route_target`, `aggregate`, `rir`, `ipam_role`, `asn`, `fhrp_group`, `service` |
| Circuits | `provider`, `circuit_type`, `circuit`, `circuit_termination` |
| Virtualization | `cluster_type`, `cluster_group`, `cluster`, `virtual_machine`, `vm_interface` |
| Tenancy | `tenant`, `tenant_group`, `contact`, `contact_role`, `contact_group` |
| Extras | `tag`, `journal_entry` |

There are also helpers for handing out addresses from a prefix (`list_available_ips`, `create_available_ip`, `list_available_prefixes`, `create_available_prefix`), plus `netbox_status` and `netbox_search`.

`list_*` tools take `filters` (any [NetBox](https://netboxlabs.com/docs/netbox/) filter name), `limit`, `offset` and `brief` (on by default).

## Safe writes

By default, writes are a two-step process. The first call to `create_*`, `update_*` or `delete_*` only shows a preview of what would change, along with a `preview_id`. To apply it, call the tool again with the same arguments plus `confirm=<preview_id>`. Previews expire after 10 minutes and can be used once.

```text
update_device(id=7, data={"status": "offline"})
-> changes: status active -> offline, preview_id: "3f9a1c0b7d2e"

update_device(id=7, data={"status": "offline"}, confirm="3f9a1c0b7d2e")   # applies it
```

This is a sanity check, not a security boundary, since the same caller asks for the preview and confirms it. Keep your [MCP](https://modelcontextprotocol.io/) client's tool-permission prompts turned on for the write tools.

For bulk work where the extra step is just noise, set `NETBOX_DRY_RUN=0` and writes go straight through. You can still pass `dry_run=true` to any call to preview it.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q   # offline, HTTP is mocked
```

## Links

- [NetBox docs](https://netboxlabs.com/docs/netbox/)
- [NetBox on GitHub](https://github.com/netbox-community/netbox)
- [Model Context Protocol](https://modelcontextprotocol.io/)
