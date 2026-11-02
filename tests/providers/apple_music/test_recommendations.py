"""Test Apple Music two-method recommendations served from the cached bulk payload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Album, Artist, Playlist, Track, UniqueList

from music_assistant.providers.apple_music.provider import AppleMusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine

INSTANCE_ID = "apple_music--test1"


def _station(station_id: str, name: str, is_live: bool = False) -> dict[str, Any]:
    """Build a station content object for a me/recommendations response."""
    return {"type": "stations", "id": station_id, "attributes": {"name": name, "isLive": is_live}}


def _recommendations_response(sections: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a me/recommendations response from section title -> station objects."""
    return {
        "data": [
            {
                "id": f"rec{index}",
                "attributes": {"title": {"stringForDisplay": title}},
                "relationships": {"contents": {"data": stations}},
            }
            for index, (title, stations) in enumerate(sections.items())
        ]
    }


def _default_response() -> dict[str, Any]:
    """Build the default two-section payload response used by most tests."""
    return _recommendations_response(
        {
            "Made for You": [_station("ra.111", "My Station One")],
            "Stations for You": [
                _station("ra.222", "My Station Two"),
                _station("ra.333", "Live Station", is_live=True),
            ],
        }
    )


@pytest.fixture
async def provider() -> AsyncGenerator[AppleMusicProvider]:
    """Create a real AppleMusicProvider with mocked mass (dict-backed cache) and API client."""
    cache_store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any) -> Any:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    mass = Mock()
    mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)
    mass.cache.set = AsyncMock(side_effect=_cache_set)
    mass.create_task = Mock(side_effect=_create_task)
    manifest = Mock()
    manifest.domain = "apple_music"
    config = Mock()
    config.instance_id = INSTANCE_ID
    config.name = "Apple Music Test"
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    provider = AppleMusicProvider(mass, manifest, config)
    provider.api_client.get_data = AsyncMock(return_value=_default_response())  # type: ignore[method-assign]
    yield provider
    await asyncio.gather(*background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_get_recommendations_returns_rows_without_items(
    provider: AppleMusicProvider,
) -> None:
    """get_recommendations() returns the payload's section rows, stripped of items."""
    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows] == [
        "made_for_you",
        "stations_for_you",
        "apple_recent_tracks_1759",
        "apple_recently_added_1759",
    ]
    assert [row.name for row in rows] == [
        "Made for You",
        "Stations for You",
        "Recently Played Tracks",
        "Recently Added",
    ]
    assert all(row.provider == INSTANCE_ID for row in rows)
    assert all(not row.items for row in rows)


@pytest.mark.asyncio
async def test_rows_and_items_share_one_backend_fetch(provider: AppleMusicProvider) -> None:
    """The rows call and both per-row items calls are served from one me/recommendations fetch."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)

    await provider.get_recommendations()
    # let the background cache-store task complete before the next calls
    await asyncio.sleep(0)
    items_one = await provider.get_recommendation_items("made_for_you")
    items_two = await provider.get_recommendation_items("stations_for_you")

    api_get_data.assert_awaited_once()
    assert [item.item_id for item in items_one] == ["ra.111"]
    assert all(isinstance(item, Playlist) for item in items_one)
    # the live station in the payload is skipped
    assert [item.item_id for item in items_two] == ["ra.222"]


@pytest.mark.asyncio
async def test_warm_cache_hit_serves_items_without_backend_fetch(
    provider: AppleMusicProvider,
) -> None:
    """A second items call is served from the stored cache entry, not a new backend fetch."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)

    items_first = await provider.get_recommendation_items("made_for_you")
    # let the background cache-store task complete before the next call
    await asyncio.sleep(0)
    items_second = await provider.get_recommendation_items("made_for_you")

    api_get_data.assert_awaited_once()
    assert [item.item_id for item in items_first] == ["ra.111"]
    assert [item.item_id for item in items_second] == ["ra.111"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: AppleMusicProvider,
) -> None:
    """An item_id not present in the payload yields an empty UniqueList."""
    result = await provider.get_recommendation_items("no_such_row")

    assert isinstance(result, UniqueList)
    assert list(result) == []


@pytest.mark.asyncio
async def test_browse_stations_returns_all_payload_stations(
    provider: AppleMusicProvider,
) -> None:
    """browse_stations flattens the stations of every payload folder."""
    stations = await provider.recommendation_manager.browse_stations()

    assert [station.item_id for station in stations] == ["ra.111", "ra.222"]


