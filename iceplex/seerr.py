"""Seerr: search everything (not just the library) and request it.

Seerr searches The Movie Database (the same catalogue IMDb covers) and sends requests on to Sonarr / Radarr.
Ice signs in with the Plex account it already has, so there's no separate password, and requests show up in
Seerr under that user exactly as if made on Seerr's own website.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

import requests

IMG = "https://image.tmdb.org/t/p/w342"
BACKDROP = "https://image.tmdb.org/t/p/w1280"
FACE = "https://image.tmdb.org/t/p/w185"
LOGO = "https://image.tmdb.org/t/p/w154"
REGION = "US"

# TMDB ids of TV networks and movie studios (the same ones Seerr's own Discover page shows).
NETWORKS = [("Netflix", 213), ("HBO", 49), ("Max", 3186), ("Disney+", 2739), ("Prime Video", 1024),
            ("Apple TV+", 2552), ("Hulu", 453), ("Paramount+", 4330), ("Peacock", 3353), ("AMC", 174),
            ("FX", 88), ("Showtime", 67), ("Starz", 318), ("Paramount Network", 2076), ("NBC", 6),
            ("CBS", 16), ("ABC", 2), ("FOX", 19), ("History", 65), ("Discovery", 64)]
STUDIOS = [("A24", 41077), ("Marvel Studios", 420), ("Pixar", 3), ("Walt Disney Pictures", 2),
           ("Warner Bros.", 174), ("Universal", 33), ("Paramount", 4), ("Sony Pictures", 34),
           ("20th Century", 127928), ("DreamWorks", 521), ("Lucasfilm", 1), ("Legendary", 923),
           ("Blumhouse", 3172), ("Lionsgate", 1632)]
# Streaming services, matched by name against TMDB's list for the region (their ids come from there).
SERVICES = [("Netflix", ("netflix",)), ("Disney+", ("disney plus", "disney+")), ("Max", ("max", "hbo max")),
            ("Prime Video", ("amazon prime video",)), ("Apple TV+", ("apple tv plus", "apple tv+", "apple tv")),
            ("Hulu", ("hulu",)), ("Peacock", ("peacock premium", "peacock")),
            ("Paramount+", ("paramount plus", "paramount+", "paramount plus premium"))]

# Seerr's media status numbers.
UNKNOWN, PENDING, PROCESSING, PARTIAL, AVAILABLE, DELETED = 1, 2, 3, 4, 5, 6
RETRY_WAITS = (1.5, 3.0)   # a connection that is still coming up (WireGuard) gets a few seconds before giving up
STATUS_TEXT = {PENDING: "Requested", PROCESSING: "Requested", PARTIAL: "Partly available", AVAILABLE: "Available"}


class Results(list):
    """A page of results; .total is how many there are in all (when Seerr says)."""
    total = None


class SeerrError(Exception):
    pass


class Seerr:
    def __init__(self, base_url: str, plex_token: str):
        self.base = (base_url or "").strip().rstrip("/")
        if self.base and "://" not in self.base:
            self.base = "http://" + self.base
        self.token = plex_token
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        self.s.cookies.set_policy(__import__("http.cookiejar").cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self._signed_in = False
        self._lock = threading.Lock()
        # Seerr's sign-in cookie, sent by hand: Seerr can mark it HTTPS-only, and this runs over plain http
        # (WireGuard), so a normal cookie jar would never send it back.
        self.cookie: Optional[str] = None

    # -- plumbing -----------------------------------------------------------------

    def _login(self):
        self.cookie = None
        try:
            r = self._send("POST", f"{self.base}/api/v1/auth/plex", json={"authToken": self.token}, timeout=15)
        except requests.RequestException as e:
            raise SeerrError(f"Can't reach Seerr at {self.base}.") from e
        if r.status_code == 403:
            raise SeerrError("Seerr didn't let this Plex account in (does it have a Seerr user?).")
        if r.status_code >= 400:
            raise SeerrError(f"Seerr sign-in failed ({r.status_code}).")
        m = re.search(r"connect\.sid=[^;,\s]+", r.headers.get("Set-Cookie", ""))
        if not m:
            raise SeerrError("Seerr signed in but sent no session cookie.")
        self.cookie = m.group(0)
        self._signed_in = True

    def _send(self, method, url, **kw):
        """One request, tried again a couple of times over a few seconds if the connection fails."""
        for wait in RETRY_WAITS + (None,):
            try:
                return self.s.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if wait is None:
                    raise
                time.sleep(wait)

    def _call(self, method: str, path: str, **kw):
        if not self.base:
            raise SeerrError("Set your Seerr address in Settings first.")
        with self._lock:
            if not self._signed_in:
                self._login()
        for attempt in (1, 2):
            try:
                r = self._send(method, f"{self.base}/api/v1{path}", timeout=20,
                               headers={"Cookie": self.cookie or ""}, **kw)
            except requests.RequestException as e:
                raise SeerrError(f"Can't reach Seerr at {self.base}.") from e
            if r.status_code in (401, 403) and attempt == 1:
                with self._lock:
                    self._login()       # session expired: sign in again once
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("message")
                except Exception:
                    msg = None
                raise SeerrError(msg or f"Seerr said {r.status_code}.")
            return r.json() if r.content else {}
        raise SeerrError("Seerr sign-in keeps failing.")

    # -- what the app uses ------------------------------------------------------

    def test(self) -> str:
        """Sign in and return the Seerr user name (for the Settings 'Test' button)."""
        with self._lock:
            self._login()
        me = self._call("GET", "/auth/me")
        return me.get("displayName") or me.get("plexUsername") or me.get("email") or "OK"

    # -- turning Seerr's answers into plain dicts ---------------------------------

    @staticmethod
    def result(r: dict) -> Optional[dict]:
        """One movie or show from a list (search, discover, credits)."""
        kind = r.get("mediaType") or ("tv" if r.get("firstAirDate") is not None or r.get("name") and not r.get("title")
                                      else "movie")
        if kind not in ("movie", "tv"):
            return None
        date = r.get("releaseDate") if kind == "movie" else r.get("firstAirDate")
        mi = r.get("mediaInfo") or {}
        return {
            "id": r.get("id"),
            "kind": kind,
            "title": (r.get("title") or r.get("name")) if kind == "movie" else (r.get("name") or r.get("title")),
            "year": (date or "")[:4],
            "date": date or "",
            "overview": r.get("overview") or "",
            "poster": (IMG + r["posterPath"]) if r.get("posterPath") else None,
            "backdrop": (BACKDROP + r["backdropPath"]) if r.get("backdropPath") else None,
            "status": mi.get("status") or UNKNOWN,
            "rating_key": mi.get("ratingKey"),
            "vote": r.get("voteAverage"),
            "votes": r.get("voteCount") or 0,
            "popularity": r.get("popularity") or 0,
            "character": r.get("character"),
            "job": r.get("job"),
            "department": r.get("department"),
            "episodes": r.get("episodeCount"),
        }

    def _list(self, path: str, page: int = 1, **params) -> tuple[list[dict], int]:
        q = "&".join(f"{k}={requests.utils.quote(str(v), safe=',|')}" for k, v in params.items() if v not in (None, ""))
        sep = "&" if "?" in path else "?"
        data = self._call("GET", f"{path}{sep}page={page}" + (f"&{q}" if q else ""))
        out = Results(x for x in (self.result(r) for r in data.get("results") or []) if x)
        out.total = data.get("totalResults")
        return out, int(data.get("totalPages") or 1)

    # -- Discover -----------------------------------------------------------------

    def trending(self, page=1):
        return self._list("/discover/trending", page)

    def popular(self, kind: str, page=1, **filters):
        return self._list("/discover/movies" if kind == "movie" else "/discover/tv", page, **filters)

    def upcoming(self, kind: str, page=1):
        return self._list("/discover/movies/upcoming" if kind == "movie" else "/discover/tv/upcoming", page)

    def network(self, network_id: int, page=1):
        return self._list(f"/discover/tv/network/{network_id}", page)

    def studio(self, studio_id: int, page=1):
        return self._list(f"/discover/movies/studio/{studio_id}", page)

    def watchlist(self, page=1):
        data = self._call("GET", f"/discover/watchlist?page={page}")
        out = []
        for r in data.get("results") or []:
            kind = r.get("mediaType")
            if kind not in ("movie", "tv"):
                continue
            # Watchlist entries are short; fill them out from the title itself.
            try:
                d = self._call("GET", f"/{kind}/{r.get('tmdbId')}")
                d["mediaType"] = kind
                x = self.result(d)
            except SeerrError:
                x = None
            if x:
                out.append(x)
        return out, int(data.get("totalPages") or 1)

    def services(self, kind: str = "movie") -> list[dict]:
        """The big streaming services for the region: [{name, id, logo}]."""
        data = self._call("GET", f"/watchproviders/{'movies' if kind == 'movie' else 'tv'}?watchRegion={REGION}")
        rows = sorted(data if isinstance(data, list) else [], key=lambda p: p.get("displayPriority") or 999)
        out = []
        for label, names in SERVICES:
            hit = next((p for p in rows if (p.get("name") or "").strip().lower() in names), None)
            if hit:
                out.append({"name": label, "id": hit.get("id"),
                            "logo": (LOGO + hit["logoPath"]) if hit.get("logoPath") else None})
        return out

    def genres(self, kind: str) -> list[dict]:
        data = self._call("GET", f"/genres/{'movie' if kind == 'movie' else 'tv'}")
        return [{"id": g.get("id"), "name": g.get("name")} for g in (data if isinstance(data, list) else [])
                if g.get("id") and g.get("name")]

    # -- one title ----------------------------------------------------------------

    @staticmethod
    def _cert(kind: str, d: dict) -> str:
        if kind == "movie":
            for r in (d.get("releases") or {}).get("results") or []:
                if r.get("iso_3166_1") == REGION:
                    dates = sorted(r.get("release_dates") or [], key=lambda x: 0 if x.get("type") == 3 else 1)
                    for x in dates:
                        if x.get("certification"):
                            return x["certification"]
            return ""
        for r in (d.get("contentRatings") or {}).get("results") or []:
            if r.get("iso_3166_1") == REGION and r.get("rating"):
                return r["rating"]
        return ""

    @staticmethod
    def _imdb_id(d: dict) -> Optional[str]:
        return (d.get("externalIds") or {}).get("imdbId") or d.get("imdbId") or None

    def extra(self, kind: str, tmdb_id: int) -> dict:
        """What a poster needs beyond the list entry: IMDb id and age rating."""
        d = self._call("GET", f"/{kind}/{tmdb_id}")
        return {"imdb_id": self._imdb_id(d), "cert": self._cert(kind, d)}

    def title(self, kind: str, tmdb_id: int) -> dict:
        """Everything for a title page."""
        d = self._call("GET", f"/{kind}/{tmdb_id}")
        d["mediaType"] = kind
        info = self.result(d) or {"id": tmdb_id, "kind": kind}
        mi = d.get("mediaInfo") or {}
        credits = d.get("credits") or {}
        crew = credits.get("crew") or []

        def people(rows, n=None):
            seen, out = set(), []
            for p in rows:
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                out.append({"id": p.get("id"), "name": p.get("name") or "",
                            "role": p.get("character") or p.get("job") or "",
                            "photo": (FACE + p["profilePath"]) if p.get("profilePath") else None})
            return out[:n] if n else out

        trailer = None
        for v in d.get("relatedVideos") or []:
            if v.get("type") == "Trailer" and v.get("url"):
                trailer = v["url"]
                break
        providers = []
        for wp in d.get("watchProviders") or []:
            if wp.get("iso_3166_1") == REGION:
                providers = [{"id": p.get("id"), "name": p.get("name")} for p in wp.get("flatrate") or []]
        companies = d.get("productionCompanies") or []
        networks = d.get("networks") or []
        col = d.get("collection") or None
        info.update({
            "imdb_id": self._imdb_id(d),
            "cert": self._cert(kind, d),
            "tagline": d.get("tagline") or "",
            "runtime": d.get("runtime") or ((d.get("episodeRunTime") or [None])[0]),
            "genres": [g.get("name") for g in d.get("genres") or [] if g.get("name")],
            "cast": people(credits.get("cast") or [], 20),
            "directors": people([c for c in crew if c.get("job") == "Director"]),
            "writers": people([c for c in crew if c.get("department") == "Writing"], 4),
            "creators": people(d.get("createdBy") or []),
            "studio": {"id": companies[0].get("id"), "name": companies[0].get("name")} if companies else None,
            "network": {"id": networks[0].get("id"), "name": networks[0].get("name")} if networks else None,
            "providers": providers,
            "collection": {"id": col.get("id"), "name": col.get("name"),
                           "poster": (IMG + col["posterPath"]) if col.get("posterPath") else None} if col else None,
            "trailer": trailer,
            "seasons": [],
            "num_seasons": d.get("numberOfSeasons"),
        })
        if kind == "tv":
            have = {s.get("seasonNumber"): s.get("status") for s in mi.get("seasons") or []}
            for rq in mi.get("requests") or []:
                for s in rq.get("seasons") or []:
                    have.setdefault(s.get("seasonNumber"), PENDING)
            for s in d.get("seasons") or []:
                n = s.get("seasonNumber")
                if n is None or n == 0:
                    continue
                info["seasons"].append({"number": n, "name": s.get("name") or f"Season {n}",
                                        "episodes": s.get("episodeCount") or 0,
                                        "status": have.get(n) or UNKNOWN})
        return info

    def scores(self, kind: str, tmdb_id: int) -> dict:
        """Rotten Tomatoes (and IMDb, for movies) as Seerr reports them."""
        out = {}
        try:
            if kind == "movie":
                d = self._call("GET", f"/movie/{tmdb_id}/ratingscombined")
                rt = d.get("rt") or {}
                if (d.get("imdb") or {}).get("criticsScore"):
                    out["imdb"] = float(d["imdb"]["criticsScore"])
            else:
                rt = self._call("GET", f"/tv/{tmdb_id}/ratings") or {}
            if rt.get("criticsScore") is not None:
                out["rt"] = int(rt["criticsScore"])
        except (SeerrError, ValueError, TypeError):
            pass
        return out

    def recommendations(self, kind: str, tmdb_id: int):
        out, _ = self._list(f"/{kind}/{tmdb_id}/recommendations", 1)
        if not out:
            out, _ = self._list(f"/{kind}/{tmdb_id}/similar", 1)
        return out

    def collection(self, collection_id: int) -> dict:
        d = self._call("GET", f"/collection/{collection_id}")
        parts = []
        for p in d.get("parts") or []:
            p.setdefault("mediaType", "movie")
            x = self.result(p)
            if x:
                parts.append(x)
        parts.sort(key=lambda x: x.get("date") or "9999")
        return {"name": d.get("name") or "Collection", "overview": d.get("overview") or "",
                "backdrop": (BACKDROP + d["backdropPath"]) if d.get("backdropPath") else None, "parts": parts}

    # -- your requests ----------------------------------------------------------------

    def my_requests(self, take: int = 100) -> list[dict]:
        """Requests, newest first, each with the title filled in: [{request fields + title/poster/...}]."""
        data = self._call("GET", f"/request?take={take}&skip=0&filter=all&sort=added&sortDirection=desc")
        out = []
        for rq in data.get("results") or []:
            media = rq.get("media") or {}
            kind = media.get("mediaType") or rq.get("type")
            tid = media.get("tmdbId")
            if kind not in ("movie", "tv") or not tid:
                continue
            out.append({"request_id": rq.get("id"), "req_status": rq.get("status"), "kind": kind, "id": tid,
                        "status": media.get("status") or UNKNOWN, "rating_key": media.get("ratingKey"),
                        "requested_at": (rq.get("createdAt") or "")[:10],
                        "seasons_requested": sorted(s.get("seasonNumber") for s in rq.get("seasons") or []
                                                    if s.get("seasonNumber") is not None),
                        "media_seasons": {s.get("seasonNumber"): s.get("status") for s in media.get("seasons") or []}})
        return out

    def fill(self, r: dict) -> dict:
        """Title, poster, dates (and seasons with air dates for shows) for a request row."""
        d = self._call("GET", f"/{r['kind']}/{r['id']}")
        d["mediaType"] = r["kind"]
        x = self.result(d) or {}
        out = dict(r, title=x.get("title"), year=x.get("year"), date=x.get("date"), poster=x.get("poster"),
                   imdb_id=self._imdb_id(d), cert=self._cert(r["kind"], d))
        if r["kind"] == "tv":
            out["seasons"] = [{"number": s.get("seasonNumber"), "name": s.get("name"), "episodes": s.get("episodeCount") or 0,
                               "air_date": s.get("airDate") or ""}
                              for s in d.get("seasons") or [] if s.get("seasonNumber")]
            nxt = d.get("nextEpisodeToAir") or {}
            out["next_air"] = nxt.get("airDate") or ""
            out["next_code"] = (f"S{nxt.get('seasonNumber')} E{nxt.get('episodeNumber')}"
                                if nxt.get("seasonNumber") is not None else "")
        return out

    def schedule(self, tmdb_id: int) -> dict:
        """A show's release schedule: next episode (code, name, date), whether it has ended."""
        d = self._call("GET", f"/tv/{tmdb_id}")
        out = {"status": d.get("status") or "", "next": None, "last_date": ""}
        n = d.get("nextEpisodeToAir") or {}
        if n.get("seasonNumber") is not None:
            out["next"] = {"code": f"S{n.get('seasonNumber')} E{n.get('episodeNumber')}", "name": n.get("name") or "",
                           "date": n.get("airDate") or ""}
        last = (d.get("lastEpisodeToAir") or {}).get("airDate") or ""
        if last:
            import datetime
            try:
                x = datetime.date.fromisoformat(last[:10])
                out["last_date"] = f"{x.strftime('%b')} {x.day}, {x.year}"
            except ValueError:
                pass
        return out

    def season_episodes(self, tmdb_id: int, season: int) -> list[dict]:
        d = self._call("GET", f"/tv/{tmdb_id}/season/{season}")
        return [{"number": e.get("episodeNumber"), "name": e.get("name") or "", "air_date": e.get("airDate") or ""}
                for e in d.get("episodes") or [] if e.get("episodeNumber") is not None]

    # -- people ---------------------------------------------------------------------

    def person(self, person_id: int) -> dict:
        d = self._call("GET", f"/person/{person_id}")
        c = self._call("GET", f"/person/{person_id}/combined_credits")

        def rows(lst):
            out = [x for x in (self.result(r) for r in lst or []) if x and x.get("id")]
            return out
        cast = rows(c.get("cast"))
        crew = rows(c.get("crew"))
        # Talk shows and award shows clutter an actor's list; Plex and IMDb rank them low too.
        known_pool = cast if (d.get("knownForDepartment") or "Acting") == "Acting" else crew
        seen, known = set(), []
        for x in sorted(known_pool, key=lambda x: -(x["votes"] or 0)):
            guest_spot = known_pool is cast and x["kind"] == "tv" and (x.get("episodes") or 99) < 3
            if (x["kind"], x["id"]) in seen or guest_spot:
                continue
            seen.add((x["kind"], x["id"]))
            known.append(x)
            if len(known) >= 30:
                break
        groups: dict[str, list] = {}
        if cast:
            groups["Acting"] = cast
        for x in crew:
            dep = {"Directing": "Directing", "Writing": "Writing", "Production": "Producing"}.get(
                x.get("department") or "", "Crew")
            groups.setdefault(dep, []).append(x)
        for k, lst in groups.items():
            merged: dict = {}
            for x in lst:   # one row per title (several jobs on one film become one line)
                key = (x["kind"], x["id"])
                if key in merged:
                    role = x.get("character") or x.get("job")
                    m = merged[key]
                    if role and role not in (m.get("role") or ""):
                        m["role"] = f"{m['role']}, {role}" if m.get("role") else role
                else:
                    merged[key] = dict(x, role=x.get("character") or x.get("job") or "")
            groups[k] = sorted(merged.values(), key=lambda x: x.get("date") or "0000", reverse=True)
        order = ["Acting", "Directing", "Writing", "Producing", "Crew"]
        if (d.get("knownForDepartment") or "Acting") != "Acting":
            order = ["Directing", "Writing", "Producing", "Acting", "Crew"]
        return {
            "id": person_id, "name": d.get("name") or "", "bio": d.get("biography") or "",
            "birthday": d.get("birthday") or "", "deathday": d.get("deathday") or "",
            "place": d.get("placeOfBirth") or "", "dept": d.get("knownForDepartment") or "",
            "photo": (FACE.replace("w185", "h632") + d["profilePath"]) if d.get("profilePath") else None,
            "known": known, "groups": [(k, groups[k]) for k in order if groups.get(k)],
        }

    def find_person(self, name: str) -> Optional[int]:
        """TMDB id of a person by name (for cast from Plex, which doesn't give one)."""
        q = requests.utils.quote(name, safe="")
        data = self._call("GET", f"/search?query={q}&page=1")
        for r in data.get("results") or []:
            if r.get("mediaType") == "person" and (r.get("name") or "").lower() == name.lower():
                return r.get("id")
        for r in data.get("results") or []:
            if r.get("mediaType") == "person":
                return r.get("id")
        return None

    def search(self, query: str, page: int = 1) -> list[dict]:
        # Seerr wants the query encoded with %20 rather than '+'.
        q = requests.utils.quote(query, safe="")
        data = self._call("GET", f"/search?query={q}&page={page}")
        return [x for x in (self.result(r) for r in data.get("results") or [] if r.get("mediaType") in ("movie", "tv"))
                if x]

    def search_people(self, query: str) -> list[dict]:
        q = requests.utils.quote(query, safe="")
        data = self._call("GET", f"/search?query={q}&page=1")
        return [{"id": r.get("id"), "name": r.get("name") or "", "dept": r.get("knownForDepartment") or "",
                 "photo": (FACE + r["profilePath"]) if r.get("profilePath") else None}
                for r in data.get("results") or [] if r.get("mediaType") == "person"]

    def details(self, kind: str, tmdb_id: int) -> dict:
        """Full info: for shows, the seasons and which are already there / requested."""
        d = self._call("GET", f"/{kind}/{tmdb_id}")
        mi = d.get("mediaInfo") or {}
        info = {
            "status": mi.get("status") or UNKNOWN,
            "overview": d.get("overview") or "",
            "genres": [g.get("name") for g in d.get("genres") or [] if g.get("name")],
            "runtime": d.get("runtime"),
            "imdb_id": (d.get("externalIds") or {}).get("imdbId") or d.get("imdbId"),
            "seasons": [],
        }
        if kind == "tv":
            have = {s.get("seasonNumber"): s.get("status") for s in mi.get("seasons") or []}
            for rq in mi.get("requests") or []:
                for s in rq.get("seasons") or []:
                    have.setdefault(s.get("seasonNumber"), PENDING)
            for s in d.get("seasons") or []:
                n = s.get("seasonNumber")
                if n is None or n == 0:      # skip "Specials"
                    continue
                info["seasons"].append({"number": n, "name": s.get("name") or f"Season {n}",
                                        "episodes": s.get("episodeCount") or 0,
                                        "status": have.get(n) or UNKNOWN})
        return info

    def imdb_rating(self, kind: str, tmdb_id: int) -> Optional[float]:
        """IMDb score (movies; Seerr doesn't offer it for shows)."""
        if kind != "movie":
            return None
        try:
            d = self._call("GET", f"/movie/{tmdb_id}/ratingscombined")
        except SeerrError:
            return None
        score = (d.get("imdb") or {}).get("criticsScore")
        return float(score) if score else None

    def request(self, kind: str, tmdb_id: int, seasons: Optional[list[int]] = None):
        body = {"mediaType": kind, "mediaId": tmdb_id}
        if kind == "tv":
            body["seasons"] = seasons if seasons else "all"
        return self._call("POST", "/request", json=body)
