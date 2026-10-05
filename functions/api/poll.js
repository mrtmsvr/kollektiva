// Cloudflare Pages Function: /api/poll – olvasói szavazások (Cloudflare D1, binding neve: DB).
// GET  /api/poll?id=ID           -> {ok, counts:[..], total, voted}
// POST /api/poll {id, option, v} -> szavazat (egy böngésző egyszer szavazhat), válasz: az eredmény
// v = a böngészőben tárolt véletlen azonosító (localStorage 'kvid'): így a szavazat IP-váltás (mobilnet) után is
// megmarad és nem számolódik újra. Régi (v nélküli) szavazatoknál az IP+böngésző alapú azonosító is számít.
// A kérdések listája statikus: /public/data/polls.json (a robot írja) – ebből ellenőrizzük az id-t és az opciót.
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

async function findPoll(request, env, id) {
  for (const p of ['/public/data/polls.json', '/data/polls.json']) {
    try {
      const r = await env.ASSETS.fetch(new URL(p, request.url));
      if (r.ok) { const d = await r.json(); const poll = (d.polls || []).find(x => x.id === id); if (poll) return poll; }
    } catch (e) { /* következő útvonal */ }
  }
  return null;
}

async function hash(s) {
  const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 32);
}

// [elsődleges, régi]: a böngésző-azonosítós (ha van) és az IP+böngésző alapú azonosító
async function voterIds(request, id, env, token) {
  const salt = env.POLL_SALT || 'kollektiva';
  const ip = request.headers.get('cf-connecting-ip') || '';
  const ua = request.headers.get('user-agent') || '';
  const legacy = await hash(`${salt}|${id}|${ip}|${ua}`);
  const t = /^[A-Za-z0-9-]{8,64}$/.test(token || '') ? await hash(`${salt}|${id}|t|${token}`) : null;
  return t ? [t, legacy] : [legacy];
}

async function results(env, poll, voters) {
  const rows = (await env.DB.prepare('SELECT option, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY option')
    .bind(poll.id).all()).results || [];
  const counts = poll.options.map((_, i) => (rows.find(r => r.option === i) || {}).n || 0);
  let mine = null;
  for (const v of voters || []) {
    mine = await env.DB.prepare('SELECT option FROM votes WHERE poll_id = ? AND voter = ?').bind(poll.id, v).first();
    if (mine) break;
  }
  return { ok: true, counts, total: counts.reduce((a, b) => a + b, 0), voted: mine ? mine.option : null,
           closed: Date.now() > Date.parse(poll.closes_at) };
}

export async function onRequestGet({ request, env }) {
  if (!env.DB) return json({ ok: false, error: 'not_configured' }, 503);
  const q = new URL(request.url).searchParams, id = q.get('id') || '';
  const poll = await findPoll(request, env, id);
  if (!poll) return json({ ok: false, error: 'unknown_poll' }, 404);
  return json(await results(env, poll, await voterIds(request, id, env, q.get('v'))));
}

export async function onRequestPost({ request, env }) {
  if (!env.DB) return json({ ok: false, error: 'not_configured' }, 503);
  let body = {};
  try { body = await request.json(); } catch (e) { return json({ ok: false, error: 'bad_request' }, 400); }
  const id = String(body.id || ''), option = Number(body.option);
  const poll = await findPoll(request, env, id);
  if (!poll) return json({ ok: false, error: 'unknown_poll' }, 404);
  if (!Number.isInteger(option) || option < 0 || option >= poll.options.length) return json({ ok: false, error: 'bad_option' }, 400);
  if (Date.now() > Date.parse(poll.closes_at)) return json({ ok: false, error: 'closed', ...(await results(env, poll, null)) }, 409);
  const voters = await voterIds(request, id, env, String(body.v || ''));
  const before = await results(env, poll, voters);
  if (before.voted !== null) return json(before);   // már szavazott (ebből a böngészőből) → nem számoljuk újra
  await env.DB.prepare('INSERT OR IGNORE INTO votes (poll_id, voter, option, created_at) VALUES (?, ?, ?, ?)')
    .bind(id, voters[0], option, new Date().toISOString()).run();
  return json(await results(env, poll, voters));
}
