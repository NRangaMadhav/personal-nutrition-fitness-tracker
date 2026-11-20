"""Initialize isolated local databases and generated secrets without printing them."""

import secrets
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from dotenv import dotenv_values, set_key
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


ROOT = Path(__file__).resolve().parents[1]


def main():
    env_file = ROOT / ".env"
    if not env_file.exists():
        raise SystemExit("Copy .env.example to .env and set your local DATABASE_URL first.")
    values = dotenv_values(env_file)
    for key in ("JWT_SECRET_KEY", "OBJECT_STORAGE_ACCESS_KEY", "OBJECT_STORAGE_SECRET_KEY"):
        if not values.get(key):
            set_key(env_file, key, secrets.token_urlsafe(36))
    for key in ("DATABASE_URL", "TEST_DATABASE_URL"):
        url = values.get(key)
        if not url:
            if key == "TEST_DATABASE_URL":
                continue
            raise SystemExit("DATABASE_URL must name a dedicated project database.")
        name = urlsplit(url).path.lstrip("/")
        if not name or name in ("postgres", "template0", "template1"):
            raise SystemExit(f"{key} must name a dedicated project database, not a system database.")
        if key == "TEST_DATABASE_URL" and not name.endswith("_test"):
            raise SystemExit("The test database name must end in _test.")
        params = conninfo_to_dict(url)
        params["dbname"] = "postgres"
        with psycopg.connect(**params, autocommit=True) as connection:
            exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            if not exists:
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                print(f"Created dedicated database: {name}")
            else:
                print(f"Dedicated database already exists: {name}")
    print("Local configuration initialized. Secret values were not printed.")


if __name__ == "__main__":
    main()
