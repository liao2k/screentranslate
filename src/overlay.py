"""畫面元件：框選區域、翻譯結果覆蓋層、狀態提示、AI 校稿確認視窗。全部用 PyQt6。"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image
from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence,
                         QPainter, QPen, QShortcut)
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QVBoxLayout, QWidget)


# --------------------------------------------------------------------------
# macOS 多桌面 (Spaces)
# --------------------------------------------------------------------------

def join_all_spaces(widget: QWidget, level: int | None = None) -> None:
    """讓視窗出現在「目前這個」桌面，而不是把使用者拉回 app 原本的桌面。

    Qt 建出來的是一般 NSWindow，只屬於建立當下的那個 Space；
    在桌面 2 按快捷鍵時，macOS 會為了顯示它而整個切回桌面 1，
    截到的自然就是桌面 1 的畫面。canJoinAllSpaces 讓它跟著使用者跑，
    fullScreenAuxiliary 則讓它能蓋在全螢幕 app 上面。
    """
    try:
        import AppKit
        import objc

        view = objc.objc_object(c_void_p=int(widget.winId()))
        window = view.window()
        if window is None:
            return
        window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary)
        if level is not None:
            window.setLevel_(level)
    except Exception:                    # 不是 macOS 或 pyobjc 缺了就算了
        pass


def activate_app() -> None:
    """accessory 模式的 app 不會自動變成前景，鍵盤事件（Esc）就收不到。"""
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def force_key_window(widget: QWidget) -> None:
    """強制讓視窗變成 key window。

    Qt 的 Tool 視窗在 macOS 上是 NSPanel，預設「需要時才變成 key」，
    所以框選遮罩雖然蓋上去了，卻要先點一下才拿到焦點，
    游標也因此沒有馬上變成十字。
    """
    try:
        import AppKit
        import objc

        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        view = objc.objc_object(c_void_p=int(widget.winId()))
        window = view.window()
        if window is None:
            return
        if hasattr(window, "setBecomesKeyOnlyIfNeeded_"):
            window.setBecomesKeyOnlyIfNeeded_(False)
        window.makeKeyAndOrderFront_(None)
    except Exception:
        pass


def use_menu_bar_app_mode() -> None:
    """切成選單列工具（accessory）：沒有 Dock 圖示，啟用時也不會切換桌面。"""
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 取色：從原圖採樣底色與字色，讓覆蓋上去的文字看起來像原本就在那裡
# --------------------------------------------------------------------------

def _luma(c: tuple[int, int, int]) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def sample_colors(img: Image.Image, box: tuple[int, int, int, int]):
    """回傳 (底色, 字色)。

    底色取「出現最多次的實際顏色」——背景像素永遠佔多數，
    而且不量化才能拿到原本那個精準的值。
    字色取「離底色亮度最遠的那 12% 像素」的平均——
    文字邊緣有抗鋸齒的灰階，只看最多數或量化結果會抓成灰色，
    取極端那一撮才會是真正的字心顏色。
    """
    from collections import Counter

    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, max(x1, x0 + 1)), min(img.height, max(y1, y0 + 1))
    def sample(box, limit=96):
        a, b, c, e = box
        crop = img.crop((a, b, c, e)).convert("RGB")
        if crop.width > limit or crop.height > limit:   # NEAREST：不插值出原圖沒有的顏色
            crop = crop.resize((min(limit, crop.width), min(limit, crop.height)),
                               Image.Resampling.NEAREST)
        return list(crop.getdata())

    pixels = sample((x0, y0, x1, y1))
    if not pixels:
        return (255, 255, 255), (0, 0, 0)

    # 底色從「稍微放大」的框取樣：OCR 的框貼著字，粗體或大字時
    # 筆畫本身就佔掉大半像素，底色會被取成字的顏色。
    # 往外讓一點，真正的背景才會是多數。
    pad = max(2, round(0.4 * (y1 - y0)))
    around = sample((max(0, x0 - pad), max(0, y0 - pad),
                     min(img.width, x1 + pad), min(img.height, y1 + pad)))
    bg = Counter(around or pixels).most_common(1)[0][0]
    bg_l = _luma(bg)

    far = sorted(pixels, key=lambda c: -abs(_luma(c) - bg_l))
    top = far[: max(1, len(far) * 12 // 100)]
    if abs(_luma(top[-1]) - bg_l) < 25:       # 整塊幾乎同色，沒有文字可言
        return bg, ((0, 0, 0) if bg_l > 128 else (255, 255, 255))

    n = len(top)
    fg = tuple(sum(c[i] for c in top) // n for i in range(3))
    return bg, fg


# --------------------------------------------------------------------------
# 框選
# --------------------------------------------------------------------------

class RegionSelector(QWidget):
    """全螢幕半透明遮罩，拖曳選取要翻譯的範圍。"""

    selected = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)

        geo = QRect()
        for scr in QGuiApplication.screens():
            geo = geo.united(scr.geometry())
        self._origin_offset = geo.topLeft()
        self.setGeometry(geo)

        self._start: QPoint | None = None
        self._end: QPoint | None = None
        self._cursor_pushed = False
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._show_now)

    # --- 事件 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._start = e.position().toPoint()
            self._end = self._start
            self.update()

    def mouseMoveEvent(self, e):
        if self._start is not None:
            self._end = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._start is None:
            return
        rect = QRect(self._start, e.position().toPoint()).normalized()
        self.hide()
        self._release_cursor()
        if rect.width() < 8 or rect.height() < 8:
            self.cancelled.emit()
        else:
            rect.translate(self._origin_offset)     # 轉成螢幕全域座標
            self.selected.emit(rect)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
            self._release_cursor()
            self.cancelled.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._start and self._end:
            sel = QRect(self._start, self._end).normalized()
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(sel, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(90, 170, 255), 2))
            p.drawRect(sel)
            label = f"{sel.width()} × {sel.height()}"
            p.setFont(QFont("Menlo", 12))
            p.setPen(QColor(255, 255, 255))
            p.drawText(sel.adjusted(2, -20, 0, 0), Qt.AlignmentFlag.AlignLeft, label)

    def start(self):
        self._start = self._end = None
        # 從選單列叫出來時，NSMenu 還在收起來的動畫中，
        # 晚一拍再顯示，焦點才搶得到。
        self._show_timer.start(60)

    def cancel_selection(self):
        self._show_timer.stop()
        self.hide()
        self._release_cursor()
        self._start = self._end = None

    def _show_now(self):
        self.show()
        join_all_spaces(self, level=1000)      # NSScreenSaverWindowLevel
        force_key_window(self)
        self.raise_()
        self.activateWindow()
        self.setFocus()
        if not self._cursor_pushed:            # 保險：連視窗焦點都拿不到時仍有十字游標
            QApplication.setOverrideCursor(Qt.CursorShape.CrossCursor)
            self._cursor_pushed = True

    def _release_cursor(self):
        if self._cursor_pushed:
            QApplication.restoreOverrideCursor()
            self._cursor_pushed = False


# --------------------------------------------------------------------------
# 翻譯結果
# --------------------------------------------------------------------------

@dataclass
class Item:
    rect: QRect                     # 相對於覆蓋層的座標（點）
    source: str
    text: str
    bg: tuple[int, int, int]
    fg: tuple[int, int, int]
    src_h: float = 0.0              # 原文的行高（點），用來決定譯文字級


class ResultOverlay(QWidget):
    """蓋在原本文字上的翻譯層：Esc 關閉，按住滑鼠可暫時看原文。"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.items: list[Item] = []
        self._show_source = False
        self._hint_visible = True
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def clear(self):
        self.hide()
        self.items = []
        self._show_source = False
        self.update()

    def show_items(self, screen_rect: QRect, items: list[Item]):
        self.items = items
        self._show_source = False
        self.setGeometry(screen_rect)
        self.show()
        join_all_spaces(self, level=999)
        activate_app()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._hint_visible = True
        QTimer.singleShot(4000, self._hide_hint)
        self.update()

    def _hide_hint(self):
        self._hint_visible = False
        self.update()

    # --- 事件 ---
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.close()

    def mousePressEvent(self, _):
        self._show_source = True
        self.update()

    def mouseReleaseEvent(self, _):
        self._show_source = False
        self.update()

    def mouseDoubleClickEvent(self, _):
        self.close()

    # --- 繪製 ---
    def _fit_font(self, text: str, box: QRect, src_h: float = 0.0) -> tuple[QFont, QRect]:
        """挑一個塞得進方塊、又不會比原文大的字級。

        只看方塊大小的話，遇到又高又窄的方塊會把字撐到很誇張；
        螢幕翻譯要的是「看起來像原本就在那裡」，所以上限鎖在原文行高。
        """
        family = self.cfg["font_family"]
        lo, hi = int(self.cfg["min_font_size"]), int(self.cfg["max_font_size"])
        cap = round(src_h * 0.92) if src_h else int(box.height() * 1.2)
        hi = min(hi, max(lo, cap))
        # 方塊高度只框到墨跡，但字體的行高大約是字級的 1.3 倍，
        # 直接拿方塊高度當限制會逼出比原文小四成的字。留一點垂直餘裕，
        # 真的長高了再由下面的「往下長」邏輯補背景。
        avail_h = max(box.height(), round(src_h * 1.6)) if src_h else box.height()
        best = QFont(family, lo)
        best_rect = QRect(box)
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft)
        while lo <= hi:                                  # 二分搜尋最大可放進去的字級
            mid = (lo + hi) // 2
            font = QFont(family, mid)
            need = QFontMetrics(font).boundingRect(
                QRect(0, 0, box.width(), 10000), flags, text)
            if need.height() <= avail_h and need.width() <= box.width():
                best, best_rect, lo = font, need, mid + 1
            else:
                hi = mid - 1
        return best, best_rect

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        for item in self.items:
            text = item.source if self._show_source else item.text
            if not text:
                continue
            pad = 2
            box = item.rect.adjusted(-pad, -pad, pad, pad)
            font, need = self._fit_font(text, item.rect, item.src_h)

            # 字太多時允許往下長一點，但不超出覆蓋層
            if need.height() > item.rect.height():
                grow = min(need.height() - item.rect.height(),
                           max(0, self.height() - box.bottom() - 2))
                box.setHeight(box.height() + grow)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*item.bg))
            p.drawRoundedRect(box, 3, 3)

            p.setFont(font)
            p.setPen(QColor(*item.fg))
            p.drawText(box.adjusted(pad, pad, -pad, -pad),
                       int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
                       text)

        # 提示列放左下角、幾秒後自動消失：放左上會正好蓋住第一行譯文。
        if not self._hint_visible:
            return
        hint = "Esc 關閉　·　按住滑鼠看原文　·　雙擊關閉"
        p.setFont(QFont(self.cfg["font_family"], 11))
        fm = QFontMetrics(p.font())
        bar = QRect(0, 0, fm.horizontalAdvance(hint) + 16, fm.height() + 8)
        bar.moveBottomLeft(QPoint(6, self.height() - 6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 20, 20, 190))
        p.drawRoundedRect(bar, 5, 5)
        p.setPen(QColor(235, 235, 235))
        p.drawText(bar, int(Qt.AlignmentFlag.AlignCenter), hint)


