from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys

from orchestrator.control_plane.config import load_control_plane_config
from orchestrator.common.security import hash_password
from orchestrator.deployment.inventory import prepare_deployment


def main() -> None:
    parser = argparse.ArgumentParser(description="WinSW Recovery Control Plane (LAB_HTTP MVP)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--config", help="control-plane.json absolute path")
    mode.add_argument(
        "--generate-secrets",
        action="store_true",
        help="Interactively generate the admin hash, session secret, and cluster token",
    )
    mode.add_argument(
        "--prepare-deployment",
        metavar="INVENTORY",
        help="Validate a non-secret deployment inventory and render fail-closed drafts",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting the Control Plane",
    )
    parser.add_argument(
        "--output-dir",
        help="New output directory required by --prepare-deployment",
    )
    args = parser.parse_args()

    if args.prepare_deployment:
        if args.check_config or not args.output_dir:
            parser.error(
                "--prepare-deployment requires --output-dir and cannot use --check-config"
            )
        try:
            rendered = prepare_deployment(
                args.prepare_deployment,
                args.output_dir,
            )
        except Exception:
            print(
                json.dumps(
                    {
                        "component": "recovery-deployment-preparer",
                        "inventory_valid": False,
                        "error": "deployment inventory validation failed",
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
                    "component": "recovery-deployment-preparer",
                    "inventory_valid": True,
                    "config_ready": False,
                    "agent_count": rendered.agent_count,
                    "service_count": rendered.service_count,
                    "recovery_group_count": rendered.recovery_group_count,
                    "manifest_sha256": rendered.manifest_sha256,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return

    if args.generate_secrets:
        if args.check_config or args.output_dir:
            parser.error("--generate-secrets cannot use --check-config or --output-dir")
        password = getpass.getpass("Administrator password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if not password or password != confirmation:
            print("error: password is empty or confirmation does not match", file=sys.stderr)
            raise SystemExit(2)
        print(
            json.dumps(
                {
                    "admin_password_hash": hash_password(password),
                    "session_secret": secrets.token_urlsafe(48),
                    "cluster_token": secrets.token_urlsafe(48),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return

    if not args.config:
        parser.error("--config is required unless --generate-secrets is used")
    if args.output_dir:
        parser.error("--output-dir is only valid with --prepare-deployment")

    if args.check_config:
        try:
            load_control_plane_config(args.config)
        except Exception:
            print(
                json.dumps(
                    {
                        "component": "control-plane",
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
                    "component": "control-plane",
                    "config_valid": True,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return

    try:
        config = load_control_plane_config(args.config)
    except Exception:
        print(
            json.dumps(
                {
                    "component": "control-plane",
                    "config_valid": False,
                    "error": "configuration validation failed",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    # Deployment preparation and configuration checks are deliberately offline.
    # Import the web stack only when the Control Plane will actually start.
    import uvicorn

    from orchestrator.control_plane.app import create_app

    uvicorn.run(
        create_app(config),
        host=config.listen_host,
        port=config.listen_port,
        access_log=True,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