@pytest.mark.asyncio
async def test_resolve_station_id_returns_rotated_id(provider: AppleMusicProvider) -> None:
    """resolve_station_id maps a stale station id to the fresh id via the station name."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)
    rotated = _recommendations_response({"Made for You": [_station("ra.999", "My Station One")]})
    api_get_data.side_effect = [_default_response(), rotated]

    result = await provider.recommendation_manager.resolve_station_id("ra.111")

    assert result == "ra.999"
    # one payload fetch to populate the maps, one fresh fetch to learn the current id
    assert api_get_data.await_count == 2


@pytest.mark.asyncio
async def test_resolve_station_id_stores_fresh_payload_for_rows_and_items(
    provider: AppleMusicProvider,
) -> None:
    """The forced-fresh fetch of resolve_station_id is stored back to the payload cache."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)
    rotated = _recommendations_response({"Made for You": [_station("ra.999", "My Station One")]})
    api_get_data.side_effect = [_default_response(), rotated]

    result = await provider.recommendation_manager.resolve_station_id("ra.111")
    items = await provider.get_recommendation_items("made_for_you")

    assert result == "ra.999"
    # rows/items serve the refreshed (rotated) payload from the cache, without a new fetch
    assert [item.item_id for item in items] == ["ra.999"]
    assert api_get_data.await_count == 2


@pytest.mark.asyncio
async def test_resolve_station_id_after_restart_with_cached_payload(
    provider: AppleMusicProvider,
) -> None:
    """After a restart the station maps are rebuilt from the cache-served payload folders."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)
    rotated = _recommendations_response({"Made for You": [_station("ra.999", "My Station One")]})
    api_get_data.side_effect = [_default_response(), rotated]
    # warm the persistent cache, then simulate a process restart: the in-memory
    # station maps are gone while the cached payload entry survives
    await provider.get_recommendations()
    await asyncio.sleep(0)
    manager = provider.recommendation_manager
    manager._station_id_to_name.clear()
    manager._station_name_to_id.clear()

    result = await manager.resolve_station_id("ra.111")

    assert result == "ra.999"
    # the cache-served payload populated the maps without a backend fetch;
    # only the initial warm-up and the forced-fresh rotation fetch hit the API
    assert api_get_data.await_count == 2


@pytest.mark.asyncio
async def test_resolve_station_id_unknown_returns_none(provider: AppleMusicProvider) -> None:
    """resolve_station_id returns None for an id absent from the payload, without a fresh fetch."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)

    result = await provider.recommendation_manager.resolve_station_id("ra.unknown")

    assert result is None
    api_get_data.assert_awaited_once()


