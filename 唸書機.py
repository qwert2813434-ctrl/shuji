#!/usr/bin/env python3
"""唸書機 — 把英文書翻成口語中文，唸成有聲書。

翻譯引擎走本機 claude CLI（吃 Max 訂閱，不花 API 錢），
語音走 macOS 內建 say，串檔用 ffmpeg。

    python3 唸書機.py run  某本書.epub
    python3 唸書機.py run  某份報告.pdf --voice Meijia --rate 190

分步跑（想中途檢查譯文就用這個）：
    python3 唸書機.py extract    某本書.epub
    python3 唸書機.py translate  某本書
    python3 唸書機.py speak      某本書
"""

import argparse
import html.parser
import os
import re
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

BASE = Path(__file__).resolve().parent
OUT = BASE / "輸出"
CHUNK_CHARS = 3500        # 每次丟給 claude 的原文長度
MIN_CHAPTER_CHARS = 500   # 比這短的當成封面／目錄，跳過


# ---------------------------------------------------------------- 抽文字

class _Text(html.parser.HTMLParser):
    """把 XHTML 轉純文字，段落之間留空行。"""

    SKIP = {"script", "style", "head", "sup", "sub"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    # 註腳號碼會黏在字尾（Verus1），唸出來是「Verus 一」。
    # Standard Ebooks 不包 sup，用 epub:type／role 標，得靠屬性認。
    NOTE = {"noteref", "doc-noteref", "endnote", "footnote"}

    # 編者導讀是獨立 section（id 形如 crito-introduction），要丟就整段丟
    INTRO = re.compile(r"-(introduction|preface)$")

    def __init__(self, skip_intro=False):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_intro = skip_intro
        self._skip = 0          # >0 表示在被跳過的子樹裡，用深度計數才擋得住巢狀

    def _drop(self, tag, attrs):
        if tag in self.SKIP:
            return True
        d = dict(attrs)
        if any(v in self.NOTE for v in d.values() if v):
            return True
        return bool(self.skip_intro and self.INTRO.search(d.get("id") or ""))

    def handle_starttag(self, tag, attrs):
        if self._skip:
            self._skip += 1
        elif self._drop(tag, attrs):
            self._skip = 1
        elif tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t ]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", raw)
        return raw.strip()


def _html_to_text(data, skip_intro=False):
    p = _Text(skip_intro)
    try:
        p.feed(data.decode("utf-8", "ignore"))
    except Exception:
        return ""
    return p.text()


# Standard Ebooks 每本都夾這幾頁，不是內文。
# 要用開頭比對——長書的尾註會拆成 endnotes-1／endnotes-2，全等比對擋不掉。
BOILERPLATE = {"imprint", "colophon", "uncopyright", "endnotes", "titlepage",
               "halftitlepage", "loi", "dedication", "appendix"}


def is_boilerplate(stem):
    stem = stem.lower()
    return any(stem == s or stem.startswith(s + "-") for s in BOILERPLATE)


def extract_epub(path, skip_intro=False):
    """回傳 [(章節標題, 內文), ...]，順序照 EPUB 的 spine。"""
    chapters = []
    with zipfile.ZipFile(path) as z:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile").get("full-path")
        opf = ElementTree.fromstring(z.read(rootfile))
        root_dir = os.path.dirname(rootfile)

        ids = {}
        for item in opf.findall(".//{*}manifest/{*}item"):
            ids[item.get("id")] = item.get("href")

        for ref in opf.findall(".//{*}spine/{*}itemref"):
            href = ids.get(ref.get("idref"))
            if not href:
                continue
            name = os.path.normpath(os.path.join(root_dir, href))
            if is_boilerplate(Path(href).stem):
                continue
            try:
                body = _html_to_text(z.read(name), skip_intro)
            except KeyError:
                continue
            if len(body) < MIN_CHAPTER_CHARS:
                continue
            # 標題可能被拆成幾個 span（<span>Book</span><span>I</span>），要併回一行
            first = re.sub(r"\s+", " ", body.split("\n\n", 1)[0]).strip()
            title = first[:60] if 0 < len(first) < 80 else Path(href).stem
            chapters.append((title, body))
    return chapters


