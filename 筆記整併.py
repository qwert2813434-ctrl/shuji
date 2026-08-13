#!/usr/bin/env python3
"""筆記本依段落整併：同一段畫的幾句合成一則，一個引文、一個註解框。

只動顯示層（npRender／npText），儲存還是一句一筆——
動儲存格式會跟既有筆記和同步合併打架，顯示層整併就夠了。
句子在段落裡不相鄰時，中間補「……」，才不會把跳著畫的句子誤讀成連續原文。

直接改跟讀器頁面（頁面即正本：新書拿 meditations 當樣板，改樣板等於改以後每本書）。

  python3 筆記整併.py            # 全部書頁
  python3 筆記整併.py --check    # 只檢查，不改檔
"""
import argparse, re, subprocess, tempfile
from pathlib import Path

SITE = Path(__file__).resolve().parent / "跟讀器"
MARK = "/* ── 筆記依段整併"

OLD_RENDER = """function npRender() {
  const o = hlAll(), ks = hlSorted(o), box = $('npb');
  if (!ks.length) {
    box.innerHTML = '<p class="npe">還沒有畫任何重點。<br>在課文裡<b>長按</b>任何一句就會存到這裡，' +
                    '之後可以加自己的註解，再整份複製出去。</p>';
    return;
  }
  let h = '', last = null;
  ks.forEach(k => {
    const c = Number(k.split('-')[0]);
    if (c !== last) {
      const d = DATA.find(x => x.n === c);
      h += '<div class="nch">第 ' + c + ' 章　' + (d ? esc0(d.title) : '') + '</div>';
      last = c;
    }
    h += '<div class="nit" data-k="' + k + '"><p>' + esc0(o[k].t) + '</p>' +
         '<textarea placeholder="我的註解…">' + esc0(o[k].n || '') + '</textarea>' +
         '<button class="del">刪除</button></div>';
  });
  box.innerHTML = h;
  box.querySelectorAll('.nit').forEach(el => {
    const k = el.dataset.k;
    el.querySelector('textarea').oninput = e => {
      const v = hlAll()[k];
      if (v) hlPut(k, Object.assign({}, v, { n: e.target.value, ts: Date.now() }));
    };
    el.querySelector('.del').onclick = () => {
      hlDel(k); npRender(); paintHl();
    };
  });
}"""

NEW_RENDER = """/* ── 筆記依段整併 ──
   同一段畫的幾句在筆記本裡合成一則：引文接起來（跳句補「……」）、
   一個註解框。儲存還是一句一筆，只有顯示整併——動儲存格式會跟同步合併打架。 */
function paraOf(c) {
  const d = DATA.find(x => x.n === c), map = [];
  if (d) d.points.forEach((p, pi) => p.sents.forEach(() => map.push(pi)));
  return map;
}
function npGroups() {
  const ks = hlSorted(hlAll()), maps = {}, gs = [];
  let cur = null;
  ks.forEach(k => {
    const parts = k.split('-'), c = Number(parts[0]), si = Number(parts[1]);
    const map = maps[c] || (maps[c] = paraOf(c));
    const pi = map[si] === undefined ? -1 : map[si];
    if (cur && cur.c === c && cur.pi === pi && pi >= 0) cur.ks.push(k);
    else { cur = { c: c, pi: pi, ks: [k] }; gs.push(cur); }
  });
  return gs;
}
function npQuote(o, ks) {
  return ks.map((k, i) => {
    if (i === 0) return o[k].t;
    const prev = Number(ks[i - 1].split('-')[1]), now = Number(k.split('-')[1]);
    return (now === prev + 1 ? '' : '……') + o[k].t;
  }).join('');
}
const npNote = (o, ks) => ks.map(k => o[k].n || '').filter(Boolean).join('\\n');
function npRender() {
  const o = hlAll(), gs = npGroups(), box = $('npb');
  if (!gs.length) {
    box.innerHTML = '<p class="npe">還沒有畫任何重點。<br>在課文裡<b>長按</b>任何一句就會存到這裡，' +
                    '之後可以加自己的註解，再整份複製出去。</p>';
    return;
  }
  let h = '', last = null;
  gs.forEach(g => {
    if (g.c !== last) {
      const d = DATA.find(x => x.n === g.c);
      h += '<div class="nch">第 ' + g.c + ' 章　' + (d ? esc0(d.title) : '') + '</div>';
      last = g.c;
    }
    h += '<div class="nit" data-ks="' + g.ks.join(',') + '"><p>' + esc0(npQuote(o, g.ks)) + '</p>' +
         '<textarea placeholder="我的註解…">' + esc0(npNote(o, g.ks)) + '</textarea>' +
         '<button class="del">刪除</button></div>';
  });
  box.innerHTML = h;
  box.querySelectorAll('.nit').forEach(el => {
    const ks = el.dataset.ks.split(',');
    el.querySelector('textarea').oninput = e => {
      /* 整組的註解收在第一句名下，其他句清空——舊的逐句註解會在第一次編輯時被收攏 */
      const now = Date.now();
      ks.forEach((k, i) => {
        const v = hlAll()[k];
        if (v) hlPut(k, Object.assign({}, v, { n: i === 0 ? e.target.value : '', ts: now }));
      });
    };
    el.querySelector('.del').onclick = () => {
      ks.forEach(k => hlDel(k)); npRender(); paintHl();
    };
  });
}"""

OLD_TEXT = """function npText() {
  const o = hlAll(), ks = hlSorted(o);
  let out = '# ' + BOOK + '　讀書筆記\\n', last = null;
  ks.forEach(k => {
    const c = Number(k.split('-')[0]);
    if (c !== last) {
      const d = DATA.find(x => x.n === c);
      out += '\\n## 第 ' + c + ' 章　' + (d ? d.title : '') + '\\n'; last = c;
    }
    out += '\\n> ' + o[k].t + '\\n';
    if (o[k].n) out += '\\n' + o[k].n + '\\n';
  });
  return out;
}"""

NEW_TEXT = """function npText() {
  const o = hlAll();
  let out = '# ' + BOOK + '　讀書筆記\\n', last = null;
  npGroups().forEach(g => {
    if (g.c !== last) {
      const d = DATA.find(x => x.n === g.c);
      out += '\\n## 第 ' + g.c + ' 章　' + (d ? d.title : '') + '\\n'; last = g.c;
    }
    out += '\\n> ' + npQuote(o, g.ks) + '\\n';
    const n = npNote(o, g.ks);
    if (n) out += '\\n' + n + '\\n';
  });
  return out;
}"""


def check_js(html, label):
    body = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(body); p = f.name
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    Path(p).unlink()
    if r.returncode:
        raise SystemExit(f"{label} JS 語法錯誤，已中止：\n" + r.stderr[:700])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    for p in sorted(SITE.glob("*/index.html")):
        lbl = p.parent.name
        s = p.read_text("utf-8")
        if a.check:
            print(f"  {lbl:<14}{'有' if MARK in s else '沒有'}整併")
            continue
        if MARK in s:
            print(f"  {lbl:<14}已整併過，跳過"); continue
        for old, new, what in [(OLD_RENDER, NEW_RENDER, "npRender"), (OLD_TEXT, NEW_TEXT, "npText")]:
            if old not in s:
                raise SystemExit(f"{lbl}：{what} 對不上現有程式，先確認頁面版本")
            s = s.replace(old, new, 1)
        check_js(s, lbl)
        p.write_text(s, "utf-8")
        print(f"  {lbl:<14}已整併")


if __name__ == "__main__":
    main()
