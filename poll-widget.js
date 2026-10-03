/* Kollektíva – oldalt előjövő „A nap kérdése” fül.
   Hol látszik: a főoldalon (mobilon is, de ott csak kis görgetés után, hogy ne takarja a kiemelt hírt), és gépen
   a többi cikkoldalon is. Annál a cikknél, amelyikhez a kérdés tartozik, nincs fül (ott a cikk alján a szavazás).
   Rányomásra balról becsúszik; az X-re vagy bárhová máshová kattintva visszahúzódik (a kattintás ilyenkor nem
   nyit meg mást). Aki szavazott egy azóta lezárult kérdésre, annak egyszer szólunk, hogy nézze meg az eredményt.
   Adat: /public/data/polls.json (a robot írja), szavazás: /api/poll (Cloudflare D1). */
(function () {
  if (window.__kpw) return; window.__kpw = 1;
  var esc = function (v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  var HOME = location.pathname === '/' || location.pathname === '/index.html';
  var MOBILE = matchMedia('(max-width: 1023px)').matches;
  function getPolls() {
    return fetch('/public/data/polls.json', { cache: 'no-cache' }).then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .catch(function () { return fetch('/data/polls.json', { cache: 'no-cache' }).then(function (r) { return r.json(); }); });
  }
  function seenGet() { try { return JSON.parse(localStorage.getItem('kpw_seen') || '[]'); } catch (e) { return []; } }
  function notifyClosed(polls) {
    var seen = seenGet(), now = Date.now();
    polls.filter(function (p) { var c = Date.parse(p.closes_at); return c < now && now - c < 3 * 864e5 && seen.indexOf(p.id) < 0; })
      .slice(0, 3).forEach(function (p) {
        fetch('/api/poll?id=' + encodeURIComponent(p.id)).then(function (r) { return r.json(); }).then(function (r) {
          if (!r.ok || r.voted === null) return;
          seen = seenGet(); seen.push(p.id); try { localStorage.setItem('kpw_seen', JSON.stringify(seen.slice(-40))); } catch (e) {}
          var t = document.createElement('a'); t.href = '/szavazasok/#p' + p.id;
          t.textContent = 'Lezárult a szavazás, amiben részt vettél: „' + p.question + '” – nézd meg az eredményt →';
          t.style.cssText = 'position:fixed;left:50%;top:14px;transform:translateX(-50%);z-index:80;max-width:calc(100vw - 32px);background:#C9A45C;color:#0E1024;border-radius:14px;padding:10px 16px;font:600 13px/1.4 Manrope,system-ui,sans-serif;text-decoration:none;box-shadow:0 6px 20px rgba(0,0,0,.4)';
          document.body.appendChild(t); setTimeout(function () { t.remove(); }, 9000);
        }).catch(function () {});
      });
  }
  var CSS = '#kpw-tab{position:fixed;left:0;top:46%;z-index:55;display:flex;flex-direction:column;align-items:center;gap:9px;padding:13px 8px 15px;background:#C9A45C;color:#0E1024;border:0;border-radius:0 12px 12px 0;cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.4);font:800 12px/1 Manrope,system-ui,sans-serif;letter-spacing:.18em;transition:transform .35s ease;transform-origin:left center}'
    + '#kpw-tab.off{transform:translateX(-120%)}#kpw-tab.wig{animation:kpww .9s ease 2}#kpw-tab span{writing-mode:vertical-rl;transform:rotate(180deg)}'
    + '@keyframes kpww{0%,100%{transform:translateX(0) rotate(0)}20%{transform:translateX(6px) rotate(-4deg)}40%{transform:translateX(0) rotate(3deg)}60%{transform:translateX(4px) rotate(-2deg)}80%{transform:translateX(0) rotate(1deg)}}'
    + '#kpw-tab svg{width:24px;height:auto}#kpw-tab i{width:8px;height:8px;border-radius:50%;background:#B3261E;animation:kpwp 1.4s infinite}'
    + '@keyframes kpwp{50%{transform:scale(1.6);opacity:.45}}'
    + '@media (max-width:1023px){#kpw-tab{top:auto;bottom:96px;padding:7px 5px 8px;gap:5px;font-size:9px;letter-spacing:.14em}#kpw-tab svg{width:15px}#kpw-tab i{width:6px;height:6px}}'
    + '#kpw{position:fixed;left:0;top:50%;z-index:56;width:min(350px,calc(100vw - 28px));max-height:calc(100vh - 40px);overflow:auto;transform:translate(-110%,-50%);transition:transform .35s ease;background:#171A36;color:#ECE6D8;border:1px solid #2A2D52;border-left:0;border-radius:0 16px 16px 0;padding:16px 18px 16px;box-shadow:0 12px 40px rgba(0,0,0,.55);font:15px/1.45 Manrope,system-ui,sans-serif}'
    + '#kpw.on{transform:translate(0,-50%)}#kpw .h{display:flex;justify-content:space-between;align-items:center;gap:10px}'
    + '#kpw .h b{color:#C9A45C;font-size:11px;letter-spacing:.16em;text-transform:uppercase}'
    + '#kpw .x{background:transparent;border:1px solid #2A2D52;color:#9492B3;border-radius:999px;width:28px;height:28px;cursor:pointer;font-size:16px;line-height:1}#kpw .x:hover{color:#C9A45C;border-color:#C9A45C}'
    + '#kpw h3{margin:8px 0 4px;font:600 22px/1.2 "Cormorant Garamond",Georgia,serif}#kpw .sub{display:inline-block;margin:2px 0 6px;color:rgba(201,164,92,.8);font-size:12.5px;line-height:1.4;text-decoration:underline;text-decoration-color:rgba(201,164,92,.4);text-underline-offset:3px}#kpw .sub:hover{color:#C9A45C}'
    + '#kpw .o{display:block;width:100%;text-align:left;margin:7px 0;padding:10px 13px;border:1px solid #2A2D52;border-radius:12px;background:transparent;color:#ECE6D8;font:14px Manrope,system-ui,sans-serif;cursor:pointer}#kpw .o:hover{border-color:#C9A45C}'
    + '#kpw .r{position:relative;overflow:hidden;margin:7px 0;padding:10px 13px;border:1px solid #2A2D52;border-radius:12px;display:flex;justify-content:space-between;gap:10px;font-size:14px}#kpw .r.me{border-color:#C9A45C}'
    + '#kpw .r s{position:absolute;inset:0 auto 0 0;background:rgba(201,164,92,.16);text-decoration:none}#kpw .r em,#kpw .r strong{position:relative;font-style:normal}'
    + '#kpw .m{margin:8px 0 0;color:#9492B3;font-size:12px;display:flex;align-items:center;gap:6px}#kpw .dot{width:8px;height:8px;border-radius:50%;background:#3FBF6F;box-shadow:0 0 0 3px rgba(63,191,111,.2)}'
    + '#kpw .ft{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid #2A2D52}'
    + '#kpw .b1,#kpw .b2{display:inline-flex;align-items:center;font:600 13px/1 Manrope,system-ui,sans-serif;border-radius:999px;padding:9px 14px;text-decoration:none}'
    + '#kpw .b1{background:#C9A45C;color:#0E1024}#kpw .b1:hover{background:#D8B46B;color:#0E1024}#kpw .b2{border:1px solid #2A2D52;color:#ECE6D8}#kpw .b2:hover{border-color:#C9A45C;color:#ECE6D8}';
  getPolls().then(function (d) {
    var polls = (d && d.polls) || [];
    notifyClosed(polls);
    var poll = polls.filter(function (p) { return Date.parse(p.closes_at) > Date.now(); })[0];
    if (!poll || location.pathname === poll.article_url) return;     // a cikk alján ott a saját szavazása
    return fetch('/api/poll?id=' + encodeURIComponent(poll.id)).then(function (r) { return r.json(); }).then(function (st) {
      if (!st.ok) return;
      var s = document.createElement('style'); s.textContent = CSS; document.head.appendChild(s);
      var tab = document.createElement('button'); tab.id = 'kpw-tab'; tab.type = 'button'; tab.className = 'off'; tab.setAttribute('aria-label', 'A nap kérdése – szavazás');
      tab.innerHTML = '<svg viewBox="-480 -340 960 680" aria-hidden="true"><g transform="translate(-20 60) rotate(-24)"><ellipse rx="430" ry="130" fill="none" stroke="#0E1024" stroke-width="64"/></g><circle cx="352" cy="-39" r="74" fill="#0E1024"/></svg><span>SZAVAZZ</span>' + (st.voted === null ? '<i></i>' : '');
      var box = document.createElement('div'); box.id = 'kpw'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'A nap kérdése');
      document.body.appendChild(tab); document.body.appendChild(box);
      function head() {
        return '<div class="h"><b>A nap kérdése</b><button type="button" class="x" aria-label="Bezár">×</button></div><h3>' + esc(poll.question) + '</h3>'
          + '<a class="sub" href="' + esc(poll.article_url) + '">A cikk: ' + esc(poll.article_title) + '</a>';
      }
      function foot(meta) {
        return '<p class="m">' + meta + '</p><div class="ft"><a class="b2" href="/szavazasok/">Korábbi szavazások</a><a class="b1" href="' + esc(poll.article_url) + '">Cikk</a></div>';
      }
      function status(r) { return r.closed ? 'lezárult' : '<span class="dot"></span>nyitva'; }
      function show(r) {
        var t = r.total || 0;
        box.innerHTML = head() + poll.options.map(function (o, i) {
          var p = t ? Math.round(100 * (r.counts[i] || 0) / t) : 0;
          return '<div class="r' + (r.voted === i ? ' me' : '') + '"><s style="width:' + p + '%"></s><em>' + esc(o) + (r.voted === i ? ' ✓' : '') + '</em><strong>' + p + '%</strong></div>';
        }).join('') + foot(status(r) + (t >= 200 ? ' · ' + t + ' szavazat' : ''));
      }
      function ask() {
        box.innerHTML = head() + poll.options.map(function (o, i) { return '<button type="button" class="o" data-i="' + i + '">' + esc(o) + '</button>'; }).join('')
          + foot(status(st));
      }
      (st.voted !== null || st.closed) ? show(st) : ask();
      function appear() { if (!tab.classList.contains('off') || box.classList.contains('on')) return; tab.classList.remove('off'); setTimeout(function () { tab.classList.add('wig'); }, 400); }
      setTimeout(appear, 500);  // fél mp után jön elő (gépen és mobilon is)
      // amíg meg nem nyitják, időnként újra megrándul (max. 5-ször), hogy feltűnjön
      var wigs = 0, wigT = setInterval(function () {
        if (box.classList.contains('on') || tab.classList.contains('off') || ++wigs > 5) { if (wigs > 5) clearInterval(wigT); return; }
        tab.classList.remove('wig'); void tab.offsetWidth; tab.classList.add('wig');
      }, 12000);
      function open() { box.classList.add('on'); tab.classList.add('off'); tab.classList.remove('wig'); clearInterval(wigT); var i = tab.querySelector('i'); if (i) i.remove(); }
      function close() { box.classList.remove('on'); tab.classList.remove('off'); }
      tab.addEventListener('click', open);
      // bárhová máshová kattintva visszahúzódik – és ilyenkor a kattintás nem nyit meg semmi mást
      document.addEventListener('click', function (e) {
        if (!box.classList.contains('on') || box.contains(e.target) || tab.contains(e.target)) return;
        e.preventDefault(); e.stopPropagation(); close();
      }, true);
      box.addEventListener('click', function (e) {
        if (e.target.closest('.x')) return close();
        var b = e.target.closest('.o'); if (!b) return;
        box.querySelectorAll('.o').forEach(function (x) { x.disabled = true; });
        fetch('/api/poll', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: poll.id, option: Number(b.dataset.i) }) })
          .then(function (r) { return r.json(); }).then(function (r) { if (r.counts) show(r); else throw 0; })
          .catch(function () { box.querySelectorAll('.o').forEach(function (x) { x.disabled = false; }); });
      });
      document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    });
  }).catch(function () { /* nincs szavazás → nincs fül */ });
})();
