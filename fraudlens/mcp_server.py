"""FRAUDLENS MCP server — exposes scan() as an MCP tool for Cognis.Studio."""
from __future__ import annotations
from fraudlens.core import scan, to_json

def serve() -> int:
    """Start an MCP stdio server. Requires the optional 'mcp' extra:
        pip install "cognis-fraudlens[mcp]"
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print("Install the MCP extra: pip install 'cognis-fraudlens[mcp]'")
        return 1
    app = FastMCP("fraudlens")

    @app.tool()
    def fraudlens_scan(target: str) -> str:
        """Replays a stream of transactions against pluggable fraud rules and ML scorers, emitting precision/recall and alert volume from the terminal.. Returns JSON findings."""
        return to_json(scan(target))

    app.run()
    return 0
