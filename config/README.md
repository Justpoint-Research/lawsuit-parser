# Config

This directory contains configuration files for the project.

## Files

- **`database.toml`** - Non-secret database connection parameters (host, port, database)
  - Safe to commit to git
  - Used for connecting to PostgreSQL through Cloud SQL Proxy

- **`secrets.toml.example`** - Example secrets file template
  - Copy to `~/.config/lawsuit-parser/secrets.toml` and fill in actual credentials
  - **Note**: This project shares the same database as `hidden-danger`, so secrets have already been copied from `~/.config/hidden-danger/`

## Usage

Configuration files should not contain sensitive information. Store secrets in:
```
~/.config/lawsuit-parser/secrets.toml
```

The configuration is split into two files:
- **Public config** (`config/database.toml`) - committed to git
- **Secrets** (`~/.config/lawsuit-parser/secrets.toml`) - NOT in git

The `.gitignore` is configured to exclude `secrets.toml` but include `secrets.toml.example`.

## Database Connection

Both `database.toml` and `secrets.toml` are used together to connect to the Cloud SQL PostgreSQL instance:
- `database.toml`: host, port, database name
- `secrets.toml`: username and password
