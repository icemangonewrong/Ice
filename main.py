"""IcePlex for Windows — a Plex player with true direct play."""
import os
import sys


def _prepare_mpv_path():
    """libmpv-2.dll ships next to the exe (or next to this file when run from source)."""
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for d in (here, os.path.join(here, "lib"), os.path.dirname(sys.executable)):
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass


def main():
    _prepare_mpv_path()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    fmt = QSurfaceFormat()
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("IcePlex")
    app.setOrganizationName("IcePlex")
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IcePlex.Windows")
        except Exception:
            pass

    from iceplex import theme
    from iceplex.pages import resource_path
    app.setStyle("Fusion")
    app.setStyleSheet(theme.full_stylesheet(os.path.dirname(resource_path("check.png"))))
    app.setFont(theme.font(10))

    try:
        from iceplex.window import MainWindow
    except OSError as e:  # libmpv-2.dll missing or blocked
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "IcePlex", "IcePlex couldn't load its video engine (libmpv-2.dll).\n\n"
                             "Make sure you extracted the whole IcePlex folder, then try again.\n\n" + str(e))
        sys.exit(1)
    win = MainWindow()
    win.show_initial()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
