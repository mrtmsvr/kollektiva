#!/usr/bin/env python3
"""
Kollektíva – az év körforgása (ünnepek, ünnepkörök, évszakok)
=============================================================

Megmondja, hol tartunk az évben: milyen ünnep, ünnepkör vagy jeles nap közeleg (advent, Mikulás, karácsony,
farsang, húsvét, anyák napja, ballagás, tanévkezdés, Halloween, halottak napja, Márton-nap stb.).
A saját cikkek (offtopic.py) ebből terveznek tematikus anyagokat: ajánlók, ajándékötletek, eredettörténetek,
„X dolog, amit nem tudtál”, gyakorlati tippek – mindig a megfelelő napokra időzítve.
"""
from __future__ import annotations

from datetime import date, timedelta


def easter(y: int) -> date:
    """Húsvétvasárnap (Gauss–Meeus-féle algoritmus, Gergely-naptár)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    """A hónap n-edik adott napja (weekday: hétfő=0); n=-1: az utolsó."""
    if n > 0:
        d = date(y, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(y + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def occasions(y: int) -> list:
    """Az év jeles napjai. major=True: nagy ünnep (korábban kezdünk készülni, több cikk)."""
    e = easter(y)
    xmas = date(y, 12, 25)
    adv4 = xmas - timedelta(days=(xmas.weekday() + 1) % 7 or 7)  # a karácsony előtti utolsó vasárnap
    out = [
        ("ujev", "Újév, újévi fogadalmak", date(y, 1, 1), True),
        ("farsang", "Farsang kezdete (vízkereszt)", date(y, 1, 6), False),
        ("valentin", "Valentin-nap", date(y, 2, 14), True),
        ("hamvazoszerda", "Farsang vége, hamvazószerda, nagyböjt", e - timedelta(days=46), False),
        ("nonap", "Nőnap", date(y, 3, 8), False),
        ("marcius15", "Március 15., nemzeti ünnep", date(y, 3, 15), False),
        ("tavasz", "Tavasz kezdete (napéjegyenlőség)", date(y, 3, 20), False),
        ("oraatallitas-tavasz", "Óraátállítás (nyári időszámítás)", _nth_weekday(y, 3, 6, -1), False),
        ("husvet", "Húsvét", e, True),
        ("fold-napja", "A Föld napja", date(y, 4, 22), False),
        ("majus1", "Május 1., majális", date(y, 5, 1), False),
        ("anyak-napja", "Anyák napja", _nth_weekday(y, 5, 6, 1), True),
        ("erettsegi", "Érettségi és ballagási szezon", date(y, 5, 4), False),
        ("punkosd", "Pünkösd", e + timedelta(days=49), False),
        ("nyari-szunet", "Nyári szünet, nyaralási szezon", date(y, 6, 20), True),
        ("nyar", "Nyár kezdete (napforduló)", date(y, 6, 21), False),
        ("augusztus20", "Augusztus 20., államalapítás", date(y, 8, 20), False),
        ("tanevkezdes", "Tanévkezdés", date(y, 9, 1), True),
        ("osz", "Ősz kezdete (napéjegyenlőség)", date(y, 9, 22), False),
        ("oktober6", "Október 6., az aradi vértanúk emléknapja", date(y, 10, 6), False),
        ("oktober23", "Október 23., nemzeti ünnep", date(y, 10, 23), False),
        ("oraatallitas-osz", "Óraátállítás (téli időszámítás)", _nth_weekday(y, 10, 6, -1), False),
        ("halloween", "Halloween", date(y, 10, 31), False),
        ("halottak-napja", "Mindenszentek és halottak napja", date(y, 11, 1), True),
        ("marton-nap", "Márton-nap", date(y, 11, 11), False),
        ("black-friday", "Black Friday, akciós szezon", _nth_weekday(y, 11, 4, 4), False),
        ("advent1", "Advent 1. vasárnapja, az adventi időszak kezdete", adv4 - timedelta(days=21), True),
        ("advent2", "Advent 2. vasárnapja", adv4 - timedelta(days=14), False),
        ("mikulas", "Mikulás", date(y, 12, 6), True),
        ("advent3", "Advent 3. vasárnapja", adv4 - timedelta(days=7), False),
        ("luca", "Luca-nap", date(y, 12, 13), False),
        ("advent4", "Advent 4. vasárnapja", adv4, False),
        ("karacsony", "Karácsony", date(y, 12, 24), True),
        ("szilveszter", "Szilveszter", date(y, 12, 31), True),
    ]
    return [{"id": f"{i}-{y}", "key": i, "name": n, "date": d, "major": m} for i, n, d, m in out]


def season(d: date) -> str:
    m = d.month
    return "tél" if m in (12, 1, 2) else "tavasz" if m in (3, 4, 5) else "nyár" if m in (6, 7, 8) else "ősz"


def upcoming(d: date, horizon: int = 25) -> list:
    """A következő `horizon` napban esedékes jeles napok, közelség szerint: [{..., 'days': n}]."""
    out = []
    for y in (d.year, d.year + 1):
        for o in occasions(y):
            n = (o["date"] - d).days
            if 0 <= n <= horizon:
                out.append({**o, "days": n})
    return sorted(out, key=lambda o: o["days"])


def lead_days(o: dict) -> int:
    """Ennyi nappal előtte kezdünk róla írni (nagy ünnep: 3 hét, kisebb: 10 nap)."""
    return 21 if o["major"] else 10


def where_are_we(d: date) -> str:
    """Egy mondat az AI-nak: hol tartunk az évben (évszak + közelgő jeles napok)."""
    up = [o for o in upcoming(d, 30)]
    if not up:
        return f"Évszak: {season(d)}."
    return f"Évszak: {season(d)}. Közeleg: " + "; ".join(
        f"{o['name']} ({o['date'].isoformat()}, {'ma' if o['days'] == 0 else str(o['days']) + ' nap múlva'})" for o in up[:5]) + "."


if __name__ == "__main__":
    import sys
    t = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    print(where_are_we(t))
    for o in occasions(t.year):
        print(o["date"], o["name"])
