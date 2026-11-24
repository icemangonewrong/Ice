# Ice (web)

A small web version of Ice for in-car browsers (CarTV on CarPlay) and any other browser.
Plex only: Continue Watching, Recently Added, libraries, search, show/season pages with air dates,
and a player with 10-second skips, English audio, subtitles off, progress reported to Plex.

There's nothing secret in here: you sign in with your own Plex account and the page talks to your
own server. The page is served from the Oracle box on the WireGuard address only (10.66.66.1:8099),
so it isn't reachable from the internet.

Update on the server: `~/ice-web/update.sh`
