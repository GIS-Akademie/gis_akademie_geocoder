# -*- coding: utf-8 -*-
"""
Minimal replacement for qgis.PyQt.QtCore (QObject, pyqtSignal) so that the
plugin's worker modules also run WITHOUT QGIS — for the test suite and the
build script tools/build_db.py. If QGIS can be imported, nothing happens.
"""
import sys
import types


class _Signal:
    def __init__(self):
        self._subs = []

    def connect(self, fn):
        self._subs.append(fn)

    def disconnect(self, fn=None):
        if fn is None:
            if not self._subs:
                raise TypeError('disconnect(): keine Verbindung')
            self._subs = []
        else:
            self._subs = [f for f in self._subs if f is not fn]

    def emit(self, *args):
        for fn in list(self._subs):
            fn(*args)


class pyqtSignal:
    def __init__(self, *types_):
        pass

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        sigs = obj.__dict__.setdefault('_stub_signals', {})
        return sigs.setdefault(self.name, _Signal())


class QObject:
    def __init__(self, *a, **k):
        pass

    def moveToThread(self, *a):
        pass

    def deleteLater(self):
        pass


def install_qt_stub():
    """Registers the stub in sys.modules if no real QGIS is available.
    Returns True if the stub is active."""
    try:
        import qgis.PyQt.QtCore  # noqa: F401
        return False
    except Exception:
        pass
    qtcore = types.ModuleType('qgis.PyQt.QtCore')
    qtcore.QObject = QObject
    qtcore.pyqtSignal = pyqtSignal
    sys.modules.setdefault('qgis', types.ModuleType('qgis'))
    sys.modules.setdefault('qgis.PyQt', types.ModuleType('qgis.PyQt'))
    sys.modules['qgis.PyQt.QtCore'] = qtcore
    return True


def register_plugin_package(plugin_dir, name='gis_akademie_geocoder'):
    """Makes the plugin folder importable as a package (relative imports)."""
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [plugin_dir]
        sys.modules[name] = pkg
    return name
