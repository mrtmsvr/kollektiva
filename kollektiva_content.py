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
import html
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
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
# A kanonikus webcím (kollektíva.hu punycode alakja). Ékezet nélküli kollektiva.hu MÁS domain!
SITE_URL = os.getenv("SITE_URL", "https://xn--kollektva-m5a.hu").rstrip("/")
SITE_NAME = "Kollektíva"
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
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash"),
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
            # A Gemini API OpenAI-kompatibilis végpontja (ingyenes szint: aistudio.google.com).
            # GEMINI_MODEL vesszővel elválasztott lista is lehet: túlterhelés (503) esetén
            # a következő modellel próbálkozik.
            models = [m.strip() for m in c.gemini_model.split(",") if m.strip()]
            last: Optional[Exception] = None
            for model in models:
                try:
                    data = post_json(
                        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                        {"Authorization": f"Bearer {c.gemini_key}"},
                        {"model": model, "max_tokens": max_tokens,
                         "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": prompt}]},
                        c.http_timeout, c.http_retries)
                    return data["choices"][0]["message"]["content"]
                except Exception as e:  # noqa: BLE001 – következő modell
                    log.warning("Gemini modell sikertelen (%s): %s", model, str(e)[:200])
                    last = e
            raise last or AIError("Nincs megadott Gemini modell")
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
    "Szórakoztató rovatot írsz, amelynek célja, hogy az olvasó úgy érezze: pontosan róla szól. "
    "Természetes, igényes, közérthető magyar nyelven írsz (tegezve); kerülöd a közhelyeket, a "
    "rémisztgetést, az egészségügyi, jogi vagy pénzügyi konkrét tanácsot. Csak érvényes JSON-t adsz vissza."
)


