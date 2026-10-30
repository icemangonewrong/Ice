# Ice for Windows

A Plex player for Windows with true direct play (MKV and everything else, no server transcoding),
Continue Watching, library hiding, info pages, progress sync, and a disk buffer that rides out
dropped connections.

## Build
Push to GitHub. The **Build Ice for Windows** workflow publishes a GitHub Release with:
- `Ice-Setup.exe`: installer (per-user, no admin needed)
- `Ice-Windows.zip`: portable folder; extract and run `Ice.exe`

## Player keys
| Key | Action |
|---|---|
| Space / K | Play / pause |
| ← / J, → / L | Back / forward 10 seconds |
| ↑ / ↓, mouse wheel | Volume |
| M | Mute |
| F / double-click | Full screen |
| Esc | Pause and minimize to the mini player |

Settings live in `%APPDATA%\Ice\settings.json`.
