# -*- coding: utf-8 -*-
"""
One-time migration: recompute the normalization columns 'gemeinde_norm' and/or
'strasse_norm' of an existing geocoder.db — consistent with the
current logic (_core_gemeinde and normalize respectively). Display fields
(gemeinde, strasse) remain unchanged.

Background
----------
Older databases may have filled these columns with an earlier normalization.
The current search then misses some addresses (it falls back to the most
similar neighbor), e.g.:
  • 'gemeinde_norm' = 'frankenthal pfalz' instead of 'frankenthal'
  • 'strasse_norm' differs from normalize('…')

The script builds one mapping per column from the DISTINCT values (few
compared to the number of rows) and writes it back in a single UPDATE.

Usage
-----
    python migrate_norm.py C:/GIS/geocoder.db --dry-run     # report only
    python migrate_norm.py C:/GIS/geocoder.db               # both columns
    python migrate_norm.py C:/GIS/geocoder.db --only strasse
    python migrate_norm.py C:/GIS/geocoder.db --only gemeinde

Recommendation: make a copy of the DB beforehand.
"""

import os
import sys
import time
import argparse

_HERE       = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, _PLUGIN_DIR)
try:
    from gis_akademie_geocoder import local_geocoder as lc
except Exception:
    import local_geocoder as lc


def _migrate_column(conn, src_col, norm_col, fn, dry_run):
    """Recomputes norm_col from src_col via fn() (mapping over DISTINCT values)."""
    print(f"\n== {norm_col} (aus {src_col}) ==")
    t0 = time.time()
    src_vals = [row[src_col] for row in
                conn.execute(f"SELECT DISTINCT {src_col} FROM addresses").fetchall()]
    mapping = [(v, fn(v or "")) for v in src_vals]
    print(f"  {len(mapping):,} verschiedene Werte gelesen ({time.time()-t0:.1f}s).")

    conn.execute("DROP TABLE IF EXISTS _nmap")
    conn.execute("CREATE TEMP TABLE _nmap(src TEXT PRIMARY KEY, nn TEXT)")
    conn.executemany("INSERT OR REPLACE INTO _nmap(src, nn) VALUES(?,?)", mapping)

    n_change = conn.execute(
        f"SELECT COUNT(*) FROM addresses a JOIN _nmap m ON a.{src_col} IS m.src "
        f"WHERE a.{norm_col} IS NOT m.nn").fetchone()[0]
    n_total = conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    print(f"  {n_change:,} von {n_total:,} Zeilen würden ein neues {norm_col} erhalten.")

    sample = conn.execute(
        f"SELECT a.{src_col} AS s, a.{norm_col} AS alt, m.nn AS neu "
        f"FROM addresses a JOIN _nmap m ON a.{src_col} IS m.src "
        f"WHERE a.{norm_col} IS NOT m.nn LIMIT 10").fetchall()
    if sample:
        print("  Beispiele (Wert | alt -> neu):")
        for s in sample:
            print(f"    {s['s']!r}: {s['alt']!r} -> {s['neu']!r}")

    if dry_run:
        conn.execute("DROP TABLE IF EXISTS _nmap")
        return n_change

    if n_change:
        t0 = time.time()
        conn.execute(
            f"UPDATE addresses SET {norm_col} = "
            f"(SELECT nn FROM _nmap WHERE _nmap.src IS addresses.{src_col})")
        conn.commit()
        print(f"  {n_change:,} Zeilen aktualisiert ({time.time()-t0:.1f}s).")
    else:
        print("  Bereits konsistent – nichts zu tun.")
    conn.execute("DROP TABLE IF EXISTS _nmap")
    return n_change


def main():
    ap = argparse.ArgumentParser(description="gemeinde_norm/strasse_norm neu berechnen")
    ap.add_argument("db", help="Pfad zur geocoder.db")
    ap.add_argument("--only", choices=["gemeinde", "strasse"],
                    help="nur eine der beiden Spalten migrieren (Standard: beide)")
    ap.add_argument("--dry-run", action="store_true", help="nur berichten, nichts schreiben")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("Datenbank nicht gefunden:", args.db)
        sys.exit(1)

    lc.set_db_path(args.db)
    conn = lc.get_connection()

    total = 0
    if args.only != "strasse":
        total += _migrate_column(conn, "gemeinde", "gemeinde_norm",
                                 lc._core_gemeinde, args.dry_run)
    if args.only != "gemeinde":
        total += _migrate_column(conn, "strasse", "strasse_norm",
                                 lc.normalize, args.dry_run)

    if not args.dry_run and total:
        conn.execute("ANALYZE")
        conn.commit()
    conn.close()

    if args.dry_run:
        print("\n--dry-run: keine Änderungen geschrieben.")
    else:
        print(f"\nFertig. Bitte anschließend die Testsuite erneut laufen lassen.")


if __name__ == "__main__":
    main()
