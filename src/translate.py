"""翻譯：走本地 Ollama 的 /api/chat，模型小、可離線、速度快。"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

THINK = re.compile(r"<think>.*?</think>", re.S)

# 有沒有「字」——數字、標點、符號都不算
LETTER = re.compile(r"[A-Za-zÀ-ɏͰ-ӿ֐-ۿ"
                    r"぀-ヿ㐀-鿿가-힯]")
HAN = re.compile(r"[㐀-鿿豈-﫿]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
TRIM = " \t\n\r.,;:!?()[]{}\"'`"


def needs_translation(text: str, lang: str,
                      keep_as_is: frozenset[str] = frozenset()) -> bool:
    """哪些字串根本不該送進模型。

    螢幕上有一大堆數字、百分比、時間、單一字母的圖示標籤，
    小模型看到 80% 會翻成「八成」、看到 D 會亂猜一個詞，
    而且每一個都要花一次推論。直接原樣保留比較好也比較快。
    """
    t = text.strip()
    if not t:
        return False
    if t in keep_as_is:
        return False                      # config.json 的 never_translate
    if not LETTER.search(t):
        return False                      # 80%、8、?、12:34
    if len(t) == 1:
        return False                      # 單一字母的圖示標籤
    if is_chinese(lang):
        letters = LETTER.findall(t)
        han = len(HAN.findall(t))
        # 分母只算「字」：9月20日週日上午8:55 裡的數字和冒號不能稀釋掉中文比例，
        # 否則會被當成外文送去翻，然後被模型改成「週一」。
        if letters and han / len(letters) >= 0.8 and not LATIN_WORD.search(t):
            return False                  # 本來就是中文了
    return True


# ---------- 語言判斷 ----------

def is_chinese(lang: str) -> bool:
    return "中文" in lang or "zh" in lang.lower()


def is_traditional(lang: str) -> bool:
    return any(k in lang for k in ("繁體", "繁体", "zh-TW", "zh-HK", "臺灣", "台灣", "香港"))


# ---------- 術語表 ----------

def _norm(word: str) -> str:
    return word.strip(TRIM).lower()


def singular(word: str) -> str:
    """複數還原成單數，術語表只要寫單數就夠了。"""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def build_glossary(raw: dict | None) -> dict[str, str]:
    """config.json 的 glossary 正規化：鍵一律小寫，順便補上單數形。"""
    table: dict[str, str] = {}
    for key, value in (raw or {}).items():
        k, v = _norm(str(key)), str(value).strip()
        if k and v:
            table[k] = v
            table.setdefault(singular(k), v)
    return table


def glossary_hit(table: dict[str, str], text: str) -> str:
    """整串剛好是一個術語就直接給答案，不必問模型。"""
    if not table:
        return ""
    k = _norm(text)
    return table.get(k) or table.get(singular(k), "")


def glossary_hints(table: dict[str, str], text: str, limit: int = 8) -> dict[str, str]:
    """挑出這段文字裡出現過的術語，當作提示塞進 prompt。"""
    hits: dict[str, str] = {}
    if not table:
        return hits
    for m in WORD.finditer(text):
        fixed = glossary_hit(table, m.group())
        if fixed:
            hits.setdefault(m.group(), fixed)
            if len(hits) >= limit:
                break
    return hits


# ---------- 簡體轉繁體 ----------

_TRAD: list = []            # 只探一次：macOS 內建的 ICU 轉換，或 None


def to_traditional(text: str) -> str:
    """把簡體字形轉成繁體。

    小模型（尤其 3B）偶爾會吐「实例」這種簡體。macOS 的 Foundation 內建
    ICU 的 Hans-Hant 轉換，不必多裝 OpenCC。只管字形，用詞（網絡／網路）
    還是靠 prompt 裡的對照表壓。
    """
    if not _TRAD:
        try:
            from Foundation import NSString
            _TRAD.append(lambda s: NSString.stringWithString_(s)
                         .stringByApplyingTransform_reverse_("Hans-Hant", False))
        except Exception:                 # noqa: BLE001  非 macOS 或沒裝 pyobjc
            _TRAD.append(None)
    fn = _TRAD[0]
    if fn is None or not HAN.search(text):
        return text
    try:
        return str(fn(text)) or text
    except Exception:                     # noqa: BLE001
        return text


# 小模型常常無視 prompt 裡的用詞對照（實測 Defaults 還是答「默認」），
# 輸出端再硬換一次。只收兩邊意思不會撞在一起的詞：
# 「文件」（document／file 都用得到）、「程序」「數據」「支持」在台灣也有正當用法，
# 一律換掉反而會錯，那幾個就只能靠 prompt。
TW_FIX = {
    "默認": "預設", "軟件": "軟體", "網絡": "網路", "內存": "記憶體",
    "屏幕": "螢幕", "鼠標": "滑鼠", "視頻": "影片", "硬件": "硬體",
    "回收站": "垃圾桶", "首選項": "偏好設定", "窗口": "視窗",
    "驅動器": "磁碟機", "導出": "匯出", "導入": "匯入", "設置": "設定",
}


def enforce_tw(text: str) -> str:
    """把漏網的大陸用詞換成台灣用詞。"""
    for bad, good in TW_FIX.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


# ---------- 輸出檢查 ----------

def looks_untranslated(src: str, out: str, lang: str) -> bool:
    """模型根本沒翻成目標語言的那些情況。

    1.5B 碰到 Instances 這種複數科技名詞會直接回英文原形（Instance），
    只差一個字母，躲得過「一字不差」的比對，然後一個假譯文就貼到畫面上。
    """
    if not out:
        return True
    if out.strip().lower() == src.strip().lower():
        return False        # 「已經是目標語言就原樣輸出」是規則之一，不算失敗
    return is_chinese(lang) and not HAN.search(out)


SYSTEM = (
    "You are a translation engine embedded in a screen translator. "
    "Translate the user's text into {lang}. "
    "The text comes from a graphical user interface — menu items, buttons, labels, "
    "dialogs — so use the conventional localized UI wording, not a literal gloss. "
    "It may start with one or two stray characters that the text recognizer picked up "
    "from an adjacent icon (for example 'q) Voice', 'E Skills', 'MA Connected Apps'); "
    "ignore those and translate only the real label. "
    "Rules: output ONLY the translation; no explanations, no quotes, no romanization, "
    "no added punctuation; always answer in {lang} and never in the source language; "
    "if the input is a short label of one to three words, answer with the short {lang} "
    "label a menu would use, never a sentence and never a synonym of the original; "
    "keep numbers, URLs, file paths, code identifiers and "
    "product names unchanged; keep it about the same length as the original; "
    "if the text is already in {lang}, repeat it unchanged."
)

# 小模型翻繁中時會混進大陸慣用詞（保存/文件/軟件/默認…），補一份對照壓回台灣用語。
ZH_TW_HINT = (
    " Use Taiwan (zh-TW) software terminology, never mainland wording: "
    "儲存 not 保存, 檔案 not 文件, 軟體 not 軟件, 網路 not 網絡, 預設 not 默認, "
    "設定 not 設置, 程式 not 程序, 資料 not 數據, 登入 not 登錄, 視窗 not 窗口, "
    "滑鼠 not 鼠標, 記憶體 not 內存, 螢幕 not 屏幕, 支援 not 支持, 影片 not 視頻, 變更 not 更改, 匯出 not 導出, 匯入 not 導入, 磁碟機 not 驅動器, 硬體 not 硬件, 垃圾桶 not 回收站, 說明 not 幫助, 偏好設定 not 首選項."
)

# 重試用的加強語氣：第一次吐回原文時才附上。
RETRY_HINT = (" Your previous answer was in the source language, which is wrong. "
              "Answer again using only {lang}; do not output a single Latin word "
              "unless it is a product name, a URL or a code identifier.")


def _system_prompt(lang: str, hints: dict[str, str] | None = None,
                   retry: bool = False) -> str:
    prompt = SYSTEM.format(lang=lang)
    if is_traditional(lang):
        prompt += ZH_TW_HINT
    if hints:
        pairs = "; ".join(f"{k} = {v}" for k, v in hints.items())
        prompt += f" Always translate these terms exactly like this: {pairs}."
    if retry:
        prompt += RETRY_HINT.format(lang=lang)
    return prompt


class Translator:
    def __init__(self, cfg):
        self.url = cfg["ollama_url"].rstrip("/")
        self.model = cfg["ollama_model"]
        self.lang = cfg["target_lang"]
        self.timeout = cfg.get("ollama_timeout", 60)
        self.keep_alive = cfg.get("keep_alive", "10m")
        self.workers = int(cfg.get("max_workers", 4))
        self.keep_as_is = frozenset(cfg.get("never_translate") or [])
        self.glossary = build_glossary(cfg.get("glossary"))
        self._cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # ---------- 伺服器 / 模型 ----------

    def health(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as exc:
            return False, f"連不上 Ollama（{self.url}）：{exc}\n請先執行 `ollama serve`"
        names = [m["name"] for m in r.json().get("models", [])]
        if self.model not in names and f"{self.model}:latest" not in names:
            return False, (f"找不到模型 {self.model}，請先執行：\n"
                           f"  ollama pull {self.model}\n目前有：{', '.join(names) or '（無）'}")
        return True, "ok"

    def warmup(self) -> None:
        """先把模型載進記憶體，第一次翻譯才不會卡住。"""
        try:
            requests.post(f"{self.url}/api/chat", timeout=self.timeout, json={
                "model": self.model, "messages": [], "keep_alive": self.keep_alive})
        except Exception:
            pass

    # ---------- 翻譯 ----------

    def _ask(self, messages: list[dict], num_predict: int) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "top_p": 0.9, "num_predict": num_predict},
            "messages": messages,
        }
        r = requests.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
        r.raise_for_status()
        out = r.json()["message"]["content"]
        out = THINK.sub("", out).strip().strip('"「」').strip()
        if is_traditional(self.lang):
            out = enforce_tw(to_traditional(out))
        return out

    def _one(self, text: str) -> str:
        text = text.strip()
        if not needs_translation(text, self.lang, self.keep_as_is):
            return text
        fixed = glossary_hit(self.glossary, text)
        if fixed:
            return fixed                  # 整段就是一個術語，直接給答案
        key = (text, self.lang)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        hints = glossary_hints(self.glossary, text)
        budget = max(64, len(text) * 3)
        user = [{"role": "user", "content": text}]
        out = self._ask([{"role": "system",
                          "content": _system_prompt(self.lang, hints)}] + user, budget)
        if looks_untranslated(text, out, self.lang):
            # 只在「有動過但沒翻成中文」時重試；原樣吐回代表它照規則保留，不用再問
            out = self._ask([{"role": "system",
                              "content": _system_prompt(self.lang, hints, retry=True)}]
                            + user, budget)
            if looks_untranslated(text, out, self.lang):
                out = text                # 兩次都失敗：寧可留原文，也不要貼假譯文

        with self._lock:
            self._cache[key] = out
        return out

    def wants_word(self, word: str) -> bool:
        """這個字值不值得查：數字、單一字母、本來就是目標語言的、never_translate 的都不必。"""
        word = word.strip(TRIM)
        return bool(word) and needs_translation(word, self.lang, self.keep_as_is)

    def translate_word(self, word: str, context: str = "") -> str:
        """查一個單字。帶著它所在的句子，多義字才不會翻錯。

        翻不出來就回空字串，讓呼叫端當成沒查到——氣泡不出現，
        好過浮出一顆寫著英文原形的假譯文。
        """
        word = word.strip(TRIM)
        if not self.wants_word(word):
            return ""
        fixed = glossary_hit(self.glossary, word)
        if fixed:
            return fixed                  # 術語表命中：零延遲、不會錯
        key = (f"\x00word\x00{word}", context or self.lang)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        # 小模型對「只輸出單字的意思」這種指令幾乎不遵守，會把整句翻掉，
        # 甚至把指令內容本身吐出來。改用範例教它，遵從度高很多。
        system = (f"你是一本字典。使用者給你一個詞和它出現的句子，"
                  f"你只回答那個詞的{self.lang}意思，越短越好。"
                  f"不要翻譯整句，不要解釋，不要加標點。")
        if is_traditional(self.lang):
            system += ZH_TW_HINT          # 實測 12 題從 9 對變 10 對（記憶體、未儲存）
        # 三個範例就好。試過再加一組同字不同義的 right（權利／右邊）示範多義，
        # 結果反而讓 1.5B 混淆，兩種情境都答「正確」，分數從 8 掉到 7。
        shots = [
            ("Word: close\nSentence: Click here to close the window.", "關閉"),
            ("Word: right\nSentence: You have the right to remain silent.", "權利"),
            ("Word: document\nSentence: Your document has unsaved edits.", "文件"),
        ]
        user = f"Word: {word}"
        if context and context.strip() != word:
            user += f"\nSentence: {context.strip()}"

        def messages(system_prompt: str) -> list[dict]:
            msgs = [{"role": "system", "content": system_prompt}]
            for q, a in shots:
                msgs.append({"role": "user", "content": q})
                msgs.append({"role": "assistant", "content": a})
            msgs.append({"role": "user", "content": user})
            return msgs

        out = self._ask(messages(system), 24)
        if not out or out.strip().lower() == word.lower() \
                or looks_untranslated(word, out, self.lang):
            # 再問一次，明講上次答錯在哪；還是不行就當作沒查到
            retry_system = system + (f"　上一次你用原文回答，那是錯的，"
                                     f"這次一定要用{self.lang}回答。")
            out = self._ask(messages(retry_system), 24)
            if not out or out.strip().lower() == word.lower() \
                    or looks_untranslated(word, out, self.lang):
                out = ""

        with self._lock:
            self._cache[key] = out
        return out

    def translate_many(self, texts: list[str], on_done=None) -> list[str]:
        """多段平行翻譯，回傳順序與輸入相同。失敗的那段回傳原文。"""
        results: list[str] = [""] * len(texts)

        def job(i_text):
            i, t = i_text
            try:
                return i, self._one(t)
            except Exception as exc:
                return i, f"[翻譯失敗] {exc.__class__.__name__}"

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for n, (i, out) in enumerate(pool.map(job, enumerate(texts)), 1):
                results[i] = out
                if on_done:
                    on_done(n, len(texts))
        return results
