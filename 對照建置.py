#!/usr/bin/env python3
"""對照版（模式 3 英中對照＋模式 4 生字講解）建置。

吃「對照稿 json」（格式見 開源/對照稿-示範.json），產出：
  ch01-en.m4a   英文軌（逐句，句間留白）
  ch01-mix.m4a  混讀軌（每句英→中）
  ch01-study.m4a 學習軌（每句英→中→生字：字→字義→為什麼用這個字，最後摘要＋總結）
  v/ch01-NNN{n,s,u}.m4a  生字三速：n 正常、s 慢速(-40%)、u 例句
  ch01.json + index.html（照 number/bilingual 模板換 DATA）

時間戳慣例（照《數》ch01 逆推，播放器靠這個做逐字亮字）：
  句/塊的 start,end＝軌內絕對秒；ew/zw/塊w＝相對句首/塊首。
合成走 mp3→wav（PCM 長度精確）→concat→aac——直接 concat mp3 會因解碼 delay
每個邊界漂 ~26ms，30 句就漂掉近一秒，逐字亮字整個對不上。

用法：
  python3 對照建置.py 開源/對照稿-示範.json --out 開源/跟讀器/demo/bilingual
"""
import argparse, asyncio, json, re, shutil, subprocess, wave
from pathlib import Path

BASE = Path(__file__).resolve().parent
FFMPEG = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
TEMPLATE = BASE / "跟讀器/number/bilingual/index.html"
GAP_EN, IG_MIX, GAP_MIX, BG, SG = 0.45, 0.3, 0.6, 0.3, 0.7


async def synth_one(text, voice, rate, mp3, sem):
    import edge_tts
    async with sem:
        marks = []
        with open(mp3, "wb") as f:
            async for c in edge_tts.Communicate(text, voice, rate=rate,
                                                boundary="WordBoundary").stream():
                if c["type"] == "audio":
                    f.write(c["data"])
                elif c["type"] == "WordBoundary":
                    marks.append([c["text"], round(c["offset"] / 1e7, 3),
                                  round((c["offset"] + c["duration"]) / 1e7, 3)])
        mp3.with_suffix(".marks").write_text(json.dumps(marks, ensure_ascii=False))


def wav_of(mp3):
    w = mp3.with_suffix(".wav")
    if not w.exists():
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(mp3),
                        "-ar", "24000", "-ac", "1", str(w)], check=True)
    with wave.open(str(w)) as f:
        return w, round(f.getnframes() / f.getframerate(), 3)


def silence(sec, clips):
    w = clips / ("gap%s.wav" % str(sec).replace(".", "_"))
    if not w.exists():
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=24000:cl=mono", "-t", str(sec), str(w)], check=True)
    return w


