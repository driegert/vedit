#!/usr/bin/env bash
# Extract the fixture frames from the setup recording into tests/fixtures/frames/.
set -euo pipefail
video="${1:?usage: extract.sh <video>}"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/frames"
for t in $(python3 -c 'import json,sys; print(" ".join(sorted({c["t"] for c in json.load(open(sys.argv[1]))}, key=float)))' "$here/ground_truth.json"); do
  ffmpeg -y -v error -ss "$t" -i "$video" -frames:v 1 -update 1 "$here/frames/t$t.png"
done
ls -la "$here/frames"
