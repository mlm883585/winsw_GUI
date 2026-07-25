from __future__ import annotations

import getpass
import secrets

from orchestrator.common.security import hash_password


def main() -> None:
    password = getpass.getpass("管理员密码: ")
    confirmation = getpass.getpass("再次输入: ")
    if not password or password != confirmation:
        raise SystemExit("密码为空或两次输入不一致")
    print("admin_password_hash:", hash_password(password))
    print("session_secret:", secrets.token_urlsafe(48))
    print("cluster_token:", secrets.token_urlsafe(48))


if __name__ == "__main__":
    main()