def horoscope_prompt(d: date) -> str:
    signs = ", ".join(f'{s["id"]} ({s["name"]}, {s["element"]})' for s in SIGNS)
    return f"""Írd meg a {hu_date(d)} napi horoszkópot mind a 12 csillagjegyre.

Csillagjegyek (id, név, elem): {signs}

Minden jegyhez:
- "headline": 3–7 szavas, egyedi főcím
- "text": 45–75 szavas (3–4 mondat), személyesnek ható napi szöveg, tegező formában

Írástechnika (Barnum/Forer-hatás – ettől érzi az olvasó személyre szabottnak):
- Olyan állításokat írj, amelyek szinte bárkire igazak, de konkrétnak hatnak
  (pl. „Az utóbbi napokban többször visszatért hozzád egy félbehagyott gondolat.”).
- Használj kétoldalú jellemzést (pl. „kifelé magabiztosnak tűnsz, belül mégis mérlegelsz”).
- Utalj rejtett erősségre vagy ki nem használt lehetőségre, hízelgően, de nem túlzóan.
- Adj egy hétköznapi, felismerhető helyzetet (üzenet, beszélgetés, halogatott teendő, döntés),
  és egy apró, könnyen megtehető javaslatot.
- Időbeli támpont segít (délelőtt, a nap második fele, este).
- Az elem hangulata (tűz, föld, levegő, víz) finoman érződjön, de ne ismételd a jegy nevét.
- Minden jegynél más szerkezettel és más képekkel kezdj; ne ismételj mondatot vagy fordulatot.
- Soha ne jósolj konkrét eseményt, betegséget, pénzösszeget vagy veszteséget.
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
Ellenőrzött tények (lehetnek angolul – a cikket magyarul írd, a neveket a magyar
szakirodalomban szokásos alakjukban):
{facts}

Elvárások:
- "title": figyelemfelkeltő, de nem bulvár cím (max. 12 szó), NE kezdődjön az „Ekkor történt” szavakkal
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


WIKI_EXCLUDE = re.compile(
    r"\b(kill|killed|killing|massacre|bomb|bombing|attack|shoot|shooting|terror|murder|genocide|"
    r"execut|stampede|hostage|suicide|rape|assassinat|explosion|crash|died|dies|death)\w*", re.I)


WIKI_UA = {"User-Agent": "KollektivaBot/1.0 (https://kollektiva.hu; bot@kollektiva.hu)", "Accept": "application/json"}

# Megbízható külső források (a Wikipédia-cikkek hivatkozásaiból válogatva)
TRUSTED_SOURCES = {
    "britannica.com": "Encyclopaedia Britannica", "bbc.co.uk": "BBC", "bbc.com": "BBC",
    "nytimes.com": "The New York Times", "theguardian.com": "The Guardian", "history.com": "History",
    "smithsonianmag.com": "Smithsonian Magazine", "nationalgeographic.com": "National Geographic",
    "loc.gov": "Library of Congress", "archives.gov": "National Archives", "nasa.gov": "NASA",
    "reuters.com": "Reuters", "apnews.com": "Associated Press", "time.com": "TIME",
    "washingtonpost.com": "The Washington Post", "latimes.com": "Los Angeles Times",
    "variety.com": "Variety", "hollywoodreporter.com": "The Hollywood Reporter",
    "rollingstone.com": "Rolling Stone", "nobelprize.org": "Nobel Prize", "un.org": "United Nations",
    "europa.eu": "Európai Unió", "cam.ac.uk": "University of Cambridge", "ox.ac.uk": "University of Oxford",
    "nature.com": "Nature", "science.org": "Science", "esa.int": "ESA", "unesco.org": "UNESCO",
    "rubicon.hu": "Rubicon", "arcanum.com": "Arcanum", "mek.oszk.hu": "Magyar Elektronikus Könyvtár",
    "nemzetiarchivum.hu": "Nemzeti Archívum", "npr.org": "NPR", "theatlantic.com": "The Atlantic",
    "independent.co.uk": "The Independent", "telegraph.co.uk": "The Telegraph", "wsj.com": "The Wall Street Journal",
    "pbs.org": "PBS", "newyorker.com": "The New Yorker", "economist.com": "The Economist", "ft.com": "Financial Times",
    "cbsnews.com": "CBS News", "nbcnews.com": "NBC News", "abcnews.go.com": "ABC News", "olympics.com": "Olympics",
    "fifa.com": "FIFA", "espn.com": "ESPN", "ew.com": "Entertainment Weekly", "people.com": "People",
}
FREE_LICENSE = re.compile(r"^(public domain|pd|cc0|cc[ -]by(-sa)?( \d\.\d)?|cc by(-sa)? \d\.\d.*)", re.I)


def http_get_json(url: str, timeout: int) -> Optional[dict]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=WIKI_UA), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.debug("GET sikertelen: %s (%s)", url, e)
        return None


def _q(title: str) -> str:
    return urllib.parse.quote(title.replace(" ", "_"), safe="")


def wiki_hu_summary(en_title: str, timeout: int) -> Optional[dict]:
    """A magyar Wikipédia megfelelő cikkének kivonata (ha létezik)."""
    data = http_get_json("https://en.wikipedia.org/w/api.php?action=query&format=json&prop=langlinks"
                         f"&lllang=hu&titles={_q(en_title)}", timeout)
    try:
        page = next(iter(data["query"]["pages"].values()))
        hu_title = page["langlinks"][0]["*"]
    except (TypeError, KeyError, IndexError, StopIteration):
        return None
    summ = http_get_json(f"https://hu.wikipedia.org/api/rest_v1/page/summary/{_q(hu_title)}", timeout)
    if not summ or not summ.get("extract"):
        return None
    return {"title": hu_title, "extract": summ["extract"],
            "url": summ.get("content_urls", {}).get("desktop", {}).get("page",
                                                                        f"https://hu.wikipedia.org/wiki/{_q(hu_title)}")}


def wiki_external_sources(en_title: str, timeout: int, limit: int = 2) -> list:
    """A Wikipédia-cikk hivatkozásai közül a megbízható, nem archív külső források."""
    data = http_get_json("https://en.wikipedia.org/w/api.php?action=query&format=json&prop=extlinks"
                         f"&ellimit=500&titles={_q(en_title)}", timeout)
    try:
        links = [l["*"] for l in next(iter(data["query"]["pages"].values())).get("extlinks", [])]
    except (TypeError, KeyError, StopIteration):
        return []
    out, seen = [], set()
    for url in links:
        if not url.startswith("https://") or "web.archive.org" in url or "archive.today" in url:
            continue
        host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc.lower())
        dom = next((d for d in TRUSTED_SOURCES if host == d or host.endswith("." + d)), None)
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append({"url": url, "title": TRUSTED_SOURCES[dom], "publisher": TRUSTED_SOURCES[dom]})
        if len(out) >= limit:
            break
    return out


GRAPHIC_HINT = re.compile(r"logo|wordmark|icon|seal|coat[_ ]of[_ ]arms|flag|emblem|map|diagram|\.svg$", re.I)


def _image_from_info(info: dict, fname: str = "") -> Optional[dict]:
    """Commons imageinfo -> cikk-kép. Csak szabad licenc; logó/grafika 'graphic' típust kap
    (a megjelenítés ilyenkor nem vágja, hanem arányosan, háttérrel illeszti)."""
    meta = info.get("extmetadata", {})
    lic = re.sub(r"<[^>]+>", "", meta.get("LicenseShortName", {}).get("value", "")).strip()
    if not FREE_LICENSE.match(lic):
        return None
    w, h = info.get("width") or 0, info.get("height") or 0
    if w < 300 or h < 150:
        return None  # túl kicsi, pixeles lenne
    fname = fname or info.get("descriptionurl", "").rsplit("/", 1)[-1]
    ratio = w / h if h else 0
    kind = "graphic" if (GRAPHIC_HINT.search(fname) or w < 800 or not 1.2 <= ratio <= 2.2) else "photo"
    artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "ismeretlen szerző"
    return {
        "url": info.get("thumburl") or info["url"],
        "width": info.get("thumbwidth") or w,
        "height": info.get("thumbheight") or h,
        "kind": kind,
        "alt": re.sub(r"<[^>]+>", "", meta.get("ImageDescription", {}).get("value", ""))[:200].strip(),
        "credit": f"{artist[:80]} / Wikimedia Commons",
        "license": lic,
        "source_url": info.get("descriptionurl", ""),
    }


def commons_image(page: dict, timeout: int) -> Optional[dict]:
    """A Wikipédia-oldal fő képe, CSAK ha a Wikimedia Commonson van és szabad licencű."""
    src = (page.get("originalimage") or page.get("thumbnail") or {}).get("source", "")
    if "/commons/" not in src:
        return None  # helyi (pl. fair use) kép – nem használjuk
    fname = urllib.parse.unquote(src.split("/")[-1] if "/thumb/" not in src else src.split("/thumb/")[1].split("/")[2])
    data = http_get_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo"
                         f"&iiprop=url|extmetadata|size&iiurlwidth=1200&titles=File:{_q(fname)}", timeout)
    try:
        info = next(iter(data["query"]["pages"].values()))["imageinfo"][0]
    except (TypeError, KeyError, IndexError, StopIteration):
        return None
    return _image_from_info(info, fname)


def commons_search_image(query: str, timeout: int) -> Optional[dict]:
    """Szabad licencű kép keresése a Wikimedia Commonson (fotót részesít előnyben)."""
    if not query:
        return None
    data = http_get_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search"
                         f"&gsrnamespace=6&gsrlimit=10&gsrsearch={urllib.parse.quote(query)}"
                         "&prop=imageinfo&iiprop=url|extmetadata|size&iiurlwidth=1200", timeout)
    try:
        pages = sorted(data["query"]["pages"].values(), key=lambda p: p.get("index", 99))
    except (TypeError, KeyError):
        return None
    # Csak olyan képet fogadunk el, amelynek fájlneve/leírása tényleg a keresett dologról szól
    # (különben pl. egy ELTE-s hírhez egy indiai előadás képe jönne be).
    def norm(t: str) -> str:
        return t.lower().translate(str.maketrans("áéíóöőúüű", "aeiooouuu"))
    generic = {"with", "from", "that", "this", "university", "building", "people", "city", "photo", "image",
               "picture", "center", "centre", "house", "street", "group", "meeting", "hungary", "hungarian"}
    tokens = [w for w in re.findall(r"\w{4,}", norm(query)) if w not in generic] or re.findall(r"\w{4,}", norm(query))
    need = 1
    graphic = None
    for pg in pages:
        info = (pg.get("imageinfo") or [None])[0]
        if not info:
            continue
        desc = norm(pg.get("title", "") + " " + re.sub(r"<[^>]+>", " ", info.get("extmetadata", {})
                                                          .get("ImageDescription", {}).get("value", ""))[:300])
        if sum(1 for t in tokens if t in desc) < min(need, len(tokens)):
            continue
        img = _image_from_info(info, pg.get("title", ""))
        if img and img["kind"] == "photo":
            return img
        graphic = graphic or img
    return graphic


def wiki_onthisday_event(d: date, http_timeout: int) -> Optional[dict]:
    """Tartalék: a Wikipédia szerkesztők által válogatott „On this day” eseményei (en).
    A tények a Wikipédiából jönnek (esemény + cikkkivonat), az AI csak megfogalmaz."""
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{d.month:02d}/{d.day:02d}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "KollektivaBot/1.0 (https://kollektiva.hu; bot@kollektiva.hu)",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("selected", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("Wikipédia On this day nem elérhető: %s", e)
        return None

    min_age = 25
    candidates = []
    for it in items:
        text, year, pages = it.get("text", ""), it.get("year"), it.get("pages") or []
        if not text or not isinstance(year, int) or year > d.year - min_age or not pages:
            continue
        extracts = [p.get("extract", "") for p in pages[:2] if p.get("extract")]
        if WIKI_EXCLUDE.search(text) or not extracts:
            continue
        score = 2 if re.search(r"Hungar|Budapest", text + " ".join(extracts)) else 0
        candidates.append((score, it, pages, extracts))
    if not candidates:
        log.warning("Nincs megfelelő Wikipédia-esemény erre a napra (%s).", d.strftime("%m-%d"))
        return None
    best = max(c[0] for c in candidates)
    pool = [c for c in candidates if c[0] == best]
    _, it, pages, extracts = seeded_rng("wiki", d.isoformat()).choice(pool)
    main = pages[0]
    title = (main.get("normalizedtitle") or main.get("title", "")).replace("_", " ")
    sources = [{"url": p.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "title": (p.get("normalizedtitle") or p.get("title", "")).replace("_", " "),
                "publisher": "Wikipedia", "license": "CC BY-SA 4.0"}
               for p in pages[:2] if p.get("content_urls")]
    facts = [f'{it["year"]}: {it["text"]}', *extracts]
    image = None
    for pg in pages[:3]:
        t = (pg.get("normalizedtitle") or pg.get("title", "")).replace("_", " ")
        hu = wiki_hu_summary(t, http_timeout)
        if hu:
            facts.append(hu["extract"])
            sources.append({"url": hu["url"], "title": hu["title"], "publisher": "Wikipédia",
                            "license": "CC BY-SA 4.0"})
        sources.extend(wiki_external_sources(t, http_timeout, limit=1))
        if image is None:
            image = commons_image(pg, http_timeout)
    uniq, seen_src = [], set()
    for src in sources:  # azonos kiadó/URL csak egyszer
        key = src["url"] if src.get("publisher", "").startswith("Wiki") else src.get("publisher")
        if key and key not in seen_src:
            seen_src.add(key)
            uniq.append(src)
    sources = uniq
    return {
        "year": it["year"],
        "title": it["text"].rstrip(".")[:120] or title,
        "summary": it["text"],
        "facts": facts,
        "tags": [],
        "sources": sources,
        "image": image,
        "origin": "wikipedia",
        "language": "en",
    }


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
    title = re.sub(r"^\s*ekkor történt\s*[:–-]\s*", "", str(raw["title"]).strip(), flags=re.I)
    return {
        "title": title[:1].upper() + title[1:],
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
    return re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")[:80].rstrip("-")


def build_retro_article(ai: AIClient, d: date, tz: ZoneInfo, events: dict) -> Optional[dict]:
    event = pick_event(events, d)
    if not event:
        log.info("Nincs kurált esemény erre a napra (%s) – Wikipédia-tartalék.", d.strftime("%m-%d"))
        if not ai.enabled:
            log.warning("AI nélkül a Wikipédia-tartalék nem használható – retro cikk kimarad.")
            return None
        event = wiki_onthisday_event(d, ai.cfg.http_timeout)
        if not event:
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
        if event.get("origin") == "wikipedia":
            log.warning("Wikipédia-eseményből AI nélkül nem készül cikk – kimarad.")
            return None
        article = fallback_retro(event)

    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    full_text = " ".join([article["lead"], *article["body"]])
    sources = normalize_sources(event.get("sources", []), now_iso)
    if not sources:
        log.warning("A kurált eseménynek nincs forrása – a séma legalább egyet vár: %s", event["title"])
    is_ai = source.startswith("ai:")
    slug = slugify(f"{d.isoformat()}-{article['title']}")
    auto_publish = os.getenv("RETRO_AUTO_PUBLISH", "false").lower() in ("1", "true", "yes")
    status = "published" if (not is_ai or auto_publish) else "needs_review"
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
        "hero_image": event.get("image"),      # szabad licencű Wikimedia Commons kép (ha van)
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
        "url": f"/retro/{slug}/",              # saját, statikus cikkoldal
        # --- SEO, monetizáció ---
        "seo": {
            "meta_title": article["title"][:60],
            "meta_description": article["lead"][:160],
            "canonical_url": f"{SITE_URL}/retro/{slug}/",
            "og_image": None,
            "noindex": status != "published",  # csak a publikált cikk indexelhető
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
# 3) Statikus oldalak: cikkoldalak, retro archívum, sitemap, hírsitemap, RSS
# ---------------------------------------------------------------------------
# A Cloudflare Pages a repó gyökerét szolgálja ki; a gyökérben lévő `_redirects`
# a /retro/*, /sitemap.xml, /news-sitemap.xml és /feed.xml címeket a public/ alá irányítja.

E = html.escape

PAGE_CSS = """
:root{--night:#0E1024;--vault:#171A36;--line:#2A2D52;--parch:#ECE6D8;--dusk:#9492B3;--brass:#C9A45C}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--night);color:var(--parch);font:17px/1.75 Manrope,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--parch)}a:hover{color:var(--brass)}
header,main,footer{max-width:720px;margin:0 auto;padding:0 20px}
header{display:flex;justify-content:space-between;align-items:center;padding-top:14px;padding-bottom:14px;border-bottom:1px solid var(--line);position:relative}
.menu summary{list-style:none;cursor:pointer;color:var(--dusk);font-size:14px;padding:6px 12px;border:1px solid var(--line);border-radius:999px}
.menu summary::-webkit-details-marker{display:none}
.menu[open] nav{position:absolute;right:20px;top:58px;z-index:10;display:flex;flex-direction:column;gap:10px;min-width:220px;padding:16px 18px;background:var(--vault);border:1px solid var(--line);border-radius:12px}
.menu nav a{color:var(--parch)}
.keypoints{margin:28px 0;padding:16px 20px;border-left:2px solid var(--brass);background:var(--vault);border-radius:0 12px 12px 0}
.keypoints p{margin:0 0 6px;color:var(--brass);font-size:13px;letter-spacing:.12em;text-transform:uppercase}
.keypoints ul{margin:0;padding-left:18px}
article ul li{margin:4px 0;color:rgba(236,230,216,.88)}
details.box summary{cursor:pointer;color:var(--parch);font-weight:600}
.related{margin:48px 0 0}.related h2{font-size:26px;margin-bottom:14px}
.rel-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:18px}
.rel-grid a{text-decoration:none;display:block}.rel-grid img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:var(--vault)}
.rel-grid img.graphic{object-fit:contain;padding:14px;background:#ECE6D8}
.rel-grid .t{margin-top:8px;font-weight:600;line-height:1.35;color:var(--parch)}.rel-grid .k{font-size:12px;color:var(--brass);text-transform:uppercase;letter-spacing:.1em}
.logo{font:600 28px/1 "Cormorant Garamond",Georgia,serif;text-decoration:none}
.menu nav{display:none}nav a{text-decoration:none;font-size:15px}
footer a{color:var(--dusk);margin-right:10px}
.kicker{margin-top:44px;color:var(--brass);font-size:13px;letter-spacing:.14em;text-transform:uppercase}
h1{font:600 clamp(32px,6vw,48px)/1.15 "Cormorant Garamond",Georgia,serif;margin:12px 0 16px}
h2{font:600 28px/1.25 "Cormorant Garamond",Georgia,serif;margin:0 0 6px}
.meta{color:var(--dusk);font-size:14px}
.lead{font-size:20px;line-height:1.6;color:var(--parch)}
blockquote{margin:32px 0;padding-left:18px;border-left:2px solid var(--brass);font:italic 24px/1.4 "Cormorant Garamond",Georgia,serif}
article p{color:rgba(236,230,216,.88)}
.box{margin:40px 0;padding:18px 20px;background:var(--vault);border:1px solid var(--line);border-radius:12px;font-size:14px;color:var(--dusk)}
.box a{color:var(--parch)}
figure{margin:32px 0}figure img{display:block;width:100%;height:auto;max-height:70vh;object-fit:contain;border-radius:12px;background:var(--vault)}
figure.graphic img{max-height:340px;padding:28px;background:#ECE6D8}
figcaption{margin-top:8px;color:var(--dusk);font-size:12px}figcaption a{color:var(--dusk)}
.thumb{display:block;width:100%;aspect-ratio:16/9;height:auto;object-fit:cover;object-position:center;border-radius:10px;margin:10px 0 12px}
.thumb.graphic{object-fit:contain;padding:24px;background:#ECE6D8}
.list{list-style:none;padding:0;margin:32px 0}
.list li{padding:22px 0;border-bottom:1px solid var(--line)}
.year{color:var(--brass);font:600 34px/1 "Cormorant Garamond",Georgia,serif}
footer{margin-top:60px;padding-top:24px;padding-bottom:40px;border-top:1px solid var(--line);color:var(--dusk);font-size:13px}
"""


def _page(title: str, description: str, canonical: str, body: str, head_extra: str = "",
          noindex: bool = False) -> str:
    robots = "noindex,follow" if noindex else "index,follow,max-image-preview:large"
    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)}</title>
<meta name="description" content="{E(description)}">
<meta name="robots" content="{robots}">
<link rel="canonical" href="{E(canonical)}">
<meta name="theme-color" content="#0E1024">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(description)}">
<meta property="og:url" content="{E(canonical)}">
<meta property="og:locale" content="hu_HU">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME} – Retro" href="{SITE_URL}/feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,600;1,500&family=Manrope:wght@400;600&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style>
{head_extra}
</head>
<body>
<header><a class="logo" href="/">{SITE_NAME}</a><details class="menu"><summary aria-label="Rovatok">Rovatok ☰</summary><nav>{NAV_LINKS}</nav></details></header>
<main>
{body}
</main>
<footer>© {datetime.now().year} {SITE_NAME}<br><a href="/">Főoldal</a>{NAV_LINKS}<a href="/feed.xml">RSS</a></footer>
</body>
</html>
"""


def _article_jsonld(a: dict) -> str:
    url = a["seo"]["canonical_url"]
    data = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": a["title"][:110],
        "description": a["lead"],
        "datePublished": a.get("published_at") or a.get("created_at"),
        "dateModified": a.get("updated_at") or a.get("created_at"),
        "inLanguage": "hu-HU",
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "url": url,
        "articleSection": "Retro",
        "keywords": ", ".join(a.get("tags", [])),
        "author": {"@type": "Organization", "name": a["authorship"]["byline"], "url": SITE_URL},
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
        "isBasedOn": [s["url"] for s in a.get("sources", []) if s.get("url")],
    }
    if (a.get("hero_image") or {}).get("url"):
        data["image"] = [a["hero_image"]["url"]]
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>"


BULLET = re.compile(r"^[-•*]\s+")


def _para(p: str) -> str:
    """Bekezdés -> HTML; a „- ” kezdetű sorokból felsorolás lesz."""
    lines = [l.strip() for l in p.split("\n") if l.strip()]
    bullets = [l for l in lines if BULLET.match(l)]
    if not bullets:
        return f"<p>{E(p)}</p>"
    out, items = [], []
    for l in lines:
        if BULLET.match(l):
            items.append("<li>" + E(BULLET.sub("", l)) + "</li>")
        else:
            if items:
                out.append("<ul>" + "".join(items) + "</ul>")
                items = []
            out.append("<p>" + E(l) + "</p>")
    if items:
        out.append("<ul>" + "".join(items) + "</ul>")
    return "".join(out)


def _related_html(related: list) -> str:
    if not related:
        return ""
    cards = []
    for r in related[:4]:
        img = r.get("hero_image") or {}
        sec = SECTIONS.get(r.get("category"), RETRO_SECTION)
        pic = (f'<img class="{E(img.get("kind", "photo"))}" src="{E(img["url"])}" alt="" loading="lazy">'
               if img.get("url") else "")
        cards.append(f'<a href="{E(r["url"])}">{pic}<div class="k">{E(sec["kicker"])}</div>'
                     f'<div class="t">{E(r["title"])}</div></a>')
    return f'<section class="related"><h2>Olvass tovább</h2><div class="rel-grid">{"".join(cards)}</div></section>'


def render_article_page(a: dict, related: Optional[list] = None) -> str:
    year = a.get("category_meta", {}).get("event_year", "")
    section = SECTIONS.get(a.get("category"), RETRO_SECTION)
    paras = "\n".join(_para(p) for p in a.get("body", []))
    kp = [k for k in a.get("key_points") or [] if k]
    keypoints = (f'<div class="keypoints"><p>Röviden</p><ul>{"".join(f"<li>{E(k)}</li>" for k in kp)}</ul></div>'
                 if kp else "")
    quote = f"<blockquote>{E(a['pull_quote'])}</blockquote>" if a.get("pull_quote") else ""
    sources = "".join(
        f'<li><a href="{E(s["url"])}" rel="noopener" target="_blank">{E(s.get("title") or s["url"])}</a>'
        f'{" (" + E(s["publisher"]) + ")" if s.get("publisher") and s.get("publisher") != s.get("title") else ""}</li>'
        for s in a.get("sources", []) if s.get("url"))
    img = a.get("hero_image") or None
    figure = ""
    if img and img.get("url"):
        figure = (f'<figure class="{E(img.get("kind", "photo"))}"><img src="{E(img["url"])}" alt="{E(img.get("alt") or a["title"])}" '
                  f'width="{E(str(img.get("width") or ""))}" height="{E(str(img.get("height") or ""))}" '
                  f'loading="eager" decoding="async">'
                  f'<figcaption>{"Kép" if img.get("kind") == "graphic" else "Fotó"}: <a href="{E(img.get("source_url") or img["url"])}" rel="noopener" '
                  f'target="_blank">{E(img.get("credit", ""))}</a>, {E(img.get("license", ""))}</figcaption></figure>')
    published = (a.get("published_at") or a.get("created_at") or a["date"])[:10]
    body = f"""<article>
<p class="kicker">{E(section["kicker"])}{(" · " + E(str(year))) if year else ""}</p>
<h1>{E(a["title"])}</h1>
<p class="meta">{E(a["authorship"]["byline"])} · <time datetime="{E(published)}">{E(published.replace("-", ". "))}.</time> · {a.get("reading_time_min", 1)} perc olvasás</p>
<p class="lead">{E(a["lead"])}</p>
{figure}
{keypoints}
{quote}
{paras}
</article>
<details class="box"><summary>Források ({len(a.get("sources", []))})</summary><ul>{sources or "<li>—</li>"}</ul></details>
<p><a href="/{section["id"]}/">← Vissza: {E(section["name"])}</a></p>
{_related_html(related or [])}"""
    og = (f'<meta property="og:image" content="{E(img["url"])}">\n<meta property="og:type" content="article">\n'
          if img and img.get("url") else '<meta property="og:type" content="article">\n')
    return _page(f'{a["seo"]["meta_title"] or a["title"]} – {SITE_NAME}', a["seo"]["meta_description"] or a["lead"],
                 a["seo"]["canonical_url"], body, og + _article_jsonld(a), noindex=a["seo"].get("noindex", False))


def render_section_index(section: dict, articles: list) -> str:
    def item(a: dict) -> str:
        img = a.get("hero_image") or {}
        year = a.get("category_meta", {}).get("event_year", "") if section["id"] == "retro" else ""
        return ((f'<li><div class="year">{E(str(year))}</div>' if year else "<li>")
                + (f'<a href="{E(a["url"])}"><img class="thumb {E(img.get("kind", "photo"))}" src="{E(img["url"])}" '
                   f'alt="" loading="lazy"></a>' if img.get("url") else "")
                + f'<h2><a href="{E(a["url"])}">{E(a["title"])}</a></h2>'
                f'<p class="meta">{E(a.get("date_label", ""))} · {a.get("reading_time_min", 1)} perc olvasás</p>'
                f'<p>{E(a["lead"])}</p></li>')
    items = "\n".join(item(a) for a in articles)
    body = (f'<p class="kicker">Rovat</p><h1>{E(section["name"])}</h1>'
            f'<p class="lead">{E(section["tagline"])}</p><ul class="list">{items or "<li>Hamarosan…</li>"}</ul>')
    return _page(f'{section["name"]} – {SITE_NAME}', section["tagline"], f'{SITE_URL}/{section["id"]}/', body)


def _rfc822(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%a, %d %b %Y %H:%M:%S %z")
    except (TypeError, ValueError):
        return ""


def build_static_site(output_dir: Path, tz: ZoneInfo) -> None:
    """A publikált cikkekből (retro + rovatok) statikus oldalakat, sitemapeket és RSS-t generál a public/ alá."""
    public = output_dir.parent  # public/data -> public
    pool = (read_json(output_dir / "retro_articles.json", {"articles": []}).get("articles", [])
            + read_json(output_dir / "articles.json", {"articles": []}).get("articles", []))
    articles = [a for a in pool if a.get("status") == "published" and a.get("slug") and a.get("seo")]
    by_section: dict = {sid: [] for sid in [RETRO_SECTION["id"], *SECTIONS]}
    for a in articles:
        sid = a.get("category") if a.get("category") in SECTIONS else "retro"
        a["url"] = f"/{sid}/{a['slug']}/"
        a["seo"]["canonical_url"] = f"{SITE_URL}/{sid}/{a['slug']}/"
        by_section[sid].append(a)
    articles.sort(key=lambda a: a.get("published_at") or a.get("created_at") or "", reverse=True)
    for sid, items in by_section.items():
        sec_dir = public / sid
        keep = {a["slug"] for a in items}
        if sec_dir.exists():  # már nem publikált cikkek oldalainak törlése
            for child in sec_dir.iterdir():
                if child.is_dir() and child.name not in keep:
                    for f in child.iterdir():
                        f.unlink()
                    child.rmdir()
        for a in items:
            page_dir = sec_dir / a["slug"]
            page_dir.mkdir(parents=True, exist_ok=True)
            related = [r for r in items if r is not a][:2] + [r for r in articles if r.get("category") != a.get("category")][:4]
            (page_dir / "index.html").write_text(render_article_page(a, related[:4]), encoding="utf-8")
        sec_dir.mkdir(parents=True, exist_ok=True)
        section = SECTIONS.get(sid, RETRO_SECTION)
        items.sort(key=lambda a: a.get("published_at") or a.get("created_at") or "", reverse=True)
        (sec_dir / "index.html").write_text(render_section_index(section, items), encoding="utf-8")

    now = datetime.now(tz)
    urls = [(f"{SITE_URL}/", now.date().isoformat())]
    urls += [(f"{SITE_URL}/{sid}/", now.date().isoformat()) for sid in by_section]
    urls += [(a["seo"]["canonical_url"], (a.get("updated_at") or a["date"])[:10]) for a in articles]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sitemap += [f"  <url><loc>{E(u)}</loc><lastmod>{m}</lastmod></url>" for u, m in urls]
    sitemap.append("</urlset>")
    (public / "sitemap.xml").write_text("\n".join(sitemap) + "\n", encoding="utf-8")

    # Google News sitemap: csak az elmúlt 2 nap cikkei
    news = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">']
    for a in articles:
        pub = a.get("published_at") or a.get("created_at")
        try:
            fresh = pub and now - datetime.fromisoformat(pub) <= timedelta(days=2)
        except ValueError:
            fresh = False
        if fresh:
            news.append(f"  <url><loc>{E(a['seo']['canonical_url'])}</loc><news:news>"
                        f"<news:publication><news:name>{SITE_NAME}</news:name><news:language>hu</news:language>"
                        f"</news:publication><news:publication_date>{E(pub)}</news:publication_date>"
                        f"<news:title>{E(a['title'])}</news:title></news:news></url>")
    news.append("</urlset>")
    (public / "news-sitemap.xml").write_text("\n".join(news) + "\n", encoding="utf-8")

    items = "\n".join(
        f"<item><title>{E(a['title'])}</title><link>{E(a['seo']['canonical_url'])}</link>"
        f"<guid isPermaLink=\"true\">{E(a['seo']['canonical_url'])}</guid>"
        f"<pubDate>{_rfc822(a.get('published_at') or a.get('created_at'))}</pubDate>"
        f"<description>{E(a['lead'])}</description></item>" for a in articles[:40])
    rss = (f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
           f"<title>{SITE_NAME}</title><link>{SITE_URL}/</link>"
           f"<description>Kollektíva – online magazin.</description><language>hu</language>\n{items}\n"
           f"</channel></rss>\n")
    (public / "feed.xml").write_text(rss, encoding="utf-8")
    log.info("✔ statikus oldalak: %d cikkoldal, sitemap.xml, news-sitemap.xml, feed.xml", len(articles))


# ---------------------------------------------------------------------------
# 4) Rovatok (Univerzum, Pénzvilág, Tech, Életmód, Kultúra, Közélet)
# ---------------------------------------------------------------------------
# Rovatonként RSS-forrásokból kiválasztja a nap legtöbb forrásban szereplő témáját,
# és a forrásokra hivatkozva SAJÁT megfogalmazású összefoglaló cikket írat.
# Feed: (url, kategória-szűrő regex vagy None, kulcsszó-szűrő regex vagy None)

ECON = (r"forint|árfolyam|infláció|kamat|MNB|jegybank|\bbér|fizetés|nyugdíj|\badó|\bár(ak|a)?\b|drág|olcsó|tőzsde|"
        r"részvény|befektet|hitel|lakás|ingatlan|energiaár|benzin|üzemanyag|gazdaság|GDP|költségvetés|bank|"
        r"megtakarít|euró|dollár|bitcoin|kripto|vállalat|cég|munkanélküli|fogyasztó")
HEALTH = (r"egészs|edzés|mozgás|alvás|táplálkoz|étrend|diéta|vitamin|szív|stressz|mentális|pszich|fogyás|"
          r"elhízás|cukor|kutatás|orvos|betegség|immun|futás|jóga|izom|életmód")
PUBLIC = (r"adat|statisztik|KSH|felmérés|kutatás|oktatás|iskola|egészségügy|kórház|közlekedés|MÁV|BKV|lakhatás|"
          r"nyugdíj|család|népesség|szavazó|választás|törvény|önkormányzat|időjárás|klíma|környezet")
# Minden rovatból kizárt témák: bűnügy, tragédia, háború, konkrét személyek elleni vádak
EXCLUDE_ALL = re.compile(r"gyanú|őrizet|letartóztat|vádemel|vádol|bűncselek|bűnügy|gyilkos|meghalt|halálos|tragédi|"
                         r"holttest|erőszak|bántalmaz|háború|katonai|katona|fegyver|támadás|lövöldöz|robban|"
                         r"öngyilk|botrány|killed|dies|death|war\b|attack", re.I)

RETRO_SECTION = {"id": "retro", "name": "Ekkor történt", "kicker": "Ekkor történt",
                 "tagline": "Minden nap egy történet a múltból."}

SECTIONS = {
    "kozelet": {
        "id": "kozelet", "name": "Közélet", "kicker": "Közélet",
        "tagline": "Pártatlan, adatvezérelt magyarázók a közügyekről.",
        "focus": "közügyek, társadalom, adatok és számok mögötti összefüggések – pártsemlegesen",
        "feeds": [("https://telex.hu/rss", r"Adat", None), ("https://telex.hu/rss", r"Belföld", PUBLIC),
                  ("https://hvg.hu/rss", r"Itthon", PUBLIC)],
    },
    "penzvilag": {
        "id": "penzvilag", "name": "Pénzvilág", "kicker": "Pénzvilág",
        "tagline": "Árfolyamok, infláció, bérek – mit jelentenek a számok a pénztárcádnak.",
        "focus": "gazdaság, pénzügyek, árfolyamok, infláció, bérek, befektetés – a hétköznapi olvasó szemszögéből",
        "feeds": [("https://www.portfolio.hu/rss/all.xml", None, ECON), ("https://www.vg.hu/feed", None, ECON),
                  ("https://telex.hu/rss", r"Gazdaság|Vállalat", ECON)],
    },
    "tech": {
        "id": "tech", "name": "Tech / Jövő", "kicker": "Tech / Jövő",
        "tagline": "Mesterséges intelligencia, eszközök és a digitális élet változásai.",
        "focus": "technológia, mesterséges intelligencia, digitális eszközök, tudomány gyakorlati hatásai",
        "feeds": [("https://telex.hu/rss", r"Techtud", None), ("https://qubit.hu/feed", None, None),
                  ("https://hvg.hu/rss", r"Tech|Tudomány", None)],
    },
    "eletmod": {
        "id": "eletmod", "name": "Életmód & Egészség", "kicker": "Életmód",
        "tagline": "Mozgás, alvás, táplálkozás, mentális jóllét – forrásokkal alátámasztva.",
        "focus": "egészség, mozgás, edzés, alvás, táplálkozás, mentális jóllét – kutatási eredmények érthetően",
        "feeds": [("https://www.sciencedaily.com/rss/health_medicine/fitness.xml", None, None),
                  ("https://www.sciencedaily.com/rss/health_medicine/nutrition.xml", None, None),
                  ("https://telex.hu/rss", r"^Élet$", HEALTH), ("https://hvg.hu/rss", r"Élet|egészség", HEALTH)],
    },
    "kultura": {
        "id": "kultura", "name": "Kultúra & Ajánló", "kicker": "Kultúra",
        "tagline": "Film, sorozat, könyv, zene és programok válogatva.",
        "focus": "film, sorozat, könyv, zene, színház, kiállítás, programajánló",
        "feeds": [("https://telex.hu/rss", r"Kultúra", None), ("https://hvg.hu/rss", r"Kult", None)],
    },
    "univerzum": {
        "id": "univerzum", "name": "Univerzum", "kicker": "Univerzum",
        "tagline": "Csillagászat és űrkutatás, érthetően.",
        "focus": "csillagászat, űrkutatás, bolygók, űrmissziók",
        "feeds": [("https://www.nasa.gov/feed/", None, None),
                  ("https://qubit.hu/feed", None, r"űr|NASA|ESA|bolygó|csillag|galaxis|Hold|Mars|teleszkóp|rakéta|asztro"),
                  ("https://telex.hu/rss", r"Techtud", r"űr|NASA|ESA|bolygó|csillag|galaxis|Hold|Mars|teleszkóp|rakéta")],
    },
}
NAV_LINKS = " ".join(f'<a href="/{sid}/">{html.escape(sec["name"])}</a>'
                     for sid, sec in [*SECTIONS.items(), ("retro", RETRO_SECTION)])
SPONSORED = re.compile(r"PR-cikk|Támogatott|Szponzor|Hirdetés|Közlemény|partner", re.I)
STOPWORDS = set("""a az és is egy hogy nem de már még meg el ki be le fel van volt lesz lett mint
csak ez azt ezt itt ott mit mi ami aki akik kell után alatt miatt szerint között új több nagy the of and
to in for on with from""".split())

SECTION_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) szerkesztője vagy. Friss hírekből írsz SAJÁT "
    "megfogalmazású, magyarázó magazincikket: nem másolsz, nem fordítasz szó szerint, hanem összefoglalsz, "
    "kontextust adsz és elmagyarázod, mit jelent ez az olvasónak. SZIGORÚ SZABÁLY: csak a megadott "
    "forráskivonatokban szereplő tényekre és vitathatatlan, közismert háttérre támaszkodhatsz; nem találsz ki "
    "számot, idézetet, nevet vagy dátumot. Pártpolitikai állást nem foglalsz. Csak érvényes JSON-t adsz vissza."
)


def _text(el: Optional[ET.Element]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(el.text or ""))).strip() if el is not None else ""


