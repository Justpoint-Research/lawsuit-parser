"""
Local Case Browser - Browse exported legal cases from local data folder.

This Streamlit app allows you to:
- Browse cases exported to data/cases directory
- View all case information from JSON files
- See all documents associated with a case
- Preview PDF documents from local storage

Usage:
    streamlit run apps/case_browser_local.py
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import pandas as pd
import json
import base64
from urllib.parse import quote

# Page config
st.set_page_config(
    page_title="Local Case Browser",
    page_icon="📂",
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
    .status-disposed {
        color: #6c757d;
        font-weight: bold;
    }
    .status-active {
        color: #28a745;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)


def get_data_directory():
    """Get the data directory path."""
    return Path(__file__).resolve().parent.parent / "data" / "cases"


@st.cache_data
def load_available_cases():
    """Load list of available cases from data directory."""
    data_dir = get_data_directory()

    if not data_dir.exists():
        return []

    cases = []
    for case_dir in sorted(data_dir.iterdir()):
        if case_dir.is_dir() and case_dir.name.startswith("case_"):
            case_id = int(case_dir.name.replace("case_", ""))
            json_file = case_dir / f"case_{case_id}.json"
            if json_file.exists():
                cases.append({
                    'id': case_id,
                    'directory': case_dir,
                    'json_file': json_file
                })

    return sorted(cases, key=lambda x: x['id'])


@st.cache_data
def load_case_data(case_id: int):
    """Load case data from JSON file."""
    cases = load_available_cases()
    case = next((c for c in cases if c['id'] == case_id), None)

    if not case:
        return None

    try:
        with open(case['json_file'], 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Add directory path for PDF loading
        data['_case_directory'] = case['directory']
        return data
    except Exception as e:
        st.error(f"Error loading case {case_id}: {e}")
        return None


def get_next_case_id(current_id: int, cases: list):
    """Get the next case ID."""
    current_idx = next((i for i, c in enumerate(cases) if c['id'] == current_id), None)
    if current_idx is None or current_idx >= len(cases) - 1:
        return current_id
    return cases[current_idx + 1]['id']


def get_prev_case_id(current_id: int, cases: list):
    """Get the previous case ID."""
    current_idx = next((i for i, c in enumerate(cases) if c['id'] == current_id), None)
    if current_idx is None or current_idx <= 0:
        return current_id
    return cases[current_idx - 1]['id']


def display_pdf_from_file(pdf_path: Path, width: int = 700, height: int = 1000):
    """Display PDF from local file using iframe."""
    if not pdf_path.exists():
        st.error(f"PDF file not found: {pdf_path.name}")
        return

    try:
        with open(pdf_path, 'rb') as f:
            pdf_bytes = f.read()

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
    except Exception as e:
        st.error(f"Error loading PDF: {e}")


def display_case_info(case_data):
    """Display case information."""
    st.markdown("<div class='main-header'>📂 Case Information</div>", unsafe_allow_html=True)

    case_info = case_data['case_info']

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Basic Information")
        st.markdown(f"**Case ID:** {case_info.get('case_id', 'N/A')}")
        st.markdown(f"**Docket ID:** {case_info.get('docket_id', 'N/A')}")
        st.markdown(f"**Court:** {case_info.get('court', 'N/A')}")
        st.markdown(f"**Case Type:** {case_info.get('case_type', 'N/A')}")
        st.markdown(f"**Received Date:** {case_info.get('case_received_date', 'N/A')}")

    with col2:
        st.markdown("### Status")
        status = case_info.get('case_status', 'N/A')
        status_class = "status-active" if "active" in str(status).lower() else "status-disposed"
        st.markdown(f"**Status:** <span class='{status_class}'>{status}</span>", unsafe_allow_html=True)
        st.markdown(f"**E-filing Status:** {case_info.get('efiling_status', 'N/A')}")

    st.markdown("### Caption")
    st.info(case_info.get('caption', 'N/A'))


def display_documents(case_data):
    """Display case documents."""
    documents = case_data.get('documents', [])
    case_dir = case_data['_case_directory']

    st.markdown("---")
    st.markdown(f"## 📄 Documents ({len(documents)} total)")

    if len(documents) == 0:
        st.warning("No documents found for this case.")
        return

    # Display summary
    summary = case_data.get('summary', {})
    st.markdown(f"**Exported:** {summary.get('exported_at', 'N/A')}")

    for idx, doc in enumerate(documents, 1):
        with st.expander(f"📄 {doc.get('document_name', 'Untitled')} - Filed: {doc.get('filed_create', 'N/A')}", expanded=False):
            col1, col2 = st.columns([2, 1])

            with col1:
                st.markdown(f"**Filed By:** {doc.get('filed_by', 'N/A')}")
                st.markdown(f"**Filed Date:** {doc.get('filed_create', 'N/A')}")
                st.markdown(f"**Received Date:** {doc.get('filed_received', 'N/A')}")
                st.markdown(f"**Status:** {doc.get('document_status', 'N/A')}")

                if doc.get('document_details'):
                    st.markdown(f"**Details:** {doc['document_details']}")

            with col2:
                # Local file paths
                local_doc_path = doc.get('local_document_path')
                local_conf_path = doc.get('local_confirmation_path')

                if local_doc_path:
                    pdf_path = case_dir / local_doc_path
                    if pdf_path.exists():
                        st.success(f"✓ Document available ({pdf_path.stat().st_size / 1024:.1f} KB)")

                        # Preview checkbox
                        show_preview = st.checkbox(
                            "📖 Preview Document",
                            value=st.session_state.get(f'preview_doc_{idx}', False),
                            key=f'preview_doc_{idx}'
                        )
                    else:
                        st.warning("⚠️ Document file not found")
                        show_preview = False
                else:
                    st.info("No document file")
                    show_preview = False

                if local_conf_path:
                    conf_path = case_dir / local_conf_path
                    if conf_path.exists():
                        st.markdown(f"✓ Confirmation ({conf_path.stat().st_size / 1024:.1f} KB)")

                        show_conf = st.checkbox(
                            "📋 Preview Confirmation",
                            value=st.session_state.get(f'preview_conf_{idx}', False),
                            key=f'preview_conf_{idx}'
                        )
                    else:
                        show_conf = False
                else:
                    show_conf = False

            # Show PDF previews
            if show_preview and local_doc_path:
                pdf_path = case_dir / local_doc_path
                if pdf_path.exists():
                    st.markdown("---")
                    st.markdown("### 📄 Document Preview")
                    display_pdf_from_file(pdf_path)

            if show_conf and local_conf_path:
                conf_path = case_dir / local_conf_path
                if conf_path.exists():
                    st.markdown("---")
                    st.markdown("### 📋 Confirmation Preview")
                    display_pdf_from_file(conf_path)


def main():
    st.markdown("<div class='main-header'>📂 Local Case Browser</div>", unsafe_allow_html=True)
    st.markdown("Browse exported legal cases from local data directory")

    # Load available cases
    cases = load_available_cases()

    if not cases:
        st.error("❌ No cases found in data/cases directory")
        st.info("💡 Export cases first using: `uv run python scripts/export_random_cases.py --count 20`")
        return

    # Sidebar navigation
    with st.sidebar:
        st.header("Navigation")

        st.success(f"✓ {len(cases)} cases loaded")

        st.markdown("---")

        # Case ID input
        if 'current_case_id' not in st.session_state:
            st.session_state.current_case_id = cases[0]['id']

        # Navigation buttons
        col1, col2, col3 = st.columns(3)

        with col1:
            if st.button("⏮️ First"):
                # Clear PDF preview states
                keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
                for key in keys_to_remove:
                    del st.session_state[key]
                st.session_state.current_case_id = cases[0]['id']
                st.rerun()

        with col2:
            if st.button("◀️ Prev"):
                # Clear PDF preview states
                keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
                for key in keys_to_remove:
                    del st.session_state[key]
                st.session_state.current_case_id = get_prev_case_id(st.session_state.current_case_id, cases)
                st.rerun()

        with col3:
            if st.button("Next ▶️"):
                # Clear PDF preview states
                keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
                for key in keys_to_remove:
                    del st.session_state[key]
                st.session_state.current_case_id = get_next_case_id(st.session_state.current_case_id, cases)
                st.rerun()

        # Case selector dropdown
        case_options = {f"Case {c['id']}": c['id'] for c in cases}
        selected_label = st.selectbox(
            "Select Case",
            options=list(case_options.keys()),
            index=list(case_options.values()).index(st.session_state.current_case_id)
        )

        if case_options[selected_label] != st.session_state.current_case_id:
            # Clear PDF preview states
            keys_to_remove = [k for k in st.session_state.keys() if k.startswith('preview_')]
            for key in keys_to_remove:
                del st.session_state[key]
            st.session_state.current_case_id = case_options[selected_label]
            st.rerun()

        st.markdown("---")

        # Data directory info
        data_dir = get_data_directory()
        st.caption(f"📁 Data: {data_dir.relative_to(Path.cwd())}")

        # Quick stats
        total_pdfs = sum(len(list((c['directory'] / 'documents').glob('*.pdf'))) +
                        len(list((c['directory'] / 'confirmations').glob('*.pdf')))
                        for c in cases if (c['directory'] / 'documents').exists())
        st.caption(f"📄 Total PDFs: {total_pdfs}")

    # Main content
    case_data = load_case_data(st.session_state.current_case_id)

    if case_data is None:
        st.error(f"Case with ID {st.session_state.current_case_id} not found or could not be loaded.")
        return

    # Display case information
    display_case_info(case_data)

    # Display documents
    display_documents(case_data)

    # Footer
    st.markdown("---")
    current_idx = next((i for i, c in enumerate(cases) if c['id'] == st.session_state.current_case_id), 0)
    st.caption(f"Viewing Case {st.session_state.current_case_id} ({current_idx + 1}/{len(cases)}) | Local Data Browser")


if __name__ == "__main__":
    main()
