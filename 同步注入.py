#!/usr/bin/env python3
"""把跨裝置同步注入跟讀器頁面（書架＋每本書）。

為什麼是「注入」而不是重新產頁：現在的管線（跟讀器建置.py 的 write_page）是
拿 meditations/index.html 當樣板複製，所以只要把 meditations 一起注入，
以後新書就自動繼承同步功能。

同步機制：Firestore 一份文件裝全部書的筆記與進度，文件位置＝密語的 SHA-256。
密語只留在裝置上不上傳；沒設密語就完全不連線。走 REST 不載 SDK。

  python3 同步注入.py            # 全部頁面
  python3 同步注入.py --check    # 只檢查有沒有注入過，不改檔
"""
import argparse, datetime, re, subprocess, sys, tempfile
from pathlib import Path

SITE = Path(__file__).resolve().parent / "跟讀器"
MARK = "/* ── 跨裝置同步"

CSS = """
.spin{width:100%; background:var(--surface); border:1px solid var(--line); border-radius:8px;
  color:var(--ink); font:inherit; font-size:15px; padding:11px 13px; margin:4px 0 12px}
.spnote{font-size:13px; line-height:1.95; color:var(--ink2); margin:0 0 18px}
.spnote b{color:var(--ink); font-weight:600}
.spst{font-size:13px; color:var(--faint); margin:16px 0 0; min-height:1.4em}
.spst.bad{color:var(--accent)}
"""

PANEL = """
<div class="np" id="sp" hidden>
  <div class="nph"><b>跨裝置同步</b><span class="sp"></span>
    <button class="tbtn" id="spclose">關閉</button></div>
  <div class="npb">
    <p class="spnote">在手機和電腦輸入<b>同一組密語</b>，筆記與閱讀進度就會互通。<br>
      密語只留在這台裝置、不會上傳——送出去的只有它算出來的位置代碼。<br>
      設定後看下面的<b>位置代碼</b>：兩台顯示一樣，才是同一份資料。</p>
    <input class="spin" id="spkey" type="password" placeholder="輸入密語" autocomplete="off">
    <div class="pact">
      <button id="spsave">設定並同步</button>
      <button id="spnow">立即同步</button>
      <button id="spoff">停用</button>
    </div>
    <p class="spst" id="spst"></p>
  </div>
</div>
"""