def concat(wavs, out, clips):
    lst = clips / (out.stem + ".list")
    lst.write_text("".join("file '%s'\n" % w.resolve() for w in wavs))
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c:a", "aac", "-b:a", "64k", str(out)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft")
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", default=str(TEMPLATE))
    a = ap.parse_args()
    d = json.loads(Path(a.draft).read_text())
    out = Path(a.out); (out / "v").mkdir(parents=True, exist_ok=True)
    clips = out / ".clips"; clips.mkdir(exist_ok=True)
    env, zhv = d["enVoice"], d["zhVoice"]

    # ---- 收集所有要合成的段 ----
    jobs, vi = [], 0          # (key, text, voice, rate)
    for p, para in enumerate(d["paras"]):
        for s, st in enumerate(para["sents"]):
            jobs += [("e%d-%d" % (p, s), st["en"], env, "+0%"),
                     ("z%d-%d" % (p, s), st["zh"], zhv, "+0%")]
            for nt in st.get("notes", []):
                nt["_vi"] = vi
                jobs += [("v%dn" % vi, nt["w"], env, "+0%"),
                         ("v%ds" % vi, nt["w"], env, "-40%"),
                         ("v%du" % vi, nt["use"], env, "+0%"),
                         ("v%dm" % vi, nt["m"], zhv, "+0%"),
                         ("v%dy" % vi, nt["why"], zhv, "+0%")]
                vi += 1
    for i, pt in enumerate(d["summary"]["points"]):
        jobs.append(("sum%d" % i, pt, zhv, "+0%"))
    jobs.append(("take", d["summary"]["take"], zhv, "+0%"))

    async def run_all():
        sem = asyncio.Semaphore(6)
        todo = [synth_one(t, v, r, clips / (k + ".mp3"), sem)
                for k, t, v, r in jobs if not (clips / (k + ".mp3")).exists()]
        print("合成 %d/%d 段…" % (len(todo), len(jobs)))
        await asyncio.gather(*todo)
    asyncio.run(run_all())

    W, DUR, MK = {}, {}, {}
    for k, *_ in jobs:
        W[k], DUR[k] = wav_of(clips / (k + ".mp3"))
        MK[k] = json.loads((clips / (k + ".marks")).read_text())

    # ---- 英文軌 / 混讀軌 / 學習軌 ----
    en_paras, mix_paras, blocks = [], [], []
    en_seq, mix_seq, st_seq = [], [], []
    ce = cm = cs = 0.0
    g_en, g_ig, g_mix, g_bg, g_sg = (silence(x, clips) for x in (GAP_EN, IG_MIX, GAP_MIX, BG, SG))

    def block(key, clip, gap_after, gw):
        nonlocal cs
        b = {"k": key, "start": round(cs, 3), "end": round(cs + DUR[clip], 3), "w": MK[clip]}
        blocks.append(b); st_seq.append(W[clip]); cs += DUR[clip]
        st_seq.append(gw); cs += gap_after

    for p, para in enumerate(d["paras"]):
        ep, mp = [], []
        for s, st in enumerate(para["sents"]):
            e, z = "e%d-%d" % (p, s), "z%d-%d" % (p, s)
            base = {"en": st["en"], "zh": st["zh"], "notes":
                    [{k2: v2 for k2, v2 in nt.items() if k2 != "_vi"} for nt in st.get("notes", [])]}
            ep.append({**base, "start": round(ce, 3), "end": round(ce + MK[e][-1][2], 3), "ew": MK[e]})
            en_seq += [W[e], g_en]; ce += DUR[e] + GAP_EN
            zs = cm + DUR[e] + IG_MIX
            mp.append({**base, "start": round(cm, 3), "zhStart": round(zs, 3),
                       "end": round(zs + MK[z][-1][2], 3), "ew": MK[e], "zw": MK[z]})
            mix_seq += [W[e], g_ig, W[z], g_mix]; cm = zs + DUR[z] + GAP_MIX
            block("%d-%d/en" % (p, s), e, BG, g_bg)
            block("%d-%d/zh" % (p, s), z, BG, g_bg)
            for j, nt in enumerate(st.get("notes", [])):
                v = nt["_vi"]
                block("%d-%d/n%d/w" % (p, s, j), "v%dn" % v, BG, g_bg)
                block("%d-%d/n%d/m" % (p, s, j), "v%dm" % v, BG, g_bg)
                last = j == len(st["notes"]) - 1
                block("%d-%d/n%d/why" % (p, s, j), "v%dy" % v, SG if last else BG, g_sg if last else g_bg)
        en_paras.append({"sents": ep}); mix_paras.append({"sents": mp})
    for i in range(len(d["summary"]["points"])):
        block("sum/%d" % i, "sum%d" % i, BG, g_bg)
    block("sum/take", "take", BG, g_bg)

    concat(en_seq, out / "ch01-en.m4a", clips)
    concat(mix_seq, out / "ch01-mix.m4a", clips)
    concat(st_seq, out / "ch01-study.m4a", clips)

    # ---- 生字三速 ----
    vocab = {}
    for p in d["paras"]:
        for st in p["sents"]:
            for nt in st.get("notes", []):
                v = nt.pop("_vi")
                fs = {}
                for suf, key in (("n", "v%dn" % v), ("s", "v%ds" % v), ("u", "v%du" % v)):
                    m4a = out / "v" / ("ch01-%03d%s.m4a" % (v, suf))
                    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i",
                                    str(clips / (key + ".mp3")), "-c:a", "aac", "-b:a", "48k",
                                    str(m4a)], check=True)
                    fs[suf] = "v/" + m4a.name
                vocab[nt["w"].lower()] = fs

    ch = {"mode": "bilingual", "n": 1, "title": d["chapter"],
          "tracks": {"en": {"audio": "ch01-en.m4a", "duration": round(ce, 2), "paras": en_paras},
                     "mix": {"audio": "ch01-mix.m4a", "duration": round(cm, 2), "paras": mix_paras}},
          "summary": d["summary"], "vocabFiles": vocab,
          "studyAudio": "ch01-study.m4a", "studyBlocks": blocks}
    (out / "ch01.json").write_text(json.dumps(ch, ensure_ascii=False))

    # ---- 頁面：換 DATA、換書名 ----
    tpl = Path(a.template).read_text()
    tpl = re.sub(r"const DATA = \[.*?\], TOTAL = \d+;",
                 lambda _: "const DATA = [%s], TOTAL = 1;" % json.dumps(ch, ensure_ascii=False),
                 tpl, count=1, flags=re.S)
    tpl = tpl.replace("數—科學的語言", d["book"])
    tpl = tpl.replace("Fingerprints", d.get("h1", d["chapter"]))  # 模板 h1 是寫死的章名
    (out / "index.html").write_text(tpl)
    n_sents = sum(len(p["sents"]) for p in d["paras"])
    print("完成：%d 句 / %d 生字 / en %.1f 分・mix %.1f 分・study %.1f 分"
          % (n_sents, vi, ce / 60, cm / 60, cs / 60))


if __name__ == "__main__":
    main()
