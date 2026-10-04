#!/usr/bin/env python3
"""
Kollektíva – Heti kvíz
========================

Hetente egy 8 kérdéses hírkvíz a hét kint lévő cikkeiből (szombaton készül, 8 és 21 óra között).
Minden kérdés egy konkrét cikkre épül, a válasz után rövid magyarázat és link a cikkre (visszahozza az olvasót).
Az AI írja, a robot ellenőrzi (4 különböző válasz, létező cikk-link), a helyes válasz helyét a robot keveri.
Adat: public/data/quizzes.json (legfrissebb elöl, max. 26). Oldal: /kviz/ (kollektiva_content.build_quiz_page).
Telegramon értesítés jön „🗑 Kvíz leszedése” gombbal.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.quiz")
QUIZ_DAY = int(os.getenv("QUIZ_WEEKDAY", "5"))  # 0 = hétfő … 5 = szombat
N_QUESTIONS = 8

QUIZ_SYSTEM = (
    "Egy magyar online magazin (Kollektíva) szerkesztője vagy. A hét cikkeiből hírkvízt írsz az olvasóknak. "
    "SZABÁLYOK: minden kérdés EGY megadott cikk tényeire épül (szám, név, hely, döntés, eredmény), és a cikk "
    "elolvasása nélkül is kitalálható legyen józan ésszel vagy hírkövetéssel – ne apróság, ne becsapós. "
    "4 válaszlehetőség, pontosan egy helyes; a rossz válaszok hihetők, de egyértelműen rosszak. Pártsemleges, "
    "nem sértő; bűnügynél, tragédiánál, áldozatoknál nem kérdezel. A kérdések témában változatosak legyenek "
    "(ne mind közélet). Tegezve, közvetlen, kicsit játékos hangon, de pontosan. Csak érvényes JSON-t adsz vissza."
)


def _week_id(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _candidates(articles: list, d: date) -> list:
    since = (d - timedelta(days=7)).isoformat()
    pool = [a for a in articles if a.get("status") == "published" and a.get("url") and (a.get("date") or "") >= since
            and a.get("category") in kc.SECTIONS]
    pool.sort(key=lambda a: (6 if a.get("offtopic") else (a.get("hot_score") or 0)), reverse=True)
    out, per = [], {}
    for a in pool:  # rovatonként max. 4, hogy változatos legyen
        c = a.get("category")
        if per.get(c, 0) >= (2 if c == "bulvar" else 4):
            continue
        per[c] = per.get(c, 0) + 1
        out.append(a)
        if len(out) >= 18:
            break
    return out


def _validate(raw: dict, by_url: dict, seed: str) -> list:
    rng = kc.seeded_rng("quiz", seed)
    qs, used = [], set()
    for q in raw.get("questions") or []:
        try:
            text = str(q.get("question") or "").strip()[:200]
            opts = [str(o).strip()[:90] for o in (q.get("options") or [])]
            ci = int(q.get("correct"))
            url = str(q.get("article_url") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if not text or len(opts) != 4 or len({o.lower() for o in opts}) != 4 or not all(opts) or not 0 <= ci < 4:
            continue
        if url not in by_url or url in used:
            continue
        used.add(url)
        right = opts[ci]
        rng.shuffle(opts)  # az AI hajlamos a helyeset előre tenni
        qs.append({"question": text, "options": opts, "correct": opts.index(right),
                   "explain": str(q.get("explain") or "").strip()[:260],
                   "article_url": url, "article_title": by_url[url]["title"],
                   "category": by_url[url].get("category")})
        if len(qs) >= N_QUESTIONS:
            break
    return qs


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False, force: bool = False) -> int:
    if os.getenv("QUIZ_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    path = output_dir / "quizzes.json"
    data = kc.read_json(path, {"quizzes": []})
    quizzes = data.get("quizzes", [])
    week = _week_id(d)
    if not force and (d.weekday() < QUIZ_DAY or not 8 <= now.hour <= 21 or any(q.get("week") == week for q in quizzes)):
        return 0
    articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
    cands = _candidates(articles, d)
    if len(cands) < N_QUESTIONS:
        log.info("Kvíz: kevés a cikk (%d).", len(cands))
        return 0
    by_url = {a["url"]: a for a in cands}
    listing = "\n".join(
        f"- URL: {a['url']}\n  Rovat: {kc.SECTIONS[a['category']]['name']}\n  Cím: {a['title']}\n  Bevezető: {a.get('lead', '')}\n"
        f"  Lényeg: " + " | ".join(a.get("key_points") or [])[:500] for a in cands)
    prompt = (f"A hét cikkei:\n{listing}\n\nÍrj {N_QUESTIONS + 2} kérdést, mindegyiket MÁS cikkből. "
              "JSON: {\"title\": \"a kvíz rövid, kedvcsináló alcíme (max. 8 szó)\", \"questions\": [{\"question\": "
              "\"max. 20 szó\", \"options\": [\"4 rövid válasz, max. 8 szó\"], \"correct\": 0, \"explain\": "
              "\"1 mondat: mi a helyes válasz és miért\", \"article_url\": \"a fenti URL-ek egyike, pontosan\"}]}")
    try:
        raw = ai.complete_json(QUIZ_SYSTEM, prompt, 3500)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Kvíz kimaradt: %s", e)
        return 0
    qs = _validate(raw, by_url, week)
    if len(qs) < 6:
        log.warning("Kvíz: csak %d érvényes kérdés jött, kimarad.", len(qs))
        return 0
    quiz = {"id": hashlib.sha1(f"quiz|{week}".encode()).hexdigest()[:10], "week": week, "date": d.isoformat(),
            "title": str(raw.get("title") or "Mennyire követted a hetet?").strip()[:80],
            "created_at": now.isoformat(timespec="seconds"), "questions": qs}
    log.info("✔ Kvíz: %s (%d kérdés)", quiz["title"], len(qs))
    if dry_run:
        return 1
    kc.write_json_atomic(path, {"updated_at": now.isoformat(timespec="seconds"), "quizzes": [quiz] + quizzes[:25]})
    try:
        import telegram_review as tr
        chat = tr.load_state().get("chat_id") if os.getenv("TELEGRAM_BOT_TOKEN") else None
        if chat:
            tr.tg("sendMessage", {"chat_id": chat, "text": f"🧩 Kint az új heti kvíz ({len(qs)} kérdés): {kc.SITE_URL}/kviz/",
                                  "reply_markup": {"inline_keyboard": [[{"text": "🗑 Kvíz leszedése",
                                                                        "callback_data": f"qdel|{quiz['id']}"}]]}})
    except Exception as e:  # noqa: BLE001
        log.warning("Kvíz-értesítés kimaradt: %s", e)
    return 1