_FEED_CACHE: dict = {}


def fetch_feed(url: str, timeout: int) -> list:
    """RSS 2.0 / Atom beolvasása -> [{title, link, summary, published, categories, source}] (futásonként cache-elve)"""
    if url not in _FEED_CACHE:
        _FEED_CACHE[url] = _fetch_feed(url, timeout)
    return [dict(i) for i in _FEED_CACHE[url]]


def _fetch_feed(url: str, timeout: int) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA["User-Agent"],
                                               "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError, ValueError) as e:
        log.warning("Feed nem olvasható: %s (%s)", url, e)
        return []
    host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc)
    items = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        pub = None
        try:
            pub = parsedate_to_datetime(_text(it.find("pubDate")))
        except (TypeError, ValueError):
            pass
        items.append({"title": _text(it.find("title")), "link": _text(it.find("link")),
                      "summary": _text(it.find("description"))[:600], "published": pub,
                      "categories": [_text(c) for c in it.findall("category")], "source": host})
    for it in root.iter(atom + "entry"):
        link = it.find(atom + "link")
        pub = None
        try:
            pub = datetime.fromisoformat(_text(it.find(atom + "updated")).replace("Z", "+00:00"))
        except ValueError:
            pass
        items.append({"title": _text(it.find(atom + "title")), "link": link.get("href", "") if link is not None else "",
                      "summary": _text(it.find(atom + "summary"))[:600], "published": pub,
                      "categories": [c.get("term", "") for c in it.findall(atom + "category")], "source": host})
    return [i for i in items if i["title"] and i["link"].startswith("http")]


