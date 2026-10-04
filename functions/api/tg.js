// Cloudflare Pages Function: /api/tg – azonnali Telegram-fogadás (webhook) a Kollektíva robotnak.
// POST /api/tg  <- a Telegram küldi (fejléc: X-Telegram-Bot-Api-Secret-Token = TG_WEBHOOK_SECRET)
//   • az üzenetet / gombnyomást eltárolja a D1-ben (tg_updates), azonnal visszajelez (👀 / „Megkaptam”),
//   • és elindítja a GitHub-robotot (workflow_dispatch), ha épp nem fut – percenként legfeljebb egyszer.
// GET  /api/tg?take=1  (fejléc: X-Queue-Secret = TG_WEBHOOK_SECRET) <- a robot így veszi ki a sorból.
// Kell (Cloudflare Pages → Settings → Variables and Secrets): TG_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN, GH_DISPATCH_TOKEN
// Titkok felvéve: 2026-10-04.
// (opcionális: GH_REPO, alap: mrtmsvr/vx9-orrery-lumen-4qk7t-szinter-motor). D1 binding: DB (ugyanaz, mint a szavazásnál).
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

async function ensure(env) {
  await env.DB.batch([
    env.DB.prepare('CREATE TABLE IF NOT EXISTS tg_updates (id INTEGER PRIMARY KEY, body TEXT NOT NULL, created INTEGER NOT NULL)'),
    env.DB.prepare('CREATE TABLE IF NOT EXISTS tg_meta (k TEXT PRIMARY KEY, v TEXT)'),
  ]);
}

async function tg(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
    });
  } catch (e) { /* a visszajelzés nem kötelező */ }
}

async function dispatch(env) {
  if (!env.GH_DISPATCH_TOKEN) return 'no_token';
  const now = Date.now();
  const last = await env.DB.prepare("SELECT v FROM tg_meta WHERE k = 'dispatched'").first();
  if (last && now - Number(last.v) < 60000) return 'recent';
  await env.DB.prepare("INSERT OR REPLACE INTO tg_meta (k, v) VALUES ('dispatched', ?)").bind(String(now)).run();
  const repo = env.GH_REPO || 'mrtmsvr/vx9-orrery-lumen-4qk7t-szinter-motor';
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/telegram.yml/dispatches`, {
    method: 'POST',
    headers: { authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`, accept: 'application/vnd.github+json',
               'user-agent': 'kollektiva-tg-webhook', 'content-type': 'application/json' },
    body: JSON.stringify({ ref: 'main' }),
  });
  return r.status === 204 ? 'ok' : `http_${r.status}`;
}

export async function onRequestPost({ request, env, waitUntil }) {
  if (!env.DB || !env.TG_WEBHOOK_SECRET) return json({ ok: false, error: 'not_configured' }, 503);
  if (request.headers.get('x-telegram-bot-api-secret-token') !== env.TG_WEBHOOK_SECRET) return json({ ok: false }, 403);
  let u;
  try { u = await request.json(); } catch (e) { return json({ ok: false }, 400); }
  if (!u || typeof u.update_id !== 'number') return json({ ok: true });
  await ensure(env);
  await env.DB.prepare('INSERT OR IGNORE INTO tg_updates (id, body, created) VALUES (?, ?, ?)')
    .bind(u.update_id, JSON.stringify(u), Date.now()).run();
  // azonnali, csendes visszajelzés: gombnál a „homokóra” eltűnik, üzenetnél 👀 reakció
  if (u.callback_query) {
    waitUntil(tg(env, 'answerCallbackQuery', { callback_query_id: u.callback_query.id, text: '⏳ Megkaptam' }));
  } else if (u.message && u.message.chat) {
    waitUntil(tg(env, 'setMessageReaction', { chat_id: u.message.chat.id, message_id: u.message.message_id,
                                               reaction: [{ type: 'emoji', emoji: '👀' }] }));
  }
  waitUntil(dispatch(env).catch(() => 'error'));
  return json({ ok: true });
}

export async function onRequestGet({ request, env }) {
  if (!env.DB || !env.TG_WEBHOOK_SECRET) return json({ ok: false, error: 'not_configured' }, 503);
  if (request.headers.get('x-queue-secret') !== env.TG_WEBHOOK_SECRET) return json({ ok: false }, 403);
  await ensure(env);
  const rows = (await env.DB.prepare('SELECT id, body FROM tg_updates ORDER BY id LIMIT 50').all()).results || [];
  if (rows.length) {
    const ids = rows.map(r => r.id);
    await env.DB.prepare(`DELETE FROM tg_updates WHERE id IN (${ids.map(() => '?').join(',')})`).bind(...ids).run();
  }
  return json({ ok: true, result: rows.map(r => JSON.parse(r.body)) });
}