# 有些 PDF 的連字（fi/fl/ff…）會被抽成 IPA 音標字元或私用區碼位，
# 不修的話 first→ɹrst、difficult→diɽcult，整本高頻字全毀。
LIGATURES = {"ɹ": "fi", "ɻ": "fl", "ʃ": "ff", "ɽ": "ffi", "ʀ": "ffl",
             "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
_LIG = str.maketrans(LIGATURES)


def extract_pdf(path):
    import fitz
    doc = fitz.open(path)
    toc = doc.get_toc()
    pages = [doc[i].get_text().translate(_LIG) for i in range(doc.page_count)]

    if not toc:                       # 沒書籤就每 20 頁切一段
        chapters = []
        for i in range(0, len(pages), 20):
            body = "\n\n".join(pages[i:i + 20]).strip()
            if len(body) >= MIN_CHAPTER_CHARS:
                chapters.append(("第 %d-%d 頁" % (i + 1, min(i + 20, len(pages))), body))
        return chapters

    marks = [(t[1], t[2] - 1) for t in toc if t[0] <= 2]
    chapters = []
    for idx, (title, start) in enumerate(marks):
        end = marks[idx + 1][1] if idx + 1 < len(marks) else len(pages)
        body = "\n\n".join(pages[start:end]).strip()
        if len(body) >= MIN_CHAPTER_CHARS:
            chapters.append((title.strip()[:60], body))
    return chapters


def extract_txt(path):
    body = Path(path).read_text("utf-8", "ignore")
    paras = [p for p in body.split("\n\n") if p.strip()]
    chapters, buf = [], []
    size = 0
    for p in paras:
        buf.append(p)
        size += len(p)
        if size > 20000:
            chapters.append(("第 %d 段" % (len(chapters) + 1), "\n\n".join(buf)))
            buf, size = [], 0
    if buf:
        chapters.append(("第 %d 段" % (len(chapters) + 1), "\n\n".join(buf)))
    return chapters


MAX_CHAPTER_WORDS = 6000   # 約 20 分鐘語音；再長在跟讀器裡就難用了


def split_long(chapters):
    """太長的章切成「章名・之N」。柏拉圖的斐多篇一篇 4.3 萬字＝2.5 小時，
    不切就變成一個沒辦法導航的巨章。照段落邊界切，不切在句子中間。"""
    out = []
    for title, body in chapters:
        paras = [p for p in body.split("\n\n") if p.strip()]
        if sum(len(p.split()) for p in paras) <= MAX_CHAPTER_WORDS:
            out.append((title, body))
            continue
        buf, n, part = [], 0, 1
        for p in paras:
            w = len(p.split())
            if buf and n + w > MAX_CHAPTER_WORDS:
                out.append((f"{title}・之{part}", "\n\n".join(buf)))
                buf, n, part = [], 0, part + 1
            buf.append(p); n += w
        if buf:
            out.append((f"{title}・之{part}", "\n\n".join(buf)))
    return out


def cmd_extract(args):
    src = Path(args.file).expanduser()
    if not src.exists():
        sys.exit("找不到檔案：%s" % src)

    suffix = src.suffix.lower()
    if suffix == ".epub":
        chapters = extract_epub(src, getattr(args, "skip_intro", False))
    elif suffix == ".pdf":
        chapters = extract_pdf(src)
    else:
        chapters = extract_txt(src)

    if not chapters:
        sys.exit("抽不到內文。若是 DRM 保護的書，這條路走不通。")

    if getattr(args, "only", None):
        want = {s.strip().lower() for s in args.only.split(",")}
        chapters = [(t, b) for t, b in chapters if t.strip().lower() in want]
        if not chapters:
            sys.exit("--only 沒對到任何章，章名要跟抽出來的一致")

    before = len(chapters)
    chapters = split_long(chapters)
    if len(chapters) != before:
        print("有長章切開：%d → %d 章（每章上限 %d 字）" % (before, len(chapters), MAX_CHAPTER_WORDS))

    book = src.stem
    raw = OUT / book / "原文"
    raw.mkdir(parents=True, exist_ok=True)
    # 補零位數要跟著章數走，不然 100 會排到 10 跟 11 中間，整本順序亂掉
    pad = max(2, len(str(len(chapters))))
    for i, (title, body) in enumerate(chapters, 1):
        safe = re.sub(r'[/\\:*?"<>|]', "_", title)
        (raw / ("%0*d %s.txt" % (pad, i, safe))).write_text(body, "utf-8")

    total = sum(len(b) for _, b in chapters)
    print("《%s》抽出 %d 章，約 %s 字元" % (book, len(chapters), format(total, ",")))
    print("原文在 %s" % raw)
    return book


# ---------------------------------------------------------------- 翻譯

PROMPT = """你在把一本英文書翻成中文有聲書的稿子。讀者是用耳朵聽，不是用眼睛看。

規則：
1. 忠實翻譯，不刪減、不摘要、不加入原文沒有的內容。
2. 譯成台灣口語中文——像一個人講給朋友聽，不是翻譯腔。
3. 長句拆短，中文一句超過四十字就該斷開。
4. 阿拉伯數字寫成中文讀法：1990 寫成一九九零年，3.5% 寫成百分之三點五。
5. 不要留括號補充，改寫成插入句。
6. 專有名詞一律只寫中文，**絕不附英文原文**——耳朵聽不出括號，中英夾雜非常難聽。
   人名、品牌、公司、機構、型號一律音譯或意譯成中文（例：T pin 寫「T 形別針」要改成「丁字別針」）。
7. 少用「首先／其次／綜上所述」，用口語的「那」「再來」「所以說」。
8. 圖表編號、註腳記號、頁碼、參考文獻，直接略過不翻。
9. 原文可能來自舊書掃描的 OCR，會有辨識錯誤（字母數字混淆、破碎標點）。
   明顯的錯字依上下文自行判讀修正，不要照著錯的翻，也不要在譯文裡提到 OCR。
10. 數學算式：簡單的改寫成口語念法，例如「a 的平方加 b 的平方等於 c 的平方」。
    複雜的、或 OCR 已經糊掉看不出原貌的，寫成「這裡有一段算式，建議看書」，
    不要硬猜、不要編造式子。
11. 每章開頭若有引言或題詞，照翻，並在前面加一句「本章題詞：」。

已建立的術語對照，請沿用以保持前後一致：
{glossary}

把譯文放進 <譯文></譯文>。譯文之後，若這段出現新的重要專有名詞，
用 <術語>中文=English</術語> 每行一個列出。除此之外不要輸出任何說明。"""


CONDENSE_PROMPT = """你在把一份中文有聲書稿濃縮成速讀版。讀者是用耳朵聽。

規則：
1. 濃縮到原文的四到五成。**保留論證骨架與具體案例**，砍掉重複舉例、鋪陳、人名清單。
2. 同一個論點換一個物件再講一次的段落，只留最有力的那一個，其餘刪掉。
3. 具體的數字、年份、版本號、物件名稱要留——那是這本書的價值所在，不是可省的細節。
4. 仍然是一段一段講給人聽的散文，**不要寫成大綱或條列**。
5. 不要加入原文沒有的評論、總結或「本章重點」。
6. **輸出必須是純中文：零英文字母、零阿拉伯數字。** 遇到英文專有名詞用下面的術語對照；
   對照裡沒有的自行音譯或意譯。數字寫成中文讀法。
7. 出版社名、參考文獻、註腳、頁碼、圖表編號一律刪掉。
8. 長句拆短，中文一句超過四十字就斷開。

術語對照，請沿用：
{glossary}

把結果放進 <速讀></速讀>，除此之外不要輸出任何說明。"""


def _call_claude(system_text, body):
    p = subprocess.run(
        ["claude", "-p", system_text],
        input=body, capture_output=True, text=True, timeout=900,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "claude 執行失敗").strip()[:300])
    return p.stdout