@pytest.mark.asyncio
async def test_household_1759_mixed_recommendation_media_and_station_browse(
    provider: AppleMusicProvider,
) -> None:
    """Existing Apple request yields typed media without changing radio browse."""
    response = _recommendations_response(
        {
            "Made for You": [
                {
                    "type": "playlists",
                    "id": "pl.custom",
                    "attributes": {"name": "Private Mix", "curatorName": "Apple Music"},
                },
                {
                    "type": "albums",
                    "id": "123",
                    "attributes": {
                        "name": "Example Album",
                        "artistName": "Example Artist",
                        "playParams": {"id": "123", "kind": "album"},
                    },
                },
                {
                    "type": "artists",
                    "id": "456",
                    "attributes": {"name": "Example Artist"},
                },
                {
                    "type": "songs",
                    "id": "789",
                    "attributes": {
                        "name": "Example Track",
                        "artistName": "Example Artist",
                        "durationInMillis": 180000,
                        "playParams": {"id": "789", "kind": "song"},
                    },
                },
                _station("ra.dynamic", "Your Radio"),
            ]
        }
    )
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = response

    rows = await provider.get_recommendations()
    await asyncio.sleep(0)
    items = await provider.get_recommendation_items("made_for_you")
    station_items = await provider.recommendation_manager.browse_stations()

    assert [row.name for row in rows] == [
        "Made for You",
        "Recently Played Tracks",
        "Recently Added",
    ]
    assert not rows[0].items
    assert [type(item) for item in items] == [Playlist, Album, Artist, Track, Playlist]
    assert [item.name for item in items] == [
        "Private Mix",
        "Example Album",
        "Example Artist",
        "Example Track",
        "Your Radio",
    ]
    assert not items[0].is_dynamic
    assert items[4].is_dynamic
    assert [item.item_id for item in station_items] == ["ra.dynamic"]
    assert [item.item_id for item in items] == ["pl.custom", "123", "456", "789", "ra.dynamic"]
    assert provider.recommendation_manager._station_id_to_name == {"ra.dynamic": "Your Radio"}
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_household_1759_count_log_contains_no_titles_or_ids(
    provider: AppleMusicProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Type counts are generated from the original fetch without disclosing contents."""
    response = _recommendations_response(
        {
            "Secret Personal Section": [
                {
                    "type": "albums",
                    "id": "confidential-album-id",
                    "attributes": {"name": "Confidential Album"},
                },
                {"type": "songs", "id": "private-song", "attributes": {}},
                _station("ra.private", "Private Station"),
            ]
        }
    )
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = response
    log = Mock()
    monkeypatch.setattr(provider.recommendation_manager.logger, "info", log)

    await provider.get_recommendation_items("secret_personal_section")
    records = [
        call.args[0] % call.args[1:]
        for call in log.call_args_list
        if call.args and str(call.args[0]).startswith("Apple recommendation resource counts")
    ]
    assert len(records) == 1
    assert "albums=1/1" in records[0]
    assert "songs=1/0" in records[0]
    assert "stations=1/1" in records[0]
    assert "Secret Personal Section" not in records[0]
    assert "Confidential Album" not in records[0]
    assert "confidential-album-id" not in records[0]
    assert "ra.private" not in records[0]
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_household_1759_unsupported_items_do_not_corrupt_radio_maps(
    provider: AppleMusicProvider,
) -> None:
    """ID-only media and unsupported live stations are ignored; radio IDs remain."""
    response = _recommendations_response(
        {
            "Stations for You": [
                {"type": "playlists", "id": "pl.missing", "attributes": {}},
                {"type": "music-videos", "id": "mv.unsupported", "attributes": {"name": "Video"}},
                _station("ra.live", "Live Radio", is_live=True),
                _station("ra.good", "Dynamic Radio"),
            ]
        }
    )
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = response

    items = await provider.get_recommendation_items("stations_for_you")
    manager = provider.recommendation_manager
    assert [item.item_id for item in items] == ["ra.good"]
    manager._station_id_to_name.clear()
    manager._station_name_to_id.clear()
    manager._populate_station_maps(await provider._recommendation_payload())
    assert manager._station_id_to_name == {"ra.good": "Dynamic Radio"}
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_1759_native_history_row_is_distinct_and_lazy(provider: AppleMusicProvider) -> None:
    """Native provider exposes history without querying history during row enumeration."""
    api = cast("AsyncMock", provider.api_client.get_data)
    rows = await provider.get_recommendations()
    assert [row.item_id for row in rows][-2] == "apple_recent_tracks_1759"
    api.assert_awaited_once()
    await asyncio.sleep(0)
    result = await provider.get_recommendation_items("made_for_you")
    assert [item.item_id for item in result] == ["ra.111"]
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_1759_native_history_uses_track_endpoint_and_parser(
    provider: AppleMusicProvider,
) -> None:
    """Retrieve actual songs, excluding stations and malformed history entries."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.reset_mock()
    api.return_value = {
        "data": [
            {
                "type": "songs",
                "id": "song.1",
                "attributes": {
                    "name": "History Song",
                    "artistName": "Example Artist",
                    "durationInMillis": 180000,
                    "playParams": {"id": "song.1", "kind": "song"},
                },
            },
            {"type": "stations", "id": "ra.1", "attributes": {"name": "Radio"}},
            {"type": "songs", "id": "broken", "attributes": {}},
        ]
    }
    result = await provider.get_recommendation_items("apple_recent_tracks_1759")
    assert [item.item_id for item in result] == ["song.1"]
    assert all(isinstance(item, Track) for item in result)
    api.assert_awaited_once_with("me/recent/played/tracks", types="songs,library-songs", limit=20)


@pytest.mark.asyncio
async def test_1759_history_failure_is_not_silently_relabelled(
    provider: AppleMusicProvider,
) -> None:
    """Permission errors propagate and do not yield station recommendations."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.side_effect = PermissionError("not authorised")
    with pytest.raises(PermissionError):
        await provider.get_recommendation_items("apple_recent_tracks_1759")
    api.assert_awaited_once_with("me/recent/played/tracks", types="songs,library-songs", limit=20)


@pytest.mark.asyncio
async def test_1759_native_history_empty_is_not_mixed_recent(
    provider: AppleMusicProvider,
) -> None:
    """An empty native history must stay empty, not become mixed recommendations."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = {"data": []}

    result = await provider.get_recommendation_items("apple_recent_tracks_1759")

    assert len(result) == 0
    api.assert_awaited_once_with("me/recent/played/tracks", types="songs,library-songs", limit=20)


@pytest.mark.asyncio
async def test_1759_native_history_preserves_api_order(
    provider: AppleMusicProvider,
) -> None:
    """Newest-first API history is retained without re-sorting by title."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = {
        "data": [
            {
                "type": "songs",
                "id": identifier,
                "attributes": {
                    "name": name,
                    "artistName": "Example Artist",
                    "durationInMillis": 180000,
                    "playParams": {"id": identifier, "kind": "song"},
                },
            }
            for identifier, name in [
                ("song.9", "Zulu"),
                ("song.2", "Alpha"),
                ("song.5", "Middle"),
            ]
        ]
    }

    result = await provider.get_recommendation_items("apple_recent_tracks_1759")

    assert [item.item_id for item in result] == ["song.9", "song.2", "song.5"]
    assert all(isinstance(item, Track) for item in result)
    api.assert_awaited_once_with("me/recent/played/tracks", types="songs,library-songs", limit=20)


@pytest.mark.asyncio
async def test_1759_native_history_does_not_hide_rate_limit(
    provider: AppleMusicProvider,
) -> None:
    """Transient endpoint failures stay visible rather than yielding fictional music."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.side_effect = TimeoutError("provider request timed out")

    with pytest.raises(TimeoutError):
        await provider.get_recommendation_items("apple_recent_tracks_1759")

    api.assert_awaited_once_with("me/recent/played/tracks", types="songs,library-songs", limit=20)


@pytest.mark.asyncio
async def test_1759_native_history_filters_invalid_resource_shapes(
    provider: AppleMusicProvider,
) -> None:
    """Unparseable and non-track entries cannot masquerade as Apple song history."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = {
        "data": [
            None,
            {"type": "playlists", "id": "pl.1", "attributes": {"name": "A Playlist"}},
            {"type": "songs", "attributes": {"name": "Missing ID"}},
            {"type": "songs", "id": "song.missing_attributes"},
        ]
    }

    result = await provider.get_recommendation_items("apple_recent_tracks_1759")

    assert len(result) == 0
    api.assert_awaited_once_with("me/recent/played/tracks", types="songs,library-songs", limit=20)


@pytest.mark.asyncio
async def test_1759_native_recently_added_uses_dedicated_library_endpoint(
    provider: AppleMusicProvider,
) -> None:
    """An empty real Recently Added library response stays empty."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = {"data": []}

    rows = await provider.get_recommendations()
    assert rows[-1].item_id == "apple_recently_added_1759"

    result = await provider.get_recommendation_items("apple_recently_added_1759")

    assert len(result) == 0
    assert api.call_args_list[-1].args == ("me/library/recently-added",)


@pytest.mark.asyncio
async def test_1759_native_recently_added_ignores_unrelated_resources(
    provider: AppleMusicProvider,
) -> None:
    """Do not mislabel catalog songs or stations as recently added library media."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = {
        "data": [
            {"type": "stations", "id": "ra.example", "attributes": {"name": "Station"}},
            {"type": "songs", "id": "song.example", "attributes": {"name": "Catalog Song"}},
            {"type": "library-albums", "id": "missing.name", "attributes": {}},
        ]
    }

    result = await provider.get_recommendation_items("apple_recently_added_1759")

    assert len(result) == 0
    api.assert_awaited_once_with("me/library/recently-added")


@pytest.mark.asyncio
async def test_1759_native_recently_added_keeps_library_playlist_order(
    provider: AppleMusicProvider,
) -> None:
    """Recently Added retains Apple's order and correct editable library identities."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.return_value = {
        "data": [
            {
                "type": "library-playlists",
                "id": "p.recent1",
                "attributes": {"name": "Most Recent", "canEdit": True},
            },
            {
                "type": "library-playlists",
                "id": "p.recent2",
                "attributes": {"name": "Earlier", "canEdit": False},
            },
        ]
    }

    result = await provider.get_recommendation_items("apple_recently_added_1759")

    assert [item.item_id for item in result] == ["p.recent1", "p.recent2"]
    assert all(isinstance(item, Playlist) for item in result)
    api.assert_awaited_once_with("me/library/recently-added")


