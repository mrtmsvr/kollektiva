#!/usr/bin/env python3
"""
Kollektíva (kollektiva.hu) – napi tartalomautomatizálás
=======================================================

Két JSON-t állít elő a statikus oldalnak:
  • horoscope.json        – a mai nap horoszkópja mind a 12 jegyre
  • retro_articles.json   – "Ekkor történt" cikkek archívuma (legújabb elöl)

A cikkek a Kollektíva cikk-sémáját követik (v1: id, slug, status, category,
title, lead, content, sources[], authorship, seo, monetization, category_meta…),
így ugyanaz a szerkezet később változtatás nélkül mehet Supabase-be / Vercelre.

Működés:
  - Ha van API-kulcs (Anthropic vagy OpenAI), AI-val ír szöveget.
  - Ha nincs kulcs vagy az API hibázik: logol, és beépített fallback tartalmat
    használ – a futás SOHA nem áll le emiatt.
  - A retro cikkek TÉNYEI a kurált `data/retro_events.json` fájlból jönnek;
    az AI csak megfogalmaz, nem talál ki eseményt. Ha egy napra nincs kurált
    esemény, a cikk kimarad (rossz dátumú/kitalált történelem rosszabb, mint semmi).

Futtatás:
  python kollektiva_content.py                 # mai nap (Europe/Budapest)
  python kollektiva_content.py --date 2026-09-26
  python kollektiva_content.py --provider mock # AI nélkül
  python kollektiva_content.py --dry-run       # csak kiírja, nem ment

Csak a Python standard könyvtárát használja (Python 3.9+), nincs pip install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("kollektiva")


# ---------------------------------------------------------------------------
# Konfiguráció
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Egyszerű .env betöltő (KULCS=érték). A már beállított környezeti
    változókat nem írja felül – így CI-ban a secretek elsőbbséget élveznek."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    provider: str            # auto | anthropic | openai | mock
    anthropic_key: str
    anthropic_model: str
    openai_key: str
    openai_model: str
    gemini_key: str
    gemini_model: str
    output_dir: Path
    events_file: Path
    timezone: str
    retro_archive_limit: int
    http_timeout: int
    http_retries: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            provider=os.getenv("AI_PROVIDER", "auto").lower(),
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            openai_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            gemini_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
            output_dir=(BASE_DIR / os.getenv("OUTPUT_DIR", "public/data")).resolve(),
            events_file=(BASE_DIR / os.getenv("RETRO_EVENTS_FILE", "data/retro_events.json")).resolve(),
            timezone=os.getenv("SITE_TIMEZONE", "Europe/Budapest"),
            retro_archive_limit=int(os.getenv("RETRO_ARCHIVE_LIMIT", "60")),
            http_timeout=int(os.getenv("HTTP_TIMEOUT", "90")),
            http_retries=int(os.getenv("HTTP_RETRIES", "3")),
        )


# ---------------------------------------------------------------------------
# Csillagjegyek
# ---------------------------------------------------------------------------

SIGNS = [
    {"id": "kos",      "name": "Kos",      "symbol": "♈", "element": "tűz",  "dates": "03.21–04.19"},
    {"id": "bika",     "name": "Bika",     "symbol": "♉", "element": "föld", "dates": "04.20–05.20"},
    {"id": "ikrek",    "name": "Ikrek",    "symbol": "♊", "element": "levegő", "dates": "05.21–06.20"},
    {"id": "rak",      "name": "Rák",      "symbol": "♋", "element": "víz",  "dates": "06.21–07.22"},
    {"id": "oroszlan", "name": "Oroszlán", "symbol": "♌", "element": "tűz",  "dates": "07.23–08.22"},
    {"id": "szuz",     "name": "Szűz",     "symbol": "♍", "element": "föld", "dates": "08.23–09.22"},
    {"id": "merleg",   "name": "Mérleg",   "symbol": "♎", "element": "levegő", "dates": "09.23–10.22"},
    {"id": "skorpio",  "name": "Skorpió",  "symbol": "♏", "element": "víz",  "dates": "10.23–11.21"},
    {"id": "nyilas",   "name": "Nyilas",   "symbol": "♐", "element": "tűz",  "dates": "11.22–12.21"},
    {"id": "bak",      "name": "Bak",      "symbol": "♑", "element": "föld", "dates": "12.22–01.19"},
    {"id": "vizonto",  "name": "Vízöntő",  "symbol": "♒", "element": "levegő", "dates": "01.20–02.18"},
    {"id": "halak",    "name": "Halak",    "symbol": "♓", "element": "víz",  "dates": "02.19–03.20"},
]
SIGN_IDS = [s["id"] for s in SIGNS]
HU_MONTHS = ["január", "február", "március", "április", "május", "június", "július",
             "augusztus", "szeptember", "október", "november", "december"]
HU_WEEKDAYS = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]


def hu_date(d: date) -> str:
    return f"{d.year}. {HU_MONTHS[d.month - 1]} {d.day}., {HU_WEEKDAYS[d.weekday()]}"


# ---------------------------------------------------------------------------
# HTTP + AI kliens
# ---------------------------------------------------------------------------

class AIError(Exception):
    """Bármilyen AI-hívási hiba – a hívó oldalon fallbackre váltunk."""


def post_json(url: str, headers: dict, payload: dict, timeout: int, retries: int) -> dict:
    """POST JSON, exponenciális visszalépéssel. 429/5xx és hálózati hiba esetén újrapróbál."""
    body = json.dumps(payload).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = AIError(f"HTTP {e.code}: {detail}")
            if e.code not in (408, 429, 500, 502, 503, 504, 529):
                break  # kliens hiba (pl. rossz kulcs) – nincs értelme újrapróbálni
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = AIError(f"Hálózati/válasz hiba: {e}")
        if attempt < retries:
            wait = 2 ** attempt
            log.warning("AI hívás sikertelen (%s), újra %ss múlva…", last_err, wait)
            time.sleep(wait)
    raise last_err or AIError("Ismeretlen hiba")


class AIClient:
    """Egységes felület Anthropic és OpenAI felé. `provider` = 'mock' esetén nincs hívás."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        p = cfg.provider
        if p == "auto":
            p = ("anthropic" if cfg.anthropic_key else "gemini" if cfg.gemini_key
                 else "openai" if cfg.openai_key else "mock")
        if p == "gemini" and not cfg.gemini_key:
            log.warning("AI_PROVIDER=gemini, de nincs GEMINI_API_KEY – mock mód.")
            p = "mock"
        if p == "anthropic" and not cfg.anthropic_key:
            log.warning("AI_PROVIDER=anthropic, de nincs ANTHROPIC_API_KEY – mock mód.")
            p = "mock"
        if p == "openai" and not cfg.openai_key:
            log.warning("AI_PROVIDER=openai, de nincs OPENAI_API_KEY – mock mód.")
            p = "mock"
        self.provider = p
        log.info("Tartalomforrás: %s", self.label)

    @property
    def enabled(self) -> bool:
        return self.provider != "mock"

    @property
    def label(self) -> str:
        if self.provider == "anthropic":
            return f"ai:anthropic:{self.cfg.anthropic_model}"
        if self.provider == "openai":
            return f"ai:openai:{self.cfg.openai_model}"
        if self.provider == "gemini":
            return f"ai:gemini:{self.cfg.gemini_model}"
        return "fallback"

    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> str:
        c = self.cfg
        if self.provider == "anthropic":
            data = post_json(
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": c.anthropic_key, "anthropic-version": "2023-06-01"},
                {"model": c.anthropic_model, "max_tokens": max_tokens, "system": system,
                 "messages": [{"role": "user", "content": prompt}]},
                c.http_timeout, c.http_retries)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if self.provider == "openai":
            data = post_json(
                "https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {c.openai_key}"},
                {"model": c.openai_model, "max_tokens": max_tokens,
                 "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": prompt}]},
                c.http_timeout, c.http_retries)
            return data["choices"][0]["message"]["content"]
        if self.provider == "gemini":
            # A Gemini API OpenAI-kompatibilis végpontja (ingyenes szint: aistudio.google.com)
            data = post_json(
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                {"Authorization": f"Bearer {c.gemini_key}"},
                {"model": c.gemini_model, "max_tokens": max_tokens,
                 "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": prompt}]},
                c.http_timeout, c.http_retries)
            return data["choices"][0]["message"]["content"]
        raise AIError("Mock módban nincs AI hívás")

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4000) -> dict:
        text = self.complete(system, prompt, max_tokens)
        return extract_json(text)


