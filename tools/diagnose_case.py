# -*- coding: utf-8 -*-
"""
Diagnosis for ONE specific address case of the local search.

Shows whether the current local_geocoder.py is really executed, whether the exact
street is in the DB, what the stage-1 ranking looks like and what search() returns.
This distinguishes between (a) stale bytecode/a second module copy and
(b) a genuine search problem.

Usage (from the plugin folder):
    python tools/diagnose_case.py C:/GIS/geocoder.db "Sylt" "Munkmarscher Straße" 14
    python tools/diagnose_case.py C:/GIS/geocoder.db "Bardowick" "Hinterm Dom" 2
"""

import os
import sys
import argparse

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, _PLUGIN_DIR)
try:
    from gis_akademie_geocoder import local_geocoder as lc
except Exception:
    import local_geocoder as lc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("gemeinde")
    ap.add_argument("strasse")
    ap.add_argument("hausnummer", nargs="?", default="")
    args = ap.parse_args()

    print("Geladenes Modul :", lc.__file__)
    print("Bytecode-Cache  :", getattr(lc, "__cached__", None))
    # Two-stage search: first the distinct street names, then their rows
    has_two_stage = "SELECT DISTINCT strasse_norm" in open(lc.__file__, encoding="utf-8").read()
    print("Quelle enthält zweistufige Logik:", has_two_stage)

    lc.set_db_path(args.db)
    if not lc.db_exists():
        print("DB nicht gefunden/leer:", args.db)
        return

    gem  = lc._core_gemeinde(args.gemeinde)
    strn = lc.normalize(args.strasse)
    strc = lc._canonical(strn)
    pref = lc._safe_prefix(strn)
    print(f"\ngemeinde_norm (berechnet): {gem!r}")
    print(f"strasse_norm  (berechnet): {strn!r}   canonical: {strc!r}   prefix: {pref!r}")

    conn = lc.get_connection()
    exact = conn.execute(
        "SELECT COUNT(*) FROM addresses WHERE gemeinde_norm=? AND strasse_norm=?",
        (gem, strn)).fetchone()[0]
    exact_hn = conn.execute(
        "SELECT COUNT(*) FROM addresses WHERE gemeinde_norm=? AND strasse_norm=? "
        "AND replace(lower(hausnummer)||coalesce(lower(hausnr_zusatz),''),' ','')=?",
        (gem, strn, (args.hausnummer or '').lower().replace(' ', ''))).fetchone()[0]
    print(f"\nExakte Zeilen mit dieser Straße im Ort : {exact}")
    print(f"davon mit Hausnummer {args.hausnummer!r:>6}            : {exact_hn}")

    rows = conn.execute(
        "SELECT DISTINCT strasse_norm FROM addresses WHERE gemeinde_norm=? "
        "AND strasse_norm>=? AND strasse_norm<? LIMIT 5000",
        (gem, pref, pref + '\uffff')).fetchall()
    ranked = sorted(
        ((1.0 - lc.levenshtein(strc, lc._canonical(r[0] or '')) /
          max(len(strc), len(lc._canonical(r[0] or '')), 1), r[0]) for r in rows),
        reverse=True)
    print(f"\nStufe-1: {len(rows)} distinkte Straßen im Präfixbereich. Top 6:")
    for sc, nm in ranked[:6]:
        print(f"    {sc:0.3f}  {nm!r}")
    conn.close()

    res = lc.search(args.strasse, hausnummer=args.hausnummer,
                    gemeinde=args.gemeinde, max_results=3)
    print("\nsearch() Top 3:")
    for h in res:
        print(f"    {h['score']:0.3f}  {h['strasse']!r} {h['hausnummer']!r}  "
              f"(hn_score={h['hn_score']})")
    if not res:
        print("    (kein Treffer)")


if __name__ == "__main__":
    main()
