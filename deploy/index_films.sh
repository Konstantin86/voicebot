#!/usr/bin/env bash
# Rebuilds the bot's film list from the TV's disk while it is plugged into the Pi.
# Usage: sudo deploy/index_films.sh [mount point, default /mnt/films]
# Mount the disk read-only first: sudo mount -t ntfs3 -o ro /dev/sda2 /mnt/films
set -euo pipefail
ROOT="${1:-/mnt/films}"
OUT=/var/lib/voicebot/films.tsv
[ -d "$ROOT" ] && [ -n "$(ls -A "$ROOT")" ] || { echo "Nothing mounted at $ROOT" >&2; exit 1; }
TMP=$(mktemp)
(cd "$ROOT" && find . -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.m4v' -o -iname '*.ts' \) \
  -not -path './$RECYCLE.BIN/*' -not -path './LG Smart TV/*' -printf '%P\t%s\n' | sort) > "$TMP"
install -o voicebot -g voicebot -m 0644 "$TMP" "$OUT"
rm -f "$TMP"
echo "$(wc -l < "$OUT") videos saved to $OUT"