def extract_json(text: str) -> dict:
    """JSON kinyerése a modell válaszából (kódkerítés és kísérőszöveg eltávolítása)."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise AIError("A válasz nem tartalmaz JSON objektumot")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"Érvénytelen JSON a válaszban: {e}") from e


# ---------------------------------------------------------------------------
# Fájlkezelés
# ---------------------------------------------------------------------------

def write_json_atomic(path: Path, data: Any) -> None:
    """Ideiglenes fájlba ír, majd átnevez – félkész JSON sosem kerül ki az oldalra."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        log.error("Nem olvasható: %s (%s) – alapértelmezett érték", path, e)
        return default


def seeded_rng(*parts: Any) -> random.Random:
    """Determinisztikus véletlen: ugyanarra a napra és jegyre mindig ugyanazt adja."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def reading_time(text: str, wpm: int = 200) -> int:
    """Olvasási idő percben (magyar szövegre ~200 szó/perc)."""
    return max(1, math.ceil(len(re.findall(r"\w+", text)) / wpm))


# ---------------------------------------------------------------------------
# 1) Napi horoszkóp
# ---------------------------------------------------------------------------

HOROSCOPE_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) asztrológiai rovatának szerkesztője vagy. "
    "Igényes, irodalmi, de közérthető magyar nyelven írsz; kerülöd a közhelyeket, a "
    "rémisztgetést, az egészségügyi vagy pénzügyi konkrét tanácsot. Csak érvényes JSON-t adsz vissza."
)


def horoscope_prompt(d: date) -> str:
    signs = ", ".join(f'{s["id"]} ({s["name"]}, {s["element"]})' for s in SIGNS)
    return f"""Írd meg a {hu_date(d)} napi horoszkópot mind a 12 csillagjegyre.

