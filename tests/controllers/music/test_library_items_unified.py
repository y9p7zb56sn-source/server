"""Executable Music Assistant SQLite/controller regressions for opt-in unified saved Songs."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.constants import DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio


def _mapping(instance: str, *, in_library: bool = True) -> ProviderMapping:
    return ProviderMapping(
        item_id=uuid4().hex,
        provider_domain=instance.removesuffix("_inst"),
        provider_instance=instance,
        in_library=in_library,
    )


@pytest.fixture(scope="module")
async def unified_mass(
    music_mass_module: MusicAssistant,
) -> tuple[MusicAssistant, dict[int, str]]:
    """Seed actual MA library SQLite, artist FTS, provider mappings and filter columns."""
    mass = music_mass_module
    matching = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Nebula Collective",
            provider_mappings={_mapping("prov_a_inst")},
        )
    )
    ordinary = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Ordinary Ensemble",
            provider_mappings={_mapping("prov_b_inst")},
        )
    )
    secondary = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Another Collaborator",
            provider_mappings={_mapping("prov_a_inst")},
        )
    )

    ids: dict[int, str] = {}
    for idx in range(61):
        # Ten title hits and 51 artist-only hits; row 0 overlaps title and artist.
        title = (
            f"Nebula Title {idx:02d}"
            if idx < 10
            else ("7th Tune 60" if idx == 60 else f"Other Tune {idx:02d}")
        )
        artists = [matching if idx == 0 or idx >= 10 else ordinary]
        if idx in (0, 12):
            artists.append(secondary)
        db_item = await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name=title,
                provider_mappings={_mapping("prov_a_inst" if idx % 2 == 0 else "prov_b_inst")},
                artists=UniqueList(artists),
                favorite=idx % 2 == 0,
            )
        )
        ids[idx] = db_item.item_id
        db_id = int(db_item.item_id)
        if idx % 3 == 0:
            await mass.music.database.insert(
                DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
                {"genre_id": 7, "media_id": db_id, "media_type": "track"},
            )
        await mass.music.database.execute(
            "UPDATE tracks SET "
            "play_count = 5, "
            "last_played = :last_played, "
            "metadata = json_set(COALESCE(metadata, '{}'), '$.explicit', :explicit) "
            "WHERE item_id = :item_id",
            {
                "item_id": db_id,
                "last_played": db_id if idx % 5 == 0 else 0,
                "explicit": int(idx % 4 == 0),
            },
        )
    hidden = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name="Nebula Hidden Unavailable",
            artists=UniqueList([matching]),
            provider_mappings={_mapping("prov_a_inst")},
        )
    )
    await mass.music.database.execute(
        "UPDATE provider_mappings SET in_library = 0 "
        "WHERE media_type = 'track' AND item_id = :item_id",
        {"item_id": int(hidden.item_id)},
    )
    await mass.music.database.commit()
    return mass, ids


async def test_command_registered_with_existing_read_scope(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """A separate opt-in handler retains legacy and does not acquire playback scope."""
    mass, _ = unified_mass
    unified = mass.command_handlers["music/tracks/library_items_unified"]
    legacy = mass.command_handlers["music/tracks/library_items"]
    assert unified.required_scope == Scope.LIBRARY_READ
    assert unified.allow_impersonation is True
    assert legacy.required_scope == Scope.LIBRARY_READ
    assert unified is not legacy


async def test_61_unique_matches_paginate_25_25_11(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """All 51 artist-only hits appear after first-page title hits, without fanout."""
    mass, ids = unified_mass
    pages = [
        await mass.music.tracks.library_items_unified(search="Nebula", limit=25, offset=offset)
        for offset in (0, 25, 50)
    ]
    assert [len(p) for p in pages] == [25, 25, 11]
    items = [track for page in pages for track in page]
    assert len({t.item_id for t in items}) == 61
    assert {t.item_id for t in items} == set(ids.values())
    assert [t.name for t in items] == sorted(t.name for t in items)
    assert await mass.music.tracks.library_items_unified(search="Nebula", limit=25, offset=61) == []


async def test_short_like_and_longer_fts_have_identical_union(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """Short terms use LIKE; full terms use native FTS5 for titles and artists."""
    mass, ids = unified_mass
    short = await mass.music.tracks.library_items_unified(search="Ne", limit=100)
    long = await mass.music.tracks.library_items_unified(search="Nebula", limit=100)
    assert {t.item_id for t in short} == set(ids.values())
    assert {t.item_id for t in long} == set(ids.values())


async def test_global_letters_before_paging(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """Letter selection must filter the total matching set, never one fetched page."""
    mass, ids = unified_mass
    o1 = await mass.music.tracks.library_items_unified(letter="O", limit=25)
    o2 = await mass.music.tracks.library_items_unified(letter="o", limit=25, offset=25)
    numeric = await mass.music.tracks.library_items_unified(letter="#")
    title_only = await mass.music.tracks.library_items_unified(letter="N", limit=25)
    assert len(o1) == len(o2) == 25
    assert len({i.item_id for i in [*o1, *o2]}) == 50
    assert [i.item_id for i in numeric] == [ids[60]]
    assert len(title_only) == 10
    assert all(i.name.startswith("Nebula") for i in title_only)


async def test_all_filters_apply_to_title_and_artist_matches(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Previously dropped played_only now applies to every artist-only match."""
    mass, ids = unified_mass
    monkeypatch.setattr(
        mass.music,
        "get_active_provider_instances",
        lambda: ["prov_a_inst", "prov_b_inst"],
    )
    selected = await mass.music.tracks.library_items_unified(
        search="Nebula",
        provider="prov_a_inst",
        reachable_via=["prov_a_inst"],
        favorite=True,
        played_only=True,
        genre=7,
        explicit=False,
        limit=25,
    )
    assert [t.item_id for t in selected] == [ids[30]]
    assert (
        len(
            await mass.music.tracks.library_items_unified(
                search="Nebula", played_only=True, limit=100
            )
        )
        == 13
    )
    assert (
        len(await mass.music.tracks.library_items_unified(search="Nebula", genre=7, limit=100))
        == 21
    )
    assert (
        await mass.music.tracks.library_items_unified(
            search="Nebula", reachable_via=["unavailable"], limit=25
        )
        == []
    )


