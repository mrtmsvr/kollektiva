#!/usr/bin/env python3
"""Heti Kollektíva – vasárnap reggel kiküldi a hírlevelet a Brevo-listának.

A robot 5 percenként hívja; csak vasárnap NEWSLETTER_HOUR (alap 8) óra után fut, és hetente egyszer
(data/robot.json: last_newsletter_week). Tartalom: a hét 5 legforróbb cikke + a hét egyik retro sztorija.
Kell hozzá: BREVO_API_KEY (GitHub Secret). Feladó: NEWSLETTER_SENDER_EMAIL, vagy a Brevo első hitelesített feladója.
Kézi próba: python scripts/newsletter.py --force [--test cim@pelda.hu]
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kollektiva_content as kc  # noqa: E402

log = logging.getLogger("kollektiva.newsletter")
API = "https://api.brevo.com/v3"
LIST_NAME = "Kollektíva hírlevél"
RUNS = kc.BASE_DIR / "data" / "robot.json"
E = lambda t: html.escape(str(t or ""))  # noqa: E731


def api_key() -> str:
    k = os.getenv("BREVO_API_KEY", "").strip()
    if k and not k.startswith("xkeysib-"):  # az „MCP”-kulcs base64-be csomagolt JSON
        try:
            k = json.loads(base64.b64decode(k + "===").decode()).get("api_key", k)
        except (ValueError, UnicodeDecodeError):
            pass
    return k


def brevo(path: str, payload: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(API + path, method=method or ("POST" if payload is not None else "GET"),
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"api-key": api_key(), "accept": "application/json",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode() or "{}"
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}


def list_id() -> int | None:
    if os.getenv("BREVO_LIST_ID"):
        return int(os.environ["BREVO_LIST_ID"])
    _, data = brevo("/contacts/lists?limit=50&offset=0")
    for l in data.get("lists", []):
        if l.get("name") == LIST_NAME:
            return l["id"]
    _, f = brevo("/contacts/folders?limit=10&offset=0")
    folder = (f.get("folders") or [{}])[0].get("id") or brevo("/contacts/folders", {"name": "Kollektíva"})[1].get("id")
    return brevo("/contacts/lists", {"name": LIST_NAME, "folderId": folder})[1].get("id")


def sender() -> dict | None:
    if os.getenv("NEWSLETTER_SENDER_EMAIL"):
        return {"name": "Kollektíva", "email": os.environ["NEWSLETTER_SENDER_EMAIL"]}
    _, data = brevo("/senders")
    for s in data.get("senders", []):
        if s.get("active"):
            return {"name": "Kollektíva", "email": s["email"]}
    return None


def build(tz: ZoneInfo) -> tuple[str, str]:
    out = kc.Config.from_env().output_dir
    now = datetime.now(tz)
    since = (now - timedelta(days=7)).isoformat()
    arts = [a for a in kc.read_json(out / "articles.json", {"articles": []}).get("articles", [])
            if (a.get("created_at") or "") >= since and a.get("status") == "published"]
    arts.sort(key=lambda a: (a.get("hot_score") or 0), reverse=True)
    top, per = [], {}
    for a in arts:  # változatos: rovatonként legfeljebb 2 cikk
        if per.get(a.get("category"), 0) < 2:
            top.append(a)
            per[a.get("category")] = per.get(a.get("category"), 0) + 1
        if len(top) == 5:
            break
    retro = next((a for a in kc.read_json(out / "retro_articles.json", {"articles": []}).get("articles", [])
                  if (a.get("created_at") or "") >= since), None)
    site = kc.SITE_URL
    utm = "?utm_source=hirlevel&utm_medium=email&utm_campaign=heti-" + now.strftime("%G-%V")
    card = lambda a: (  # noqa: E731
        f'<tr><td style="padding:0 0 22px">'
        + (f'<a href="{site}{E(a["url"])}{utm}"><img src="{E(a["hero_image"]["url"])}" width="560" alt="" '
           f'style="width:100%;max-width:560px;height:auto;border-radius:10px;display:block;margin:0 0 10px"></a>'
           if (a.get("hero_image") or {}).get("url") else "")
        + f'<div style="font:600 12px Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#A8853F">'
          f'{E(kc.SECTIONS.get(a.get("category"), {}).get("name", "Retro"))}</div>'
          f'<a href="{site}{E(a["url"])}{utm}" style="font:700 22px Georgia,serif;color:#0E1024;text-decoration:none;'
          f'line-height:1.25">{E(a["title"])}</a>'
          f'<p style="font:15px/1.55 Arial,sans-serif;color:#3b3d58;margin:8px 0 0">{E(a.get("lead"))}</p></td></tr>')
    items = "".join(card(a) for a in top)
    retro_html = (f'<tr><td style="padding:10px 0 4px;font:700 13px Arial,sans-serif;color:#0E1024">'
                  f'EKKOR TÖRTÉNT – A HÉT RETRO SZTORIJA</td></tr>{card({**retro, "category": "retro"})}') if retro else ""
    subject = f"Heti Kollektíva: {top[0]['title']}" if top else "Heti Kollektíva"
    body = f"""<!doctype html><html><body style="margin:0;background:#ECE6D8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ECE6D8"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border-radius:14px">
