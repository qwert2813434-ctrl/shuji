/* 離線快取：外殼自動存，音檔逐章手動存
   index.html 內嵌章節資料，所以外殼一定要「網路優先」——
   快取優先會讓舊版 index.html 卡死，新章節永遠出不來。 */
const SHELL = 'nn-shell-v1', AUDIO = 'nn-audio-v1';
const FILES = ["/", "/index.html", "/manifest.json", "/icon.png", "/demo/", "/demo/index.html", "/demo/bilingual/", "/demo/bilingual/index.html"];

self.addEventListener('install', e => e.waitUntil(
  caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())));

self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(k => Promise.all(k.filter(x => x !== SHELL && x !== AUDIO).map(x => caches.delete(x))))
    /* 書搬進各自的資料夾後，舊路徑的離線音檔只占空間，清掉 */
    .then(() => caches.open(AUDIO))
    .then(c => c.keys().then(ks => Promise.all(
      ks.filter(r => /^\/ch\d+\.m4a$/.test(new URL(r.url).pathname)).map(r => c.delete(r)))))
    .then(() => self.clients.claim())));

/* 從快取放音檔時要自己切 206，否則 Safari 拖不動進度 */
async function audio(req) {
  const c = await caches.open(AUDIO);
  const hit = await c.match(new Request(req.url));
  if (!hit) return fetch(req);
  const range = req.headers.get('range');
  if (!range) return hit;
  const buf = await hit.arrayBuffer();
  const m = /bytes=(\d*)-(\d*)/.exec(range) || [];
  const start = m[1] ? parseInt(m[1]) : 0;
  const end = m[2] ? Math.min(parseInt(m[2]), buf.byteLength - 1) : buf.byteLength - 1;
  return new Response(buf.slice(start, end + 1), { status: 206, headers: {
    'Content-Type': 'audio/mp4', 'Accept-Ranges': 'bytes',
    'Content-Range': `bytes ${start}-${end}/${buf.byteLength}`,
    'Content-Length': String(end - start + 1) } });
}

/* 網路優先：拿得到就更新快取，拿不到（離線）才回快取 */
async function fresh(req) {
  try {
    const r = await fetch(req, { cache: 'no-store' });
    if (r.ok) (await caches.open(SHELL)).put(req, r.clone());
    return r;
  } catch (e) {
    const hit = await caches.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw e;
  }
}

self.addEventListener('message', e => { if (e.data === 'skip') self.skipWaiting(); });

self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin) return;
  if (u.pathname.endsWith('.m4a')) return e.respondWith(audio(e.request));
  if (e.request.mode === 'navigate' || /\.(html|json|js)$/.test(u.pathname) || u.pathname.endsWith('/'))
    return e.respondWith(fresh(e.request));
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then(r => r || fetch(e.request)));
});
