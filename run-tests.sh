#!/usr/bin/env bash
# Run the vedit test suite. Renders real video, so it takes a minute or two.
set -euo pipefail
cd "$(dirname "$0")"

for tool in ffmpeg ffprobe uv; do
    command -v "$tool" >/dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done

# auto-editor downloads its native binary on first use, so the first run needs network.
exec uv run pytest "$@"
