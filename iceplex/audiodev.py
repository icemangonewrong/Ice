"""Name of the current default audio output (e.g. "Silverado" or "Speakers (Realtek)").

Used to remember an audio delay per device: Bluetooth car stereos and headsets lag behind
the picture by different amounts. On Windows this asks the Core Audio API directly through
ctypes, so no extra packages are needed. Returns "" if it can't tell.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_uint, c_ushort, c_void_p, c_wchar_p

if sys.platform == "win32":
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, s: str):
            super().__init__()
            ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(s), byref(self))

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                    ("pwszVal", c_wchar_p), ("pad", c_void_p)]

    CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    PKEY_FMTID_FriendlyName = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
    VT_LPWSTR = 31
    HRESULT = ctypes.HRESULT

    def _method(obj, index, *argtypes):
        vtbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
        fn = proto(vtbl[index])
        return lambda *args: fn(obj, *args)

    def _release(obj):
        if obj:
            vtbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
            ctypes.WINFUNCTYPE(c_uint, c_void_p)(vtbl[2])(obj)

    _com_ready = False

    def default_output_name() -> str:
        global _com_ready
        enum = dev = store = None
        try:
            if not _com_ready:
                try:
                    ctypes.windll.ole32.CoInitializeEx(None, 2)  # already initialized by Qt is fine
                except OSError:
                    pass
                _com_ready = True
            enum = c_void_p()
            ctypes.oledll.ole32.CoCreateInstance(byref(GUID(CLSID_MMDeviceEnumerator)), None, 0x17,
                                                 byref(GUID(IID_IMMDeviceEnumerator)), byref(enum))
            dev = c_void_p()
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender=0, eConsole=0, &device)
            _method(enum, 4, c_uint, c_uint, POINTER(c_void_p))(0, 0, byref(dev))
            store = c_void_p()
            # IMMDevice::OpenPropertyStore(STGM_READ=0, &store)
            _method(dev, 4, c_uint, POINTER(c_void_p))(0, byref(store))
            key = PROPERTYKEY()
            key.fmtid = GUID(PKEY_FMTID_FriendlyName)
            key.pid = 14
            pv = PROPVARIANT()
            # IPropertyStore::GetValue(&key, &propvariant)
            _method(store, 5, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(byref(key), byref(pv))
            name = pv.pwszVal if pv.vt == VT_LPWSTR else ""
            ctypes.oledll.ole32.PropVariantClear(byref(pv))
            return name or ""
        except Exception:
            return ""
        finally:
            for o in (store, dev, enum):
                try:
                    _release(o)
                except Exception:
                    pass
else:
    def default_output_name() -> str:
        return ""
