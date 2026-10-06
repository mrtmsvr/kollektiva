#!/usr/bin/env python3
"""
Kollektíva – automatikus Instagram-posztolás (Instagram API with Instagram Login, Facebook-oldal nélkül)
=====================================================================================================

Naponta legfeljebb IG_DAILY_MAX (alap 3) kirakott cikkből – a legforróbbakból, legalább IG_GAP_H (alap 3) óra
különbséggel, 8 és 21 óra között – álló, 4:5-ös képkártyát készít (főkép + rovat + cím + Kollektíva-logó), és
képaláírással (cím, lead, „link a bióban”, hashtagek) kiteszi az Instagramra.

Menete: 1) kártya + előnézet Telegramon „🚫 Ne menjen ki Instagramra” gombbal, a kártya kikerül az oldalra
(az Instagram csak nyilvános URL-ről tölt le képet); 2) IG_DELAY_MIN (alap 45) perc múlva, ha nem tiltottad le
és a kártya már elérhető, posztol, és Telegramon jelez.

Kell: IG_ACCESS_TOKEN (GitHub Secret). A token kb. 60 napig él: a robot hetente megpróbálja meghosszabbítani,
és ha a lejárat 7 napon belül van, Telegramon szól, hogy generálj újat.
Kézi futtatás: python instagram.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.instagram")
API = "https://graph.instagram.com"
STATE = kc.BASE_DIR / "data" / "ig_state.json"
CARD_DIR = kc.BASE_DIR / "public" / "img" / "ig"
DAILY_MAX = int(os.getenv("IG_DAILY_MAX", "3"))
GAP_H = float(os.getenv("IG_GAP_H", "3"))
DELAY_MIN = int(os.getenv("IG_DELAY_MIN", "45"))
W, H = 1080, 1350
NAVY, BRASS, PARCH = (14, 16, 36), (201, 164, 92), (236, 230, 216)


def token() -> str:
    return os.getenv("IG_ACCESS_TOKEN", "").strip()


def api(path: str, params: dict, post: bool = False) -> dict:
    q = urllib.parse.urlencode({**params, "access_token": token()})
    url = f"{API}/{path.lstrip('/')}"
    req = urllib.request.Request(url, data=q.encode(), method="POST") if post else urllib.request.Request(f"{url}?{q}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Instagram API {e.code}: {body}") from None


def load_state() -> dict:
    return kc.read_json(STATE, {"posts": []})


def save_state(st: dict) -> None:
    st["posts"] = st.get("posts", [])[-200:]
    kc.write_json_atomic(STATE, st)


def _tg():
    try:
        import telegram_review as tr
        chat = tr.load_state().get("chat_id") if os.getenv("TELEGRAM_BOT_TOKEN") else None
        return (tr, chat) if chat else (None, None)
    except Exception:  # noqa: BLE001
        return None, None


def _notify(text: str) -> None:
    tr, chat = _tg()
    if tr:
        tr.tg("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True})


# ---------------------------------------------------------------------------
# Képkártya
# ---------------------------------------------------------------------------

def _pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"], check=False)
    from PIL import Image, ImageDraw, ImageFont  # noqa: F811
    return Image, ImageDraw, ImageFont


def _font(ImageFont, size: int, serif: bool = True):
    names = (["DejaVuSerif-Bold.ttf", "LiberationSerif-Bold.ttf", "NotoSerif-Bold.ttf"] if serif
             else ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"])
    for root in ("/usr/share/fonts/truetype", "/usr/share/fonts"):
        for p in Path(root).rglob("*.ttf") if Path(root).exists() else []:
            if p.name in names:
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def _hero_bytes(art: dict) -> Optional[bytes]:
    hero = art.get("hero_image") or {}
    url = hero.get("url") or ""
    if url.startswith(kc.SITE_URL + "/"):
        p = kc.BASE_DIR / urllib.parse.unquote(url[len(kc.SITE_URL) + 1:])
        if p.exists():
            return p.read_bytes()
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers=kc.WIKI_UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        log.warning("Főkép letöltése sikertelen: %s", e)
        return None


def _wrap(draw, text: str, font, width: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_card(art: dict) -> Path:
    Image, ImageDraw, ImageFont = _pil()
    img = Image.new("RGB", (W, H), NAVY)
    raw = _hero_bytes(art)
    if raw:
        try:
            ph = Image.open(io.BytesIO(raw)).convert("RGB")
            s = max(W / ph.width, H / ph.height)
            ph = ph.resize((int(ph.width * s) + 1, int(ph.height * s) + 1))
            img.paste(ph, ((W - ph.width) // 2, (H - ph.height) // 2))
        except Exception as e:  # noqa: BLE001
            log.warning("Főkép feldolgozása sikertelen: %s", e)
    # alsó sötét átmenet, hogy a cím olvasható legyen
    grad = Image.new("L", (1, H))
    for y in range(H):
        grad.putpixel((0, y), int(max(0, min(1, (y - H * 0.30) / (H * 0.55))) * 235))
    img.paste(Image.new("RGB", (W, H), NAVY), (0, 0), grad.resize((W, H)))
    d = ImageDraw.Draw(img)
    pad = 72
    # cím: a lehető legnagyobb betűvel, legfeljebb 5 sorban
    title = art.get("title", "")
    for size in (84, 76, 68, 60, 54, 48):
        f_title = _font(ImageFont, size)
        lines = _wrap(d, title, f_title, W - 2 * pad)
        if len(lines) <= 5:
            break
    lines = lines[:5]
    lh = int(size * 1.18)
    y = H - 150 - lh * len(lines)
    sec = (kc.SECTIONS.get(art.get("category"), {}) or {}).get("name") or ("Ekkor történt" if art.get("category") == "retro" else "")
    if sec:
        f_k = _font(ImageFont, 30, serif=False)
        d.text((pad, y - 62), sec.upper(), font=f_k, fill=BRASS)
        d.rectangle((pad, y - 18, pad + 90, y - 13), fill=BRASS)
    for i, ln in enumerate(lines):
        d.text((pad, y + i * lh), ln, font=f_title, fill=PARCH)
    f_logo = _font(ImageFont, 44)
    d.text((pad, H - 100), "Kollektíva", font=f_logo, fill=PARCH)
    lw = d.textlength("Kollektíva", font=f_logo)
    d.text((pad + lw + 2, H - 100), ".", font=f_logo, fill=BRASS)
    f_u = _font(ImageFont, 26, serif=False)
    u = "kollektíva.hu"
    d.text((W - pad - d.textlength(u, font=f_u), H - 88), u, font=f_u, fill=(200, 196, 214))
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    out = CARD_DIR / (re.sub(r"[^a-z0-9-]", "", (art.get("slug") or art.get("id", "x")).lower())[:70] + ".jpg")
    img.save(out, "JPEG", quality=88, optimize=True)
    return out


def caption(art: dict) -> str:
    def tag(t: str) -> str:
        return "#" + re.sub(r"[^\wáéíóöőúüű]", "", str(t).lower().replace(" ", ""))
    sec = (kc.SECTIONS.get(art.get("category"), {}) or {}).get("name", "")
    tags = ["#hírek", "#kollektíva"] + ([tag(sec)] if sec else []) + [tag(t) for t in (art.get("tags") or [])[:5]]
    tags = [t for i, t in enumerate(tags) if len(t) > 2 and t not in tags[:i]]
    text = (f"{art.get('title', '')}\n\n{art.get('lead', '')}\n\n"
            "👉 A teljes cikk a kollektíva.hu-n – link a bióban.\n\n" + " ".join(tags[:9]))
    return text[:2150]


# ---------------------------------------------------------------------------
# Ütemezés
# ---------------------------------------------------------------------------

def _pick(articles: list, st: dict, now: datetime) -> Optional[dict]:
    if not 8 <= now.hour < 21:
        return None
    posts = [p for p in st.get("posts", []) if p.get("status") != "cancelled"]
    today = now.date().isoformat()
    if sum(1 for p in posts if (p.get("created") or "").startswith(today)) >= DAILY_MAX:
        return None
    last = max((p.get("created") or "" for p in posts), default="")
    if last and datetime.fromisoformat(last) > now - timedelta(hours=GAP_H):
        return None
    done = {p.get("art_id") for p in st.get("posts", [])}
    fresh = (now - timedelta(hours=8)).isoformat()
    cands = [a for a in articles if a.get("status") == "published" and a.get("id") not in done
             and a.get("hero_image") and (a.get("published_at") or a.get("created_at") or "") >= fresh
             and not (a.get("legal") or {}).get("hold")]
    return max(cands, key=lambda a: a.get("hot_score") or 0) if cands else None


def enqueue(out_dir: Path, st: dict, now: datetime, dry: bool = False) -> bool:
    articles = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
    art = _pick(articles, st, now)
    if not art:
        return False
    card = make_card(art)
    cap = caption(art)
    pid = hashlib.sha1(art["id"].encode()).hexdigest()[:10]
    item = {"id": pid, "art_id": art["id"], "title": art.get("title"), "url": art.get("url"),
            "card": str(card.relative_to(kc.BASE_DIR)), "card_url": f"{kc.SITE_URL}/{card.relative_to(kc.BASE_DIR).as_posix()}",
            "caption": cap, "created": now.isoformat(timespec="seconds"),
            "due": (now + timedelta(minutes=DELAY_MIN)).isoformat(timespec="seconds"), "status": "queued"}
    log.info("Instagram-poszt sorban: %s", art.get("title"))
    if dry:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return True
    st.setdefault("posts", []).append(item)
    try:  # a kártya kerüljön ki az oldalra (az Instagram onnan tölti le)
        (kc.BASE_DIR / "data" / "deploy_now").write_text("1")
    except OSError:
        pass
    tr, chat = _tg()
    if tr:
        tr._send_photo_file(chat, str(card), f"📸 Instagram-poszt kb. {DELAY_MIN} perc múlva:\n{art.get('title')}"[:1000])
        tr.tg("sendMessage", {"chat_id": chat, "text": "Képaláírás:\n\n" + cap[:3500],
                              "reply_markup": {"inline_keyboard": [[{"text": "🚫 Ne menjen ki Instagramra",
                                                                    "callback_data": f"igno|{pid}"}]]}})
    return True


def cancel(pid: str) -> bool:
    st = load_state()
    for p in st.get("posts", []):
        if p.get("id") == pid and p.get("status") == "queued":
            p["status"] = "cancelled"
            save_state(st)
            return True
    return False


def _live(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (KollektivaBot)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200 and "image" in (r.headers.get("content-type") or "")
    except Exception:  # noqa: BLE001
        return False


def publish_due(st: dict, now: datetime) -> int:
    n = 0
    for p in st.get("posts", []):
        if p.get("status") != "queued" or (p.get("due") or "") > now.isoformat():
            continue
        if not _live(p["card_url"]):
            if datetime.fromisoformat(p["created"]) < now - timedelta(hours=6):
                p["status"] = "failed"
                p["error"] = "a kártya 6 óra alatt sem került ki az oldalra"
                _notify(f"⚠️ Instagram: nem ment ki ({p['error']}): {p.get('title')}")
            continue
        try:
            uid = api("me", {"fields": "user_id,username"}).get("user_id")
            c = api(f"{uid}/media", {"image_url": p["card_url"], "caption": p["caption"]}, post=True)
            cid = c.get("id")
            for _ in range(10):  # a feltöltött képet az Instagram feldolgozza
                s = api(cid, {"fields": "status_code"}).get("status_code")
                if s in ("FINISHED", "ERROR", "EXPIRED"):
                    break
                time.sleep(3)
            r = api(f"{uid}/media_publish", {"creation_id": cid}, post=True)
            p.update({"status": "posted", "media_id": r.get("id"), "posted": now.isoformat(timespec="seconds")})
            n += 1
            _notify(f"✅ Kint van Instagramon: {p.get('title')}")
        except Exception as e:  # noqa: BLE001
            p["status"] = "failed"
            p["error"] = str(e)[:300]
            log.warning("Instagram-posztolás sikertelen: %s", e)
            _notify(f"⚠️ Instagram-posztolás sikertelen: {p.get('title')}\n{str(e)[:300]}")
    return n


def check_token(st: dict, now: datetime) -> None:
    """Hetente meghosszabbítja a tokent; ha a lejárat 7 napon belül van, szól. A tokent nem tároljuk, csak a lenyomatát."""
    fp = hashlib.sha256(token().encode()).hexdigest()[:12]
    tk = st.setdefault("token", {})
    if tk.get("fp") != fp:
        st["token"] = tk = {"fp": fp, "first_seen": now.isoformat(timespec="seconds")}
    if (tk.get("checked") or "") > (now - timedelta(days=7)).isoformat():
        return
    tk["checked"] = now.isoformat(timespec="seconds")
    try:
        r = api("refresh_access_token", {"grant_type": "ig_refresh_token"})
        if r.get("access_token") == token() and r.get("expires_in"):
            tk["expires"] = (now + timedelta(seconds=int(r["expires_in"]))).isoformat(timespec="seconds")
    except Exception as e:  # noqa: BLE001
        log.warning("Instagram-token frissítése: %s", e)
    exp = tk.get("expires") or (datetime.fromisoformat(tk["first_seen"]) + timedelta(days=59)).isoformat()
    if exp < (now + timedelta(days=7)).isoformat() and not tk.get("warned"):
        tk["warned"] = True
        _notify(f"⏳ Az Instagram-token kb. {exp[:10]}-én lejár. Generálj újat (Meta → Kollektíva poster → API setup with "
                "Instagram login → Generate token), és cseréld a GitHub Secretben (IG_ACCESS_TOKEN).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not token() or os.getenv("IG_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return 0
    cfg = kc.Config.from_env()
    now = datetime.now(ZoneInfo(cfg.timezone))
    st = load_state()
    if not args.dry_run:
        check_token(st, now)
        publish_due(st, now)
    enqueue(cfg.output_dir, st, now, args.dry_run)
    if not args.dry_run:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
