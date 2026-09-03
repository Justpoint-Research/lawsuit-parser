"""
Case Browser - Browse legal cases and preview documents from the scrapping database.

This Streamlit app allows you to:
- Browse cases by ID or using next/prev navigation
- View all case information
- See all documents associated with a case
- Preview PDF documents from GCS bucket

Usage:
    streamlit run apps/case_browser.py
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
import tomllib
from datetime import datetime
import base64
import html
from urllib.parse import quote
import logging
import traceback

from lawsuit_parser.utils import try_download_from_buckets, get_storage_client

# Postgres schema/table-prefix the crawl tables live under (see
# lawsuit_parser.utils.case_exporter.CaseExporter for the same convention).
# Configurable from the sidebar so this browser can point at another
# state's tables (e.g. "fl_") without a code change.
DEFAULT_SCHEMA = "courts_final"
DEFAULT_TABLE_PREFIX = "ny_"

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/case_browser.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Page config
st.set_page_config(
    page_title="Legal Case Browser",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
        margin-bottom: 1rem;
    }
    .case-info {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        margin-bottom: 1rem;
    }
    .doc-card {
        background-color: #ffffff;
        border: 1px solid #ddd;
        padding: 1rem;
        border-radius: 0.5rem;
        margin-bottom: 0.5rem;
    }
    .status-active {
        color: #28a745;
        font-weight: bold;
    }
    .status-disposed {
        color: #6c757d;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)


def get_db_credentials():
    """Get database credentials from secrets file (cached)."""
    if 'db_credentials' not in st.session_state:
        try:
            logger.info("Loading database credentials")
            secrets_path = Path.home() / ".config" / "lawsuit-parser" / "secrets.toml"
            if not secrets_path.exists():
                logger.error(f"Secrets file not found at {secrets_path}")
                st.error(f"❌ Secrets file not found at {secrets_path}")
                st.info("Please create the secrets file with your database credentials.")
                st.stop()

            with open(secrets_path, "rb") as f:
                secrets = tomllib.load(f)

            secrets_data = secrets.get("postgres", secrets.get("database", {}))
            user = secrets_data.get("user")
            password = secrets_data.get("password")

            if not user or not password:
                logger.error("Database credentials not found in secrets file")
                st.error("❌ Database credentials not found in secrets file")
                st.stop()

            st.session_state.db_credentials = (user, password)
            logger.info("Database credentials loaded successfully")
        except Exception as e:
            logger.error(f"Error loading database configuration: {e}\n{traceback.format_exc()}")
            st.error(f"❌ Error loading database configuration: {e}")
            st.stop()

    return st.session_state.db_credentials


def get_database_engine():
    """Create database engine for scrapping database (not cached to avoid segfault)."""
    try:
        logger.debug("Creating database engine")
        user, password = get_db_credentials()
        engine = create_engine(
            f"postgresql+psycopg://{user}:{password}@127.0.0.1:5433/postgres",
            poolclass=NullPool  # Use NullPool to avoid connection pooling issues with cached engine
        )
        logger.debug("Database engine created successfully")
        return engine
    except Exception as e:
        logger.error(f"Error creating database engine: {e}\n{traceback.format_exc()}")
        raise


@st.cache_resource
def get_gcs_client_cached():
    """Get Google Cloud Storage client with authentication (cached)."""
    try:
        return get_storage_client(), None
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=300)
def get_case_count(schema: str = DEFAULT_SCHEMA, table_prefix: str = DEFAULT_TABLE_PREFIX):
    """Get total number of cases."""
    try:
        logger.info("Getting case count")
        engine = get_database_engine()
        query = text(f"SELECT COUNT(*) FROM {schema}.{table_prefix}cases_after_search")
        with engine.connect() as conn:
            count = conn.execute(query).scalar()
        logger.info(f"Total cases: {count}")
        return count
    except Exception as e:
        logger.error(f"Database connection error: {e}\n{traceback.format_exc()}")
        st.error(f"❌ Database connection error: {e}")
        st.info("💡 Make sure Cloud SQL Proxy is running: `make run-proxy`")
        st.code(traceback.format_exc())
        return 1  # Return 1 to prevent crashes


@st.cache_data(ttl=60)
def get_case_by_id(case_id: int, schema: str = DEFAULT_SCHEMA, table_prefix: str = DEFAULT_TABLE_PREFIX):
    """Get case information by ID."""
    try:
        logger.info(f"Fetching case ID: {case_id}")
        engine = get_database_engine()
        query = text(f"SELECT * FROM {schema}.{table_prefix}cases_after_search WHERE id = :id")
        with engine.connect() as conn:
            logger.debug(f"Executing query for case {case_id}")
            result = conn.execute(query, {"id": case_id})
            row = result.fetchone()
            if row:
                logger.debug(f"Case {case_id} found")
                # Convert row to pandas Series for compatibility with existing code
                return pd.Series(dict(row._mapping))
            logger.warning(f"Case {case_id} not found")
            return None
    except Exception as e:
        logger.error(f"Error fetching case {case_id}: {e}\n{traceback.format_exc()}")
        st.error(f"Error fetching case: {e}")
        st.code(traceback.format_exc())
        return None


@st.cache_data(ttl=60)
def get_case_documents(docket_id: str, schema: str = DEFAULT_SCHEMA, table_prefix: str = DEFAULT_TABLE_PREFIX):
    """Get all documents for a case."""
    try:
        logger.info(f"Fetching documents for docket_id: {docket_id}")
        engine = get_database_engine()
        query = text(f"""
            SELECT
                id,
                document_doc_index,
                document_name,
                document_details,
                document_link,
                document_bucket_link,
                filed_by,
                filed_create,
                filed_received,
                document_status,
                document_confirmation_link,
                document_confirmation_bucket_link
            FROM {schema}.{table_prefix}docket_documents
            WHERE docket_id = :docket_id
            ORDER BY filed_create, id
        """)
        with engine.connect() as conn:
            logger.debug(f"Executing documents query for {docket_id}")
            result = conn.execute(query, {"docket_id": docket_id})
            rows = result.fetchall()
            # Convert to DataFrame for compatibility with existing code
            if rows:
                logger.debug(f"Found {len(rows)} documents for {docket_id}")
                data = [dict(row._mapping) for row in rows]
                return pd.DataFrame(data)
            logger.debug(f"No documents found for {docket_id}")
            return pd.DataFrame()
    except Exception as e:
        logger.error(f"Error fetching documents: {e}\n{traceback.format_exc()}")
        st.error(f"Error fetching documents: {e}")
        st.code(traceback.format_exc())
        return pd.DataFrame()


def get_next_case_id(current_id: int, schema: str = DEFAULT_SCHEMA, table_prefix: str = DEFAULT_TABLE_PREFIX):
    """Get the next case ID."""
    try:
        logger.info(f"Getting next case after ID: {current_id}")
        engine = get_database_engine()
        query = text(f"SELECT MIN(id) FROM {schema}.{table_prefix}cases_after_search WHERE id > :current_id")
        logger.debug("Executing next case query")
        with engine.connect() as conn:
            next_id = conn.execute(query, {"current_id": current_id}).scalar()
        logger.info(f"Next case ID: {next_id}")
        return next_id if next_id else current_id
    except Exception as e:
        logger.error(f"Error getting next case: {e}\n{traceback.format_exc()}")
        st.error(f"Error getting next case: {e}")
        st.code(traceback.format_exc())
        return current_id


def get_prev_case_id(current_id: int, schema: str = DEFAULT_SCHEMA, table_prefix: str = DEFAULT_TABLE_PREFIX):
    """Get the previous case ID."""
    try:
        logger.info(f"Getting previous case before ID: {current_id}")
        engine = get_database_engine()
        query = text(f"SELECT MAX(id) FROM {schema}.{table_prefix}cases_after_search WHERE id < :current_id")
        logger.debug("Executing previous case query")
        with engine.connect() as conn:
            prev_id = conn.execute(query, {"current_id": current_id}).scalar()
        logger.info(f"Previous case ID: {prev_id}")
        return prev_id if prev_id else current_id
    except Exception as e:
        logger.error(f"Error getting previous case: {e}\n{traceback.format_exc()}")
        st.error(f"Error getting previous case: {e}")
        st.code(traceback.format_exc())
        return current_id


def clean_url(url: str) -> str:
    """Clean URLs from database by decoding HTML entities and custom encodings.

    The database stores URLs with:
    - HTML entities like &amp; (needs decoding to &)
    - Custom encoding like _PLUS_ (needs decoding to +)
    - Unencoded spaces (need proper URL encoding)

    Args:
        url: URL string from database

    Returns:
        Cleaned URL ready for use in links
    """
    if not url or pd.isna(url):
        return url

    # Decode HTML entities (&amp; -> &)
    cleaned = html.unescape(url)

    # Decode custom encoding patterns (_PLUS_ -> +)
    cleaned = cleaned.replace('_PLUS_', '+')

    # URL-encode spaces and other special characters
    # Keep URL structure characters safe: :/?#[]@!$&'()*+,;=
    cleaned = quote(cleaned, safe=":/?#[]@!$&'()*+,;=")

    return cleaned


def display_pdf(pdf_bytes: bytes, width: int = 700, height: int = 1000):
    """Display PDF in Streamlit using iframe.

    Args:
        pdf_bytes: PDF file as bytes
        width: Width of the display area
        height: Height of the display area
    """
    base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
    pdf_display = f'''
        <iframe
            src="data:application/pdf;base64,{base64_pdf}"
            width="{width}"
            height="{height}"
            type="application/pdf"
            style="border: 1px solid #ccc; border-radius: 4px;"
        >
        </iframe>
    '''
    st.markdown(pdf_display, unsafe_allow_html=True)


def display_case_info(case):
    """Display case information."""
    st.markdown("<div class='main-header'>⚖️ Case Information</div>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Basic Information")
        st.markdown(f"**Case ID:** {case['case_id']}")
        st.markdown(f"**Docket ID:** {case['docket_id']}")
        st.markdown(f"**Court:** {case['court']}")
        st.markdown(f"**Case Type:** {case['case_type']}")
        st.markdown(f"**Received Date:** {case['case_received_date']}")

    with col2:
        st.markdown("### Status")
        status_class = "status-active" if "active" in str(case['case_status']).lower() else "status-disposed"
        st.markdown(f"**Status:** <span class='{status_class}'>{case['case_status']}</span>", unsafe_allow_html=True)
        st.markdown(f"**E-filing Status:** {case['efiling_status']}")
        if case['documents_scrapped_at'] and not pd.isna(case['documents_scrapped_at']):
            st.markdown(f"**Documents Scraped:** {case['documents_scrapped_at']}")

    st.markdown("### Caption")
    st.info(case['caption'])

    if case['case_link'] and not pd.isna(case['case_link']):
        # Clean URL (decode HTML entities and custom encodings)
        clean_link = clean_url(case['case_link'])
        st.markdown(f"[🔗 View on Court Website]({clean_link})")


def display_documents(documents_df):
    """Display case documents."""
    st.markdown("---")
    st.markdown(f"## 📄 Documents ({len(documents_df)} total)")

    if len(documents_df) == 0:
        st.warning("No documents found for this case.")
        return

    # Group by document type for better organization
    for idx, doc in documents_df.iterrows():
        with st.expander(f"📄 {doc['document_name'] or 'Untitled Document'} - Filed: {doc['filed_create']}", expanded=False):
            col1, col2 = st.columns([2, 1])

            with col1:
                st.markdown(f"**Filed By:** {doc['filed_by']}")
                st.markdown(f"**Filed Date:** {doc['filed_create']}")
                st.markdown(f"**Received Date:** {doc['filed_received']}")
                st.markdown(f"**Status:** {doc['document_status']}")

                if doc['document_details'] and not pd.isna(doc['document_details']):
                    st.markdown(f"**Details:** {doc['document_details']}")

            with col2:
                # Document links
                if doc['document_link'] and not pd.isna(doc['document_link']):
                    # Clean URL (decode HTML entities and custom encodings)
                    clean_link = clean_url(doc['document_link'])
                    st.markdown(f"[🔗 View Online]({clean_link})")

                if doc['document_bucket_link'] and not pd.isna(doc['document_bucket_link']):
                    st.markdown(f"**GCS Path:** `{doc['document_bucket_link']}`")

                    # PDF preview toggle - using checkbox for better UX
                    show_preview = st.checkbox(
                        "📖 Preview PDF",
                        value=st.session_state.get(f'preview_{doc["id"]}', False),
                        key=f'preview_{doc["id"]}'
                    )

                if doc['document_confirmation_link'] and not pd.isna(doc['document_confirmation_link']):
                    # Clean URL (decode HTML entities and custom encodings)
                    clean_conf_link = clean_url(doc['document_confirmation_link'])
                    st.markdown(f"[📋 Confirmation]({clean_conf_link})")

            # Show PDF preview if checkbox is checked
            if st.session_state.get(f'preview_{doc["id"]}', False):
                st.markdown("---")
                st.markdown("### 📄 PDF Preview")

                gcs_path = doc['document_bucket_link']

                # Try bucket names - documents are in gs://court-docs/document_link/
                possible_buckets = [
                    'court-docs',  # Actual bucket where documents are stored
                    'nyscef-documents',
                    'nyscef-files',
                    'court-documents',
                    'scrapping-documents',
                ]

                # Get GCS client
                gcs_client, gcs_error = get_gcs_client_cached()

                if gcs_error:
                    st.error(f"GCS authentication error: {gcs_error}")
                else:
                    with st.spinner("Loading PDF from GCS..."):
                        pdf_bytes, bucket_name, error = try_download_from_buckets(
                            gcs_path, possible_buckets, gcs_client
                        )

                        if pdf_bytes:
                            try:
                                display_pdf(pdf_bytes)
                                st.success(f"✓ Loaded from gs://{bucket_name}/{gcs_path}")
                            except Exception as e:
                                st.error(f"Error displaying PDF: {str(e)}")
                        else:
                            st.error(f"Could not load PDF: {error}")
                            st.info("💡 **Troubleshooting:**")
                            st.markdown(f"""
                            1. Make sure you're authenticated: `gcloud auth application-default login`
                            2. Verify you have read access to the bucket
                            3. The document path is: `{gcs_path}`
                            4. Tried buckets: {', '.join(possible_buckets)}
                            """)

                            # Show download button as fallback
                            st.markdown("**Alternative:** Try viewing the document online:")
                            if doc['document_link'] and not pd.isna(doc['document_link']):
                                # Clean URL (decode HTML entities and custom encodings)
                                clean_link = clean_url(doc['document_link'])
                                st.markdown(f"[🔗 Open in Browser]({clean_link})")

                # User can uncheck the checkbox above to close the preview


def main():
    st.markdown("<div class='main-header'>⚖️ Legal Case Browser</div>", unsafe_allow_html=True)
    st.markdown("Browse legal cases and documents from the NY Supreme Court")

    # Sidebar navigation
    with st.sidebar:
        st.header("Navigation")

        # Check GCS authentication status
        gcs_client, gcs_error = get_gcs_client_cached()
        if gcs_client:
            st.success("✓ GCS Authenticated")
        else:
            st.error("✗ GCS Not Authenticated")
            with st.expander("ℹ️ How to authenticate"):
                st.markdown("""
                Run this command in your terminal:
                ```bash
                gcloud auth application-default login
                ```

                Then refresh this page.
                """)

        st.markdown("---")

        # Schema / table-prefix the crawl tables live under (e.g.
        # courts_final.ny_cases_after_search) - configurable so this
        # browser can point at another state's tables (e.g. "fl_").
        schema = st.text_input("Schema", value=DEFAULT_SCHEMA, key="schema")
        table_prefix = st.text_input("Table prefix", value=DEFAULT_TABLE_PREFIX, key="table_prefix")

        st.markdown("---")

        # Get total cases
        total_cases = get_case_count(schema, table_prefix)
        st.metric("Total Cases", f"{total_cases:,}")

        # Case ID input
        if 'current_case_id' not in st.session_state:
            st.session_state.current_case_id = 1

        # Navigation buttons
        col1, col2, col3 = st.columns(3)

        with col1:
            if st.button("⏮️ First"):
                logger.info("First button clicked")
                # Clear PDF preview states and cached data
                keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
                for key in keys_to_remove:
                    del st.session_state[key]
                get_case_by_id.clear()
                get_case_documents.clear()
                st.session_state.current_case_id = 1
                logger.info("Navigating to first case (ID: 1)")
                st.rerun()

        with col2:
            if st.button("◀️ Prev"):
                logger.info(f"Prev button clicked, current case: {st.session_state.current_case_id}")
                # Clear PDF preview states and cached data
                keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
                for key in keys_to_remove:
                    del st.session_state[key]
                get_case_by_id.clear()
                get_case_documents.clear()
                prev_id = get_prev_case_id(st.session_state.current_case_id, schema, table_prefix)
                st.session_state.current_case_id = prev_id
                logger.info(f"Navigating to previous case (ID: {prev_id})")
                st.rerun()

        with col3:
            if st.button("Next ▶️"):
                logger.info(f"Next button clicked, current case: {st.session_state.current_case_id}")
                # Clear PDF preview states and cached data
                keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
                for key in keys_to_remove:
                    del st.session_state[key]
                get_case_by_id.clear()
                get_case_documents.clear()
                next_id = get_next_case_id(st.session_state.current_case_id, schema, table_prefix)
                st.session_state.current_case_id = next_id
                logger.info(f"Navigating to next case (ID: {next_id})")
                st.rerun()

        # Case ID input - place after navigation buttons to avoid conflicts
        case_id_input = st.number_input(
            "Enter Case ID",
            min_value=1,
            max_value=total_cases,
            value=st.session_state.current_case_id,
            step=1,
            key="case_id_input"
        )

        # Go button
        if st.button("🔍 Go to Case", type="primary"):
            st.session_state.current_case_id = case_id_input
            st.rerun()

        st.markdown("---")
        st.caption("💡 Tip: Use the number input to jump to a specific case, or use the navigation buttons to browse.")

    # Main content
    case = get_case_by_id(st.session_state.current_case_id, schema, table_prefix)

    if case is None:
        st.error(f"Case with ID {st.session_state.current_case_id} not found.")
        return

    # Display case information
    display_case_info(case)

    # Get and display documents
    documents_df = get_case_documents(case['docket_id'], schema, table_prefix)
    display_documents(documents_df)

    # Footer
    st.markdown("---")
    st.caption(
        f"Viewing Case ID: {st.session_state.current_case_id} | "
        f"Database: scrapping (localhost:5433) | Table: {schema}.{table_prefix}cases_after_search"
    )

    # Debug panel
    with st.expander("🐛 Debug Logs", expanded=False):
        st.markdown("**Recent log entries:**")
        try:
            with open('/tmp/case_browser.log', 'r') as f:
                lines = f.readlines()
                recent_logs = ''.join(lines[-50:])  # Last 50 lines
                st.code(recent_logs, language='log')
        except Exception as e:
            st.error(f"Could not read log file: {e}")

        if st.button("🗑️ Clear Logs"):
            try:
                with open('/tmp/case_browser.log', 'w') as f:
                    f.write('')
                st.success("Logs cleared")
                st.rerun()
            except Exception as e:
                st.error(f"Could not clear logs: {e}")


if __name__ == "__main__":
    main()
