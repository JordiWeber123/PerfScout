"""Window controls and navigation for the desktop reader."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .theme import icon


def label(text, name=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


def tool(name, glyph, callback):
    button = QToolButton()
    button.setIcon(icon(glyph))
    button.setToolTip(name)
    button.setAccessibleName(name)
    button.setIconSize(QSize(16, 16))
    button.clicked.connect(callback)
    return button


class Dock(QDockWidget):
    def __init__(self, title, glyph, widget, parent):
        super().__init__(title, parent)
        self.setObjectName("dock-" + glyph)
        self.setWidget(widget)
        bar = QWidget()
        bar.setObjectName("dockTitle")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 3, 5, 3)
        row.setSpacing(7)
        mark = QLabel()
        mark.setPixmap(icon(glyph).pixmap(15, 15))
        row.addWidget(mark)
        row.addWidget(label(title, "dockLabel"))
        row.addStretch()
        row.addWidget(
            tool("Float or dock " + title, "float", lambda: self.setFloating(not self.isFloating()))
        )
        row.addWidget(tool("Hide " + title, "close", self.hide))
        self.setTitleBarWidget(bar)


class Navigator(QWidget):
    selected = Signal(str)
    groups = (
        (
            "Investigation",
            (
                ("report", "Overview"),
                ("findings", "Findings"),
                ("measurements", "Measurements"),
                ("gaps", "Evidence gaps"),
            ),
        ),
        (
            "Inspect",
            (("evidence", "Evidence"), ("code", "Code and diff"), ("experiments", "Experiments")),
        ),
    )

    def __init__(self):
        super().__init__()
        self.setObjectName("navigator")
        self.setMinimumWidth(166)
        self.buttons = {}
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(10, 13, 10, 12)
        self.column.setSpacing(2)
        for title, entries in self.groups:
            heading = label(title.upper(), "navGroup")
            heading.setContentsMargins(10, 7, 0, 6)
            self.column.addWidget(heading)
            for key, text in entries:
                button = QPushButton(icon(key), text)
                button.setObjectName("navItem")
                button.setCheckable(True)
                button.setIconSize(QSize(16, 16))
                button.clicked.connect(
                    lambda checked=False, section=key: self.selected.emit(section)
                )
                self.buttons[key] = button
                self.column.addWidget(button)
            self.column.addSpacing(10)
        self.profiles = QToolButton()
        self.profiles.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.profiles.setIcon(icon("measurements"))
        self.profiles.setText("Profiles  ▾")
        self.profiles.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.profiles.setAccessibleName("Browse captured profiles")
        self.column.addWidget(self.profiles, alignment=Qt.AlignmentFlag.AlignLeft)
        self.column.addStretch()

    def select(self, section):
        for key, button in self.buttons.items():
            button.setChecked(key == section)


class Welcome(QWidget):
    newRequested = Signal()
    openRequested = Signal()
    recentRequested = Signal(str)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.addStretch(2)
        content = QWidget()
        content.setMaximumWidth(650)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(icon("brand").pixmap(46, 46))
        layout.addWidget(mark)
        layout.addSpacing(6)
        heading = label("Investigation workspace")
        font = heading.font()
        font.setPointSize(25)
        font.setWeight(QFont.Weight.DemiBold)
        heading.setFont(font)
        layout.addWidget(heading)
        detail = label("Compare measured revisions or open a saved report to explore its evidence.")
        detail.setStyleSheet("color: #b7b6b2")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addSpacing(5)
        actions = QHBoxLayout()
        for text, glyph, primary, signal in [
            ("New investigation", "plus", True, self.newRequested),
            ("Open investigation", "folder", False, self.openRequested),
        ]:
            button = QPushButton(icon(glyph, "#22211d" if primary else "#c9c8c4"), text)
            if primary:
                button.setObjectName("primary")
            button.clicked.connect(signal.emit)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.recent = QPushButton()
        self.recent.setObjectName("navItem")
        self.recent.setIcon(icon("report"))
        self.recent.clicked.connect(lambda: self.recentRequested.emit(self.recent.toolTip()))
        self.recent.hide()
        layout.addWidget(self.recent)
        outer.addWidget(content, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(3)


class TopBar(QWidget):
    def __init__(self, new, open_existing, back):
        super().__init__()
        self.setObjectName("topBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 12, 9)
        mark = QLabel()
        mark.setPixmap(icon("brand").pixmap(25, 25))
        row.addWidget(mark)
        row.addWidget(label("PerfScout", "product"))
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color: #494844; margin: 4px 8px")
        row.addWidget(divider)
        self.back = tool("Back to report (Alt+Left)", "back", back)
        row.addWidget(self.back)
        self.name = label("Workspace", "documentName")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.name, 1)
        self.open = QPushButton(icon("folder"), "Open")
        self.open.setToolTip("Open investigation (Ctrl+O)")
        self.open.clicked.connect(open_existing)
        row.addWidget(self.open)
        self.new = QPushButton(icon("plus", "#24231f"), "New investigation")
        self.new.setObjectName("primary")
        self.new.clicked.connect(new)
        row.addWidget(self.new)
        self.layout_button = QToolButton()
        self.layout_button.setIcon(icon("layout"))
        self.layout_button.setAccessibleName("Workspace layout")
        self.layout_button.setToolTip("Workspace layout")
        self.layout_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        row.addWidget(self.layout_button)