def _keywords(text: str) -> set:
    words = re.findall(r"[a-záéíóöőúüű0-9]{4,}", text.lower())
    return {w[:7] for w in words if w not in STOPWORDS}


def pick_story(section: dict, used_links: set, now: datetime, timeout: int, max_age_h: int = 36) -> Optional[list]:
    """A rovat friss híreiből a legtöbb (lehetőleg több kiadónál is szereplő) témát választja; 1–4 forrás."""
    items, seen = [], set()
    for url, cat_re, kw_re in section["feeds"]:
        for it in fetch_feed(url, timeout):
            if it["link"] in seen or it["link"] in used_links:
                continue
            cats = " ".join(it["categories"])
            if cat_re and not re.search(cat_re, cats, re.I):
                continue
            if kw_re and not re.search(kw_re, it["title"] + " " + it["summary"], re.I):
                continue
            if SPONSORED.search(cats + " " + it["title"]) or EXCLUDE_ALL.search(it["title"] + " " + it["summary"][:300]):
                continue
            if it["published"] and (now - it["published"]).total_seconds() > max_age_h * 3600:
                continue
            seen.add(it["link"])
            it["kw"] = _keywords(it["title"] + " " + it["summary"][:200])
            items.append(it)
    if not items:
        return None
    best, best_score = None, -1.0
    for it in items:
        group = [it] + [o for o in items if o is not it and len(it["kw"] & o["kw"]) >= 3]
        publishers = {g["source"] for g in group}
        fresh = 1.0 if it["published"] and (now - it["published"]).total_seconds() < 12 * 3600 else 0.0
        score = len(publishers) * 3 + len(group) + fresh + min(len(it["summary"]), 400) / 400
        if score > best_score:
            best, best_score = group, score
    return best[:4]


