"""游標取詞的切詞：中英混排的一行不能整行當成一個字。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocr import WORD_TOKEN, utf16_range


def tokens(line: str) -> list[str]:
    return [m.group() for m in WORD_TOKEN.finditer(line)]


class TokenTests(unittest.TestCase):
    def test_latin_word_inside_a_chinese_line_stands_alone(self):
        # 原案：游標停在 prompt 上，查到的卻是一整行
        self.assertEqual(tokens("區塊翻譯時同一份表會當成提示塞進 prompt，所以"),
                         ["區塊翻譯時同一份表會當成提示塞進", "prompt", "所以"])

    def test_chinese_runs_split_on_punctuation(self):
        self.assertEqual(tokens("關閉視窗、儲存檔案"), ["關閉視窗", "儲存檔案"])

    def test_english_line_splits_per_word(self):
        self.assertEqual(tokens("Local Model Defaults"), ["Local", "Model", "Defaults"])

    def test_apostrophes_and_hyphens_stay_in_the_word(self):
        self.assertEqual(tokens("don't re-run"), ["don't", "re-run"])

    def test_numbers_alone_are_not_words(self):
        self.assertEqual(tokens("3 instances (2 GB)"), ["instances", "GB"])

    def test_japanese_and_korean_are_one_run(self):
        self.assertEqual(tokens("ファイルを保存"), ["ファイルを保存"])
        self.assertEqual(tokens("파일 저장"), ["파일", "저장"])


class Utf16RangeTests(unittest.TestCase):
    def test_plain_text_matches_python_indexes(self):
        self.assertEqual(utf16_range("Loaded Instances", 7, 16), (7, 9))

    def test_emoji_shifts_the_range(self):
        # emoji 在 UTF-16 佔兩個單位，不換算的話框會取到隔壁的字
        line = "🔒 Locked"
        start = line.index("Locked")
        self.assertEqual(utf16_range(line, start, start + 6), (3, 6))


if __name__ == "__main__":
    unittest.main()
