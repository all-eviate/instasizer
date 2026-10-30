"""이미지 처리 로직. UI와 무관하게 동작하며 여러 스레드에서 동시에 호출해도 안전하다."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import pillow_heif
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

pillow_heif.register_heif_opener()

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff")
BG_MODES = ("black", "white", "blur", "color", "image")
SIZE_MODES = ("original", "long", "short")

# 블러 배경: 작은 크기에서 흐린 뒤 확대한다. 반경을 작업 크기에 비례시켜
# 원본 해상도와 미리보기 해상도에서 같은 느낌이 나도록 한다.
BLUR_MAX_WORK_SIDE = 768
BLUR_RADIUS_RATIO = 0.02
BLUR_BRIGHTNESS = 0.4

Size = Tuple[int, int]


# ------------------------------------------------------------------------------
# 옵션
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Options:
    split: bool = False              # 하프프레임 분할 (가로 스캔 → 좌/우, 세로 스캔 → 위/아래)
    split_ratio: float = 0.71        # 한 컷의 가로/세로 비율
    rotate_portrait: bool = False    # 세로 사진을 반시계 90° 회전해 가로로
    square: bool = True              # 정방형으로 채우기
    bg_mode: str = "blur"            # BG_MODES 중 하나
    bg_color: Tuple[int, int, int] = (40, 40, 40)
    bg_image: str = ""
    size_mode: str = "original"      # SIZE_MODES 중 하나
    size_px: int = 1440
    quality: int = 95
    keep_exif: bool = False          # 촬영 정보 유지 (GPS가 포함될 수 있어 기본은 끔)

    def validate(self) -> Optional[str]:
        """변환을 시작할 수 없는 설정이면 사용자에게 보여줄 메시지를 반환."""
        if self.square and self.bg_mode == "image" and not Path(self.bg_image or "").is_file():
            return "배경으로 쓸 이미지를 먼저 선택해 주세요."
        return None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["bg_color"] = list(self.bg_color)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Options":
        """저장된 설정에서 복원한다. 모르는 키나 잘못된 값은 기본값을 쓴다."""
        default = cls()
        values = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            base, raw = getattr(default, f.name), data[f.name]
            try:
                if isinstance(base, bool):
                    value = bool(raw)
                elif isinstance(base, int):
                    value = int(raw)
                elif isinstance(base, float):
                    value = float(raw)
                elif isinstance(base, tuple):
                    value = tuple(max(0, min(255, int(v))) for v in raw)
                    if len(value) != 3:
                        continue
                else:
                    value = str(raw)
            except (TypeError, ValueError):
                continue
            values[f.name] = value

        opts = replace(default, **values)
        return replace(
            opts,
            bg_mode=opts.bg_mode if opts.bg_mode in BG_MODES else default.bg_mode,
            size_mode=opts.size_mode if opts.size_mode in SIZE_MODES else default.size_mode,
            split_ratio=min(1.0, max(0.3, opts.split_ratio)),
            size_px=min(20000, max(64, opts.size_px)),
            quality=min(100, max(1, opts.quality)),
        )


# ------------------------------------------------------------------------------
# 불러오기 / 저장
# ------------------------------------------------------------------------------
@dataclass
class Loaded:
    image: Image.Image          # RGB, EXIF 방향 적용됨
    original_size: Size         # 방향 적용 후 원본 픽셀 크기 (축소해서 불렀어도 원본 기준)
    icc: Optional[bytes]        # RGB 색 프로파일일 때만
    exif: Optional[bytes]       # 방향 태그가 제거된 EXIF


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))  # 투명 영역은 흰색으로
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    if img.mode.startswith("I;16"):  # 16비트 흑백 스캔
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L").convert("RGB")
    return img.convert("RGB")


def load_image(path, max_side: Optional[int] = None) -> Loaded:
    """이미지를 RGB로 불러온다. max_side를 주면 긴 변이 그 이하가 되도록 축소해서 빠르게 불러온다."""
    with Image.open(path) as im:
        w, h = im.size
        if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):  # 90° 회전이 필요한 방향
            w, h = h, w
        if max_side and im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))  # JPEG는 디코딩 단계에서 축소
        icc = im.info.get("icc_profile")
        transposed = ImageOps.exif_transpose(im)  # 새 이미지를 반환하고 방향 태그를 제거한다
        exif = transposed.info.get("exif")
        image = _to_rgb(transposed)
        image.load()

    if max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if not (icc and icc[16:20] == b"RGB "):  # 흑백·CMYK 프로파일을 RGB 결과에 붙이면 색이 틀어진다
        icc = None
    return Loaded(image, (w, h), icc, exif or None)


def save_jpeg(img: Image.Image, path: Path, quality: int,
              icc: Optional[bytes] = None, exif: Optional[bytes] = None) -> None:
    kwargs = {"quality": quality}
    if icc:
        kwargs["icc_profile"] = icc
    if exif:
        kwargs["exif"] = exif
    img.save(path, "JPEG", **kwargs)


# ------------------------------------------------------------------------------
# 변환
# ------------------------------------------------------------------------------
def split_boxes(size: Size, ratio: float) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """하프프레임 스캔에서 두 컷의 영역. 스캔 오차를 피하려고 양 끝을 기준으로 자른다."""
    w, h = size
    if w >= h:
        cut = max(1, min(w, int(h * ratio)))
        return [("L", (0, 0, cut, h)), ("R", (w - cut, 0, w, h))]
    cut = max(1, min(h, int(w * ratio)))
    return [("T", (0, 0, w, cut)), ("B", (0, h - cut, w, h))]


def scale_factor(size: Size, opts: Options) -> float:
    """크기 옵션에 따른 배율. 정방형이면 결과 한 변(= 긴 변)을 기준으로 맞춘다."""
    if opts.size_mode == "original":
        return 1.0
    w, h = size
    ref = max(w, h) if (opts.square or opts.size_mode == "long") else min(w, h)
    return opts.size_px / ref if ref else 1.0


def _scaled(size: Size, factor: float) -> Size:
    return max(1, round(size[0] * factor)), max(1, round(size[1] * factor))


def plan_sizes(size: Size, opts: Options) -> List[Size]:
    """원본 크기에서 결과 이미지들의 크기를 계산한다 (미리보기 정보 표시용)."""
    if opts.split:
        pieces = [(b[2] - b[0], b[3] - b[1]) for _, b in split_boxes(size, opts.split_ratio)]
    else:
        pieces = [size]
    result = []
    for w, h in pieces:
        if opts.rotate_portrait and h > w:
            w, h = h, w
        w, h = _scaled((w, h), scale_factor((w, h), opts))
        if opts.square:
            w = h = max(w, h)
        result.append((w, h))
    return result


class Processor:
    """옵션 하나로 여러 장을 변환한다. 배경 이미지는 한 번만 불러와 재사용한다."""

    def __init__(self, opts: Options, bg_source: Optional[Image.Image] = None):
        """bg_source: 이미 불러온 배경 이미지 (미리보기에서 축소본을 재사용할 때)."""
        self.opts = opts
        self._bg_source: Optional[Image.Image] = None
        self._bg_lock = threading.Lock()
        if opts.square and opts.bg_mode == "image":
            self._bg_source = bg_source or load_image(opts.bg_image, max_side=4096).image

    def process(self, img: Image.Image, *, preview: bool = False) -> List[Tuple[str, Image.Image]]:
        """(태그, 결과 이미지) 목록을 반환. 태그는 분할 위치(L/R/T/B)이며 분할하지 않으면 빈 문자열.
        preview=True면 크기 옵션을 건너뛴다 (이미 축소된 이미지로 모양만 확인)."""
        o = self.opts
        if o.split:
            pieces = [(tag, img.crop(box)) for tag, box in split_boxes(img.size, o.split_ratio)]
        else:
            pieces = [("", img)]

        result = []
        for tag, piece in pieces:
            if o.rotate_portrait and piece.height > piece.width:
                piece = piece.transpose(Image.Transpose.ROTATE_90)  # 반시계 90°
            if not preview:
                factor = scale_factor(piece.size, o)
                if abs(factor - 1.0) > 1e-9:
                    piece = piece.resize(_scaled(piece.size, factor), Image.Resampling.LANCZOS)
            if o.square:
                piece = self._pad_square(piece)
            result.append((tag, piece))
        return result

    def _pad_square(self, img: Image.Image) -> Image.Image:
        w, h = img.size
        if w == h:
            return img
        side = max(w, h)
        canvas = self._background(img, side)
        canvas.paste(img, ((side - w) // 2, (side - h) // 2))
        return canvas

    def _background(self, img: Image.Image, side: int) -> Image.Image:
        mode = self.opts.bg_mode
        if mode == "black":
            return Image.new("RGB", (side, side), (0, 0, 0))
        if mode == "white":
            return Image.new("RGB", (side, side), (255, 255, 255))
        if mode == "color":
            return Image.new("RGB", (side, side), tuple(self.opts.bg_color))
        if mode == "image":
            with self._bg_lock:  # 공유하는 원본을 여러 스레드가 동시에 읽지 않도록
                return ImageOps.fit(self._bg_source, (side, side), Image.Resampling.LANCZOS)
        # blur
        work = max(8, min(BLUR_MAX_WORK_SIDE, side // 4))
        bg = img.resize((work, work), Image.Resampling.BOX)
        bg = bg.filter(ImageFilter.GaussianBlur(max(1.0, work * BLUR_RADIUS_RATIO)))
        bg = bg.resize((side, side), Image.Resampling.BICUBIC)
        return ImageEnhance.Brightness(bg).enhance(BLUR_BRIGHTNESS)


def output_stem(src_stem: str, tag: str, opts: Options) -> str:
    parts = [src_stem]
    if tag:
        parts.append(tag)
    if opts.square:
        parts.append("square")
    elif not tag and opts.size_mode != "original":
        parts.append("resized")
    elif not tag and opts.rotate_portrait:
        parts.append("rotated")
    return "_".join(parts)


class OutputNamer:
    """한 폴더 안에서 결과 파일명이 겹치지 않게 예약한다 (여러 스레드에서 호출)."""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self._taken = set()
        self._lock = threading.Lock()

    def reserve(self, stem: str, ext: str = ".jpg") -> Path:
        with self._lock:
            candidate, n = stem, 2
            while candidate.lower() in self._taken or (self.out_dir / f"{candidate}{ext}").exists():
                candidate, n = f"{stem}_{n}", n + 1
            self._taken.add(candidate.lower())
            return self.out_dir / f"{candidate}{ext}"


@dataclass
class OutputFile:
    path: Path
    size: Size
    thumb: Image.Image  # 결과 목록에 보여줄 작은 이미지


def process_file(src, processor: Processor, namer: OutputNamer, thumb_side: int = 256) -> List[OutputFile]:
    src = Path(src)
    opts = processor.opts
    loaded = load_image(src)
    outputs = []
    for tag, img in processor.process(loaded.image):
        path = namer.reserve(output_stem(src.stem, tag, opts))
        save_jpeg(img, path, opts.quality, loaded.icc, loaded.exif if opts.keep_exif else None)
        thumb = img.copy()
        thumb.thumbnail((thumb_side, thumb_side), Image.Resampling.BILINEAR)
        outputs.append(OutputFile(path, img.size, thumb))
    return outputs


def render_preview(img: Image.Image, processor: Processor, gap: int = 8) -> Image.Image:
    """축소된 원본으로 결과 모양을 만든다. 분할하면 두 컷을 나란히 붙인다."""
    pieces = [p for _, p in processor.process(img, preview=True)]
    if len(pieces) == 1:
        return pieces[0]
    height = max(p.height for p in pieces)
    width = sum(p.width for p in pieces) + gap * (len(pieces) - 1)
    sheet = Image.new("RGB", (width, height), (128, 128, 128))
    x = 0
    for p in pieces:
        sheet.paste(p, (x, (height - p.height) // 2))
        x += p.width + gap
    return sheet


# ------------------------------------------------------------------------------
# 파일 목록 / 임시 폴더
# ------------------------------------------------------------------------------
def natural_key(path) -> list:
    """'img2' < 'img10' 처럼 숫자를 숫자로 비교하는 정렬 키."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(path).lower())]