def _split(body):
    out, buf, size = [], [], 0
    for para in body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if size + len(para) > CHUNK_CHARS and buf:
            out.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para)
    if buf:
        out.append("\n\n".join(buf))
    return out


def _translate_chapter(src, dst, cache_dir, glossary, lock, prompt=PROMPT, tag="譯文"):
    if dst.exists():
        return "跳過（已完成）"

    chunks = _split(src.read_text("utf-8"))
    pieces = []
    for n, chunk in enumerate(chunks, 1):
        cache = cache_dir / ("%s.%03d.txt" % (dst.stem, n))
        if cache.exists():
            pieces.append(cache.read_text("utf-8"))
            continue

        with lock:
            terms = "\n".join("%s = %s" % (k, v) for k, v in sorted(glossary.items())) or "（尚無）"
        raw = _call_claude(prompt.format(glossary=terms), chunk)

        m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), raw, re.S)
        text = (m.group(1) if m else raw).strip()
        if not text:
            raise RuntimeError("第 %d 段譯文是空的" % n)

        for term in re.findall(r"<術語>(.*?)</術語>", raw, re.S):
            if "=" in term:
                zh, en = term.split("=", 1)
                with lock:
                    glossary.setdefault(zh.strip(), en.strip())

        cache.write_text(text, "utf-8")
        pieces.append(text)

    dst.write_text("\n\n".join(pieces), "utf-8")
    return "%d 段" % len(chunks)


