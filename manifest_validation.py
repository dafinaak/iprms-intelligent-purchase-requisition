"""Manifest validation for IPRMS PR bundles.

Reads a bundle's manifest.yaml and verifies the bundle is complete BEFORE the
pipeline runs, so invalid bundles are blocked at the gate (Agent A) and by the API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, ValidationError

REQUIRED_SUPPORTING_FILES = [
    "budget_snapshot", "approved_vendors", "catalogue_pricing",
    "approval_policy", "cost_center_mapping", "historical_prs",
]
VALID_INPUT_TYPES = {"pdf", "scanned_pdf", "json", "web_form"}


class Manifest(BaseModel):
    bundle_id: str
    input_type: str
    requisition_file: str
    supporting_files: Dict[str, str]


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    manifest: Optional[Manifest] = None
    # Resolved absolute paths, populated only when the manifest parses successfully.
    # (Paths are resolved regardless of physical existence; existence is in `errors`.)
    requisition_path: Optional[Path] = None
    supporting_paths: Dict[str, Path] = field(default_factory=dict)


def validate_bundle(bundle_dir: Path | str) -> ValidationResult:
    bundle = Path(bundle_dir)
    mf = bundle / "manifest.yaml"

    if not mf.exists():
        return ValidationResult(False, [f"manifest.yaml not found in '{bundle}'"])
    try:
        raw = yaml.safe_load(mf.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return ValidationResult(False, [f"manifest.yaml is not valid YAML: {e}"])
    if not isinstance(raw, dict):
        return ValidationResult(False, ["manifest.yaml must be a YAML mapping"])
    try:
        manifest = Manifest(**raw)
    except ValidationError as e:
        return ValidationResult(
            False,
            [f"manifest.yaml field {err['loc']}: {err['msg']}" for err in e.errors()],
        )

    # Resolve absolute paths (manifest parsed OK), so Agent A can build the
    # evidence_index without re-resolving. Existence is checked separately below.
    requisition_path = (bundle / manifest.requisition_file).resolve()
    supporting_paths = {k: (bundle / v).resolve() for k, v in manifest.supporting_files.items()}

    errors: List[str] = []
    if manifest.input_type not in VALID_INPUT_TYPES:
        errors.append(
            f"input_type '{manifest.input_type}' is not one of {sorted(VALID_INPUT_TYPES)}"
        )
    if not (bundle / manifest.requisition_file).exists():
        errors.append(f"requisition_file '{manifest.requisition_file}' is missing")
    for key in REQUIRED_SUPPORTING_FILES:
        if key not in manifest.supporting_files:
            errors.append(f"supporting_files missing required key '{key}'")
            continue
        fname = manifest.supporting_files[key]
        if not (bundle / fname).exists():
            errors.append(f"supporting file '{key}' -> '{fname}' is missing")

    return ValidationResult(not errors, errors, manifest, requisition_path, supporting_paths)


class ManifestValidationError(Exception):
    """Raised when a PR bundle fails manifest validation."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__("Invalid PR bundle:\n- " + "\n- ".join(result.errors))


def validate_bundle_or_raise(bundle_dir: Path | str) -> Manifest:
    result = validate_bundle(bundle_dir)
    if not result.is_valid:
        raise ManifestValidationError(result)
    assert result.manifest is not None
    return result.manifest