"""Matching the monitor's refresh rate to the video (Windows), the way Plex / Jellyfin desktop players do it.

A 24 fps film on a 60 Hz screen can't be shown evenly (60 / 24 = 2.5), so the player has to show frames for
uneven lengths and now and then drop one. If the monitor offers a rate that's an exact multiple of the video's
frame rate (24, 48, 72, 120, 144 Hz for film), switching to it in full screen makes every frame last the same
time: no judder, no drops. The change is temporary (Windows puts the old mode back if Ice closes) and is
undone when leaving full screen or stopping the video.
"""
from __future__ import annotations

import sys
from typing import Optional

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DEVMODEW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmPositionX", wintypes.LONG),
            ("dmPositionY", wintypes.LONG),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]

    _user32 = ctypes.windll.user32
    _ENUM_CURRENT_SETTINGS = -1
    _DM_PELSWIDTH, _DM_PELSHEIGHT, _DM_DISPLAYFREQUENCY = 0x80000, 0x100000, 0x400000
    _CDS_FULLSCREEN = 0x4
    _DISP_CHANGE_SUCCESSFUL = 0


def _mode(device: str, index: int):
    dm = _DEVMODEW()
    dm.dmSize = ctypes.sizeof(_DEVMODEW)
    if not _user32.EnumDisplaySettingsW(device, index, ctypes.byref(dm)):
        return None
    return dm


def current(device: str) -> Optional[tuple[int, int, int]]:
    """(width, height, Hz) the display is running at now."""
    if sys.platform != "win32":
        return None
    dm = _mode(device, _ENUM_CURRENT_SETTINGS)
    return (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency) if dm else None


def rates(device: str, width: int, height: int) -> list[int]:
    """Refresh rates the display offers at this resolution."""
    out, i = set(), 0
    while True:
        dm = _mode(device, i)
        if dm is None:
            break
        if dm.dmPelsWidth == width and dm.dmPelsHeight == height and dm.dmDisplayFrequency > 1:
            out.add(int(dm.dmDisplayFrequency))
        i += 1
    return sorted(out)


def _real_hz(hz: int) -> float:
    # Windows reports 59.94 / 23.976 / 29.97 Hz modes as 59 / 23 / 29 (same fudge as Jellyfin's player).
    return (hz + 1) / 1.001 if hz in (23, 29, 47, 59, 71, 119, 143) else float(hz)


def is_multiple(hz: float, fps: float, tol: float = 0.001) -> bool:
    """Does a frame at this fps last a whole number of refreshes? `tol` is how far off is fine: with the
    Resample sync mode the player evens out up to ~1% itself (so 23.976 fps on 24 Hz is fine), so 0.5% then."""
    if fps <= 0:
        return False
    ratio = hz / fps
    n = round(ratio)
    return n >= 1 and abs(ratio - n) / n < tol


def best_rate(device: str, fps: float, tol: float = 0.001) -> Optional[int]:
    """The display rate to use for this video, or None if the current one is already fine / nothing fits."""
    cur = current(device)
    if not cur or fps <= 0:
        return None
    w, h, hz = cur
    if is_multiple(_real_hz(hz), fps, tol):
        return None
    fits = [r for r in rates(device, w, h) if is_multiple(_real_hz(r), fps, tol)]
    return max(fits) if fits else None   # highest fitting rate keeps the rest of the screen smooth too


def switch(device: str, hz: int) -> bool:
    cur = current(device)
    if not cur:
        return False
    dm = _mode(device, _ENUM_CURRENT_SETTINGS)
    dm.dmDisplayFrequency = hz
    dm.dmFields = _DM_PELSWIDTH | _DM_PELSHEIGHT | _DM_DISPLAYFREQUENCY
    return _user32.ChangeDisplaySettingsExW(device, ctypes.byref(dm), None, _CDS_FULLSCREEN, None) \
        == _DISP_CHANGE_SUCCESSFUL


def restore(device: str):
    """Back to the display's normal (registry) mode."""
    if sys.platform == "win32":
        _user32.ChangeDisplaySettingsExW(device, None, None, 0, None)
