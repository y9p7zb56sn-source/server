"""
Stage the exact #1759 candidate-only diagnostic module set.

Produce an inert minimal Docker build context from reviewed Git blobs.
Never call Docker, access NAS, credentials or Music Assistant.
"""

# ruff: noqa: INP001 - standalone build script, not a package

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tempfile
from pathlib import Path

REVIEWED_SOURCE_COMMIT = "4b2332d0a16f4c10aacf406170c2d5a09dc17dac"
REQUIRED_BASE_IMAGE_ID = (
    "sha256:e681b6aeedb25e639860b8c1e687578377277c2930908e3fa4cc7605501335ed"
)
MODULE_BLOBS = {
    "music_assistant/controllers/music/controller.py": (
        "f5d13036f27e03223937df1e9b24a852db9d60d9"
    ),
    "music_assistant/controllers/music/media/tracks.py": (
        "53746f3f783f2bc7bce2f635dca7293046eb9aed"
    ),
    "music_assistant/providers/apple_music/provider.py": (
        "e8ab0338b407306e83f948f3c66db7d0c9f696bd"
    ),
    "music_assistant/providers/apple_music/recommendations.py": (
        "4f27b7f0b282376bc08e8c06d0d96b8e25c9e28d"
    ),
    "music_assistant/providers/apple_music/readonly_diagnostic_1759.py": (
        "4623d6876f2d0fc9cd875c174616e5d6c23d6e71"
    ),
}
DEPLOY_FILES = ("Dockerfile", "verify_image.py")


class SourceAdmissionError(ValueError):
    """Only fixed error labels; never print source or private files."""


def _file_bytes(path: Path) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise SourceAdmissionError("MA1759_SOURCE_NOT_REGULAR")
    return path.read_bytes()


def _git_blob_sha(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
    # Git blob object format requires SHA-1; this is not an authentication hash.
    return hashlib.sha1(header + data).hexdigest()


def verify_sources(source_root: Path) -> dict[str, dict[str, str]]:
    """Verify every reviewed runtime blob before writing any staging files."""
    records = {}
    for relative, expected_blob in MODULE_BLOBS.items():
        data = _file_bytes(source_root / relative)
        if _git_blob_sha(data) != expected_blob:
            raise SourceAdmissionError("MA1759_SOURCE_BLOB_MISMATCH")
        records[relative] = {
            "git_blob_sha1": expected_blob,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return records


def stage(source_root: Path, destination: Path) -> dict[str, object]:
    """Create a new, exact-file-set context; never overwrite an existing target."""
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise SourceAdmissionError("MA1759_DESTINATION_NOT_NEW_ABSOLUTE")
    if not destination.parent.is_dir():
        raise SourceAdmissionError("MA1759_DESTINATION_PARENT_MISSING")

    records = verify_sources(source_root)
    deployment = source_root / "deploy/ma1759-diagnostic"
    for name in DEPLOY_FILES:
        _file_bytes(deployment / name)
    manifest = {
        "schema": "ma1759.diagnostic_build_context.v1",
        "reviewed_source_commit": REVIEWED_SOURCE_COMMIT,
        "required_base_image_id": REQUIRED_BASE_IMAGE_ID,
        "modules": records,
    }

    temporary = Path(tempfile.mkdtemp(prefix=".ma1759-stage-", dir=destination.parent))
    try:
        for relative in MODULE_BLOBS:
            out = temporary / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_file_bytes(source_root / relative))
        for name in DEPLOY_FILES:
            (temporary / name).write_bytes(_file_bytes(deployment / name))
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if destination.exists() or destination.is_symlink():
            raise SourceAdmissionError("MA1759_DESTINATION_COLLISION")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def main() -> None:
    """Stage only reviewed files into a new operator-supplied directory."""
    parser = argparse.ArgumentParser(description="Stage source-only #1759 Docker context")
    parser.add_argument("destination", type=Path, help="new absolute context directory")
    args = parser.parse_args()
    source_root = Path(__file__).resolve().parents[2]
    try:
        manifest = stage(source_root, args.destination)
    except (OSError, SourceAdmissionError) as exc:
        parser.exit(2, f"MA1759_SOURCE_STAGING=BLOCKED {type(exc).__name__}\n")
    sys.stdout.write("MA1759_SOURCE_STAGING=PASS\n")
    sys.stdout.write(f"MA1759_MODULES={len(manifest['modules'])}\n")
    sys.stdout.write("MA1759_DOCKER_BUILD=NOT_EXECUTED\n")
    sys.stdout.write("MA1759_RUNTIME_CHANGE=NONE\n")


if __name__ == "__main__":
    main()
