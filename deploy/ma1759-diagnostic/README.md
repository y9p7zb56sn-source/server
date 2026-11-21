# #1759 — Candidate-only native diagnostic image (SOURCE ONLY)

Owner: Home Automation #1759. This directory belongs to fork draft PR #4.
**Not built, installed, authenticated, deployed or approved for restart.** It does
not commission a receiver or grant Docker/NAS/Apple/Production authority.

## Complete source closure

The prior two-module `deploy/media/ma1759/Dockerfile` in the Home Automation
repository pinned an earlier provider source. It does **not** build this
diagnostic. The new collector is part of the cumulative five-module runtime
delta from verified native fork baseline
`e48f9ba8f2dd2a5db17ffc112cdb563795fcdcba` to source reference
`4b2332d0a16f4c10aacf406170c2d5a09dc17dac`:

| Source module | Git blob (SHA-1) |
| --- | --- |
| `music_assistant/controllers/music/controller.py` | `f5d13036f27e03223937df1e9b24a852db9d60d9` |
| `music_assistant/controllers/music/media/tracks.py` | `53746f3f783f2bc7bce2f635dca7293046eb9aed` |
| `music_assistant/providers/apple_music/provider.py` | `e8ab0338b407306e83f948f3c66db7d0c9f696bd` |
| `music_assistant/providers/apple_music/recommendations.py` | `4f27b7f0b282376bc08e8c06d0d96b8e25c9e28d` |
| `music_assistant/providers/apple_music/readonly_diagnostic_1759.py` | `4623d6876f2d0fc9cd875c174616e5d6c23d6e71` |

These blob values are **Git object IDs, not installed file SHA-256**.
The pure standard-library `stage.py` verifies all five before creating
a new staging directory; it writes only the five modules, a fixed Dockerfile,
a build-time verifier and a SHA-256 manifest. It refuses existing targets,
changed blobs, symlinks and non-regular source files. It also pins the
staged `Dockerfile` to reviewed Git blob
`eb3bd4e2b2b709547aaaf91c454e4d84f7e2013c` and
`verify_image.py` to reviewed Git blob
`a3c7358a29db145ab9e2da487becfe4699c89cac`, independent of
the candidate checkout's current file bytes. Both are rechecked during staging;
tampering with either blocks creation of the destination. It has no Docker,
network, credential or live runtime call.

## Isolated build procedure — specification, NOT execution approval

1. Use an isolated, approved builder. Obtain the existing MA 2.10.1 image via
   a separately admitted, authenticated and immutable supply path.
   Its **local Docker `.Id` must equal**
   `sha256:e681b6aeedb25e639860b8c1e687578377277c2930908e3fa4cc7605501335ed`.
   Do **not** mistake a local image ID for a registry `@sha256` manifest digest.
   Reject absence or mismatch; do not fetch `latest`, alter a NAS container,
   or assume candidate-ci has Docker or LAN access.
2. On the isolated builder, check out the **independently reviewed exact fork
   commit** (obtain its full 40-character SHA from the approved PR/CI record).
   Before staging, check `git rev-parse HEAD` equals that specific SHA,
   `git status --porcelain` is empty, and the expected five module and two
   build-input Git blobs match the fixed pins in `stage.py`. Reject a dirty
   checkout, missing file or mismatched identity; do not stage from a moving
   branch, tag, unverified archive or a different commit. Stage a **new**
   absolute temporary directory with
   `python -B deploy/ma1759-diagnostic/stage.py <new-absolute-directory>`.
   Verify `MA1759_SOURCE_STAGING=PASS` and the resulting context contains
   exactly five Python modules, `Dockerfile`, `verify_image.py` and
   `manifest.json` (eight files). Before the Docker build, recheck the
   staged packaging inputs' pinned Git blobs and all five module SHA-256
   values against `manifest.json`; preserve the exact checkout SHA and
   manifest SHA-256 with the isolated build evidence. Abort on any
   post-staging alteration or extra context file.
3. On the isolated builder, resolve the supplied image reference and confirm
   Docker inspect `.Id` matches the fixed base SHA above **before** building.
   Use the staged Dockerfile and context with `--pull=false --network=none`;
   pass only the separately verified local image reference as `MA_BASE_IMAGE`.
   The build overwrites exactly five known Python site-packages modules, reads
   no `/data`, does not mount an account, and validates every installed
   file SHA-256 against the manifest. A matching source byte set is not
   equivalent to successful imports or genuine authenticated Apple data.
4. Capture the new immutable **resulting image ID**, base image ID,
   staged manifest SHA-256, build logs (non-sensitive), actual Python import
   checks and isolated tests. Preserve the exact input/source association.
   Validate Music Assistant startup, Apple provider registration, imports and
   intended native command against synthetic configuration only where an
   isolated sandbox is genuinely available and explicitly admitted.
5. Prepare a reviewed Synology Container Manager-compatible **one-writer**
   cutover and full `/data` rollback under the separately owned #2285
   lifecycle mechanism. The observed original is
   `music-assistant-1759-candidate` image `e681b6ae...`, **not** historical
   `music-assistant-server` image `d844e93f...`. No candidate with live
   `/data` may run in parallel.

## Remaining evidence and permission gates

- The actual runtime's installed five-module SHA-256 and comprehensive
  Docker create-fields have **not** been authenticated. Their equivalence to
  this source closure must be verified before any live swap.
- A private, preexisting MA client with `LIBRARY_READ` and `QUEUES_READ`
  and a fixed, authenticated candidate-only receiver remain unverified.
  Do not create a token, general API proxy, root receiver or SSH service
  through this source package.
- The native `music/ma1759_apple_first_page_shapes` command is **once per
  server process**, not once per installation or operator approval. It makes
  three first-page logical Apple GETs through the authenticated in-process
  client, may encounter internal HTTP retries and needs an independent
  receiver-wide hard deadline.
- Take matching before/after private queue and Group fingerprints; never
  publish raw IDs, songs, Apple URLs, credentials or application payloads.
  Do not issue playback/sync/Group/queue/volume mutations.
- #1755 Development installation HOLD, #47 playback ownership, #723
  CLOSED/NOT PLANNED and #2285 lifecycle ownership remain in force.

No part of this directory is a runnable NAS installer or a Production
transaction. Any build must remain inert until fresh source CI, independent
review, new image/read-scope/receiver admission, verified recovery and a
**separate explicit operator-approved maintenance window**. Source rollback:
close/revert this draft source-only change without touching Music Assistant.
