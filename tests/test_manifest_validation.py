import shutil

import pytest

from configs.config import PR_BUNDLES_DIR
from manifest_validation import (
    ManifestValidationError,
    REQUIRED_SUPPORTING_FILES,
    validate_bundle,
    validate_bundle_or_raise,
)

GOOD_BUNDLE = PR_BUNDLES_DIR / "pr_bundle_001"


def test_valid_bundle_passes():
    result = validate_bundle(GOOD_BUNDLE)
    assert result.is_valid
    assert result.errors == []
    assert result.manifest.bundle_id == "pr_bundle_001"
    assert set(REQUIRED_SUPPORTING_FILES).issubset(result.manifest.supporting_files)


def test_missing_manifest_is_blocked(tmp_path):
    result = validate_bundle(tmp_path)  # empty dir, no manifest.yaml
    assert not result.is_valid
    assert any("manifest.yaml not found" in e for e in result.errors)
    # negative path must not crash and must leave paths empty
    assert result.requisition_path is None
    assert result.supporting_paths == {}


def test_resolved_paths_for_valid_bundle():
    result = validate_bundle(GOOD_BUNDLE)
    assert result.requisition_path.is_absolute()
    assert result.requisition_path.exists()
    assert set(result.supporting_paths) == set(REQUIRED_SUPPORTING_FILES)
    for key in REQUIRED_SUPPORTING_FILES:
        p = result.supporting_paths[key]
        assert p.is_absolute() and p.exists(), f"{key} -> {p}"


def test_missing_supporting_file_reported(tmp_path):
    # Copy the good bundle, then delete one supporting file.
    dst = tmp_path / "bundle"
    shutil.copytree(GOOD_BUNDLE, dst)
    (dst / "budget_snapshot.csv").unlink()
    result = validate_bundle(dst)
    assert not result.is_valid
    assert any("budget_snapshot.csv" in e and "missing" in e for e in result.errors)


def test_malformed_yaml_reported(tmp_path):
    dst = tmp_path / "bundle"
    dst.mkdir()
    (dst / "manifest.yaml").write_text("bundle_id: x\n  bad: : indent\n", encoding="utf-8")
    result = validate_bundle(dst)
    assert not result.is_valid
    assert any("not valid YAML" in e for e in result.errors)


def test_missing_required_key_reported(tmp_path):
    dst = tmp_path / "bundle"
    dst.mkdir()
    (dst / "requisition_form.pdf").write_text("x", encoding="utf-8")
    (dst / "manifest.yaml").write_text(
        "bundle_id: x\ninput_type: pdf\nrequisition_file: requisition_form.pdf\n"
        "supporting_files: {}\n",
        encoding="utf-8",
    )
    result = validate_bundle(dst)
    assert not result.is_valid
    assert any("missing required key 'budget_snapshot'" in e for e in result.errors)


def test_validate_or_raise(tmp_path):
    with pytest.raises(ManifestValidationError) as exc:
        validate_bundle_or_raise(tmp_path)
    assert "Invalid PR bundle" in str(exc.value)
    # and the happy path returns the parsed manifest
    manifest = validate_bundle_or_raise(GOOD_BUNDLE)
    assert manifest.input_type == "pdf"