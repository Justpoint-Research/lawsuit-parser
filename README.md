# lawsuit-parser

A tool for parsing and analyzing legal documents, court filings, and lawsuit data.

## Project Structure

```
lawsuit-parser/
├── lawsuit_parser/     # Main Python package
├── apps/              # Streamlit or standalone applications
├── config/            # Configuration files
├── notebooks/         # Jupyter notebooks for analysis
├── scripts/           # Data processing and utility scripts
├── sql/              # SQL queries and schema definitions
├── tests/            # Test suite
├── pyproject.toml    # Project dependencies and configuration
└── Makefile          # Build and development commands
```

## Setup

### Requirements

- Python 3.12+
- `uv` package manager (recommended) or `pip`

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd lawsuit-parser
```

2. Create a virtual environment and install dependencies:
```bash
# Using uv (recommended)
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e .

# Or using pip
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

### Configuration

This project shares the same database configuration as the `hidden-danger` project. The secrets have been copied from `~/.config/hidden-danger/` to `~/.config/lawsuit-parser/`.

If you need to set up from scratch:
```bash
mkdir -p ~/.config/lawsuit-parser
cp config/secrets.toml.example ~/.config/lawsuit-parser/secrets.toml
chmod 600 ~/.config/lawsuit-parser/secrets.toml
# Edit the file with your actual credentials
```

## Database Connection

This project connects to two Cloud SQL PostgreSQL instances:
- **`data-382711:us-central1:hidden-danger`** - Main database (port 5432)
- **`data-382711:us-central1:scrapping`** - Scrapping/lawsuit data (port 5433)

### Connecting to PostgreSQL

The database uses **IAM database authentication** — your Google account (the one used in `gcloud auth application-default login`) is the database user. The user name is **`research`**.

#### Setup and connect via Cloud SQL Proxy

**Each session**: Start the proxy (it will auto-download and authenticate if needed)
```bash
make run-proxy
```

This command will:
1. Download the cloud-sql-proxy binary if not already present
2. Authenticate with Google Cloud if needed
3. Start the proxy connecting to both databases

Once `run-proxy` is running, **both databases** are accessible:
- **hidden-danger**: `localhost:5432`
- **scrapping**: `localhost:5433`

Leave the terminal open while you need the connection; stop it with `Ctrl+C`.

**Manual setup** (optional):
```bash
make sql-proxy-setup  # Download proxy binary
make auth             # Authenticate with Google Cloud
```

#### Using in Python

For password-based connections through the Cloud SQL Proxy at `127.0.0.1:5432`, connection params are split into two files:

- **`config/database.toml`** — committed to git. Holds non-secret params (`host`, `port`, `database`).
- **`~/.config/lawsuit-parser/secrets.toml`** — *not* in git. Holds `user` and `password`.

Then in a notebook or script, you can connect using libraries like `psycopg2`, `sqlalchemy`, or `pandas`:

```python
import tomllib
from pathlib import Path
import pandas as pd
from sqlalchemy import create_engine

# Load secrets
with open(Path.home() / '.config/lawsuit-parser/secrets.toml', 'rb') as f:
    secrets = tomllib.load(f)

# Connect to hidden-danger database (port 5432)
engine = create_engine(
    f"postgresql+psycopg://{secrets['database']['user']}:{secrets['database']['password']}"
    f"@127.0.0.1:5432/postgres"
)

# Or connect to scrapping database (port 5433)
engine_scrapping = create_engine(
    f"postgresql+psycopg://{secrets['database']['user']}:{secrets['database']['password']}"
    f"@127.0.0.1:5433/postgres"
)

# Query
df = pd.read_sql("SELECT * FROM my_table LIMIT 100", engine)
```

**Note**: The default `config/database.toml` points to port 5432 (hidden-danger). To use the scrapping database, connect to port 5433 as shown above.

### Requirements

- `gcloud` CLI installed and on `PATH`
- `curl` (used by `sql-proxy-setup`)
- Network access to `storage.googleapis.com` to download the proxy binary

## Usage

### Case Browser App

A Streamlit web application for browsing legal cases and documents:

**Setup:**
```bash
# 1. Authenticate with Google Cloud (one-time)
gcloud auth application-default login

# 2. Terminal 1: Start the Cloud SQL Proxy
make run-proxy

# 3. Terminal 2: Start the Case Browser
make case-browser
```

The app will open at `http://localhost:8501` and provides:
- Browse cases by ID or using next/prev buttons
- View complete case information
- See all documents for each case
- **Preview PDF documents** directly from GCS bucket (requires authentication)

**GCS Utilities:**
```bash
# Check authentication status
python scripts/list_gcs_buckets.py check-auth

# List all available buckets
python scripts/list_gcs_buckets.py list-buckets

# List files in a bucket
python scripts/list_gcs_buckets.py list-files --bucket BUCKET_NAME

# Search for files
python scripts/list_gcs_buckets.py find-files --bucket BUCKET_NAME --search document
```

### Database Utilities

The package includes utilities for working with PostgreSQL databases. See `lawsuit_parser/utils/db.py` for available functions:

- **`make_engine()`** - Create a SQLAlchemy engine for database connections
- **`fetch_from_postgres(query)`** - Run a query and return results as a DataFrame (with automatic caching)
- **`load_db_config()`** - Load database configuration from config files

Example usage:

```python
from lawsuit_parser.utils import fetch_from_postgres

# Query the database (results are cached)
df = fetch_from_postgres("SELECT * FROM my_table LIMIT 100")

# Force refresh (bypass cache)
df = fetch_from_postgres(query, force_refresh=True)

# Save results to data/ directory
df = fetch_from_postgres(query, output_filename="results")
```

### Case Exporter

Export complete court cases with all documents to denormalized JSON files and download PDFs from GCS:

```bash
# Export a single case
uv run python scripts/export_case.py 1229

# Export 100 random cases
uv run python scripts/export_random_cases.py --count 100

# Export specific cases
uv run python scripts/export_random_cases.py --case-ids "273,51,70"
```

Each exported case includes:
- Complete case metadata from `court_cases` table
- All associated documents from `court_documents` table
- Downloaded PDF files from Google Cloud Storage
- Denormalized JSON file combining all information

See [Case Exporter Usage Guide](docs/case_exporter_usage.md) for detailed documentation.

### Example Scripts

The `scripts/` directory includes example scripts demonstrating package usage:

```bash
# Test database connection
python scripts/example_query.py test-connection

# List tables in the database
python scripts/example_query.py list-tables --limit 10

# Show database schemas
python scripts/example_query.py show-schemas

# Run a custom query
python scripts/example_query.py run-query "SELECT version()"

# Run a query and save results
python scripts/example_query.py run-query "SELECT * FROM my_table" --output results
```

### Example Notebooks

Check out `notebooks/example.ipynb` for a Jupyter notebook demonstrating:
- How to import the package
- Database connection examples
- Running queries with caching
- Working with pandas DataFrames

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=lawsuit_parser
```

### Jupyter Notebooks

```bash
# Start Jupyter
jupyter notebook
# Navigate to the notebooks/ directory
```

## Development

### Running Linters

```bash
# Format code
ruff format .

# Check for issues
ruff check .
```

### Project Commands

See the `Makefile` for available commands:
```bash
make help
```

## License

[Add license information here]