<tr><td style="background:#0E1024;border-radius:14px 14px 0 0;padding:22px 20px;font:700 26px Georgia,serif;color:#ECE6D8">
Kollektíva<span style="color:#C9A45C">.</span><div style="font:13px Arial,sans-serif;color:#9492B3;margin-top:4px">
Heti Kollektíva · {now.strftime('%Y.%m.%d.')}</div></td></tr>
<tr><td style="padding:24px 20px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td style="padding:0 0 18px;font:15px/1.55 Arial,sans-serif;color:#3b3d58">A hét legfontosabb történetei röviden – kávé mellé.</td></tr>
{items}{retro_html}</table></td></tr>
<tr><td style="padding:14px 20px 22px;font:12px/1.5 Arial,sans-serif;color:#8a8aa3;border-top:1px solid #eee">
Azért kapod ezt a levelet, mert feliratkoztál a Heti Kollektívára a kollektíva.hu oldalon.<br>
<a href="{{{{ unsubscribe }}}}" style="color:#8a8aa3">Leiratkozás</a></td></tr>
</table></td></tr></table></body></html>"""
    return subject[:150], body


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="most azonnal (nem csak vasárnap)")
    p.add_argument("--test", help="próbalevél erre a címre (a lista helyett)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not api_key():
        return 0
    tz = ZoneInfo(kc.Config.from_env().timezone)
    now = datetime.now(tz)
    runs = kc.read_json(RUNS, {})
    week = now.strftime("%G-W%V")
    if not args.force and not (now.weekday() == 6 and now.hour >= int(os.getenv("NEWSLETTER_HOUR", "8"))):
        return 0
    if not args.force and runs.get("last_newsletter_week") == week:
        return 0
    lid, snd = list_id(), sender()
    if not lid or not snd:
        log.warning("Brevo: nincs lista (%s) vagy hitelesített feladó (%s) – a hírlevél kimarad.", lid, snd)
        return 0
    subject, body = build(tz)
    code, camp = brevo("/emailCampaigns", {
        "name": f"Heti Kollektíva {week}", "subject": subject, "sender": snd, "htmlContent": body,
        "recipients": {"listIds": [lid]}, "inlineImageActivation": False})
    if code >= 300:
        log.warning("Brevo kampány létrehozása sikertelen: %s %s", code, camp)
        return 0
    if args.test:
        code, r = brevo(f"/emailCampaigns/{camp['id']}/sendTest", {"emailTo": [args.test]})
    else:
        code, r = brevo(f"/emailCampaigns/{camp['id']}/sendNow", {})
    log.info("Hírlevél (%s): HTTP %s %s", "teszt" if args.test else "kiküldve", code, r if code >= 300 else "")
    if code < 300 and not args.test:
        kc.write_json_atomic(RUNS, {**kc.read_json(RUNS, {}), "last_newsletter_week": week})
    return 0


if __name__ == "__main__":
    sys.exit(main())