Csillagjegyek (id, név, elem): {signs}

Minden jegyhez:
- "headline": 4–8 szavas, egyedi főcím
- "text": 90–130 szavas, mélyebb, személyes hangvételű napi elemzés (tegező forma)
- "love", "work", "energy": egész szám 1–5
- "focus": egyetlen rövid mondat, a nap kulcsgondolata
- "lucky_color": egy szín magyarul

Kizárólag ezt a JSON szerkezetet add vissza, más szöveget ne:
{{"signs": {{"kos": {{"headline": "...", "text": "...", "love": 3, "work": 4, "energy": 2, "focus": "...", "lucky_color": "..."}}, ... mind a 12 id ...}}}}"""


def validate_horoscope(raw: dict) -> dict:
    """Ellenőrzi és normalizálja az AI kimenetét. Hibás/hiányzó jegynél AIError."""
    signs = raw.get("signs")
    if not isinstance(signs, dict):
        raise AIError("Hiányzik a 'signs' objektum")
    out = {}
    for sid in SIGN_IDS:
        item = signs.get(sid)
        if not isinstance(item, dict) or len(str(item.get("text", ""))) < 150:
            raise AIError(f"Hiányos vagy túl rövid bejegyzés: {sid}")
        out[sid] = {
            "headline": str(item.get("headline", "")).strip(),
            "text": str(item["text"]).strip(),
            "love": min(5, max(1, int(item.get("love", 3)))),
            "work": min(5, max(1, int(item.get("work", 3)))),
            "energy": min(5, max(1, int(item.get("energy", 3)))),
            "focus": str(item.get("focus", "")).strip(),
            "lucky_color": str(item.get("lucky_color", "")).strip(),
        }
    return out


# Fallback: igényes, elemekhez igazított mondatbank, naponta determinisztikusan keverve.
FB_OPENERS = {
    "tűz": ["Ma a lendületed előbb ér célba, mint a kételyeid.",
            "Belső tüzed ma nem kapkodást, hanem irányt kér.",
            "A nap kihívást tartogat, és te ezt nem teherként, hanem meghívásként éled meg."],
    "föld": ["Ma a lassú, biztos lépések hozzák a legtöbbet.",
             "A figyelmed a kézzelfogható dolgok felé fordul, és ez jó iránytű.",
             "Egy régóta halogatott, gyakorlati ügy ma meglepően könnyen a helyére kerül."],
    "levegő": ["Gondolataid ma szokatlanul tiszták, és ezt mások is észreveszik.",
               "Egy beszélgetés ma többet mozdít, mint egy hét tervezgetés.",
               "A kíváncsiságod ma kaput nyit egy új nézőpont felé."],
    "víz": ["Ma az érzéseid pontosabban látnak, mint a logika.",
            "A csend ma nem üresség, hanem válasz.",
            "Egy finom megérzés ma jó irányba terel, ha hagyod."],
}
FB_MIDDLES = [
    "Érdemes most különválasztanod, mi az, amit valóban akarsz, és mi az, amit csak elvárnak tőled.",
    "Egy apró gesztus – egy üzenet, egy visszahívás – most aránytalanul sokat számít.",
    "Ne siesd el a döntést: a délután olyan információt hozhat, ami átrendezi a képet.",
    "A kapcsolataidban most az őszinteség többet ér a diplomáciánál, ha tapintattal teszed.",
    "Munkában a kevesebb most több: egyetlen jól elvégzett feladat felér három félbehagyottal.",
    "Figyelj a testedre is: a pihenés ma nem luxus, hanem befektetés.",
    "Valaki a környezetedben a támogatásodra vár, még ha nem is mondja ki.",
    "A múlt egy darabja ma más megvilágításba kerül, és ez felszabadító lehet.",
]
FB_CLOSERS = [
    "Este adj magadnak időt, hogy a nap tapasztalatai leülepedjenek.",
    "A nap végére kiderül: jó irányba indultál.",
    "Amit ma elengedsz, annak a helyén holnap tér nyílik valami újnak.",
    "Bízz a saját ritmusodban – ma ez a legjobb stratégia.",
]
FB_FOCUS = ["Kevesebb zaj, több figyelem.", "Az egyszerű út most a bölcs út.",
            "Kérdezz, mielőtt ítélsz.", "A türelem is cselekvés.", "Mondd ki, amit gondolsz."]
FB_COLORS = ["mélykék", "arany", "smaragdzöld", "bordó", "gyöngyházfehér", "levendula", "rozsdabarna"]
FB_HEADLINES = ["Csendes erő", "Új irány a láthatáron", "Tisztuló kép", "A bátorság napja",
                "Belső egyensúly", "Váratlan kapuk", "Lassú, biztos lépések", "A szavak súlya"]


def fallback_horoscope(d: date) -> dict:
    out = {}
    for s in SIGNS:
        r = seeded_rng("horoscope", d.isoformat(), s["id"])
        text = " ".join([r.choice(FB_OPENERS[s["element"]]), *r.sample(FB_MIDDLES, 2), r.choice(FB_CLOSERS)])
        out[s["id"]] = {
            "headline": r.choice(FB_HEADLINES), "text": text,
            "love": r.randint(2, 5), "work": r.randint(2, 5), "energy": r.randint(2, 5),
            "focus": r.choice(FB_FOCUS), "lucky_color": r.choice(FB_COLORS),
        }
    return out


def build_horoscope(ai: AIClient, d: date, tz: ZoneInfo) -> dict:
    source = "fallback"
    entries = None
    if ai.enabled:
        try:
            entries = validate_horoscope(ai.complete_json(HOROSCOPE_SYSTEM, horoscope_prompt(d), 6000))
            source = ai.label
        except (AIError, ValueError, TypeError, KeyError) as e:
            log.error("Horoszkóp AI generálás sikertelen: %s – fallback tartalom", e)
    if entries is None:
        entries = fallback_horoscope(d)

    now = datetime.now(tz)
    expires = datetime.combine(d + timedelta(days=1), datetime.min.time(), tz)
    return {
        "date": d.isoformat(),
        "date_label": hu_date(d),
        "generated_at": now.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "period": "daily",
        "source": source,
        "signs": [{**meta, "sign": meta["id"], **entries[meta["id"]]} for meta in SIGNS],
    }


# ---------------------------------------------------------------------------
# 2) "Ekkor történt" – retro cikk
# ---------------------------------------------------------------------------

RETRO_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) retro rovatának írója vagy. Olvasmányos, "
    "atmoszférikus, de pontos magazincikkeket írsz. SZIGORÚ SZABÁLY: kizárólag a megadott "
    "tényekre és általánosan közismert, vitathatatlan háttérinformációra támaszkodhatsz. "
    "Nem találsz ki idézetet, számot, nevet vagy dátumot. Ha valamiben bizonytalan vagy, "
    "hagyd ki. Csak érvényes JSON-t adsz vissza."
)


def retro_prompt(event: dict, d: date) -> str:
    facts = "\n".join(f"- {f}" for f in event.get("facts", []))
    return f"""Írj egy "Ekkor történt" magazincikket erről az eseményről ({HU_MONTHS[d.month - 1]} {d.day}.):

