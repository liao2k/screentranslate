"""游標取詞：滑鼠停在一個字上不動，就把那個字的意思顯示出來。

氣泡裡有兩種答案：模型看著整句話回答的「這裡是什麼意思」，
加上系統辭典列出的「這個字所有可能的意思」（dictionary.py）。

做法是「停留偵測 + 局部截圖」：每 120ms 看一次游標位置，
停住超過設定的時間才動作，只截游標周圍一小塊（預設 420×90 點），
所以不會一直吃 CPU，也不會整個螢幕跑 OCR。
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QPoint, QRect, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication

import capture
import dictionary
import ocr as ocr_mod


class _Lookup(QThread):
    """背景查一個字：截圖 → OCR 逐字座標 → 命中游標的那個字 → 查辭典＋翻譯。"""

    # 原文, 這句話裡的意思, 原文在螢幕上的位置, 辭典的所有義項（list[dictionary.Sense]）
    found = pyqtSignal(str, str, QRect, object)
    missed = pyqtSignal()
    stalled = pyqtSignal(str)                # OCR 卡住了，要讓使用者知道

    def __init__(self, cfg, engine, translator, pos: QPoint, box: tuple[int, int]):
        super().__init__()
        self.cfg, self.engine, self.translator = cfg, engine, translator
        self.pos, self.box = pos, box

    def run(self):
        try:
            bw, bh = self.box
            screen = QGuiApplication.screenAt(self.pos) or QGuiApplication.primaryScreen()
            area = screen.geometry()
            x = max(area.left(), min(self.pos.x() - bw // 2, area.right() - bw))
            y = max(area.top(), min(self.pos.y() - bh // 2, area.bottom() - bh))

            shot = capture.grab(x, y, bw, bh)
            if not hasattr(self.engine, "run_words"):
                self.missed.emit()           # RapidOCR 沒有逐字座標
                return

            words = self.engine.run_words(shot.image)
            # 游標位置換算成這張圖的像素座標
            cx = (self.pos.x() - shot.x) * shot.scale
            cy = (self.pos.y() - shot.y) * shot.scale
            hit = next((w for w in words if w.contains(cx, cy, pad=3)), None)
            if hit is None:
                self.missed.emit()
                return

            if not self.translator.wants_word(hit.text):
                self.missed.emit()           # 數字、本來就是目標語言的字
                return
            senses = []
            if self.cfg.get("hover_all_senses", True):
                senses = dictionary.lookup(hit.text, self.cfg["target_lang"])
            try:
                meaning = self.translator.translate_word(hit.text, hit.line)
            except Exception:                # noqa: BLE001  模型掛了，辭典查到的照樣能看
                meaning = ""
            if meaning == hit.text:
                meaning = ""
            if not meaning and not senses:
                self.missed.emit()
                return

            rect = QRect(int(shot.x + hit.x0 / shot.scale),
                         int(shot.y + hit.y0 / shot.scale),
                         max(1, int((hit.x1 - hit.x0) / shot.scale)),
                         max(1, int((hit.y1 - hit.y0) / shot.scale)))
            self.found.emit(hit.text, meaning, rect, senses)
        except ocr_mod.OCRTimeout:
            self.stalled.emit(ocr_mod.STALL_HINT)
        except Exception:                    # noqa: BLE001  取詞失敗就安靜跳過
            self.missed.emit()


class HoverTranslator(QObject):
    def __init__(self, cfg, tip, ensure_engine, translator, notify=None):
        super().__init__()
        self.cfg = cfg
        self.tip = tip
        self.ensure_engine = ensure_engine    # 延後載入 OCR，開啟時才付這個成本
        self.translator = translator
        self.notify = notify or (lambda _msg: None)

        self.dwell = float(cfg.get("hover_dwell_ms", 600)) / 1000
        self.box = tuple(cfg.get("hover_capture", [420, 90]))
        self.enabled = False

        self._timer = QTimer()
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._tick)

        self._last_pos = QPoint(-1, -1)
        self._still_since = 0.0
        self._fired_at: QPoint | None = None
        self._anchor: QRect | None = None
        self._worker: _Lookup | None = None
        self._generation = 0

    # ---------- 開關 ----------

    def set_enabled(self, on: bool) -> None:
        self._generation += 1
        self.enabled = on
        self.tip.hide()
        self._fired_at = self._anchor = None
        self._last_pos = QPoint(-1, -1)
        self._still_since = time.monotonic()
        if on:
            self._timer.start()
        else:
            self._timer.stop()

    # ---------- 主迴圈 ----------

    def _tick(self):
        if not self.enabled:
            return
        pos = QCursor.pos()
        now = time.monotonic()

        if (pos - self._last_pos).manhattanLength() > 3:
            self._last_pos = pos
            self._still_since = now
            # 游標離開原本那個字就收起氣泡
            if self._anchor and not self._anchor.adjusted(-12, -12, 12, 12).contains(pos):
                self.tip.hide()
                self._anchor = None
            return

        if now - self._still_since < self.dwell:
            return
        if self._worker and self._worker.isRunning():
            return
        if self._fired_at is not None and (pos - self._fired_at).manhattanLength() <= 3:
            return                            # 同一個點不重複查

        self._fired_at = pos
        try:
            engine = self.ensure_engine()
        except Exception:                     # noqa: BLE001
            return
        self._worker = _Lookup(self.cfg, engine, self.translator, pos, self.box)
        generation = self._generation
        self._worker.found.connect(
            lambda *args: self._on_found(generation, *args))
        self._worker.missed.connect(lambda: self._on_missed(generation))
        self._worker.stalled.connect(lambda m: self._on_stalled(generation, m))
        self._worker.start()

    def _on_missed(self, generation: int):
        if self.enabled and generation == self._generation:
            self.tip.hide()

    def _on_stalled(self, generation: int, msg: str):
        if self.enabled and generation == self._generation:
            self.tip.hide()
            self.notify(msg)

    def _on_found(self, generation: int, word: str, meaning: str, rect: QRect,
                  senses=()):
        if not self.enabled or generation != self._generation:
            return
        self._anchor = rect
        self.tip.show_word(word, meaning, rect, senses)
