"""Talks to plex.tv and to your Plex Media Server."""
from __future__ import annotations

import urllib.parse
import uuid
from typing import Callable, Optional

import requests

PRODUCT = "Ice"
VERSION = "1.0"


class PlexError(Exception):
    pass


# ---------------------------------------------------------------------------
# Item model
# ---------------------------------------------------------------------------

class Item:
    """One Plex item (movie, show, season or episode). Wraps the JSON dict."""

    def __init__(self, d: dict):
        self.d = d or {}

    def __eq__(self, other):
        return isinstance(other, Item) and other.key == self.key

    def __hash__(self):
        return hash(self.key)

    def get(self, k, default=None):
        return self.d.get(k, default)

    @property
    def key(self) -> str:
        return str(self.d.get("ratingKey", ""))

    @property
    def type(self) -> str:
        return self.d.get("type", "")

    @property
    def title(self) -> str:
        return self.d.get("title") or ""

    @property
    def is_playable(self) -> bool:
        return self.type in ("movie", "episode")

    @property
    def display_title(self) -> str:
        if self.type == "episode" and self.d.get("grandparentTitle"):
            return self.d["grandparentTitle"]
        return self.title or "Untitled"

    @property
    def episode_code(self) -> str:
        parts = []
        if self.d.get("parentIndex") is not None:
            parts.append(f"S{self.d['parentIndex']}")
        if self.d.get("index") is not None:
            parts.append(f"E{self.d['index']}")
        return " · ".join(parts)

    @property
    def display_subtitle(self) -> str:
        t = self.type
        if t == "episode":
            parts = [self.episode_code] if self.episode_code else []
            if self.title:
                parts.append(self.title)
            return " · ".join(parts)
        if t == "season":
            return self.d.get("parentTitle") or ""
        if self.d.get("year"):
            return str(self.d["year"])
        return ""

    @property
    def poster_path(self) -> Optional[str]:
        d = self.d
        if self.type == "episode":
            return d.get("grandparentThumb") or d.get("parentThumb") or d.get("thumb")
        return d.get("thumb") or d.get("parentThumb")

    @property
    def duration_ms(self) -> int:
        return int(self.d.get("duration") or 0)

    @property
    def offset_ms(self) -> int:
        return int(self.d.get("viewOffset") or 0)

    @property
    def progress(self) -> Optional[float]:
        off, dur = self.offset_ms, self.duration_ms
        if dur <= 0 or off <= 0:
            return None
        return max(0.0, min(off / dur, 1.0))

    @property
    def is_watched(self) -> bool:
        leaf, viewed = self.d.get("leafCount"), self.d.get("viewedLeafCount")
        if leaf is not None and viewed is not None:
            return leaf > 0 and viewed >= leaf
        return (self.d.get("viewCount") or 0) > 0

    @property
    def show_check(self) -> bool:
        """Checkmark only for finished items (a rewatch in progress shows the bar instead)."""
        return self.is_watched and self.progress is None

    @property
    def media(self) -> dict:
        m = self.d.get("Media") or []
        return m[0] if m else {}

    @property
    def part(self) -> dict:
        p = self.media.get("Part") or []
        return p[0] if p else {}

    @property
    def section_id(self) -> str:
        v = self.d.get("librarySectionID")
        return "" if v is None else str(v)

    def markers(self) -> list[dict]:
        """Intro / credits markers as [{'type': 'intro', 'start': secs, 'end': secs}, ...]."""
        out = []
        for m in self.d.get("Marker") or []:
            t = m.get("type")
            if t in ("intro", "credits") and m.get("startTimeOffset") is not None:
                out.append({"type": t, "start": m["startTimeOffset"] / 1000,
                            "end": (m.get("endTimeOffset") or m["startTimeOffset"]) / 1000,
                            "final": bool(m.get("final"))})
        return out

    def external_subtitles(self) -> list[dict]:
        """Subtitle files Plex has next to the video (.srt etc.), as opposed to ones inside it."""
        out = []
        for s in self.part.get("Stream") or []:
            if s.get("streamType") == 3 and s.get("key"):
                out.append({"key": s["key"], "lang": s.get("languageCode") or s.get("language") or "",
                            "title": s.get("displayTitle") or s.get("title") or s.get("language") or "External",
                            "forced": bool(s.get("forced"))})
        return out

    def tags(self, name: str) -> list[str]:
        return [t.get("tag", "") for t in (self.d.get(name) or []) if t.get("tag")]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PlexClient:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.token: Optional[str] = None
        self.server: Optional[dict] = None   # name, clientIdentifier, accessToken, baseURL, allURIs
        self.on_server_changed: Optional[Callable[[dict], None]] = None
        self.session = requests.Session()

    # -- headers ------------------------------------------------------------

    def headers(self, token: Optional[str] = None) -> dict:
        h = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": VERSION,
            "X-Plex-Platform": "Windows",
            "X-Plex-Device": "PC",
            "X-Plex-Device-Name": "Ice (Windows)",
        }
        if token:
            h["X-Plex-Token"] = token
        return h

    def _req(self, method: str, url: str, token: Optional[str] = None, params=None, timeout=15):
        r = self.session.request(method, url, headers=self.headers(token), params=params, timeout=timeout)
        if not (200 <= r.status_code < 300):
            raise PlexError(f"Plex returned an error ({r.status_code}).")
        return r

    # -- sign in (PIN flow) ---------------------------------------------------

    def create_pin(self) -> dict:
        return self._req("POST", "https://plex.tv/api/v2/pins", params={"strong": "true"}).json()

    def check_pin(self, pin_id: int) -> dict:
        return self._req("GET", f"https://plex.tv/api/v2/pins/{pin_id}").json()

    def auth_url(self, pin: dict) -> str:
        q = urllib.parse.urlencode({
            "clientID": self.client_id,
            "code": pin["code"],
            "context[device][product]": PRODUCT,
        })
        return "https://app.plex.tv/auth#?" + q

    # -- servers --------------------------------------------------------------

    def servers(self) -> list[dict]:
        if not self.token:
            raise PlexError("Not signed in to Plex.")
        r = self._req("GET", "https://clients.plex.tv/api/v2/resources", token=self.token,
                      params={"includeHttps": 1, "includeRelay": 1})
        return [s for s in r.json() if "server" in (s.get("provides") or "")]

    def _first_reachable(self, uris: list[str], token: str) -> Optional[str]:
        for uri in uris:
            try:
                self._req("GET", uri + "/identity", token=token, timeout=5)
                return uri
            except Exception:
                continue
        return None

    def connect(self, resource: dict) -> dict:
        token = resource.get("accessToken") or self.token or ""

        def rank(c):
            if c.get("relay"):
                return 2
            return 0 if c.get("local") else 1

        conns = sorted(resource.get("connections") or [], key=rank)
        uris = [c["uri"] for c in conns if c.get("uri")]
        working = self._first_reachable(uris, token)
        if not working:
            raise PlexError("Couldn't reach the Plex server on any address.")
        return {
            "name": resource.get("name", "Plex"),
            "clientIdentifier": resource.get("clientIdentifier", ""),
            "accessToken": token,
            "baseURL": working,
            "allURIs": uris,
        }

    # -- server requests (fail over between addresses) -------------------------

    def get(self, path: str, params=None, timeout=15) -> dict:
        s = self.server
        if not s:
            raise PlexError("No Plex server selected.")
        try:
            return self._req("GET", s["baseURL"] + path, token=s["accessToken"],
                             params=params, timeout=timeout).json()
        except requests.RequestException:
            others = [u for u in s.get("allURIs", []) if u != s["baseURL"]]
            working = self._first_reachable(others, s["accessToken"])
            if not working:
                raise
            s = dict(s, baseURL=working)
            self.server = s
            if self.on_server_changed:
                self.on_server_changed(s)
            return self._req("GET", working + path, token=s["accessToken"],
                             params=params, timeout=timeout).json()

    def _fire(self, path: str, params=None):
        """Fire-and-forget request (progress reports etc.)."""
        try:
            s = self.server
            if s:
                self._req("GET", s["baseURL"] + path, token=s["accessToken"], params=params, timeout=10)
        except Exception:
            pass

    @staticmethod
    def _items(data: dict) -> list[Item]:
        return [Item(m) for m in (data.get("MediaContainer", {}).get("Metadata") or [])]

    @staticmethod
    def _hubs(data: dict) -> list[dict]:
        return data.get("MediaContainer", {}).get("Hub") or []

    # -- library -----------------------------------------------------------------

    def sections(self) -> list[dict]:
        dirs = self.get("/library/sections").get("MediaContainer", {}).get("Directory") or []
        return [d for d in dirs if d.get("type") in ("movie", "show")]

    def continue_watching(self) -> list[Item]:
        try:
            hubs = self._hubs(self.get("/hubs/home/continueWatching"))
            items = [Item(m) for h in hubs for m in (h.get("Metadata") or [])]
            if items:
                return items
        except Exception:
            pass
        try:
            hubs = self._hubs(self.get("/hubs", params={"count": 30}))
            match = next((h for h in hubs if "continue" in (h.get("hubIdentifier") or "")), None) \
                or next((h for h in hubs if "ondeck" in (h.get("hubIdentifier") or "")), None)
            if match and match.get("Metadata"):
                return [Item(m) for m in match["Metadata"]]
        except Exception:
            pass
        return self._items(self.get("/library/onDeck"))

    def recently_added(self, section: str, count: int = 30) -> list[Item]:
        return self._items(self.get(f"/library/sections/{section}/recentlyAdded", params={
            "X-Plex-Container-Start": 0, "X-Plex-Container-Size": count}))

    def items(self, section: str, sort: str, unwatched: bool, start: int, size: int):
        params = {"sort": sort, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size}
        if unwatched:
            params["unwatched"] = 1
        mc = self.get(f"/library/sections/{section}/all", params=params).get("MediaContainer", {})
        return [Item(m) for m in (mc.get("Metadata") or [])], mc.get("totalSize")

    def children(self, key: str) -> list[Item]:
        return self._items(self.get(f"/library/metadata/{key}/children"))

    def metadata(self, key: str) -> Optional[Item]:
        items = self._items(self.get(f"/library/metadata/{key}"))
        return items[0] if items else None

    def next_episode(self, show_key: str) -> Optional[Item]:
        eps = self._items(self.get(f"/library/metadata/{show_key}/allLeaves"))
        return (next((e for e in eps if e.offset_ms > 0), None)
                or next((e for e in eps if not (e.get("viewCount") or 0)), None)
                or (eps[0] if eps else None))

    def episode_after(self, item: Item) -> Optional[Item]:
        show = item.get("grandparentRatingKey")
        if item.type != "episode" or not show:
            return None
        eps = self._items(self.get(f"/library/metadata/{show}/allLeaves"))
        for i, e in enumerate(eps):
            if e.key == item.key:
                return eps[i + 1] if i + 1 < len(eps) else None
        return None

    def episode_before(self, item: Item) -> Optional[Item]:
        show = item.get("grandparentRatingKey")
        if item.type != "episode" or not show:
            return None
        eps = self._items(self.get(f"/library/metadata/{show}/allLeaves"))
        for i, e in enumerate(eps):
            if e.key == item.key:
                return eps[i - 1] if i > 0 else None
        return None

    def details(self, key: str) -> Optional[Item]:
        """Full metadata including intro/credits markers and every subtitle stream."""
        items = self._items(self.get(f"/library/metadata/{key}", params={"includeMarkers": 1}))
        return items[0] if items else None

    def stream_url(self, key: str) -> Optional[str]:
        s = self.server
        if not s or not key:
            return None
        return s["baseURL"] + key + "?" + urllib.parse.urlencode({"X-Plex-Token": s["accessToken"]})

    def search(self, text: str) -> list[Item]:
        hubs = self._hubs(self.get("/hubs/search", params={"query": text, "limit": 20}))
        return [Item(m) for h in hubs if h.get("type") in ("movie", "show", "episode")
                for m in (h.get("Metadata") or [])]

    # -- watch state -------------------------------------------------------------

    def report_timeline(self, item: Item, state: str, time_ms: int, duration_ms: int):
        self._fire("/:/timeline", {
            "ratingKey": item.key, "key": f"/library/metadata/{item.key}",
            "state": state, "time": int(time_ms), "duration": int(duration_ms)})

    def mark_watched(self, key: str):
        self._fire("/:/scrobble", {"identifier": "com.plexapp.plugins.library", "key": key})

    def mark_unwatched(self, key: str):
        self._fire("/:/unscrobble", {"identifier": "com.plexapp.plugins.library", "key": key})

    # -- URLs ---------------------------------------------------------------------

    def image_url(self, path: Optional[str], width=300, height=450) -> Optional[str]:
        s = self.server
        if not path or not s:
            return None
        q = urllib.parse.urlencode({
            "width": width, "height": height, "minSize": 1, "upscale": 1,
            "url": path, "X-Plex-Token": s["accessToken"]})
        return s["baseURL"] + "/photo/:/transcode?" + q

    def direct_url(self, item: Item) -> Optional[str]:
        """Original file, no server-side conversion (true direct play)."""
        s = self.server
        key = item.part.get("key")
        if not s or not key:
            return None
        q = urllib.parse.urlencode({
            "X-Plex-Token": s["accessToken"],
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Product": PRODUCT})
        return s["baseURL"] + key + "?" + q


def new_client_id() -> str:
    return str(uuid.uuid4())
