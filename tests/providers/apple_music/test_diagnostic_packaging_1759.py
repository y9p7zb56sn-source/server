"""Offline #1759 diagnostic image packaging contract and negative tests."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy/ma1759-diagnostic"
STAGE = runpy.run_path(str(DEPLOY / "stage.py"))
MODULE_BLOBS = STAGE["MODULE_BLOBS"]
REQUIRED_BASE = STAGE["REQUIRED_BASE_IMAGE_ID"]
SourceAdmissionError = STAGE["SourceAdmissionError"]


def test_stage_exact_reviewed_five_modules_without_extras(tmp_path: Path) -> None:
    """Stage only the five reviewed runtime sources into a new context."""
    output = tmp_path / "source-only-context"
    manifest = STAGE["stage"](REPO, output)
    assert manifest["reviewed_source_commit"] == STAGE["REVIEWED_SOURCE_COMMIT"]
    assert manifest["required_base_image_id"] == REQUIRED_BASE
    assert set(manifest["modules"]) == set(MODULE_BLOBS)
    assert {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()} == set(
        MODULE_BLOBS
    ) | {"Dockerfile", "verify_image.py", "manifest.json"}
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest

    for relative, expected_git_blob in MODULE_BLOBS.items():
        data = (output / relative).read_bytes()
        assert STAGE["_git_blob_sha"](data) == expected_git_blob
        assert manifest["modules"][relative]["sha256"] == hashlib.sha256(data).hexdigest()

    for name, expected_git_blob in STAGE["DEPLOY_BLOBS"].items():
        assert STAGE["_git_blob_sha"]((output / name).read_bytes()) == expected_git_blob

    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    for relative in MODULE_BLOBS:
        assert f"COPY {relative} /app/venv/lib/python3.14/site-packages/{relative}" in (dockerfile)
    assert "FROM ${MA_BASE_IMAGE}" in dockerfile


def _copy_source_closure(destination: Path) -> Path:
    for relative in MODULE_BLOBS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    deploy_target = destination / "deploy/ma1759-diagnostic"
    deploy_target.mkdir(parents=True)
    for name in STAGE["DEPLOY_FILES"]:
        (deploy_target / name).write_bytes((DEPLOY / name).read_bytes())
    return destination


def test_staging_rejects_changed_provider_without_creating_context(tmp_path: Path) -> None:
    """Reject a modified Apple provider before creating staging output."""
    source = _copy_source_closure(tmp_path / "fork")
    provider = source / "music_assistant/providers/apple_music/provider.py"
    provider.write_bytes(provider.read_bytes() + b"\n# unexpected source edit\n")
    context = tmp_path / "context"
    with pytest.raises(SourceAdmissionError, match="MA1759_SOURCE_BLOB_MISMATCH"):
        STAGE["stage"](source, context)
    assert not context.exists()


@pytest.mark.parametrize("name", ["Dockerfile", "verify_image.py"])
def test_staging_rejects_tampered_deploy_file(tmp_path: Path, name: str) -> None:
    """Reject changes to either independently reviewed build input."""
    source = _copy_source_closure(tmp_path / "fork")
    deploy_file = source / "deploy/ma1759-diagnostic" / name
    deploy_file.write_bytes(deploy_file.read_bytes() + b"\n# unreviewed edit\n")
    context = tmp_path / "context"
    with pytest.raises(SourceAdmissionError, match="MA1759_DEPLOY_BLOB_MISMATCH"):
        STAGE["stage"](source, context)
    assert not context.exists()


def test_staging_rejects_symlinked_source(tmp_path: Path) -> None:
    """Reject a source symlink even when its referenced bytes match."""
    source = _copy_source_closure(tmp_path / "fork")
    module = source / "music_assistant/controllers/music/controller.py"
    original = module.read_bytes()
    module.unlink()
    independent = tmp_path / "external-controller.py"
    independent.write_bytes(original)
    module.symlink_to(independent)
    with pytest.raises(SourceAdmissionError, match="MA1759_SOURCE_NOT_REGULAR"):
        STAGE["stage"](source, tmp_path / "context")
    assert not (tmp_path / "context").exists()


def test_staging_refuses_existing_target(tmp_path: Path) -> None:
    """Preserve preexisting target content without overwriting it."""
    context = tmp_path / "context"
    context.mkdir()
    (context / "keep").write_text("preserve", encoding="utf-8")
    with pytest.raises(SourceAdmissionError, match="MA1759_DESTINATION_NOT_NEW_ABSOLUTE"):
        STAGE["stage"](REPO, context)
    assert (context / "keep").read_text(encoding="utf-8") == "preserve"


def test_image_verifier_is_manifest_bound_and_offline() -> None:
    """Require a complete fixed manifest without data or network access."""
    verifier = (DEPLOY / "verify_image.py").read_text(encoding="utf-8")
    for relative in MODULE_BLOBS:
        assert relative in verifier
    assert REQUIRED_BASE in verifier
    assert "hashlib.sha256" in verifier
    assert "ast.parse" in verifier
    assert "/data" not in verifier
    assert "subprocess" not in verifier
    assert "requests" not in verifier