def section_prompt(section: dict, story: list, d: date) -> str:
    src = "\n\n".join(f"[{i + 1}] {s['source']} – {s['title']}\n{s['summary']}" for i, s in enumerate(story))
    return f"""Rovat: {section['name']} ({section['focus']}). Dátum: {hu_date(d)}.

Forráskivonatok:
{src}

Írj ebből egy eredeti, magyar nyelvű magazincikket:
- "title": RÖVID (max. 8 szó), ütős, kíváncsiságot keltő cím – de ne hazudjon és ne túlozzon
- "lead": 2 mondatos bevezető: mi történt és miért fontos
- "key_points": 3 rövid, egymondatos pont a lényegről („Röviden” doboz)
- "body": 4–6 bekezdés (tömb), összesen kb. 350–600 szó: a tények, a háttér, és hogy mit jelent ez a
  hétköznapi olvasónak. A cikk elején nevezd meg a forrást a szövegben is (pl. „– írja a Telex.”).
  Ha személy szerepel, első említéskor egy rövid jelzővel mutasd be, ki ő (pl. „Kovács Anna, az MNB
  alelnöke”) – csak ha ez a forrásból kiderül. Ahol illik, egy bekezdés lehet felsorolás: sorok „- ” jellel.
  Ha a forrásokból nem derül ki valami, ne találgass.
- "tags": 3–5 rövid címke
- "image_query": 1–4 szavas ANGOL keresőkifejezés a Wikimedia Commonshoz: ha a hír egy konkrét, ismert
  személyről, helyről, intézményről vagy tárgyról szól, AZ legyen (pl. "Hungarian Parliament Building",
  "James Webb Space Telescope", "Viktor Orbán", "Eötvös Loránd University"); különben egy kifejező, konkrét téma
  (pl. "Budapest Stock Exchange"). Magyar hírnél magyar helyszínt/intézményt keress, ne általános külföldi képet.
- "image_query_alt": 1–2 további, tágabb angol keresőkifejezés tartaléknak (pl. ["ELTE Budapest", "Budapest university"])

Kizárólag ezt a JSON-t add vissza:
{{"title": "...", "lead": "...", "key_points": ["...", "...", "..."], "body": ["...", "..."], "tags": ["..."], "image_query": "...", "image_query_alt": ["..."]}}"""


