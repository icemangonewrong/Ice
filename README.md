# IcePlex for Windows

A Plex player for Windows with true direct play (MKV and everything else, no server transcoding),
Continue Watching, library hiding, info pages, progress sync, and a disk buffer that rides out
dropped connections.

## Build
Push to GitHub. The **Build IcePlex for Windows** workflow produces an artifact with:
- `IcePlex-Setup.exe`: installer (per-user, no admin needed)
- `IcePlex-Windows.zip`: portable folder; extract and run `IcePlex.exe`

## Player keys
| Key | Action |
|---|---|
| Space / K | Play / pause |
| ← / J, → / L | Back / forward 10 seconds |
| ↑ / ↓, mouse wheel | Volume |
| M | Mute |
| F / double-click | Full screen |
| Esc | Leave full screen / close player |

Settings live in `%APPDATA%\IcePlex\settings.json`.
