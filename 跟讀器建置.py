#!/usr/bin/env python3
"""把 速讀/（唸出來的）＋ 譯文/（螢幕上看的）合成語音並打包成跟讀器。

資料契約（照 meditations 逆推）：
  {title, duration, paras[], points[{t, src[i,j], start, end, sents[{s,start,end,w[[字,相對start,相對end]]}]}]}
  points = 唸出來的濃縮句群；src = 對應到 paras 的段落區間；w = edge-tts WordBoundary。
用法： python3 跟讀器建置.py <書名> [--slug petroski] [--voice zh-TW-HsiaoChenNeural] [--only 01]
"""
import argparse, asyncio, json, re, shutil, subprocess, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "輸出"
SITE = BASE / "跟讀器"
POINT_CHARS = 120          # 一個 point 約 120 字，對齊 meditations 的 2198/18
FFMPEG = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")


def split_sents(text):
    """切句：句號問號驚嘆分號後斷開，保留標點。"""
    return [s for s in re.findall(r"[^。！？；\n]*[。！？；]|[^。！？；\n]+", text) if s.strip()]


def group_points(sents, limit=POINT_CHARS):
    """句子併成 point，每組約 limit 字。"""
    out, buf, n = [], [], 0
    for s in sents:
        if buf and n + len(s) > limit:
            out.append(buf); buf, n = [], 0
        buf.append(s); n += len(s)
    if buf:
        out.append(buf)
    return out


def _grams(t):
    t = re.sub(r"[^一-鿿]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def align_src(point_texts, paras, max_span=None, max_skip=None):
    """每個 point 對回 paras 的單調區間。濃縮是順序進行的，所以整體是一條
    不回頭的路徑：相鄰 point 的區間只能共用一個邊界段（a==e）或往後跳過
    最多 max_skip 段（整段被濃縮丟掉的情況）。分數＝point 的中文 bigram
    被區間覆蓋的比例，區間每多吃一段扣一點，避免無限擴張。
    span／skip 上限跟著濃縮比走——對話體一個 point 吃十幾段是常態，
    上限寫死 8 的話 DP 跟不上進度，整章從中段開始一路落後。"""
    P, Q = [_grams(t) for t in point_texts], [_grams(p) for p in paras]
    n, m = len(P), len(Q)
    if not n or not m:
        return [[0, 0]] * n
    r = m / n
    max_span = max_span or max(8, round(r * 2.5))
    max_skip = max_skip or max(4, round(r * 1.2))
    pen = 0.16 / max_span

    memo = {}   # (i, a) -> [score(a..a), score(a..a+1), ...] 增量算，b 越長只加新覆蓋
    def score(i, a, b):
        k = (i, a)
        if k not in memo:
            g, acc, scores = P[i], set(), []
            for bb in range(a, min(m, a + max_span)):
                acc |= (g & Q[bb])
                scores.append((len(acc) / len(g) if g else 0.0) - pen * (bb - a))
            memo[k] = scores
        return memo[k][b - a]

    NEG = float("-inf")
    best = [[NEG] * m for _ in range(n)]   # best[i][b] = point 0..i 對完、i 的區間結束在 b
    back = [[None] * m for _ in range(n)]
    for b in range(min(m, max_span + max_skip)):
        for a in range(max(0, b - max_span + 1), b + 1):
            s = score(0, a, b)
            if s > best[0][b]:
                best[0][b], back[0][b] = s, (a, None)
    for i in range(1, n):
        for e in range(m):
            if best[i - 1][e] == NEG:
                continue
            for a in range(e, min(m, e + max_skip + 2)):
                for b in range(a, min(m, a + max_span)):
                    s = best[i - 1][e] + score(i, a, b)
                    if s > best[i][b]:
                        best[i][b], back[i][b] = s, (a, e)
    end = max(range(m), key=lambda b: best[n - 1][b])
    if best[n - 1][end] == NEG:   # 對不出路徑就退回等比估算
        return [[int(i / n * m), max(int(i / n * m), int((i + 1) / n * m) - 1)] for i in range(n)]
    out = [None] * n
    for i in range(n - 1, -1, -1):
        a, e = back[i][end]
        out[i] = [a, end]
        end = e
    return out


async def synth(text, voice, mp3_path):
    """合成整章，回傳 [(字串, 起, 訖)] 秒。"""
    import edge_tts
    import edge_tts.communicate
    # 2026-10-12 微軟舊主機 speech.platform.bing.com 合成一律 403（edge-tts #490），改走新主機（照 PR #491）
    edge_tts.communicate.WSS_URL = ("wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
                                    "?Ocp-Apim-Subscription-Key=" + edge_tts.constants.TRUSTED_CLIENT_TOKEN)
    marks = []
    with open(mp3_path, "wb") as f:
        # edge-tts 7.x 預設是 SentenceBoundary，逐字時間戳一定要指定 WordBoundary
        async for c in edge_tts.Communicate(text, voice, boundary="WordBoundary").stream():
            if c["type"] == "audio":
                f.write(c["data"])
            elif c["type"] == "WordBoundary":
                marks.append((c["text"], c["offset"] / 1e7, (c["offset"] + c["duration"]) / 1e7))
    return marks


def attach(marks, sents):
    """把 WordBoundary 依序分配給每個句子——marks 的順序與文字順序一致，
    所以照句子字數往下吃即可，不需要做模糊比對。"""
    out, i = [], 0
    for s in sents:
        need = len(re.sub(r"[^\w]", "", s))
        got, w = 0, []
        while i < len(marks) and got < need:
            t, a, b = marks[i]
            w.append([t, a, b]); got += len(t); i += 1
        if not w:
            continue
        out.append({"s": s, "start": round(w[0][1], 3), "end": round(w[-1][2], 3),
                    "w": [[t, round(a - w[0][1], 3), round(b - w[0][1], 3)] for t, a, b in w]})
    return out


def build_chapter(spoken, paras, title, voice, mp3, m4a):
    sents = split_sents(spoken)
    marks = asyncio.run(synth(spoken, voice, mp3))
    timed = attach(marks, sents)
    if not timed:
        raise RuntimeError("拿不到時間戳")

    groups = group_points([t["s"] for t in timed])
    by_text = {t["s"]: t for t in timed}
    kept = [(g, [by_text[s] for s in g if s in by_text]) for g in groups]
    kept = [(g, ts) for g, ts in kept if ts]
    # src：真對齊（濃縮段 vs 譯文段做 bigram 覆蓋率＋單調 DP），舊版等比估算會飄
    srcs = align_src(["".join(g) for g, _ in kept], paras)
    points = [{"t": "".join(g), "src": src,
               "start": ts[0]["start"], "end": ts[-1]["end"], "sents": ts}
              for (g, ts), src in zip(kept, srcs)]

    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(mp3),
                    "-c:a", "aac", "-b:a", "64k", str(m4a)], check=True)
    mp3.unlink(missing_ok=True)
    return {"title": title, "duration": round(points[-1]["end"], 2),
            "points": points, "paras": paras}