# SLUG 在書頁有、在書架沒有，所以 touch() 一律要傳書名進來。
CORE = r"""
/* ── 跨裝置同步 ──────────────────────────────────────────────
   Firestore 一份文件裝全部書的筆記與進度，文件位置＝密語的 SHA-256。
   密語只留在裝置上、不上傳；沒設密語就完全不連線。
   走 REST 不載 SDK：省一整包，也避開主畫面 App 的登入導向坑。 */
const FS = k => 'https://firestore.googleapis.com/v1/projects/YOUR-FIREBASE-PROJECT'
  + '/databases/(default)/documents/sync/' + k;
const SK = () => localStorage.getItem('nn-sync') || '';

async function hashKey(s) {
  /* 先正規化再雜湊：全形轉半形（NFKC）、去掉所有空白、轉小寫。
     手機輸入法很容易打出全形字或多一個空格，看起來一樣、雜湊完全不同，
     兩台就會各同步到各的文件，永遠碰不到面——實際發生過（2026-08-07）。 */
  const t = s.normalize('NFKC').replace(/\s+/g, '').toLowerCase();
  const b = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(t));
  return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
}
/* 位置代碼＝雜湊前四碼。兩台顯示一樣，才是同一份資料 */
const fp = () => SK() ? '・位置代碼 ' + SK().slice(0, 4).toUpperCase() : '';

/* localStorage 是整個站共用，所以任何一頁都掃得到全部書的狀態 */
function localState() {
  const b = {}, get = s => (b[s] = b[s] || { mt: 0, done: {}, pos: {}, hl: {} });
  for (const k of Object.keys(localStorage)) {
    let m;
    if ((m = /^nn-(.+)-(ch|hl|mt)$/.exec(k))) {
      const o = get(m[1]), v = localStorage.getItem(k);
      if (m[2] === 'ch') o.ch = Number(v) || 0;
      else if (m[2] === 'mt') o.mt = Number(v) || 0;
      else { try { o.hl = JSON.parse(v) || {}; } catch (e) {} }
    } else if ((m = /^nn-(.+)-(done|pos)-(\d+)$/.exec(k))) {
      const o = get(m[1]);
      if (m[2] === 'done') o.done[m[3]] = 1;
      else o.pos[m[3]] = Number(localStorage.getItem(k)) || 0;
    }
  }
  return { v: 1, b: b };
}

function applyState(st) {
  for (const s in st.b) {
    const o = st.b[s], p = 'nn-' + s + '-';
    if (o.ch) localStorage.setItem(p + 'ch', String(o.ch));
    localStorage.setItem(p + 'mt', String(o.mt || 0));
    for (const n in (o.done || {})) localStorage.setItem(p + 'done-' + n, '1');
    for (const n in (o.pos || {})) localStorage.setItem(p + 'pos-' + n, String(o.pos[n]));
    localStorage.setItem(p + 'hl', JSON.stringify(o.hl || {}));
  }
}

/* 合併規則：
   筆記   逐條比時間，新的贏（刪除留墓碑，才不會被另一台復活）
   已讀   聯集，標過就是標過
   章與位置  整組取「最近讀過那一台」的，免得舊裝置把新進度蓋掉 */
function mergeState(a, c) {
  const out = { v: 1, b: {} };
  const names = {};
  for (const s in a.b) names[s] = 1;
  for (const s in c.b) names[s] = 1;
  for (const s in names) {
    const blank = { mt: 0, done: {}, pos: {}, hl: {} };
    const x = a.b[s] || blank, y = c.b[s] || blank;
    const newer = (y.mt || 0) > (x.mt || 0) ? y : x;
    const hl = Object.assign({}, x.hl || {});
    for (const k in (y.hl || {})) {
      const p = hl[k], q = y.hl[k];
      if (!p || (q.ts || 0) >= (p.ts || 0)) hl[k] = q;
    }
    out.b[s] = {
      mt: Math.max(x.mt || 0, y.mt || 0),
      /* 兩邊都沒有時間戳（同步功能上線前的舊資料）就取讀比較遠的那台，
         不然電腦的第 3 章會贏過手機的第 7 章 */
      ch: (x.mt || 0) === (y.mt || 0) ? Math.max(x.ch || 0, y.ch || 0)
                                      : (newer.ch || x.ch || y.ch || 0),
      pos: Object.assign({}, x.pos || {}, y.pos || {}, newer.pos || {}),
      done: Object.assign({}, x.done || {}, y.done || {}),
      hl: hl
    };
  }
  return out;
}

function stamp(slug) { localStorage.setItem('nn-' + slug + '-mt', String(Date.now())); }
function touch(slug) { stamp(slug); syncSoon(); }
let syncTimer = null;
function syncSoon() { clearTimeout(syncTimer); syncTimer = setTimeout(() => syncNow(), 4000); }

function syncSay(msg, bad) {
  const el = document.getElementById('spst');
  if (el) { el.textContent = msg; el.classList.toggle('bad', !!bad); }
  const b = document.getElementById('sync');
  if (b) b.textContent = SK() ? '同步' : '同步・未設';
}

let syncing = false;
async function syncNow(manual) {
  const k = SK();
  if (!k) { if (manual) syncOpen(); return; }
  if (syncing) return;
  syncing = true; syncSay('同步中…');
  try {
    let remote = { v: 1, b: {} };
    const r = await fetch(FS(k), { cache: 'no-store' });
    if (r.ok) {
      const j = await r.json();
      const s = j.fields && j.fields.blob && j.fields.blob.stringValue;
      if (s) remote = JSON.parse(s);
    } else if (r.status !== 404) {
      throw new Error('讀取失敗 ' + r.status);
    }
    const merged = mergeState(localState(), remote);
    applyState(merged);
    const w = await fetch(FS(k), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields: { blob: { stringValue: JSON.stringify(merged) } } })
    });
    if (!w.ok) throw new Error('寫入失敗 ' + w.status);
    syncSay('已同步・' + new Date().toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' }) + fp());
    if (typeof afterSync === 'function') afterSync();
  } catch (e) {
    syncSay(e.message || '同步失敗', true);
  }
  syncing = false;
}

function syncOpen() {
  document.getElementById('sp').hidden = false;
  syncSay(SK() ? '已設定密語' + fp() : '尚未設定');
}
document.getElementById('sync').onclick = () => syncOpen();
document.getElementById('spclose').onclick = () => { document.getElementById('sp').hidden = true; };
document.getElementById('spsave').onclick = async () => {
  const v = document.getElementById('spkey').value.trim();
  if (v.length < 4) { syncSay('密語太短，至少四個字', true); return; }
  localStorage.setItem('nn-sync', await hashKey(v));
  document.getElementById('spkey').value = '';
  syncNow(true);
};
document.getElementById('spnow').onclick = () => syncNow(true);
document.getElementById('spoff').onclick = () => {
  localStorage.removeItem('nn-sync'); syncSay('已停用，這台不再同步');
};
syncSay(SK() ? '已設定密語' : '尚未設定');
if (SK()) {
  syncNow();
  addEventListener('visibilitychange', () => { if (!document.hidden) syncNow(); });
}
"""

