#!/usr/bin/env python3
"""
Kollektíva – napi szavazás
==========================

Naponta egy semleges, rövid szavazás a nap egyik legforróbb (már kint lévő) közéleti/világ/pénz/tech cikkéhez.
A kérdést az AI írja (pártsemlegesen, nem sugalmazó módon), a szavazatokat a Cloudflare D1 adatbázis tárolja
(functions/api/poll.js). A kérdések listája: public/data/polls.json (legfrissebb elöl, max. 30).
Futás: a robot (kollektiva_content.py) hívja; naponta egyszer, 10 és 20 óra között.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.polls")
POLL_SECTIONS = ("kozelet", "vilag", "penzvilag", "tech", "eletmod")

POLL_SYSTEM = (
    "Egy magyar online magazin (Kollektíva) szerkesztője vagy. Egy cikkhez olvasói szavazást írsz. SZABÁLYOK: a kérdés "
    "rövid, érthető, semleges – nem sugalmaz választ, nem sértő, nem pártpolitikai hovatartozásra kérdez rá, nem "
    "személyeskedik; a válaszlehetőségek kiegyensúlyozottak, egymást kizárók, és lefedik a fő álláspontokat "
    "(lehet köztük „Nem tudom / nincs véleményem”). Bűnügyben nem kérdezhetsz rá valaki bűnösségére. "
    "Csak érvényes JSON-t adsz vissza."
)


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    if os.getenv("POLLS_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    path = output_dir / "polls.json"
    data = kc.read_json(path, {"polls": []})
    polls = data.get("polls", [])
    if any(p.get("date") == d.isoformat() for p in polls) or not 10 <= now.hour <= 20:
        return 0
    articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
    used = {p.get("article_url") for p in polls}
    cands = [a for a in articles if a.get("status") == "published" and a.get("category") in POLL_SECTIONS
             and a.get("date") == d.isoformat() and a.get("url") not in used and not a.get("offtopic")]
    if not cands:
        return 0
    art = max(cands, key=lambda a: a.get("hot_score") or 0)
    prompt = (f"Cikk: {art['title']}\nBevezető: {art.get('lead', '')}\nLényeg: " + " | ".join(art.get("key_points") or [])
              + "\n\nÍrj hozzá EGY olvasói szavazást. JSON: {\"question\": \"max. 12 szavas kérdés\", "
                "\"options\": [\"2–4 rövid válasz, max. 6 szó\"]}. Ha a témához nem illik tisztességes szavazás "
                "(pl. tragédia, bűncselekmény áldozatai), add vissza: {\"skip\": true}.")
    try:
        raw = ai.complete_json(POLL_SYSTEM, prompt, 600)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Szavazás kimaradt: %s", e)
        return 0
    opts = [str(o).strip()[:60] for o in (raw.get("options") or []) if str(o).strip()][:4]
    q = str(raw.get("question") or "").strip()[:140]
    if raw.get("skip") or not q or len(opts) < 2:
        log.info("Szavazás: ehhez a cikkhez nem készül (%s).", art["title"])
        return 0
    poll = {"id": hashlib.sha1(f"{d.isoformat()}|{art['url']}".encode()).hexdigest()[:12], "date": d.isoformat(),
            "question": q, "options": opts, "article_url": art["url"], "article_title": art["title"],
            "category": art.get("category"), "created_at": now.isoformat(timespec="seconds"),
            "closes_at": (now + timedelta(days=int(os.getenv("POLL_DAYS", "7")))).isoformat(timespec="seconds")}
    log.info("✔ Szavazás: %s %s", q, opts)
    if dry_run:
        return 1
    kc.write_json_atomic(path, {"updated_at": now.isoformat(timespec="seconds"), "polls": [poll] + polls[:29]})
    return 1
