#!/usr/bin/env python3
"""
Kollektíva – saját („off-topic”) magazincikkek
=============================================

A hírek (on-topic) mellett naponta egy saját, időtálló, mélyebb cikk készül egy előre összeállított témalistából
(data/offtopic_topics.json): életmód, univerzum, tech, kultúra, pénz. A cikk a magyar (és ha kell, angol)
Wikipédia szövegére épül – ezek a források a cikk alatt is megjelennek –, nem hírforrásra.

Időzítés: a cikk Telegramra megy (🗓 SAJÁT). Ha nem nyúlsz hozzá, CSENDES időszakban kerül ki: legkorábban
a generálás után OFFTOPIC_MIN_DELAY_H órával (alap 2), 9 és 21 óra között, amikor az elmúlt OFFTOPIC_QUIET_MIN
percben (alap 90) nem került ki hírcikk – legkésőbb OFFTOPIC_DEADLINE (alap 21:30) órakor. „✅ Kirakom” = azonnal.

Futás: a robot (kollektiva_content.py --if-due) naponta egyszer hívja, 7 és 20 óra között.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.offtopic")
TOPICS_FILE = kc.BASE_DIR / "data" / "offtopic_topics.json"
RUNS_FILE = kc.BASE_DIR / "data" / "robot.json"

OFFTOPIC_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) vezető szerzője vagy. Időtálló, alapos, mégis könnyen olvasható "
    "magazincikket írsz egy megadott témáról. SZIGORÚ SZABÁLY: a tényeket a megadott Wikipédia-kivonatokból és "
    "vitathatatlan, közismert tudásból veszed; nem találsz ki számot, tanulmányt, idézetet, nevet vagy dátumot. "
    "Ha valamiben a tudomány bizonytalan, azt kimondod. Egészségügyi témánál nem adsz személyre szabott tanácsot, "
    "és a végén egy mondatban jelzed, hogy a cikk nem helyettesíti az orvosi/szakértői véleményt. "
    "STÍLUS: természetes, élvezetes, újságírói magyar nyelv; változatos mondathossz; konkrét példák, számok, "
    "érdekességek; nincs töltelék, nincs ismétlés, nincsenek gépies fordulatok („fontos megjegyezni”, "
    "„összességében”, „nem csupán… hanem”, „ez rávilágít”). Nem a Wikipédiát foglalod össze szócikk-szerűen: "
    "az olvasó kérdéseire felelsz, saját szerkezettel. Csak érvényes JSON-t adsz vissza."
)


# ---------------------------------------------------------------------------
# Wikipédia
# ---------------------------------------------------------------------------

def wiki_search(query: str, lang: str, timeout: int) -> Optional[str]:
    url = (f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&list=search&srlimit=1"
           f"&srsearch={urllib.parse.quote(query)}")
    data = kc.http_get_json(url, timeout) or {}
    hits = (data.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


def wiki_extract(title: str, lang: str, timeout: int, chars: int = 5000) -> Optional[dict]:
    """A szócikk sima szövege (bevezető + fejezetek) – ha a pontos cím nincs meg, kereséssel."""
    for t in (title, wiki_search(title, lang, timeout)):
        if not t:
            continue
        url = (f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&prop=extracts|info&inprop=url"
               f"&explaintext=1&exsectionformat=plain&redirects=1&titles={urllib.parse.quote(t)}")
        data = kc.http_get_json(url, timeout) or {}
        for page in ((data.get("query") or {}).get("pages") or {}).values():
            text = re.sub(r"\n{3,}", "\n\n", page.get("extract") or "").strip()
            if len(text) > 400:
                return {"title": page.get("title", t), "url": page.get("fullurl") or
                        f"https://{lang}.wikipedia.org/wiki/{kc._q(page.get('title', t))}",
                        "lang": lang, "text": text[:chars]}
    return None


def gather(topic: dict, timeout: int) -> list:
    """Források egy témához: a megadott magyar szócikkek, és ha kevés, az angolok is."""
    out = []
    for t in topic.get("wiki_hu", [])[:3]:
        ex = wiki_extract(t, "hu", timeout)
        if ex and ex["url"] not in {o["url"] for o in out}:
            out.append(ex)
    if sum(len(o["text"]) for o in out) < 4000:
        for t in topic.get("wiki_en", [])[:2]:
            ex = wiki_extract(t, "en", timeout, 6000)
            if ex and ex["url"] not in {o["url"] for o in out}:
                out.append(ex)
    return out


# ---------------------------------------------------------------------------
# Cikk
# ---------------------------------------------------------------------------

def offtopic_prompt(topic: dict, section: dict, sources: list, d: date) -> str:
    src = "\n\n".join(f"[{i + 1}] Wikipédia ({s['lang']}) – {s['title']}\n{s['text']}" for i, s in enumerate(sources))
    return f"""Rovat: {section['name']} ({section['focus']}). Dátum: {kc.hu_date(d)}.
