"""AI 校稿／修飾：輸出清理、繁體把關、模型選擇。不碰網路也不碰剪貼簿。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rewrite
from rewrite import Rewriter, clean_output, keep_outer_whitespace

CFG = {"ollama_url": "http://127.0.0.1:11434/", "ollama_model": "small"}


class CleanOutputTests(unittest.TestCase):
    def test_strips_prefix_and_code_fence(self):
        self.assertEqual(clean_output("修改後：我已經寫好了。"), "我已經寫好了。")
        self.assertEqual(clean_output("```\n我已經寫好了。\n```"), "我已經寫好了。")

    def test_strips_think_block(self):
        self.assertEqual(clean_output("<think>想一下</think>\n應該沒問題。"), "應該沒問題。")

    def test_keeps_inner_line_breaks(self):
        self.assertEqual(clean_output("第一段\n\n第二段\n"), "第一段\n\n第二段")


class WhitespaceTests(unittest.TestCase):
    def test_restores_surrounding_whitespace(self):
        self.assertEqual(keep_outer_whitespace("  以經好了\n\n", "已經好了。"), "  已經好了。\n\n")


class RewriterTests(unittest.TestCase):
    def test_rewrite_model_overrides_translation_model(self):
        self.assertEqual(Rewriter(CFG).model, "small")
        self.assertEqual(Rewriter({**CFG, "rewrite_model": "big"}).model, "big")

    def test_output_is_forced_to_taiwan_traditional(self):
        r = Rewriter(CFG)
        with patch.object(r, "_ask", return_value="修改後：请检查网络设置"):
            self.assertEqual(r.rewrite("請檢查網路設定", "proofread"), "請檢查網路設定")

    def test_each_mode_sends_its_own_prompt(self):
        r = Rewriter(CFG)
        with patch.object(r, "_ask", return_value="ok") as ask:
            r.rewrite("  文字  ", "polish")
        system, user = ask.call_args.args[0]
        self.assertEqual(system["content"], rewrite.MODES["polish"]["system"])
        self.assertTrue(user["content"].endswith("\n\n文字"))

    def test_health_checks_the_rewrite_model(self):
        r = Rewriter({**CFG, "rewrite_model": "big", "target_lang": "繁體中文"})
        with patch("translate.Translator.health", autospec=True,
                   side_effect=lambda tr: (tr.model == "big", "")) as health:
            self.assertTrue(r.health()[0])
        health.assert_called_once()


if __name__ == "__main__":
    unittest.main()
