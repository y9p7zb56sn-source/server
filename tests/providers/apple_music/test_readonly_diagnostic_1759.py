"""#1759 candidate-only first-page Apple shape and command safety regressions."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from music_assistant.providers.apple_music.readonly_diagnostic_1759 import (
    AppleFirstPageShapeError,
    collect_apple_first_pages,
)

HOME = "me/recommendations?include[personal-recommendation]=contents"
ADDED = "me/library/recently-added"
TRACKS = "me/recent/played/tracks"


def _responses() -> dict[str, dict]:
    return {
        HOME: {
            "data": [
                {
                    "id": "PRIVATE-HOME-ID",
                    "attributes": {"title": {"stringForDisplay": "PRIVATE TITLE"}},
                    "relationships": {
                        "contents": {
                            "data": [{"type": "stations", "id": "PRIVATE-STATION"}],
                            "next": "/v1/PRIVATE-HOME-LOCATION",
                        }
                    },
                }
            ],
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


class CandidateAppleFirstPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_three_first_pages_and_private_output(self) -> None:
        calls = []
        response = _responses()

        async def read(path: str, **kwargs: object) -> dict:
            calls.append((path, kwargs))
            return response[path]

        result = await collect_apple_first_pages(read)
        self.assertEqual(
            calls,
            [
                (HOME, {}),
                (ADDED, {}),
                (TRACKS, {"types": "songs,library-songs", "limit": 20}),
            ],
        )
        self.assertEqual(result["apple_get_count"], 3)
        self.assertTrue(result["first_page_only"])
        self.assertFalse(result["additional_pages_requested"])
        self.assertFalse(result["image_and_module_identity_attested"])
        self.assertFalse(result["queue_unchanged_attested"])
        self.assertEqual(result["apple"]["home"]["section_count"], 1)
        self.assertEqual(result["apple"]["home"]["embedded_item_count"], 1)
        self.assertEqual(result["apple"]["home"]["contents_next"]["string"], 1)
        self.assertEqual(result["apple"]["recently_added"]["types"]["library-albums"], 1)
        self.assertEqual(result["apple"]["recent_tracks"]["types"]["songs"], 1)
        for stage in result["apple"].values():
            self.assertGreaterEqual(stage["elapsed_ms"], 1)
        exported = json.dumps(result)
        for private in ("PRIVATE", "/v1/", "TOKEN", "PRIVATE TITLE"):
            self.assertNotIn(private, exported)

    async def test_authentic_empty_is_empty_not_fabricated(self) -> None:
        async def read(_path: str, **_kwargs: object) -> dict:
            return {"data": []}

        result = await collect_apple_first_pages(read)
        self.assertEqual(result["apple"]["home"]["state"], "empty")
        self.assertEqual(result["apple"]["recently_added"]["item_count"], 0)
        self.assertEqual(result["apple"]["recent_tracks"]["top_level_next"], "absent")

    async def test_apple_failure_does_not_export_message_or_replay(self) -> None:
        calls = []

        async def fail(path: str, **kwargs: object) -> dict:
            calls.append((path, kwargs))
            raise RuntimeError("PRIVATE-TOKEN-AND-ACCOUNT")

        with self.assertRaisesRegex(
            AppleFirstPageShapeError, "^APPLE_FIRST_PAGE_UNAVAILABLE$"
        ) as failure:
            await collect_apple_first_pages(fail)
        self.assertNotIn("PRIVATE", str(failure.exception))
        self.assertEqual(len(calls), 1)

    async def test_malformed_home_shape_fails_closed(self) -> None:
        response = _responses()
        response[HOME]["data"][0]["relationships"]["contents"]["data"] = "PRIVATE BAD"

        async def read(path: str, **_kwargs: object) -> dict:
            return response[path]

        with self.assertRaisesRegex(
            AppleFirstPageShapeError, "^APPLE_FIRST_PAGE_UNAVAILABLE$"
        ):
            await collect_apple_first_pages(read)

    async def test_malformed_first_page_and_oversized_pages_fail(self) -> None:
        for bad in ({}, {"data": "PRIVATE"}, {"data": [{}] * 1001}):
            async def read(_path: str, **_kwargs: object) -> dict:
                return bad

            with self.assertRaises(AppleFirstPageShapeError):
                await collect_apple_first_pages(read)

    async def test_timeout_stops_before_following_pages(self) -> None:
        calls = []

        async def hang(path: str, **_kwargs: object) -> dict:
            calls.append(path)
            await asyncio.sleep(0.05)
            return {"data": []}

        with self.assertRaisesRegex(
            AppleFirstPageShapeError, "^APPLE_REQUEST_TIMEOUT$"
        ):
            await collect_apple_first_pages(hang, timeout_seconds=0.001)
        self.assertEqual(calls, [HOME])

    async def test_cancel_is_not_misreported_as_success(self) -> None:
        async def cancel(_path: str, **_kwargs: object) -> dict:
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await collect_apple_first_pages(cancel)

    async def test_only_existing_provider_client_is_passed(self) -> None:
        from music_assistant.providers.apple_music.provider import AppleMusicProvider

        read = AsyncMock(side_effect=lambda path, **_kwargs: _responses()[path])
        provider = SimpleNamespace(api_client=SimpleNamespace(get_data=read))
        result = await AppleMusicProvider.get_household_readonly_first_page_shapes(provider)
        self.assertEqual(result["apple_get_count"], 3)
        self.assertEqual(read.await_count, 3)

    async def test_endpoint_requires_authenticated_read_scope_and_is_one_use(self) -> None:
        from music_assistant.controllers.music.controller import MusicController
        from music_assistant_models.auth import Scope

        cmd = MusicController.ma1759_apple_first_page_shapes
        self.assertTrue(cmd.api_authenticated)
        self.assertEqual(cmd.api_required_scope, Scope.LIBRARY_READ)
        self.assertEqual(cmd.api_cmd, "music/ma1759_apple_first_page_shapes")
        response = AsyncMock(return_value={"apple_get_count": 3})
        provider = SimpleNamespace(
            domain="apple_music", get_household_readonly_first_page_shapes=response
        )
        fake = SimpleNamespace(
            _ma1759_first_pages_consumed=False,
            get_provider_instances=lambda domain: [provider] if domain == "apple_music" else [],
            _apply_user_provider_filter=lambda providers: providers,
        )
        self.assertEqual((await cmd(fake))["apple_get_count"], 3)
        self.assertTrue(fake._ma1759_first_pages_consumed)
        with self.assertRaisesRegex(ValueError, "MA1759_DIAGNOSTIC_ALREADY_CONSUMED"):
            await cmd(fake)
        response.assert_awaited_once()

    async def test_ambiguous_or_disallowed_provider_never_probes(self) -> None:
        from music_assistant.controllers.music.controller import MusicController

        probe = AsyncMock(return_value={})
        provider = SimpleNamespace(
            domain="apple_music", get_household_readonly_first_page_shapes=probe
        )
        for candidates in ([], [provider, provider]):
            fake = SimpleNamespace(
                _ma1759_first_pages_consumed=False,
                get_provider_instances=lambda domain, rows=candidates: rows,
                _apply_user_provider_filter=lambda providers: providers,
            )
            with self.assertRaises(ValueError):
                await MusicController.ma1759_apple_first_page_shapes(fake)
            self.assertFalse(fake._ma1759_first_pages_consumed)
        probe.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
