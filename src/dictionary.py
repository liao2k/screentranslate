"""系統辭典：把一個字「所有可能的意思」列出來，游標取詞用。

模型只適合回答「這個字在這句話裡是什麼意思」；要它把全部義項列出來，
1.5B 會亂編（實測 windows 答出「滑鼠」、spring 答出「發明」）。
所以完整的義項改查 macOS 內建的《譯典通英漢雙向字典》——辭典 App 用的同一份資料，
離線、一次查詢 2ms 左右，而且 Instances、ran 這種變化形會自動還原成原形。

DictionaryServices 公開的 API 只有 DCSCopyTextDefinition，一次只給第一個詞條的純文字
（close 只會拿到「關閉」那一條，拿不到「近的」那一條），所以這裡用的是沒有公開文件的
DCSCopyRecordsForSearchString。任何一步失敗都回空清單，氣泡就退回只顯示模型的答案。
"""
from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from translate import is_traditional

FRAMEWORK = ("/System/Library/Frameworks/CoreServices.framework"
             "/Frameworks/DictionaryServices.framework")

# 目標語言 → 辭典。解析是照這一本的 XHTML 結構寫的，其他辭典要各自對過才能加。
DICTIONARIES = [
    (is_traditional, "com.apple.dictionary.zh_TW-en.DrEye"),    # 譯典通英漢雙向字典
]

LOOKUP_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
SPACES = re.compile(r"\s+")
# 義項裡不要的部分：例句、文法註記（［可數名詞…］）、「→ run」這種參照
SKIP = frozenset({"eg", "gg", "xrg"})
# 詞條裡不是「這個字的意思」的區塊：片語與衍生字（close down、deprecatingly）、同義字
ASIDE = frozenset({"subEntryBlock", "thesBlock"})


@dataclass(frozen=True)
class Sense:
    """一個詞條裡、同一個詞性底下的所有義項。"""
    headword: str
    pos: str                     # n.、vt.、a.……；辭典沒標就是空字串
    meanings: tuple[str, ...]


@dataclass(frozen=True)
class Entry:
    headword: str
    senses: tuple[Sense, ...]
    refs: frozenset[str]         # 「→ see1」指過去的原形，小寫
    homograph: str = ""          # 同形異義字的編號：lie1 躺、lie2 說謊

    @property
    def names(self) -> set[str]:
        name = self.headword.lower()
        return {name, name + self.homograph}


# ---------- 解析詞條 ----------

def _classes(el) -> set[str]:
    return set((el.get("class") or "").split())


def _walk(el, wanted: str):
    """深度優先找出 class 含 wanted 的元素，找到就不再往它裡面找。"""
    for child in el:
        classes = _classes(child)
        if wanted in classes:
            yield child
        elif not classes & ASIDE:
            yield from _walk(child, wanted)