BOOK_AFTER = r"""
/* 同步完把畫面補上：導覽的已讀點、筆記數、重點底線。
   不會硬把你跳到別章——除非這頁還沒開始播，那才順手切到接續的章。 */
function afterSync() {
  try {
    paintNav();
    hlWrite(hlRaw());
    paintHl();
    const want = Number(localStorage.getItem(K('ch')) || 0);
    if (want && DATA[ci] && DATA[ci].n !== want && au.paused && au.currentTime < 1) {
      const i = DATA.findIndex(c => c.n === want);
      if (i >= 0) load(i, false);
    }
  } catch (e) {}
}
"""

SHELF_AFTER = r"""
/* 同步完重新算每本書的進度字樣 */
function afterSync() { try { paintShelf(); } catch (e) {} }
"""


def check_js(html, label):
    body = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(body); p = f.name
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    Path(p).unlink()
    if r.returncode:
        raise SystemExit(f"{label} JS 語法錯誤，已中止：\n" + r.stderr[:700])


def sub(s, a, b, label):
    if a not in s:
        raise SystemExit(f"{label}：找不到要改的地方 → {a[:70]}")
    return s.replace(a, b, 1)


def do_book(path):
    s = path.read_text("utf-8")
    if MARK in s:
        return "已注入過，跳過"
    lbl = path.parent.name

    s = sub(s, ".para span.past{color:var(--ink2)}",
            ".para span.past{color:var(--ink2)}" + CSS, lbl + " CSS")
    s = sub(s, '<button class="tbtn" id="notes">筆記</button>',
            '<button class="tbtn" id="sync">同步</button>'
            '<button class="tbtn" id="notes">筆記</button>', lbl + " 頂列")
    s = sub(s, '<div class="np" id="np" hidden>', PANEL + '\n<div class="np" id="np" hidden>',
            lbl + " 面板")

    # 筆記改成「刪除留墓碑」，否則另一台同步回來會把刪掉的復活
    s = sub(s, """const hlAll = () => { try { return JSON.parse(localStorage.getItem(K('hl')) || '{}'); }
                      catch (e) { return {}; } };
function hlSave(o) {
  localStorage.setItem(K('hl'), JSON.stringify(o));
  const n = Object.keys(o).length;
  $('notes').textContent = n ? '筆記 ' + n : '筆記';
}""",
            """const hlRaw = () => { try { return JSON.parse(localStorage.getItem(K('hl')) || '{}'); }
                      catch (e) { return {}; } };
/* 刪掉的重點留一個墓碑（只有 d 時間戳），不然另一台同步回來會把它復活 */
const hlAll = () => { const r = hlRaw(), o = {}; for (const k in r) if (!r[k].d) o[k] = r[k]; return o; };
function hlWrite(raw) {
  localStorage.setItem(K('hl'), JSON.stringify(raw));
  const n = Object.keys(raw).filter(k => !raw[k].d).length;
  $('notes').textContent = n ? '筆記 ' + n : '筆記';
}
function hlPut(k, v) { const r = hlRaw(); r[k] = v; hlWrite(r); touch(SLUG); }
/* 墓碑一定要帶 ts，不然合併時被當成時間 0，另一台的舊資料會把刪除蓋回去（踩過） */
function hlDel(k) { const r = hlRaw(); r[k] = { d: 1, ts: Date.now() }; hlWrite(r); touch(SLUG); }""",
            lbl + " 筆記儲存")

    s = sub(s, """  const o = hlAll(), k = n + '-' + si;
  if (o[k]) { delete o[k]; el.classList.remove('hl'); }
  else { o[k] = { t: text, n: '' }; el.classList.add('hl'); }
  hlSave(o);""",
            """  const o = hlAll(), k = n + '-' + si;
  if (o[k]) { hlDel(k); el.classList.remove('hl'); }
  else { hlPut(k, { t: text, n: '', ts: Date.now() }); el.classList.add('hl'); }""",
            lbl + " 長按切換")

    s = sub(s, "      const o2 = hlAll(); if (o2[k]) { o2[k].n = e.target.value; hlSave(o2); }",
            "      const v = hlAll()[k];\n"
            "      if (v) hlPut(k, Object.assign({}, v, { n: e.target.value, ts: Date.now() }));",
            lbl + " 註解輸入")

    s = sub(s, "      const o2 = hlAll(); delete o2[k]; hlSave(o2); npRender(); paintHl();",
            "      hlDel(k); npRender(); paintHl();", lbl + " 刪除")

    s = sub(s, "  paintHl(); hlSave(hlAll());", "  paintHl(); hlWrite(hlRaw());", lbl + " 載入重畫")

    # 進度寫入處補上「這本書剛動過」的時間戳
    s = sub(s, "  localStorage.setItem(K('ch'), d.n);",
            "  localStorage.setItem(K('ch'), d.n);\n"
            "  if (autoplay) touch(SLUG);   /* 初次還原不算動過，否則會蓋掉另一台的新進度 */",
            lbl + " 章進度")
    s = sub(s, "  if (DATA[ci]) localStorage.setItem(K('pos-' + DATA[ci].n), t.toFixed(1));",
            "  if (DATA[ci]) { localStorage.setItem(K('pos-' + DATA[ci].n), t.toFixed(1)); stamp(SLUG); }",
            lbl + " 播放位置")
    s = sub(s, "  localStorage.setItem(K('done-' + DATA[ci].n), '1');",
            "  localStorage.setItem(K('done-' + DATA[ci].n), '1'); touch(SLUG);", lbl + " 已讀")

    # 一定要插在 load() 之前：載入時就會呼叫 touch()，
    # 同步碼擺頁尾的話 syncTimer 還沒宣告，整頁會 TDZ 死掉（踩過）
    s = sub(s, "if (!DATA.length) $('body').innerHTML",
            CORE + BOOK_AFTER + "\nif (!DATA.length) $('body').innerHTML", lbl + " 主程式")

    # 版本戳換成注入當下，使用者才能在手機上確認頁面真的更新了
    stamp = datetime.datetime.now().strftime("%m%d-%H%M")
    s2 = re.sub(r'(id="ver"[^>]*>)\d{4}-\d{4}(</button>)', r"\g<1>" + stamp + r"\g<2>", s)
    if s2 == s:
        raise SystemExit(lbl + "：版本戳沒換到")
    s = s2
    check_js(s, lbl)
    path.write_text(s, "utf-8")
    return "已注入"


