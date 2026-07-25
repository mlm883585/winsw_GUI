from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from orchestrator.agent.app import create_app
from orchestrator.agent.config import load_agent_config
from orchestrator.agent.store import ServiceMappingChangedError


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows Recovery MVP Agent")
    parser.add_argument("--config", required=True, help="Path to agent.json")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting the Agent",
    )
    args = parser.parse_args()

    if args.check_config:
        try:
            config = load_agent_config(args.config)
        except Exception:
            print(
                json.dumps(
                    {
                        "component": "agent",
                        "config_valid": False,
                        "error": "configuration validation failed",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        print(
            json.dumps(
                {
                    "component": "agent",
                    "config_valid": True,
                    "service_count": len(config.services),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return

    try:
        config = load_agent_config(args.config)
        app = create_app(config)
    except ServiceMappingChangedError:
        print(
            json.dumps(
                {
                    "component": "agent",
                    "started": False,
                    "error_code": "SERVICE_MAPPING_CHANGED",
                    "error": "service mapping changed",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    uvicorn.run(
        app,
        host=config.listen_host,
        port=config.listen_port,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
