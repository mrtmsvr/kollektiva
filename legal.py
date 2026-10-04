#!/usr/bin/env python3
"""
Kollektíva – jogi ellenőr
=========================

Minden Telegramra küldött cikket átnéz, mielőtt jóváhagyásra kerül, és kockázatot jelez:
  • képjog (licenc hiányzik / nem kereskedelmi / nem módosítható), MI-képmás jelölése,
  • rágalmazás, becsületsértés gyanúja, ártatlanság vélelme (bűnügyi hír feltételes mód nélkül),
  • magánszemély / kiskorú azonosíthatósága, képmás, személyes adat,
  • egészségügyi ígéret, szó szerinti átvétel a forrásból.
Két lépés: gyors szabályalapú szűrés (AI nélkül) + egy könnyű AI-átnézés (a magyar jog fogalmaival).
Magas kockázatnál a cikk nem kerül ki magától (hold), csak kézi ✅ Kirakommal.
Ez figyelmeztetés, nem jogi tanács – kétes esetben ügyvéd.
Kikapcsolás: LEGAL_CHECK=false.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("kollektiva.legal")

LEVELS = {"alacsony": 0, "közepes": 1, "magas": 2}
ICON = {0: "🟢", 1: "🟡", 2: "🔴"}

FREE_LIC = re.compile(r"(?i)\b(cc0|cc[ -]?by(?![- ]?(nc|nd))(-sa)?|public domain|pd|közkincs|pexels|pixabay|saját|"
                      r"nasa|esa)\b")
NC_ND = re.compile(r"(?i)\b(nc|nd|noncommercial|noderivs?)\b")
CRIME = re.compile(r"(?i)\b(gyanúsít\w*|letartóztat\w*|őrizetbe\w*|vádemel\w*|vádlott\w*|bűnös\w*|csal(ás|t|ó)\w*|"
                   r"sikkaszt\w*|vesztegetés\w*|korrupci\w*|megerőszak\w*|gyilkos\w*|megöl\w*|lop(ás|ott)\w*|"
                   r"zsarol\w*|visszaél\w*|hűtlen kezelés\w*)\b")
HEDGE = re.compile(r"(?i)(a gyanú szerint|gyanúsít|állítólag|állítása szerint|a vád szerint|vádirat szerint|"
                   r"ártatlanság vélelme|nem jogerős|jogerős|a rendőrség szerint|az ügyészség szerint|feltehetően|"
                   r"tagadja|vizsgálják)")
MINOR = re.compile(r"(?i)\b(kiskorú\w*|gyerek\w*|gyermek\w*|\d{1,2} éves (fiú|lány)\w*|diák\w*|tanuló\w*)\b")
HEALTH = re.compile(r"(?i)\b(meggyógyít\w*|gyógyítja|garantáltan|100\s?%-ban|csodaszer\w*|rákot gyógyít\w*|"
                    r"helyettesíti a gyógyszer\w*)\b")

SYSTEM = (
    "Magyar sajtójogi lektor vagy egy online lapnál. Egy megjelenés előtti cikket nézel át, és CSAK valódi, a "
    "szövegben ténylegesen szereplő kockázatot jelzel (ne találj ki problémát; ha nincs, üres lista). Szempontok: "
    "rágalmazás / becsületsértés (Btk. 226–227.: valótlan, becsületet sértő tényállítás bizonyíték vagy forrás nélkül); "
    "ártatlanság vélelme (bűnügyben jogerős ítélet nélkül valakit bűnösnek nevezni); személyiségi jogok (Ptk. 2:43–2:45: "
    "magánszemély azonosítása, képmás, magánélet; közszereplőnél a közügyhöz kapcsolódó kritika megengedett); kiskorú "
    "azonosíthatósága (Smtv.); egészségügyi állítás, ami orvosi tanácsnak tűnik vagy gyógyulást ígér; a forrás szó szerinti, "
    "hosszabb átvétele (szerzői jog); idézet, ami nem a megnevezett személytől származhat; uszító, gyűlöletkeltő megfogalmazás. "
    "Minden problémához adj rövid, konkrét javítási javaslatot. Csak érvényes JSON-t adsz vissza."
)


def _lic_issues(art: dict) -> list:
    out = []
    imgs = list(art.get("image_options") or [])
    if art.get("hero_image"):
        imgs.insert(0, art["hero_image"])
    imgs += art.get("inline_images") or []
    seen = set()
    for im in imgs:
        if not isinstance(im, dict) or im.get("url") in seen:
            continue
        seen.add(im.get("url"))
        lic = str(im.get("license") or "")
        if im.get("likeness"):
            if "AI-generált" not in str(im.get("credit", "")):
                out.append((1, "MI-képmás jelölés nélkül – a feliratban legyen: „AI-generált illusztráció”"))
            continue
        if not lic:
            out.append((1, f"kép licenc nélkül ({str(im.get('credit') or im.get('url', ''))[:60]}) – csak ha biztos a felhasználási jog"))
        elif NC_ND.search(lic):
            out.append((1, f"kép licence korlátozott ({lic}) – NC: hirdetéses oldalon kerülendő, ND: nem vágható"))
        elif not FREE_LIC.search(lic):
            out.append((1, f"kép licence ellenőrizendő: {lic[:50]}"))
    return out


def rule_check(art: dict) -> list:
    text = " ".join([art.get("title", ""), art.get("lead", ""), *(art.get("body") or [])])
    out = _lic_issues(art)
    if CRIME.search(text) and not HEDGE.search(text):
        out.append((1, "bűnügyi tartalom feltételes mód nélkül – „a gyanú szerint”, „nem jogerős”, ártatlanság vélelme"))
    if CRIME.search(text) and MINOR.search(text):
        out.append((1, "bűnügy + kiskorú: ne legyen azonosítható (név, iskola, lakóhely, kép)"))
    if HEALTH.search(text):
        out.append((1, "gyógyulást ígérő / túlzó egészségügyi állítás – tompítani, „nem orvosi tanács”"))
    return out


def ai_check(ai, art: dict) -> list:
    if ai is None or not getattr(ai, "enabled", False):
        return []
    body = "\n".join(art.get("body") or [])[:7000]
    srcs = ", ".join(s.get("publisher") or s.get("url", "") for s in art.get("sources") or [])[:400]
    prompt = (f"Rovat: {art.get('category')}\nCím: {art.get('title')}\nBevezető: {art.get('lead', '')}\n"
              f"Források: {srcs}\n\nSzöveg:\n{body}\n\n"
              "JSON: {\"issues\": [{\"risk\": \"alacsony|közepes|magas\", \"quote\": \"a kifogásolt rész (max. 15 szó)\", "
              "\"problem\": \"mi a gond (max. 12 szó)\", \"fix\": \"javítás (max. 15 szó)\"}]} – legfeljebb 4 tétel, "
              "a legsúlyosabb elöl; ha nincs valódi kockázat: {\"issues\": []}.")
    try:
        raw = ai.complete_json(SYSTEM, prompt, 700, light=True)
    except Exception as e:  # noqa: BLE001 – az ellenőrzés hibája nem állíthatja meg a cikket
        log.warning("Jogi ellenőrzés (AI) kimaradt: %s", e)
        return []
    out = []
    for it in (raw.get("issues") or [])[:4] if isinstance(raw, dict) else []:
        if not isinstance(it, dict):
            continue
        lvl = LEVELS.get(str(it.get("risk", "")).strip().lower(), 1)
        q, p, f = (str(it.get(k) or "").strip() for k in ("quote", "problem", "fix"))
        if not p:
            continue
        out.append((lvl, (f"„{q[:110]}” – " if q else "") + p[:120] + (f" → {f[:140]}" if f else "")))
    return out


def review(ai, art: dict) -> dict:
    """{level: 0–2, issues: [str]} – az art["legal"]-ba kerül; magasnál a cikk hold-ot kap."""
    if os.getenv("LEGAL_CHECK", "true").lower() not in ("1", "true", "yes"):
        return {}
    found = rule_check(art) + ai_check(ai, art)
    lines, seen = [], set()
    for lvl, txt in sorted(found, key=lambda x: -x[0]):
        if txt not in seen:
            seen.add(txt)
            lines.append((lvl, txt))
    level = max((lvl for lvl, _ in lines), default=0)
    res = {"level": level, "issues": [f"{ICON[lvl]} {t}" for lvl, t in lines[:6]]}
    if level >= 2 and not art.get("live"):
        art["hold"] = True
    art["legal"] = res
    return res


def summary(res: dict) -> str:
    if not res:
        return ""
    if not res.get("issues"):
        return "⚖️ Jogi ellenőrzés: 🟢 nincs jelzés"
    head = {0: "🟢 alacsony", 1: "🟡 nézd át", 2: "🔴 magas – magától nem kerül ki"}[res.get("level", 0)]
    return "⚖️ Jogi ellenőrzés: " + head + "\n" + "\n".join(res["issues"])


FIX_SYSTEM = (
    "Magyar sajtójogi szerkesztő vagy. Egy cikket javítasz a jogi lektor megjegyzései alapján. CSAK a kifogásolt "
    "részeket módosítod, a lehető legkisebb beavatkozással: forrásmegjelölés („X szerint”, „a … beszámolója alapján”), "
    "feltételes mód („a gyanú szerint”, „állítólag”), ártatlanság vélelme, magánszemély/kiskorú azonosíthatóságának "
    "megszüntetése, túlzó egészségügyi állítás tompítása; ami nem igazolható, azt kihagyod. Új tényt nem írsz bele, "
    "a stílus, a szerkezet és a bekezdések száma marad. Csak érvényes JSON-t adsz vissza."
)


def fix(ai, art: dict) -> dict:
    """A jogi jelzések alapján kijavítja a cikket (cím, bevezető, pontok, szöveg). Visszaad: {changes: [...]} vagy {}."""
    res = art.get("legal") or {}
    issues = res.get("issues") or []
    if ai is None or not getattr(ai, "enabled", False) or not issues:
        return {}
    titles = art.get("title_options") or [art.get("title", "")]
    body = art.get("body") or []
    prompt = ("JOGI MEGJEGYZÉSEK:\n" + "\n".join(issues)
              + "\n\nCIKK (JSON):\n" + json.dumps({"titles": titles, "lead": art.get("lead", ""),
                                                  "key_points": art.get("key_points") or [], "body": body}, ensure_ascii=False)
              + "\n\nAdd vissza ugyanebben a szerkezetben a javított változatot: {\"titles\": [...ugyanannyi cím...], "
                "\"lead\": \"...\", \"key_points\": [...], \"body\": [...ugyanannyi bekezdés...], "
                "\"changes\": [\"mit javítottál, röviden, max. 4 tétel\"]}")
    try:
        raw = ai.complete_json(FIX_SYSTEM, prompt, 4000)
    except Exception as e:  # noqa: BLE001
        log.warning("Jogi javítás sikertelen: %s", e)
        return {}
    nb = [str(p).strip() for p in raw.get("body") or [] if str(p).strip()]
    if not nb or len(nb) < max(1, len(body) - 1):
        return {}  # ha a modell megcsonkítaná a cikket, inkább nem javítunk
    nt = [str(t).strip() for t in raw.get("titles") or [] if str(t).strip()]
    if len(nt) == len(titles):
        art["title_options"] = nt
        art["title"] = nt[0]
    art["lead"] = str(raw.get("lead") or art.get("lead", "")).strip()
    kp = [str(k).strip() for k in raw.get("key_points") or [] if str(k).strip()]
    if kp:
        art["key_points"] = kp[:4]
    art["body"] = nb
    art["content"] = "\n\n".join(([f"> {art['pull_quote']}"] if art.get("pull_quote") else []) + nb)
    art.pop("legal", None)
    return {"changes": [str(c)[:160] for c in raw.get("changes") or []][:4]}
