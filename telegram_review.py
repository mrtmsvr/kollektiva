#!/usr/bin/env python3
"""
Kollektíva – emberi jóváhagyás Telegramon
=========================================

A rovatcikkek nem kerülnek ki automatikusan: a robot elküldi őket Telegramon, és csak jóváhagyás után
jelennek meg az oldalon.

Egy cikkhez ezt kapod:
  1. fejléc: rovat, forróság, címjavaslatok, lead, „Röviden” pontok, források
  2. a teljes szöveg
  3. a képjelöltek számozva (1–4)
  4. vezérlőüzenet gombokkal:  Cím 1–3 · Kép 1–4 / Nincs kép · ✅ Kirakom · 🔁 Újraírás · 🗑 Elvetem
Saját cím: válaszolj (reply) a cikk bármelyik üzenetére a kívánt címmel.

Futtatás: `python telegram_review.py` (GitHub Actions, 5 percenként) – feldolgozza a beérkezett
gombnyomásokat/üzeneteket. Az első üzenet, amit a botnak írsz, összeköti a botot veled (chat ID).

Állapot: data/review/pending.json (függő cikkek), data/review/state.json (offset, chat ID, elvetett linkek).
A token csak a TELEGRAM_BOT_TOKEN környezeti változóból jön, sehova nem íródik ki.
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.telegram")
REVIEW_DIR = kc.BASE_DIR / "data" / "review"
PENDING_FILE = REVIEW_DIR / "pending.json"
STATE_FILE = REVIEW_DIR / "state.json"
PENDING_MAX_AGE_H = int(os.getenv("PENDING_MAX_AGE_H", "48"))
PUBLISH_FLAG = kc.BASE_DIR / ".published_flag"
E = lambda t: html.escape(str(t or ""), quote=False)  # noqa: E731


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg(method: str, payload: dict, timeout: int = 30) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {}
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning("Telegram %s: HTTP %s %s", method, e.code, e.read()[:300].decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("Telegram %s: %s", method, getattr(e, "reason", type(e).__name__))
    return {}


def _mid(resp: dict) -> Optional[int]:
    r = resp.get("result")
    if isinstance(r, list):
        return r[0].get("message_id") if r else None
    return (r or {}).get("message_id")


# ---------------------------------------------------------------------------
# Állapot
# ---------------------------------------------------------------------------

def load_state() -> dict:
    return kc.read_json(STATE_FILE, {"offset": 0, "chat_id": None, "rejected_links": []})


def save_state(st: dict) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    st["rejected_links"] = st.get("rejected_links", [])[-800:]
    kc.write_json_atomic(STATE_FILE, st)


def load_pending(_out_dir: Optional[Path] = None) -> list:
    return kc.read_json(PENDING_FILE, {"articles": []}).get("articles", [])


def save_pending(_out_dir: Optional[Path], items: list) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    kc.write_json_atomic(PENDING_FILE, {"articles": items})


def enabled(_out_dir: Optional[Path] = None) -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(load_state().get("chat_id"))


def rejected_links(_out_dir: Optional[Path] = None) -> set:
    return set(load_state().get("rejected_links", []))


# ---------------------------------------------------------------------------
# Küldés
# ---------------------------------------------------------------------------

def _chunks(text: str, size: int = 3800) -> list:
    out, cur = [], ""
    for p in text.split("\n\n"):
        if len(cur) + len(p) + 2 > size and cur:
            out.append(cur)
            cur = ""
        cur = (cur + "\n\n" + p).strip()
    if cur:
        out.append(cur)
    return out


def _chosen_title(art: dict) -> str:
    rv = art.get("review", {})
    if rv.get("custom_title"):
        return rv["custom_title"]
    opts = art.get("title_options") or [art["title"]]
    return opts[min(rv.get("title", 0), len(opts) - 1)]


def _control(art: dict) -> tuple:
    rv = art.get("review", {})
    sid = art["id"][:8]
    opts = art.get("title_options") or [art["title"]]
    imgs = art.get("image_options") or []
    ti, ii = rv.get("title", 0), rv.get("image", 0 if imgs else -1)
    mark = lambda ok: "✓ " if ok else ""  # noqa: E731
    rows = [[{"text": f"{mark(not rv.get('custom_title') and ti == i)}Cím {i + 1}", "callback_data": f"{sid}|t|{i}"}
             for i in range(len(opts))]]
    if imgs:
        rows.append([{"text": f"{mark(ii == i)}Kép {i + 1}", "callback_data": f"{sid}|i|{i}"} for i in range(len(imgs))]
                    + [{"text": f"{mark(ii == -1)}Nincs kép", "callback_data": f"{sid}|i|-1"}])
    rows.append([{"text": "✅ Kirakom", "callback_data": f"{sid}|ok"},
                 {"text": "🔁 Újraírás", "callback_data": f"{sid}|rw"},
                 {"text": "🗑 Elvetem", "callback_data": f"{sid}|no"}])
    img_txt = f"{ii + 1}. kép" if ii >= 0 and imgs else "nincs kép"
    text = (f"<b>Döntés</b> – {E(kc.SECTIONS.get(art['category'], {}).get('name', art['category']))}\n"
            f"Cím: <b>{E(_chosen_title(art))}</b>\nKép: {img_txt}\n\n"
            "Saját címhez válaszolj (reply) erre az üzenetre a címmel.")
    return text, {"inline_keyboard": rows}


def send_article(_out_dir: Optional[Path], art: dict) -> None:
    chat = load_state().get("chat_id")
    if not chat:
        return
    sec = kc.SECTIONS.get(art["category"], {}).get("name", art["category"])
    titles = art.get("title_options") or [art["title"]]
    head = (f"🆕 <b>{E(sec)}</b> · forróság {art.get('hot_score', 0)} · {len(art.get('sources', []))} forrás · "
            f"{art.get('reading_time_min', 1)} perc\n\n<b>Címjavaslatok</b>\n"
            + "\n".join(f"{i + 1}) {E(t)}" for i, t in enumerate(titles))
            + f"\n\n<i>{E(art.get('lead'))}</i>")
    if art.get("key_points"):
        head += "\n\n<b>Röviden</b>\n" + "\n".join("• " + E(k) for k in art["key_points"])
    head += "\n\n<b>Források:</b> " + ", ".join(
        f'<a href="{html.escape(s["url"])}">{E(s.get("publisher") or "forrás")}</a>' for s in art.get("sources", []))
    ids = [_mid(tg("sendMessage", {"chat_id": chat, "text": head[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": True}))]
    for part in _chunks("\n\n".join(art.get("body", []))):
        ids.append(_mid(tg("sendMessage", {"chat_id": chat, "text": part})))
    imgs = art.get("image_options") or []
    if len(imgs) >= 2:
        media = [{"type": "photo", "media": im["url"], "caption": f"{i + 1}. kép – {im.get('credit', '')}"[:200]}
                 for i, im in enumerate(imgs[:4])]
        resp = tg("sendMediaGroup", {"chat_id": chat, "media": media}, timeout=60)
        if not resp.get("ok"):  # ha a Telegram nem tudja letölteni valamelyiket: egyenként
            for i, im in enumerate(imgs[:4]):
                r = tg("sendPhoto", {"chat_id": chat, "photo": im["url"], "caption": f"{i + 1}. kép"})
                if not r.get("ok"):
                    tg("sendMessage", {"chat_id": chat, "text": f"{i + 1}. kép: {im['url']}"})
        else:
            ids += [m.get("message_id") for m in resp.get("result", [])]
    elif imgs:
        ids.append(_mid(tg("sendPhoto", {"chat_id": chat, "photo": imgs[0]["url"], "caption": "1. kép"})))
    else:
        ids.append(_mid(tg("sendMessage", {"chat_id": chat, "text": "Ehhez a cikkhez nem találtam illő, szabad licencű képet."})))
    art["review"] = {"title": 0, "image": 0 if imgs else -1, "sent_at": datetime.now().isoformat(timespec="seconds")}
    text, kb = _control(art)
    ctl = _mid(tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "reply_markup": kb}))
    art["review"].update({"msg_ids": [i for i in ids if i], "control_id": ctl})


def _refresh_control(chat: int, art: dict) -> None:
    text, kb = _control(art)
    tg("editMessageText", {"chat_id": chat, "message_id": art["review"].get("control_id"), "text": text,
                           "parse_mode": "HTML", "reply_markup": kb})


# ---------------------------------------------------------------------------
# Kirakás
# ---------------------------------------------------------------------------

def publish(art: dict, tz: ZoneInfo) -> dict:
    rv = art.pop("review", {})
    title = _chosen_title({**art, "review": rv})
    imgs = art.get("image_options") or []
    ii = rv.get("image", 0 if imgs else -1)
    art["hero_image"] = imgs[ii] if 0 <= ii < len(imgs) else None
    cat = art["category"]
    if title != art["title"]:
        art["title"] = title
        art["slug"] = kc.slugify(f"{art['date']}-{title}")
        art["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kc.SITE_URL}/{cat}/{art['slug']}"))
    art["url"] = f"/{cat}/{art['slug']}/"
    art["seo"].update({"meta_title": art["title"][:60], "canonical_url": f"{kc.SITE_URL}/{cat}/{art['slug']}/",
                       "og_image": (art["hero_image"] or {}).get("url"), "noindex": False})
    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    art.update({"status": "published", "published_at": now_iso, "created_at": now_iso, "updated_at": now_iso})
    art["authorship"]["reviewed_by"] = "szerkesztő (Telegram)"
    art["authorship"]["reviewed_at"] = now_iso
    for k in ("title_options", "image_options", "story"):
        art.pop(k, None)
    return art


def _add_published(out_dir: Path, art: dict, tz: ZoneInfo) -> None:
    path = out_dir / "articles.json"
    articles = kc.read_json(path, {"articles": []}).get("articles", [])
    articles.insert(0, art)
    articles.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    kc.write_json_atomic(path, {"schema_version": 1, "updated_at": datetime.now(tz).isoformat(timespec="seconds"),
                                "articles": articles[:int(os.getenv("ARTICLES_ARCHIVE_LIMIT", "600"))]})


# ---------------------------------------------------------------------------
# Beérkezett válaszok feldolgozása
# ---------------------------------------------------------------------------

HELP = ("Szia! Ide küldöm jóváhagyásra az új Kollektíva-cikkeket.\n\n"
        "• Cím 1–3 / Kép 1–4 / Nincs kép: kiválasztás\n• ✅ Kirakom: megjelenik az oldalon (pár percen belül)\n"
        "• 🔁 Újraírás: új változatot kérek\n• 🗑 Elvetem: törlés\n• Saját cím: válaszolj a cikk üzenetére a címmel\n"
        "• /lista – függő cikkek")


def poll(out_dir: Path, ai=None, tz: Optional[ZoneInfo] = None) -> int:
    """Feldolgozza a Telegram-frissítéseket. Visszatér: hány cikk került ki."""
    tz = tz or ZoneInfo("Europe/Budapest")
    st, pending = load_state(), load_pending()
    resp = tg("getUpdates", {"offset": st.get("offset", 0), "timeout": 0,
                             "allowed_updates": ["message", "callback_query"]})
    published, changed = 0, False
    for u in resp.get("result", []):
        st["offset"] = u["update_id"] + 1
        changed = True
        if "message" in u:
            m = u["message"]
            chat = m.get("chat", {})
            if not st.get("chat_id"):
                if chat.get("type") == "private":
                    st["chat_id"] = chat["id"]
                    tg("sendMessage", {"chat_id": chat["id"], "text": "✅ Összekötve.\n\n" + HELP})
                continue
            if chat.get("id") != st["chat_id"]:
                continue
            text = (m.get("text") or "").strip()
            rep = (m.get("reply_to_message") or {}).get("message_id")
            if rep and text and not text.startswith("/"):
                art = next((a for a in pending if rep in (a.get("review", {}).get("msg_ids", []) +
                                                          [a.get("review", {}).get("control_id")])), None)
                if art:
                    art["review"]["custom_title"] = text[:140]
                    _refresh_control(st["chat_id"], art)
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"Cím beállítva: {text[:140]}",
                                       "reply_to_message_id": m["message_id"]})
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "Ez a cikk már nincs függőben."})
            elif text.startswith("/lista"):
                lines = [f"• {kc.SECTIONS.get(a['category'], {}).get('name', '')}: {_chosen_title(a)}" for a in pending]
                tg("sendMessage", {"chat_id": st["chat_id"], "text": "\n".join(lines) or "Nincs függő cikk."})
            elif text.startswith("/"):
                tg("sendMessage", {"chat_id": st["chat_id"], "text": HELP})
        elif "callback_query" in u:
            q = u["callback_query"]
            if (q.get("message") or {}).get("chat", {}).get("id") != st.get("chat_id"):
                continue
            parts = (q.get("data") or "").split("|")
            art = next((a for a in pending if a["id"].startswith(parts[0])), None)
            if not art:
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Ez a cikk már nincs függőben."})
                continue
            act, note = parts[1] if len(parts) > 1 else "", ""
            if act == "t":
                art["review"]["title"] = int(parts[2])
                art["review"].pop("custom_title", None)
                _refresh_control(st["chat_id"], art)
                note = f"Cím {int(parts[2]) + 1}"
            elif act == "i":
                art["review"]["image"] = int(parts[2])
                _refresh_control(st["chat_id"], art)
                note = "Nincs kép" if int(parts[2]) < 0 else f"Kép {int(parts[2]) + 1}"
            elif act == "ok":
                pending.remove(art)
                final = publish(art, tz)
                _add_published(out_dir, final, tz)
                published += 1
                note = "Kirakva ✅"
                tg("editMessageText", {"chat_id": st["chat_id"], "message_id": q["message"]["message_id"],
                                       "parse_mode": "HTML", "text": f"✅ <b>Kirakva</b> (pár perc múlva látszik):\n"
                                       f"{E(final['title'])}\n{kc.SITE_URL}{final['url']}"})
            elif act == "no":
                pending.remove(art)
                st.setdefault("rejected_links", []).extend(art.get("category_meta", {}).get("source_links", []))
                note = "Elvetve"
                tg("editMessageText", {"chat_id": st["chat_id"], "message_id": q["message"]["message_id"],
                                       "text": f"🗑 Elvetve: {_chosen_title(art)}"})
            elif act == "rw":
                note = "Újraírás…"
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": note})
                q = None
                new = _rewrite(art, ai, tz)
                if new:
                    pending[pending.index(art)] = new
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": art["review"].get("control_id"),
                                           "text": "🔁 Újraírva – lásd az új változatot lent."})
                    send_article(out_dir, new)
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "Az újraírás most nem sikerült, próbáld később."})
            if q:
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": note})
    # lejárt függő cikkek
    limit = (datetime.now(tz) - timedelta(hours=PENDING_MAX_AGE_H)).isoformat()
    for a in [a for a in pending if (a.get("created_at") or "") < limit]:
        pending.remove(a)
        st.setdefault("rejected_links", []).extend(a.get("category_meta", {}).get("source_links", []))
        changed = True
    if changed:
        save_state(st)
        save_pending(out_dir, pending)
    if published:
        PUBLISH_FLAG.write_text("1")
    return published


def _rewrite(art: dict, ai, tz: ZoneInfo) -> Optional[dict]:
    if ai is None:
        ai = kc.AIClient(kc.Config.from_env())
    if not ai.enabled or not art.get("story"):
        return None
    story = [dict(s, kw=kc._keywords(s["title"] + " " + (s.get("summary") or "")[:200])) for s in art["story"]]
    new = kc.build_section_article(ai, kc.SECTIONS[art["category"]], date.fromisoformat(art["date"]), tz, story,
                                   {im["url"] for im in art.get("image_options") or []})
    if new:
        new["status"] = "pending"
    return new


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    kc.load_dotenv(kc.BASE_DIR / ".env")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        log.warning("Nincs TELEGRAM_BOT_TOKEN – nincs mit tenni.")
        return 0
    cfg = kc.Config.from_env()
    tz = ZoneInfo(cfg.timezone)
    n = poll(cfg.output_dir, None, tz)
    if n:
        kc.build_static_site(cfg.output_dir, tz)
        log.info("✔ %d jóváhagyott cikk kirakva", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
