#!/usr/bin/env python3
"""給一本書，估四種模式各要多久、多大、機器要跑多久。

係數是《數》與《沉思錄》兩本實測回推的（2026-08-04）：
  中文語速 4.42／4.45 字/秒（兩本幾乎一致，公式可信）
  模式一 3.33／3.71 分每千英文字、1.22／1.35 MB
  edge-tts 8.7–12.6 倍實時、claude CLI 1659 英文字/分、對照翻譯 234 秒/千字

⚠️ 字數要算「真正會翻譯的正文」。第一版拿原始 txt 去算（含版權頁、附錄、
   索引）灌水三成，害《沉思錄》估 1.9 小時、實際 2.8 小時。給資料夾時
   本程式只算 原文/*.txt。

用法：
  python3 估算.py 沉思錄                 # 書名（讀 輸出/<書名>/原文/）
  python3 估算.py 某本書.epub
  python3 估算.py 某本書.epub 對話        # 第二個參數是文體：對話／說明／格言
  python3 估算.py 50625 對話             # 直接給正文英文字數
"""
import sys, os, re, glob, zipfile
from xml.etree import ElementTree as ET

BASE = str(__import__("pathlib").Path(__file__).resolve().parent)

# 文體會大幅影響濃縮率（實測中文字/英文字）：
#   說明文《數》0.89　格言《沉思錄》0.99　對話《柏拉圖》0.57
# 對話體有大量「他說／他回答」可以壓掉，估出來會偏高約四成。
GENRE = {"對話": 0.62, "說明": 1.0, "格言": 1.1}

# 每一千個英文字 →（聽完分鐘, 檔案MB, 機器分鐘, 依賴誰）
# 模式一二取兩本實測的中間值；三四是對照樣本直接量的（樣本自己數字數，沒有灌水問題）
MODES = [
    ("1  速讀", "濃縮中文", "原文四五成，一章 8–20 分", 3.5, 1.28, 1.5, None),
    ("2  精讀", "全文中譯", "整本翻完不刪，聽得到細節", 7.35, 2.72, 1.4, None),
    ("3  進階精讀", "英中對照", "逐句英文＋中文，學英文用", 11.2, 4.31, 5.2, None),
    ("4  學習", "逐句解析＋生字卡", "每句講解、生字有慢速發音", 27.8, 14.6, 14.4, "3"),
]
FIREBASE_STORE, FIREBASE_DAY = 10_000, 360      # 免費層 MB


def words(path):
    """只算會被翻譯的正文。附錄、版權頁、索引算進去會灌水三成。"""
    if re.fullmatch(r"\d+", path):
        return int(path)
    folder = os.path.join(BASE, "輸出", path, "原文")
    if os.path.isdir(folder):
        return sum(len(open(f, encoding="utf-8", errors="ignore").read().split())
                   for f in glob.glob(os.path.join(folder, "*.txt")))
    if path.lower().endswith(".epub"):
        # 直接借 唸書機.py 的抽文，估的字數才會跟真正送去翻譯的一致。
        # 舊版自己數 <p>，對話體的書（柏拉圖）會少算一半。
        import importlib.util
        spec = importlib.util.spec_from_file_location("nsj", os.path.join(BASE, "唸書機.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return sum(len(b.split()) for _, b in m.extract_epub(path))
    return len(open(path, encoding="utf-8", errors="ignore").read().split())


def hm(mins):
    return f"{mins/60:.1f} 小時" if mins >= 60 else f"{mins:.0f} 分"


def wide(s):
    """中日文字元佔兩格，算真實顯示寬度。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def pad(s, n, right=False):
    sp = " " * max(0, n - wide(s))
    return sp + s if right else s + sp


def do(path, genre=None):
    w = words(path)
    g = GENRE.get(genre, 1.0)
    name = os.path.basename(path) if not path.isdigit() else "（自訂）"
    k = w / 1000 * g
    print(f"\n{name}　英文 {w:,} 字"
          + (f"　文體：{genre}（濃縮率 ×{g}）" if genre else "") + "\n")
    print(pad("模式", 12) + pad("內容", 20) + pad("聽完", 11, 1)
          + pad("檔案", 11, 1) + pad("機器", 11, 1) + "   說明")
    print("─" * 92)
    tot_mb = tot_min = 0
    for tag, kind, note, amin, mb, mmin, dep in MODES:
        A, M, T = amin * k, mb * k, mmin * k
        tot_mb += M; tot_min += T
        d = f"（要先做 {dep}）" if dep else ""
        print(pad(tag, 12) + pad(kind, 20) + pad(hm(A), 11, 1)
              + pad(f"{M:.0f} MB", 11, 1) + pad(hm(T), 11, 1) + "   " + note + d)
    print("─" * 92)
    print(pad("四種全開", 32) + pad(hm(sum(m[3] for m in MODES) * k), 11, 1)
          + pad(f"{tot_mb:.0f} MB", 11, 1) + pad(hm(tot_min), 11, 1))
    print(f"\n免費層　佔用 {tot_mb/FIREBASE_STORE*100:.0f}% 的 Firebase 儲存空間"
          f"（10 GB，約放得下 {FIREBASE_STORE//max(tot_mb,1)} 本全開）")
    print(f"　　　　一次全下載 = {tot_mb/FIREBASE_DAY:.1f} 天的免費流量（每天 360 MB）")
    only1 = MODES[0][4] * k
    print(f"　　　　只做速讀的話 {only1:.0f} MB，{FIREBASE_DAY/max(only1,1):.0f} 次全書下載才用完一天\n")


if __name__ == "__main__":
    do(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