# --------------------------------------------------------------------------
# 游標取詞的提示氣泡
# --------------------------------------------------------------------------

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
GLUE = "\u2060"          # 看不見的「不可在此折行」，編號才不會孤零零留在行尾


def sense_rows(word: str, senses, limit: int = 0) -> list[tuple[str, str, str]]:
    """辭典義項排成氣泡裡的一列一列：("sense", 詞性, 義項) 或 ("head", "", 原形)。

    查到的詞條和游標停的字不一樣時（Instances → instance、running 之後接 run）
    先插一列原形當小標題。limit 是每個詞性最多列幾個義項，0 代表全部。
    """
    rows: list[tuple[str, str, str]] = []
    current = word.strip().lower()
    for sense in senses:
        if sense.headword.lower() != current:
            current = sense.headword.lower()
            rows.append(("head", "", sense.headword))
        items = list(sense.meanings)
        cut = 0 < limit < len(items)
        if cut:
            items = items[:limit]
        if len(items) > 1 or cut:
            items = [(CIRCLED[i] if i < len(CIRCLED) else f"({i + 1})") + GLUE + m
                     for i, m in enumerate(items)]
        rows.append(("sense", sense.pos, " ".join(items) + (" …" if cut else "")))
    return rows


class WordTip(QWidget):
    """跟著游標的小氣泡：原文、這句話裡的意思，底下列出辭典裡所有的意思。不搶焦點。"""

    MAX_TEXT = 400          # 義項最寬到這裡就換行（點）
    LINE = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    WRAP = LINE | int(Qt.TextFlag.TextWordWrap)

    DIM = QColor(150, 150, 158)
    BRIGHT = QColor(245, 245, 248)
    BODY = QColor(222, 222, 228)
    POS = QColor(128, 176, 255)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._ops: list[tuple[QRect, int, str, QFont, QColor]] = []
        self._rule: int | None = None         # 分隔線的 y，沒有辭典義項時不畫
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def show_word(self, word: str, meaning: str, anchor: QRect, senses=()):
        """anchor 是原文那個字在螢幕上的位置（點），氣泡貼著它下面。

        meaning 是模型看上下文給的意思，senses 是辭典的所有義項（dictionary.Sense），
        兩個可以缺一個：辭典沒收的字只有 meaning，模型答不出來的只有 senses。
        """
        fam = self.cfg["font_family"]
        small, big, body = QFont(fam, 11), QFont(fam, 16), QFont(fam, 13)
        fm_small, fm_big, fm_body = (QFontMetrics(f) for f in (small, big, body))
        rows = sense_rows(word, senses, int(self.cfg.get("hover_max_senses") or 0))

        label_w = max((fm_small.horizontalAdvance(label)
                       for kind, label, _ in rows if kind == "sense"), default=0)
        label_w += 8 if label_w else 0
        rows_w = max((fm_small.horizontalAdvance(text) if kind == "head"
                      else label_w + fm_body.horizontalAdvance(text) + 2   # 留一點，免得剛好折行
                      for kind, _, text in rows), default=0)
        inner = max(fm_small.horizontalAdvance(word), fm_big.horizontalAdvance(meaning),
                    min(rows_w, label_w + self.MAX_TEXT))
        text_w = inner - label_w

        scr = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = scr.availableGeometry()
        below = area.bottom() - anchor.bottom() - 8
        above = anchor.top() - 8 - area.top()
        room = max(below, above)              # 氣泡不能蓋住原文，最高只能長到這裡

        ops = [(QRect(12, 8, inner, fm_small.height()), self.LINE, word, small, self.DIM)]
        y = 8 + fm_small.height()
        if meaning:
            ops.append((QRect(12, y + 2, inner, fm_big.height()), self.LINE,
                        meaning, big, self.BRIGHT))
            y += 2 + fm_big.height()
        self._rule = None
        if rows:
            self._rule = y + 6
            y += 13
        drop = fm_body.ascent() - fm_small.ascent()   # 詞性的小字對齊義項第一行的基線
        for i, (kind, label, text) in enumerate(rows):
            if kind == "head":
                row_h = fm_small.height()
            else:
                row_h = fm_body.boundingRect(QRect(0, 0, text_w, 100000),
                                             self.WRAP, text).height()
            last = i == len(rows) - 1
            if y + row_h + (0 if last else 3 + fm_small.height()) + 8 > room and i > 0:
                # 螢幕放不下了（set、run 這種幾十個義項的字）：剩下的收成一個刪節號
                ops.append((QRect(12, y, inner, fm_small.height()), self.LINE,
                            "…", small, self.DIM))
                y += fm_small.height() + 3
                break
            if kind == "head":
                ops.append((QRect(12, y, inner, row_h), self.LINE, text, small, self.DIM))
            else:
                if label:
                    ops.append((QRect(12, y + drop, label_w, fm_small.height()),
                                self.LINE, label, small, self.POS))
                ops.append((QRect(12 + label_w, y, text_w, row_h), self.WRAP,
                            text, body, self.BODY))
            y += row_h + 3
        if rows:
            y -= 3
        self._ops = ops

        w, h = inner + 24, y + 8
        x = max(area.left() + 4, min(anchor.left(), area.right() - w - 4))
        y = anchor.bottom() + 8
        if y + h > area.bottom():                 # 下面放不下就翻到上面
            y = max(area.top(), anchor.top() - h - 8)
        self.setGeometry(x, y, w, h)
        self.show()
        join_all_spaces(self, level=1002)
        self.raise_()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(28, 28, 32, 240))
        p.drawRoundedRect(self.rect(), 8, 8)

        if self._rule is not None:
            p.setPen(QColor(255, 255, 255, 36))
            p.drawLine(12, self._rule, self.width() - 12, self._rule)
        for rect, flags, text, font, color in self._ops:
            p.setFont(font)
            p.setPen(color)
            p.drawText(rect, flags, text)


