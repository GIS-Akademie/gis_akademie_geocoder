# -*- coding: utf-8 -*-
"""
Network access through QGIS (QgsNetworkAccessManager) for all requests of the
plugin: downloads of the sources, the online source list and Nominatim.

The proxy settings of QGIS apply (including the exclusion list and SOCKS
proxies), as well as its SSL settings (e.g. certificates of an own
certification authority in corporate networks) and authentication
configurations. QgisHttp offers the interface of source_downloader.Http, so
the download logic stays the same; outside QGIS (tests, tools/build_db.py)
the plain urllib variant is used.

Works in worker threads: QgsNetworkAccessManager.instance() returns one
manager per thread, and each request runs in its own local event loop.
Downloads are written to disk piece by piece (several GB per source).
"""

import os
import hashlib

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QUrl, QEventLoop, QTimer
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from . import source_downloader as _sd


def _enum(owner, scope, name):
    """Qt enum value for PyQt6 (scoped) and PyQt5 (unscoped)."""
    return getattr(getattr(owner, scope, owner), name)


_STATUS     = _enum(QNetworkRequest, 'Attribute', 'HttpStatusCodeAttribute')
_CACHE_LOAD = _enum(QNetworkRequest, 'Attribute', 'CacheLoadControlAttribute')
_CACHE_SAVE = _enum(QNetworkRequest, 'Attribute', 'CacheSaveControlAttribute')
_ALWAYS_NETWORK = _enum(QNetworkRequest, 'CacheLoadControl', 'AlwaysNetwork')
_REDIRECT   = _enum(QNetworkRequest, 'Attribute', 'RedirectPolicyAttribute')
_NO_LESS_SAFE = _enum(QNetworkRequest, 'RedirectPolicy', 'NoLessSafeRedirectPolicy')
_NO_ERROR   = _enum(QNetworkReply, 'NetworkError', 'NoError')
_CANCELED   = _enum(QNetworkReply, 'NetworkError', 'OperationCanceledError')


class QgisHttp(_sd.Http):
    """source_downloader.Http over the network access of QGIS."""

    def _request(self, url, headers=None):
        req = QNetworkRequest(QUrl(url))
        h = {'User-Agent': self.user_agent, 'Accept': '*/*',
             # no transparent decompression: Content-Length must match the bytes read
             'Accept-Encoding': 'identity'}
        h.update(headers or {})
        for k, v in h.items():
            req.setRawHeader(k.encode('latin-1'), str(v).encode('latin-1'))
        req.setAttribute(_REDIRECT, _NO_LESS_SAFE)
        # Bypass the QGIS network cache: it would answer a conditional request
        # (304) with the cached file as 200, and multi-GB downloads do not
        # belong in it.
        req.setAttribute(_CACHE_LOAD, _ALWAYS_NETWORK)
        req.setAttribute(_CACHE_SAVE, False)
        return req

    def _fetch(self, url, headers, sink, max_bytes=None, progress=None, cancel=None):
        """Run one GET request; every received piece goes to sink(bytes).
        Returns (status, headers_dict, bytes_received)."""
        if cancel and cancel():
            raise _sd.Cancelled()
        reply = QgsNetworkAccessManager.instance().get(self._request(url, headers))
        loop = QEventLoop()
        st = {'done': 0, 'total': 0, 'timeout': False, 'cancel': False, 'enough': False}
        idle = QTimer()                   # no data for `timeout` seconds → abort
        idle.setSingleShot(True)
        idle.setInterval(int(self.timeout * 1000))
        poll = QTimer()                   # react to a cancel request
        poll.setInterval(250)

        def on_ready():
            data = bytes(reply.readAll())
            if not data or st['enough'] or st['cancel']:
                return
            if cancel and cancel():
                st['cancel'] = True
                reply.abort()
                return
            if max_bytes is not None and st['done'] + len(data) >= max_bytes:
                data = data[:max_bytes - st['done']]
                st['enough'] = True
            sink(data)
            st['done'] += len(data)
            idle.start()
            if progress:
                progress(st['done'], st['total'])
            if st['enough']:
                reply.abort()

        def on_progress(_received, total):
            st['total'] = max(int(total), 0)

        def on_timeout():
            st['timeout'] = True
            reply.abort()

        def on_poll():
            if cancel and cancel():
                st['cancel'] = True
                reply.abort()

        reply.readyRead.connect(on_ready)
        reply.downloadProgress.connect(on_progress)
        reply.finished.connect(loop.quit)
        idle.timeout.connect(on_timeout)
        poll.timeout.connect(on_poll)
        idle.start()
        poll.start()
        try:
            if not reply.isFinished():
                (loop.exec if hasattr(loop, 'exec') else loop.exec_)()
            idle.stop()
            poll.stop()
            on_ready()                    # rest of the data
            status = reply.attribute(_STATUS)
            status = int(status) if status not in (None, '') else None
            hdrs = {bytes(k).decode('latin-1').lower(): bytes(v).decode('latin-1')
                    for k, v in reply.rawHeaderPairs()}
            err = reply.error()
            if st['cancel']:
                raise _sd.Cancelled()
            if st['timeout']:
                raise _sd._net_error(url, f'keine Antwort seit {self.timeout:.0f} s')
            if status == 304:
                return 304, hdrs, st['done']
            if status is not None and status >= 400:
                raise _sd._http_error(status, url, hdrs)
            if err not in (_NO_ERROR,) and not (err == _CANCELED and st['enough']):
                total = int(hdrs.get('content-length') or 0)
                if total and 0 < st['done'] < total:
                    raise _sd.SourceError(
                        f'Der Download von {_sd._host(url)} wurde vorzeitig beendet '
                        f'({_sd._mb(st["done"])} von {_sd._mb(total)} MB). Bitte später erneut '
                        'versuchen – der bisherige Bestand bleibt erhalten.',
                        detail=f'Verbindung nach {st["done"]} von {total} Bytes beendet '
                               f'({reply.errorString()}): {url}',
                        kind='voruebergehend')
                raise _sd._net_error(url, reply.errorString())
            return status or 200, hdrs, st['done']
        finally:
            reply.deleteLater()

    def get(self, url, headers=None, max_bytes=None):
        parts = []
        status, hdrs, _n = self._fetch(url, headers, parts.append, max_bytes=max_bytes)
        return status, hdrs, b''.join(parts)

    def download(self, url, dest, headers=None, progress=None, cancel=None):
        tmp = dest + '.part'
        sha = hashlib.sha256()
        try:
            with open(tmp, 'wb') as f:
                def sink(data):
                    f.write(data)
                    sha.update(data)
                status, hdrs, done = self._fetch(url, headers, sink, progress=progress,
                                                 cancel=cancel)
            if status == 304:
                _sd._unlink(tmp)
                return 304, hdrs
            total = int(hdrs.get('content-length') or 0)
            if total and done < total:
                raise _sd.SourceError(
                    f'Der Download von {_sd._host(url)} wurde vorzeitig beendet '
                    f'({_sd._mb(done)} von {_sd._mb(total)} MB). Bitte später erneut versuchen – '
                    'der bisherige Bestand bleibt erhalten.',
                    detail=f'Verbindung nach {done} von {total} Bytes beendet: {url}',
                    kind='voruebergehend')
            hdrs['_sha256'] = sha.hexdigest()
            hdrs['_size'] = str(done)
            os.replace(tmp, dest)
            return status, hdrs
        except BaseException:
            _sd._unlink(tmp)
            raise