def write_page(dest, slug, author, title):
    """照既有書的樣板產 index.html。DATA 那一行的格式是
    const DATA = [...], TOTAL = n, BOOK = "書名", SLUG = "slug";  ——四個值同一行。"""
    tpl = (SITE / "meditations" / "index.html").read_text("utf-8").splitlines()
    chs = sorted(dest.glob("ch*.json"), key=lambda p: p.name)
    data = []
    for n, jf in enumerate(chs, 1):
        d = json.loads(jf.read_text("utf-8"))
        d["audio"] = "ch%02d.m4a" % n
        d["n"] = n
        data.append(d)

    line = "const DATA = %s, TOTAL = %d, BOOK = %s, SLUG = %s;" % (
        json.dumps(data, ensure_ascii=False), len(data),
        json.dumps(title, ensure_ascii=False), json.dumps(slug, ensure_ascii=False))
    stamp = subprocess.run(["date", "+%m%d-%H%M"], capture_output=True, text=True).stdout.strip()

    out = []
    for i, l in enumerate(tpl, 1):
        if l.startswith("const DATA = "):
            l = line
        elif "沉思錄" in l:
            l = l.replace("沉思錄", title)
        if 'id="ver"' in l:
            l = re.sub(r'>(\d{4}-\d{4})</button>', ">%s</button>" % stamp, l)
        out.append(l)
    (dest / "index.html").write_text("\n".join(out) + "\n", "utf-8")

    (dest / "book.json").write_text(json.dumps(
        {"title": title, "author": author, "chapters": len(data),
         "order": len(list(SITE.glob("*/book.json"))) + 1}, ensure_ascii=False, indent=1), "utf-8")
    print("  index.html %d 章 inline，book.json 已寫" % len(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--title", help="書架上顯示的書名，預設同 book")
    ap.add_argument("--author", default="", help="作者，寫進 book.json")
    ap.add_argument("--voice", default="zh-TW-HsiaoChenNeural")
    ap.add_argument("--only", help="只做這幾章，逗號分隔的章號前綴，例如 01,04")
    a = ap.parse_args()

    src_zh, src_full = OUT / a.book / "速讀", OUT / a.book / "譯文"
    for d in (src_zh, src_full):
        if not d.exists():
            sys.exit("找不到 %s" % d)

    dest = SITE / a.slug
    dest.mkdir(parents=True, exist_ok=True)
    files = sorted(src_zh.glob("*.txt"))
    if a.only:
        want = tuple(s.strip() for s in a.only.split(","))
        files = [f for f in files if f.name.startswith(want)]

    for n, f in enumerate(files, 1):
        stem = "ch%02d" % n
        jf = dest / (stem + ".json")
        if jf.exists():
            print("  跳過 %s" % stem); continue
        spoken = f.read_text("utf-8").strip()
        full = (src_full / f.name).read_text("utf-8")
        paras = [p.strip() for p in full.split("\n\n") if p.strip()]
        title = re.sub(r"^\d+\s*", "", f.stem)
        d = build_chapter(spoken, paras, title, a.voice, dest / (stem + ".mp3"), dest / (stem + ".m4a"))  # noqa
        jf.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        print("  %s  %s  %d points / %d 段 / %.1f 分" %
              (stem, title[:24], len(d["points"]), len(paras), d["duration"] / 60))

    write_page(dest, a.slug, a.author, a.title or a.book)
    print("完成，輸出在 %s" % dest)


if __name__ == "__main__":
    main()
