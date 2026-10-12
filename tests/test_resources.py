import asyncio

import pytest

from netbox_mcp import server

NEW_STEMS = [s for s in server.REGISTRY if s not in {
    "site", "prefix", "ip_address", "vlan", "vrf", "device", "interface", "device_type",
    "manufacturer", "device_role", "tenant", "service", "cable", "tag", "location",
}]


def test_tool_names_unique_and_complete():
    tools = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(tools) == len(set(tools))
    for stem in NEW_STEMS:
        plural = server.REGISTRY[stem][0]
        for n in (f"list_{plural}", f"get_{stem}", f"create_{stem}", f"update_{stem}", f"delete_{stem}"):
            assert n in tools, n
    for n in ("list_available_ips", "create_available_ip", "list_available_prefixes", "create_available_prefix"):
        assert n in tools, n


@pytest.mark.parametrize("stem", ["rack", "region", "platform", "virtual_machine", "circuit", "cluster"])
def test_new_resource_create_preview_then_confirm(nb, stem):
    path = server.REGISTRY[stem][1]
    r = nb.res(nb.call(f"create_{stem}", {"data": {"name": "x"}}))
    assert nb.log == [] and r["path"] == f"/{path}/"
    nb.call(f"create_{stem}", {"data": {"name": "x"}, "confirm": r["preview_id"]})
    assert nb.log == ["POST"]


def test_impact_filters_reference_registered_paths():
    paths = {p for _, p, _ in server.REGISTRY.values()} | {"dcim/devices", "dcim/racks", "ipam/prefixes", "ipam/vlans", "dcim/interfaces", "ipam/ip-addresses"}
    for stem, rows in server.IMPACT.items():
        assert stem in server.REGISTRY, stem
        for _, path, _ in rows:
            assert path in paths, (stem, path)


def test_create_available_prefix_requires_prefix_length(nb):
    with pytest.raises(Exception, match="prefix_length"):
        nb.call("create_available_prefix", {"prefix_id": 1, "data": {}})


def test_create_available_ip_is_gated(nb):
    r = nb.res(nb.call("create_available_ip", {"prefix_id": 1, "data": {"status": "active"}}))
    assert r["path"] == "/ipam/prefixes/1/available-ips/" and set(nb.log) <= {"GET"}
    nb.log.clear()
    nb.call("create_available_ip", {"prefix_id": 1, "data": {"status": "active"}, "confirm": r["preview_id"]})
    assert nb.log == ["POST"]