def build_section_article(ai: AIClient, section: dict, d: date, tz: ZoneInfo, used_links: set) -> Optional[dict]:
    now = datetime.now(tz)
    story = pick_story(section, used_links, now, ai.cfg.http_timeout)
    if not story:
        log.warning("[%s] nincs friss, megfelelő hír a forrásokban – kimarad.", section["id"])
        return None
    try:
        raw = ai.complete_json(SECTION_SYSTEM, section_prompt(section, story, d), 4000)
        art = validate_retro(raw)
    except (AIError, ValueError, TypeError, KeyError) as e:
        log.error("[%s] AI cikkírás sikertelen: %s – kimarad.", section["id"], e)
        return None
    image = None
    for q in [raw.get("image_query"), *(raw.get("image_query_alt") or [])][:3]:
        image = commons_search_image(str(q or "").strip()[:60], ai.cfg.http_timeout)
        if image:
            break
    now_iso = now.isoformat(timespec="seconds")
    slug = slugify(f"{d.isoformat()}-{art['title']}")
    sources = normalize_sources([{"url": s["link"], "title": s["title"], "publisher": s["source"]} for s in story], now_iso)
    full_text = " ".join([art["lead"], *art["body"]])
    auto_publish = os.getenv("RETRO_AUTO_PUBLISH", "false").lower() in ("1", "true", "yes")
    status = "published" if auto_publish else "needs_review"
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{SITE_URL}/{section['id']}/{slug}")),
        "slug": slug, "status": status, "category": section["id"], "subcategory": None,
        "tags": art["tags"], "title": art["title"], "subtitle": None, "lead": art["lead"],
        "key_points": [str(k).strip() for k in (raw.get("key_points") or []) if str(k).strip()][:4],
        "content": to_markdown(art), "content_format": "markdown", "body": art["body"], "pull_quote": None,
        "reading_time_min": reading_time(full_text), "word_count": len(re.findall(r"\w+", full_text)),
        "locale": "hu-HU", "hero_image": image, "sources": sources,
        "authorship": {"mode": "ai_generated", "byline": "Kollektíva szerkesztőség", "model": ai.label.split(":", 2)[-1],
                       "prompt_version": "section-v1", "reviewed_by": None, "reviewed_at": None},
        "category_meta": {"source_links": [s["link"] for s in story]},
        "date": d.isoformat(), "date_label": f"{HU_MONTHS[d.month - 1]} {d.day}.",
        "url": f"/{section['id']}/{slug}/",
        "seo": {"meta_title": art["title"][:60], "meta_description": art["lead"][:160],
                "canonical_url": f"{SITE_URL}/{section['id']}/{slug}/", "og_image": image["url"] if image else None,
                "noindex": status != "published", "schema_type": "NewsArticle"},
        "monetization": {"ads_enabled": True, "brand_safety": "safe", "sponsored": False,
                         "sponsor_name": None, "affiliate_links": False},
        "related_ids": [], "dedupe_hash": hashlib.sha256(f"{section['id']}|{story[0]['link']}".encode()).hexdigest(),
        "pipeline_run_id": os.getenv("GITHUB_RUN_ID"), "generator": ai.label,
        "created_at": now_iso, "updated_at": now_iso, "published_at": now_iso if status == "published" else None,
        "expires_at": None,
    }