@pytest.mark.asyncio
async def test_1759_readonly_capability_summary_counts_without_personal_data(
    provider: AppleMusicProvider,
) -> None:
    """Read only two fixed endpoints and return aggregate metadata."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.side_effect = [
        {"data": [{"type": "songs", "id": "private.song", "attributes": {"name": "Private Song"}}]},
        {"data": [{"type": "library-playlists", "id": "private.playlist"}]},
    ]
    result = await provider.get_household_readonly_capability_summary()
    assert result == {
        "recent_tracks": {"state": "available", "count": 1, "types": ["songs"]},
        "recently_added": {"state": "available", "count": 1, "types": ["library-playlists"]},
    }
    assert "private" not in str(result)
    assert [call.args[0] for call in api.await_args_list] == [
        "me/recent/played/tracks",
        "me/library/recently-added",
    ]


@pytest.mark.asyncio
async def test_1759_readonly_capability_summary_handles_empty_and_failure(
    provider: AppleMusicProvider,
) -> None:
    """Endpoint failure does not hide a successful, empty independent response."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.side_effect = [{"data": []}, TimeoutError()]
    result = await provider.get_household_readonly_capability_summary()
    assert result == {
        "recent_tracks": {"state": "empty", "count": 0, "types": []},
        "recently_added": {"state": "unavailable"},
    }