def _run_pass(book, src_name, dst_name, cache_name, prompt, tag, jobs, verb):
    """翻譯與濃縮共用一套：只換 prompt、來源夾、輸出夾、快取夾。"""
    src_dir = OUT / book / src_name
    if not src_dir.exists():
        sys.exit("找不到 %s" % src_dir)

    dst_dir = OUT / book / dst_name
    cache = OUT / book / cache_name
    dst_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    glossary = {}
    gfile = OUT / book / "術語表.txt"
    if gfile.exists():
        for line in gfile.read_text("utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                glossary[k.strip()] = v.strip()

    files = sorted(src_dir.glob("*.txt"))
    print("《%s》共 %d 章，開始%s（%d 條並行）" % (book, len(files), verb, jobs))

    import threading
    lock = threading.Lock()

    def work(src):
        try:
            note = _translate_chapter(src, dst_dir / src.name, cache, glossary, lock, prompt, tag)
            return "  完成  %s  %s" % (src.stem, note)
        except Exception as exc:
            return "  失敗  %s  %s" % (src.stem, exc)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for line in pool.map(work, files):
            print(line, flush=True)

    gfile.write_text(
        "\n".join("%s = %s" % (k, v) for k, v in sorted(glossary.items())), "utf-8"
    )
    print("%s在 %s" % (dst_name, dst_dir))
    print("術語表 %d 條：%s" % (len(glossary), gfile))


def cmd_translate(args):
    _run_pass(args.book, "原文", "譯文", ".分段快取", PROMPT, "譯文", args.jobs, "翻譯")


def cmd_condense(args):
    """模式一 速讀：把全文譯稿濃縮到四五成，順便清掉英數字元。"""
    _run_pass(args.book, "譯文", "速讀", ".濃縮快取", CONDENSE_PROMPT, "速讀", args.jobs, "濃縮")


# ---------------------------------------------------------------- 語音

SAMPLE = ("這是試聽。粗略來說，進到感官的訊息裡面，大概有百分之七十五，"
          "還沒變成意識就先被丟掉了。早在一八九零年，大家就已經懂這件事。")


def cmd_voices(args):
    """列出中文語音並產生試聽檔，同時標出沒安裝、被系統偷換掉的。"""
    import hashlib

    listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    voices = []
    for line in listing.splitlines():
        m = re.match(r"^(.+?)\s{2,}(zh_[A-Z]{2})\s", line)
        if m:
            voices.append((m.group(1).strip(), m.group(2)))
    if not voices:
        sys.exit("找不到任何中文語音")

    dest = OUT / "_試聽"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    print("找到 %d 個中文語音，逐一合成比對……\n" % len(voices))
    digests = {}
    for name, locale in voices:
        tmp = dest / "tmp.aiff"
        try:
            subprocess.run(["say", "-v", name, "-r", "190", "-o", str(tmp), SAMPLE],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError:
            print("  %-34s %s  合成失敗" % (name, locale))
            continue
        digest = hashlib.md5(tmp.read_bytes()).hexdigest()
        digests.setdefault(digest, []).append((name, locale))
        safe = re.sub(r'[/\\:*?"<>|（）()]', "_", name).strip("_")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
                        str(dest / ("%s.m4a" % safe))], check=True)
        tmp.unlink()

    print("結果：")
    for group in digests.values():
        if len(group) == 1:
            print("  ✓ %-32s %s  真的" % (group[0][0], group[0][1]))
        else:
            names = "、".join(n for n, _ in group)
            print("  ✗ 這幾個發出一模一樣的聲音，代表沒安裝、被系統換掉了：\n      %s" % names)
    print("\n試聽檔在 %s" % dest)


