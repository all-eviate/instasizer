"""메인 창: ① 원본 → ② 옵션·미리보기 → ③ 결과를 한 화면에서."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from . import APP_NAME, core
from .widgets import DropZone, PreviewView, ResultList, SourceList, SwatchBar, WheelGuard, local_paths
from .workers import ConvertJob, PreviewRenderer, ThumbnailLoader, pil_to_qimage, shutdown_pool

IMAGE_FILTER = "이미지 (" + " ".join(f"*{e}" for e in core.IMAGE_EXTS) + ");;모든 파일 (*)"

BG_DESCRIPTIONS = {
    "black": "검정 여백으로 채웁니다",
    "white": "흰색 여백으로 채웁니다",
    "blur": "사진을 흐리게 깔아 여백을 채웁니다",
}

STYLE = """
QFrame#panel { background: palette(base); border: 1px solid palette(mid); border-radius: 10px; }
QLabel#panelTitle { font-size: 11pt; font-weight: 600; }
QLabel#hint { color: gray; }
QPushButton#dropZone {
    border: 2px dashed palette(highlight); border-radius: 10px; padding: 12px;
    background: transparent; color: palette(highlight); font-weight: 600;
}
QPushButton#dropZone:hover, QPushButton#dropZone[active="true"] { background: rgba(59, 130, 246, 40); }
QPushButton#primary {
    background: palette(highlight); color: palette(highlighted-text); border: none;
    border-radius: 8px; padding: 10px; font-size: 10.5pt; font-weight: 600;
}
QPushButton#primary:disabled { background: palette(mid); color: palette(window); }
QToolButton#swatch { border: 2px solid transparent; border-radius: 8px; padding: 2px; }
QToolButton#swatch:checked { border: 2px solid palette(highlight); }
QToolButton#swatch:focus:!checked { border: 2px dotted palette(mid); }
QListWidget { border: none; background: transparent; }
"""


class MainWindow(QMainWindow):
    def __init__(self, session: core.Session):
        super().__init__()
        self.session = session
        self.settings = QSettings()
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.resize(1200, 780)

        opts = self._load_options()
        self._bg_mode = opts.bg_mode
        self._bg_color = opts.bg_color
        self._bg_image = opts.bg_image
        self._job: Optional[ConvertJob] = None
        self._failures: List[str] = []
        self._preview_token = 0
        self._preview_path: Optional[str] = None

        self.thumbs = ThumbnailLoader(parent=self)
        self.preview = PreviewRenderer(parent=self)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)  # 옵션을 연달아 바꿀 때 마지막 것만 그린다
        self._preview_timer.timeout.connect(self._update_preview)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._build_sources_panel())
        self.splitter.addWidget(self._build_options_panel())
        self.splitter.addWidget(self._build_results_panel())
        self.splitter.setChildrenCollapsible(False)
        for index, stretch in enumerate((1, 0, 1)):
            self.splitter.setStretchFactor(index, stretch)
        self.splitter.setSizes([380, 340, 460])

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(10, 10, 10, 4)
        outer.addWidget(self.splitter)
        self.setCentralWidget(container)
        self._build_status_bar()

        self.thumbs.loaded.connect(self.sources.set_thumbnail)
        self.thumbs.failed.connect(self.sources.mark_broken)
        self.preview.ready.connect(self._on_preview_ready)
        self.preview.failed.connect(self._on_preview_failed)

        self._apply_options(opts)
        self._connect_option_signals()
        self._install_shortcuts()
        self._restore_window_state()
        self._update_counts()

    # ------------------------------------------------------------------ 화면 구성
    def _panel(self, title: str):
        frame = QFrame()
        frame.setObjectName("panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("panelTitle")
        count = QLabel("")
        count.setObjectName("hint")
        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(count)
        layout.addLayout(header)
        return frame, layout, count

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        label.setWordWrap(True)
        return label

    def _build_sources_panel(self) -> QWidget:
        panel, layout, self.source_count = self._panel("① 원본")
        self.drop_zone = DropZone()
        self.drop_zone.clicked.connect(self.open_files)
        layout.addWidget(self.drop_zone)

        self.sources = SourceList()
        # 드롭 이벤트 안에서 폴더를 훑으면 드래그를 시작한 탐색기가 멈추므로 이벤트가 끝난 뒤 처리
        self.sources.filesDropped.connect(lambda paths: QTimer.singleShot(0, lambda: self.add_sources(paths)))
        self.sources.changed.connect(self._on_sources_changed)
        self.sources.itemSelectionChanged.connect(self._on_source_selection)
        layout.addWidget(self.sources, 1)
        layout.addWidget(self._hint("끌어서 순서 변경 · Delete 키로 제거\n일부만 선택하면 선택한 사진만 변환합니다"))

        row = QHBoxLayout()
        add_folder = QPushButton("폴더 추가…")
        add_folder.clicked.connect(lambda: self.open_folder())
        remove = QPushButton("선택 제거")
        remove.clicked.connect(lambda: self.sources.remove_selected())
        clear = QPushButton("모두 비우기")
        clear.clicked.connect(lambda: self.sources.clear_all())
        for btn in (add_folder, remove, clear):
            row.addWidget(btn)
        layout.addLayout(row)
        return panel

    def _build_options_panel(self) -> QWidget:
        panel, layout, _ = self._panel("② 옵션 · 미리보기")
        panel.setMinimumWidth(330)

        task = QGroupBox("작업")
        task_layout = QVBoxLayout(task)
        split_row = QHBoxLayout()
        self.split_check = QCheckBox("하프프레임 분할")
        self.split_check.setToolTip("가로 스캔은 좌/우, 세로 스캔은 위/아래 두 컷으로 나눕니다.")
        self.split_ratio = QDoubleSpinBox()
        self.split_ratio.setRange(0.30, 1.00)
        self.split_ratio.setSingleStep(0.01)
        self.split_ratio.setDecimals(2)
        self.split_ratio.setPrefix("컷 비율 ")
        self.split_ratio.setToolTip("한 컷의 가로÷세로 비율 (하프프레임 스캔은 보통 0.71 전후)")
        self.split_ratio.setAccessibleName("분할 컷 비율")
        split_row.addWidget(self.split_check)
        split_row.addStretch(1)
        split_row.addWidget(self.split_ratio)
        task_layout.addLayout(split_row)
        self.rotate_check = QCheckBox("세로 사진은 가로로 눕히기 (반시계 90°)")
        self.square_check = QCheckBox("정방형으로 채우기")
        task_layout.addWidget(self.rotate_check)
        task_layout.addWidget(self.square_check)
        layout.addWidget(task)

        self.bg_group = QGroupBox("정방형 배경")
        bg_layout = QVBoxLayout(self.bg_group)
        self.swatches = SwatchBar()
        self.swatches.clicked.connect(self._on_swatch_clicked)
        bg_layout.addWidget(self.swatches)
        detail = QHBoxLayout()
        self.bg_detail = self._hint("")
        self.bg_change = QPushButton("변경…")
        self.bg_change.clicked.connect(lambda: self._on_swatch_clicked(self._bg_mode, force_dialog=True))
        detail.addWidget(self.bg_detail, 1)
        detail.addWidget(self.bg_change)
        bg_layout.addLayout(detail)
        layout.addWidget(self.bg_group)

        output = QGroupBox("출력")
        form = QFormLayout(output)
        size_row = QHBoxLayout()
        self.size_mode = QComboBox()
        for label, value in (("원본 크기", "original"), ("긴 변 기준", "long"), ("짧은 변 기준", "short")):
            self.size_mode.addItem(label, value)
        self.size_mode.setToolTip("정방형으로 채우면 한 변의 길이가 이 값이 됩니다.")
        self.size_px = QSpinBox()
        self.size_px.setRange(64, 20000)
        self.size_px.setSingleStep(10)
        self.size_px.setSuffix(" px")
        self.size_px.setAccessibleName("출력 크기 픽셀")
        size_row.addWidget(self.size_mode, 1)
        size_row.addWidget(self.size_px)
        form.addRow("크기", size_row)
        self.quality = QSpinBox()
        self.quality.setRange(50, 100)
        form.addRow("JPEG 품질", self.quality)
        self.exif_check = QCheckBox("촬영 정보(EXIF) 유지")
        self.exif_check.setToolTip("카메라·날짜 정보를 남깁니다. 위치(GPS)가 들어 있을 수 있으니 공유용이면 끄는 것을 권장합니다.")
        form.addRow(self.exif_check)
        layout.addWidget(output)
        self._wheel_guard = WheelGuard(self)
        self._wheel_guard.watch(self.split_ratio, self.size_mode, self.size_px, self.quality)

        self.preview_view = PreviewView()
        layout.addWidget(self.preview_view, 1)
        self.preview_info = self._hint(" ")
        self.preview_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview_info)

        self.convert_btn = QPushButton("변환 시작")
        self.convert_btn.setObjectName("primary")
        self.convert_btn.setMinimumHeight(42)
        self.convert_btn.setToolTip("Ctrl+Enter")
        self.convert_btn.clicked.connect(lambda: self.on_convert())
        layout.addWidget(self.convert_btn)
        return panel

    def _build_results_panel(self) -> QWidget:
        panel, layout, self.result_count = self._panel("③ 결과")
        layout.addWidget(self._hint("썸네일을 메신저·탐색기로 끌어다 놓으면 바로 보내기·저장됩니다.\n"
                                    "선택 후 Ctrl+C → 메신저에서 Ctrl+V 도 됩니다."))
        self.results = ResultList()
        self.results.copied.connect(lambda n: self.statusBar().showMessage(
            f"{n}장을 클립보드에 복사했습니다. 메신저에서 Ctrl+V로 붙여넣으세요.", 6000))
        self.results.saveRequested.connect(self.save_results)
        self.results.itemSelectionChanged.connect(self._update_counts)
        layout.addWidget(self.results, 1)

        row = QHBoxLayout()
        self.save_selected_btn = QPushButton("선택 저장…")
        self.save_selected_btn.setToolTip("Ctrl+S")
        self.save_selected_btn.clicked.connect(lambda: self.save_results(self.results.selected_paths()))
        self.save_all_btn = QPushButton("전체 저장…")
        self.save_all_btn.clicked.connect(lambda: self.save_results(self.results.paths()))
        self.clear_results_btn = QPushButton("비우기")
        self.clear_results_btn.clicked.connect(self._clear_results)
        for btn in (self.save_selected_btn, self.save_all_btn, self.clear_results_btn):
            row.addWidget(btn)
        layout.addLayout(row)
        return panel

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setFormat("%v / %m")
        self.progress.hide()
        bar.addPermanentWidget(self.progress)
        self.top_check = QCheckBox("항상 위에 표시")
        self.top_check.setToolTip("메신저로 끌어다 놓을 때 창이 가려지지 않도록")
        self.top_check.toggled.connect(self._set_stay_on_top)
        bar.addPermanentWidget(self.top_check)

    def _install_shortcuts(self) -> None:
        for keys, slot in ((QKeySequence.StandardKey.Open, self.open_files),
                           (QKeySequence("Ctrl+Return"), self.on_convert),
                           (QKeySequence("Ctrl+Enter"), self.on_convert),
                           (QKeySequence.StandardKey.Save, self._save_selected_or_all)):
            QShortcut(QKeySequence(keys), self).activated.connect(slot)

    # ------------------------------------------------------------------ 옵션
    def _apply_options(self, opts: core.Options) -> None:
        self.split_check.setChecked(opts.split)
        self.split_ratio.setValue(opts.split_ratio)
        self.rotate_check.setChecked(opts.rotate_portrait)
        self.square_check.setChecked(opts.square)
        self.size_mode.setCurrentIndex(max(0, self.size_mode.findData(opts.size_mode)))
        self.size_px.setValue(opts.size_px)
        self.quality.setValue(opts.quality)
        self.exif_check.setChecked(opts.keep_exif)
        self.swatches.set_color(self._bg_color)
        self._refresh_bg_image_icon()
        self._sync_option_widgets()

    def _connect_option_signals(self) -> None:
        for check in (self.split_check, self.rotate_check, self.square_check, self.exif_check):
            check.toggled.connect(self._on_options_changed)
        for spin in (self.split_ratio, self.size_px, self.quality):
            spin.valueChanged.connect(self._on_options_changed)
        self.size_mode.currentIndexChanged.connect(self._on_options_changed)

    def current_options(self) -> core.Options:
        return core.Options(
            split=self.split_check.isChecked(),
            split_ratio=round(self.split_ratio.value(), 2),
            rotate_portrait=self.rotate_check.isChecked(),
            square=self.square_check.isChecked(),
            bg_mode=self._bg_mode,
            bg_color=self._bg_color,
            bg_image=self._bg_image,
            size_mode=self.size_mode.currentData(),
            size_px=self.size_px.value(),
            quality=self.quality.value(),
            keep_exif=self.exif_check.isChecked(),
        )

    def _on_options_changed(self, *_):
        self._sync_option_widgets()
        self._schedule_preview()

    def _sync_option_widgets(self) -> None:
        self.split_ratio.setEnabled(self.split_check.isChecked())
        self.bg_group.setEnabled(self.square_check.isChecked())
        self.size_px.setEnabled(self.size_mode.currentData() != "original")
        self.swatches.set_mode(self._bg_mode)

        mode = self._bg_mode
        if mode == "color":
            r, g, b = self._bg_color
            self.bg_detail.setText(f"사용자 색상 #{r:02X}{g:02X}{b:02X}")
        elif mode == "image":
            valid = Path(self._bg_image or "").is_file()
            self.bg_detail.setText(Path(self._bg_image).name if valid else "배경으로 쓸 이미지를 선택하세요")
        else:
            self.bg_detail.setText(BG_DESCRIPTIONS[mode])
        self.bg_change.setVisible(mode in ("color", "image"))

    def _on_swatch_clicked(self, mode: str, force_dialog: bool = False) -> None:
        previous = self._bg_mode
        if mode == "color":
            color = QColorDialog.getColor(QColor(*self._bg_color), self, "배경 색상 선택")
            if not color.isValid():  # 취소하면 이전 모드 유지
                self.swatches.set_mode(previous)
                return
            self._bg_color = (color.red(), color.green(), color.blue())
            self.swatches.set_color(self._bg_color)
        elif mode == "image":
            needs_pick = force_dialog or previous == "image" or not Path(self._bg_image or "").is_file()
            if needs_pick and not self._pick_bg_image():
                self.swatches.set_mode(previous)
                return
        self._bg_mode = mode
        self._on_options_changed()

    def _pick_bg_image(self) -> bool:
        start = str(Path(self._bg_image).parent) if self._bg_image else self._last_dir("last_open_dir")
        path, _ = QFileDialog.getOpenFileName(self, "배경으로 쓸 이미지 선택", start, IMAGE_FILTER)
        if not path:
            return False
        try:
            core.load_image(path, max_side=96)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"이미지를 열 수 없습니다.\n{e}")
            return False
        self._bg_image = path
        self._refresh_bg_image_icon()
        return True

    def _refresh_bg_image_icon(self) -> None:
        image = None
        if Path(self._bg_image or "").is_file():
            try:
                image = pil_to_qimage(core.load_image(self._bg_image, max_side=96).image)
            except Exception:
                image = None
        self.swatches.set_image(image)

    # ------------------------------------------------------------------ 원본
    def _last_dir(self, key: str) -> str:
        value = self.settings.value(key, "")
        if value and Path(value).is_dir():
            return str(value)
        pictures = Path.home() / "Pictures"
        return str(pictures if pictures.is_dir() else Path.home())

    def open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "변환할 사진 선택", self._last_dir("last_open_dir"), IMAGE_FILTER)
        if paths:
            self.settings.setValue("last_open_dir", str(Path(paths[0]).parent))
            self.add_sources(paths)

    def open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "사진 폴더 선택", self._last_dir("last_open_dir"))
        if folder:
            self.settings.setValue("last_open_dir", folder)
            self.add_sources([folder])

    def add_sources(self, paths: Sequence) -> List[str]:
        paths = list(paths)
        if not paths:
            return []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            files = core.collect_images(paths)
        finally:
            QApplication.restoreOverrideCursor()
        added = self.sources.add_paths(files)
        for path in added:
            self.thumbs.request(path)

        if not files:
            message = "지원하는 이미지가 없습니다 (JPG · PNG · HEIC · TIFF 등)"
        else:
            message = f"{len(added)}장을 추가했습니다"
            if len(files) > len(added):
                message += f" · 이미 있는 {len(files) - len(added)}장 제외"
        self.statusBar().showMessage(message, 5000)
        return added

    def _on_sources_changed(self) -> None:
        self._update_counts()
        self._schedule_preview()

    def _on_source_selection(self) -> None:
        self._update_counts()
        self._schedule_preview()

    # ------------------------------------------------------------------ 미리보기
    def _preview_target(self) -> Optional[str]:
        current = self.sources.currentItem()
        if current is not None and current.isSelected():
            return self.sources.item_path(current)
        selected = self.sources.selected_paths()
        if selected:
            return selected[0]
        return self.sources.item_path(self.sources.item(0)) if self.sources.count() else None

    def _schedule_preview(self) -> None:
        self._preview_timer.start()

    def _update_preview(self) -> None:
        path = self._preview_target()
        if path is None:
            self._preview_token = 0
            self.preview_view.set_message("사진을 추가하면 미리보기가 나타납니다")
            self.preview_info.setText(" ")
            return
        self._preview_path = path
        self._preview_token = self.preview.request(path, self.current_options())

    def _on_preview_ready(self, token: int, image, info: str) -> None:
        if token != self._preview_token:
            return
        self.preview_view.set_image(image)
        self.preview_info.setText(f"{Path(self._preview_path).name}\n{info}")

    def _on_preview_failed(self, token: int, error: str) -> None:
        if token != self._preview_token:
            return
        self.preview_view.set_message(f"미리보기를 만들 수 없습니다\n{error}")
        self.preview_info.setText(Path(self._preview_path).name)

    # ------------------------------------------------------------------ 변환
    def on_convert(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self.convert_btn.setEnabled(False)
            self.convert_btn.setText("취소하는 중…")
            return

        all_paths = self.sources.paths()
        selected = self.sources.selected_paths()
        sources = selected if 0 < len(selected) < len(all_paths) else all_paths
        if not sources:
            self.statusBar().showMessage("변환할 사진을 먼저 추가하세요.", 4000)
            return
        opts = self.current_options()
        error = opts.validate()
        if error:
            QMessageBox.information(self, APP_NAME, error)
            return

        self.results.clear()
        self._failures = []
        job = ConvertJob(sources, opts, self.session.new_run_dir(), self)
        job.file_done.connect(self._on_file_done)
        job.file_failed.connect(self._on_file_failed)
        job.progress.connect(self._on_progress)
        job.finished.connect(self._on_job_finished)
        self._job = job

        self.progress.setRange(0, len(sources))
        self.progress.setValue(0)
        self.progress.show()
        self.convert_btn.setText("취소")
        self.statusBar().showMessage(f"{len(sources)}장 변환 중…")
        self._update_counts()
        job.start()

    def _on_file_done(self, index: int, source: str, outputs: list) -> None:
        for path, thumb, size in outputs:
            self.results.add_result(path, thumb, tuple(size))
        self._update_counts()

    def _on_file_failed(self, index: int, source: str, error: str) -> None:
        self._failures.append(f"{Path(source).name}: {error}")

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setValue(done)
        self.statusBar().showMessage(f"변환 중 {done}/{total} · {name}")

    def _on_job_finished(self) -> None:
        job, self._job = self._job, None
        if job is None:
            return
        self.progress.hide()
        self.convert_btn.setEnabled(True)
        self._update_counts()

        if job.fatal:
            self.statusBar().showMessage("변환하지 못했습니다.", 6000)
            QMessageBox.warning(self, APP_NAME, job.fatal)
        else:
            message = f"{job.ok}장 변환 → 결과 {job.outputs}장"
            if job.cancelled:
                message = "변환을 취소했습니다 · " + message
            if job.failed:
                message += f" · 실패 {job.failed}장"
            self.statusBar().showMessage(message)
        if self._failures:
            shown = "\n".join(self._failures[:15]) + ("\n…" if len(self._failures) > 15 else "")
            QMessageBox.warning(self, "변환 실패", f"다음 파일을 변환하지 못했습니다.\n\n{shown}")
        job.deleteLater()

    def _update_counts(self) -> None:
        total = self.sources.count()
        selected = len(self.sources.selectedItems())
        self.source_count.setText(f"{total}장" if total else "")
        results = self.results.count()
        self.result_count.setText(f"{results}장" if results else "")

        if self._job is None:
            if 0 < selected < total:
                self.convert_btn.setText(f"선택한 {selected}장 변환")
            else:
                self.convert_btn.setText(f"변환 시작 ({total}장)" if total else "변환 시작")
            self.convert_btn.setEnabled(total > 0)
        self.save_selected_btn.setEnabled(bool(self.results.selectedItems()))
        self.save_all_btn.setEnabled(results > 0)
        self.clear_results_btn.setEnabled(results > 0 and self._job is None)

    def _clear_results(self) -> None:
        # 파일은 메신저가 아직 읽고 있을 수 있어 바로 지우지 않는다 (다음 실행 때 정리)
        self.results.clear()
        self._update_counts()

    # ------------------------------------------------------------------ 저장
    def _save_selected_or_all(self) -> None:
        self.save_results(self.results.selected_paths() or self.results.paths())

    def save_results(self, paths: Sequence) -> None:
        paths = [Path(p) for p in paths]
        if not paths:
            self.statusBar().showMessage("저장할 결과가 없습니다.", 4000)
            return
        folder = QFileDialog.getExistingDirectory(self, f"결과 {len(paths)}장을 저장할 폴더",
                                                  self._last_dir("last_save_dir"))
        if not folder:
            return
        dest = Path(folder)
        self.settings.setValue("last_save_dir", str(dest))

        conflicts = [p for p in paths if (dest / p.name).exists()]
        overwrite = False
        if conflicts:
            box = QMessageBox(QMessageBox.Icon.Question, "같은 이름의 파일",
                              f"저장할 폴더에 같은 이름의 파일이 {len(conflicts)}개 있습니다.", parent=self)
            keep_both = box.addButton("번호 붙여 저장", QMessageBox.ButtonRole.AcceptRole)
            replace_btn = box.addButton("덮어쓰기", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep_both)
            box.exec()
            if box.clickedButton() is replace_btn:
                overwrite = True
            elif box.clickedButton() is not keep_both:
                return

        namer = core.OutputNamer(dest)
        saved, errors = 0, []
        for src in paths:
            target = dest / src.name
            if target.exists() and not overwrite:
                target = namer.reserve(src.stem, src.suffix)
            try:
                shutil.copy2(src, target)
                saved += 1
            except OSError as e:
                errors.append(f"{src.name}: {e}")

        self.statusBar().showMessage(f"{saved}장을 {dest}에 저장했습니다.", 8000)
        if errors:
            QMessageBox.warning(self, "저장 실패", "다음 파일을 저장하지 못했습니다.\n\n" + "\n".join(errors[:15]))
        if saved:
            os.startfile(str(dest))

    # ------------------------------------------------------------------ 창 상태
    def _set_stay_on_top(self, on: bool) -> None:
        handle = self.windowHandle()
        if handle is not None and self.isVisible():
            handle.setFlag(Qt.WindowType.WindowStaysOnTopHint, on)  # 창을 숨기거나 재생성하지 않고 즉시 반영
            return
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if was_visible:
            self.show()

    def _load_options(self) -> core.Options:
        raw = self.settings.value("options", "")
        try:
            return core.Options.from_dict(json.loads(raw)) if raw else core.Options()
        except (TypeError, ValueError):
            return core.Options()

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter = self.settings.value("splitter")
        if splitter:
            self.splitter.restoreState(splitter)
        self.top_check.setChecked(self.settings.value("stay_on_top", False, type=bool))

    def _save_settings(self) -> None:
        self.settings.setValue("options", json.dumps(self.current_options().to_dict()))
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.setValue("stay_on_top", self.top_check.isChecked())

    # ------------------------------------------------------------------ 창 전체 드롭
    def dragEnterEvent(self, event):
        if event.source() is None and event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone.set_active(True)

    def dragLeaveEvent(self, event):
        self.drop_zone.set_active(False)

    def dropEvent(self, event):
        self.drop_zone.set_active(False)
        paths = local_paths(event.mimeData())
        event.acceptProposedAction()
        QTimer.singleShot(0, lambda: self.add_sources(paths))

    def closeEvent(self, event):
        if self._job is not None:
            self._job.cancel()
            self._job.wait(20000)
        self._save_settings()
        shutdown_pool(self.thumbs.pool)
        shutdown_pool(self.preview.pool)
        super().closeEvent(event)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    QApplication.setOrganizationName("instasizer")
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(argv)
    app.setStyleSheet(STYLE)

    try:
        session = core.Session()
    except OSError as e:
        QMessageBox.critical(None, APP_NAME, f"임시 폴더를 만들 수 없습니다.\n{e}")
        return 1

    window = MainWindow(session)
    window.show()
    # exe 아이콘 위에 사진을 끌어다 놓거나 '보내기'로 실행한 경우
    initial = [a for a in argv[1:] if Path(a).exists()]
    if initial:
        QTimer.singleShot(0, lambda: window.add_sources(initial))
    try:
        return app.exec()
    finally:
        session.close()
