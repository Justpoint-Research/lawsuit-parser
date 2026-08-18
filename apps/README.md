# Apps

This directory contains standalone applications (e.g., Streamlit apps, CLI tools, web interfaces).

## Available Apps

### 🗄️ Database Case Browser (`case_browser.py`)

A Streamlit web application for browsing legal cases from the NY Supreme Court scrapping database in real-time.

### 📂 Local Case Browser (`case_browser_local.py`) ⭐ Recommended

A lightweight Streamlit app for browsing exported cases from local `data/cases` directory. **No database or GCS authentication required!**

### 📅 Event Browser (`event_browser.py`)

A Streamlit app for browsing extracted event-candidate segments (the output of `scripts/extract.py` stages 0-4), with tag-style filtering by actor.

---

## Database Case Browser

Browse cases directly from the PostgreSQL database with real-time GCS PDF downloads.

**Features:**
- Browse cases by ID with next/prev navigation
- View all case information (caption, court, status, dates, etc.)
- List all documents associated with a case
- **Preview PDF documents** directly in the browser from GCS bucket
- Authentication status indicator
- Expandable document cards with full details

**Setup:**

1. **Authenticate with Google Cloud** (one-time):
```bash
gcloud auth application-default login
```

2. **Start the Cloud SQL Proxy**:
```bash
make run-proxy
```

3. **Start the Case Browser**:
```bash
# In another terminal
make case-browser

# Or run directly
streamlit run apps/case_browser.py
```

The app will open at `http://localhost:8501`

**PDF Preview:**

The app automatically downloads and displays PDFs from GCS when you click the "📖 Preview PDF" button. It tries common bucket names automatically.

**Troubleshooting:**

If PDFs don't load:
- Check authentication: `uv run python scripts/list_gcs_buckets.py check-auth`
- Verify bucket access: `uv run python scripts/list_gcs_buckets.py list-files --bucket BUCKET_NAME`
- Ensure you have read permissions on the bucket
- The app tries these buckets: `nyscef-documents`, `nyscef-files`, `court-documents`, `scrapping-documents`

**Database Connection:**
- Connects to the `scrapping` database on port 5433
- Shows data from `court_cases` and `court_documents` tables
- Displays GCS bucket links for PDF documents

---

## Local Case Browser

Browse exported cases from the local filesystem - **no database or GCS authentication required!**

**Features:**
- ✅ Works offline - no database or internet connection needed
- ✅ Fast performance - all data is local
- ✅ Preview PDFs directly from local storage
- ✅ Navigate between cases with Next/Prev buttons
- ✅ Dropdown selector to jump to any case
- ✅ Shows file sizes and case statistics

**Setup:**

1. **Export cases first** (one-time):
```bash
# Export 20 random cases
uv run python scripts/export_random_cases.py --count 20

# Or export specific cases
uv run python scripts/export_random_cases.py --case-ids "273,51,70"
```

2. **Start the Local Browser**:
```bash
streamlit run apps/case_browser_local.py
```

The app will open at `http://localhost:8501`

**Data Location:**
- Reads from `data/cases/` directory
- Each case has its own subdirectory with JSON and PDFs
- No external dependencies required

**Advantages:**
- 🚀 **Fast** - No network latency, instant loading
- 💻 **Portable** - Works on any machine with the data folder
- 🔒 **Offline** - No database or internet required
- 📊 **Shareable** - Export and share specific cases easily

**See:** [Local Case Browser Documentation](../docs/local_case_browser.md) for more details.

---

## Event Browser

The interactive counterpart to `scripts/extract.py`'s output and `scripts/browse_events.py` - same input (the `data/cases/<case_id>/stages/*.json` artifacts from extraction stages 0-4), but as a filterable app instead of a terminal dump.

**Features:**
- Case-level summary: caption, court, plaintiff(s)/defendant(s), document count, time span, entity-span breakdown
- Event table (timestamp / actors / summary / document / page / source excerpt) for every stage-4 priority segment
- **Tag-style actor filter** - pick one or more actors from a multiselect and the event list narrows to segments mentioning them (ANY by default, with an optional ALL/AND toggle when 2+ actors are selected)
- Per-event expander with the full source text

**Setup:**

Requires a case that's already been run through the extraction pipeline:
```bash
uv run scripts/extract.py --case-id case_2132 --stages 0-4
```

Then:
```bash
make event-browser

# Or run directly
streamlit run apps/event_browser.py
```

**Note:** the event table renders as plain monospace text (`st.code`), not `st.dataframe`/`st.table`. In some environments those Arrow-backed widgets segfault the Streamlit server process on rerun (a `pyarrow`/`libarrow.so` issue, confirmed against a live server here) - the plain-text table sidesteps that entirely and still gives the same tabular layout as `scripts/browse_events.py`.

---

## Which Browser Should I Use?

| Use Case | Recommended Browser |
|----------|-------------------|
| **Development & Testing** | 📂 Local Browser |
| **Offline Analysis** | 📂 Local Browser |
| **Quick Case Review** | 📂 Local Browser |
| **Full Dataset Access** | 🗄️ Database Browser |
| **Live Data Exploration** | 🗄️ Database Browser |
| **Production Monitoring** | 🗄️ Database Browser |

**Recommendation:** Start with the **Local Case Browser** for most use cases. It's faster, simpler, and works offline!

## Structure

- Each app should be a self-contained module
- Apps can import from the main `lawsuit_parser` package
- Database connections use the shared utilities from `lawsuit_parser.utils`
