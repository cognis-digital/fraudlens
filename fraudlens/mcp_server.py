"""FRAUDLENS MCP server — exposes backtest() as an MCP tool for Cognis.Studio."""
from __future__ import annotations

from fraudlens.core import backtest, build_ruleset, load_transactions, to_json


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
    def fraudlens_scan(csv_path: str) -> str:
        """Replay a labeled transaction CSV against the default fraud ruleset.

        Returns JSON with precision/recall metrics, per-rule alert counts,
        and caught/missed/false-alarm transaction IDs.
        """
        try:
            txns = load_transactions(csv_path)
        except (FileNotFoundError, ValueError) as exc:
            return f"error: {exc}"
        return to_json(backtest(txns, build_ruleset()))

    app.run()
    return 0
