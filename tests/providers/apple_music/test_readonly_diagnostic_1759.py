"""Tests for #1759 first-page Apple diagnostics and one-use command admission."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.music.controller import MusicController
from music_assistant.providers.apple_music.provider import AppleMusicProvider
from music_assistant.providers.apple_music.readonly_diagnostic_1759 import (
    AppleFirstPageShapeError,
    collect_apple_first_pages,
)

HOME = "me/recommendations?include[personal-recommendation]=contents"
ADDED = "me/library/recently-added"
TRACKS = "me/recent/played/tracks"


def _responses() -> dict[str, dict]:
    """Return synthetic account-identifiable responses that must never be exported."""
    return {
        HOME: {
            "data": [{
                "id": "PRIVATE-HOME-ID",
                "attributes": {"title": {"stringForDisplay": "PRIVATE TITLE"}},
                "relationships": {"contents": {
                    "data": [{"type": "stations", "id": "PRIVATE-STATION"}],
                    "next": "/v1/PRIVATE-HOME-LOCATION",
                }},
            }],
            "next": "/v1/PRIVATE-NEXT",
        },
        ADDED: {
            "data": [
                {"type": "library-albums", "id": "PRIVATE-ALBUM"},
                {"type": "library-songs", "id": "PRIVATE-SONG"},
            ],
            "next": "/v1/PRIVATE-ADDED-NEXT",
        },
        TRACKS: {
            "data": [{"type": "songs", "id": "PRIVATE-TRACK"}],
            "next": "/v1/PRIVATE-TRACK-NEXT",
        },
    }


@pytest.mark.asyncio
async def test_exact_three_first_pages_and_private_output() -> None:
    """Limit requests to the three approved first pages and export no raw values."""
    calls: list[tuple[str, dict]] = []
    responses = _responses()

    async def read(path: str, **kwargs: object) -> dict:
        calls.append((path, kwargs))
        return responses[path]

    result = await collect_apple_first_pages(read)
    assert calls == [
        (HOME, {}),
        (ADDED, {}),
        (TRACKS, {"types": "songs,library-songs", "limit": 20}),
    ]
    assert result["apple_get_count"] == 3
    assert result["first_page_only"] is True
    assert result["additional_pages_requested"] is False
    assert result["image_and_module_identity_attested"] is False
    assert result["queue_unchanged_attested"] is False
    assert result["apple"]["home"]["section_count"] == 1
    assert result["apple"]["home"]["embedded_item_count"] == 1
    assert result["apple"]["home"]["contents_next"]["string"] == 1
    assert result["apple"]["recently_added"]["types"]["library-albums"] == 1
    assert result["apple"]["recent_tracks"]["types"]["songs"] == 1
    assert all(stage["elapsed_ms"] >= 1 for stage in result["apple"].values())
    exported = json.dumps(result)
    for private in ("PRIVATE", "/v1/", "TOKEN", "PRIVATE TITLE"):
        assert private not in exported


@pytest.mark.asyncio
async def test_authentic_empty_is_empty_not_fabricated() -> None:
    """Preserve real emptiness for each Apple first page."""
    async def read(_path: str, **_kwargs: object) -> dict:
        return {"data": []}

    result = await collect_apple_first_pages(read)
    assert result["apple"]["home"]["state"] == "empty"
    assert result["apple"]["recently_added"]["item_count"] == 0
    assert result["apple"]["recent_tracks"]["top_level_next"] == "absent"


@pytest.mark.asyncio
async def test_apple_failure_does_not_export_message_or_replay() -> None:
    """Stop after first failure without leaking the provider error."""
    calls: list[tuple[str, dict]] = []

    async def fail(path: str, **kwargs: object) -> dict:
        calls.append((path, kwargs))
        raise RuntimeError("PRIVATE-TOKEN-AND-ACCOUNT")

    with pytest.raises(AppleFirstPageShapeError, match=r"^APPLE_FIRST_PAGE_UNAVAILABLE$") as exc:
        await collect_apple_first_pages(fail)
    assert "PRIVATE" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_malformed_home_shape_fails_closed() -> None:
    """Reject non-list embedded content rather than inventing counts."""
    responses = _responses()
    responses[HOME]["data"][0]["relationships"]["contents"]["data"] = "PRIVATE BAD"

    async def read(path: str, **_kwargs: object) -> dict:
        return responses[path]

    with pytest.raises(AppleFirstPageShapeError, match=r"^APPLE_FIRST_PAGE_UNAVAILABLE$"):
        await collect_apple_first_pages(read)


@pytest.mark.asyncio
async def test_malformed_first_page_and_oversized_pages_fail() -> None:
    """Reject missing, invalid and unbounded native response shapes."""
    for bad in ({}, {"data": "PRIVATE"}, {"data": [{}] * 1001}):
        read = AsyncMock(return_value=bad)
        with pytest.raises(AppleFirstPageShapeError):
            await collect_apple_first_pages(read)
        read.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_stops_before_following_pages() -> None:
    """Abort the first slow Apple request without continuing to other endpoints."""
    calls: list[str] = []

    async def hang(path: str, **_kwargs: object) -> dict:
        calls.append(path)
        await asyncio.sleep(0.05)
        return {"data": []}

    with pytest.raises(AppleFirstPageShapeError, match=r"^APPLE_REQUEST_TIMEOUT$"):
        await collect_apple_first_pages(hang, timeout_seconds=0.001)
    assert calls == [HOME]


@pytest.mark.asyncio
async def test_cancel_does_not_become_success() -> None:
    """Propagate cancellation without initiating a second Apple read."""
    async def cancel(_path: str, **_kwargs: object) -> dict:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await collect_apple_first_pages(cancel)


@pytest.mark.asyncio
async def test_only_existing_provider_client_is_used() -> None:
    """Bind the private helper to the already-authenticated provider client."""
    read = AsyncMock(side_effect=lambda path, **_kwargs: _responses()[path])
    provider = SimpleNamespace(api_client=SimpleNamespace(get_data=read))
    result = await AppleMusicProvider.get_household_readonly_first_page_shapes(provider)
    assert result["apple_get_count"] == 3
    assert read.await_count == 3


@pytest.mark.asyncio
async def test_endpoint_requires_authenticated_read_scope_and_is_one_use() -> None:
    """Enforce authentic MA scope, unique instance and a consumed one-shot."""
    command = MusicController.ma1759_apple_first_page_shapes
    assert command.api_authenticated is True
    assert command.api_required_scope == Scope.LIBRARY_READ
    assert command.api_cmd == "music/ma1759_apple_first_page_shapes"
    response = AsyncMock(return_value={"apple_get_count": 3})
    provider = SimpleNamespace(
        domain="apple_music", get_household_readonly_first_page_shapes=response
    )
    fake = SimpleNamespace(
        _ma1759_first_pages_consumed=False,
        get_provider_instances=lambda domain: [provider] if domain == "apple_music" else [],
        _apply_user_provider_filter=lambda providers: providers,
    )
    assert (await command(fake))["apple_get_count"] == 3
    assert fake._ma1759_first_pages_consumed is True
    with pytest.raises(InvalidDataError, match="MA1759_DIAGNOSTIC_ALREADY_CONSUMED"):
        await command(fake)
    response.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_or_disallowed_provider_never_probes() -> None:
    """Refuse zero or multiple Apple providers before using any Apple session."""
    probe = AsyncMock(return_value={})
    provider = SimpleNamespace(
        domain="apple_music", get_household_readonly_first_page_shapes=probe
    )
    for candidates in ([], [provider, provider]):
        fake = SimpleNamespace(
            _ma1759_first_pages_consumed=False,
            get_provider_instances=lambda _domain, rows=candidates: rows,
            _apply_user_provider_filter=lambda providers: providers,
        )
        with pytest.raises(InvalidDataError):
            await MusicController.ma1759_apple_first_page_shapes(fake)
        assert fake._ma1759_first_pages_consumed is False
    probe.assert_not_awaited()
