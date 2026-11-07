"""Seerr: search everything (not just the library) and request it.

Seerr searches The Movie Database (the same catalogue IMDb covers) and sends requests on to Sonarr / Radarr.
Ice signs in with the Plex account it already has, so there's no separate password, and requests show up in
Seerr under that user exactly as if made on Seerr's own website.
"""
from __future__ import annotations

import re
import threading
from typing import Optional

import requests

IMG = "https://image.tmdb.org/t/p/w342"

# Seerr's media status numbers.
UNKNOWN, PENDING, PROCESSING, PARTIAL, AVAILABLE, DELETED = 1, 2, 3, 4, 5, 6
STATUS_TEXT = {PENDING: "Requested", PROCESSING: "Requested", PARTIAL: "Partly available", AVAILABLE: "Available"}


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
            r = self.s.post(f"{self.base}/api/v1/auth/plex", json={"authToken": self.token}, timeout=15)
        except requests.RequestException as e:
            raise SeerrError(f"Can't reach Seerr at {self.base} (is WireGuard on?)") from e
        if r.status_code == 403:
            raise SeerrError("Seerr didn't let this Plex account in (does it have a Seerr user?).")
        if r.status_code >= 400:
            raise SeerrError(f"Seerr sign-in failed ({r.status_code}).")
        m = re.search(r"connect\.sid=[^;,\s]+", r.headers.get("Set-Cookie", ""))
        if not m:
            raise SeerrError("Seerr signed in but sent no session cookie.")
        self.cookie = m.group(0)
        self._signed_in = True

    def _call(self, method: str, path: str, **kw):
        if not self.base:
            raise SeerrError("Set your Seerr address in Settings first.")
        with self._lock:
            if not self._signed_in:
                self._login()
        for attempt in (1, 2):
            try:
                r = self.s.request(method, f"{self.base}/api/v1{path}", timeout=20,
                                   headers={"Cookie": self.cookie or ""}, **kw)
            except requests.RequestException as e:
                raise SeerrError(f"Can't reach Seerr at {self.base} (is WireGuard on?)") from e
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

    def search(self, query: str, page: int = 1) -> list[dict]:
        # Seerr wants the query encoded with %20 rather than '+'.
        q = requests.utils.quote(query, safe="")
        data = self._call("GET", f"/search?query={q}&page={page}")
        out = []
        for r in data.get("results") or []:
            kind = r.get("mediaType")
            if kind not in ("movie", "tv"):
                continue
            date = r.get("releaseDate") if kind == "movie" else r.get("firstAirDate")
            mi = r.get("mediaInfo") or {}
            out.append({
                "id": r.get("id"),
                "kind": kind,
                "title": r.get("title") if kind == "movie" else r.get("name"),
                "year": (date or "")[:4],
                "overview": r.get("overview") or "",
                "poster": (IMG + r["posterPath"]) if r.get("posterPath") else None,
                "status": mi.get("status") or UNKNOWN,
                "vote": r.get("voteAverage"),
            })
        return out

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