# --------------------------------------------------------------------------
# 狀態提示
# --------------------------------------------------------------------------

class Toast(QWidget):
    """右下角的小提示條（辨識中／翻譯中／錯誤）。"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._text = ""
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def say(self, text: str, msec: int = 0):
        self._text = text
        font = QFont(self.cfg["font_family"], 13)
        fm = QFontMetrics(font)
        w = max(160, fm.horizontalAdvance(text) + 32)
        h = fm.height() * (text.count("\n") + 1) + 20
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(scr.right() - w - 24, scr.bottom() - h - 24, w, h)
        self.show()
        join_all_spaces(self, level=1001)
        self.raise_()
        self.update()
        if msec:
            QTimer.singleShot(msec, self.hide)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(24, 24, 27, 225))
        p.drawRoundedRect(self.rect(), 8, 8)
        p.setPen(QColor(240, 240, 240))
        p.setFont(QFont(self.cfg["font_family"], 13))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self._text)


# --------------------------------------------------------------------------
# AI 校稿／修飾的確認視窗
# --------------------------------------------------------------------------

class RewriteDialog(QWidget):
    """上面原文（唯讀）、下面結果（可以再改），三個按鈕：取消／只複製／取代。

    選擇結果用 chosen(動作, 文字) 送出去，動作是 "replace"、"copy" 或 "cancel"。
    """

    chosen = pyqtSignal(str, str)

    def __init__(self, cfg):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(560, 420)
        font = QFont(cfg["font_family"], 14)

        self.original = QPlainTextEdit(readOnly=True)
        self.original.setFont(font)
        self.original.setMaximumHeight(110)
        self.result = QPlainTextEdit()
        self.result.setFont(font)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for label, action in (("取消", "cancel"), ("只複製", "copy"), ("取代原文", "replace")):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, a=action: self._choose(a))
            buttons.addWidget(btn)
            if action == "replace":
                btn.setDefault(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("原文"))
        layout.addWidget(self.original)
        layout.addWidget(QLabel("結果（可以直接修改，⌘↩ 取代原文）"))
        layout.addWidget(self.result, 1)
        layout.addLayout(buttons)

        # 用快捷鍵而不是 keyPressEvent：焦點在文字框時 Return 會先被文字框吃掉
        QShortcut(QKeySequence("Ctrl+Return"), self,       # Qt 在 macOS 上 Ctrl＝⌘
                  activated=lambda: self._choose("replace"))
        QShortcut(QKeySequence("Escape"), self, activated=lambda: self._choose("cancel"))

    def ask(self, title: str, original: str, revised: str):
        self.setWindowTitle(title)
        self.original.setPlainText(original.strip())
        self.result.setPlainText(revised)
        self.show()
        join_all_spaces(self)
        force_key_window(self)
        self.raise_()
        self.result.setFocus()

    def _choose(self, action: str):
        self.hide()
        self.chosen.emit(action, self.result.toPlainText())

    def closeEvent(self, e):
        e.ignore()
        self._choose("cancel")
