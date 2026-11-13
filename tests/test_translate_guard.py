"""術語表、輸出檢查、重試：都不碰網路，用假的 _ask 模擬模型回答。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import translate
from translate import (Translator, build_glossary, glossary_hints, glossary_hit,
                       looks_untranslated, singular, to_traditional)

CFG = {
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "test",
    "target_lang": "繁體中文",
    "glossary": {"instance": "實例", "Runtime": "執行環境"},
}


def make(**over):
    return Translator({**CFG, **over})


class GlossaryTests(unittest.TestCase):
    def test_keys_are_case_insensitive(self):
        table = build_glossary({"Runtime": "執行環境"})
        self.assertEqual(glossary_hit(table, " runtime "), "執行環境")

    def test_plural_falls_back_to_singular(self):
        table = build_glossary({"instance": "實例", "library": "程式庫"})
        self.assertEqual(glossary_hit(table, "Instances"), "實例")
        self.assertEqual(glossary_hit(table, "Libraries"), "程式庫")

    def test_singular_keeps_words_that_only_look_plural(self):
        for word in ("status", "this", "class", "is"):
            self.assertEqual(singular(word), word)

    def test_hints_pick_out_terms_inside_a_block(self):
        table = build_glossary({"instance": "實例"})
        self.assertEqual(glossary_hints(table, "Loaded Instances (3)"),
                         {"Instances": "實例"})

    def test_word_lookup_never_asks_the_model(self):
        tr = make()
        with patch.object(Translator, "_ask") as ask:
            self.assertEqual(tr.translate_word("Instances", "Loaded Instances"), "實例")
        ask.assert_not_called()

    def test_block_that_is_exactly_one_term_skips_the_model(self):
        tr = make()
        with patch.object(Translator, "_ask") as ask:
            self.assertEqual(tr._one("Runtime"), "執行環境")
        ask.assert_not_called()

    def test_empty_glossary_is_fine(self):
        self.assertEqual(glossary_hit(build_glossary(None), "instance"), "")


class SkipTests(unittest.TestCase):
    def test_text_already_in_the_target_language_is_skipped(self):
        tr = make()
        with patch.object(Translator, "_ask") as ask:
            self.assertEqual(tr.translate_word("關閉視窗", "關閉視窗"), "")
        ask.assert_not_called()

    def test_numbers_and_single_letters_are_skipped(self):
        tr = make()
        with patch.object(Translator, "_ask") as ask:
            for junk in ("12:34", "80%", "D"):
                self.assertEqual(tr.translate_word(junk), "")
        ask.assert_not_called()

    def test_never_translate_is_honoured(self):
        tr = make(never_translate=["Finder"])
        with patch.object(Translator, "_ask") as ask:
            self.assertEqual(tr.translate_word("Finder", "Open Finder"), "")
        ask.assert_not_called()


class UntranslatedTests(unittest.TestCase):
    def test_english_answer_is_rejected(self):
        # 這就是 Instances 的原案：只去掉一個 s，躲過「一字不差」的比對
        self.assertTrue(looks_untranslated("Instances", "Instance", "繁體中文"))

    def test_chinese_answer_passes(self):
        self.assertFalse(looks_untranslated("Instances", "實例", "繁體中文"))

    def test_unchanged_output_is_allowed(self):
        # 「已經是目標語言就原樣輸出」是 prompt 的規則，config.json 這種識別碼要保留
        self.assertFalse(looks_untranslated("config.json", "config.json", "繁體中文"))

    def test_other_target_languages_are_not_checked(self):
        self.assertFalse(looks_untranslated("設定", "Settings", "English"))


class RetryTests(unittest.TestCase):
    def test_word_retries_once_then_gives_up(self):
        tr = make(glossary={})
        with patch.object(Translator, "_ask", side_effect=["Instance", "Instances"]) as ask:
            self.assertEqual(tr.translate_word("Instances", "Loaded Instances"), "")
        self.assertEqual(ask.call_count, 2)

    def test_word_takes_the_retry_when_it_is_chinese(self):
        tr = make(glossary={})
        with patch.object(Translator, "_ask", side_effect=["Instance", "實例"]):
            self.assertEqual(tr.translate_word("Instances", ""), "實例")

    def test_block_keeps_the_original_after_two_failures(self):
        tr = make(glossary={})
        with patch.object(Translator, "_ask", side_effect=["Instance", "Instance"]):
            self.assertEqual(tr._one("Instances"), "Instances")

    def test_block_does_not_retry_when_output_is_unchanged(self):
        tr = make(glossary={})
        with patch.object(Translator, "_ask", side_effect=["config.json"]) as ask:
            self.assertEqual(tr._one("config.json"), "config.json")
        self.assertEqual(ask.call_count, 1)

    def test_retry_prompt_says_what_went_wrong(self):
        prompt = translate._system_prompt("繁體中文", {"Instances": "實例"}, retry=True)
        self.assertIn("Instances = 實例", prompt)
        self.assertIn("previous answer was in the source language", prompt)


class TaiwanWordingTests(unittest.TestCase):
    def test_mainland_wording_is_replaced(self):
        self.assertEqual(translate.enforce_tw("默認值"), "預設值")
        self.assertEqual(translate.enforce_tw("打開軟件設置"), "打開軟體設定")

    def test_ambiguous_words_are_left_alone(self):
        # document 的正解就是「文件」，不能一律換成「檔案」
        for word in ("文件", "程序", "數據", "支持"):
            self.assertEqual(translate.enforce_tw(word), word)

    def test_applied_to_model_output(self):
        tr = make(glossary={})
        with patch("translate.requests.post") as post:
            post.return_value.json.return_value = {"message": {"content": "默認"}}
            self.assertEqual(tr.translate_word("Defaults", "Local Model Defaults"), "預設")


class TraditionalTests(unittest.TestCase):
    def test_simplified_output_is_converted(self):
        if translate.to_traditional("实") == "实":
            self.skipTest("這台機器沒有 macOS 的 ICU 轉換")
        self.assertEqual(to_traditional("实例"), "實例")

    def test_latin_text_is_untouched(self):
        self.assertEqual(to_traditional("Instances"), "Instances")

    def test_only_applied_to_traditional_targets(self):
        tr = make(target_lang="简体中文", glossary={})
        with patch("translate.to_traditional") as conv, \
                patch("translate.requests.post") as post:
            post.return_value.json.return_value = {"message": {"content": "实例"}}
            self.assertEqual(tr._one("Instances"), "实例")
        conv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