Téma: {topic['topic']}
Szög / amire az olvasó kíváncsi: {topic.get('angle', '')}

Háttéranyag (csak ebből és közismert tudásból dolgozz; angol anyagot magyarul, saját szavaiddal használd):
{src}

Írj egy eredeti, időtálló magyar magazincikket (nem hír, hanem „olvasnivaló”):
- "title": RÖVID (max. 8 szó), kíváncsiságot keltő, de igaz cím (kérdés, meglepő tény vagy szám is lehet)
- "title_options": 2 további, eltérő stílusú címváltozat, tömbként
- "lead": 2 mondatos bevezető, ami behúzza az olvasót
- "key_points": 4–5 rövid, egymondatos pont a lényegről („Röviden” doboz)
- "body": bekezdések tömbje, 700–1100 szó (4–6 perc olvasás). 3–5 tematikus blokk, mindegyik első bekezdése
  „## Rövid alcím” sorral kezdődjön. Legyen benne: miért érdekes/fontos, hogyan működik, konkrét számok és
  példák, gyakori tévhitek, és hogy mit jelent ez a hétköznapokban. Ahol illik, egy bekezdés lehet felsorolás
  („- ” kezdetű sorok). Ne ismételd a leadet, ne legyen „kerekítő” zárómondat.
- "tags": 3–5 rövid címke
- "image_query": 1–4 szavas ANGOL keresőkifejezés a Wikimedia Commonshoz (konkrét, fotózható tárgy/hely/jelenség)
- "image_query_alt": 1–2 további angol keresőkifejezés
- "image_generic": 1–2 szavas ANGOL, egyszerű hangulatkép-téma