@pytest.mark.asyncio
async def test_1759_recently_added_enriches_unknown_album_without_reordering(
    provider: AppleMusicProvider,
) -> None:
    """A bounded Apple library detail read improves a placeholder in its original slot."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.side_effect = [
        {
            "data": [
                {
                    "type": "library-albums",
                    "id": "l.first",
                    "attributes": {"name": "First Real Album", "artistName": "Artist One"},
                },
                {
                    "type": "library-albums",
                    "id": "l.placeholder",
                    "attributes": {"name": "Unknown Album"},
                },
                {
                    "type": "library-albums",
                    "id": "l.last",
                    "attributes": {"name": "Last Real Album", "artistName": "Artist Two"},
                },
            ]
        },
        {
            "data": [
                {
                    "type": "library-albums",
                    "id": "l.placeholder",
                    "attributes": {
                        "name": "Recovered Album",
                        "artistName": "Recovered Artist",
                    },
                }
            ]
        },
    ]
    result = await provider.get_recommendation_items("apple_recently_added_1759")
    assert [item.name for item in result] == [
        "First Real Album",
        "Recovered Album",
        "Last Real Album",
    ]
    assert [item.item_id for item in result] == ["l.first", "l.placeholder", "l.last"]
    assert [call.args[0] for call in api.await_args_list] == [
        "me/library/recently-added",
        "me/library/albums",
    ]
    assert api.await_args_list[1].kwargs == {"ids": "l.placeholder"}


@pytest.mark.asyncio
async def test_1759_recently_added_preserves_unresolved_unknown_album(
    provider: AppleMusicProvider,
) -> None:
    """Do not invent album details when Apple does not resolve a placeholder."""
    api = cast("AsyncMock", provider.api_client.get_data)
    raw = {
        "type": "library-albums",
        "id": "l.placeholder",
        "attributes": {"name": "Unknown Album"},
    }
    api.side_effect = [{"data": [raw]}, {"data": [raw]}]
    result = await provider.get_recommendation_items("apple_recently_added_1759")
    assert len(result) == 1
    assert result[0].name == "Unknown Album"
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_1759_recently_added_resilient_to_enrichment_timeout(
    provider: AppleMusicProvider,
) -> None:
    """An optional metadata request failure cannot remove original recent albums."""
    api = cast("AsyncMock", provider.api_client.get_data)
    api.side_effect = [
        {
            "data": [
                {
                    "type": "library-albums",
                    "id": "l.placeholder",
                    "attributes": {"name": "Unknown Album"},
                }
            ]
        },
        TimeoutError(),
    ]
    result = await provider.get_recommendation_items("apple_recently_added_1759")
    assert [item.name for item in result] == ["Unknown Album"]
    assert api.await_count == 2