async def test_deterministic_bidirectional_sort_and_artist_ties(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """Every allowlisted sort has deterministic global direction and item-ID ties."""
    mass, ids = unified_mass
    for key in ("sort_name", "name", "track_artist_name", "play_count", "favorite"):
        asc = await mass.music.tracks.library_items_unified(
            search="Nebula", order_by=key, limit=100
        )
        desc = await mass.music.tracks.library_items_unified(
            search="Nebula", order_by=f"{key}_desc", limit=100
        )
        assert len(asc) == len(desc) == 61
        assert [i.item_id for i in asc] == [i.item_id for i in reversed(desc)]
        assert len({i.item_id for i in asc}) == 61
    # Two-artists entries do not repeat after correlated artist sort.
    artist = await mass.music.tracks.library_items_unified(order_by="track_artist_name", limit=100)
    assert set(ids.values()) == {i.item_id for i in artist}


async def test_full_hydration_and_unavailable_or_empty(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """Full items remain native Track objects; unavailable/empty cases stay honest."""
    mass, _ = unified_mass
    tracks = await mass.music.tracks.library_items_unified(search="Nebula", summary=False, limit=2)
    assert len(tracks) == 2
    assert all(isinstance(t, Track) for t in tracks)
    assert await mass.music.tracks.library_items_unified(search="zzznotpresentzzz", limit=25) == []


@pytest.mark.parametrize(
    "invalid",
    [
        {"limit": 0},
        {"limit": 251},
        {"limit": True},
        {"offset": -1},
        {"offset": True},
        {"offset": 2**63},
        {"order_by": "random"},
        {"order_by": "sort_name; DROP TABLE tracks"},
        {"order_by": None},
        {"search": ""},
        {"search": "***"},
        {"search": 9},
        {"letter": "AA"},
        {"letter": "1"},
        {"favorite": 1},
        {"played_only": "yes"},
        {"summary": "no"},
        {"genre": [True]},
        {"genre": []},
        {"genre": 2**63},
        {"genre": [7, 2**63]},
        {"provider": [""]},
        {"provider": 123},
        {"reachable_via": "prov_a_inst"},
    ],
)
async def test_invalid_inputs_fail_closed(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
    invalid: dict[str, Any],
) -> None:
    """No unknown SQL, malformed offset or silently ignored input is accepted."""
    mass, _ = unified_mass
    with pytest.raises(InvalidDataError):
        await mass.music.tracks.library_items_unified(**invalid)


async def test_maximum_signed_sqlite_genre_id_is_accepted(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """The SQLite INTEGER upper boundary is valid without an overflow."""
    mass, _ = unified_mass
    assert await mass.music.tracks.library_items_unified(genre=2**63 - 1) == []


async def test_unknown_command_and_database_errors_do_not_fallback(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """No legacy fallback or fabricated empty response on native database errors."""
    mass, _ = unified_mass
    assert "music/tracks/library_items_unified_unsupported" not in mass.command_handlers
    with (
        patch.object(
            mass.music.database,
            "get_rows_from_query",
            side_effect=RuntimeError("database unavailable"),
        ),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        await mass.music.tracks.library_items_unified(search="Nebula")


async def test_unified_query_does_not_touch_playback(
    unified_mass: tuple[MusicAssistant, dict[int, str]],
) -> None:
    """Read-only listing uses only music database query entrypoints."""
    mass, _ = unified_mass
    with patch.object(
        mass.music.database,
        "get_rows_from_query",
        wraps=mass.music.database.get_rows_from_query,
    ) as query:
        await mass.music.tracks.library_items_unified(search="Nebula", limit=25, offset=25)
        assert query.call_count == 1
        statement = query.call_args.args[0]
        assert "EXISTS" in statement
        assert "ORDER BY" in statement
        assert "FROM tracks" in statement
        # Correlated hydration may legitimately JOIN artists; no outer fanout.
        outer = statement.rsplit("FROM tracks", 1)[1].split(" WHERE ", 1)[0]
        assert "JOIN track_artists" not in outer
        assert "GROUP BY tracks.item_id" not in statement
