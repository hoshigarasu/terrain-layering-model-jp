#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国土地理院 標高タイル（PNG形式）→ GeoTIFF 変換スクリプト

使用例:
  python download_dem.py --lat 37.81-37.87 --lon 139.59-139.69 -o dem.tif
  python download_dem.py --lat 35.65-35.70 --lon 139.70-139.75 -o tokyo.tif
"""

import sys
import math
import requests
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from pathlib import Path
import argparse
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


# ──────────────────────────────────────────────
# タイル座標変換
# ──────────────────────────────────────────────

def deg2num(lat, lon, zoom):
    lat_r = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y

def num2deg(x, y, zoom):
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


# ──────────────────────────────────────────────
# PNG → 標高変換
# ──────────────────────────────────────────────

def png_to_elevation(png_bytes):
    """
    国土地理院標高タイル（PNG）から標高配列を生成

    仕様:
      x = R * 65536 + G * 256 + B
      x < 8388608  → elevation = x * 0.01 [m]  (正の標高)
      x == 8388608 → NoData  (RGB = 128, 0, 0)
      x > 8388608  → elevation = (x - 16777216) * 0.01 [m]  (負の標高)
    """
    img = Image.open(BytesIO(png_bytes)).convert('RGB')
    arr = np.array(img, dtype=np.uint32)

    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    x = r * 65536 + g * 256 + b

    elev = np.where(
        x < 8388608,
        x.astype(np.float32) * 0.01,                    # 正の標高
        (x.astype(np.float32) - 16777216.0) * 0.01      # 負の標高
    )

    # NoData: x == 8388608 (RGB = 128, 0, 0)
    elev[x == 8388608] = np.nan

    return elev.astype(np.float32)


# ──────────────────────────────────────────────
# 並列タイルダウンローダー
# ──────────────────────────────────────────────

class TileDownloader:

    SOURCES = ['dem5a_png', 'dem5b_png', 'dem_png']

    def __init__(self, cache_dir='tiles_cache', max_workers=10):
        self.cache_dir = Path(cache_dir)
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'Mozilla/5.0'

    def _cache(self, zoom, x, y):
        return self.cache_dir / str(zoom) / str(x) / f"{y}.npy"

    def download_one(self, x, y, zoom):
        cache = self._cache(zoom, x, y)
        cache.parent.mkdir(parents=True, exist_ok=True)

        if cache.exists():
            try:
                return x, y, np.load(cache)
            except Exception:
                cache.unlink()

        # 3ソースを全て取得してピクセル単位で補完
        # DEM5A（レーザー測量）→ DEM5B（写真測量）→ DEM10B（広域）の順で優先
        layers = []
        for src in self.SOURCES:
            url = f"https://cyberjapandata.gsi.go.jp/xyz/{src}/{zoom}/{x}/{y}.png"
            try:
                resp = self.session.get(url, timeout=10)
                if resp.status_code == 200:
                    layers.append(png_to_elevation(resp.content))
                else:
                    layers.append(None)
            except Exception:
                layers.append(None)

        # ピクセル単位で補完: 上位ソースがNaNの箇所を下位ソースで埋める
        result = np.full((256, 256), np.nan, dtype=np.float32)
        for layer in layers:
            if layer is None:
                continue
            # まだNaNの箇所だけ埋める
            mask = np.isnan(result) & ~np.isnan(layer)
            result[mask] = layer[mask]
            # 全ピクセルが埋まったら終了
            if not np.any(np.isnan(result)):
                break

        np.save(cache, result)
        return x, y, result

    def download_all(self, tiles, zoom, progress_callback=None):
        results = {}
        total = len(tiles)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.download_one, x, y, zoom): (x, y)
                       for x, y in tiles}
            for done_count, f in enumerate(tqdm(as_completed(futures), total=total,
                          desc="ダウンロード", unit="枚"), 1):
                x, y = futures[f]
                try:
                    _, _, data = f.result()
                    results[(x, y)] = data
                except Exception:
                    results[(x, y)] = np.full((256, 256), np.nan, dtype=np.float32)
                if progress_callback:
                    progress_callback(done_count, total)
        return results


# ──────────────────────────────────────────────
# GeoTIFF 生成（メイン処理）
# ──────────────────────────────────────────────

def create_geotiff(lat_min, lat_max, lon_min, lon_max,
                   output_file, zoom=14, skip_confirm=False, max_workers=10,
                   progress_callback=None):
    """
    指定範囲のGeoTIFFを生成する。
    terrain_web_app.py からも直接呼び出し可能。
    progress_callback : callable(done, total) | None
    """
    # 座標逆転を自動修正
    lat_min, lat_max = min(lat_min, lat_max), max(lat_min, lat_max)
    lon_min, lon_max = min(lon_min, lon_max), max(lon_min, lon_max)

    print(f"範囲: {lat_min:.4f}～{lat_max:.4f}°N, {lon_min:.4f}～{lon_max:.4f}°E")
    print(f"ズームレベル: {zoom}")

    x_min, y_min = deg2num(lat_max, lon_min, zoom)
    x_max, y_max = deg2num(lat_min, lon_max, zoom)
    
    print(f"タイル範囲計算:")
    print(f"  deg2num({lat_max:.6f}, {lon_min:.6f}) = ({x_min}, {y_min})")
    print(f"  deg2num({lat_min:.6f}, {lon_max:.6f}) = ({x_max}, {y_max})")
    
    nx, ny = x_max - x_min + 1, y_max - y_min + 1
    num_tiles = nx * ny
    print(f"タイル数: {num_tiles}枚 ({nx} x {ny})")

    if num_tiles > 100 and not skip_confirm:
        if input(f"タイル数が多い ({num_tiles}枚)。続行？ (y/N): ").lower() != 'y':
            return False

    tiles = [(tx, ty)
             for ty in range(y_min, y_max + 1)
             for tx in range(x_min, x_max + 1)]

    downloader = TileDownloader(max_workers=max_workers)
    tile_data = downloader.download_all(tiles, zoom, progress_callback=progress_callback)

    # タイルを結合
    TILE = 256
    height, width = ny * TILE, nx * TILE
    elevation = np.full((height, width), np.nan, dtype=np.float32)

    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            data = tile_data.get((tx, ty))
            if data is not None:
                r0 = (ty - y_min) * TILE
                c0 = (tx - x_min) * TILE
                elevation[r0:r0 + TILE, c0:c0 + TILE] = data

    valid = elevation[~np.isnan(elevation)]
    if len(valid) == 0:
        print("❌ 有効なデータが取得できませんでした")
        print("   インターネット接続と座標範囲（日本国内）を確認してください")
        return False

    # ── タイルモザイクの実際の座標範囲 ──────────────────────────────
    lat_max_actual, lon_min_actual = num2deg(x_min,     y_min,     zoom)
    lat_min_actual, lon_max_actual = num2deg(x_max + 1, y_max + 1, zoom)

    # ── 要求座標にクロップしてアスペクト比を正しく保持 ──────────────
    # タイル1ピクセルあたりの度数を計算
    deg_per_px_lon = (lon_max_actual - lon_min_actual) / width
    deg_per_px_lat = (lat_max_actual - lat_min_actual) / height  # 正値（上が大）

    # 要求座標に対応するピクセル範囲（rowは上=lat_max_actual側が0）
    col_start = int((lon_min - lon_min_actual) / deg_per_px_lon)
    col_end   = int((lon_max - lon_min_actual) / deg_per_px_lon)
    row_start = int((lat_max_actual - lat_max) / deg_per_px_lat)
    row_end   = int((lat_max_actual - lat_min) / deg_per_px_lat)

    # 範囲をクランプ（タイル境界の丸め誤差でアレイ外になることを防ぐ）
    col_start = max(0, col_start)
    col_end   = min(width,  max(col_end,  col_start + 1))
    row_start = max(0, row_start)
    row_end   = min(height, max(row_end,  row_start + 1))

    elevation = elevation[row_start:row_end, col_start:col_end]
    height, width = elevation.shape

    # クロップ後の正確な座標範囲を再計算
    lon_min_actual = lon_min_actual + col_start * deg_per_px_lon
    lon_max_actual = lon_min_actual + width      * deg_per_px_lon
    lat_max_actual = lat_max_actual - row_start  * deg_per_px_lat
    lat_min_actual = lat_max_actual - height     * deg_per_px_lat

    print(f"✅ 有効データ: {(~np.isnan(elevation)).sum():,}px")
    print(f"   標高範囲: {np.nanmin(elevation):.1f}m ～ {np.nanmax(elevation):.1f}m")
    print(f"   出力サイズ: {width} x {height}px  ({'横長' if width > height else '縦長' if height > width else '正方形'})")

    transform = from_bounds(lon_min_actual, lat_min_actual,
                            lon_max_actual, lat_max_actual,
                            width, height)

    out = np.where(np.isnan(elevation), -9999.0, elevation)
    with rasterio.open(output_file, 'w',
                       driver='GTiff', height=height, width=width,
                       count=1, dtype=np.float32,
                       crs='EPSG:4326', transform=transform,
                       nodata=-9999, compress='lzw') as dst:
        dst.write(out, 1)

    print(f"✅ 保存完了: {output_file}  ({width} x {height}px)")
    return True


# ──────────────────────────────────────────────
# CLI エントリポイント
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='国土地理院 標高タイル → GeoTIFF',
        epilog="""
