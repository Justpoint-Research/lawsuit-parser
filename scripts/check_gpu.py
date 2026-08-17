#!/usr/bin/env python3
"""Check if CUDA/GPU is available and working for Docling."""

import sys

def check_pytorch():
    """Check PyTorch CUDA availability."""
    try:
        import torch
        print(f"PyTorch version: {torch.__version__}")
        print(f"CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"CUDA version: {torch.version.cuda}")
            print(f"GPU device count: {torch.cuda.device_count()}")
            for i in range(torch.cuda.device_count()):
                print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")
                print(f"    Memory: {torch.cuda.get_device_properties(i).total_memory / 1e9:.2f} GB")
        return torch.cuda.is_available()
    except ImportError:
        print("PyTorch not installed")
        return False

def check_docling():
    """Check Docling configuration."""
    try:
        from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
        print("\nDocling accelerator options:")
        print(f"  Available devices: {[d.value for d in AcceleratorDevice]}")

        # Test creating options with CUDA
        opts = AcceleratorOptions(device=AcceleratorDevice.CUDA)
        print(f"  Test CUDA config: {opts}")
        return True
    except ImportError:
        print("Docling not installed")
        return False

def main():
    print("=" * 60)
    print("GPU/CUDA Configuration Check")
    print("=" * 60)

    pytorch_ok = check_pytorch()
    docling_ok = check_docling()

    print("\n" + "=" * 60)
    if pytorch_ok and docling_ok:
        print("✓ GPU is available and configured correctly!")
        print("\nTo use GPU with parsing:")
        print("  uv run python scripts/parse_all_pdfs.py")
        print("\nTo force CPU only:")
        print("  uv run python scripts/parse_all_pdfs.py --no-gpu")
        sys.exit(0)
    else:
        print("✗ GPU is not available or not configured")
        print("\nParsing will run on CPU (slower)")
        sys.exit(1)

if __name__ == "__main__":
    main()
