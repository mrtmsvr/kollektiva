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
import re
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
    "MÓDSZERTAN: egy kérdés egyetlen dologra kérdezzen (ne legyen kettős kérdés), konkrét döntésre vagy helyzetre; "
    "csak olyan témát válassz, amiben az emberek tényleg megoszlanak; a pro és kontra válaszok száma és erőssége "
    "legyen azonos, egy köztes válasz belefér; ne legyen értékítéletet sugalló szó a kérdésben; a „Nem tudom” mindig "
    "az utolsó. Magázva vagy személytelenül kérdezz (pl. „Ön szerint…”, „Hogyan értékeli…”), ne tegezve. "
    "Csak érvényes JSON-t adsz vissza."
)


# Tegező alakok -> magázó (a modell néha a szabály ellenére tegez). A gyakori kérdésformákat cseréljük;
# ha ezután is tegező jelet találunk, a kérdést újraíratjuk (új szavazásnál), vagy kimarad.
FORMAL_SUBS = [
    (r"\bSzerinted\b", "Ön szerint"), (r"\bszerinted\b", "ön szerint"),
    (r"\bTe mit\b", "Ön mit"), (r"\bte mit\b", "ön mit"), (r"\bNeked\b", "Önnek"), (r"\bneked\b", "önnek"),
    (r"\bTámogatod\b", "Támogatja"), (r"\btámogatod\b", "támogatja"), (r"\bGondolod\b", "Gondolja"),
    (r"\bgondolod\b", "gondolja"), (r"\bTartod\b", "Tartja"), (r"\btartod\b", "tartja"),
    (r"\bÉrtesz egyet\b", "Egyetért"), (r"\bértesz egyet\b", "egyetért"), (r"\bEgyetértesz\b", "Egyetért"),
    (r"\begyetértesz\b", "egyetért"), (r"\bÉrtékeled\b", "Értékeli"), (r"\bértékeled\b", "értékeli"),
    (r"\bLátod\b", "Látja"), (r"\blátod\b", "látja"), (r"\bVárod\b", "Várja"), (r"\bvárod\b", "várja"),
    (r"\bfizetnél\b", "fizetne"), (r"\bvennél\b", "venne"), (r"\bszavaznál\b", "szavazna"),
]
INFORMAL = re.compile(r"\b(szerinted|neked|veled|tőled|nálad|te\s|gondolod|tartod|támogatod|szeretnéd|"
                      r"\w+od\?|\w+ed\?|\w+öd\?)", re.I)


def formal(text: str) -> str:
    for pat, rep in FORMAL_SUBS:
        text = re.sub(pat, rep, text)
    return text


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    if os.getenv("POLLS_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    path = output_dir / "polls.json"
    data = kc.read_json(path, {"polls": []})
    polls = data.get("polls", [])
    fixed = False
    for p in polls:  # a kint lévő szavazások tegező szövegét is javítjuk (magázás)
        q2, o2 = formal(p.get("question", "")), [formal(o) for o in p.get("options", [])]
        if q2 != p.get("question") or o2 != p.get("options"):
            p["question"], p["options"], fixed = q2, o2, True
    if fixed and not dry_run:
        log.info("Szavazás: tegező szöveg javítva magázóra.")
        kc.write_json_atomic(path, {**data, "polls": polls})
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
        raw = ai.complete_json(POLL_SYSTEM, prompt, 600, light=True)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Szavazás kimaradt: %s", e)
        return 0
    opts = [formal(str(o).strip()[:60]) for o in (raw.get("options") or []) if str(o).strip()][:4]
    q = formal(str(raw.get("question") or "").strip()[:140])
    if INFORMAL.search(q):  # még mindig tegez -> egyszer újraíratjuk, kifejezetten magázva
        try:
            fx = ai.complete_json(POLL_SYSTEM, f"Írd át ezt a szavazási kérdést MAGÁZÓ formára (pl. „Ön szerint…”, "
                                  f"„Támogatja…”), a jelentés maradjon: {q}\nJSON: {{\"question\": \"...\"}}", 200, light=True)
            q = formal(str(fx.get("question") or q).strip()[:140])
        except Exception:  # noqa: BLE001
            pass
        if INFORMAL.search(q):
            log.info("Szavazás kimarad: tegező kérdés (%s).", q)
            return 0
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
    try:  # értesítés Telegramon, leszedés gombbal
        import telegram_review as tr
        chat = tr.load_state().get("chat_id") if os.getenv("TELEGRAM_BOT_TOKEN") else None
        if chat:
            tr.tg("sendMessage", {"chat_id": chat, "text": f"🗳 Új szavazás kint: {q}\n" + " / ".join(opts)
                                  + f"\n(cikk: {art['title']})",
                                  "reply_markup": {"inline_keyboard": [[{"text": "🗑 Szavazás leszedése",
                                                                        "callback_data": f"pdel|{poll['id']}"}]]}})
    except Exception as e:  # noqa: BLE001
        log.warning("Szavazás-értesítés kimaradt: %s", e)
    return 1
