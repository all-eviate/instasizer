"""메인 창에서 쓰는 위젯들."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (QByteArray, QEvent, QItemSelectionModel, QMimeData, QObject, QPoint, QRectF, QSize,
                            Qt, QUrl, Signal)
from PySide6.QtGui import (QColor, QDrag, QGuiApplication, QIcon, QImage, QKeySequence, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QListView, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QSizePolicy, QToolButton, QWidget)

PATH_ROLE = Qt.ItemDataRole.UserRole
SIZE_ROLE = Qt.ItemDataRole.UserRole + 1


def file_mime(paths: Sequence) -> QMimeData:
    """탐색기에서 파일을 끌 때와 같은 데이터. Windows에서는 CF_HDROP으로 전달된다."""
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return md


def local_paths(md: QMimeData) -> List[str]:
    return [u.toLocalFile() for u in md.urls() if u.isLocalFile()]


def placeholder_pixmap(side: int, text: str = "") -> QPixmap:
    pm = QPixmap(side, side)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(150, 150, 150, 60))
    p.drawRoundedRect(QRectF(2, 2, side - 4, side - 4), 8, 8)
    if text:
        p.setPen(QColor(120, 120, 120))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm


def fit_icon(image: QImage, side: int) -> QIcon:
    """정사각형 칸 가운데에 비율을 유지해 그린 아이콘 (목록에서 칸이 들쭉날쭉하지 않도록)."""
    pm = QPixmap(side, side)
    pm.fill(QColor(0, 0, 0, 0))
    scaled = QPixmap.fromImage(image).scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
    p = QPainter(pm)
    p.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
    p.end()
    return QIcon(pm)


def drag_pixmap(icon: QIcon, count: int) -> QPixmap:
    pm = icon.pixmap(72, 72)
    if count > 1:  # 여러 장을 끌 때 개수 표시
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(221, 42, 123))
        p.drawEllipse(QRectF(pm.width() - 28, 0, 28, 28))
        p.setPen(QColor(255, 255, 255))
        font = p.font()
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRectF(pm.width() - 28, 0, 28, 28), Qt.AlignmentFlag.AlignCenter, str(count))
        p.end()
    return pm


# ------------------------------------------------------------------------------
class DropZone(QPushButton):
    """클릭하면 파일 선택, 드래그 중에는 강조 표시 (실제 드롭은 창 전체가 받는다)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("사진·폴더를 여기에 드롭\n또는 클릭해서 선택 (JPG · PNG · HEIC · TIFF)", parent)
        self.setObjectName("dropZone")
        self.setMinimumHeight(80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("사진 추가")

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)


