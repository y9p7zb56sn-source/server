"""
Recommendations and station helpers for Apple Music.

Adapted from Music Assistant 2.10.1 under Apache-2.0; prototype only.
https://github.com/music-assistant/server/blob/2.10.1/music_assistant/providers/apple_music/recommendations.py
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientResponseError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    Playlist,
    RecommendationFolder,
    Track,
)

from music_assistant.controllers.cache import use_cache

from .parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_station_as_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from .provider import AppleMusicProvider


def _slugify_title(title: str) -> str:
    """Return a stable, alphanumeric slug for a recommendation folder title."""
    slug = "".join(c for c in title.lower().replace(" ", "_") if c.isalnum() or c == "_")
    if not slug:
        # Fall back to a hash for titles that yield no alphanumeric characters.
        slug = hashlib.md5(title.encode(), usedforsecurity=False).hexdigest()[:12]
    return slug


class AppleMusicRecommendationManager:
    """Handles recommendations, stations, and similar-track lookups."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize recommendation manager."""
        self.provider = provider
        self._station_id_to_name: dict[str, str] = {}
        self._station_name_to_id: dict[str, str] = {}
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.api = provider.api_client
        self.logger = provider.logger

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """
        Retrieve tracks similar to the provided track.

        :param prov_track_id: The Apple Music track ID.
        :param limit: Maximum number of tracks to return.
        """
        if limit <= 0:
            return []
        endpoint = f"me/stations/next-tracks/ra.{prov_track_id}"
        try:
            response = await self.api.post_data(endpoint, include="artists")
        except ClientResponseError as err:
            if err.status == 500:
                self.logger.debug("Similar tracks unavailable for %s (%s)", prov_track_id, endpoint)
                return []
            raise
        if not response:
            return []
        tracks = [track for track in response.get("data", []) if track and track.get("id")][:limit]
        if not tracks:
            return []
        track_ids = [track["id"] for track in tracks]
        rating_response = await self.api.get_ratings(track_ids, MediaType.TRACK)
        return [
            parse_track(self.provider, track, rating_response.get(track["id"])) for track in tracks
        ]

    @use_cache(3600 * 24)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist via Apple Music similar-artists view."""
        storefront = self.provider._storefront
        response = await self.api.get_data(
            f"catalog/{storefront}/artists/{prov_artist_id}",
            views="similar-artists",
        )
        data = response.get("data", [])
        if not data:
            return []
        similar = data[0].get("views", {}).get("similar-artists", {}).get("data", [])
        artists: list[Artist] = []
        for artist_obj in similar[:limit]:
            parsed = parse_artist(self.provider, artist_obj)
            if isinstance(parsed, Artist):
                artists.append(parsed)
        return artists

    async def get_station_playlist(self, station_id: str) -> Playlist:
        """Fetch name and artwork for a radio station and return it as a dynamic Playlist."""
        try:
            station_response = await self.api.get_data(
                f"catalog/{self.provider._storefront}/stations/{station_id}"
            )
            station_obj = station_response["data"][0]
            station_obj["id"] = station_id
            return parse_station_as_playlist(self.provider, station_obj)
        except MediaNotFoundError, KeyError, IndexError:
            return parse_station_as_playlist(self.provider, {"id": station_id})

    def _log_recommendation_counts(self, response: dict[str, Any]) -> None:
        """Report private aggregate recommendation counts without additional Apple requests."""
        # Count resource types from the already-authorised response. Never log
        # individual titles, station IDs, account data or API credentials.
        observed = {
            kind: [0, 0] for kind in ("albums", "artists", "playlists", "songs", "stations")
        }
        for section in response.get("data", []):
            for raw in section.get("relationships", {}).get("contents", {}).get("data", []):
                kind = raw.get("type")
                if kind in observed:
                    observed[kind][0] += 1
                    attrs = raw.get("attributes") or {}
                    if raw.get("id") and isinstance(attrs, dict) and attrs.get("name"):
                        observed[kind][1] += 1
        totals = tuple((kind, *values) for kind, values in observed.items())
        if totals != getattr(self, "_last_recommendation_counts", None):
            self.logger.info(
                "Apple recommendation resource counts (total/named): %s",
                " ".join(f"{kind}={total}/{named}" for kind, total, named in totals),
            )
            self._last_recommendation_counts = totals

    def _parse_recommended_media(
        self, item_type: str, item: dict[str, Any]
    ) -> Album | Artist | ItemMapping | Playlist | Track | None:
        """Convert Apple recommendation media using the existing provider parsers."""
        try:
            if item_type == "artists":
                return parse_artist(self.provider, item)
            if item_type == "albums":
                return parse_album(self.provider, item)
            if item_type == "songs":
                return parse_track(self.provider, item)
            return parse_playlist(self.provider, item)
        except (KeyError, TypeError, ValueError) as err:
            self.logger.debug(
                "Skipping incomplete Apple recommendation %s: %s",
                item_type,
                type(err).__name__,
            )
            return None

    async def get_recently_played_tracks(self, limit: int = 20) -> list[Track]:
        """
        Read actual recent song history; never substitute recommendation stations.

        This is source-only until a supported Music Assistant provider API entry
        point has been selected and authorised for the running installation.
        """
        if not 1 <= limit <= 30:
            raise ValueError("Apple recent tracks limit must be between 1 and 30")
        response = await self.api.get_data(
            "me/recent/played/tracks", types="songs,library-songs", limit=limit
        )
        tracks: list[Track] = []
        for raw in response.get("data", []):
            if not isinstance(raw, dict) or raw.get("type") not in ("songs", "library-songs"):
                continue
            attrs = raw.get("attributes")
            if not raw.get("id") or not isinstance(attrs, dict) or not attrs.get("name"):
                continue
            try:
                parsed = parse_track(self.provider, raw)
            except KeyError, TypeError, ValueError:
                continue
            if isinstance(parsed, Track):
                tracks.append(parsed)
        return tracks[:limit]

    async def get_personal_recommendations(self) -> list[RecommendationFolder]:  # noqa: PLR0915
        """
        Return supported Apple recommendations, not stations alone.

        Keep authenticated Apple Music and playback inside Music Assistant.
        Retain provider order, preserve repeated items across different rows,
        and skip unsupported/incomplete entries without failing other rows.
        """
        # Source-only diagnostic candidate (#1759). Warning-level status is
        # deliberately independent of the provider's INFO-level count logger.
        self.logger.warning(
            "MA1759_REFRESH=START effective_level=%s",
            self.logger.getEffectiveLevel(),
        )
        try:
            response = await self.api.get_data(
                "me/recommendations?include[personal-recommendation]=contents"
            )
        except BaseException as exc:
            # No exception message, account data or raw response in diagnostic logs.
            event = "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "FETCH_FAILED"
            self.logger.warning("MA1759_REFRESH=%s", event)
            raise
        counts = dict.fromkeys(("albums", "artists", "songs", "playlists", "stations"), 0)
        for section in response.get("data", []):
            for item in section.get("relationships", {}).get("contents", {}).get("data", []):
                kind = item.get("type")
                if kind in counts:
                    counts[kind] += 1
        self.logger.warning(
            "MA1759_REFRESH=APPLE_RESPONSE %s",
            " ".join(f"{kind}={count}" for kind, count in counts.items()),
        )
        self._log_recommendation_counts(response)

        folders: dict[str, RecommendationFolder] = {}
        seen: set[tuple[str, str, str]] = set()
        self._station_id_to_name.clear()
        self._station_name_to_id.clear()

        for recommendation in response.get("data", []):
            rec_id = recommendation.get("id", "")
            title = (
                recommendation.get("attributes", {}).get("title", {}).get("stringForDisplay", "")
            )
            if not rec_id or not title:
                continue
            contents = recommendation.get("relationships", {}).get("contents", {})
            for item in contents.get("data", []):
                item_type = item.get("type")
                item_id = item.get("id")
                if not item_id or item_type not in {
                    "artists",
                    "albums",
                    "songs",
                    "playlists",
                    "stations",
                }:
                    continue
                key = (title, item_type, str(item_id))
                if key in seen:
                    continue

                attributes = item.get("attributes") or {}
                media: Album | Artist | ItemMapping | Playlist | Track | None
                if item_type == "stations":
                    if attributes.get("isLive", False):
                        # Existing Widevine-protected live station exclusion.
                        continue
                    if attributes.get("name"):
                        media = parse_station_as_playlist(self.provider, item)
                    else:
                        media = await self.provider.get_playlist(item_id)
                    if not media.name or media.name == item_id:
                        continue
                    self._station_id_to_name[item_id] = media.name
                    self._station_name_to_id[media.name] = item_id
                else:
                    # Apple can return ID-only relationship references. Do not
                    # invent titles or make network requests for every card.
                    if not isinstance(attributes, dict) or not attributes.get("name"):
                        continue
                    media = self._parse_recommended_media(item_type, item)
                    if media is None or not media.name:
                        continue

                seen.add(key)
                if title not in folders:
                    folders[title] = RecommendationFolder(
                        item_id=_slugify_title(title),
                        provider=self.provider.instance_id,
                        name=title,
                    )
                folders[title].items.append(media)

        return list(folders.values())

    async def resolve_station_id(self, stale_id: str) -> str | None:
        """
        Return the current station ID for a stale one.

        :param stale_id: The outdated station ID that may have been rotated by Apple.
        """
        station_name = self._station_id_to_name.get(stale_id)
        if not station_name:
            # Maps may be empty after a process restart; populate from the cached payload first.
            self._populate_station_maps(await self.provider._recommendation_payload())
            station_name = self._station_id_to_name.get(stale_id)
            if not station_name:
                return None
        # Apple rotates station ids: refresh through the mixin cache so the fresh payload
        # (which rebuilds the maps) is also what rows/items serve afterwards.
        await self.provider._refresh_recommendation_payload()
        return self._station_name_to_id.get(station_name)

    async def browse_stations(self) -> list[ItemMapping | Playlist]:
        """Return recommended radio stations from personal recommendations."""
        return cast(
            "list[ItemMapping | Playlist]",
            [
                item
                for folder in await self.provider._recommendation_payload()
                for item in folder.items
                if isinstance(item, Playlist) and item.is_dynamic
            ],
        )

    def _populate_station_maps(self, folders: list[RecommendationFolder]) -> None:
        """
        Populate the station name maps from payload folders, if they are empty.

        After a process restart the payload may be served from the persistent cache
        without running get_personal_recommendations, leaving the maps empty; the
        folder items (stations parsed as playlists) carry the id/name pairs.
        """
        if self._station_id_to_name:
            return
        for folder in folders:
            for item in folder.items:
                # Mixed rows contain artists, albums and ordinary playlists.
                if (
                    isinstance(item, Playlist)
                    and item.is_dynamic
                    and item.name
                    and item.name != item.item_id
                ):
                    self._station_id_to_name[item.item_id] = item.name
                    self._station_name_to_id[item.name] = item.item_id
