"""螢幕翻譯工具 —— 截圖 → 本地 OCR → 本地小模型翻譯 → 原位覆蓋。

用法：
    python src/main.py            # 常駐，用快捷鍵或選單列圖示觸發
    python src/main.py --once     # 只翻譯一次就結束
    python src/main.py --check    # 檢查環境（OCR 模型、Ollama、權限）

選單列「譯」裡另有「AI 校稿／修飾選取的文字」：抓別的 App 選取的文字送本地模型改寫。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image
from PyQt6.QtCore import QObject, QRect, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

import capture
import config
import dictionary
import group as grouping
import ocr as ocr_mod
import selection
from hover import HoverTranslator
from overlay import (Item, RegionSelector, ResultOverlay, RewriteDialog, Toast, WordTip,
                     sample_colors, use_menu_bar_app_mode)
from rewrite import MODES, Rewriter, keep_outer_whitespace
from statusbar import MenuBarIcon
from translate import Translator, is_traditional


# --------------------------------------------------------------------------

class Pipeline(QThread):
    """背景執行：截圖 → OCR → 分段 → 翻譯。"""

    progress = pyqtSignal(str)
    done = pyqtSignal(object, object, object)   # shot, blocks, translations
    failed = pyqtSignal(str)

    def __init__(self, cfg, rect: QRect, engine, translator: Translator):
        super().__init__()
        self.cfg, self.rect, self.engine, self.translator = cfg, rect, engine, translator

    def run(self):
        try:
            t0 = time.time()
            self.progress.emit("擷取畫面…")
            shot = capture.grab(self.rect.x(), self.rect.y(),
                                self.rect.width(), self.rect.height())

            dbg = self.cfg.get("debug_save_dir")
            if dbg:
                Path(dbg).mkdir(parents=True, exist_ok=True)
                shot.image.save(Path(dbg) / f"shot-{int(t0)}.png")

            self.progress.emit("辨識文字…")
            lines = ocr_mod.run_with_autoscale(self.engine, shot.image)
            blocks = grouping.group(lines,
                                    min_conf=float(self.cfg["min_confidence"]),
                                    tol=float(self.cfg["block_merge_gap"]))
            if not blocks:
                self.failed.emit("這個區域沒有辨識到文字")
                return

            texts = [b.text for b in blocks]
            self.progress.emit(f"翻譯中 0/{len(texts)}…")
            out = self.translator.translate_many(
                texts, on_done=lambda n, total: self.progress.emit(f"翻譯中 {n}/{total}…"))

            self.progress.emit(f"完成（{time.time() - t0:.1f}s）")
            self.done.emit(shot, blocks, out)
        except ocr_mod.OCRTimeout:
            self.failed.emit(ocr_mod.STALL_HINT)
        except Exception as exc:                      # noqa: BLE001
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")


class RewriteJob(QThread):
    """背景執行：⌘C 抓選取的文字 → 送模型改寫。"""

    done = pyqtSignal(str, str)                 # original, revised
    failed = pyqtSignal(str)

    def __init__(self, rewriter: Rewriter, mode: str):
        super().__init__()
        self.rewriter, self.mode = rewriter, mode

    def run(self):
        try:
            text = selection.copy_selection()
            if not text.strip():
                self.failed.emit("沒有抓到選取的文字\n請先選取文字，再從選單列執行")
                return
            self.done.emit(text, self.rewriter.rewrite(text, self.mode))
        except Exception as exc:                      # noqa: BLE001
            self.failed.emit(f"{MODES[self.mode]['title']}失敗：{exc.__class__.__name__}: {exc}")


class HotkeyBridge(QObject):
    """pynput 在自己的執行緒觸發，透過 signal 丟回 Qt 主執行緒。"""
    fired = pyqtSignal()


# --------------------------------------------------------------------------

class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = None
        self.translator = Translator(cfg)
        self.selector = RegionSelector()
        self.result = ResultOverlay(cfg)
        self.toast = Toast(cfg)
        self.tip = WordTip(cfg)
        self.hover = HoverTranslator(cfg, self.tip, self.ensure_engine, self.translator,
                                     notify=lambda m: self.toast.say(m, 8000))
        self.worker: Pipeline | None = None
        self.last_rect: QRect | None = None
        self.quit_after = False
        self._region_generation = 0
        self.rewriter = Rewriter(cfg)
        self.rewrite_dialog = RewriteDialog(cfg)
        self.rewrite_dialog.chosen.connect(self.on_rewrite_chosen)
        self.rewrite_job: RewriteJob | None = None
        self._rewrite_target = None               # (原本的 App, 原文)

        self.selector.selected.connect(self.on_region)
        self.selector.cancelled.connect(self.on_cancel)

    # ---------- 準備 ----------

    def ensure_engine(self):
        if self.engine is None:
            self.toast.say("載入 OCR 模型…")
            QApplication.processEvents()
            self.engine = ocr_mod.build(self.cfg)
        return self.engine

    # ---------- 流程 ----------

    def _set_hover_enabled(self, on: bool):
        self.hover.set_enabled(on)
        action = getattr(self, "_act_hover", None)
        if action is not None:
            blocked = action.blockSignals(True)
            action.setChecked(on)
            action.blockSignals(blocked)

    def _clear_region(self):
        # 已送出的背景翻譯可繼續完成，但不得重新顯示舊結果。
        self._region_generation += 1
        self.selector.cancel_selection()
        self.result.clear()
        self.toast.hide()

    def _region_callback(self, generation, callback, *args):
        if generation == self._region_generation:
            callback(*args)

    def start_selection(self):
        if self.worker and self.worker.isRunning():
            self.toast.say("上一個區域還在翻譯中，請稍候", 2500)
            return
        self._set_hover_enabled(False)
        self._clear_region()
        self.selector.start()

    def toggle_hover(self, on: bool):
        if on:
            ok, msg = self.translator.health()
            if not ok:
                self._set_hover_enabled(False)
                self.toast.say(msg, 8000)
                return
            try:
                self.ensure_engine()
            except Exception as exc:                  # noqa: BLE001
                self._set_hover_enabled(False)
                self.toast.say(f"OCR 初始化失敗：{exc}", 8000)
                return
            if not hasattr(self.engine, "run_words"):
                self._set_hover_enabled(False)
                self.toast.say("游標取詞需要 Vision OCR\n"
                               "請把 config.json 的 ocr_engine 改成 vision", 8000)
                return
            self._clear_region()
        self._set_hover_enabled(on)
        self.toast.say("游標取詞：開　停在單字上不動即可" if on else "游標取詞：關", 2500)

    def retranslate_last(self):
        if self.last_rect:
            self.on_region(self.last_rect)
        else:
            self.toast.say("還沒有翻譯過任何區域", 2000)

    def on_cancel(self):
        self.toast.hide()
        if self.quit_after:
            QApplication.quit()

    def on_region(self, rect: QRect):
        if self.worker and self.worker.isRunning():
            return
        self._set_hover_enabled(False)
        self._clear_region()
        self.last_rect = rect
        ok, msg = self.translator.health()
        if not ok:
            self.toast.say(msg, 8000)
            return
        try:
            engine = self.ensure_engine()
        except Exception as exc:                      # noqa: BLE001
            self.toast.say(f"OCR 初始化失敗：{exc}", 8000)
            return

        self.worker = Pipeline(self.cfg, rect, engine, self.translator)
        generation = self._region_generation
        self.worker.progress.connect(
            lambda m: self._region_callback(generation, self.toast.say, m))
        self.worker.failed.connect(
            lambda m: self._region_callback(generation, self.on_failed, m))
        self.worker.done.connect(
            lambda *args: self._region_callback(generation, self.on_done, *args))
        self.worker.start()

    # ---------- AI 校稿／修飾 ----------

    def rewrite_selection(self, mode: str):
        if (self.rewrite_job and self.rewrite_job.isRunning()) or self.rewrite_dialog.isVisible():
            return
        title = MODES[mode]["title"]
        # 選單列的選單打開時，前景仍是使用者原本在打字的那個 App
        target = selection.frontmost_app()
        if target is None or selection.is_self(target):
            self.toast.say("請先到要修改的 App 選取文字，再從選單列執行", 4000)
            return
        if not selection.can_post_keys(prompt=True):
            self.toast.say(f"{title}需要「輔助使用」權限才能複製選取的文字\n"
                           "系統設定 › 隱私權與安全性 › 輔助使用，打勾後把程式結束再重開", 8000)
            return
        ok, msg = self.rewriter.health()
        if not ok:
            self.toast.say(msg, 8000)
            return

        self.toast.say(f"{title}中…（{self.rewriter.model}）")
        # 選單列的選單收起後，原本 App 的視窗不一定拿回鍵盤焦點，⌘C 會落空；先把它叫回前景
        selection.activate(target)
        job = RewriteJob(self.rewriter, mode)
        job.done.connect(lambda original, revised: self.on_rewritten(mode, target, original, revised))
        job.failed.connect(lambda m: self.toast.say(m, 6000))
        self.rewrite_job = job
        job.start()

    def on_rewritten(self, mode: str, target, original: str, revised: str):
        self.toast.hide()
        self._rewrite_target = (target, original)
        self.rewrite_dialog.ask(MODES[mode]["title"], original, revised)

    def on_rewrite_chosen(self, action: str, text: str):
        target, original = self._rewrite_target or (None, "")
        self._rewrite_target = None
        if action == "copy":
            selection.set_text(text)
            self.toast.say("已複製到剪貼簿", 2000)
        selection.activate(target)
        if action == "replace" and target is not None:
            # 等原本的 App 真的回到前景、選取範圍恢復，再貼上
            QTimer.singleShot(300, lambda: self._paste_back(keep_outer_whitespace(original, text)))

    def _paste_back(self, text: str):
        saved = selection.paste_text(text)
        QTimer.singleShot(800, lambda: selection.restore(saved))   # 等對方讀完剪貼簿再還原

    def on_failed(self, msg: str):
        self.toast.say(msg, 6000)

    def on_done(self, shot, blocks, translations):
        items: list[Item] = []
        for block, text in zip(blocks, translations):
            bg, fg = sample_colors(shot.image, (block.x0, block.y0, block.x1, block.y1))
            x0, y0 = block.x0 / shot.scale, block.y0 / shot.scale      # 像素 → 點
            x1, y1 = block.x1 / shot.scale, block.y1 / shot.scale
            items.append(Item(
                rect=QRect(int(x0), int(y0), max(1, int(x1 - x0)), max(1, int(y1 - y0))),
                source=block.text, text=text, bg=bg, fg=fg,
                src_h=block.line_height / shot.scale))

        self.toast.hide()
        self.result.show_items(QRect(shot.x, shot.y, shot.w, shot.h), items)
        if self.quit_after:
            self.result.destroyed.connect(QApplication.quit)
            self.result.installEventFilter(QuitOnClose())


class QuitOnClose(QObject):
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.Close:
            QApplication.quit()
        return False


# --------------------------------------------------------------------------

def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(40, 110, 220))
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QColor(255, 255, 255))
    f = QFont("PingFang TC", 34)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), int(Qt.AlignmentFlag.AlignCenter), "譯")
    p.end()
    return QIcon(pm)


def install_hotkey(app_obj: App, bridge: HotkeyBridge, combo: str):
    try:
        from pynput import keyboard
    except Exception:
        return None
    try:
        listener = keyboard.GlobalHotKeys({combo: bridge.fired.emit})
        listener.daemon = True
        listener.start()
        return listener
    except Exception as exc:                          # noqa: BLE001
        print(f"⚠️  快捷鍵註冊失敗（需要「輔助使用」權限）：{exc}", file=sys.stderr)
        return None


def check_env(cfg) -> int:
    print(f"設定檔：{config.CONFIG_PATH}")
    print(f"目標語言：{cfg['target_lang']}　模型：{cfg['ollama_model']}　OCR：{cfg['ocr_engine']}")

    print("\n[1/3] 螢幕擷取…", end=" ")
    shot = None
    try:
        shot = capture.grab(0, 0, 200, 80)
        print(f"OK（{shot.image.width}×{shot.image.height}px, scale={shot.scale:g}）")
    except Exception as exc:                          # noqa: BLE001
        print(f"失敗：{exc}\n"
              "    → 系統設定 › 隱私權與安全性 › 螢幕錄製，把執行這支程式的\n"
              "      終端機／應用程式打勾，然後把它整個結束再重開。")

    print("[2/3] OCR 模型…", end=" ")
    try:
        engine = ocr_mod.build(cfg)
        sample = shot.image if shot else Image.new("RGB", (240, 80), "white")
        engine.run(sample)
        print(f"OK（{engine.name}）")
    except Exception as exc:                          # noqa: BLE001
        print(f"失敗：{exc}")

    # 不算在三關裡：沒有辭典一樣能用，只是游標取詞的氣泡少了「所有意思」那一段
    if dictionary.available(cfg["target_lang"]):
        print("[選用] 游標取詞辭典… OK（譯典通英漢雙向字典）")
    elif is_traditional(cfg["target_lang"]):
        print("[選用] 游標取詞辭典… 沒有，氣泡只會顯示模型的翻譯\n"
              "    → 打開「辭典」App › 設定，勾選「譯典通英漢雙向字典」讓它下載。")
    else:
        print("[選用] 游標取詞辭典… 目前只支援繁體中文，氣泡只會顯示模型的翻譯")

    print("[3/3] Ollama…", end=" ")
    tr = Translator(cfg)
    ok, msg = tr.health()
    if ok:
        t0 = time.time()
        out = tr.translate_many(["Save changes before closing?"])[0]
        print(f"OK（{time.time() - t0:.1f}s）→ {out}")
    else:
        print(f"失敗：{msg}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本地螢幕翻譯工具")
    ap.add_argument("--once", action="store_true", help="翻譯一次就結束")
    ap.add_argument("--check", action="store_true", help="檢查環境是否就緒")
    ap.add_argument("--lang", help="覆寫目標語言，例如 --lang English")
    ap.add_argument("--model", help="覆寫 Ollama 模型")
    args = ap.parse_args()

    cfg = config.load()
    if args.lang and args.lang != cfg["target_lang"]:
        cfg["glossary"] = {}          # 術語表是寫給原本那個目標語言的，換語言就不能用
        cfg["target_lang"] = args.lang
    if args.model:
        cfg["ollama_model"] = args.model

    if args.check:
        return check_env(cfg)

    qt = QApplication(sys.argv)
    qt.setQuitOnLastWindowClosed(False)
    if not args.once:
        use_menu_bar_app_mode()
    QGuiApplication.setApplicationName("螢幕翻譯")

    app = App(cfg)
    app.quit_after = args.once

    if not args.once:
        act_run = QAction(f"翻譯選取區域　({cfg['hotkey']})")
        act_run.triggered.connect(app.start_selection)
        act_again = QAction("重新翻譯上次區域")
        act_again.triggered.connect(app.retranslate_last)
        act_hover = QAction("游標取詞翻譯")
        act_hover.setCheckable(True)
        act_hover.setChecked(bool(cfg.get("hover_translate")))
        act_hover.toggled.connect(app.toggle_hover)
        act_proofread = QAction("AI 校稿選取的文字")
        act_proofread.triggered.connect(lambda: app.rewrite_selection("proofread"))
        act_polish = QAction("AI 修飾選取的文字")
        act_polish.triggered.connect(lambda: app.rewrite_selection("polish"))
        act_quit = QAction("離開")
        act_quit.triggered.connect(qt.quit)
        tray = MenuBarIcon(make_icon(), [act_run, act_again, act_hover, None,
                                         act_proofread, act_polish, None, act_quit],
                           tooltip="螢幕翻譯工具")

        bridge = HotkeyBridge()
        bridge.fired.connect(app.start_selection, Qt.ConnectionType.QueuedConnection)
        install_hotkey(app, bridge, cfg["hotkey"])
        if cfg.get("hover_translate"):
            app.toggle_hover(True)
        app.toast.say(f"螢幕翻譯已啟動　{cfg['hotkey']} 開始選取", 4000)
        app._act_hover = act_hover
        app._tray = tray      # 保留參考，避免被回收
    else:
        app.start_selection()

    return qt.exec()


if __name__ == "__main__":
    raise SystemExit(main())
