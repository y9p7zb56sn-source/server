# #1759 candidate-only Apple first-page diagnostic — SOURCE ONLY

Owning issue: [Home Automation #1759](https://github.com/y9p7zb56sn-source/home-assistant-automations/issues/1759).
Base: protected source-only native integration branch `validation/1759-nonhosted-integration-20261121`, merge `adc6d2a9583bc2efc65719e0931af782c316c8c2`.

## Scope and current truth

This is **not installed** in `music-assistant-1759-candidate`; creating or reviewing this fork branch does not update the running image. No credentials, NAS, player, group, queue, backend or dashboard are touched. #1755 Development HOLD, #47 playback ownership, #723 closed/parked and final Production admission remain unchanged.

The candidate adds one no-argument command
`music/ma1759_apple_first_page_shapes` via the existing authenticated MusicController with `LIBRARY_READ` permission (not anonymous; no impersonation). It applies Music Assistant's current user provider filter, requires exactly one enabled and authorised `apple_music` provider instance, and is consumed **before** awaiting its first response so concurrent calls or failures cannot perform subsequent reads in the same process lifetime. Restart would reset this in-memory one-use latch; this is not a durable operator receipt and does not grant a restart.

The command invokes the *existing in-process AppleMusicProvider.api_client.get_data*, and only the following **first pages**:

1. `me/recommendations?include[personal-recommendation]=contents` (Home);
2. `me/library/recently-added` (Recently Added);
3. `me/recent/played/tracks` with literal `types=songs,library-songs`, `limit=20` (Recently Played Tracks).

It follows **no** pagination links, issues no additional GETs for detail enrichment, and contains no Apple POST/PUT/DELETE, playback, refresh, synchronisation, event or queue command. Each GET has a hard asynchronous 10-second wait limit. Transport cancellation is propagated and failed/malformed responses abort before further calls.

The returned object consists only of counts, known fixed type buckets, next-field **type** (absent/string/other), bounded Home per-shelf embedded shape counts, the timings and explicit `image_and_module_identity_attested=false` / `queue_unchanged_attested=false`. Raw Apple results, titles, user/station/item IDs, actual next URLs, credentials, exception messages and provider instance identity are not emitted. **Source fixtures are not authenticated live evidence.**

## Complete six-operation observation boundary

The existing authenticated MA `music/browse` root and verified-Apple-stations path use `LIBRARY_READ`; existing `player_queues/all` requires `QUEUES_READ`. A future admitted NAS-local fixed-command receiver must independently obtain those reads and use the existing private `scripts/media/native_readonly_shape.py` sanitiser without exporting raw queue data. This command alone cannot provide the six-operation acceptance, source image digest, actual installed module digests, before/after queue equality or trusted runtime provenance.

Before any native execution, separately verify: (a) a genuinely installed reviewed NAS-local receiver/installer and existing permission/credential scope; (b) exact container/image ID and provider-module hashes; (c) the same MA instance and Apple provider binding; (d) actual `LIBRARY_READ` and `QUEUES_READ` authority with no new token issuance; (e) a before/after immutable queue/group snapshot inside the trusted private receiver; (f) strict output sanitisation, time limits, fail-closed behaviour and no credential/log leakage. The source branch itself authorises **none** of these.

## Validation and release

`tests/providers/apple_music/test_readonly_diagnostic_1759.py` exercises fixed three-call order and parameters, private-output non-leakage, cursor projection, genuine empty, malformed/oversized result, cancellation, timeouts and one-use permission/provenance guards. Run exact-head Python/Ruff and actual native regression tests through the **existing approved self-hosted, isolated** test path; GitHub-hosted fork workflows remain prohibited. Obtain independent source/privacy/permission review of the exact new head and protected PR integration before considering any candidate image build or separate installation.

Do not merge into `dev` or `stable`, activate against a live provider, install this controller, restart Music Assistant, change any queue, bypass #1755 HOLD, or claim Production acceptance under this source-only decision. If source review fails, correct the same candidate and retest. Before merge, rollback is to close the draft PR and retain the branch/history; after an authorised source-only merge, rollback is a separately governed protected revert. There is no live rollback because no live change has been made.