# ------------------------------------------------------------------------------
class _ThumbList(QListWidget):
    def __init__(self, icon_side: int, grid: QSize, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.icon_side = icon_side
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(QSize(icon_side, icon_side))
        self.setGridSize(grid)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setSpacing(4)

    def item_path(self, item: QListWidgetItem) -> str:
        return item.data(PATH_ROLE)

    def paths(self) -> List[str]:
        return [self.item(i).data(PATH_ROLE) for i in range(self.count())]

    def selected_paths(self) -> List[str]:
        # 화면 순서대로
        return [self.item(i).data(PATH_ROLE) for i in range(self.count()) if self.item(i).isSelected()]


class SourceList(_ThumbList):
    """원본 목록. 외부 파일 드롭으로 추가하고, 끌어서 순서를 바꾼다."""

    filesDropped = Signal(list)
    changed = Signal()  # 추가·삭제·순서 변경

    INTERNAL_MIME = "application/x-instasizer-source-rows"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(96, QSize(118, 128), parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(False)
        self.setAccessibleName("원본 사진 목록")
        self._items: Dict[str, QListWidgetItem] = {}
        self._placeholder = QIcon(placeholder_pixmap(96))
        self._broken = QIcon(placeholder_pixmap(96, "열 수 없음"))

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def add_paths(self, paths: Sequence) -> List[str]:
        """새로 추가된 경로만 반환 (이미 있는 사진은 건너뜀)."""
        added = []
        for p in map(str, paths):
            key = self._key(p)
            if key in self._items:
                continue
            item = QListWidgetItem(self._placeholder, Path(p).name)
            item.setData(PATH_ROLE, p)
            item.setToolTip(p)
            self.addItem(item)
            self._items[key] = item
            added.append(p)
        if added:
            self.changed.emit()
        return added

    def set_thumbnail(self, path: str, image: QImage) -> None:
        item = self._items.get(self._key(path))
        if item is not None:
            item.setIcon(fit_icon(image, self.icon_side))

    def mark_broken(self, path: str, reason: str) -> None:
        item = self._items.get(self._key(path))
        if item is not None:
            item.setIcon(self._broken)
            item.setToolTip(f"{path}\n열 수 없음: {reason}")

    def remove_selected(self) -> List[str]:
        removed = []
        for item in self.selectedItems():
            path = item.data(PATH_ROLE)
            self._items.pop(self._key(path), None)
            self.takeItem(self.row(item))
            removed.append(path)
        if removed:
            self.changed.emit()
        return removed

    def clear_all(self) -> None:
        self.clear()
        self._items.clear()
        self.changed.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected()
            return
        super().keyPressEvent(event)

    # --- 드래그 앤 드롭 ------------------------------------------------------
    def startDrag(self, supported_actions):
        items = self.selectedItems()
        if not items:
            return
        # 파일 URL을 넣지 않는다: 탐색기에 놓았을 때 원본이 이동·복사되는 사고를 막고, 목록 안 정렬에만 쓴다
        md = QMimeData()
        md.setData(self.INTERNAL_MIME, QByteArray(b"1"))
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(drag_pixmap(items[0].icon(), len(items)))
        drag.exec(Qt.DropAction.MoveAction)

    def _accepts(self, event) -> bool:
        md = event.mimeData()
        if event.source() is self and md.hasFormat(self.INTERNAL_MIME):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return True
        if event.source() is None and md.hasUrls():
            event.acceptProposedAction()
            return True
        event.ignore()
        return False

    def dragEnterEvent(self, event):
        self._accepts(event)

    def dragMoveEvent(self, event):
        self._accepts(event)

    def dropEvent(self, event):
        if not self._accepts(event):
            return
        if event.source() is self:
            self._move_selected_to(event.position().toPoint())
        else:
            self.filesDropped.emit(local_paths(event.mimeData()))

    def _move_selected_to(self, pos: QPoint) -> None:
        rows = sorted(self.row(i) for i in self.selectedItems())
        if not rows:
            return
        target = self.itemAt(pos)
        if target is None:
            insert_at = self.count()
        else:
            rect = self.visualItemRect(target)
            insert_at = self.row(target) + (1 if pos.x() > rect.center().x() else 0)
        taken = []
        for row in reversed(rows):
            if row < insert_at:
                insert_at -= 1
            taken.insert(0, self.takeItem(row))
        for offset, item in enumerate(taken):
            self.insertItem(insert_at + offset, item)
        # setCurrentItem 기본 동작은 다른 선택을 지우므로 NoUpdate로 옮긴 뒤 다시 선택
        self.setCurrentItem(taken[0], QItemSelectionModel.SelectionFlag.NoUpdate)
        for item in taken:
            item.setSelected(True)
        self.changed.emit()


class ResultList(_ThumbList):
    """결과 목록. 선택한 파일을 메신저·탐색기로 끌어다 놓거나 Ctrl+C로 복사한다."""

    copied = Signal(int)
    saveRequested = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(128, QSize(150, 162), parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setAccessibleName("변환 결과 목록")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.itemActivated.connect(lambda item: os.startfile(item.data(PATH_ROLE)))

    def keyPressEvent(self, event):
        # 기본 동작(항목 이름 텍스트 복사) 대신 파일 자체를 복사한다
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selected()
            return
        super().keyPressEvent(event)

    def add_result(self, path: str, thumb: QImage, size: Tuple[int, int]) -> None:
        item = QListWidgetItem(fit_icon(thumb, self.icon_side), Path(path).name)
        item.setData(PATH_ROLE, path)
        item.setData(SIZE_ROLE, size)
        item.setToolTip(f"{Path(path).name}\n{size[0]}×{size[1]}\n더블클릭: 열기 · 끌어다 놓기: 보내기/저장")
        self.addItem(item)

    def startDrag(self, supported_actions):
        paths = self.selected_paths()
        if not paths:
            return
        drag = QDrag(self)
        drag.setMimeData(file_mime(paths))
        drag.setPixmap(drag_pixmap(self.selectedItems()[0].icon(), len(paths)))
        # 복사만 허용: 탐색기에 놓아도 '이동'이 아니라 '복사'가 된다
        drag.exec(Qt.DropAction.CopyAction)

    def copy_selected(self) -> None:
        paths = self.selected_paths()
        if paths:
            QGuiApplication.clipboard().setMimeData(file_mime(paths))
            self.copied.emit(len(paths))

    def _context_menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
        paths = self.selected_paths()
        menu = QMenu(self)
        menu.addAction(f"복사 ({len(paths)}장)\tCtrl+C", self.copy_selected)
        menu.addAction("저장…", lambda: self.saveRequested.emit(paths))
        menu.addSeparator()
        menu.addAction("기본 앱으로 열기", lambda: os.startfile(item.data(PATH_ROLE)))
        menu.addAction("폴더에서 보기", lambda: subprocess.Popen(
            ["explorer", "/select,", os.path.normpath(item.data(PATH_ROLE))]))
        menu.exec(self.viewport().mapToGlobal(pos))


# ------------------------------------------------------------------------------
SWATCHES = (("black", "검정"), ("white", "흰색"), ("blur", "블러"), ("color", "색상"), ("image", "이미지"))


def swatch_icon(kind: str, side: int = 34, color: Optional[Tuple[int, int, int]] = None,
                image: Optional[QImage] = None) -> QIcon:
    pm = QPixmap(side, side)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(1, 1, side - 2, side - 2)
    shape = QPainterPath()
    shape.addRoundedRect(rect, 6, 6)
    p.setClipPath(shape)

    if kind == "black":
        p.fillRect(rect, QColor(0, 0, 0))
    elif kind == "white":
        p.fillRect(rect, QColor(255, 255, 255))
    elif kind == "blur":
        g = QLinearGradient(0, 0, side, side)
        g.setColorAt(0, QColor("#5d6b80"))
        g.setColorAt(1, QColor("#b89c7c"))
        p.fillRect(rect, g)
    elif kind == "color":
        if color:
            p.fillRect(rect, QColor(*color))
        else:
            g = QLinearGradient(0, 0, side, side)
            for stop, c in ((0, "#ff6b6b"), (0.35, "#ffd93d"), (0.65, "#6bcb77"), (1, "#4d96ff")):
                g.setColorAt(stop, QColor(c))
            p.fillRect(rect, g)
    elif kind == "image":
        if image is not None and not image.isNull():
            scaled = QPixmap.fromImage(image).scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                     Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
        else:
            p.fillRect(rect, QColor("#e5e7eb"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#9ca3af"))
            mountain = QPainterPath()
            mountain.moveTo(side * 0.15, side * 0.8)
            mountain.lineTo(side * 0.4, side * 0.45)
            mountain.lineTo(side * 0.58, side * 0.65)
            mountain.lineTo(side * 0.7, side * 0.52)
            mountain.lineTo(side * 0.88, side * 0.8)
            mountain.closeSubpath()
            p.drawPath(mountain)
            p.drawEllipse(QRectF(side * 0.66, side * 0.2, side * 0.14, side * 0.14))

    p.setClipping(False)
    p.setPen(QPen(QColor(128, 128, 128, 180), 1))  # 밝은/어두운 테마 모두에서 보이는 테두리
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(shape)
    p.end()
    return QIcon(pm)


class SwatchBar(QWidget):
    """배경 모드 선택 버튼 줄. 클릭한 모드를 알릴 뿐, 실제 모드 변경은 창이 결정한다
    (색상·이미지는 대화상자에서 취소하면 이전 모드로 돌아가야 하므로)."""

    clicked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QToolButton] = {}
        for mode, label in SWATCHES:
            btn = QToolButton(self)
            btn.setObjectName("swatch")
            btn.setCheckable(True)
            btn.setText(label)
            btn.setIcon(swatch_icon(mode))
            btn.setIconSize(QSize(34, 34))
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            btn.setAccessibleName(f"배경: {label}")
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _=False, m=mode: self.clicked.emit(m))
            self.group.addButton(btn)
            self.buttons[mode] = btn
            layout.addWidget(btn)
        self.buttons["color"].setToolTip("클릭해서 배경 색상 선택")
        self.buttons["image"].setToolTip("배경으로 쓸 이미지 선택 (선택된 상태에서 다시 클릭하면 변경)")

    def set_mode(self, mode: str) -> None:
        self.buttons[mode].setChecked(True)

    def set_color(self, color: Tuple[int, int, int]) -> None:
        self.buttons["color"].setIcon(swatch_icon("color", color=color))

    def set_image(self, image: Optional[QImage]) -> None:
        self.buttons["image"].setIcon(swatch_icon("image", image=image))


# ------------------------------------------------------------------------------
class WheelGuard(QObject):
    """포커스가 없는 스핀박스·콤보박스는 마우스 휠을 무시한다
    (창 위에서 스크롤하다가 옵션 값이 몰래 바뀌는 것을 막는다)."""

    def watch(self, *widgets: QWidget) -> None:
        for w in widgets:
            w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            w.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return False


# ------------------------------------------------------------------------------
class PreviewView(QWidget):
    """미리보기 이미지를 비율을 유지해 가운데에 그린다."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._message = "사진을 추가하면 미리보기가 나타납니다"
        self.setMinimumSize(160, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("미리보기")

    def set_image(self, image: Optional[QImage]) -> None:
        self._pixmap = QPixmap.fromImage(image) if image is not None else None
        self.update()

    def set_message(self, text: str) -> None:
        self._pixmap = None
        self._message = text
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        area = self.rect().adjusted(4, 4, -4, -4)
        if self._pixmap is None or self._pixmap.isNull():
            p.setPen(QColor(128, 128, 128))
            p.drawText(area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)
            return
        size = self._pixmap.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = area.x() + (area.width() - size.width()) // 2
        y = area.y() + (area.height() - size.height()) // 2
        p.drawPixmap(x, y, size.width(), size.height(), self._pixmap)
