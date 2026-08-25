#!/usr/bin/env python3
"""
Препроцессинг серий растров глубины из HEC-RAS RAS Mapper в компактные
ассеты для веб-карты.

Вход:   data/raw/<сценарий>/*.tif   — по одному файлу на временной шаг
Выход:  web/data/manifest.json + PNG-атласы кадров

Что делает:
  1. Находит общий контур затопления по всем кадрам (обрезка пустых полей).
  2. Перепроецирует в EPSG:3857 и прореживает до целевого разрешения.
  3. Квантует глубину в uint8 (0 = сухо, 1..255 = глубина).
  4. Пакует кадры в PNG-атласы, считает время добегания, макс. глубину
     и гидрограф площади затопления.

Запуск:
    python scripts/prepare.py --max-dim 1400 --max-frames 60
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "web" / "data"

# Порог, ниже которого пиксель считается сухим (м).
WET_THRESHOLD = 0.05
# Максимальная сторона одного PNG-атласа, px.
SHEET_MAX = 4096

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ─────────────────────────────────────────────────────────── обнаружение входа


def natural_key(path: Path):
    """Сортировка file2.tif < file10.tif."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def parse_time(name: str) -> datetime | None:
    """Пытается вытащить дату/время из имени файла RAS Mapper."""
    # Depth (24JAN2024 06 00 00).tif
    m = re.search(r"(\d{2})([A-Z]{3})(\d{4})[ _](\d{2})[ _:](\d{2})[ _:](\d{2})", name.upper())
    if m and m.group(2) in MONTHS:
        d, mon, y, hh, mm, ss = m.groups()
        return datetime(int(y), MONTHS[mon], int(d), int(hh), int(mm), int(ss))
    # 2024-01-24T06-00-00 / 20240124_060000
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})[T_ ]?(\d{2})[-:]?(\d{2})[-:]?(\d{2})", name)
    if m:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss)
        except ValueError:
            return None
    return None


@dataclass
class Scenario:
    key: str
    label: str
    files: list[Path]


def discover() -> list[Scenario]:
    if not RAW_DIR.exists():
        sys.exit(f"Нет каталога {RAW_DIR}. Положите растры в data/raw/<сценарий>/")

    scenarios = []
    for sub in sorted(p for p in RAW_DIR.iterdir() if p.is_dir()):
        files = sorted(
            [f for f in sub.iterdir() if f.suffix.lower() in (".tif", ".tiff")],
            key=natural_key,
        )
        if not files:
            print(f"  пропуск {sub.name}: нет .tif")
            continue
        # Подпись: label.txt, если положен импортёром, иначе имя каталога
        # (числовой префикс задаёт порядок и в подпись не попадает).
        lf = sub / "label.txt"
        label = (lf.read_text(encoding="utf-8").strip() if lf.exists()
                 else re.sub(r"^\d+[_\-\s]*", "", sub.name).replace("_", " ").strip())
        scenarios.append(Scenario(key=sub.name, label=label or sub.name, files=files))

    if not scenarios:
        sys.exit("Сценарии не найдены.")
    return scenarios


# ─────────────────────────────────────────────────────────────── чтение растра


def clean(arr: np.ndarray, nodata) -> np.ndarray:
    """NoData и мусор → 0 (сухо). Возвращает float32."""
    arr = arr.astype(np.float32, copy=False)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    if nodata is not None and not math.isnan(nodata):
        arr[arr == np.float32(nodata)] = 0.0
    arr[arr < 0] = 0.0
    return arr


