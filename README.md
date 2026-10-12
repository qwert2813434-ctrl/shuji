# 唸書機

把一本英文書翻成台灣口語中文、唸成有聲書，再包成手機上能「邊聽邊看字」的跟讀器網頁。
翻譯與濃縮交給你手上的 AI agent（例如 claude CLI），語音用 edge-tts（免費、附逐字時間戳），串檔用 ffmpeg——**整條管線零成本**。

這份 README 同時寫給人和 agent 看：把整個 repo 丟給你的 agent，說「照 README 把某本書做成跟讀器」，它應該就能跑完。

**English:** Turn an English book into a Taiwanese-Mandarin audiobook with a read-along web app (PWA). Your coding agent does the translation; voice is free edge-tts. Agents: follow "公式" below.

**給 AI agent：**開工先問使用者「速讀還是精讀」，再照下面「公式」第 1–6 步跑。**完成＝在 `跟讀器/` 開本機伺服器，書架上看得到新書，點第一章能播放、字幕跟著聲音走。** 2026-10-12 起微軟舊配音主機回 403，本 repo 已改連新主機；仍偶爾失敗時見[修正說明](https://tools.arminkao.com/research/fix-edge-tts-403.html)。作者的其他工具與研究：[小高工具間](https://tools.arminkao.com/)（[llms.txt](https://tools.arminkao.com/llms.txt)）

---

## 先聽示範書，再決定要翻到什麼程度

`跟讀器/` 裡放了一本示範書：《沉思錄》卷一（Standard Ebooks 公版，CC0），同一份內容做了三種深度——

| 入口 | 深度 | 體驗重點 |
|----|------|---------|
| 卷一・速讀版 | 原文濃縮成四五成 | 一本書 2–4 小時聽完；點每段下方「原文」可展開完整譯文對照 |
| 卷一・精讀版 | 全文中譯，不刪減 | 一字不漏，時間約速讀的兩倍 |
| 對照（頂列分頁） | 英中逐句對照＋生字講解（節選 30 句） | 英文軌／英中交替軌／學習軌三種聽法，43 張生字卡（字義、例句、為什麼原文用這個字），生字可點發音（正常＋慢速） |

本機試聽：

```bash
cd 跟讀器 && python3 -m http.server 8000
# 瀏覽器開 http://localhost:8000
```

聽完你就知道自己要哪種。**agent 開工前該問使用者的第一個問題就是：「你要速讀還是精讀？」**

四種模式的完整成本（每千英文字）：

| 模式 | 內容 | 聽時 | 檔案 |
|------|------|------|------|
| 1 速讀 | 濃縮中文 | 3.5 分 | 1.28 MB |
| 2 精讀 | 全文中譯 | 7.4 分 | 2.7 MB |
| 3 進階精讀 | 英中逐句對照 | 11.2 分 | 4.3 MB |
| 4 學習 | 逐句解析＋生字卡 | 27.8 分 | 14.6 MB |

模式 3／4 請按章做，不要整本做（整本動輒 20–40 小時語音、1 GB 起跳）。示範書目前提供模式 1、2；3、4 的示範章之後補。

---

## 需求

| 需要 | 說明 |
|------|------|
| Python 3.9+ | 標準庫為主；PDF 另需 PyMuPDF |
| AI agent | 翻譯與濃縮用。`唸書機.py` 內建走 claude CLI；用別家就把翻譯步驟交給你的 agent 照下面的稿件規則做 |
| edge-tts | `pip install edge-tts`，免費、無金鑰、回逐字時間戳 |
| ffmpeg | mp3 → m4a 轉檔 |

---

## 公式（agent 照這個順序跑）

```bash
# 1. 抽文字（吃 .epub / .pdf / .txt；DRM 書抽不出來）
python3 唸書機.py extract 某本書.epub
#    文集可加 --only "篇名,篇名" 挑篇、--skip-intro 丟編者導讀

# 2. 估算四種模式的成本，拿去問使用者要哪種
python3 估算.py 某本書

# 3. 全文翻譯（產出 輸出/某本書/譯文/，中斷可續跑、術語表自動累積）
python3 唸書機.py translate 某本書 --jobs 3

# 4.（要做速讀才有這步）把 譯文/*.txt 濃縮成 速讀/*.txt
#    這步直接由 agent 自己做，契約見下面「濃縮契約」

# 5. 合成語音＋打包成跟讀器網頁（一章一個 json + m4a）
python3 跟讀器建置.py 某本書 --slug mybook --author 作者名
#    精讀版就把 速讀/ 指到 譯文/ 同內容（或建資料夾軟連結）

# 5b.（要做英中對照／生字講解才有這步）agent 先照 對照稿-示範.json 的格式
#     寫「對照稿」——逐句 en/zh ＋ 生字卡（字義/KK/例句/為什麼原文用這個字）＋摘要，
#     一次做一章的節選（30 句上下）就好，然後：
python3 對照建置.py 對照稿.json --out 跟讀器/mybook/bilingual
#     會產英文軌/英中交替軌/學習軌＋生字三速音檔＋頁面，
#     再到書頁頂列的 seg 加一顆 <a href="bilingual/">對照</a>

# 6. 上書架：跟讀器/index.html 是寫死的 HTML，要手動加兩處——
#    <a class="bk"> 卡片一塊 ＋ paintShelf() 裡的書目陣列一筆；
#    再把 sw.js 的 FILES 補上新書路徑、SHELL 版號 +1
```

### 聲音可以自己選

語音全走 edge-tts，聲音在兩個地方設定，換人聲不用改程式：

- **中文（跟讀器全站）**：`跟讀器建置.py --voice`。台灣腔有三個：`zh-TW-HsiaoChenNeural`（曉臻，示範書用的）、`zh-TW-HsiaoYuNeural`（曉雨）、`zh-TW-YunJheNeural`（雲哲，男聲）
- **英文（對照版）**：對照稿 json 的 `enVoice`。示範用 `en-GB-RyanNeural`（英國男聲，配《沉思錄》的第一人稱皇帝視角選的）；女聲可換 `en-GB-SoniaNeural`、`en-US-AriaNeural` 等
- 完整清單：`edge-tts --list-voices`。挑之前先合成一句試聽，語氣差很多

### 稿件規則（翻譯與濃縮都適用：給耳朵，不是給眼睛）

- 長句拆短；括號改插入句；阿拉伯數字寫成讀法（1990 → 一九九零年）
- **稿裡零英數字元**——括號英文術語進 TTS 就是災難，人名用中譯
- 濃縮契約：保留論證骨架與例子的關鍵細節，砍修辭與重複，壓到原文四五成；一段濃縮稿對應原文一到數段，順序不能跳動

### 品質細節（都是踩過的坑）

- 「原文」對照的段落對應（`src`）由 `跟讀器建置.py` 的 `align_src` 真對齊自動算，別用等比估算——濃縮不均勻就整章飄
- 舊書要重算對應就跑 `python3 原文對齊.py`（同時改 json 和頁面內嵌 DATA，不動音檔）
- 註腳、版權頁在 extract 已自動跳過；PDF 連字（ﬁ ﬂ）已自動修

---

## 部署

任何靜態主機都行（GitHub Pages、Cloudflare Pages、Firebase Hosting…）。附的 `firebase.json` 可直接 `firebase deploy --only hosting`。
注意流量：一章 m4a 約 3–10 MB，Firebase 免費層流量 360 MB／天；Cloudflare Pages 單檔上限 25 MB，模式 4 的章會超。

Safari 開啟後「加入主畫面」即成獨立 App：鎖定畫面可控制、可逐章下載離線、長按句子畫重點做筆記。

### 跨裝置同步（選配）

筆記與進度可跨裝置同步，設計是免登入：文件位置＝密語的 SHA-256，密語只留在裝置。要啟用：

1. 開一個自己的 Firebase 專案，啟用 Firestore
2. 部署附的 `firestore.rules`（只開 get/write 給 64 位十六進位 id，沒有 list）
3. 把 `跟讀器/` 裡所有 `YOUR-FIREBASE-PROJECT` 全域替換成你的專案 id

不設定也完全能用，只是進度留在單機。**密語別用太簡單的詞**——規則是任何人可寫任意文件，弱密語可被離線猜出位置。

---

## 版權界線（重要）

- 只處理**你有權處理**的書：公版（Project Gutenberg、Standard Ebooks）、無 DRM 技術書（O'Reilly／Manning／No Starch／Leanpub）、arXiv、自己的文件
- 有版權書的機器翻譯是**衍生著作**：自用可以，**不要公開散布**你的部署站或成品檔
- 示範書《沉思錄》用 Standard Ebooks 版，CC0，怎麼用都行
