"""Dark, minimal, futuristic theme."""
BG = "#070b14"
PANEL = "#0d1424"
PANEL2 = "#111b30"
BORDER = "#1c2a47"
TEXT = "#d7e3ff"
DIM = "#7d8fb3"
ACCENT = "#38e1ff"

STATE_COLORS = {
    "IDLE": "#2fb8d6", "LISTENING": "#3dffb5", "PROCESSING": "#a98bff",
    "EXECUTING": "#ffb84d", "SPEAKING": "#ff5fc8", "ERROR": "#ff4d5e",
}

QSS = f"""
* {{ font-family: 'Inter','Noto Sans','DejaVu Sans'; font-size: 13px; color: {TEXT}; }}
QMainWindow, QDialog, QWidget#root {{ background: {BG}; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#brand {{ font-size: 22px; font-weight: 800; letter-spacing: 6px; color: {ACCENT}; }}
QLabel#sub {{ color: {DIM}; font-size: 11px; letter-spacing: 2px; }}
QLabel#state {{ font-size: 15px; font-weight: 700; letter-spacing: 5px; }}
QLabel#caption {{ color: {DIM}; font-size: 12px; }}
QLabel#pill {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 10px; padding: 3px 10px; color: {DIM}; font-size: 11px; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 9px; padding: 8px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; background: #15233f; }}
QPushButton:pressed {{ background: #0f1a30; }}
QPushButton:checked {{ border-color: #3dffb5; color: #3dffb5; }}
QPushButton#danger {{ border-color: #5a2630; color: #ff8896; }}
QPushButton#danger:hover {{ border-color: #ff4d5e; background: #2a1219; }}
QPushButton#primary {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#allow {{ border-color: #2c7a55; color: #3dffb5; }}
QPushButton#deny {{ border-color: #7a2c3a; color: #ff8896; }}
QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget, QComboBox, QSpinBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 9px; padding: 6px 9px;
    selection-background-color: #1d4a63; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 9px; background: {PANEL}; }}
QTabBar::tab {{ background: transparent; padding: 7px 14px; color: {DIM}; }}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; margin: -6px 0; border-radius: 7px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; }}
QFrame#confirm {{ background: #2a2210; border: 1px solid #ffb84d; border-radius: 10px; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""
