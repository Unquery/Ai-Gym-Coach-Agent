"""Prepare an M9 release offline; execute migrations only with an explicit flag."""

import argparse

from app.db import migrate
from app.preflight import validate_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-migrations", action="store_true",
        help="Connect and upgrade the selected database; requires deployment approval",
    )
    args = parser.parse_args(argv)
    try:
        validate_profile("migration")
    except ValueError:
        print("FAIL release_configuration; verify the migration profile and credential separation")
        return 1

    if not args.execute_migrations:
        print("PASS release_plan; offline validation only, no database connection")
        print("NEXT after approval: upgrade, provision or verify runtime grants, deploy the reviewed commit, smoke")
        return 0

    if migrate.main() != 0:
        print("STOP release_migrations; do not deploy or retry automatically; inspect the database revision")
        return 1
    print("PASS release_migrations; verify runtime grants before a separately approved API deployment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