Kizárólag ezt a JSON-t add vissza:
{{"title": "...", "title_options": ["...", "..."], "lead": "...", "key_points": ["..."], "body": ["..."], "tags": ["..."], "image_query": "...", "image_query_alt": ["..."], "image_generic": "..."}}"""


def build_article(ai: "kc.AIClient", topic: dict, d: date, tz: ZoneInfo, avoid_images: Optional[set] = None,
                  sources: Optional[list] = None) -> Optional[dict]:
    section = kc.SECTIONS.get(topic.get("section")) or kc.SECTIONS["eletmod"]
    sources = sources or gather(topic, ai.cfg.http_timeout)
    if not sources:
        log.warning("Off-topic: nincs Wikipédia-anyag ehhez: %s", topic.get("topic"))
        return None
    try:
        raw = ai.complete_json(OFFTOPIC_SYSTEM, offtopic_prompt(topic, section, sources, d), 6000)
        kc.validate_retro(raw)
        raw = kc.editorial_polish(ai, raw)
        art = kc.validate_retro(raw)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.error("Off-topic cikkírás sikertelen (%s): %s", topic.get("id"), e)
        return None
    words = len(re.findall(r"\w+", " ".join(art["body"])))
    if words < 450:
        log.warning("Off-topic: túl rövid lett (%d szó) – kimarad.", words)
        return None
    generic = raw.get("image_generic")
    image_options = kc.find_images([raw.get("image_query"), *(raw.get("image_query_alt") or []), *topic.get("images", [])][:4],
                                   generic if isinstance(generic, list) else [generic], ai.cfg.http_timeout, avoid_images)
    image = image_options[0] if image_options else None
    title_options = [art["title"]] + [str(t).strip() for t in (raw.get("title_options") or [])
                                      if str(t).strip() and str(t).strip() != art["title"]]
    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    slug = kc.slugify(f"{d.isoformat()}-{art['title']}")
    srcs = kc.normalize_sources([{"url": s["url"], "title": s["title"], "publisher": f"Wikipédia ({s['lang']})",
                                  "license": "CC BY-SA 4.0"} for s in sources], now_iso)
    full_text = " ".join([art["lead"], *art["body"]])
    sid = section["id"]
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kc.SITE_URL}/{sid}/{slug}")),
        "slug": slug, "status": "pending", "category": sid, "subcategory": "olvasnivalo",
        "tags": art["tags"], "title": art["title"], "subtitle": None, "lead": art["lead"],
        "key_points": [str(k).strip() for k in (raw.get("key_points") or []) if str(k).strip()][:5],
        "content": kc.to_markdown(art), "content_format": "markdown", "body": art["body"], "pull_quote": None,
        "reading_time_min": kc.reading_time(full_text), "word_count": len(re.findall(r"\w+", full_text)),
        "locale": "hu-HU", "hero_image": image, "sources": srcs,
        "authorship": {"mode": "ai_generated", "byline": "Kollektíva szerkesztőség", "model": ai.label.split(":", 2)[-1],
                       "prompt_version": "offtopic-v1", "reviewed_by": None, "reviewed_at": None},
        "hot_score": 0, "offtopic": True, "offtopic_topic": topic,
        "title_options": title_options[:3], "image_options": image_options,
        "category_meta": {"source_links": [s["url"] for s in sources], "offtopic_id": topic.get("id")},
        "date": d.isoformat(), "date_label": f"{kc.HU_MONTHS[d.month - 1]} {d.day}.",
        "url": f"/{sid}/{slug}/",
        "seo": {"meta_title": art["title"][:60], "meta_description": art["lead"][:160],
                "canonical_url": f"{kc.SITE_URL}/{sid}/{slug}/", "og_image": image["url"] if image else None,
                "noindex": False, "schema_type": "Article"},
        "monetization": {"ads_enabled": True, "brand_safety": "safe", "sponsored": False,
                         "sponsor_name": None, "affiliate_links": False},
        "related_ids": [], "dedupe_hash": hashlib.sha256(f"offtopic|{topic.get('id')}".encode()).hexdigest(),
        "pipeline_run_id": os.getenv("GITHUB_RUN_ID"), "generator": ai.label,
        "created_at": now_iso, "updated_at": now_iso, "published_at": None, "expires_at": None,
    }


# ---------------------------------------------------------------------------
# Időzítés
# ---------------------------------------------------------------------------

def make_schedule(now: datetime) -> dict:
    earliest = now + timedelta(hours=float(os.getenv("OFFTOPIC_MIN_DELAY_H", "2")))
    hh, mm = (int(x) for x in os.getenv("OFFTOPIC_DEADLINE", "21:30").split(":"))
    deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if deadline <= earliest:
        deadline = earliest + timedelta(hours=1)
    return {"earliest": earliest.isoformat(timespec="seconds"), "deadline": deadline.isoformat(timespec="seconds")}


def is_due(art: dict, articles: list, now: datetime) -> bool:
    """Kirakható-e most az időzített saját cikk: legkorábbi időpont után, 9–21 óra között, csendes időszakban
    (az elmúlt OFFTOPIC_QUIET_MIN percben nem került ki hírcikk) – vagy ha elérte a határidőt."""
    sch = art.get("schedule") or {}
    try:
        earliest, deadline = datetime.fromisoformat(sch["earliest"]), datetime.fromisoformat(sch["deadline"])
    except (KeyError, TypeError, ValueError):
        return True
    if now >= deadline:
        return True
    if now < earliest or not 9 <= now.hour <= 21:
        return False
    quiet = timedelta(minutes=int(os.getenv("OFFTOPIC_QUIET_MIN", "90")))
    for a in articles:
        if a.get("offtopic") or a.get("category") not in kc.SECTIONS:
            continue
        try:
            if now - datetime.fromisoformat(a.get("published_at") or a.get("created_at")) < quiet:
                return False
        except (TypeError, ValueError):
            continue
    return True


# ---------------------------------------------------------------------------
# Napi futás
# ---------------------------------------------------------------------------

def next_topic(done: list) -> Optional[dict]:
    topics = kc.read_json(TOPICS_FILE, {"topics": []}).get("topics", [])
    fresh = [t for t in topics if t.get("id") not in set(done)]
    if not fresh:
        return None
    # rovatok váltakozzanak: amelyikből legrégebben volt, az jön
    last_sec = {}
    for i, tid in enumerate(done):
        t = next((x for x in topics if x.get("id") == tid), None)
        if t:
            last_sec[t.get("section")] = i
    fresh.sort(key=lambda t: last_sec.get(t.get("section"), -1))
    return fresh[0]


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    if os.getenv("OFFTOPIC_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    runs = kc.read_json(RUNS_FILE, {})
    if runs.get("last_offtopic_date") == d.isoformat() or not 7 <= now.hour <= 20:
        return 0
    done = runs.get("offtopic_done", [])
    topic = next_topic(done)
    if not topic:
        log.info("Off-topic: elfogyott a témalista.")
        return 0
    articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
    avoid = {(a.get("hero_image") or {}).get("url") for a in articles[:60]} - {None}
    art = build_article(ai, topic, d, tz, avoid)
    # a témát akkor is lezárjuk, ha nem sikerült (ne próbálkozzon vele minden futásnál)
    if not dry_run:
        kc.write_json_atomic(RUNS_FILE, {**kc.read_json(RUNS_FILE, {}), "last_offtopic_date": d.isoformat(),
                                         "offtopic_done": done + [topic["id"]]})
    if not art:
        return 0
    log.info("✔ [off-topic/%s] \"%s\" (%d szó, kép: %s)", art["category"], art["title"], art["word_count"],
             (art["hero_image"] or {}).get("kind", "nincs"))
    if dry_run:
        print(json.dumps({k: art[k] for k in ("title", "title_options", "lead", "key_points", "body")},
                         ensure_ascii=False, indent=2)[:4000])
        return 1
    art["schedule"] = make_schedule(now)
    review = None
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        import telegram_review as review
        if not review.enabled(output_dir):
            review = None
    if review:
        review.send_article(output_dir, art)
        pending = review.load_pending(output_dir)
        pending.append(art)
        review.save_pending(output_dir, pending)
    else:  # Telegram nélkül azonnal kikerül
        art.update({"status": "published", "published_at": art["created_at"]})
        for k in ("title_options", "image_options", "offtopic_topic", "schedule"):
            art.pop(k, None)
        path = output_dir / "articles.json"
        data = kc.read_json(path, {"articles": []})
        kc.write_json_atomic(path, {**data, "articles": [art] + data.get("articles", [])})
    return 1


if __name__ == "__main__":  # kézi próba: python offtopic.py [--dry-run] [--topic ID]
    import argparse
    import sys
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--topic")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    kc.load_dotenv(kc.BASE_DIR / ".env")
    cfg = kc.Config.from_env()
    tz = ZoneInfo(cfg.timezone)
    ai = kc.AIClient(cfg)
    if args.topic:
        t = next((x for x in kc.read_json(TOPICS_FILE, {"topics": []})["topics"] if x["id"] == args.topic), None)
        a = build_article(ai, t, datetime.now(tz).date(), tz) if t else None
        print(json.dumps({k: a[k] for k in ("title", "lead", "body", "sources")}, ensure_ascii=False, indent=2) if a else "nincs")
        sys.exit(0)
    sys.exit(0 if run(ai, datetime.now(tz).date(), tz, cfg.output_dir, args.dry_run) >= 0 else 1)