def read_block_mean(src, window: Window, factor: int) -> np.ndarray:
    """
    Читает окно построчными полосами, чистит NoData и усредняет блоками
    factor×factor. Так NoData (-9999) не «протекает» в результат, а память
    остаётся маленькой даже для гигабайтных растров.
    """
    h = (int(window.height) // factor) * factor
    w = (int(window.width) // factor) * factor
    out = np.zeros((h // factor, w // factor), dtype=np.float32)
    nodata = src.nodata

    strip_rows = max(factor, (factor * 256 // max(1, w // 1024 or 1)) // factor * factor)
    for r0 in range(0, h, strip_rows):
        rows = min(strip_rows, h - r0)
        win = Window(window.col_off, window.row_off + r0, w, rows)
        block = clean(src.read(1, window=win), nodata)
        block = block.reshape(rows // factor, factor, w // factor, factor).mean(axis=(1, 3))
        out[r0 // factor : r0 // factor + rows // factor] = block
    return out


def flood_extent(files: list[Path]) -> tuple[Window, rasterio.Affine, str]:
    """Быстрый первый проход: общий bbox затопления по всем кадрам."""
    with rasterio.open(files[0]) as src:
        H, W, transform, crs = src.height, src.width, src.transform, src.crs
        probe = max(1, int(max(H, W) / 400))

    rmin, rmax, cmin, cmax = H, -1, W, -1
    for f in files:
        with rasterio.open(f) as src:
            small = read_block_mean(src, Window(0, 0, W, H), probe)
        wet = small > WET_THRESHOLD
        if not wet.any():
            continue
        rows = np.flatnonzero(wet.any(axis=1))
        cols = np.flatnonzero(wet.any(axis=0))
        rmin, rmax = min(rmin, rows[0]), max(rmax, rows[-1])
        cmin, cmax = min(cmin, cols[0]), max(cmax, cols[-1])

    if rmax < 0:
        sys.exit("Затопление не найдено ни в одном кадре — проверьте порог WET_THRESHOLD.")

    pad = 2  # запас в единицах грубой сетки
    r0 = max(0, (rmin - pad) * probe)
    c0 = max(0, (cmin - pad) * probe)
    r1 = min(H, (rmax + 1 + pad) * probe)
    c1 = min(W, (cmax + 1 + pad) * probe)
    return Window(c0, r0, c1 - c0, r1 - r0), transform, crs


# ────────────────────────────────────────────────────────────────── упаковка


# Формат листов. WebP lossless побитово равен PNG и всегда меньше его.
# Сжатие с потерями здесь неприменимо: оно размывает кромку затопления
# и «заливает» сушу — см. раздел про форматы в README.
FMT = {"png": (".png", {"optimize": True}),
       "webp": (".webp", {"lossless": True, "quality": 100, "method": 6})}


def save_gray(arr: np.ndarray, path_base: Path, fmt: str) -> str:
    ext, opts = FMT[fmt]
    path = path_base.with_suffix(ext)
    Image.fromarray(arr, mode="L").save(path, **opts)
    return path.name


def pack_atlas(frames: list[np.ndarray], w: int, h: int, out_dir: Path, prefix: str,
               fmt: str = "png") -> dict:
    """Кадры → сетка ячеек w×h в нескольких PNG-листах."""
    cols = max(1, SHEET_MAX // w)
    rows = max(1, SHEET_MAX // h)
    per_sheet = cols * rows
    sheets = []

    for i in range(0, len(frames), per_sheet):
        chunk = frames[i : i + per_sheet]
        used_rows = math.ceil(len(chunk) / cols)
        sheet = np.zeros((used_rows * h, cols * w), dtype=np.uint8)
        for j, fr in enumerate(chunk):
            r, c = divmod(j, cols)
            sheet[r * h : (r + 1) * h, c * w : (c + 1) * w] = fr
        sheets.append(save_gray(sheet, out_dir / f"{prefix}_atlas{len(sheets)}", fmt))

    return {"cols": cols, "rows": rows, "perSheet": per_sheet, "sheets": sheets}


# ─────────────────────────────────────────────────────────────────── обработка


def process(sc: Scenario, max_dim: int, max_frames: int, fmt: str = "png") -> dict:
    files = sc.files
    if len(files) > max_frames:
        idx = np.linspace(0, len(files) - 1, max_frames).round().astype(int)
        files = [files[i] for i in sorted(set(idx.tolist()))]
    print(f"\n▸ {sc.label}: {len(sc.files)} кадров → {len(files)}")

    window, src_transform, src_crs = flood_extent(files)
    print(f"  окно затопления: {int(window.width)}×{int(window.height)} px")

    # Геометрия окна в исходной СК
    win_transform = rasterio.windows.transform(window, src_transform)
    win_bounds = rasterio.windows.bounds(window, src_transform)

    # Целевая сетка в EPSG:3857
    dst_crs = "EPSG:3857"
    dst_bounds = transform_bounds(src_crs, dst_crs, *win_bounds, densify_pts=21)
    bw, bh = dst_bounds[2] - dst_bounds[0], dst_bounds[3] - dst_bounds[1]
    if bw >= bh:
        dw = min(max_dim, int(window.width))
        dh = max(1, round(dw * bh / bw))
    else:
        dh = min(max_dim, int(window.height))
        dw = max(1, round(dh * bw / bh))
    dst_transform = rasterio.transform.from_bounds(*dst_bounds, dw, dh)
    print(f"  целевая сетка: {dw}×{dh} px, EPSG:3857")

    # Промежуточное прореживание в исходной СК (≈2× целевого — запас на варп)
    factor = max(1, int(min(window.width / (dw * 2), window.height / (dh * 2))))
    inter_transform = win_transform * rasterio.Affine.scale(factor, factor)

    stack = np.zeros((len(files), dh, dw), dtype=np.float32)
    for i, f in enumerate(files):
        with rasterio.open(f) as src:
            inter = read_block_mean(src, window, factor)
        reproject(
            source=inter,
            destination=stack[i],
            src_transform=inter_transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
            src_nodata=None,
            dst_nodata=0,
        )
        print(f"  [{i + 1:>3}/{len(files)}] {f.name}", end="\r", flush=True)
    print(" " * 70, end="\r")

    stack[stack < WET_THRESHOLD] = 0.0
    max_depth = float(stack.max())
    if max_depth <= 0:
        sys.exit(f"{sc.label}: все кадры сухие.")

    # Квантование: 0 = сухо, 1..255 = глубина
    scale = max_depth / 255.0
    quant = np.where(stack > 0, np.clip(np.rint(stack / scale), 1, 255), 0).astype(np.uint8)
    frames = [quant[i] for i in range(quant.shape[0])]

    # Производные слои
    wet = quant > 0
    arrival_idx = np.where(wet.any(axis=0), wet.argmax(axis=0), 255).astype(np.float32)
    arrival = np.where(wet.any(axis=0), np.clip(1 + arrival_idx * 254 / max(1, len(files) - 1), 1, 255), 0)
    depth_max = quant.max(axis=0)

    # Гидрограф: площадь и глубины по кадрам.
    # Меркатор растягивает площади в 1/cos²(φ) — на 55° это ×3, поэтому
    # площадь пикселя приводим к реальной по широте центра снимка.
    lat_c = math.radians(corner_lonlat(dst_bounds)[0][1] + corner_lonlat(dst_bounds)[2][1]) / 2
    px_area_km2 = abs(dst_transform.a * dst_transform.e) * math.cos(lat_c) ** 2 / 1e6
    area = [float(w.sum() * px_area_km2) for w in wet]
    mean_d = [float(stack[i][wet[i]].mean()) if wet[i].any() else 0.0 for i in range(len(files))]
    peak_d = [float(stack[i].max()) for i in range(len(files))]

    # Время
    times = [parse_time(f.name) for f in files]
    if all(t is not None for t in times) and len(set(times)) > 1:
        t0 = times[0]
        elapsed = [(t - t0).total_seconds() / 3600.0 for t in times]
        iso = [t.isoformat() for t in times]
    else:
        elapsed = list(np.linspace(0, len(files) - 1, len(files)).astype(float))
        iso = None

    prefix = re.sub(r"[^a-z0-9]+", "-", sc.key.lower()).strip("-") or "sc"
    atlas = pack_atlas(frames, dw, dh, OUT_DIR, prefix, fmt)
    arrival_name = save_gray(arrival.astype(np.uint8), OUT_DIR / f"{prefix}_arrival", fmt)
    maxdepth_name = save_gray(depth_max, OUT_DIR / f"{prefix}_maxdepth", fmt)

    corners = corner_lonlat(dst_bounds)
    size_mb = sum((OUT_DIR / s).stat().st_size for s in atlas["sheets"]) / 1e6
    print(f"  атлас: {len(atlas['sheets'])} лист(ов), {size_mb:.1f} МБ")
    print(f"  макс. глубина {max_depth:.2f} м, макс. площадь {max(area):.2f} км²")

    return {
        "id": prefix,
        "label": sc.label,
        "width": dw,
        "height": dh,
        "frames": len(files),
        "depthScale": scale,
        "maxDepth": max_depth,
        "bounds3857": list(dst_bounds),
        "corners": corners,
        "elapsedHours": elapsed,
        "times": iso,
        "timeIsHours": iso is not None,
        "atlas": atlas,
        "arrival": arrival_name,
        "maxdepth": maxdepth_name,
        "series": {"areaKm2": area, "meanDepth": mean_d, "peakDepth": peak_d},
    }


def corner_lonlat(b) -> list[list[float]]:
    """EPSG:3857 bbox → [TL, TR, BR, BL] в градусах для MapLibre."""
    minx, miny, maxx, maxy = b
    R = 6378137.0

    def to_lonlat(x, y):
        lon = math.degrees(x / R)
        lat = math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)
        return [lon, lat]

    return [to_lonlat(minx, maxy), to_lonlat(maxx, maxy), to_lonlat(maxx, miny), to_lonlat(minx, miny)]


# ───────────────────────────────────────────────────────────────────── main


def main():
    ap = argparse.ArgumentParser(description="HEC-RAS depth rasters → web assets")
    ap.add_argument("--max-dim", type=int, default=1400, help="макс. сторона кадра, px")
    ap.add_argument("--max-frames", type=int, default=60, help="макс. число кадров")
    ap.add_argument("--format", choices=list(FMT), default="webp",
                    help="формат листов: webp (меньше) или png (совместимее)")
    ap.add_argument("--clean", action="store_true", help="очистить web/data перед сборкой")
    args = ap.parse_args()

    if args.clean and OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    scenarios = [process(sc, args.max_dim, args.max_frames, args.format) for sc in discover()]

    # Верх цветовой шкалы общий для всех сценариев — иначе их нельзя
    # сравнивать глазом. Но и упирать его в 14 м нельзя: прорыв плотины
    # даёт глубины, которых паводок не даёт.
    ramp_max = max(6.0, math.ceil(max(s["maxDepth"] for s in scenarios) / 2) * 2)

    manifest = {
        "rampMax": ramp_max,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wetThreshold": WET_THRESHOLD,
        "scenarios": scenarios,
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total = sum(f.stat().st_size for f in OUT_DIR.iterdir() if f.is_file()) / 1e6
    print(f"\n✓ Готово. {len(scenarios)} сценари(ев), {total:.1f} МБ в web/data/")


if __name__ == "__main__":
    main()
