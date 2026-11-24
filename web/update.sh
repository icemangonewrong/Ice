#!/bin/sh
# Fetch the latest Ice web files from GitHub into this folder.
cd "$(dirname "$0")" || exit 1
for f in index.html app.js style.css; do
  curl -fsSL "https://raw.githubusercontent.com/icemangonewrong/Ice/main/web/$f" -o "$f.new" && mv "$f.new" "$f" || { echo "failed: $f"; exit 1; }
done
echo "Ice web updated: $(date)"
