"""Second headphones: play the sound on a second Bluetooth headset (e.g. two pairs of AirPods) at the same time.

Windows (and the video player) send sound to one output only. While a video plays, this copies whatever the
main output is playing (WASAPI "loopback") straight to the second device, the same thing Voicemeeter does, just
built in. The copy runs a few hundredths of a second behind; a delay setting lets you hold the second pair back a
little more if it still sounds ahead of the first.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

RATE = 48000
BLOCK = 480          # frames per copy step (10 ms)
BUFFER = 480 * 6     # sound card buffer on each side (60 ms): small enough to stay close, big enough not to crackle


class Problem(Exception):
    """Something to tell the user as is."""


def available() -> bool:
    return sys.platform == "win32"


def output_names() -> list[str]:
    """Names of the sound outputs (speakers, headphones) Windows has right now."""
    try:
        import soundcard as sc
        return [s.name for s in sc.all_speakers()]
    except Exception:  # noqa: BLE001
        return []


class Mirror:
    """Copies the main output's sound to one other output, in a background thread."""

    def __init__(self, on_status: Optional[Callable[[str], None]] = None):
        self.on_status = on_status or (lambda s: None)
        self.target = ""
        self.delay_ms = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running_on = ""      # the second device actually playing ("" when not)

    def start(self, target: str, delay_ms: int = 0):
        if not available() or not target:
            self.stop()
            return
        if self._thread and self._thread.is_alive() and target == self.target and delay_ms == self.delay_ms:
            return
        self.stop()
        self.target, self.delay_ms = target, int(delay_ms)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop, target, int(delay_ms)), daemon=True,
                                        name="ice-mirror")
        self._thread.start()

    def restart(self):
        """The main output changed (e.g. the first AirPods reconnected): start over from the new one."""
        t, d = self.target, self.delay_ms
        if t:
            self.stop()
            self.start(t, d)

    def stop(self):
        self._stop.set()
        th = self._thread
        self._thread = None
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.5)
        self.running_on = ""

    # -- the copy loop -------------------------------------------------------------

    def _run(self, stop: threading.Event, target: str, delay_ms: int):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)   # sound APIs need COM on this thread
        except Exception:  # noqa: BLE001
            pass
        told = False
        while not stop.is_set():
            try:
                self._copy(stop, target, delay_ms)
            except Exception as e:  # noqa: BLE001  (device unplugged, asleep, …): try again shortly
                print("mirror:", e)
                self.running_on = ""
                if not told:
                    self.on_status(str(e) if isinstance(e, Problem) else f"Can't reach {target} right now. Ice keeps trying.")
                    told = True
            stop.wait(3.0)

    def _copy(self, stop: threading.Event, target: str, delay_ms: int):
        import warnings
        import numpy as np
        import soundcard as sc
        # A copy that briefly falls behind skips a few milliseconds (that's how it stays in step); no need to log it.
        warnings.filterwarnings("ignore", category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))
        main = sc.default_speaker()
        if main.name == target:
            raise Problem(f"{target} is the main output right now. Pick the other pair in Windows' sound menu.")
        out = next((s for s in sc.all_speakers() if s.name == target), None)
        if out is None:
            raise Problem(f"{target} isn't connected. Ice keeps trying.")
        loop = sc.get_microphone(main.id, include_loopback=True)
        with loop.recorder(samplerate=RATE, channels=2, blocksize=BUFFER) as rec, \
                out.player(samplerate=RATE, channels=2, blocksize=BUFFER) as ply:
            if delay_ms > 0:
                ply.play(np.zeros((int(RATE * delay_ms / 1000), 2), dtype="float32"))
            self.running_on = target
            self.on_status(f"Also playing on {target}")
            check = time.monotonic() + 2
            while not stop.is_set():
                ply.play(rec.record(BLOCK))
                if time.monotonic() > check:   # main output switched: follow it
                    check = time.monotonic() + 2
                    if sc.default_speaker().id != main.id:
                        return
