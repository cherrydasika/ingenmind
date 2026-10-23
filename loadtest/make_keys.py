"""Make load-test accounts and API keys on a throwaway install (R61): N users
(ask) and one admin (ask, ingest, evaluate). Never on a real install.

    docker compose exec webapp python /loadtest/make_keys.py 50 > keys.json
"""

import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/dags")

import api_keys  # noqa: E402
import identity  # noqa: E402


def main(count: int) -> dict:
    import uuid
    run = uuid.uuid4().hex[:6]
    users = [api_keys.create(identity.create_user(f"Load{i}", f"load{i}-{run}@loadtest.invalid", "user"),
                             "loadtest", ["ask"])[0] for i in range(count)]
    admin = identity.create_user("LoadAdmin", f"loadadmin-{run}@loadtest.invalid", "admin")
    return {"users": users, "admin": api_keys.create(admin, "loadtest", ["ask", "ingest", "evaluate", "admin"])[0]}


if __name__ == "__main__":
    print(json.dumps(main(int(sys.argv[1]) if len(sys.argv) > 1 else 50)))