def _text(el) -> str:
    if _classes(el) & SKIP:
        return ""
    parts = [el.text or ""]
    for child in el:
        parts.append(_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def parse_entry(xml: str) -> Entry | None:
    """一個詞條的 XHTML → 每個詞性一組義項。看不懂的結構回 None。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    hw = next(_walk(root, "hw"), None)
    # 同形異義字的編號（close1、close2）包在 hw 裡面的子元素，只取前面的文字
    headword = (hw.text or "").strip() if hw is not None else ""
    if not headword:
        return None

    senses: list[Sense] = []
    refs: set[str] = set()
    for group in _walk(root, "se1"):
        pos = next(_walk(group, "pos"), None)
        meanings: list[str] = []
        for item in _walk(group, "msDict"):
            targets = ["".join(x.itertext()).strip().lower() for x in _walk(item, "xr")]
            if targets:
                refs.update(targets)      # 「run的動詞過去式 (→ run)」：原形那一條會另外查到
                continue
            text = SPACES.sub(" ", _text(item)).replace("】 ", "】").strip(" ；;，,")
            if text and text not in meanings:
                meanings.append(text)
        if meanings:
            senses.append(Sense(headword,
                                SPACES.sub(" ", _text(pos)).strip() if pos is not None else "",
                                tuple(meanings)))
    return Entry(headword, tuple(senses), frozenset(refs), hw.get("homograph") or "")


def pick(entries: list[Entry], word: str) -> list[Sense]:
    """從查回來的詞條裡挑出要顯示的，游標停的那個字自己的詞條排前面。

    辭典的索引很寬：查 set 會連 sett 一起回來，查 I 會回碘（iodine 的符號），
    而且同一本還收了漢英詞條，查 ran 會帶出「然」「燃」。所以有同名詞條時，
    只留同名的，加上它明講「→ see1」指過去的原形（saw 先列鋸子，再列 see 看見）；
    沒有同名詞條才代表這是變化形（Instances → instance），那就全收。
    """
    entries = [e for e in entries if LOOKUP_WORD.match(e.headword)]
    own = [e for e in entries if e.headword.lower() == word.lower()]
    if own:
        refs = frozenset().union(*(e.refs for e in own))
        entries = own + [e for e in entries if e not in own and e.names & refs]
    return [s for e in entries for s in e.senses]


# ---------- 系統辭典 ----------

_lock = threading.Lock()
_api: dict = {}                  # 只載入一次；載不起來就留一個 {"failed": True}
_refs: dict[str, object] = {}    # 辭典識別碼 → DCSDictionaryRef（找不到存 None）
_cache: dict[tuple[str, str], list[Sense]] = {}


def _load() -> dict:
    if not _api:
        try:
            import objc
            from Foundation import NSBundle

            bundle = NSBundle.bundleWithPath_(FRAMEWORK)
            objc.loadBundleFunctions(bundle, _api, [
                ("DCSCopyAvailableDictionaries", b"@"),
                ("DCSDictionaryGetIdentifier", b"@@"),
                ("DCSCopyRecordsForSearchString", b"@@@QQ"),
                ("DCSRecordCopyData", b"@@q"),
            ])
        except Exception:                 # noqa: BLE001  非 macOS、沒裝 pyobjc、API 被拿掉
            _api.clear()
            _api["failed"] = True
    return _api


def _dictionary(identifier: str):
    api = _load()
    if "failed" in api:
        return None
    if identifier not in _refs:
        found = None
        try:
            for ref in api["DCSCopyAvailableDictionaries"]() or []:
                if str(api["DCSDictionaryGetIdentifier"](ref)) == identifier:
                    found = ref
                    break
        except Exception:                 # noqa: BLE001
            found = None
        _refs[identifier] = found
    return _refs[identifier]


def _identifier(lang: str) -> str:
    return next((ident for match, ident in DICTIONARIES if match(lang)), "")


def _records(identifier: str, word: str) -> list[str]:
    """查出這個字的所有詞條（XHTML）。變化形會連原形那一條一起回來。"""
    ref = _dictionary(identifier)
    if ref is None:
        return []
    api = _load()
    records = api["DCSCopyRecordsForSearchString"](ref, word, 0, 0) or []
    return [str(api["DCSRecordCopyData"](r, 0) or "") for r in records]


def available(lang: str) -> bool:
    """這個目標語言有沒有辭典可查（--check 用）。沒下載的辭典查什麼都是空的。"""
    identifier = _identifier(lang)
    if not identifier:
        return False
    with _lock:
        try:
            return bool(_records(identifier, "book"))
        except Exception:                 # noqa: BLE001
            return False


def lookup(word: str, lang: str) -> list[Sense]:
    """一個字的所有義項，照辭典的順序。查不到、不支援的語言都回空清單。"""
    word = word.strip().replace("\u2019", "'").strip("'-")
    if word.lower().endswith("'s"):
        word = word[:-2]                  # user's → user；所有格本身不是詞條
    identifier = _identifier(lang)
    if not identifier or not LOOKUP_WORD.fullmatch(word):
        return []                         # 這本只收英文詞條
    key = (word.lower(), identifier)
    with _lock:                           # 查詢本身只要幾毫秒，整段鎖住就好
        if key in _cache:
            return _cache[key]
        try:
            pages = _records(identifier, word)
        except Exception:                 # noqa: BLE001
            pages = []
        entries = [e for e in map(parse_entry, pages) if e is not None]
        senses = _cache[key] = pick(entries, word)
        return senses
