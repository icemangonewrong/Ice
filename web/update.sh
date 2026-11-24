#!/bin/sh
# Fetch the latest Ice web files from GitHub into this folder.
cd "$(dirname "$0")" || exit 1
for f in index.html app.js style.css update.sh; do
  curl -fsSL -H "Cache-Control: no-cache" "https://raw.githubusercontent.com/icemangonewrong/Ice/main/web/$f?t=$(date +%s)" -o "$f.new" && mv "$f.new" "$f" || { echo "failed: $f"; exit 1; }
done
chmod +x update.sh
echo "Ice web updated: $(grep -o 'WEB_VERSION = [0-9]*' app.js) $(date)"
