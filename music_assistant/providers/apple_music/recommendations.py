"""
Recommendations and station helpers for Apple Music.

Adapted from Music Assistant 2.10.1 under Apache-2.0; prototype only.
https://github.com/music-assistant/server/blob/2.10.1/music_assistant/providers/apple_music/recommendations.py
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, quote, unquote, urlsplit

from aiohttp import ClientResponseError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
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
        # Only paths actually offered by Apple-backed rows/pages are browseable.
        self._offered_recommendation_pages: set[str] = set()

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

    @staticmethod
    def _recommendation_contents_cursor(
        next_path: object, recommendation_id: str, previous_offset: int
    ) -> tuple[int, int | None] | None:
        """Admit only Apple's advancing relative next for this exact shelf."""
        if next_path is None:
            return None
        if not isinstance(next_path, str):
            raise TypeError("Invalid Apple recommendation continuation")
        url = urlsplit(next_path)
        expected_path = f"/v1/me/recommendations/{quote(recommendation_id, safe='')}/contents"
        if url.scheme or url.netloc or url.fragment or url.path != expected_path:
            raise ValueError("Invalid Apple recommendation relationship endpoint")
        query = parse_qs(url.query, keep_blank_values=True)
        if not query or set(query) - {"offset", "limit"}:
            raise ValueError("Invalid Apple recommendation continuation parameters")
        offsets = query.get("offset")
        if (
            offsets is None
            or len(offsets) != 1
            or re.fullmatch(r"[0-9]{1,12}", offsets[0]) is None
        ):
            raise ValueError("Invalid Apple recommendation offset")
        offset = int(offsets[0])
        if offset <= previous_offset:
            raise ValueError("Non-advancing Apple recommendation continuation")
        limit_values = query.get("limit")
        limit: int | None = None
        if limit_values is not None:
            if (
                len(limit_values) != 1
                or re.fullmatch(r"[0-9]{1,4}", limit_values[0]) is None
                or int(limit_values[0]) == 0
            ):
                raise ValueError("Invalid Apple recommendation page limit")
            limit = int(limit_values[0])
        return offset, limit

    def _recommendation_continuation_folder(
        self, recommendation_id: str, next_path: object, previous_offset: int
    ) -> BrowseFolder | None:
        """Represent a genuine Apple relationship next as a lazy MA browse folder."""
        continuation = self._recommendation_contents_cursor(
            next_path, recommendation_id, previous_offset
        )
        if continuation is None:
            return None
        offset, limit = continuation
        encoded_id = quote(recommendation_id, safe="")
        path = f"{self.instance_id}://recommendations-page/{encoded_id}/{offset}"
        if limit is not None:
            path += f"/{limit}"
        self._offered_recommendation_pages.add(path)
        return BrowseFolder(
            item_id=f"recommendations-page-{hashlib.sha256(path.encode()).hexdigest()[:16]}",
            provider=self.instance_id,
            path=path,
            name="Load more from this Apple Music shelf",
        )

    async def _parse_recommendation_content(
        self, raw: dict[str, Any]
    ) -> Album | Artist | ItemMapping | Playlist | Track | None:
        """Use the same supported media parsers for first and later shelf pages."""
        item_type = raw.get("type")
        item_id = raw.get("id")
        if not item_id or item_type not in {
            "artists", "albums", "songs", "playlists", "stations"
        }:
            return None
        attributes = raw.get("attributes") or {}
        if not isinstance(attributes, dict):
            return None
        if item_type == "stations":
            if attributes.get("isLive", False):
                return None
            media = (
                parse_station_as_playlist(self.provider, raw)
                if attributes.get("name")
                else await self.provider.get_playlist(item_id)
            )
            if not media.name or media.name == item_id:
                return None
            self._station_id_to_name[item_id] = media.name
            self._station_name_to_id[media.name] = item_id
            return media
        if not attributes.get("name"):
            # A bare Apple relationship ID is not an authored shelf card.
            return None
        return self._parse_recommended_media(item_type, raw)

    async def browse_recommendation_page(
        self, path: str
    ) -> list[Album | Artist | ItemMapping | Playlist | Track | BrowseFolder]:
        """Fetch exactly one previously offered Apple relationship page, read-only."""
        payload = await self.provider._recommendation_payload()
        cached_paths = {
            item.path
            for folder in payload
            for item in folder.items
            if isinstance(item, BrowseFolder)
        }
        if path not in cached_paths and path not in self._offered_recommendation_pages:
            raise ValueError("This Apple Music shelf continuation is no longer available")
        parts = path.split("://", 1)[-1].split("/")
        if len(parts) not in {3, 4} or parts[0] != "recommendations-page":
            raise ValueError("Invalid Apple Music shelf browse path")
        encoded_id, raw_offset = parts[1:3]
        recommendation_id = unquote(encoded_id)
        if (
            not recommendation_id
            or len(recommendation_id) > 200
            or quote(recommendation_id, safe="") != encoded_id
            or re.fullmatch(r"[0-9]{1,12}", raw_offset) is None
        ):
            raise ValueError("Invalid Apple Music recommendation identity or offset")
        offset = int(raw_offset)
        if offset <= 0:
            raise ValueError("Invalid Apple Music recommendation page")
        kwargs: dict[str, int] = {"offset": offset}
        if len(parts) == 4:
            if (
                re.fullmatch(r"[0-9]{1,4}", parts[3]) is None
                or int(parts[3]) == 0
            ):
                raise ValueError("Invalid Apple Music recommendation page limit")
            kwargs["limit"] = int(parts[3])
        response = await self.api.get_data(
            f"me/recommendations/{encoded_id}/contents", **kwargs
        )
        if "data" not in response:
            raise ResourceTemporarilyUnavailable("Incomplete Apple recommendation page")
        if not isinstance(response["data"], list):
            raise TypeError("Invalid Apple recommendation page")
        items: list[Album | Artist | ItemMapping | Playlist | Track | BrowseFolder] = []
        seen: set[tuple[str, str]] = set()
        for raw in response["data"]:
            if not isinstance(raw, dict):
                continue
            key = (str(raw.get("type")), str(raw.get("id")))
            if key in seen:
                continue
            media = await self._parse_recommendation_content(raw)
            if media is not None:
                seen.add(key)
                items.append(media)
        more = self._recommendation_continuation_folder(
            recommendation_id, response.get("next"), offset
        )
        if more is not None:
            items.append(more)
        return items

    async def get_recently_added_library(
        self,
    ) -> list[Album | Artist | Playlist | Track]:
        """Read the first Apple recently-added library page for existing callers."""
        items, _ = await self.get_recently_added_library_page()
        return items

    async def get_recently_added_library_page(
        self, offset: int = 0
    ) -> tuple[list[Album | Artist | Playlist | Track], int | None]:
        """Read one genuine Apple library-chronology page and its continuation."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid Apple recently added offset")
        params = {"offset": str(offset)} if offset else {}
        response = await self.api.get_data("me/library/recently-added", **params)
        if "data" not in response:
            raise ResourceTemporarilyUnavailable("Incomplete Apple recently added response")
        raw_items = response.get("data", [])
        if not isinstance(raw_items, list):
            raise TypeError("Invalid Apple recently added response")
        # Apple can legitimately supply library-albums named "Unknown Album".
        # A single read-only library detail request may contain better metadata.
        placeholders = [
            raw["id"]
            for raw in raw_items
            if isinstance(raw, dict)
            and raw.get("type") == "library-albums"
            and isinstance(raw.get("id"), str)
            and isinstance(raw.get("attributes"), dict)
            and isinstance(raw["attributes"].get("name"), str)
            and raw["attributes"]["name"].strip().casefold() == "unknown album"
        ][:20]
        enriched: dict[str, dict[str, Any]] = {}
        if placeholders:
            try:
                detail_response = await self.api.get_data(
                    "me/library/albums", ids=",".join(dict.fromkeys(placeholders))
                )
                for detail in detail_response.get("data", []):
                    if not isinstance(detail, dict) or detail.get("type") != "library-albums":
                        continue
                    if detail.get("id") not in placeholders:
                        continue
                    attributes = detail.get("attributes")
                    if not isinstance(attributes, dict):
                        continue
                    name = attributes.get("name")
                    if (
                        isinstance(name, str)
                        and name.strip()
                        and name.strip().casefold() != "unknown album"
                    ):
                        enriched[detail["id"]] = detail
            except (
                ClientResponseError,
                LoginFailed,
                MediaNotFoundError,
                RateLimited,
                ResourceTemporarilyUnavailable,
                TimeoutError,
                ValueError,
            ):
                # Metadata enrichment is optional: preserve the original Apple resources.
                pass
        parsers = {
            "library-albums": parse_album,
            "library-artists": parse_artist,
            "library-playlists": parse_playlist,
            "library-songs": parse_track,
        }
        items: list[Album | Artist | Playlist | Track] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = enriched.get(raw.get("id"), raw)
            parser = parsers.get(item.get("type"))
            attrs = item.get("attributes")
            if parser is None or not item.get("id") or not isinstance(attrs, dict):
                continue
            if not attrs.get("name"):
                continue
            try:
                media = parser(self.provider, item)
            except KeyError, TypeError, ValueError:
                continue
            if isinstance(media, (Album, Artist, Playlist, Track)) and media.name:
                items.append(media)
        return items, self._recently_added_next_offset(response, offset)

    @staticmethod
    def _recently_added_next_offset(response: dict[str, Any], offset: int) -> int | None:
        """Read only Apple's same-endpoint pagination offset, never a supplied URL."""
        next_path = response.get("next")
        if next_path is None:
            return None
        if not isinstance(next_path, str):
            raise TypeError("Invalid Apple recently added pagination")
        url = urlsplit(next_path)
        if url.scheme or url.netloc or url.fragment or url.path != "/v1/me/library/recently-added":
            raise ValueError("Invalid Apple recently added pagination")
        params = parse_qs(url.query, keep_blank_values=True)
        if set(params) != {"offset"}:
            raise ValueError("Unexpected Apple recently added pagination parameters")
        offsets = params["offset"]
        if len(offsets) != 1 or re.fullmatch(r"[0-9]+", offsets[0]) is None:
            raise ValueError("Invalid Apple recently added offset")
        next_offset = int(offsets[0])
        if next_offset <= offset:
            raise ValueError("Non-advancing Apple recently added pagination")
        return next_offset

    @staticmethod
    def _recent_tracks_next_offset(response: dict[str, Any], offset: int) -> int | None:
        """Use only Apple's documented relative next-page path, never a caller URL."""
        next_path = response.get("next")
        if next_path is None:
            return None
        if not isinstance(next_path, str):
            raise TypeError("Invalid Apple recent tracks pagination")
        url = urlsplit(next_path)
        if url.scheme or url.netloc or url.fragment or url.path != "/v1/me/recent/played/tracks":
            raise ValueError("Invalid Apple recent tracks pagination")
        params = parse_qs(url.query, keep_blank_values=True)
        if set(params) - {"offset", "limit", "types"}:
            raise ValueError("Unexpected Apple recent tracks pagination parameters")
        offsets = params.get("offset")
        if offsets is None or len(offsets) != 1 or re.fullmatch(r"[0-9]+", offsets[0]) is None:
            raise ValueError("Invalid Apple recent tracks offset")
        if "limit" in params and (
            len(params["limit"]) != 1 or re.fullmatch(r"[0-9]+", params["limit"][0]) is None
        ):
            raise ValueError("Invalid Apple recent tracks limit")
        if "types" in params and params["types"] != ["songs,library-songs"]:
            raise ValueError("Unexpected Apple recent tracks types")
        next_offset = int(offsets[0])
        if next_offset <= offset:
            raise ValueError("Non-advancing Apple recent tracks pagination")
        return next_offset

    async def get_recently_played_tracks_page(
        self, limit: int = 30, offset: int = 0
    ) -> tuple[list[Track], int | None]:
        """Retrieve only one genuine Apple track-history page and its continuation."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("Apple recent tracks page size must be between 1 and 30")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid Apple recent tracks offset")
        params: dict[str, Any] = {"types": "songs,library-songs", "limit": limit}
        if offset:
            params["offset"] = str(offset)
        response = await self.api.get_data("me/recent/played/tracks", **params)
        if "data" not in response:
            raise ResourceTemporarilyUnavailable("Incomplete Apple recent tracks response")
        raw_items = response.get("data", [])
        if not isinstance(raw_items, list):
            raise TypeError("Invalid Apple recent tracks response")
        tracks: list[Track] = []
        for raw in raw_items:
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
        next_offset = self._recent_tracks_next_offset(response, offset)
        return tracks, next_offset

    async def get_recently_played_tracks(self, limit: int = 20) -> list[Track]:
        """Read the first actual Apple track-history page for existing callers."""
        tracks, _ = await self.get_recently_played_tracks_page(limit=limit)
        return tracks

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
        if "data" not in response:
            raise ResourceTemporarilyUnavailable("Incomplete Apple recommendations response")
        if not isinstance(response["data"], list):
            raise TypeError("Invalid Apple recommendations response")
        counts = dict.fromkeys(("albums", "artists", "songs", "playlists", "stations"), 0)
        for section in response["data"]:
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
        self._offered_recommendation_pages.clear()
        # A public shelf identity must not depend on its title or on other
        # Apple recommendations appearing in the same Home response.
        self._station_id_to_name.clear()
        self._station_name_to_id.clear()

        for recommendation in response["data"]:
            rec_id = recommendation.get("id", "")
            title = (
                recommendation.get("attributes", {}).get("title", {}).get("stringForDisplay", "")
            )
            if not rec_id or not title:
                continue
            row_id = f"apple_home_{hashlib.sha256(str(rec_id).encode()).hexdigest()[:16]}"
            contents = recommendation.get("relationships", {}).get("contents", {})
            for item in contents.get("data", []):
                if not isinstance(item, dict):
                    continue
                item_id, item_type = item.get("id"), item.get("type")
                if not item_id or not item_type:
                    continue
                key = (str(rec_id), str(item_type), str(item_id))
                if key in seen:
                    continue
                media = await self._parse_recommendation_content(item)
                if media is None:
                    continue
                seen.add(key)
                if rec_id not in folders:
                    folders[rec_id] = RecommendationFolder(
                        item_id=row_id,
                        provider=self.provider.instance_id,
                        name=title,
                    )
                folders[rec_id].items.append(media)
            continuation = self._recommendation_continuation_folder(
                rec_id, contents.get("next"), 0
            )
            if continuation is not None:
                # A shelf consisting only of a genuine continuation remains browseable.
                if rec_id not in folders:
                    folders[rec_id] = RecommendationFolder(
                        item_id=row_id,
                        provider=self.provider.instance_id,
                        name=title,
                    )
                folders[rec_id].items.append(continuation)

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