def do_shelf(path):
    s = path.read_text("utf-8")
    if MARK in s:
        return "已注入過，跳過"

    s = sub(s, ".wip .cov img{filter:grayscale(.2) brightness(.8)}",
            ".wip .cov img{filter:grayscale(.2) brightness(.8)}" + CSS, "書架 CSS")
    s = sub(s, '<button class="tbtn" id="theme">米白</button>',
            '<button class="tbtn" id="sync">同步</button>'
            '<button class="tbtn" id="theme">米白</button>', "書架頂列")
    s = sub(s, "</main>", "</main>\n" + PANEL, "書架面板")

    # 進度渲染包成函式，同步完才叫得動
    old = "/* 每本書的進度：key 是 nn-<書>-done-N / nn-<書>-ch */\nfor (const b of "
    new = "/* 每本書的進度：key 是 nn-<書>-done-N / nn-<書>-ch */\nfunction paintShelf() {\nfor (const b of "
    s = sub(s, old, new, "書架進度函式")
    s = sub(s, "  el.textContent = b.chapters + ' 章' + (bits.length ? '・' + bits.join('・') : '');\n}",
            "  el.textContent = b.chapters + ' 章' + (bits.length ? '・' + bits.join('・') : '');\n}\n}\npaintShelf();",
            "書架進度收尾")

    s = sub(s, "\n</script>\n</body>", "\n" + CORE + SHELF_AFTER + "</script>\n</body>", "書架主程式")
    check_js(s, "書架")
    path.write_text(s, "utf-8")
    return "已注入"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只回報狀態，不改檔")
    a = ap.parse_args()

    pages = [SITE / "index.html"] + sorted(SITE.glob("*/index.html"))
    if a.check:
        for p in pages:
            has = MARK in p.read_text("utf-8")
            print(f"  {p.parent.name if p.parent != SITE else '（書架）':<14}{'有' if has else '沒有'}同步")
        return
    for p in pages:
        r = do_shelf(p) if p.parent == SITE else do_book(p)
        print(f"  {p.parent.name if p.parent != SITE else '（書架）':<14}{r}")


if __name__ == "__main__":
    main()
