# -*- coding: utf-8 -*-
"""
Drag & drop onto the dialog's path fields: turns what a drop delivers (file
URLs from the file manager, layer URIs from the QGIS browser panel) into ONE
local file path with a suitable extension. Qt-free, so the test suite covers
it without QGIS.
"""
import os
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

INPUT_EXTS = ('.csv', '.txt', '.tsv', '.tab', '.xlsx', '.xls', '.ods')
GPKG_EXTS  = ('.gpkg',)
DB_EXTS    = ('.db',)


def to_local_path(text):
    """A dropped entry as a local path: plain paths stay as they are, a
    ``file:`` URL is decoded, and the options QGIS appends to a layer URI
    (``|layername=…`` for OGR, ``?delimiter=…`` for delimited text) are cut
    off. Returns '' for anything that is not a local file."""
    text = (text or '').strip()
    if not text:
        return ''
    text = text.split('|', 1)[0]
    if text.lower().startswith('file:'):
        parts = urlsplit(text)
        if parts.netloc and parts.netloc.lower() != 'localhost':
            path = '//' + parts.netloc + unquote(parts.path)      # UNC share
        else:
            path = url2pathname(parts.path)
        return os.path.normpath(path) if path else ''
    if '://' in text:
        return ''
    return os.path.normpath(text)


def pick_path(candidates, exts):
    """First candidate whose extension is one of `exts` (case-insensitive),
    as a local path; '' if none fits. Folders and other file types are
    skipped, so a mixed selection still delivers the usable file."""
    for cand in candidates or ():
        path = to_local_path(cand)
        if path and os.path.splitext(path)[1].lower() in exts:
            return path
    return ''
