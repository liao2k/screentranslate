"""系統辭典的解析與挑選：用手寫的詞條 XHTML，不碰真正的辭典。"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtWidgets import QApplication

import dictionary
import hover
from dictionary import Entry, Sense, parse_entry, pick
from ocr import Word
from overlay import GLUE, WordTip
from overlay import sense_rows as glued_rows

LANG = "繁體中文"


def sense_rows(*args, **kwargs):
    """編號後面那個看不見的防折行字元拿掉，斷言才好讀。"""
    return [(kind, label, text.replace(GLUE, "")) for kind, label, text in glued_rows(*args, **kwargs)]


def entry(headword, body, homograph=""):
    hom = f' homograph="{homograph}"' if homograph else ""
    num = f'<span class="gp ty_hom tg_hw">{homograph} </span>' if homograph else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns:d="http://www.apple.com/DTDs/DictionaryService-1.0.rng"><body>'
            f'<d:entry class="entry"><span class="hg"><span{hom} class="hw">{headword}{num}'
            f'</span></span><span class="sg">{body}</span></d:entry></body></html>')


def group(pos, *senses):
    head = f'<span class="posg"><span class="pos">{pos} </span></span>' if pos else ""
    return f'<span class="se1"><span class="x_xdh">{head}</span>{"".join(senses)}</span>'


def sense(*words, extra=""):
    trans = "".join(f'<span class="trg"><span class="trans">{w}</span></span>' for w in words)
    return f'<span class="se2"><span class="msDict"><span class="x_xdh">{trans}</span>{extra}</span></span>'


EXAMPLE = ('<span class="eg"><span class="ex">Close the door.</span>'
           '<span class="trg"><span class="trans">把門關上。</span></span></span>')
GRAMMAR = '<span class="gg">［可數名詞］</span>'
PHRASES = ('<span class="subEntryBlock"><span class="subEntry"><span class="l">close down </span>'
           + group("", sense("停業")) + '</span></span>')


def xref(note, target):
    return ('<span class="se2"><span class="msDict"><span class="x_xdh">'
            f'<span class="trg"><span class="trans">{note}</span></span>'
            f'<span class="xrg">(→ <span class="xr"><a href="x">{target}</a></span>)</span>'
            '</span></span></span>')


class ParseTests(unittest.TestCase):
    def test_every_part_of_speech_keeps_its_own_senses(self):
        got = parse_entry(entry("close", group("vt.", sense("關閉；", "蓋上"), sense("結束"))
                                + group("n.", sense("末尾"))))
        self.assertEqual(got.senses, (Sense("close", "vt.", ("關閉；蓋上", "結束")),
                                      Sense("close", "n.", ("末尾",))))

    def test_examples_and_grammar_notes_are_left_out(self):
        got = parse_entry(entry("close", group("vt.", sense("關閉", extra=EXAMPLE + GRAMMAR))))
        self.assertEqual(got.senses[0].meanings, ("關閉",))

    def test_phrases_are_not_senses_of_the_word(self):
        got = parse_entry(entry("close", group("vt.", sense("關閉"))).replace(
            "</d:entry>", PHRASES + "</d:entry>"))
        self.assertEqual([s.meanings for s in got.senses], [("關閉",)])

    def test_homograph_number_is_not_part_of_the_headword(self):
        got = parse_entry(entry("close", group("a.", sense("近的")), homograph="2"))
        self.assertEqual((got.headword, got.homograph), ("close", "2"))

    def test_cross_reference_becomes_a_ref_not_a_sense(self):
        got = parse_entry(entry("saw", group("", xref("see的動詞過去式", "see1"))))
        self.assertEqual((got.senses, got.refs), ((), frozenset({"see1"})))

    def test_garbage_is_ignored(self):
        self.assertIsNone(parse_entry("<html><body>"))
        self.assertIsNone(parse_entry("<html><body></body></html>"))


def make(headword, meaning, refs=(), homograph=""):
    return Entry(headword, (Sense(headword, "n.", (meaning,)),) if meaning else (),
                 frozenset(refs), homograph)


class PickTests(unittest.TestCase):
    def test_inflected_form_takes_the_lemma(self):
        self.assertEqual(pick([make("instance", "實例")], "Instances")[0].headword, "instance")

    def test_same_name_entries_win_over_index_neighbours(self):
        got = pick([make("set", "一套"), make("sett", "鋪路石")], "set")
        self.assertEqual([s.headword for s in got], ["set"])

    def test_own_entry_first_then_the_lemma_it_points_to(self):
        got = pick([make("see", "看見", homograph="1"), make("see", "主教轄區", homograph="2"),
                    make("saw", "", refs={"see1"}), make("saw", "鋸子")], "saw")
        self.assertEqual([s.meanings[0] for s in got], ["鋸子", "看見"])

    def test_chinese_headwords_from_the_reverse_half_are_dropped(self):
        got = pick([make("ran", "", refs={"run"}), make("run", "跑"), make("然", "but")], "ran")
        self.assertEqual([s.headword for s in got], ["run"])


class LookupTests(unittest.TestCase):
    def setUp(self):
        dictionary._cache.clear()

    def test_only_latin_words_in_supported_languages_reach_the_dictionary(self):
        with patch("dictionary._records") as records:
            self.assertEqual(dictionary.lookup("ファイル", LANG), [])
            self.assertEqual(dictionary.lookup("12:34", LANG), [])
            self.assertEqual(dictionary.lookup("close", "English"), [])
        records.assert_not_called()

    def test_possessive_is_looked_up_without_the_suffix_and_cached(self):
        page = entry("user", group("n.", sense("使用者")))
        with patch("dictionary._records", return_value=[page]) as records:
            first = dictionary.lookup("user’s", LANG)
            second = dictionary.lookup("User's", LANG)
        self.assertEqual(first, [Sense("user", "n.", ("使用者",))])
        self.assertIs(first, second)
        records.assert_called_once()
        self.assertEqual(records.call_args.args[1], "user")

    def test_broken_system_api_means_no_senses(self):
        with patch("dictionary._records", side_effect=RuntimeError("no framework")):
            self.assertEqual(dictionary.lookup("close", LANG), [])


class RowTests(unittest.TestCase):
    SENSES = [Sense("running", "a.", ("奔跑的", "流動的", "連續的")),
              Sense("run", "vi.", ("跑",))]

    def test_lemma_gets_a_heading_and_senses_are_numbered(self):
        self.assertEqual(sense_rows("Running", self.SENSES), [
            ("sense", "a.", "①奔跑的 ②流動的 ③連續的"),
            ("head", "", "run"),
            ("sense", "vi.", "跑")])

    def test_limit_cuts_each_part_of_speech(self):
        rows = sense_rows("running", self.SENSES, limit=2)
        self.assertEqual(rows[0], ("sense", "a.", "①奔跑的 ②流動的 …"))
        self.assertEqual(rows[2], ("sense", "vi.", "跑"))

    def test_number_is_glued_to_its_sense(self):
        self.assertEqual(glued_rows("running", self.SENSES)[0][2].split(" ")[1],
                         "②" + GLUE + "流動的")


class TipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.join = patch("overlay.join_all_spaces")
        self.join.start()
        self.tip = WordTip({"font_family": "PingFang TC"})
        self.anchor = QRect(40, 40, 60, 18)

    def tearDown(self):
        self.tip.close()
        self.join.stop()

    def texts(self):
        return [op[2].replace(GLUE, "") for op in self.tip._ops]

    def test_senses_are_listed_under_the_contextual_meaning(self):
        self.tip.show_word("close", "關閉", self.anchor,
                           [Sense("close", "vt.", ("關閉", "結束")), Sense("close", "a.", ("近的",))])
        self.assertEqual(self.texts(), ["close", "關閉", "vt.", "①關閉 ②結束", "a.", "近的"])
        self.assertIsNotNone(self.tip._rule)

    def test_without_senses_it_is_the_old_two_line_bubble(self):
        self.tip.show_word("email", "電子郵件", self.anchor)
        self.assertEqual(self.texts(), ["email", "電子郵件"])
        self.assertIsNone(self.tip._rule)

    def test_dictionary_alone_is_enough(self):
        self.tip.show_word("Instances", "", self.anchor, [Sense("instance", "n.", ("實例",))])
        self.assertEqual(self.texts(), ["Instances", "instance", "n.", "實例"])

    def test_long_entries_wrap_and_never_cover_the_word(self):
        many = [Sense("set", "vt.", tuple(f"第{i}個很長很長很長的意思" for i in range(20)))
                for _ in range(12)]
        self.tip.show_word("set", "設定", self.anchor, many)
        area = self.qt.primaryScreen().availableGeometry()
        box = self.tip.geometry()
        self.assertLessEqual(box.width(), WordTip.MAX_TEXT + 80)
        self.assertGreaterEqual(box.top(), self.anchor.bottom())
        self.assertLessEqual(box.bottom(), area.bottom())
        self.assertEqual(self.texts()[-1], "…")


class LookupThreadTests(unittest.TestCase):
    """_Lookup.run() 直接在這條執行緒上跑，截圖和 OCR 用假的。"""

    def run_lookup(self, translator, cfg=None, senses=None):
        shot = SimpleNamespace(image=None, x=0, y=0, scale=1)
        engine = Mock(run_words=Mock(return_value=[
            Word("Instances", 90, 40, 150, 58, line="Loaded Instances")]))
        cfg = {"target_lang": LANG, **(cfg or {})}
        job = hover._Lookup(cfg, engine, translator, QPoint(100, 50), (420, 90))
        found, missed = [], []
        job.found.connect(lambda *args: found.append(args))
        job.missed.connect(lambda: missed.append(True))
        with patch("hover.capture.grab", return_value=shot), \
                patch("hover.QGuiApplication") as gui, \
                patch("hover.dictionary.lookup", return_value=senses or []) as lookup:
            gui.screenAt.return_value.geometry.return_value = QRect(0, 0, 1000, 800)
            job.run()
        return found, missed, lookup

    SENSES = [Sense("instance", "n.", ("例子，實例",))]

    def test_found_carries_both_the_meaning_and_the_senses(self):
        tr = Mock(wants_word=Mock(return_value=True), translate_word=Mock(return_value="實例"))
        found, missed, lookup = self.run_lookup(tr, senses=self.SENSES)
        self.assertEqual(missed, [])
        word, meaning, rect, senses = found[0]
        self.assertEqual((word, meaning, senses), ("Instances", "實例", self.SENSES))
        self.assertEqual(rect, QRect(90, 40, 60, 18))
        lookup.assert_called_once_with("Instances", LANG)

    def test_dictionary_rescues_a_word_the_model_cannot_answer(self):
        for translate in (Mock(return_value=""), Mock(side_effect=RuntimeError("ollama down"))):
            tr = Mock(wants_word=Mock(return_value=True), translate_word=translate)
            found, missed, _ = self.run_lookup(tr, senses=self.SENSES)
            self.assertEqual(missed, [])
            self.assertEqual((found[0][1], found[0][3]), ("", self.SENSES))

    def test_nothing_from_either_source_is_a_miss(self):
        tr = Mock(wants_word=Mock(return_value=True), translate_word=Mock(return_value=""))
        found, missed, _ = self.run_lookup(tr)
        self.assertEqual((found, missed), ([], [True]))

    def test_words_not_worth_looking_up_skip_the_dictionary_too(self):
        tr = Mock(wants_word=Mock(return_value=False))
        found, missed, lookup = self.run_lookup(tr, senses=self.SENSES)
        self.assertEqual((found, missed), ([], [True]))
        lookup.assert_not_called()
        tr.translate_word.assert_not_called()

    def test_all_senses_can_be_switched_off(self):
        tr = Mock(wants_word=Mock(return_value=True), translate_word=Mock(return_value="實例"))
        found, _, lookup = self.run_lookup(tr, cfg={"hover_all_senses": False},
                                           senses=self.SENSES)
        lookup.assert_not_called()
        self.assertEqual(found[0][3], [])


if __name__ == "__main__":
    unittest.main()
