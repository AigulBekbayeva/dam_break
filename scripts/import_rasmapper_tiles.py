#!/usr/bin/env python3
"""
Тайловый кэш RAS Mapper (.db) → GeoTIFF глубины.

RAS Mapper умеет экспортировать «Tile Cache» — SQLite вида MBTiles, но с
дополнительной колонкой `time`. Внутри лежат уже отрисованные PNG: цвет
в них — не значение, а картинка по легенде из таблицы `metadata`.

Легенда линейна (одна рампа между двумя цветами), поэтому преобразование
обратимо: находим канал с наибольшим размахом и решаем относительно него.
Точность — шаг легенды делить на 255, для шкалы 0–15 м это около 6 см.

    python scripts/import_rasmapper_tiles.py sce1.db --name 01_stsenariy-1

Дальше — обычный `python scripts/prepare.py --clean`.
"""

from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parent.parent
WORLD = 20037508.342789244
TILE = 256

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def time_key(t: str):
    """Сортировка срезов по реальной дате, а не по алфавиту."""
    m = re.match(r"(\d{2})([A-Z]{3})(\d{4})[ _](\d{2})[:_ ](\d{2})[:_ ](\d{2})", t.upper())
    if not m:
        return (9999, 0, 0, 0, 0, 0)
    d, mon, y, hh, mm, ss = m.groups()
    return (int(y), MONTHS.get(mon, 0), int(d), int(hh), int(mm), int(ss))


def find_layer(con) -> str:
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name<>'metadata'")]
    if not names:
        sys.exit("В базе нет таблицы с тайлами.")
    return names[0]


def build_inverse(meta: dict, layer: str):
    """Возвращает (канал, c_lo, c_hi, v_lo, v_hi) для обратного пересчёта."""
    rgba = [int(x) for x in meta[f"{layer}_legend_rgba"].split(",")]
    if len(rgba) < 8:
        sys.exit("В легенде меньше двух цветов — рампа не восстанавливается.")
    c0 = np.array(rgba[0:3], float)
    c1 = np.array(rgba[-4:-1], float)

    nums = re.findall(r"-?\d+(?:\.\d+)?", meta[f"{layer}_legend_values"])
    v_lo, v_hi = (float(nums[0]), float(nums[1])) if len(nums) >= 2 else (0.0, 1.0)

    ch = int(np.argmax(np.abs(c0 - c1)))
    if c0[ch] == c1[ch]:
        sys.exit("Цвета легенды не различаются — рампа не восстанавливается.")
    return ch, c0[ch], c1[ch], v_lo, v_hi


def main():
    ap = argparse.ArgumentParser(description="RAS Mapper tile cache → GeoTIFF")
    ap.add_argument("db", help="файл .db из RAS Mapper")
    ap.add_argument("--name", help="имя каталога сценария в data/raw/")
    ap.add_argument("--label", help="подпись сценария в интерфейсе (иначе имя каталога)")
    ap.add_argument("--zoom", type=int, help="зум (по умолчанию максимальный)")
    ap.add_argument("--frames", type=int, default=72, help="сколько срезов оставить")
    ap.add_argument("--list", action="store_true", help="только показать содержимое")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    layer = find_layer(con)
    meta = dict(con.execute("SELECT name, value FROM metadata"))

    zooms = [r[0] for r in con.execute(f'SELECT DISTINCT zoom_level FROM "{layer}" ORDER BY 1')]
    zoom = args.zoom or max(zooms)
    times = sorted({r[0] for r in con.execute(f'SELECT DISTINCT time FROM "{layer}"')} - {"Max"},
                   key=time_key)

    res = 2 * WORLD / (2 ** zoom * TILE)
    # На экваторе пиксель крупнее, чем на широте съёмки: делим на 1/cos(φ).
    lat = float(meta.get(f"{layer}_centery", 0.0))
    ground = res * np.cos(np.radians(lat))
    print(f"слой:      {layer}  ({meta.get(layer + '_map_type', '?')})")
    print(f"план:      {meta.get(layer + '_plan_name', '?')}")
    print(f"зумы:      {zooms}  → берём z{zoom} ({ground:.1f} м/пкс на широте {lat:.1f}°)")
    if ground > 10:
        need = zoom + int(np.ceil(np.log2(ground / 5)))
        print(f"           ↑ это грубее ЦМР 5 м. Для полного разрешения "
              f"пересоберите кэш в RAS Mapper с maxzoom {need}.")
    print(f"срезы:     {len(times)}  {times[0]} → {times[-1]}")
    print(f"легенда:   {meta.get(layer + '_legend_values')}")
    if args.list:
        return

    ch, c_lo, c_hi, v_lo, v_hi = build_inverse(meta, layer)
    print(f"обратный пересчёт по каналу {'RGB'[ch]}: "
          f"{c_lo:.0f}→{c_hi:.0f} ед. = {v_lo}→{v_hi} м, шаг {(v_hi - v_lo) / 255:.3f} м")

    x0, x1, y0, y1 = con.execute(
        f'SELECT MIN(tile_column),MAX(tile_column),MIN(tile_row),MAX(tile_row) '
        f'FROM "{layer}" WHERE zoom_level=?', (zoom,)).fetchone()
    W, H = (x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE
    transform = from_origin(-WORLD + x0 * TILE * res, WORLD - y0 * TILE * res, res, res)
    print(f"мозаика:   {x1 - x0 + 1}×{y1 - y0 + 1} тайлов = {W}×{H} пкс")

    if len(times) > args.frames:
        idx = np.linspace(0, len(times) - 1, args.frames).round().astype(int)
        times = [times[i] for i in sorted(set(idx.tolist()))]
        print(f"прорежено до {len(times)} срезов")

    out = ROOT / "data" / "raw" / (args.name or Path(args.db).stem)
    out.mkdir(parents=True, exist_ok=True)
    if args.label:
        (out / "label.txt").write_text(args.label, encoding="utf-8")

    profile = dict(driver="GTiff", dtype="float32", count=1, width=W, height=H,
                   crs="EPSG:3857", transform=transform, nodata=-9999.0,
                   compress="deflate", predictor=3, tiled=True,
                   blockxsize=256, blockysize=256)

    for n, t in enumerate(times, 1):
        depth = np.full((H, W), -9999.0, np.float32)
        for col, row, blob in con.execute(
                f'SELECT tile_column,tile_row,tile_data FROM "{layer}" '
                f'WHERE zoom_level=? AND time=?', (zoom, t)):
            a = np.array(Image.open(io.BytesIO(blob)).convert("RGBA"))
            wet = a[..., 3] > 0
            if not wet.any():
                continue
            v = (a[..., ch].astype(np.float32) - c_lo) / (c_hi - c_lo) * (v_hi - v_lo) + v_lo
            py, px = (row - y0) * TILE, (col - x0) * TILE
            block = depth[py:py + TILE, px:px + TILE]
            block[wet] = v[wet]

        fname = "Depth (" + t.replace(":", " ") + ").tif"
        with rasterio.open(out / fname, "w", **profile) as dst:
            dst.write(depth, 1)
        print(f"  [{n:>3}/{len(times)}] {fname}", end="\r", flush=True)

    total = sum(f.stat().st_size for f in out.iterdir()) / 1e6
    print(" " * 70, end="\r")
    print(f"\n✓ {len(times)} GeoTIFF в {out.relative_to(ROOT)} ({total:.0f} МБ)")
    print("  Дальше: python scripts/prepare.py --clean")


if __name__ == "__main__":
    main()