Esemény: {event["title"]} ({event["year"]})
Ellenőrzött tények:
{facts}

Elvárások:
- "title": figyelemfelkeltő, de nem bulvár cím (max. 12 szó)
- "lead": 2–3 mondatos bevezető
- "body": 5–7 bekezdés (tömb), összesen kb. 600–900 szó, magyarul, magazinstílusban
- "pull_quote": egy saját megfogalmazású kiemelés a cikkből (NEM valós személy idézete)
- "tags": 3–5 rövid címke

Kizárólag ezt a JSON-t add vissza:
{{"title": "...", "lead": "...", "body": ["...", "..."], "pull_quote": "...", "tags": ["..."]}}"""


def load_events(path: Path) -> dict:
    events = read_json(path, {})
    if not events:
        log.warning("Nincs kurált eseményfájl vagy üres: %s", path)
    return events


def pick_event(events: dict, d: date) -> Optional[dict]:
    """Az adott naphoz tartozó kurált eseményekből évente rotálva választ."""
    candidates = events.get(f"{d.month:02d}-{d.day:02d}", [])
    if not candidates:
        return None
    return candidates[d.year % len(candidates)]


def validate_retro(raw: dict) -> dict:
    body = raw.get("body")
    if isinstance(body, str):
        body = [p.strip() for p in body.split("\n\n") if p.strip()]
    if not raw.get("title") or not raw.get("lead") or not isinstance(body, list) or len(body) < 3:
        raise AIError("Hiányos retro cikk (title/lead/body)")
    return {
        "title": str(raw["title"]).strip(),
        "lead": str(raw["lead"]).strip(),
        "body": [str(p).strip() for p in body if str(p).strip()],
        "pull_quote": str(raw.get("pull_quote", "")).strip(),
        "tags": [str(t).strip() for t in raw.get("tags", [])][:5],
    }


def fallback_retro(event: dict) -> dict:
    """AI nélkül a kurált tényekből épít rövidebb, de korrekt cikket."""
    facts = event.get("facts", [])
    return {
        "title": event["title"],
        "lead": event.get("summary", facts[0] if facts else ""),
        "body": facts or [event.get("summary", "")],
        "pull_quote": "",
        "tags": event.get("tags", []),
    }


def normalize_sources(raw_sources: list, accessed_at: str) -> list:
    """Forrásokat a séma szerinti objektumokká alakít ({url, publisher, title, license, accessed_at})."""
    out = []
    for src in raw_sources or []:
        if isinstance(src, str):
            src = {"url": src}
        if not isinstance(src, dict) or not src.get("url"):
            continue
        host = re.sub(r"^https?://(www\.)?", "", src["url"]).split("/")[0]
        out.append({
            "url": src["url"],
            "title": src.get("title", ""),
            "publisher": src.get("publisher") or host,
            "license": src.get("license"),
            "accessed_at": src.get("accessed_at", accessed_at),
        })
    return out


def to_markdown(article: dict) -> str:
    """A cikk törzse Markdownban (a séma `content` mezője)."""
    parts = []
    if article.get("pull_quote"):
        parts.append(f"> {article['pull_quote']}")
    parts.extend(article.get("body", []))
    return "\n\n".join(parts)


def slugify(text: str) -> str:
    table = str.maketrans("áéíóöőúüű", "aeiooouuu")
    return re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")[:80]


def build_retro_article(ai: AIClient, d: date, tz: ZoneInfo, events: dict) -> Optional[dict]:
    event = pick_event(events, d)
    if not event:
        log.warning("Nincs kurált esemény erre a napra (%s) – retro cikk kimarad.", d.strftime("%m-%d"))
        return None

    source = "fallback"
    article = None
    if ai.enabled:
        try:
            article = validate_retro(ai.complete_json(RETRO_SYSTEM, retro_prompt(event, d), 4000))
            source = ai.label
        except (AIError, ValueError, TypeError, KeyError) as e:
            log.error("Retro AI generálás sikertelen: %s – fallback cikk", e)
    if article is None:
        article = fallback_retro(event)

    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    full_text = " ".join([article["lead"], *article["body"]])
    sources = normalize_sources(event.get("sources", []), now_iso)
    if not sources:
        log.warning("A kurált eseménynek nincs forrása – a séma legalább egyet vár: %s", event["title"])
    is_ai = source.startswith("ai:")
    slug = slugify(f"{d.year}-{event['title']}")
    status = "needs_review" if is_ai else "published"
    return {
        # --- Azonosítás, állapot ---
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://kollektiva.hu/retro/{slug}")),
        "slug": slug,
        "status": status,
        "category": "retro",
        "subcategory": None,
        "tags": article["tags"],
        # --- Tartalom ---
        "title": article["title"],
        "subtitle": None,
        "lead": article["lead"],
        "content": to_markdown(article),
        "content_format": "markdown",
        "body": article["body"],               # kényelmi mező a statikus frontendnek
        "pull_quote": article["pull_quote"] or None,
        "reading_time_min": reading_time(full_text),
        "word_count": len(re.findall(r"\w+", full_text)),
        "locale": "hu-HU",
        "hero_image": None,                    # később: Fortepan / Wikimedia, licenccel
        # --- Források, szerzőség ---
        "sources": sources,
        "authorship": {
            "mode": "ai_generated" if is_ai else "human",
            "byline": "Kollektíva szerkesztőség",
            "model": source.split(":", 2)[-1] if is_ai else None,
            "prompt_version": "retro-v1" if is_ai else None,
            "reviewed_by": None,
            "reviewed_at": None,
        },
        # --- Rovatspecifikus ---
        "category_meta": {
            "event_year": event["year"],
            "event_date": f"{d.month:02d}-{d.day:02d}",
            "event_title": event["title"],
        },
        "date": d.isoformat(),                 # kényelmi mezők a statikus frontendnek
        "date_label": f"{HU_MONTHS[d.month - 1]} {d.day}.",
        # --- SEO, monetizáció ---
        "seo": {
            "meta_title": article["title"][:60],
            "meta_description": article["lead"][:160],
            "canonical_url": f"https://kollektiva.hu/retro/{slug}",
            "og_image": None,
            "noindex": is_ai,                  # átnézésig ne indexelje a Google
            "schema_type": "Article",
        },
        "monetization": {
            "ads_enabled": True,
            "brand_safety": event.get("brand_safety", "safe"),
            "sponsored": False,
            "sponsor_name": None,
            "affiliate_links": False,
        },
        # --- Pipeline ---
        "related_ids": [],
        "dedupe_hash": hashlib.sha256(f"retro|{event['title'].lower()}|{sources[0]['url'] if sources else ''}".encode()).hexdigest(),
        "pipeline_run_id": os.getenv("GITHUB_RUN_ID"),
        "generator": source,
        "created_at": now_iso,
        "updated_at": now_iso,
        "published_at": now_iso if status == "published" else None,
        "expires_at": None,
    }


def update_retro_archive(path: Path, article: dict, limit: int) -> dict:
    """Hozzáadja a cikket az archívumhoz (azonos napot felülír), legújabb elöl, max `limit` db."""
    data = read_json(path, {"articles": []})
    articles = [a for a in data.get("articles", []) if a.get("date") != article["date"]]
    articles.insert(0, article)
    articles.sort(key=lambda a: a.get("date", ""), reverse=True)
    return {"schema_version": 1, "updated_at": article["updated_at"], "articles": articles[:limit]}


# ---------------------------------------------------------------------------
# Fő futtató
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kollektíva napi tartalomgenerátor")
    p.add_argument("--date", help="Cél dátum ÉÉÉÉ-HH-NN (alapból a mai nap a site időzónájában)")
    p.add_argument("--provider", choices=["auto", "anthropic", "gemini", "openai", "mock"], help="AI_PROVIDER felülírása")
    p.add_argument("--only", choices=["horoscope", "retro"], help="Csak az egyik modul futtatása")
    p.add_argument("--dry-run", action="store_true", help="Nem ír fájlt, csak a kimenetet mutatja")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """Belépési pont cronhoz / GitHub Actionshöz. 0 = siker (fallbackkel is), 1 = írási hiba."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    load_dotenv(BASE_DIR / ".env")
    if args.provider:
        os.environ["AI_PROVIDER"] = args.provider
    cfg = Config.from_env()
    tz = ZoneInfo(cfg.timezone)
    target = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    log.info("Cél dátum: %s | kimenet: %s", target, cfg.output_dir)

    ai = AIClient(cfg)
    exit_code = 0

    if args.only in (None, "horoscope"):
        try:
            horoscope = build_horoscope(ai, target, tz)
            if args.dry_run:
                print(json.dumps(horoscope, ensure_ascii=False, indent=2)[:3000])
            else:
                write_json_atomic(cfg.output_dir / "horoscope.json", horoscope)
                log.info("✔ horoscope.json mentve (%s)", horoscope["source"])
        except OSError as e:
            log.exception("horoscope.json írása sikertelen: %s", e)
            exit_code = 1

    if args.only in (None, "retro"):
        try:
            article = build_retro_article(ai, target, tz, load_events(cfg.events_file))
            if article:
                path = cfg.output_dir / "retro_articles.json"
                archive = update_retro_archive(path, article, cfg.retro_archive_limit)
                if args.dry_run:
                    print(json.dumps(article, ensure_ascii=False, indent=2)[:3000])
                else:
                    write_json_atomic(path, archive)
                    log.info("✔ retro_articles.json mentve: \"%s\" (%s perc, %s, %s)",
                             article["title"], article["reading_time_min"], article["generator"], article["status"])
        except OSError as e:
            log.exception("retro_articles.json írása sikertelen: %s", e)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
