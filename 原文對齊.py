#!/usr/bin/env python3
"""重算既有書的 src（濃縮段 → 譯文段對應）。

舊版建置用等比估算，濃縮不均勻就整章飄掉（器物演化論只有 17% 指對）。
這支對每章跑 跟讀器建置.align_src 真對齊，同時改兩處：
  1. 跟讀器/<書>/chNN.json 的 points[].src
  2. 跟讀器/<書>/index.html 內嵌的 const DATA 行（頁面實際讀的是這份）
不動音檔、不動時間戳，改完 firebase deploy 即生效。
"""
import json, re, subprocess
from pathlib import Path
from 跟讀器建置 import align_src

SITE = Path(__file__).resolve().parent / "跟讀器"
STAMP = subprocess.run(["date", "+%m%d-%H%M"], capture_output=True, text=True).stdout.strip()

for book in sorted(SITE.iterdir()):
    page = book / "index.html"
    if not (book / "book.json").exists() or not page.exists():
        continue
    lines = page.read_text("utf-8").splitlines()
    idx = next(i for i, l in enumerate(lines) if l.startswith("const DATA = "))
    m = re.match(r"const DATA = (\[.*\])(, TOTAL = .*;)\s*$", lines[idx])
    data, tail = json.loads(m.group(1)), m.group(2)

    changed = total = 0
    for d in data:
        srcs = align_src([p["t"] for p in d["points"]], d["paras"])
        for p, s in zip(d["points"], srcs):
            total += 1
            if p.get("src") != s:
                p["src"], changed = s, changed + 1
        jf = book / ("ch%02d.json" % d["n"])
        j = json.loads(jf.read_text("utf-8"))
        for p, s in zip(j["points"], srcs):
            p["src"] = s
        jf.write_text(json.dumps(j, ensure_ascii=False), "utf-8")

    lines[idx] = "const DATA = %s%s" % (json.dumps(data, ensure_ascii=False), tail)
    lines = [re.sub(r'>(\d{4}-\d{4})</button>', ">%s</button>" % STAMP, l)
             if 'id="ver"' in l else l for l in lines]
    page.write_text("\n".join(lines) + "\n", "utf-8")
    print("%-12s %d/%d 段改了 src" % (book.name, changed, total))
