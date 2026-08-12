"""Database utilities for fetching data from PostgreSQL."""

import hashlib
import os
import tomllib
from pathlib import Path

import pandas as pd
from google.cloud.sql.connector import Connector, IPTypes
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "database.toml"
SECRETS_PATH = Path.home() / ".config" / "lawsuit-parser" / "secrets.toml"

# One process-wide Cloud SQL Connector, created lazily on first use. Reused across
# every pooled connection so we don't spin up a new connector per checkout.
_connector: Connector | None = None


def load_db_config(
    config_path: Path = CONFIG_PATH,
    secrets_path: Path = SECRETS_PATH,
) -> dict:
    """Load merged PostgreSQL connection parameters.

    Non-secret params (host, port, database) come from ``config/database.toml``
    in the repo. Credentials (user, password) come from a gitignored file at
    ``~/.config/lawsuit-parser/secrets.toml``. See ``config/secrets.toml.example``
    for the expected format.
    """
    with config_path.open("rb") as f:
        config = tomllib.load(f).get("postgres", {})

    if not secrets_path.exists():
        raise FileNotFoundError(
            f"Secrets file not found at {secrets_path}. "
            f"Copy {REPO_ROOT / 'config' / 'secrets.toml.example'} to that path "
            f"and fill in real credentials."
        )
    with secrets_path.open("rb") as f:
        secrets_data = tomllib.load(f)
        # Support both 'postgres' and 'database' keys for compatibility
        secrets = secrets_data.get("postgres", secrets_data.get("database", {}))

    return {**config, **secrets}


def _make_connector_engine():
    """Engine that reaches Cloud SQL through the Python Connector (no proxy).

    Used in environments where ``INSTANCE_CONNECTION_NAME`` is set (e.g. Cloud
    Run): the connector authenticates the TLS handshake with the runtime service
    account (needs ``roles/cloudsql.client`` + the Cloud SQL Admin API), while
    the database user/password still come from the environment. ``ip_type``
    defaults to PUBLIC; set ``DB_IP_TYPE=PRIVATE`` for a private-IP instance.
    """
    global _connector
    if _connector is None:
        _connector = Connector()

    instance = os.environ["INSTANCE_CONNECTION_NAME"]
    user = os.environ["DB_USER"]
    password = os.environ["DB_PASS"]
    database = os.environ.get("DB_NAME", "postgres")
    ip_type = (
        IPTypes.PRIVATE
        if os.environ.get("DB_IP_TYPE", "").upper() == "PRIVATE"
        else IPTypes.PUBLIC
    )

    def getconn():
        return _connector.connect(
            instance,
            "pg8000",
            user=user,
            password=password,
            db=database,
            ip_type=ip_type,
        )

    return create_engine("postgresql+pg8000://", creator=getconn, pool_pre_ping=True)


def make_engine():
    """SQLAlchemy engine for the research Postgres.

    Picks a connection strategy from the environment:

    * If ``INSTANCE_CONNECTION_NAME`` is set, connect directly to Cloud SQL via
      the Cloud SQL Python Connector (the deployed / Cloud Run path).
    * Otherwise, connect over ``host:port`` using ``config/database.toml`` + the
      gitignored secrets file via :func:`load_db_config` (the local Cloud SQL
      Auth Proxy path).

    The caller owns the engine and should ``engine.dispose()`` when done.
    """
    if os.environ.get("INSTANCE_CONNECTION_NAME"):
        return _make_connector_engine()

    p = load_db_config()
    url = URL.create(
        drivername="postgresql+psycopg",
        username=p["user"],
        password=p["password"],
        host=p["host"],
        port=int(p["port"]),
        database=p["database"],
    )
    return create_engine(url)


def fetch_from_postgres(
    query: str,
    output_filename: str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Run a query against PostgreSQL using config + secrets files.

    Results are cached to ``data/cache/{sha256(query)}.parquet`` and reused on
    subsequent calls with the same query. Pass ``force_refresh=True`` to bypass
    the cache and re-run the query.

    Args:
        query: SQL query to execute.
        output_filename: If set, also save the result to ``data/{output_filename}.parquet``.
        force_refresh: If True, ignore any cached result and re-query the database.

    Returns:
        DataFrame with the query results.
    """
    cache_dir = REPO_ROOT / "data" / "cache"
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{query_hash}.parquet"

    if not force_refresh and cache_path.exists():
        df = pd.read_parquet(cache_path)
    else:
        params = load_db_config()
        url = URL.create(
            drivername="postgresql+psycopg",
            username=params["user"],
            password=params["password"],
            host=params["host"],
            port=int(params["port"]),
            database=params["database"],
        )
        engine = create_engine(url)
        try:
            df = pd.read_sql(query, engine)
        finally:
            engine.dispose()

        cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_path, index=False)

    if output_filename:
        data_dir = REPO_ROOT / "data"
        data_dir.mkdir(exist_ok=True)
        df.to_parquet(data_dir / f"{output_filename}.parquet", index=False)

    return df
