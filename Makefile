.PHONY: help install test test-cov clean format lint ensure-uv ensure-venv \
        sql-proxy-setup auth run-proxy ensure-proxy-bin ensure-auth test-pdf-parser \
        install-vllm download-nuextract run-vllm check-vllm check-gpu

# The default shell for make
SHELL := /bin/bash

PROXY_VERSION := v2.15.2
PROXY_BASE_URL := https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/$(PROXY_VERSION)

PYTHON := uv run python -u

# Where the uv installer drops the `uv` binary (prepended to PATH in recipes)
LOCAL_BIN := $(HOME)/.local/bin

# Cloud SQL instance the proxy fronts, the project billed for the proxy, and the
# local port it listens on (the app connects to 127.0.0.1:$(DB_PORT)).
INSTANCE ?= data-382711:us-central1:hidden-danger
QUOTA_PROJECT ?= data-382711
DB_PORT ?= 5432

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

help:
	@echo "Available commands:"
	@echo "  make install           - Install project dependencies"
	@echo "  make test              - Run tests"
	@echo "  make test-cov          - Run tests with coverage"
	@echo "  make format            - Format code with ruff"
	@echo "  make lint              - Lint code with ruff"
	@echo "  make clean             - Remove build artifacts and cache files"
	@echo "  make notebook          - Start Jupyter notebook server"
	@echo "  make case-browser      - Start Case Browser Streamlit app"
	@echo "  make test-pdf-parser   - Test PDF parser on sample document"
	@echo ""
	@echo "Database commands:"
	@echo "  make sql-proxy-setup   - Download Cloud SQL proxy binary"
	@echo "  make auth              - Authenticate with Google Cloud"
	@echo "  make run-proxy         - Start Cloud SQL Auth Proxy (connects to both databases)"
	@echo "                           - hidden-danger: localhost:5432"
	@echo "                           - scrapping: localhost:5433"
	@echo ""
	@echo "Extraction Pipeline commands:"
	@echo "  make install-vllm      - Install vLLM in the virtual environment"
	@echo "  make download-nuextract- Download NuExtract3 model from HuggingFace"
	@echo "  make run-vllm          - Start vLLM server with NuExtract3 (port 8000)"
	@echo "  make check-vllm        - Check if vLLM server is running"
	@echo "  make check-gpu         - Check if GPU/CUDA is available for parsing"

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

ensure-uv:
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "uv not found. Installing..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		export PATH="$(LOCAL_BIN):$$PATH"; \
	fi

ensure-venv: ensure-uv
	@if [ ! -d .venv ]; then \
		echo "Creating virtual environment..."; \
		uv venv; \
	fi

install: ensure-venv
	@echo "Installing dependencies..."
	uv pip install -e ".[test]"
	@echo "Installation complete."

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: ensure-venv
	@echo "Running tests..."
	$(PYTHON) -m pytest

test-cov: ensure-venv
	@echo "Running tests with coverage..."
	$(PYTHON) -m pytest --cov=lawsuit_parser --cov-report=html --cov-report=term

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

format: ensure-venv
	@echo "Formatting code with ruff..."
	uv run ruff format .

lint: ensure-venv
	@echo "Linting code with ruff..."
	uv run ruff check .

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

notebook: ensure-venv
	@echo "Starting Jupyter notebook..."
	$(PYTHON) -m jupyter notebook

case-browser: ensure-venv
	@echo "Starting Case Browser app..."
	@echo "Make sure the Cloud SQL Proxy is running: make run-proxy"
	@echo "Opening app at http://localhost:8501"
	uv run streamlit run apps/case_browser.py

test-pdf-parser: ensure-venv
	@echo "Testing PDF parser on sample document..."
	@if [ -f "data/cases/case_104/documents/document__PLUS__PLUS_IWEE20u6dQBAdaY1AO8w==.pdf" ]; then \
		$(PYTHON) scripts/parse_single_pdf.py \
			"data/cases/case_104/documents/document__PLUS__PLUS_IWEE20u6dQBAdaY1AO8w==.pdf" \
			--print-content; \
	else \
		echo "Error: Sample PDF not found in data/cases/case_104/"; \
		echo "Please ensure you have exported case 104 first."; \
		exit 1; \
	fi

# ---------------------------------------------------------------------------
# Database / Cloud SQL Proxy
# ---------------------------------------------------------------------------

sql-proxy-setup:
	@OS=$$(uname -s | tr '[:upper:]' '[:lower:]'); \
	ARCH=$$(uname -m); \
	if [ "$$ARCH" = "x86_64" ]; then ARCH="amd64"; fi; \
	if [ "$$ARCH" = "aarch64" ]; then ARCH="arm64"; fi; \
	if [ "$$OS" = "darwin" ]; then \
		URL="$(PROXY_BASE_URL)/cloud-sql-proxy.$$OS.$$ARCH"; \
	else \
		URL="$(PROXY_BASE_URL)/cloud-sql-proxy.$$OS.$$ARCH"; \
	fi; \
	echo "Downloading cloud-sql-proxy for $$OS/$$ARCH..."; \
	curl -o cloud-sql-proxy "$$URL"; \
	chmod +x cloud-sql-proxy; \
	echo "cloud-sql-proxy installed successfully"

auth:
	gcloud auth application-default login

run-proxy: ensure-proxy-bin ensure-auth
	./cloud-sql-proxy "data-382711:us-central1:hidden-danger?port=5432" "data-382711:us-central1:scrapping?port=5433" --quota-project data-382711

# Download the Cloud SQL proxy binary only if it isn't already in the repo root.
ensure-proxy-bin:
	@if [ -x ./cloud-sql-proxy ]; then \
		echo "cloud-sql-proxy already present."; \
	else \
		$(MAKE) sql-proxy-setup; \
	fi

# Ensure application-default credentials exist; log in only if they don't.
ensure-auth:
	@if gcloud auth application-default print-access-token >/dev/null 2>&1; then \
		echo "Application-default credentials present."; \
	else \
		echo "No application-default credentials found -- launching login..."; \
		gcloud auth application-default login; \
	fi

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean:
	@echo "Cleaning build artifacts and cache files..."
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	@echo "Clean complete."

# ---------------------------------------------------------------------------
# Extraction Pipeline / vLLM
# ---------------------------------------------------------------------------

install-vllm: ensure-venv
	@echo "Installing vLLM..."
	uv pip install vllm
	@echo "vLLM installed successfully."

download-nuextract: ensure-venv
	@echo "Downloading NuExtract3 model from HuggingFace..."
	@echo "This may take several minutes depending on your connection..."
	$(PYTHON) -c "from huggingface_hub import snapshot_download; snapshot_download('numind/NuExtract3', resume_download=True)"
	@echo "NuExtract3 model downloaded successfully."

run-vllm: ensure-venv
	@echo "Starting vLLM server with NuExtract3..."
	@echo "Server will be available at http://localhost:8000"
	@echo "Press Ctrl+C to stop the server"
	@echo ""
	uv run vllm serve numind/NuExtract3 \
		--trust-remote-code \
		--chat-template-content-format openai \
		--max-model-len 32768 \
		--port 8000

check-vllm:
	@echo "Checking vLLM server status..."
	@if curl -s http://localhost:8000/health >/dev/null 2>&1; then \
		echo "✓ vLLM server is running on port 8000"; \
	else \
		echo "✗ vLLM server is not running"; \
		echo "  Start it with: make run-vllm"; \
		exit 1; \
	fi

check-gpu: ensure-venv
	@echo "Checking GPU/CUDA availability for PDF parsing..."
	@$(PYTHON) scripts/check_gpu.py
