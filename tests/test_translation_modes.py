"""模式切換回歸測試：使用真正的 Qt 視窗、可控制完成時機的背景工作。"""
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image
from PyQt6.QtCore import QObject, QPoint, QRect, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import config
import hover
import main
from overlay import Item


class ControlledPipeline(QObject):
    progress = pyqtSignal(str)
    failed = pyqtSignal(str)
    done = pyqtSignal(object, object, object)

    def __init__(self, *args):
        super().__init__()
        self.running = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running


class ControlledLookup(ControlledPipeline):
    found = pyqtSignal(str, str, QRect)
    missed = pyqtSignal()
    stalled = pyqtSignal(str)


class TranslationModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.patches = ExitStack()
        for name in ("join_all_spaces", "activate_app", "force_key_window"):
            self.patches.enter_context(patch(f"overlay.{name}"))
        self.patches.enter_context(patch("main.Pipeline", ControlledPipeline))
        self.patches.enter_context(patch("hover._Lookup", ControlledLookup))
        self.app = main.App(config.Config(config.DEFAULTS.copy()))
        self.app.engine = Mock(run_words=Mock())
        self.app.translator.health = Mock(return_value=(True, ""))
        self.app._act_hover = QAction("游標取詞翻譯")
        self.app._act_hover.setCheckable(True)
        self.app._act_hover.toggled.connect(self.app.toggle_hover)
        self.rect = QRect(10, 10, 160, 80)

    def tearDown(self):
        self.app.hover.set_enabled(False)
        self.app.selector.cancel_selection()
        for widget in (self.app.selector, self.app.result, self.app.toast, self.app.tip):
            widget.close()
        self.patches.close()

    def show_region(self):
        self.app.result.show_items(self.rect, [Item(
            QRect(0, 0, 80, 20), "Settings", "設定", (30, 30, 30),
            (255, 255, 255), 16)])
        self.assertTrue(self.app.result.isVisible())

    def finish_region(self, worker):
        worker.running = False
        worker.done.emit(SimpleNamespace(
            image=Image.new("RGB", (160, 80), "white"),
            x=10, y=10, w=160, h=80, scale=1), [SimpleNamespace(
                x0=0, y0=0, x1=80, y1=20, text="Settings", line_height=16)], ["設定"])

    def test_enabling_hover_clears_previous_region(self):
        self.show_region()
        self.app.result._show_source = True
        self.app.toggle_hover(True)
        self.assertTrue(self.app.hover.enabled)
        self.assertTrue(self.app._act_hover.isChecked())
        self.assertFalse(self.app.result.isVisible())
        self.assertEqual(self.app.result.items, [])
        self.assertFalse(self.app.result._show_source)
        self.app.tip.show_word("Settings", "設定", self.rect)
        self.qt.processEvents()
        self.assertFalse(self.app.result.isVisible())

    def test_late_region_signals_cannot_restore_overlay_after_switch(self):
        self.app.on_region(self.rect)
        old_worker = self.app.worker
        self.app.toggle_hover(True)
        hover_message = self.app.toast._text
        old_worker.progress.emit("舊進度")
        old_worker.failed.emit("舊錯誤")
        self.assertEqual(self.app.toast._text, hover_message)
        # 即使又關閉取詞，已作廢的區域工作也不能重新顯示。
        self.app.toggle_hover(False)
        self.finish_region(old_worker)
        self.assertFalse(self.app.result.isVisible())
        self.assertEqual(self.app.result.items, [])
        self.app.on_region(self.rect)
        self.finish_region(self.app.worker)
        self.assertTrue(self.app.result.isVisible())
        self.assertEqual(self.app.result.items[0].text, "設定")

    def test_hover_cancels_delayed_selection_window(self):
        self.app.start_selection()
        self.app.toggle_hover(True)
        QTest.qWait(100)
        self.assertFalse(self.app.selector.isVisible())
        self.assertFalse(self.app.selector._cursor_pushed)

    def test_selecting_region_disables_hover_and_updates_menu(self):
        self.app.toggle_hover(True)
        self.app.tip.show_word("Settings", "設定", self.rect)
        self.app.start_selection()
        QTest.qWait(100)
        self.assertTrue(self.app.selector.isVisible())
        self.assertFalse(self.app.hover.enabled)
        self.assertFalse(self.app.hover._timer.isActive())
        self.assertFalse(self.app._act_hover.isChecked())
        self.assertFalse(self.app.tip.isVisible())
        self.app.selector.cancel_selection()

    def test_retranslate_clears_overlay_before_capture_and_disables_hover(self):
        self.app.last_rect = self.rect
        self.app.toggle_hover(True)
        self.show_region()
        self.app.retranslate_last()
        self.assertFalse(self.app.hover.enabled)
        self.assertFalse(self.app.result.isVisible())
        self.assertEqual(self.app.result.items, [])
        self.assertTrue(self.app.worker.isRunning())

    def test_lookup_from_previous_mode_cannot_show_or_hide_new_tip(self):
        self.app.toggle_hover(True)
        controller = self.app.hover
        controller._last_pos = QPoint(20, 20)
        controller._still_since = 0
        with patch.object(hover, "QCursor") as cursor:
            cursor.pos.return_value = QPoint(20, 20)
            controller._tick()
        old_lookup = controller._worker
        self.app.start_selection()
        old_lookup.found.emit("Settings", "設定", self.rect)
        self.assertFalse(self.app.tip.isVisible())
        self.app.toggle_hover(True)
        old_lookup.found.emit("Settings", "設定", self.rect)
        self.assertFalse(self.app.tip.isVisible())
        self.app.tip.show_word("Update", "更新", self.rect)
        old_lookup.missed.emit()
        self.assertTrue(self.app.tip.isVisible())

    def test_ocr_stall_during_hover_tells_user(self):
        self.app.toggle_hover(True)
        controller = self.app.hover
        controller._last_pos = QPoint(20, 20)
        controller._still_since = 0
        with patch.object(hover, "QCursor") as cursor:
            cursor.pos.return_value = QPoint(20, 20)
            controller._tick()
        controller._worker.stalled.emit("文字辨識沒有回應")
        self.assertEqual(self.app.toast._text, "文字辨識沒有回應")

    def test_busy_region_worker_is_not_silent(self):
        self.app.on_region(self.rect)
        self.app.start_selection()
        self.assertIn("還在翻譯中", self.app.toast._text)


class VisionTimeoutTests(unittest.TestCase):
    def test_hung_vision_call_raises_instead_of_blocking_forever(self):
        import threading
        import ocr

        release = threading.Event()
        handler = Mock()
        handler.performRequests_error_.side_effect = lambda *_: (release.wait(5), None)
        request = Mock()
        engine = ocr.VisionEngine(timeout=0.2)
        with self.assertRaises(ocr.OCRTimeout):
            engine._perform(handler, request)
        request.cancel.assert_called_once()
        release.set()

    def test_normal_vision_call_passes_through(self):
        import ocr

        handler = Mock()
        handler.performRequests_error_.return_value = (True, None)
        engine = ocr.VisionEngine(timeout=1)
        self.assertEqual(engine._perform(handler, Mock()), (True, None))


if __name__ == "__main__":
    unittest.main()