def collect_images(paths: Iterable) -> List[Path]:
    """파일·폴더 경로에서 지원하는 이미지만 모은다 (폴더는 하위 폴더까지)."""
    found, seen = [], set()
    for p in map(Path, paths):
        if p.is_dir():
            candidates = [f for f in p.rglob("*") if f.is_file()]
        elif p.is_file():
            candidates = [p]
        else:
            continue
        for f in candidates:
            key = os.path.normcase(str(f.resolve()))
            if f.suffix.lower() in IMAGE_EXTS and key not in seen:
                seen.add(key)
                found.append(f)
    return sorted(found, key=natural_key)


class Session:
    """결과 파일을 두는 임시 폴더.

    앱을 닫을 때 지우지 않고 다음 실행 때 정리한다. 메신저 중에는 드롭 후
    백그라운드로 파일을 읽어 업로드하는 경우가 있기 때문이다. 실행 중인 세션은
    열어 둔 .lock 파일 때문에 삭제되지 않으므로 여러 개를 동시에 실행해도 안전하다."""

    def __init__(self, root: Optional[Path] = None):
        # resolve(): %TEMP%가 8.3 짧은 이름으로 잡혀도 메신저에 긴 경로를 넘기도록
        self.root = (Path(root) if root else Path(tempfile.gettempdir()) / "INSTASIZER").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale()
        self.dir = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=self.root))
        self._lock_file = open(self.dir / ".lock", "w")
        self._runs = 0

    def _cleanup_stale(self) -> None:
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            try:
                (d / ".lock").unlink()  # 다른 인스턴스가 사용 중이면 실패한다
            except FileNotFoundError:
                pass
            except OSError:
                continue
            shutil.rmtree(d, ignore_errors=True)

    def new_run_dir(self) -> Path:
        """변환할 때마다 새 하위 폴더를 써서, 다시 변환해도 파일명이 깔끔하게 유지되도록 한다."""
        self._runs += 1
        run_dir = self.dir / f"run{self._runs:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def close(self) -> None:
        if not self._lock_file.closed:
            self._lock_file.close()
