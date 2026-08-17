#!/usr/bin/env python3
"""Test if Docling is actually using the GPU."""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import torch
from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, PdfBackend
from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice

def test_gpu_usage():
    """Test GPU usage with Docling."""
    print("=" * 60)
    print("Testing Docling GPU Usage")
    print("=" * 60)

    # Check PyTorch GPU
    print(f"\nPyTorch CUDA available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    # Configure with CUDA
    accelerator_options = AcceleratorOptions(
        num_threads=4,
        device=AcceleratorDevice.CUDA,
    )

    pipeline_options = PdfPipelineOptions(
        backend=PdfBackend.DLPARSE_V2,
        accelerator_options=accelerator_options,
    )

    print(f"\nConfigured accelerator device: {accelerator_options.device}")
    print(f"Pipeline accelerator options: {pipeline_options.accelerator_options}")

    # Create converter
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: pipeline_options,
        }
    )

    print(f"\nConverter created successfully")
    print("\nTo verify GPU usage:")
    print("1. Run this script")
    print("2. In another terminal, run: watch -n 1 nvidia-smi")
    print("3. You should see GPU utilization when processing a PDF")

    # Find a test PDF
    test_pdfs = list(Path("data/cases").glob("*/documents/*.pdf"))
    if not test_pdfs:
        print("\nNo test PDFs found in data/cases/*/documents/")
        return

    test_pdf = test_pdfs[0]
    print(f"\nProcessing test PDF: {test_pdf.name}")
    print("Watch nvidia-smi for GPU usage...")

    # Process the PDF
    result = converter.convert(str(test_pdf))

    print(f"\nProcessing complete!")
    print(f"Pages: {len(result.document.pages)}")

if __name__ == "__main__":
    test_gpu_usage()
