# #1759: SOURCE-ONLY candidate diagnostic overlay. Never build on the live NAS.
# The builder must first verify MA_BASE_IMAGE resolves to Docker .Id
# sha256:e681b6aeedb25e639860b8c1e687578377277c2930908e3fa4cc7605501335ed.
# Docker image IDs are not registry manifest digests. Do not invent @sha256 refs.
ARG MA_BASE_IMAGE
FROM ${MA_BASE_IMAGE}

COPY music_assistant/controllers/music/controller.py /app/venv/lib/python3.14/site-packages/music_assistant/controllers/music/controller.py
COPY music_assistant/controllers/music/media/tracks.py /app/venv/lib/python3.14/site-packages/music_assistant/controllers/music/media/tracks.py
COPY music_assistant/providers/apple_music/provider.py /app/venv/lib/python3.14/site-packages/music_assistant/providers/apple_music/provider.py
COPY music_assistant/providers/apple_music/recommendations.py /app/venv/lib/python3.14/site-packages/music_assistant/providers/apple_music/recommendations.py
COPY music_assistant/providers/apple_music/readonly_diagnostic_1759.py /app/venv/lib/python3.14/site-packages/music_assistant/providers/apple_music/readonly_diagnostic_1759.py

COPY manifest.json /tmp/ma1759_diag_manifest.json
COPY verify_image.py /tmp/ma1759_verify_image.py
RUN /app/venv/bin/python -I -B /tmp/ma1759_verify_image.py && \
    rm /tmp/ma1759_diag_manifest.json /tmp/ma1759_verify_image.py

LABEL org.opencontainers.image.title="MA1759 candidate-only diagnostic overlay" \
      org.opencontainers.image.revision="4b2332d0a16f4c10aacf406170c2d5a09dc17dac" \
      org.opencontainers.image.description="Source-only five-module diagnostic candidate; no NAS installation or Production authority"