使用例:
  python download_dem.py --lat 37.81-37.87 --lon 139.59-139.69 -o dem.tif
  python download_dem.py --lat 35.20-35.50 --lon 138.60-138.90 --zoom 13 -o fuji.tif
"""
    )
    parser.add_argument('--lat',     required=True, help='緯度範囲  例: 37.81-37.87')
    parser.add_argument('--lon',     required=True, help='経度範囲  例: 139.59-139.69')
    parser.add_argument('-o', '--output', default='dem.tif')
    parser.add_argument('--zoom',    type=int, default=14)
    parser.add_argument('--workers', type=int, default=10)
    parser.add_argument('-y', '--yes', action='store_true')
    args = parser.parse_args()

    try:
        lat_min, lat_max = sorted(float(v) for v in args.lat.split('-'))
        lon_min, lon_max = sorted(float(v) for v in args.lon.split('-'))
    except Exception:
        print("エラー: 座標の形式が正しくありません  例: --lat 37.81-37.87")
        sys.exit(1)

    import time
    t = time.time()
    ok = create_geotiff(lat_min, lat_max, lon_min, lon_max,
                        args.output, zoom=args.zoom,
                        skip_confirm=args.yes, max_workers=args.workers)
    if ok:
        print(f"⏱  {time.time() - t:.1f}秒")
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
