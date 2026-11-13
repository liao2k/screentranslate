"""抓別的 App 裡選取的文字、把結果貼回去：模擬 ⌘C / ⌘V，前後把剪貼簿還原。

選單列工具沒辦法直接讀別人的選取範圍（Accessibility API 只有原生文字框給得出來，
瀏覽器、Electron 多半拿不到），用剪貼簿繞一圈最通用。代價是要「輔助使用」權限，
跟 ⌃⌘T 全域快捷鍵是同一個權限。
"""
from __future__ import annotations

import time

import AppKit
import Quartz

KEY_C, KEY_V = 8, 9          # 實體鍵位，不受注音等輸入法影響


def can_post_keys(prompt: bool = False) -> bool:
    """有沒有「輔助使用」權限送出按鍵。prompt=True 時順便叫系統跳出授權詢問。"""
    if Quartz.CGPreflightPostEventAccess():
        return True
    if prompt:
        Quartz.CGRequestPostEventAccess()
    return False


def frontmost_app():
    return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()


def is_self(app) -> bool:
    return app is not None and app.processIdentifier() == AppKit.NSProcessInfo.processInfo().processIdentifier()


def _press(keycode: int) -> None:
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


# ---------- 剪貼簿 ----------

def snapshot() -> list[dict]:
    """把剪貼簿每一項、每一種格式都存起來（圖片、檔案也一樣），之後原封不動放回去。"""
    pb = AppKit.NSPasteboard.generalPasteboard()
    items = []
    for item in pb.pasteboardItems() or []:
        data = {}
        for kind in item.types():
            value = item.dataForType_(kind)
            if value is not None:
                data[kind] = value
        items.append(data)
    return items


def restore(items: list[dict]) -> None:
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    objects = []
    for data in items:
        item = AppKit.NSPasteboardItem.alloc().init()
        for kind, value in data.items():
            item.setData_forType_(value, kind)
        objects.append(item)
    if objects:
        pb.writeObjects_(objects)


def set_text(text: str) -> None:
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)


# ---------- 複製 / 貼上 ----------

def copy_selection(timeout: float = 1.0) -> str:
    """對最前面的 App 按 ⌘C，拿到選取的文字後把剪貼簿還原。沒選東西就回空字串。

    靠 changeCount 判斷有沒有真的複製到：沒選取時大部分 App 按 ⌘C 什麼都不做，
    直接讀剪貼簿會讀到上一次複製的舊東西。
    """
    pb = AppKit.NSPasteboard.generalPasteboard()
    saved = snapshot()
    before = pb.changeCount()
    text = ""
    # 從選單列點下去的當下，選單還在收、焦點還沒回到原本的 App，
    # 這時送出的 ⌘C 會被吃掉。先等一下，沒複製到再補按一次。
    for wait in (0.15, 0.35):
        time.sleep(wait)
        _press(KEY_C)
        deadline = time.monotonic() + timeout / 2
        while time.monotonic() < deadline and pb.changeCount() == before:
            time.sleep(0.03)
        if pb.changeCount() != before:
            time.sleep(0.05)             # 有些 App 分好幾次寫入，等它寫完
            text = pb.stringForType_(AppKit.NSPasteboardTypeString) or ""
            break
    if pb.changeCount() != before:
        restore(saved)
    return text


def paste_text(text: str) -> list[dict]:
    """把文字放進剪貼簿再按 ⌘V。回傳原本的剪貼簿，等目標 App 讀完再交給 restore()。"""
    saved = snapshot()
    set_text(text)
    _press(KEY_V)
    return saved


def activate(app) -> None:
    """把焦點還給原本的 App。macOS 14 起要先「讓出」前景，對方才搶得回去。"""
    if app is None:
        return
    ns_app = AppKit.NSApplication.sharedApplication()
    if hasattr(ns_app, "yieldActivationToApplication_"):
        ns_app.yieldActivationToApplication_(app)
    app.activateWithOptions_(0)
