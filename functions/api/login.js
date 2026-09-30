// Cloudflare Pages Function: POST /api/login  – Google-belépés ellenőrzése.
// A böngésző a Google-tól kapott access tokent küldi; itt ellenőrizzük, hogy a mi alkalmazásunknak szól,
// lekérjük a nevet/e-mailt/képet, és a Brevóban a „Kollektíva fiókok” listára tesszük (hírlevélre NEM iratkoztat).
const CLIENT_ID = '1014482488754-j1k6m2otl4cma3iaec2639i2nbji0p06.apps.googleusercontent.com';
const ACCOUNTS_LIST = 'Kollektíva fiókok';
const NEWSLETTER_LIST = 'Kollektíva hírlevél';
const API = 'https://api.brevo.com/v3';

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

function brevoKey(env) {
  let k = (env.BREVO_API_KEY || '').trim();
  if (k && !k.startsWith('xkeysib-')) { try { k = JSON.parse(atob(k)).api_key || k; } catch (e) { /* marad */ } }
  return k;
}
async function brevo(env, path, init = {}) {
  const r = await fetch(API + path, { ...init, headers: { 'api-key': brevoKey(env), accept: 'application/json', 'content-type': 'application/json' } });
  let data = null; try { data = await r.json(); } catch (e) { /* üres */ }
  return { ok: r.ok, status: r.status, data };
}
async function listId(env, name) {
  const lists = await brevo(env, '/contacts/lists?limit=50&offset=0');
  const found = (lists.data?.lists || []).find(l => l.name === name);
  if (found) return found.id;
  const folders = await brevo(env, '/contacts/folders?limit=10&offset=0');
  const folderId = folders.data?.folders?.[0]?.id;
  return (await brevo(env, '/contacts/lists', { method: 'POST', body: JSON.stringify({ name, folderId }) })).data?.id;
}

export async function onRequestPost({ request, env }) {
  let body = {};
  try { body = await request.json(); } catch (e) { return json({ ok: false, error: 'bad_request' }, 400); }
  const token = String(body.access_token || '');
  if (!token) return json({ ok: false, error: 'no_token' }, 400);
  const info = await (await fetch('https://oauth2.googleapis.com/tokeninfo?access_token=' + encodeURIComponent(token))).json().catch(() => ({}));
  if (info.aud !== (env.GOOGLE_CLIENT_ID || CLIENT_ID) || !info.email) return json({ ok: false, error: 'invalid_token' }, 401);
  const me = await (await fetch('https://www.googleapis.com/oauth2/v3/userinfo', { headers: { authorization: 'Bearer ' + token } })).json().catch(() => ({}));
  if (!me.email || me.email_verified === false) return json({ ok: false, error: 'no_email' }, 401);
  const user = { email: String(me.email).toLowerCase(), name: me.name || '', given_name: me.given_name || '', picture: me.picture || '' };
  if (brevoKey(env)) {
    try {
      const ids = [await listId(env, ACCOUNTS_LIST)];
      if (body.newsletter) ids.push(await listId(env, NEWSLETTER_LIST));
      const base = { email: user.email, listIds: ids.filter(Boolean), updateEnabled: true };
      const r = await brevo(env, '/contacts', { method: 'POST', body: JSON.stringify({ ...base, attributes: { FIRSTNAME: user.given_name } }) });
      if (!r.ok && r.status !== 204) await brevo(env, '/contacts', { method: 'POST', body: JSON.stringify(base) });
    } catch (e) { /* a belépés ettől még sikeres */ }
  }
  return json({ ok: true, user });
}