def run_sections(ai: AIClient, d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool) -> int:
    """Minden rovathoz legfeljebb egy új cikk naponta (SECTION_IDS / ARTICLES_PER_SECTION env)."""
    if not ai.enabled:
        log.warning("Rovatcikkekhez AI kell – kimarad.")
        return 0
    path = output_dir / "articles.json"
    data = read_json(path, {"articles": []})
    articles = data.get("articles", [])
    used_links = {l for a in articles for l in a.get("category_meta", {}).get("source_links", [])}
    wanted = [x.strip() for x in os.getenv("SECTION_IDS", ",".join(SECTIONS)).split(",") if x.strip() in SECTIONS]
    per_section = int(os.getenv("ARTICLES_PER_SECTION", "1"))
    pause = int(os.getenv("AI_PAUSE_SECONDS", "8"))
    made = 0
    for sid in wanted:
        have = sum(1 for a in articles if a.get("category") == sid and a.get("date") == d.isoformat())
        for _ in range(max(0, per_section - have)):
            art = build_section_article(ai, SECTIONS[sid], d, tz, used_links)
            if not art:
                break
            used_links.update(art["category_meta"]["source_links"])
            articles.insert(0, art)
            made += 1
            log.info("✔ [%s] \"%s\" (%s forrás, kép: %s)", sid, art["title"], len(art["sources"]),
                     (art["hero_image"] or {}).get("kind", "nincs"))
            time.sleep(pause)  # ingyenes AI-keret: ne fussunk bele a percenkénti limitbe
    articles.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    if made and not dry_run:
        write_json_atomic(path, {"schema_version": 1, "updated_at": datetime.now(tz).isoformat(timespec="seconds"),
                                 "articles": articles[:int(os.getenv("ARTICLES_ARCHIVE_LIMIT", "300"))]})
    log.info("Rovatcikkek: %d új", made)
    return made


