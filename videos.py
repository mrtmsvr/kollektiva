#!/usr/bin/env python3
"""
Kollektíva – videó- és podcastfigyelő
=====================================

Figyeli a megadott YouTube-csatornák új videóit (a csatornák ingyenes RSS-éből), a Gemini „megnézi” a videót
(hang + kevés képkocka), és részletes, idézetekkel ellátott magyar összefoglalót ad. Ebből a szokásos cikkíró
folyamat (lektor, tényellenőrzés, idézetellenőrzés, képek) cikket ír, a cikkbe beágyazzuk az eredeti videót
(hivatalos YouTube-lejátszó – ez jogszerű, és a forrásnak is jó). A cikk Telegramra megy, és CSAK jóváhagyással kerül ki.

Csatornák: data/video_sources.json (Telegramon: /csatorna <YouTube-link vagy @név>, /csatornak).
Napi keret: VIDEO_DAILY_MAX (alap 3), futásonként legfeljebb 1 videó; óránként egyszer néz rá a csatornákra.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.videos")
SOURCES_FILE = kc.BASE_DIR / "data" / "video_sources.json"
STATE_FILE = kc.BASE_DIR / "data" / "video_state.json"
DEFAULT_SOURCES = [
    {"name": "Partizán", "channel_id": "UCEFpEvuosfPGlV1VyUF6QOA"},
    {"name": "Telex", "handle": "Telexponthu"},
    {"name": "444", "handle": "444hu"},
]
UA = {"User-Agent": "Mozilla/5.0 (compatible; KollektivaBot/1.0; +https://xn--kollektva-m5a.hu)",
      "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.5"}
SKIP = re.compile(r"#shorts|\bshorts?\b|előzetes|trailer|élő adás indul|premier|stream starts|reklám", re.I)

VIDEO_SYSTEM = (
    "Egy magyar online magazin (Kollektíva) hírszerkesztője vagy. Megnézel egy videót/podcastot, és a cikkíróknak "
    "pontos, tényszerű anyagot készítesz belőle. SZABÁLYOK: csak azt írd le, ami a videóban tényleg elhangzik vagy "
    "látszik; ne egészítsd ki saját tudással. Szó szerinti idézetet csak akkor adj, ha biztosan érthető volt; ha egy "
    "név, szám vagy mondat félrehallható/bizonytalan, azt tedd az „uncertain” listába, és a szövegben ne állítsd "
    "biztosnak. Mindig írd oda, KI mondja (név, szerep). Csak érvényes JSON-t adsz vissza."
)


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600_000).decode("utf-8", "ignore")


def load_sources() -> list:
    return kc.read_json(SOURCES_FILE, {"sources": DEFAULT_SOURCES}).get("sources", DEFAULT_SOURCES)


def save_sources(src: list) -> None:
    kc.write_json_atomic(SOURCES_FILE, {"sources": src})


def resolve_channel(src: dict) -> Optional[str]:
    """@név -> csatornaazonosító (UC…), a csatornaoldalból; az eredményt eltároljuk."""
    if src.get("channel_id"):
        return src["channel_id"]
    h = str(src.get("handle") or "").lstrip("@")
    if not h:
        return None
    try:
        page = _get(f"https://www.youtube.com/@{urllib.parse.quote(h)}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Csatorna nem érhető el (@%s): %s", h, e)
        return None
    m = (re.search(r'"externalId":"(UC[\w-]{22})"', page) or re.search(r'"channelId":"(UC[\w-]{22})"', page)
         or re.search(r"/channel/(UC[\w-]{22})", page))
    if m:
        src["channel_id"] = m.group(1)
        return m.group(1)
    log.warning("Nem találom a csatornaazonosítót: @%s", h)
    return None


def parse_source(text: str) -> Optional[dict]:
    """Telegramos /csatorna paraméter: YouTube-link (csatorna vagy videó) vagy @név."""
    t = text.strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", t)
    if m:
        return {"name": m.group(1), "channel_id": m.group(1)}
    m = re.search(r"(?:youtube\.com/)?@([\w.\-]+)", t)
    if m:
        return {"name": m.group(1), "handle": m.group(1)}
    return None


def latest_videos(channel_id: str) -> list:
    try:
        xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
        root = ET.fromstring(xml)
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as e:
        log.warning("Videólista nem olvasható (%s): %s", channel_id, e)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in root.findall("a:entry", ns):
        vid = (e.findtext("yt:videoId", "", ns) or "").strip()
        title = (e.findtext("a:title", "", ns) or "").strip()
        pub = (e.findtext("a:published", "", ns) or "").strip()
        desc = (e.findtext("m:group/m:description", "", ns) or "").strip()
        author = (e.findtext("a:author/a:name", "", ns) or "").strip()
        link = e.find("a:link", ns)
        href = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        if vid and "/shorts/" not in href:
            out.append({"id": vid, "title": title, "published": pub, "description": desc, "author": author,
                        "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def watch(ai: "kc.AIClient", v: dict) -> Optional[dict]:
    """A Gemini megnézi a videót (YouTube-link közvetlenül), és részletes összefoglalót ad idézetekkel."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    models = [m.strip() for m in (ai.cfg.gemini_model or "").split(",") if m.strip()] or ["gemini-2.5-flash"]
    prompt = (f"Videó címe: {v['title']}\nCsatorna: {v.get('author', '')}\nLeírás: {v.get('description', '')[:800]}\n\n"
              "Nézd meg a videót, és add vissza JSON-ben: {\"newsworthy\": true/false (van-e benne hírértékű, "
              "magyar olvasót érdeklő állítás, bejelentés, vita – reklám, zene, előzetes, általános csevegés: false), "
              "\"topic\": \"egy mondat: miről szól\", \"speakers\": [\"név – szerep\"], \"summary\": \"részletes "
              "magyar összefoglaló 500–1200 szóban, a fontos állításokkal, ki mit mondott, a legjobb szó szerinti "
              "idézetekkel „...” jelek között\", \"uncertain\": [\"bizonytalanul érthető nevek/számok/mondatok\"]}")
    body = {"contents": [{"parts": [{"file_data": {"file_uri": v["url"]}, "video_metadata": {"end_offset": "3600s", "fps": 0.1}},
                                    {"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": VIDEO_SYSTEM}]},
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8000,
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
    last = None
    for model in models:
        try:
            data = kc.post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                {"x-goog-api-key": key}, body, 300, 2)
            kc.note_usage("gemini")
            text = "".join(p.get("text", "") for p in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts", []))
            return kc.extract_json(text)
        except Exception as e:  # noqa: BLE001
            kc.note_usage("gemini", ok=False)
            log.warning("Videó feldolgozása sikertelen (%s, %s): %s", v["id"], model, str(e)[:200])
            last = e
    log.warning("Videó kimarad: %s", last)
    return None


def article_from_video(ai: "kc.AIClient", v: dict, tz: ZoneInfo, articles: list) -> Optional[dict]:
    info = watch(ai, v)
    if not info or not info.get("newsworthy") or len(str(info.get("summary") or "")) < 400:
        log.info("Videó: nem hírértékű vagy kevés tartalom – %s", v["title"][:80])
        return None
    unsure = [str(u) for u in info.get("uncertain") or [] if str(u).strip()][:10]
    fulltext = (f"[Videó/podcast: {v.get('author', '')} – {v['title']}]\nSzereplők: {', '.join(info.get('speakers') or [])}\n\n"
                + str(info["summary"])
                + (("\n\nBIZONYTALANUL ÉRTHETŐ RÉSZEK (ezeket ne állítsd biztosnak, ne idézd): " + "; ".join(unsure)) if unsure else ""))
    now = datetime.now(tz)
    story = [{"title": v["title"], "link": v["url"], "summary": str(info.get("topic") or "")[:600],
              "source": v.get("author") or "YouTube", "published": now, "categories": [], "fulltext": fulltext}]
    story[0]["kw"] = kc._keywords(story[0]["title"] + " " + story[0]["summary"])
    section = kc.pick_section(ai, story, kc.SECTIONS.get("kozelet"))
    recent_imgs = {(a.get("hero_image") or {}).get("url") for a in articles[:60]} - {None}
    art = kc.build_section_article(ai, section, now.date(), tz, story, recent_imgs, kc.related_past(articles, story))
    if not art:
        return None
    art.update({"status": "pending", "hold": True, "video": {"id": v["id"], "title": v["title"], "channel": v.get("author", "")},
                "hot_score": max(art.get("hot_score") or 0, 5)})
    return art


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    if os.getenv("VIDEOS_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    st = kc.read_json(STATE_FILE, {"seen": [], "last_check": "", "done": {}})
    if st.get("last_check") and now - datetime.fromisoformat(st["last_check"]) < timedelta(minutes=int(os.getenv("VIDEO_CHECK_MIN", "60"))):
        return 0
    st["last_check"] = now.isoformat(timespec="seconds")
    today = d.isoformat()
    done_today = int((st.get("done") or {}).get(today, 0))
    seen = set(st.get("seen") or [])
    sources = load_sources()
    cands = []
    for src in sources:
        cid = resolve_channel(src)
        if not cid:
            continue
        for v in latest_videos(cid)[:5]:
            try:
                age = now - datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if v["id"] in seen:
                continue
            if age > timedelta(hours=36) or SKIP.search(v["title"]):
                seen.add(v["id"])  # régi vagy nem cikkbe való – többet nem nézzük
                continue
            cands.append(v)
    save_sources(sources)
    made = 0
    if cands and done_today < int(os.getenv("VIDEO_DAILY_MAX", "3")):
        v = sorted(cands, key=lambda x: x["published"], reverse=True)[0]
        seen.add(v["id"])
        articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
        art = article_from_video(ai, v, tz, articles)
        if art and not dry_run:
            import telegram_review as review
            review.send_article(output_dir, art)
            pending = review.load_pending(output_dir)
            pending.append(art)
            review.save_pending(output_dir, pending)
            st.setdefault("done", {})[today] = done_today + 1
            log.info("✔ Videóból cikk: %s", art["title"])
            made = 1
    st["seen"] = list(seen)[-500:]
    st["done"] = {k: v for k, v in (st.get("done") or {}).items() if k >= (d - timedelta(days=7)).isoformat()}
    if not dry_run:
        kc.write_json_atomic(STATE_FILE, st)
    return made
