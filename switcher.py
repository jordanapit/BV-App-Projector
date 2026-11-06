import os
import sys
import ctypes
from ctypes import wintypes
import win32gui
import win32con
import win32api
import darkdetect
import winreg
import json

# Bungkam log font Qt yang tidak perlu
os.environ["QT_LOGGING_RULES"] = "qt.qpa.fonts=false"

from PyQt5.QtCore import (
    Qt, QTimer, QPropertyAnimation, QParallelAnimationGroup, QEasingCurve, QRect
)
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFrame, QLayout
)
from PyQt5.QtWinExtras import QtWin

from qfluentwidgets import (
    ComboBox, LineEdit, PushButton, PrimaryPushButton,
    SwitchButton, TransparentToolButton,
    BodyLabel, StrongBodyLabel, CaptionLabel,
    CardWidget, SmoothScrollArea, FluentIcon as FIF,
    setTheme, Theme, setThemeColor, themeColor, MessageBox
)

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
uxtheme = ctypes.windll.uxtheme
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BVAppProjector")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int)
    ]

class WTA_OPTIONS(ctypes.Structure):
    _fields_ = [
        ("dwFlags", ctypes.c_uint32),
        ("dwMask", ctypes.c_uint32)
    ]

def get_windows_accent_color():
    """Ambil warna aksen dari pengaturan Windows."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        value, _ = winreg.QueryValueEx(key, "AccentColor")
        winreg.CloseKey(key)
        r = value & 0xFF
        g = (value >> 8) & 0xFF
        b = (value >> 16) & 0xFF
        return QColor(r, g, b)
    except Exception:
        return QColor("#0078D4")

def resource_path(name):
    """Cari file di folder program (juga bekerja setelah dibungkus jadi .exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

class BVAppProjector(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BV App Projector")
        self.setWindowIcon(QIcon(resource_path("app_icon.png")))

        # Tanpa tombol maximize aktif (tombolnya tetap tampil tapi abu-abu)
        self.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint
        )
        self.resize(560, 720)
        self.setMinimumWidth(560)

        # Supaya Mica dari Windows bisa terlihat di belakang isi aplikasi
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Deteksi tema otomatis dari Windows (Dark / Light)
        self.config = self.load_config()
        self.is_dark = self.config.get("is_dark", False)
        self.is_compact = False
        self.active_slot = None

        self.monitors = []
        self.windows = {}
        self.slot_cards = []
        self.live_buttons = []

        # Timer Transisi Dissolve
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self._step_dissolve)
        self.fade_step = 0
        self.fade_total_steps = 20
        self.fade_in_hwnd = None
        self.fade_out_hwnds = []
        self.target_virt_rect = None

        setTheme(Theme.DARK if self.is_dark else Theme.LIGHT)
        setThemeColor(get_windows_accent_color())

        self.setup_ui()

        # Buat 3 slot awal
        saved_slots = self.config.get("slots", [])
        for i in range(max(3, len(saved_slots))):
            self.add_slot()
            if i < len(saved_slots):
                self.slot_cards[i]["name"].setText(saved_slots[i].get("label", f"Slot {i+1}"))

        self.poll_system_state()
        self.restore_monitor_selection()

        # Auto-Update Monitor & Window setiap 1.5 detik
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self.poll_system_state)
        self.auto_timer.start(1500)

        QTimer.singleShot(80, self.init_native_window_style)
        self.restore_window_geometry()
        if self.config.get("compact", False):
            QTimer.singleShot(400, self.toggle_compact_mode)

    def reset_configuration(self):
        if getattr(self, "is_animating", False):
            return

        box = MessageBox(
            "Reset configuration",
            "Restore all settings (theme, pin, transition, monitors, slots and window size) to their defaults?",
            self
        )
        box.yesButton.setText("Reset")
        box.cancelButton.setText("Cancel")
        if not box.exec():
            return

        # 1. Keluar dari mode Compact langsung (tanpa animasi)
        if self.is_compact:
            self.is_compact = False
            for w in (self.toolbar_card, self.config_section):
                w.setMaximumHeight(16777215)
                w.setVisible(True)
            self.btn_expand.setVisible(False)
            self.apply_compact_style(False)
            self.setMinimumSize(560, 560)
            self.setMaximumSize(16777215, 16777215)

        # 2. Pin, tema, dan transisi kembali ke awal
        self.sw_top.setChecked(True)
        self.sw_dissolve.setChecked(True)
        self.sw_theme.setChecked(False)

        # 3. Pilihan monitor kembali ke awal
        count = len(self.monitors)
        self.cb_virtual.setCurrentIndex(1 if count > 1 else 0)
        self.cb_videotron.setCurrentIndex(2 if count > 2 else 0)

        # 4. Slot kembali ke 3 slot dengan nama awal
        self.active_slot = None
        while len(self.slot_cards) > 3:
            self.remove_slot(self.slot_cards[-1])
        for i, s in enumerate(self.slot_cards):
            s["name"].setText(f"Slot {i+1}")
            s["cb"].setCurrentIndex(0)
        self.rebuild_live_buttons()

        # 5. Ukuran window kembali ke awal, lalu simpan
        self.resize(560, 720)
        self.save_config()

    def load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self):
        geo = self.geometry()
        height = geo.height()
        width = geo.width()
        if self.is_compact:
            height = getattr(self, "full_height", height)
            width = getattr(self, "full_width", width)
        x = geo.x() - (width - geo.width()) // 2

        data = {
            "x": x,
            "bottom": geo.y() + geo.height(),
            "width": width,
            "height": height,
            "compact": self.is_compact,
            "pin": self.sw_top.isChecked(),
            "is_dark": self.is_dark,
            "dissolve": self.sw_dissolve.isChecked(),
            "virtual_monitor": self.cb_virtual.currentText(),
            "output_monitor": self.cb_videotron.currentText(),
            "slots": [{"label": s["name"].text()} for s in self.slot_cards],
        }
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def closeEvent(self, event):
        self.save_config()
        super().closeEvent(event)

    def restore_window_geometry(self):
        c = self.config
        try:
            w = max(560, int(c.get("width", 560)))
            h = max(560, int(c.get("height", 720)))
            x = int(c["x"])
            y = int(c["bottom"]) - h
        except (KeyError, ValueError, TypeError):
            return
        rect = QRect(x, y, w, h)
        # Hanya dipulihkan kalau posisinya masih terlihat di salah satu layar
        if any(rect.intersects(s.geometry()) for s in QApplication.screens()):
            self.setGeometry(rect)

    def restore_monitor_selection(self):
        names = [m["name"] for m in self.monitors]
        pairs = (
            (self.cb_virtual, "virtual_monitor"),
            (self.cb_videotron, "output_monitor"),
        )
        for combo, key in pairs:
            saved = self.config.get(key)
            if saved in names:
                combo.setCurrentText(saved)

    def blank_small_icon(self):
        # Icon KECIL = yang tampil di title bar. Dikosongkan.
        # Icon BESAR (untuk taskbar) dibiarkan memakai icon asli.
        blank = QPixmap(16, 16)
        blank.fill(Qt.transparent)
        self.small_hicon = QtWin.toHICON(blank)
        win32gui.SendMessage(
            int(self.winId()), win32con.WM_SETICON,
            win32con.ICON_SMALL, int(self.small_hicon)
        )

    def init_native_window_style(self):
        self.blank_small_icon()
        self.toggle_always_on_top(self.sw_top.isChecked())
        self.apply_fluent_backdrop()
        QTimer.singleShot(300, self.apply_fluent_backdrop)
        QTimer.singleShot(600, self.blank_small_icon)

    def apply_fluent_backdrop(self):
        hwnd = int(self.winId())

        # Title bar asli ikut mode Dark / Light
        try:
            dark_mode = ctypes.c_int(1 if self.is_dark else 0)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode))
        except Exception:
            pass

        # Mica: perluas ke seluruh isi window, lalu aktifkan
        try:
            margins = MARGINS(-1, -1, -1, -1)
            dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
            backdrop = ctypes.c_int(2)  # 2 = Mica
            dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        except Exception:
            pass

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(12)
        main_layout.setSizeConstraint(QLayout.SetNoConstraint)

        # --- Toolbar Atas ---
        self.toolbar_card = CardWidget(self)
        toolbar_layout = QHBoxLayout(self.toolbar_card)
        toolbar_layout.setContentsMargins(14, 10, 14, 10)
        toolbar_layout.setSpacing(12)

        self.sw_top = SwitchButton(self)
        self.sw_top.setOnText("Pin")
        self.sw_top.setOffText("Pin")
        self.sw_top.setChecked(self.config.get("pin", True))
        self.sw_top.checkedChanged.connect(self.toggle_always_on_top)
        toolbar_layout.addWidget(self.sw_top)

        self.sw_theme = SwitchButton(self)
        self.sw_theme.setOnText("Dark")
        self.sw_theme.setOffText("Light")
        self.sw_theme.setChecked(self.is_dark)
        self.sw_theme.checkedChanged.connect(self.toggle_theme)
        toolbar_layout.addWidget(self.sw_theme)

        self.sw_dissolve = SwitchButton(self)
        self.sw_dissolve.setOnText("Dissolve")
        self.sw_dissolve.setOffText("Cut")
        self.sw_dissolve.setChecked(self.config.get("dissolve", True))
        toolbar_layout.addWidget(self.sw_dissolve)

        toolbar_layout.addStretch()
        self.btn_reset = TransparentToolButton(FIF.SYNC, self)
        self.btn_reset.setFixedSize(34, 34)
        self.btn_reset.setToolTip("Reset configuration")
        self.btn_reset.clicked.connect(self.reset_configuration)
        toolbar_layout.addWidget(self.btn_reset)

        self.btn_compact = PushButton(FIF.MINIMIZE, "Compact", self)
        self.btn_compact.clicked.connect(self.toggle_compact_mode)
        toolbar_layout.addWidget(self.btn_compact)

        main_layout.addWidget(self.toolbar_card)

        # --- Bagian Pengaturan ---
        self.config_section = QWidget(self)
        cfg_layout = QVBoxLayout(self.config_section)
        cfg_layout.setContentsMargins(0, 0, 0, 0)
        cfg_layout.setSpacing(12)

        mon_card = CardWidget(self.config_section)
        mon_layout = QVBoxLayout(mon_card)
        mon_layout.setContentsMargins(16, 14, 16, 14)
        mon_layout.setSpacing(8)

        mon_hdr = QHBoxLayout()
        mon_hdr.addWidget(StrongBodyLabel("Display Settings", self))
        mon_hdr.addStretch()
        mon_layout.addLayout(mon_hdr)

        mon_layout.addWidget(CaptionLabel("Standby Monitor (Virtual Display):", self))
        self.cb_virtual = ComboBox(self)
        mon_layout.addWidget(self.cb_virtual)

        mon_layout.addWidget(CaptionLabel("Output Monitor (Projector / Display):", self))
        self.cb_videotron = ComboBox(self)
        mon_layout.addWidget(self.cb_videotron)

        cfg_layout.addWidget(mon_card)

        slot_hdr = QHBoxLayout()
        slot_hdr.setContentsMargins(4, 2, 4, 2)
        slot_hdr.addWidget(StrongBodyLabel("Window Slots", self))
        slot_hdr.addStretch()
        self.btn_add_slot = PrimaryPushButton(FIF.ADD, "Add Slot", self)
        self.btn_add_slot.clicked.connect(self.add_slot)
        slot_hdr.addWidget(self.btn_add_slot)
        cfg_layout.addLayout(slot_hdr)

        self.scroll_area = SmoothScrollArea(self.config_section)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setStyleSheet("SmoothScrollArea, QWidget#SlotsViewport { background: transparent; border: none; }")

        self.slots_widget = QWidget()
        self.slots_widget.setObjectName("SlotsViewport")
        self.slots_layout = QVBoxLayout(self.slots_widget)
        self.slots_layout.setContentsMargins(0, 0, 0, 0)
        self.slots_layout.setSpacing(8)
        self.slots_layout.addStretch()

        self.scroll_area.setWidget(self.slots_widget)
        cfg_layout.addWidget(self.scroll_area, stretch=1)

        main_layout.addWidget(self.config_section, stretch=1)

        # --- Panel Live Switcher ---
        self.live_card = CardWidget(self)
        live_card_layout = QVBoxLayout(self.live_card)
        live_card_layout.setContentsMargins(14, 12, 14, 14)
        live_card_layout.setSpacing(10)

        live_hdr = QHBoxLayout()
        live_hdr.addWidget(StrongBodyLabel("Live Switcher", self))
        live_hdr.addStretch()
        self.lbl_status = CaptionLabel("Status: Standby (Virtual Display)", self)
        live_hdr.addWidget(self.lbl_status)
        self.btn_expand = TransparentToolButton(FIF.FULL_SCREEN, self)
        self.btn_expand.setFixedSize(28, 28)
        self.btn_expand.setToolTip("Expand")
        keep_space = self.btn_expand.sizePolicy()
        keep_space.setRetainSizeWhenHidden(True)
        self.btn_expand.setSizePolicy(keep_space)
        self.btn_expand.clicked.connect(self.toggle_compact_mode)
        live_hdr.addWidget(self.btn_expand)
        self.btn_expand.setVisible(False)
        live_card_layout.addLayout(live_hdr)

        self.live_grid = QGridLayout()
        self.live_grid.setSpacing(8)
        live_card_layout.addLayout(self.live_grid)

        self.btn_clear = PushButton(FIF.HIDE, "Hide All to Virtual Display", self)
        self.btn_clear.setFixedHeight(38)
        self.btn_clear.clicked.connect(lambda: self.switch_live(None))
        live_card_layout.addWidget(self.btn_clear)

        main_layout.addWidget(self.live_card)

    def add_slot(self):
        idx = len(self.slot_cards)
        card = CardWidget(self.slots_widget)
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(14, 10, 14, 10)
        c_layout.setSpacing(8)

        row = QHBoxLayout()
        lbl = StrongBodyLabel(f"Slot {idx+1}", card)
        lbl.setFixedWidth(52)
        row.addWidget(lbl)

        name_input = LineEdit(card)
        name_input.setPlaceholderText(f"Type a label for Slot {idx+1}...")
        name_input.setText(f"Slot {idx+1}")
        row.addWidget(name_input)

        btn_del = TransparentToolButton(FIF.DELETE, card)
        btn_del.setFixedSize(32, 32)
        row.addWidget(btn_del)
        c_layout.addLayout(row)

        cb_win = ComboBox(card)
        win_list = ["-- Select Application Window --"] + sorted(list(self.windows.keys()))
        cb_win.addItems(win_list)
        c_layout.addWidget(cb_win)

        self.slots_layout.insertWidget(self.slots_layout.count() - 1, card)

        slot_data = {
            "card": card,
            "lbl": lbl,
            "name": name_input,
            "cb": cb_win,
            "btn_del": btn_del
        }
        self.slot_cards.append(slot_data)

        name_input.textChanged.connect(self.rebuild_live_buttons)
        btn_del.clicked.connect(lambda checked=False, s=slot_data: self.remove_slot(s))

        self.rebuild_live_buttons()

    def remove_slot(self, slot_data):
        if len(self.slot_cards) <= 3:
            return
        idx = self.slot_cards.index(slot_data)
        if self.active_slot == idx:
            self.active_slot = None
        elif self.active_slot is not None and self.active_slot > idx:
            self.active_slot -= 1

        self.slot_cards.remove(slot_data)
        slot_data["card"].deleteLater()

        for i, s in enumerate(self.slot_cards):
            s["lbl"].setText(f"Slot {i+1}")

        self.rebuild_live_buttons()

    def rebuild_live_buttons(self):
        for btn in self.live_buttons:
            btn.deleteLater()
        self.live_buttons.clear()

        for i, s in enumerate(self.slot_cards):
            raw_name = s["name"].text().strip()
            label = raw_name if raw_name else f"Slot {i+1}"

            if self.active_slot == i:
                btn = PrimaryPushButton(FIF.PLAY, label, self)
            else:
                btn = PushButton(label, self)

            btn.setFixedHeight(32 if self.is_compact else 40)
            btn.clicked.connect(lambda checked=False, idx=i: self.switch_live(idx))
            self.live_grid.addWidget(btn, i // 3, i % 3)
            self.live_buttons.append(btn)

            s["btn_del"].setEnabled(len(self.slot_cards) > 3)

        if self.active_slot is not None and self.active_slot < len(self.slot_cards):
            active_name = self.slot_cards[self.active_slot]["name"].text().strip() or f"Slot {self.active_slot+1}"
            self.lbl_status.setText(f"🟢 LIVE: {active_name}")
        else:
            self.lbl_status.setText("Status: Standby (Virtual Display)")

    def poll_system_state(self):
        new_monitors = []
        for i, mon in enumerate(win32api.EnumDisplayMonitors()):
            info = win32api.GetMonitorInfo(mon[0])
            rect = info['Monitor']
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            primary = " [Primary]" if info['Flags'] == 1 else ""
            new_monitors.append({
                "name": f"Display {i+1}: {w}x{h} (X:{rect[0]}, Y:{rect[1]}){primary}",
                "rect": rect
            })

        old_mon_names = [m["name"] for m in self.monitors]
        new_mon_names = [m["name"] for m in new_monitors]
        if new_mon_names != old_mon_names:
            first_load = (len(old_mon_names) == 0)
            virt_curr = self.cb_virtual.currentText()
            vid_curr = self.cb_videotron.currentText()
            self.monitors = new_monitors

            self.cb_virtual.clear()
            self.cb_virtual.addItems(new_mon_names)
            if not first_load and virt_curr in new_mon_names:
                self.cb_virtual.setCurrentText(virt_curr)
            elif len(new_mon_names) > 1:
                self.cb_virtual.setCurrentIndex(1)

            self.cb_videotron.clear()
            self.cb_videotron.addItems(new_mon_names)
            if not first_load and vid_curr in new_mon_names:
                self.cb_videotron.setCurrentText(vid_curr)
            elif len(new_mon_names) > 2:
                self.cb_videotron.setCurrentIndex(2)

        new_windows = {}
        my_hwnd = int(self.winId())

        def enum_handler(hwnd, ctx):
            if win32gui.IsWindowVisible(hwnd) and hwnd != my_hwnd:
                title = win32gui.GetWindowText(hwnd)
                if title and title not in ["Program Manager", "BV App Projector", "Settings", "Windows Input Experience"]:
                    new_windows[f"{title} ({hwnd})"] = hwnd

        win32gui.EnumWindows(enum_handler, None)

        if set(new_windows.keys()) != set(self.windows.keys()):
            self.windows = new_windows
            win_list = ["-- Select Application Window --"] + sorted(list(self.windows.keys()))
            for s in self.slot_cards:
                cb = s["cb"]
                curr = cb.currentText()
                cb.blockSignals(True)
                cb.clear()
                cb.addItems(win_list)
                if curr in win_list:
                    cb.setCurrentText(curr)
                cb.blockSignals(False)

    def keep_on_screen(self, rect):
        """Geser posisi window supaya tidak keluar dari layar."""
        screen = QApplication.screenAt(rect.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        geo = self.geometry()
        frame = self.frameGeometry()
        left_extra = geo.x() - frame.x()
        right_extra = frame.right() - geo.right()
        top_extra = geo.y() - frame.y()
        bottom_extra = frame.bottom() - geo.bottom()

        x = min(rect.x(), area.right() - rect.width() - right_extra + 1)
        x = max(x, area.left() + left_extra)
        y = min(rect.y(), area.bottom() - rect.height() - bottom_extra + 1)
        y = max(y, area.top() + top_extra)
        return QRect(x, y, rect.width(), rect.height())

    def apply_compact_style(self, compact):
        if compact:
            self.layout().setContentsMargins(8, 0, 8, 8)
            self.setWindowTitle(" ")
        else:
            self.layout().setContentsMargins(18, 18, 18, 18)
            self.setWindowTitle("BV App Projector")
        for b in self.live_buttons:
            b.setFixedHeight(32 if compact else 40)
        self.btn_clear.setFixedHeight(30 if compact else 38)

    def toggle_compact_mode(self):
        # Abaikan klik kalau animasi masih berjalan
        if getattr(self, "is_animating", False):
            return
        self.is_animating = True

        geo = self.geometry()
        bottom = geo.y() + geo.height()
        center_x = geo.x() + geo.width() // 2
        sections = [self.toolbar_card, self.config_section]

        # Lepas semua batas ukuran supaya animasi bisa berjalan
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        if not self.is_compact:
            # Mengecil ke mode Compact
            self.is_compact = True
            self.full_width = geo.width()
            self.full_height = geo.height()
            self.section_heights = [w.height() for w in sections]
            self.apply_compact_style(True)
            self.live_card.layout().activate()
            margins = self.layout().contentsMargins()
            target_w = 420
            target_h = margins.top() + margins.bottom() + self.live_card.sizeHint().height()
            ranges = [(h, 0) for h in self.section_heights]
            self.btn_expand.setVisible(True)
        else:
            # Kembali membesar
            self.is_compact = False
            self.apply_compact_style(False)
            target_w = self.full_width
            target_h = self.full_height
            ranges = [(0, h) for h in self.section_heights]
            for w in sections:
                w.setMaximumHeight(0)
                w.setVisible(True)
            self.btn_expand.setVisible(False)

        self.anim_group = QParallelAnimationGroup(self)

        # Animasi window: sisi bawah tetap, lebar menyusut ke tengah
        win_anim = QPropertyAnimation(self, b"geometry")
        win_anim.setDuration(250)
        win_anim.setEasingCurve(QEasingCurve.InOutCubic)
        win_anim.setStartValue(geo)
        new_rect = self.keep_on_screen(
            QRect(center_x - target_w // 2, bottom - target_h, target_w, target_h)
        )
        win_anim.setEndValue(new_rect)
        self.anim_group.addAnimation(win_anim)

        # Animasi bagian toolbar dan pengaturan (ikut mengecil / membesar)
        for w, (start, end) in zip(sections, ranges):
            sec_anim = QPropertyAnimation(w, b"maximumHeight")
            sec_anim.setDuration(250)
            sec_anim.setEasingCurve(QEasingCurve.InOutCubic)
            sec_anim.setStartValue(start)
            sec_anim.setEndValue(end)
            self.anim_group.addAnimation(sec_anim)

        self.anim_group.finished.connect(lambda: self._finish_compact(target_w, target_h))
        self.anim_group.start()

    def _finish_compact(self, target_w, target_h):
        sections = [self.toolbar_card, self.config_section]
        if self.is_compact:
            for w in sections:
                w.setVisible(False)
            self.setFixedSize(target_w, target_h)
        else:
            for w in sections:
                w.setMaximumHeight(16777215)
            self.setMinimumSize(560, 560)
            self.setMaximumSize(16777215, 16777215)
        self.is_animating = False

    def toggle_always_on_top(self, checked):
        hwnd = int(self.winId())
        flag = win32con.HWND_TOPMOST if checked else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(
            hwnd, flag, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        )

    def toggle_theme(self, checked):
        self.is_dark = checked
        setTheme(Theme.DARK if checked else Theme.LIGHT)
        self.apply_fluent_backdrop()
        self.rebuild_live_buttons()

    def set_window_opacity(self, hwnd, alpha):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return
        try:
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if not (ex_style & win32con.WS_EX_LAYERED):
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style | win32con.WS_EX_LAYERED)
            user32.SetLayeredWindowAttributes(hwnd, 0, int(max(0, min(255, alpha))), win32con.LWA_ALPHA)
        except Exception:
            pass

    def move_window_to_monitor(self, hwnd, monitor_rect, bring_top=False):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return
        left, top, right, bottom = monitor_rect
        width, height = right - left, bottom - top
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        z_order = win32con.HWND_TOPMOST if bring_top else win32con.HWND_TOP
        win32gui.SetWindowPos(
            hwnd, z_order, left, top, width, height,
            win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE
        )
        if bring_top:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_NOTOPMOST, left, top, width, height,
                win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE
            )

    def switch_live(self, active_idx):
        idx_virt = self.cb_virtual.currentIndex()
        idx_vid = self.cb_videotron.currentIndex()
        if idx_virt < 0 or idx_vid < 0 or idx_virt >= len(self.monitors) or idx_vid >= len(self.monitors):
            return

        if self.fade_timer.isActive():
            self.fade_timer.stop()

        rect_virt = self.monitors[idx_virt]["rect"]
        rect_vid = self.monitors[idx_vid]["rect"]

        target_hwnd = None
        if active_idx is not None and active_idx < len(self.slot_cards):
            target_hwnd = self.windows.get(self.slot_cards[active_idx]["cb"].currentText())

        other_hwnds = []
        for i, s in enumerate(self.slot_cards):
            if i != active_idx:
                h = self.windows.get(s["cb"].currentText())
                if h and h != target_hwnd and win32gui.IsWindow(h):
                    other_hwnds.append(h)

        self.active_slot = active_idx
        self.rebuild_live_buttons()

        if not self.sw_dissolve.isChecked():
            if target_hwnd:
                self.set_window_opacity(target_hwnd, 255)
                self.move_window_to_monitor(target_hwnd, rect_vid, bring_top=True)
            for h in other_hwnds:
                self.set_window_opacity(h, 255)
                self.move_window_to_monitor(h, rect_virt)
            return

        self.fade_step = 0
        self.fade_in_hwnd = target_hwnd
        self.fade_out_hwnds = other_hwnds
        self.target_virt_rect = rect_virt

        if target_hwnd:
            self.set_window_opacity(target_hwnd, 0)
            self.move_window_to_monitor(target_hwnd, rect_vid, bring_top=True)

        self.fade_timer.start(15)

    def _step_dissolve(self):
        self.fade_step += 1
        progress = self.fade_step / float(self.fade_total_steps)

        if self.fade_in_hwnd:
            alpha_in = int(255 * progress)
            self.set_window_opacity(self.fade_in_hwnd, alpha_in)
        else:
            alpha_out = int(255 * (1.0 - progress))
            for h in self.fade_out_hwnds:
                self.set_window_opacity(h, alpha_out)

        if self.fade_step >= self.fade_total_steps:
            self.fade_timer.stop()
            if self.fade_in_hwnd:
                self.set_window_opacity(self.fade_in_hwnd, 255)
            for h in self.fade_out_hwnds:
                self.move_window_to_monitor(h, self.target_virt_rect)
                self.set_window_opacity(h, 255)


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BV.AppProjector")
    window = BVAppProjector()
    window.show()
    sys.exit(app.exec_())