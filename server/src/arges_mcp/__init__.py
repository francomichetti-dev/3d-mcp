"""Drive Autodesk Fusion from an MCP client.

Two halves that have to agree with each other: this MCP server, and the
FusionBridge add-in that runs inside Fusion. `arges-mcp install` puts the
add-in where Fusion looks for it, since a package alone cannot reach into
another application.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
