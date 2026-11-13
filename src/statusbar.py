"""選單列圖示：直接用原生 NSStatusItem + NSMenu，不用 Qt 的 QSystemTrayIcon。

為什麼不用 QSystemTrayIcon：macOS 27 的選單列點擊改走 scene action，
選單打開當下 NSApp.currentEvent 是 KitDefined 事件而不是滑鼠事件。
Qt 6.11 的 QCocoaSystemTrayIcon 在選單開始追蹤時會無條件呼叫
currentEvent.clickCount 判斷單擊／雙擊，對非滑鼠事件呼叫就會丟
NSInternalInconsistencyException，整個程式直接 abort（沒有任何 Python 例外可接）。

選單項目仍然用 QAction 描述；這裡只負責把它們畫成原生選單，
點下去時回到 Qt 呼叫 action.trigger()，勾選狀態每次開選單前從 QAction 同步。
"""
from __future__ import annotations

import AppKit
import objc
from PyQt6.QtCore import QBuffer, QIODevice, QTimer
from PyQt6.QtGui import QAction, QIcon


class _MenuTarget(AppKit.NSObject):
    def initWithActions_(self, actions):
        self = objc.super(_MenuTarget, self).init()
        if self is None:
            return None
        self._actions = actions
        return self

    def fire_(self, sender):
        # 等選單完全收起、回到 Qt 事件迴圈再執行，免得在選單追蹤中開視窗
        QTimer.singleShot(0, self._actions[sender.tag()].trigger)

    def menuNeedsUpdate_(self, menu):
        for item in menu.itemArray():
            if item.isSeparatorItem():
                continue
            action = self._actions[item.tag()]
            item.setTitle_(action.text())
            item.setEnabled_(action.isEnabled())
            item.setState_(AppKit.NSControlStateValueOn if action.isChecked()
                           else AppKit.NSControlStateValueOff)


def _ns_image(icon: QIcon, pt: int = 18):
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    icon.pixmap(pt * 2, pt * 2).save(buf, "PNG")        # 2× 給 Retina
    raw = bytes(buf.data())
    img = AppKit.NSImage.alloc().initWithData_(
        AppKit.NSData.dataWithBytes_length_(raw, len(raw)))
    img.setSize_((pt, pt))
    return img


class MenuBarIcon:
    """actions 裡放 None 代表分隔線。物件要一直留著參考，否則圖示會消失。"""

    def __init__(self, icon: QIcon, actions: list[QAction | None], tooltip: str = ""):
        self._actions = actions
        self._target = _MenuTarget.alloc().initWithActions_(actions)

        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._target)
        for tag, action in enumerate(actions):
            if action is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                action.text(), "fire:", "")
            item.setTarget_(self._target)
            item.setTag_(tag)
            menu.addItem_(item)
        self._menu = menu

        self._item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        button = self._item.button()
        button.setImage_(_ns_image(icon))
        if tooltip:
            button.setToolTip_(tooltip)
        self._item.setMenu_(menu)