# ---------------------------------------------------------------------------
# Fő futtató
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kollektíva napi tartalomgenerátor")
    p.add_argument("--date", help="Cél dátum ÉÉÉÉ-HH-NN (alapból a mai nap a site időzónájában)")
    p.add_argument("--provider", choices=["auto", "anthropic", "gemini", "openai", "mock"], help="AI_PROVIDER felülírása")
    p.add_argument("--only", choices=["horoscope", "retro", "sections"], help="Csak az egyik modul futtatása")
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

    force = os.getenv("FORCE_REGENERATE", "false").lower() in ("1", "true", "yes")
    # Ami egyszer kikerült, az nem változik: a mai horoszkóp/retro cikk csak akkor készül, ha még nincs
    # (vagy ha csak AI nélküli tartalék-tartalom van). FORCE_REGENERATE=true felülírja.
    existing_h = read_json(cfg.output_dir / "horoscope.json", {})
    skip_h = (not force and existing_h.get("date") == target.isoformat()
              and str(existing_h.get("source", "")).startswith("ai:"))
    if skip_h:
        log.info("A mai horoszkóp már kint van – nem generálom újra.")
    if args.only in (None, "horoscope") and not skip_h:
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

    existing_r = read_json(cfg.output_dir / "retro_articles.json", {"articles": []}).get("articles", [])
    skip_r = not force and any(a.get("date") == target.isoformat() and a.get("status") == "published"
                               and str(a.get("generator", "")).startswith("ai:") for a in existing_r)
    if skip_r:
        log.info("A mai retro cikk már kint van – nem generálom újra.")
    if args.only in (None, "retro") and not skip_r:
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

    if args.only in (None, "sections"):
        try:
            run_sections(ai, target, tz, cfg.output_dir, args.dry_run)
        except OSError as e:
            log.exception("articles.json írása sikertelen: %s", e)
            exit_code = 1

    if not args.dry_run:
        try:
            build_static_site(cfg.output_dir, tz)
        except (OSError, KeyError, TypeError, ValueError) as e:
            log.exception("Statikus oldalak generálása sikertelen: %s", e)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
