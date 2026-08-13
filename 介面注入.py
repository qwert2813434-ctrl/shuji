#!/usr/bin/env python3
"""閱讀介面三件事，注入所有書頁（頁面即正本，meditations 是新書樣板）：

1. 工具列自動收合——往下滑收起來、稍微往上滑就回來，手機電腦同一套。
2. 筆記「跳到原文」——點一下關面板、切章、捲到那句並閃一下。
3. 設定面板——課文字級滑桿（16–26）＋主題切換（米白鈕移進來，工具列才擠得下）。

  python3 介面注入.py            # 全部書頁
  python3 介面注入.py --check    # 只檢查，不改檔
"""
import argparse, datetime, re, subprocess, tempfile
from pathlib import Path

SITE = Path(__file__).resolve().parent / "跟讀器"
MARK = "/* ── 閱讀介面"

CSS = """
/* 收合用高度不用 transform——Safari 對 sticky 元素套 transform 會讓吸頂失效，
   工具列一藏就再也回不來（實機 iPhone＋Mac 都中，Chrome 沒事所以測不到） */
header{transition:max-height .3s ease, padding-top .3s ease; overflow:hidden; max-height:240px}
header{position:fixed; top:0; left:0; right:0}
header.hid{max-height:0; padding-top:0; border-bottom:0}
body{padding-top:var(--hh,105px)}
.peek{display:none; position:fixed; z-index:25; top:calc(10px + env(safe-area-inset-top));
  left:0; right:0; margin:0 auto; width:fit-content; background:var(--surface); color:var(--ink2);
  border:1px solid var(--line); border-radius:18px; padding:7px 16px; font:inherit;
  font-size:12px; letter-spacing:.14em; cursor:pointer;
  box-shadow:0 8px 22px -10px rgba(0,0,0,.5)}
header.hid + .peek{display:block}
@keyframes npfl{from{background:var(--accent); color:var(--bg)}to{background:none}}
.para span.fl{animation:npfl 1.4s ease-out}
.nit .go{background:none; border:0; color:var(--faint); font:inherit; font-size:12px;
  cursor:pointer; padding:5px 0; margin-right:18px}
.nit .go:active{color:var(--accent)}
.strow{display:flex; align-items:center; gap:14px; padding:2px 0 6px}
.strow input{flex:1; accent-color:var(--accent); height:5px; cursor:pointer}
.strow output{font-size:14px; color:var(--ink2); min-width:2em; text-align:right;
  font-variant-numeric:tabular-nums}
"""

PANEL = """
<div class="np" id="st" hidden>
  <div class="nph"><b>設定</b><span class="sp"></span>
    <button class="tbtn" id="stclose">關閉</button></div>
  <div class="npb">
    <div class="nch">課文字級</div>
    <div class="strow"><input type="range" id="fs" min="16" max="26" step="1" value="19"><output id="fsv">19</output></div>
    <div class="nch">主題</div>
    <button class="tbtn" id="theme">米白</button>
  </div>
</div>
"""