def _duration(path):
    """回傳「N 分 N 秒」。沒有 ffprobe 就從 ffmpeg 的訊息裡撈，再不行就算了。"""
    try:
        if shutil.which("ffprobe"):
            secs = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True,
            ).stdout.strip())
        else:
            info = subprocess.run(
                ["ffmpeg", "-i", str(path)], capture_output=True, text=True
            ).stderr
            h, m, s = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", info).groups()
            secs = int(h) * 3600 + int(m) * 60 + float(s)
        return "%d 分 %d 秒" % (secs // 60, secs % 60)
    except Exception:
        return "%.1f MB" % (path.stat().st_size / 1048576)


def cmd_speak(args):
    book = args.book
    zh = OUT / book / "速讀"          # 有速讀版就用它（模式一），沒有才退回全文譯稿
    if not zh.exists():
        zh = OUT / book / "譯文"
    if not zh.exists():
        sys.exit("找不到 %s，先跑 translate" % zh)
    print("語音來源：%s" % zh.name)
    if not shutil.which("ffmpeg"):
        sys.exit("找不到 ffmpeg")

    audio = OUT / book / "音檔"
    audio.mkdir(parents=True, exist_ok=True)
    files = sorted(zh.glob("*.txt"))
    print("《%s》合成 %d 章，語音 %s，語速 %d" % (book, len(files), args.voice, args.rate))

    for i, src in enumerate(files, 1):
        dst = audio / (src.stem + ".m4a")
        if dst.exists() and not args.force:
            print("  跳過  %s" % src.stem)
            continue

        tmp = audio / (src.stem + ".aiff")
        subprocess.run(
            ["say", "-v", args.voice, "-r", str(args.rate), "-f", str(src), "-o", str(tmp)],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
             "-c:a", "aac", "-b:a", "64k",
             "-metadata", "album=%s" % book,
             "-metadata", "title=%s" % src.stem,
             "-metadata", "artist=唸書機",
             "-metadata", "track=%d/%d" % (i, len(files)),
             str(dst)],
            check=True,
        )
        tmp.unlink()

        print("  完成  %s  %s" % (src.stem, _duration(dst)))

    print("音檔在 %s" % audio)


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="把英文書翻成口語中文有聲書")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("voices", help="列出中文語音、產生試聽檔、標出沒安裝的")

    e = sub.add_parser("extract", help="從 epub/pdf/txt 抽出章節原文")
    e.add_argument("file")
    e.add_argument("--only", help="只留這幾章，用逗號分隔章名（文集只想做其中幾篇時用）")
    e.add_argument("--skip-intro", action="store_true", help="丟掉編者導讀，只留正文")

    t = sub.add_parser("translate", help="把原文翻成口語中文")
    t.add_argument("book")
    t.add_argument("--jobs", type=int, default=3, help="並行章節數，預設 3")

    c = sub.add_parser("condense", help="模式一 速讀：把譯文濃縮到四五成，並清掉英數字元")
    c.add_argument("book")
    c.add_argument("--jobs", type=int, default=3, help="並行章節數，預設 3")

    s = sub.add_parser("speak", help="把譯文合成語音")
    s.add_argument("book")
    s.add_argument("--voice", default="Meijia")
    s.add_argument("--rate", type=int, default=190)
    s.add_argument("--force", action="store_true", help="重做已存在的音檔")

    r = sub.add_parser("run", help="抽文字→翻譯→合成，一次跑完")
    r.add_argument("file")
    r.add_argument("--jobs", type=int, default=3)
    r.add_argument("--voice", default="Meijia")
    r.add_argument("--rate", type=int, default=190)
    r.add_argument("--force", action="store_true")

    args = ap.parse_args()

    if args.cmd == "voices":
        cmd_voices(args)
    elif args.cmd == "extract":
        cmd_extract(args)
    elif args.cmd == "translate":
        cmd_translate(args)
    elif args.cmd == "condense":
        cmd_condense(args)
    elif args.cmd == "speak":
        cmd_speak(args)
    else:
        book = cmd_extract(args)
        args.book = book
        cmd_translate(args)
        cmd_speak(args)


if __name__ == "__main__":
    main()
