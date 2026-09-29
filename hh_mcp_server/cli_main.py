import argparse
import asyncio
import logging
import sys


def configure_utf8_output() -> None:
    """Keep CLI and stdio output Unicode-safe on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main() -> None:
    configure_utf8_output()
    parser = argparse.ArgumentParser(description="HH.ru MCP Server")
    parser.add_argument("--login", action="store_true", help="Launch browser for authentication")
    parser.add_argument("--no-headless", action="store_true", help="Run browser with visible window")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--transport", default="stdio", choices=["stdio"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s: %(message)s")

    if args.login:
        from hh_mcp_server.utils.auth import run_login_flow
        asyncio.run(run_login_flow())
        return

    if args.no_headless:
        from hh_mcp_server.drivers.browser import set_headless
        set_headless(False)

    from hh_mcp_server.server import create_mcp_server
    mcp = create_mcp_server()

    mcp.run(transport="stdio", show_banner=False)