JS = r"""
/* ── 閱讀介面 ── */

/* 工具列收合：直接看手指方向，不看捲動事件。
   捲動事件跟播放的自動跟捲攪在一起，時序怎麼濾都會漏（實機兩次沒拉出來）；
   手指方向是純使用者訊號，自動捲動從頭到尾不參與。電腦看滾輪方向。 */
(() => {
  const hd = document.querySelector('header');
  /* header 是 fixed（sticky 被 overflow-x:hidden 廢掉，從第一天就沒吸過頂），
     內容的頂部空間用量出來的實際高度補 */
  const fit = () => { if (!hd.classList.contains('hid'))
    document.documentElement.style.setProperty('--hh', hd.offsetHeight + 'px'); };
  fit(); setTimeout(fit, 350); addEventListener('resize', fit);
  const show = () => hd.classList.remove('hid');
  const hide = () => { if (scrollY > 150) hd.classList.add('hid'); };
  let ty = 0, tacc = 0, skip = false;
  addEventListener('touchstart', e => {
    ty = e.touches[0].clientY; tacc = 0;
    skip = !!e.target.closest('.player, .np, .pop');   /* 拖進度條、面板裡的手勢不算 */
  }, { passive: true });
  addEventListener('touchmove', e => {
    if (skip) return;
    const y = e.touches[0].clientY, d = y - ty; ty = y;
    if ((d > 0) !== (tacc > 0)) tacc = 0;
    tacc += d;
    if (tacc > 18) show();            /* 手指往下撥＝要看選單 */
    else if (tacc < -70) hide();      /* 手指往上滑＝往下讀 */
  }, { passive: true });
  let wacc = 0, wt = 0;
  addEventListener('wheel', e => {
    const now = Date.now();
    if (now - wt > 300) wacc = 0;
    wt = now; wacc += e.deltaY;
    if (wacc < -30) { wacc = 0; show(); }
    else if (wacc > 140) { wacc = 0; hide(); }
  }, { passive: true });
  addEventListener('scroll', () => { if (scrollY < 70) show(); }, { passive: true });
  /* 保證路徑：手勢在部分實機上叫不出來（回報三次），
     收合時頂端固定顯示一顆「選單」，點了一定回來——純點擊沒有時序可失敗 */
  document.getElementById('peek').onclick = () => show();
})();

/* 筆記跳回原文：切章（不自動播）、捲到那句、閃一下 */
function jumpToNote(c, si) {
  $('np').hidden = true;
  if (!DATA[ci] || DATA[ci].n !== c) {
    const i = DATA.findIndex(x => x.n === c);
    if (i < 0) return;
    load(i, false);
  }
  const el = spans[si];
  if (!el) return;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  el.classList.add('fl');
  setTimeout(() => el.classList.remove('fl'), 1500);
}

/* 字級：全站記住。跟主題一樣是這台裝置的偏好，不進同步 */
function setFs(v) {
  v = Math.min(26, Math.max(16, Number(v) || 19));
  document.documentElement.style.setProperty('--fs', v + 'px');
  localStorage.setItem('nn-fs', String(v));
  $('fs').value = v; $('fsv').textContent = v;
}
if (localStorage.getItem('nn-fs')) setFs(localStorage.getItem('nn-fs'));
$('fs').oninput = e => setFs(e.target.value);
$('cfg').onclick = () => { $('st').hidden = false; };
$('stclose').onclick = () => { $('st').hidden = true; };
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


def do(p):
    s = p.read_text("utf-8")
    lbl = p.parent.name
    if MARK in s:
        return "已注入過，跳過"

    # 字級走 CSS 變數；沒設定時維持現狀（桌機 19、手機 17）
    s = sub(s, ".para{font-size:19px;", ".para{font-size:var(--fs,19px);", lbl + " 字級")
    s = sub(s, ".para{font-size:17px;", ".para{font-size:var(--fs,17px);", lbl + " 手機字級")

    s = sub(s, ".spst.bad{color:var(--accent)}", ".spst.bad{color:var(--accent)}" + CSS, lbl + " CSS")

    # 米白鈕移進設定面板，位置換成「設定」——工具列在窄手機上快擠不下了
    s = sub(s, '<button class="tbtn" id="theme">米白</button>',
            '<button class="tbtn" id="cfg">設定</button>', lbl + " 工具列")
    s = sub(s, "</header>", '</header>\n<button class="peek" id="peek">選單</button>',
            lbl + " 選單鈕")
    s = sub(s, '<div class="np" id="sp" hidden>', PANEL + '\n<div class="np" id="sp" hidden>',
            lbl + " 面板")

    s = sub(s, """'<button class="del">刪除</button></div>';""",
            """'<button class="go">跳到原文</button><button class="del">刪除</button></div>';""",
            lbl + " 跳回鈕")
    s = sub(s, """    el.querySelector('.del').onclick = () => {
      ks.forEach(k => hlDel(k)); npRender(); paintHl();
    };""",
            """    el.querySelector('.go').onclick = () => {
      const q = ks[0].split('-'); jumpToNote(Number(q[0]), Number(q[1]));
    };
    el.querySelector('.del').onclick = () => {
      ks.forEach(k => hlDel(k)); npRender(); paintHl();
    };""", lbl + " 跳回事件")

    s = sub(s, "if (!DATA.length) $('body').innerHTML",
            JS + "\nif (!DATA.length) $('body').innerHTML", lbl + " 主程式")

    stamp = datetime.datetime.now().strftime("%m%d-%H%M")
    s2 = re.sub(r'(id="ver"[^>]*>)\d{4}-\d{4}(</button>)', r"\g<1>" + stamp + r"\g<2>", s)
    if s2 == s:
        raise SystemExit(lbl + "：版本戳沒換到")
    check_js(s2, lbl)
    p.write_text(s2, "utf-8")
    return "已注入"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    for p in sorted(SITE.glob("*/index.html")):
        lbl = p.parent.name
        if a.check:
            print(f"  {lbl:<14}{'有' if MARK in p.read_text('utf-8') else '沒有'}介面調整")
        else:
            print(f"  {lbl:<14}{do(p)}")


if __name__ == "__main__":
    main()
