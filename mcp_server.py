"""MCP-сервер (stdio) для Claude Desktop / Cursor / VS Code: python mcp_server.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validator.mcp_tools import build_server  # noqa: E402

if __name__ == "__main__":
    build_server(remote=False).run("stdio")
