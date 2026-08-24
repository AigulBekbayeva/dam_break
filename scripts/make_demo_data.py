#!/usr/bin/env python3
"""
Синтетический прорыв плотины — чтобы репозиторий собирался и показывал
демо без реальных расчётов HEC-RAS.

Строит долину с извилистым тальвегом, прогоняет по ней кинематическую
волну и пишет серии GeoTIFF глубины в формате, который выдаёт RAS Mapper:
одноканальный float32, NoData = -9999, время в имени файла.

    python scripts/make_demo_data.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from scipy.ndimage import gaussian_filter, gaussian_filter1d

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

CELL = 5.0  # м, разрешение ЦМР
NX, NY = 900, 700  # 4.5 × 3.5 км
ORIGIN_X, ORIGIN_Y = 412_000.0, 6_105_000.0  # EPSG:32637, Московская обл.
CRS = "EPSG:32637"
DAM_X = 350.0  # створ плотины, м от левой границы

STEP_MIN = 5
N_STEPS = 48  # 4 часа

MONTH = "JAN"
DAY, YEAR = 24, 2024


def build_terrain() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Возвращает (ЦМР, отметка дна долины по створам, y тальвега по створам)."""
    x = np.arange(NX) * CELL  # расстояние вниз по долине, м
    y = np.arange(NY) * CELL

    # Извилистый тальвег
    yc = (
        NY * CELL * 0.5
        + 420 * np.sin(2 * np.pi * x / 3100)
        + 180 * np.sin(2 * np.pi * x / 1150 + 1.2)
    )

    # Продольный профиль дна
    zv = 168.0 - 0.0028 * x - 4.0 * np.sin(2 * np.pi * x / 4200)

    # Поперечник: узкое ущелье вверху, широкая пойма внизу
    widen = 1.0 + 2.4 * (x / x[-1]) ** 1.4
    n = np.abs(y[None, :] - yc[:, None])  # (NX, NY)
    banks = 0.0016 * (n / widen[:, None]) ** 1.5
    dem = zv[:, None] + np.clip(banks, 0, 90)

    # Рельеф вокруг: мягкий шум, заметно меньше глубины волны
    rng = np.random.default_rng(20260824)
    dem += gaussian_filter(rng.normal(0, 1, (NX, NY)), 22) * 5.0
    dem += gaussian_filter(rng.normal(0, 1, (NX, NY)), 3) * 0.6

    # Плотина: поперечный гребень в створе x ≈ 350 м
    dem += np.exp(-(((x - DAM_X) / 40) ** 2))[:, None] * 18.0

    return dem.T.astype(np.float32), zv, yc  # (NY, NX)


def hydrograph(u: np.ndarray, t_peak: float, sharpness: float) -> np.ndarray:
    """Гамма-образный импульс: 0 до прихода волны, пик в t_peak, спад."""
    u = np.maximum(u, 1e-6)
    r = u / t_peak
    return np.where(u > 0, r**sharpness * np.exp(sharpness * (1 - r)), 0.0)


def make_scenario(name: str, folder: str, dem, zv, *, peak_m, celerity, t_peak_h, atten, sharp):
    out = RAW / folder
    out.mkdir(parents=True, exist_ok=True)
    x = np.arange(NX) * CELL
    transform = from_origin(ORIGIN_X, ORIGIN_Y, CELL, CELL)

    downstream = x >= DAM_X
    arrival_h = np.maximum(x - DAM_X, 0) / (celerity * 3600.0)
    decay = np.exp(-atten * np.maximum(x - DAM_X, 0) / 1000.0)
    res_level = zv[int(DAM_X / CELL)] + peak_m + 5.0  # НПУ водохранилища

    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "width": NX, "height": NY, "crs": CRS, "transform": transform,
        "nodata": -9999.0, "compress": "deflate", "predictor": 3,
        "tiled": True, "blockxsize": 256, "blockysize": 256,
    }

    peak_area = 0
    for k in range(N_STEPS):
        t = k * STEP_MIN / 60.0
        u = t - arrival_h

        # Волна прорыва + остаточный сток в русле
        pulse = peak_m * decay * hydrograph(u, t_peak_h, sharp)
        base = 1.1 * decay * np.clip(u / 0.25, 0, 1)
        h_axis = np.where(downstream, gaussian_filter1d(pulse + base, 5), 0.0)

        wse = np.where(downstream, zv + h_axis, res_level - 3.2 * np.clip(t / 1.5, 0, 1))
        depth = wse[None, :] - dem
        depth = np.where((h_axis[None, :] > 0.02) | ~downstream[None, :], depth, -1.0)
        depth = np.where(depth > 0.02, depth, -9999.0).astype(np.float32)
        peak_area = max(peak_area, int((depth > 0).sum()))

        hh = 6 + (k * STEP_MIN) // 60
        mm = (k * STEP_MIN) % 60
        fname = f"Depth ({DAY:02d}{MONTH}{YEAR} {hh:02d} {mm:02d} 00).tif"
        with rasterio.open(out / fname, "w", **profile) as dst:
            dst.write(depth, 1)
            dst.set_band_description(1, "Depth (m)")

    km2 = peak_area * CELL * CELL / 1e6
    print(f"  {name}: {N_STEPS} кадров, макс. площадь ≈ {km2:.2f} км² → data/raw/{folder}/")


def main():
    print("Строю ЦМР…")
    dem, zv, _ = build_terrain()

    dem_profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "width": NX, "height": NY, "crs": CRS,
        "transform": from_origin(ORIGIN_X, ORIGIN_Y, CELL, CELL),
        "compress": "deflate", "predictor": 3,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    with rasterio.open(ROOT / "data" / "dem_5m.tif", "w", **dem_profile) as dst:
        dst.write(dem, 1)

    print("Считаю сценарии…")
    make_scenario("Полный мгновенный прорыв", "01_polnyy-proryv", dem, zv,
                  peak_m=13.5, celerity=2.9, t_peak_h=0.7, atten=0.10, sharp=2.2)
    make_scenario("Частичный прорыв, 50 %", "02_chastichnyy-proryv", dem, zv,
                  peak_m=8.0, celerity=2.0, t_peak_h=1.1, atten=0.13, sharp=2.6)
    make_scenario("Постепенный размыв гребня", "03_postepennyy-razmyv", dem, zv,
                  peak_m=5.2, celerity=1.2, t_peak_h=1.9, atten=0.09, sharp=3.4)

    print("\n✓ Демо-данные готовы. Дальше: python scripts/prepare.py --clean")


if __name__ == "__main__":
    main()
