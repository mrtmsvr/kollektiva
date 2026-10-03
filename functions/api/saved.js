// Cloudflare Pages Function: /api/saved – mentett cikkek (Google-belépéshez kötve, Cloudflare D1, binding: DB).
// A munkamenetet a /api/login hozza létre (sessions tábla); a böngésző „Authorization: Bearer <token>” fejléccel jön.
// GET    /api/saved            -> {ok, items:[{url, title, saved_at}]}
// GET    /api/saved?url=/x/y   -> {ok, saved:true|false}
// POST   /api/saved {url,title}-> mentés
// DELETE /api/saved?url=/x/y   -> törlés
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});
const SESSION_DAYS = 180;

export async function ensureTables(env) {
  await env.DB.batch([
    env.DB.prepare('CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, email TEXT NOT NULL, created_at TEXT NOT NULL)'),
    env.DB.prepare('CREATE TABLE IF NOT EXISTS saved (email TEXT NOT NULL, url TEXT NOT NULL, title TEXT, saved_at TEXT NOT NULL, PRIMARY KEY (email, url))'),
  ]);
}

async function who(request, env) {
  const t = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '').trim();
  if (!/^[a-f0-9]{48}$/.test(t)) return null;
  const r = await env.DB.prepare('SELECT email, created_at FROM sessions WHERE token = ?').bind(t).first();
  if (!r || Date.now() - Date.parse(r.created_at) > SESSION_DAYS * 864e5) return null;
  return r.email;
}

// csak a saját oldalunk cikk-útvonalai (pl. /kozelet/2026-10-03-cim/)
const cleanUrl = u => { u = String(u || '').trim(); return /^\/[a-z0-9][a-z0-9\-\/]{2,220}$/i.test(u) ? u : null; };

async function start(request, env) {
  if (!env.DB) return { err: json({ ok: false, error: 'not_configured' }, 503) };
  await ensureTables(env);
  const email = await who(request, env);
  if (!email) return { err: json({ ok: false, error: 'login_required' }, 401) };
  return { email };
}

export async function onRequestGet({ request, env }) {
  const { err, email } = await start(request, env); if (err) return err;
  const url = new URL(request.url).searchParams.get('url');
  if (url !== null) {
    const u = cleanUrl(url);
    const row = u ? await env.DB.prepare('SELECT 1 FROM saved WHERE email = ? AND url = ?').bind(email, u).first() : null;
    return json({ ok: true, saved: !!row });
  }
  const rows = (await env.DB.prepare('SELECT url, title, saved_at FROM saved WHERE email = ? ORDER BY saved_at DESC LIMIT 300')
    .bind(email).all()).results || [];
  return json({ ok: true, items: rows });
}

export async function onRequestPost({ request, env }) {
  const { err, email } = await start(request, env); if (err) return err;
  let body = {}; try { body = await request.json(); } catch (e) { return json({ ok: false, error: 'bad_request' }, 400); }
  const u = cleanUrl(body.url);
  if (!u) return json({ ok: false, error: 'bad_url' }, 400);
  await env.DB.prepare('INSERT OR IGNORE INTO saved (email, url, title, saved_at) VALUES (?, ?, ?, ?)')
    .bind(email, u, String(body.title || '').slice(0, 300), new Date().toISOString()).run();
  return json({ ok: true, saved: true });
}

export async function onRequestDelete({ request, env }) {
  const { err, email } = await start(request, env); if (err) return err;
  const u = cleanUrl(new URL(request.url).searchParams.get('url'));
  if (!u) return json({ ok: false, error: 'bad_url' }, 400);
  await env.DB.prepare('DELETE FROM saved WHERE email = ? AND url = ?').bind(email, u).run();
  return json({ ok: true, saved: false });
}
