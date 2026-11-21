"""
#1759 candidate-only authenticated Apple first-page shape collector.

Pure caller-facing projection: the authenticated AppleMusicAPIClient.get_data method
is injected from the existing live AppleMusicProvider instance. No tokens, URLs,
IDs, titles, raw responses, file I/O, queue commands, or provider configuration.
Installing or invoking this module is NOT authorised by its source existence.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

PAGE_LIMIT = 1000
HOME_CONTENT_LIMIT = 5000
REQUEST_TIMEOUT_SECONDS = 10.0
PATHS = (
    ("home", "me/recommendations?include[personal-recommendation]=contents", {}),
    ("recently_added", "me/library/recently-added", {}),
    ("recent_tracks", "me/recent/played/tracks", {"types": "songs,library-songs", "limit": 20}),
)
HOME_TYPES = ("albums", "artists", "songs", "playlists", "stations")
ADDED_TYPES = ("library-songs", "library-albums", "library-artists", "library-playlists")
RECENT_TYPES = ("songs", "library-songs")


class AppleFirstPageShapeError(ValueError):
    """Constant error code; never interpolate private response data."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AppleFirstPageShapeError(code)


def _rows(value: object, code: str, limit: int = PAGE_LIMIT) -> list[dict[str, Any]]:
    _require(isinstance(value, list) and len(value) <= limit, code)
    _require(all(isinstance(row, dict) for row in value), code)
    return value


def _cursor(value: dict[str, Any]) -> str:
    """Return only the cursor's type, never its account-bearing URL."""
    if "next" not in value:
        return "absent"
    return "string" if isinstance(value["next"], str) else "other"


def _types(rows: list[dict[str, Any]], allowed: tuple[str, ...]) -> dict[str, int]:
    counts = Counter()
    for row in rows:
        kind = row.get("type")
        counts[kind if isinstance(kind, str) and kind in allowed else "other"] += 1
    return {kind: counts[kind] for kind in (*allowed, "other")}


def _summary(response: object, which: str) -> dict[str, Any]:
    _require(isinstance(response, dict), "APPLE_RESPONSE_SHAPE")
    rows = _rows(response.get("data"), "APPLE_DATA_SHAPE")
    if which != "home":
        return {
            "state": "available" if rows else "empty",
            "item_count": len(rows),
            "types": _types(rows, ADDED_TYPES if which == "recently_added" else RECENT_TYPES),
            "top_level_next": _cursor(response),
        }

    embedded: list[dict[str, Any]] = []
    cursors = Counter()
    embedded_shapes = Counter()
    for section in rows:
        relationships = section.get("relationships")
        _require(isinstance(relationships, dict), "APPLE_HOME_RELATIONSHIPS_SHAPE")
        contents = relationships.get("contents")
        _require(isinstance(contents, dict), "APPLE_HOME_CONTENTS_SHAPE")
        cursors[_cursor(contents)] += 1
        if "data" not in contents:
            embedded_shapes["absent"] += 1
            continue
        items = _rows(contents["data"], "APPLE_HOME_ITEMS_SHAPE")
        _require(len(embedded) + len(items) <= HOME_CONTENT_LIMIT, "APPLE_HOME_ITEMS_LIMIT")
        embedded.extend(items)
        embedded_shapes["list"] += 1

    return {
        "state": "available" if rows else "empty",
        "section_count": len(rows),
        "embedded_item_count": len(embedded),
        "embedded_types": _types(embedded, HOME_TYPES),
        "top_level_next": _cursor(response),
        "contents_next": {k: cursors[k] for k in ("absent", "string", "other")},
        "embedded_data": {k: embedded_shapes[k] for k in ("absent", "list")},
    }


async def collect_apple_first_pages(
    get_data: Callable[..., Awaitable[dict[str, Any]]],
    *,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Perform exactly three first-page GETs using the injected provider session.

    The command exposing this collector must enforce existing authenticated MA
    LIBRARY_READ scope, one permitted Apple provider, and one use per process.
    Independent trusted receiver must attest image/module identity and before/
    after queue state. This helper deliberately does not claim those proofs.
    """
    _require(callable(get_data), "APPLE_CLIENT_NOT_BOUND")
    _require(
        type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= REQUEST_TIMEOUT_SECONDS,
        "APPLE_TIMEOUT_OUT_OF_RANGE",
    )
    outputs: dict[str, Any] = {}
    t0 = time.monotonic()
    for name, path, params in PATHS:
        t1 = time.monotonic()
        try:
            raw = await asyncio.wait_for(get_data(path, **params), timeout=timeout_seconds)
            outputs[name] = _summary(raw, name)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise AppleFirstPageShapeError("APPLE_REQUEST_TIMEOUT") from None
        except Exception:
            # Never expose an Apple error body, URL, account name, ID or token.
            raise AppleFirstPageShapeError("APPLE_FIRST_PAGE_UNAVAILABLE") from None
        outputs[name]["elapsed_ms"] = max(1, int((time.monotonic() - t1) * 1000 + 0.999))

    return {
        "schema": "ma1759.candidate_apple_first_pages.v1",
        "first_page_only": True,
        "additional_pages_requested": False,
        "raw_apple_content_exported": False,
        "playback_or_queue_commands_sent": False,
        "same_instance_binding": "in_process_provider_client",
        "image_and_module_identity_attested": False,
        "queue_unchanged_attested": False,
        "apple_get_count": len(PATHS),
        "total_elapsed_ms": max(1, int((time.monotonic() - t0) * 1000 + 0.999)),
        "apple": outputs,
    }
