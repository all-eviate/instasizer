"""백그라운드 작업: 썸네일 불러오기, 미리보기 렌더링, 일괄 변환."""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal
from PySide6.QtGui import QImage

from . import core


def pil_to_qimage(img: Image.Image) -> QImage:
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return qimg.copy()  # 파이썬 버퍼와 분리해 스레드 간에 안전하게 넘긴다


class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        self._fn()


def _pool(parent: QObject, threads: int) -> QThreadPool:
    pool = QThreadPool(parent)
    pool.setMaxThreadCount(threads)
    return pool


def shutdown_pool(pool: QThreadPool, timeout_ms: int = 3000) -> None:
    pool.clear()
    pool.waitForDone(timeout_ms)


# ------------------------------------------------------------------------------
class ThumbnailLoader(QObject):
    """원본 목록용 썸네일을 백그라운드에서 만든다."""

    loaded = Signal(str, QImage)
    failed = Signal(str, str)

    def __init__(self, side: int = 192, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.side = side
        self.pool = _pool(self, max(2, min(4, os.cpu_count() or 2)))

    def request(self, path: str) -> None:
        self.pool.start(_Task(lambda: self._load(path)))

    def _load(self, path: str) -> None:
        try:
            loaded = core.load_image(path, max_side=self.side)
            self.loaded.emit(path, pil_to_qimage(loaded.image))
        except Exception as e:  # 손상된 파일 등: 목록에서 표시만 바꾼다
            self.failed.emit(path, str(e))


# ------------------------------------------------------------------------------
class PreviewRenderer(QObject):
    """선택한 사진에 현재 옵션을 적용한 미리보기를 만든다.
    요청마다 번호를 붙여, 늦게 끝난 오래된 결과는 버린다."""

    ready = Signal(int, QImage, str)  # 요청 번호, 미리보기, 정보 문구
    failed = Signal(int, str)

    CACHE_SIZE = 24

    def __init__(self, side: int = 1024, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.side = side
        self.pool = _pool(self, 1)
        self._token = 0
        self._cache: "OrderedDict[str, core.Loaded]" = OrderedDict()
        self._bg_cache: Optional[tuple] = None  # (경로, 수정 시각, 이미지)
        self._lock = threading.Lock()

    def request(self, path: str, opts: core.Options) -> int:
        self._token += 1
        token = self._token
        self.pool.clear()  # 아직 시작하지 않은 이전 요청은 취소
        self.pool.start(_Task(lambda: self._render(token, path, opts)))
        return token

    def _source(self, path: str) -> core.Loaded:
        with self._lock:
            if path in self._cache:
                self._cache.move_to_end(path)
                return self._cache[path]
        loaded = core.load_image(path, max_side=self.side)
        with self._lock:
            self._cache[path] = loaded
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        return loaded

    def _background(self, path: str) -> Image.Image:
        mtime = os.path.getmtime(path)
        cached = self._bg_cache
        if cached and cached[0] == path and cached[1] == mtime:
            return cached[2]
        img = core.load_image(path, max_side=self.side).image
        self._bg_cache = (path, mtime, img)
        return img

    def _render(self, token: int, path: str, opts: core.Options) -> None:
        if token != self._token:
            return
        try:
            loaded = self._source(path)
            note = ""
            bg = None
            if opts.square and opts.bg_mode == "image":
                if opts.validate():
                    opts = replace(opts, bg_mode="color", bg_color=(128, 128, 128))
                    note = " · 배경 이미지를 선택하세요"
                else:
                    bg = self._background(opts.bg_image)
            img = core.render_preview(loaded.image, core.Processor(opts, bg_source=bg))
            if token != self._token:
                return
            self.ready.emit(token, pil_to_qimage(img), describe_sizes(loaded.original_size, opts) + note)
        except Exception as e:
            self.failed.emit(token, str(e))


def describe_sizes(original: core.Size, opts: core.Options) -> str:
    sizes = core.plan_sizes(original, opts)
    fmt = lambda s: f"{s[0]}×{s[1]}"  # noqa: E731
    unique = sorted(set(sizes), key=sizes.index)
    out = ", ".join(fmt(s) for s in unique)
    return f"원본 {fmt(original)} → {len(sizes)}장 · {out}"


# ------------------------------------------------------------------------------
class ConvertJob(QThread):
    """여러 장을 병렬로 변환한다. 결과는 원본 순서대로 알린다."""

    progress = Signal(int, int, str)       # 끝난 수, 전체 수, 방금 끝난 파일명
    file_done = Signal(int, str, list)     # 순번, 원본 경로, [(결과 경로, 썸네일 QImage, (w, h))]
    file_failed = Signal(int, str, str)    # 순번, 원본 경로, 오류

    def __init__(self, sources: List[str], opts: core.Options, out_dir: Path, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.sources = list(sources)
        self.opts = opts
        self.out_dir = Path(out_dir)
        self._cancel = threading.Event()
        self.fatal: Optional[str] = None
        self.ok = 0
        self.failed = 0
        self.outputs = 0

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        try:
            processor = core.Processor(self.opts)
        except Exception as e:
            self.fatal = f"배경 이미지를 불러오지 못했습니다.\n{e}"
            return
        namer = core.OutputNamer(self.out_dir)

        def work(src: str):
            if self._cancel.is_set():
                return None
            try:
                return core.process_file(src, processor, namer), None
            except Exception as e:
                return None, e

        # 원본이 크면 한 장에 수백 MB를 쓰므로 동시에 너무 많이 돌리지 않는다
        workers = max(1, min(3, (os.cpu_count() or 2) - 1))
        total = len(self.sources)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, (src, result) in enumerate(zip(self.sources, pool.map(work, self.sources))):
                if result is None:  # 취소됨
                    continue
                outputs, error = result
                if error is not None:
                    self.failed += 1
                    self.file_failed.emit(index, src, str(error))
                else:
                    self.ok += 1
                    self.outputs += len(outputs)
                    self.file_done.emit(index, src, [(str(o.path), pil_to_qimage(o.thumb), o.size) for o in outputs])
                self.progress.emit(self.ok + self.failed, total, Path(src).name)
