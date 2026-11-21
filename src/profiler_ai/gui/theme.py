"""Shared palette and line icons for the investigation workspace."""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

_PATHS = {
    "brand": '<path d="M1 20 10 5l4 7-3 5-1-4-4 7z M12 7l3-5 9 18h-5z" fill="#ff850f" stroke="none"/>',
    "report": '<path d="M6 3h9l4 4v14H6z M14 3v5h5 M9 12h7 M9 16h5"/>',
    "measurements": '<path d="M4 4v16h17 M8 16v-4 M13 16V7 M18 16v-7"/>',
    "findings": '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5 M7 11l2-3 2 4 2-3"/>',
    "gaps": '<circle cx="12" cy="12" r="8"/><path d="M12 7v6 M12 16v.5"/>',
    "code": '<path d="m8 6-6 6 6 6 m8-12 6 6-6 6 M14 4l-4 16"/>',
    "evidence": '<path d="m12 3 9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5"/>',
    "experiments": '<path d="M9 3h6 M10 3v6l-6 9q-1 2 2 2h12q3 0 2-2l-6-9V3 M7 15h10"/>',
    "folder": '<path d="M3 7V5h7l2 3h9v11H3z"/>',
    "plus": '<path d="M12 5v14 M5 12h14"/>',
    "back": '<path d="m13 5-7 7 7 7 M6 12h15"/>',
    "layout": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16 M9 10h12"/>',
    "close": '<path d="m7 7 10 10 M17 7 7 17"/>',
    "float": '<rect x="8" y="4" width="12" height="12" rx="1"/><path d="M5 8H3v12h12v-2"/>',
}


def icon(name: str, color: str = "#b6b5b1") -> QIcon:
    drawing = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.65" '
        f'stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(drawing.encode()))
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in {
        "Window": "#201f1b",
        "WindowText": "#e6e5e1",
        "Base": "#1d1c18",
        "AlternateBase": "#282723",
        "Text": "#e6e5e1",
        "Button": "#31302c",
        "ButtonText": "#e6e5e1",
        "Highlight": "#503a24",
        "HighlightedText": "#f1f0ec",
        "ToolTipBase": "#393834",
        "ToolTipText": "#f5f4f0",
        "Link": "#58d5ed",
        "PlaceholderText": "#a6a5a1",
        "Light": "#4a4945",
        "Mid": "#3a3935",
        "Dark": "#1b1a16",
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#93928e"))
    app.setPalette(palette)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if "Segoe UI" in QFontDatabase.families():
        font.setFamily("Segoe UI")
    font.setPointSizeF(10.5)
    app.setFont(font)
    app.setStyleSheet("""
        QMainWindow { background: #151410; }
        QMainWindow::separator { background: #393834; width: 2px; height: 2px; }
        QMainWindow::separator:hover { background: #ef8927; }
        QDockWidget { background: #201f1b; }
        QWidget#dockTitle { background: #282723; border-bottom: 1px solid #3a3935; }
        QLabel#dockLabel { color: #ebe8e0; font-size: 14px; font-weight: 600; padding: 4px 3px; border-bottom: 2px solid #ee8c26; }
        QWidget#topBar { background: #1c1b17; border-bottom: 1px solid #363531; }
        QLabel#product { color: #f4f3ef; font-weight: 650; font-size: 17px; }
        QLabel#documentName { color: #b7b6b2; font-size: 13px; }
        QWidget#navigator { background: #1d1c18; }
        QLabel#navGroup { color: #a4a39f; font-size: 11px; font-weight: 600; }
        QPushButton#navItem { text-align: left; padding: 7px 10px; border: 1px solid transparent;
                              background: transparent; color: #c7c6c2; border-radius: 5px; }
        QPushButton#navItem:hover, QToolButton:hover { background: #31302c; color: #f5f4f0; }
        QPushButton#navItem:checked { background: #403122; color: #ffe9d2; }
        QPushButton#navItem:focus, QToolButton:focus, QPushButton:focus { border: 1px solid #ffab51; }
        QToolButton { background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 4px; }
        QToolButton::menu-indicator { image: none; }
        QPushButton { background: #31302c; border: 1px solid #43423e; border-radius: 5px; padding: 5px 11px; }
        QPushButton:hover { background: #3b3a36; }
        QPushButton#primary { color: #22211d; background: #f49a38; border: 1px solid #f49a38; font-weight: 600; }
        QPushButton#primary:hover { background: #ffb563; }
        QLineEdit,QComboBox { border: 1px solid #51504c; background: #1d1c18; padding: 5px; border-radius: 3px; }
        QLineEdit:focus,QComboBox:focus { border: 1px solid #ff921f; }
        QTabBar::tab { background: #262521; color: #b9b8b4; padding: 7px 15px; }
        QTabBar::tab:selected { background: #31302c; color: #eae9e5; border-bottom: 2px solid #ff921f; }
        QStatusBar { background: #1e1d19; border-top: 1px solid #363531; color: #b7b6b2; }
        QStatusBar::item { border: none; }
        QMenu { border: 1px solid #4a4945; padding: 4px; }
        QMenu::item { padding: 6px 22px; }
        QMenu::item:selected { background: #503a24; }
        QToolTip { color: #e9e8e4; background: #32312d; border: 1px solid #63625e; padding: 5px; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
        QScrollBar::handle:vertical { background: #4f4e4a; border-radius: 4px; min-height: 28px; }
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height: 0; }
    """)
