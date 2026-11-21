"""
Verify staged module bytes during the isolated #1759 image build.

Do not access persistent application data, credentials or network.
"""

# ruff: noqa: INP001 - standalone offline Docker build verifier

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path("/app/venv/lib/python3.14/site-packages")
MANIFEST = Path("/opt/ma1759-diagnostic/manifest.json")
REQUIRED_MODULES = frozenset(
    {
        "music_assistant/controllers/music/controller.py",
        "music_assistant/controllers/music/media/tracks.py",
        "music_assistant/providers/apple_music/provider.py",
        "music_assistant/providers/apple_music/recommendations.py",
        "music_assistant/providers/apple_music/readonly_diagnostic_1759.py",
    }
)
REQUIRED_SOURCE = "4b2332d0a16f4c10aacf406170c2d5a09dc17dac"
REQUIRED_IMAGE = "sha256:e681b6aeedb25e639860b8c1e687578377277c2930908e3fa4cc7605501335ed"


def main() -> None:
    """Reject stale or mismatched module bytes and Python source syntax."""
    metadata = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (
        metadata.get("schema") != "ma1759.diagnostic_build_context.v1"
        or metadata.get("reviewed_source_commit") != REQUIRED_SOURCE
        or metadata.get("required_base_image_id") != REQUIRED_IMAGE
        or set(metadata.get("modules", {})) != REQUIRED_MODULES
    ):
        raise SystemExit("MA1759_IMAGE_MODULE_MANIFEST=INVALID")

    for relative in sorted(REQUIRED_MODULES):
        source = ROOT / relative
        expected = metadata["modules"][relative]["sha256"]
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit("MA1759_IMAGE_MODULE_DIGEST=FAIL")
        ast.parse(data, filename=relative)

    sys.stdout.write("MA1759_IMAGE_FIVE_MODULE_DIGESTS=PASS\n")
    sys.stdout.write("MA1759_IMAGE_IMPORT_AND_RUNTIME=NOT_ATTESTED\n")
    sys.stdout.write("MA1759_APPLE_AUTH=NOT_ACCESSED\n")


if __name__ == "__main__":
    main()
