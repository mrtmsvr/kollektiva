// Cloudflare Pages Function: POST /api/subscribe  – hírlevél-feliratkozás a Brevo-listára.
// Beállítás (Cloudflare Pages → Settings → Variables and Secrets): BREVO_API_KEY (titkos).
// Opcionális: BREVO_LIST_ID – ha nincs megadva, a „Kollektíva hírlevél” nevű listát használja (ha nincs, létrehozza).
const LIST_NAME = 'Kollektíva hírlevél';
const API = 'https://api.brevo.com/v3';

function apiKey(env) {
  let k = (env.BREVO_API_KEY || '').trim();
  if (k && !k.startsWith('xkeysib-')) {            // az „MCP”-változat base64-be csomagolt JSON: {"api_key": "..."}
    try { k = JSON.parse(atob(k)).api_key || k; } catch (e) { /* marad */ }
  }
  return k;
}

async function brevo(env, path, init = {}) {
  const r = await fetch(API + path, {
    ...init,
    headers: { 'api-key': apiKey(env), 'accept': 'application/json', 'content-type': 'application/json', ...(init.headers || {}) },
  });
  const text = await r.text();
  let data = null; try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  return { ok: r.ok, status: r.status, data };
}

async function listId(env) {
  if (env.BREVO_LIST_ID) return Number(env.BREVO_LIST_ID);
  const lists = await brevo(env, '/contacts/lists?limit=50&offset=0');
  const found = (lists.data?.lists || []).find(l => l.name === LIST_NAME);
  if (found) return found.id;
  const folders = await brevo(env, '/contacts/folders?limit=10&offset=0');
  let folderId = folders.data?.folders?.[0]?.id;
  if (!folderId) folderId = (await brevo(env, '/contacts/folders', { method: 'POST', body: JSON.stringify({ name: 'Kollektíva' }) })).data?.id;
  const created = await brevo(env, '/contacts/lists', { method: 'POST', body: JSON.stringify({ name: LIST_NAME, folderId }) });
  return created.data?.id;
}

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

export async function onRequestPost({ request, env }) {
  if (!apiKey(env)) return json({ ok: false, error: 'not_configured' }, 503);
  let body = {};
  try { body = await request.json(); } catch (e) { return json({ ok: false, error: 'bad_request' }, 400); }
  const email = String(body.email || '').trim().toLowerCase();
  if (body.website) return json({ ok: true });                       // bot-csapda (rejtett mező)
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email) || email.length > 200) return json({ ok: false, error: 'invalid_email' }, 400);
  if (!body.consent_at) return json({ ok: false, error: 'consent_required' }, 400);
  const id = await listId(env);
  if (!id) return json({ ok: false, error: 'list_unavailable' }, 502);
  const r = await brevo(env, '/contacts', {
    method: 'POST',
    body: JSON.stringify({
      email, listIds: [id], updateEnabled: true,
      attributes: { SOURCE: String(body.source || 'kollektiva').slice(0, 60), CONSENT_AT: String(body.consent_at).slice(0, 40) },
    }),
  });
  if (!r.ok && r.status !== 204) {
    // ismeretlen attribútum esetén attribútumok nélkül újra
    const r2 = await brevo(env, '/contacts', { method: 'POST', body: JSON.stringify({ email, listIds: [id], updateEnabled: true }) });
    if (!r2.ok && r2.status !== 204) return json({ ok: false, error: 'brevo_error', status: r2.status }, 502);
  }
  return json({ ok: true });
}

export const onRequestGet = () => json({ ok: true, info: 'POST {email, consent_at}' });
