#!/usr/bin/env python3
"""List available Docling layout models and their details."""

import docling.datamodel.pipeline_options as opts

models = {
    "EGRET_LARGE": opts.DOCLING_LAYOUT_EGRET_LARGE,
    "EGRET_MEDIUM": opts.DOCLING_LAYOUT_EGRET_MEDIUM,
    "EGRET_XLARGE": opts.DOCLING_LAYOUT_EGRET_XLARGE,
    "HERON": opts.DOCLING_LAYOUT_HERON,
    "HERON_101": opts.DOCLING_LAYOUT_HERON_101,
    "V2": opts.DOCLING_LAYOUT_V2,
}

print("Available Docling Layout Models:")
print("=" * 70)

for name, model in models.items():
    print(f"\n{name}:")
    print(f"  Model Name: {model.name}")
    print(f"  HuggingFace Repo: {model.repo_id}")
    if hasattr(model, 'revision'):
        print(f"  Revision: {model.revision}")
    if hasattr(model, 'engine_overrides'):
        print(f"  Engine Overrides: {model.engine_overrides}")

print("\n" + "=" * 70)
print("\nNOTE: Models using RT-DETR-v2 architecture require transformers>=5.0")
print("      EGRET models typically use older architectures compatible with transformers 4.x")
