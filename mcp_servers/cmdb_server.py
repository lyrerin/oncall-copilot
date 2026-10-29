"""mcp_servers/cmdb_server.py —— mcp-cmdb :8003。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 848 行起（§5.4 CMDB MCP Server）。
职责：暴露查服务拓扑 / 依赖关系的工具，供根因定位时判断爆炸半径。
"""
import logging

from fastmcp import FastMCP

from mcp_servers import store

log = logging.getLogger("mcp.cmdb")

mcp = FastMCP(
    name="cmdb",
    instructions="提供服务拓扑和变更记录。排查任何故障都应先看 list_changes —— 多数故障由上变更引起。",
)

# mock 拓扑
TOPOLOGY = {
    "api-gateway":   {"upstream": ["web"], "downstream": ["order-service", "user-service"], "replicas": 6},
    "order-service": {"upstream": ["api-gateway"], "downstream": ["order-db", "redis-cache"], "replicas": 8},
    "user-service":  {"upstream": ["api-gateway"], "downstream": ["user-db"], "replicas": 4},
    "order-db":      {"upstream": ["order-service"], "downstream": [], "replicas": 1},
    "redis-cache":   {"upstream": ["order-service", "user-service"], "downstream": [], "replicas": 3},
}

OWNERS = {
    "api-gateway":   {"team": "platform", "oncall": "alice", "channel": "#platform-oncall"},
    "order-service": {"team": "trade", "oncall": "bob", "channel": "#trade-oncall"},
    "user-service":  {"team": "user", "oncall": "carol", "channel": "#user-oncall"},
}

@mcp.tool
async def get_topology(service: str):
    """查询服务的上下游依赖和副本数。用于判断故障影响范围。

    Args:
        service: 服务名
    """
    topo = TOPOLOGY.get(service)
    if not topo:
        return {"service": service, "found": False,'known_services': sorted(TOPOLOGY)}
    
    impacted = [s for s,t in TOPOLOGY.items() if service in t["downstream"]]
    return {"service": service, "found": True,**topo, "impacted_upstream": impacted}

@mcp.tool
async def list_changes(service:str, hours:int = 24):
    """列出服务最近 N 小时的变更（发版、配置修改、扩缩容）。

    Args:
        service: 服务名
        hours: 回溯窗口（小时）
    """
    changes = await store.fetch_recent_changes(service,hours)
    return {
        "service": service, "hours": hours, "count": len(changes),
        "changes": changes,
        # 显式给出结论，帮模型做判断
        "has_recent_deploy": any(c["kind"] == "deploy" for c in changes),
    }


@mcp.tool
async def get_owner(service: str) -> dict:
    """查询服务的负责团队和当前值班人。决定故障该找谁时使用。"""
    return {"service": service, **OWNERS.get(service, {"team": "unknown", "oncall": None})}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8003)