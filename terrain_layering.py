#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
地形積層模型 SVG 生成ツール
GeoTIFF（dem.tif）から等高線レイヤーのSVGファイルを生成する。

使用例:
  python terrain_layering.py dem.tif --interval 10
  python terrain_layering.py dem.tif --interval 20 --output layers/
"""

import argparse
import time
from pathlib import Path

import numpy as np
import rasterio
import svgwrite
from scipy.ndimage import binary_fill_holes, zoom, binary_dilation
from skimage import measure

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


# ──────────────────────────────────────────────
# カスタムカラーマップ "topo"
# 濃いグリーン → 黄色 → 濃い茶色（地形模型向け・ブルー帯なし）
# ──────────────────────────────────────────────

def _register_topo_cmap():
    import matplotlib
    from matplotlib.colors import LinearSegmentedColormap
    name = 'topo'
    if name in matplotlib.colormaps:
        return
    cmap = LinearSegmentedColormap.from_list(name, [
        (0.00, '#2d6e2d'),  # 濃いグリーン（低地）
        (0.30, '#7ab648'),  # 明るいグリーン
        (0.55, '#d4c84a'),  # 黄色
        (0.75, '#c47a30'),  # オレンジ茶
        (1.00, '#5c2e0a'),  # 濃い茶色（高地）
    ], N=256)
    matplotlib.colormaps.register(cmap)

_register_topo_cmap()


# ──────────────────────────────────────────────
# メインクラス
# ──────────────────────────────────────────────

class TerrainLayerGenerator:

    def __init__(self, dem_file, interval=10, base_elevation=None, downsample=0.5,
                 smoothing_sigma=0, simplify_tolerance=1.5,
                 elev_range_min=None, elev_range_max=None):
        """
        Parameters
        ----------
        dem_file : str
            GeoTIFFファイルのパス
        interval : float
            等高線間隔（メートル）
        base_elevation : float | None
            最下層の標高。Noneの場合は自動設定
        downsample : float
            解像度係数（1.0=等倍, 0.5=半分）。処理速度と品質のトレードオフ
        """
        self.dem_file          = dem_file
        self.interval          = interval
        self.downsample        = downsample
        self.smoothing_sigma   = smoothing_sigma
        self.simplify_tolerance = simplify_tolerance
        self._src_path         = dem_file   # gsi_topo用にパスを保持
        self._topo_zoom        = 15         # zoom15: zoom14の4倍解像度
        self._topo_image       = None       # キャッシュ
        self._satellite_image  = None       # キャッシュ

        print(f"DEMファイルを読み込み中: {dem_file}")
        t = time.time()

        with rasterio.open(dem_file) as src:
            raw          = src.read(1).astype(np.float32)
            self.nodata  = src.nodata
            self.bounds  = src.bounds
            orig_shape   = raw.shape
            self.orig_shape = orig_shape  # gsi_topo用：ダウンサンプル前の解像度

        # NoData → NaN
        # ① ファイルメタデータのnodata値
        if self.nodata is not None:
            raw[raw == self.nodata] = np.nan
        # ② nodata属性が未設定でも-9999/-32768は事実上nodata
        raw[raw <= -9000] = np.nan
        # ③ 海面（標高≒0以下）は模型として不要なのでnodata扱い
        #    ただしユーザーが明示的にelev_range_minを設定した場合はそちら優先
        _sea_pixels = int((raw < 0).sum()) if elev_range_min is None else 0
        if elev_range_min is None:
            raw[raw < 0] = np.nan
        if _sea_pixels:
            print(f"  海面除去: {_sea_pixels}px を NaN に変換")

        # ダウンサンプリング
        if downsample != 1.0:
            raw = zoom(raw, downsample, order=1)

        # 標高範囲のクリッピング（GUIの段彩設定）
        if elev_range_min is not None:
            raw[raw < elev_range_min] = np.nan
        if elev_range_max is not None:
            raw[raw > elev_range_max] = np.nan

        # スムージング
        if smoothing_sigma > 0:
            from scipy.ndimage import gaussian_filter
            smoothed = gaussian_filter(np.nan_to_num(raw, nan=0.0), sigma=smoothing_sigma)
            smoothed[np.isnan(raw)] = np.nan
            raw = smoothed

        self.elevation = np.ma.masked_invalid(raw)

        self.min_elev = float(np.nanmin(self.elevation))
        self.max_elev = float(np.nanmax(self.elevation))

        if base_elevation is None:
            self.base_elev = np.floor(self.min_elev / interval) * interval
        else:
            self.base_elev = base_elevation

        print(f"  元サイズ: {orig_shape[1]} x {orig_shape[0]}px")
        if downsample != 1.0:
            print(f"  ダウンサンプル後: {self.elevation.shape[1]} x {self.elevation.shape[0]}px")
        print(f"  標高範囲: {self.min_elev:.1f}m ～ {self.max_elev:.1f}m")
        print(f"  base_elev: {self.base_elev:.0f}m  (有効base: {max(0.0,self.base_elev):.0f}m)")
        print(f"  等高線間隔: {self.interval}m  → 約{len(self.get_levels())}層")
        print(f"  読み込み: {time.time() - t:.1f}秒")

    # ──────────────────────────────────────────
    # 標高レベル一覧
    # ──────────────────────────────────────────

    def get_levels(self):
        """
        積層模型のレベル一覧を返す（昇順: 最下層→最上層）。
        各レベルは「そのレベル以上の累積シルエット」を表す。
        layer_0001 が最下層（base_elev以上 = GeoTIFF有効範囲全体）、
        最後が最上層（山頂付近）。積み重ねは layer_0001 から順に行う。
        """
        # base_elevが負になるのは海面ピクセルが混入しているサイン。
        # 陸地模型として 0m が最低限の基準なので 0 未満はクランプする。
        effective_base = max(0.0, self.base_elev)

        levels = []
        v = effective_base
        while v <= self.max_elev + self.interval * 0.01:
            levels.append(v)
            v += self.interval
        return levels  # 昇順

    # ──────────────────────────────────────────
    # 等高線抽出
    # ──────────────────────────────────────────

    def extract_contours(self, level, simplify_eps=1.5):
        """
        指定標高レベルに対応する層の輪郭を抽出。

        積層模型では各層は「このレベル以上の全領域」のシルエット。
        例: level=800m → 標高800m以上の全領域の外形輪郭を抽出。
        これを下から順に積み重ねることで正しい地形模型になる。

        Parameters
        ----------
        level : float
            この層の下端標高（= elevation >= level の領域を抽出）
        simplify_eps : float
            簡略化許容誤差（ピクセル）
        """
        # マスク配列から実データを取得
        if hasattr(self.elevation, 'data'):
            data = self.elevation.data.copy()
            if self.nodata is not None:
                data[data == self.nodata] = np.nan
        else:
            data = self.elevation.copy()

        # __init__でNaN対応スムージング済みのため、タイル境界段差は解消済み。
        # バイナリマスクをそのまま生成する。
        valid = ~np.isnan(data)
        binary_mask = valid & (data >= level)

        # 閉じた低地（周囲が陸で囲まれた海抜以下の窪地）を陸地扱いに。
        # binary_fill_holes は外部と繋がっていない「穴」を埋める。
        # NaN（海）が外部と繋がっている限り海は埋まらないので安全。
        binary_mask = binary_fill_holes(binary_mask)
        padded = np.zeros((binary_mask.shape[0] + 2, binary_mask.shape[1] + 2), dtype=np.float32)
        padded[1:-1, 1:-1] = binary_mask.astype(np.float32)

        raw_contours = measure.find_contours(padded, 0.5)

        # パディング分の座標オフセットを除去（-1）
        raw_contours = [c - 1.0 for c in raw_contours]

        # 小さすぎる輪郭（ノイズ）を除外
        raw_contours = [c for c in raw_contours if len(c) >= 10]

        simplified = []
        for c in raw_contours:
            if len(c) < 4:
                continue
            s = _rdp_simplify(c, simplify_eps)
            if len(s) >= 3:
                simplified.append(s)

        return simplified

    # ──────────────────────────────────────────
    # 国土地理院タイル画像の取得・合成
    # ──────────────────────────────────────────

    def _ensure_topo_image(self):
        """
        国土地理院標準地図タイルを DEM と同じ範囲・解像度で合成して
        self._topo_image (PIL.Image, RGB) にキャッシュする。
        一度合成したら再利用。
        """
        if hasattr(self, '_topo_image') and self._topo_image is not None:
            return self._topo_image

        import rasterio
        from PIL import Image
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import urllib.request
        import math

        with rasterio.open(self._src_path) as src:
            bounds = src.bounds
            zoom   = self._topo_zoom

        def deg2num(lat, lon, z):
            lat_r = math.radians(lat)
            n = 2 ** z
            x = int((lon + 180) / 360 * n)
            y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
            return x, y

        x_min, y_min = deg2num(bounds.top,    bounds.left,  zoom)
        x_max, y_max = deg2num(bounds.bottom, bounds.right, zoom)
        nx = x_max - x_min + 1
        ny = y_max - y_min + 1
        TILE = 256
        mosaic = Image.new('RGB', (nx * TILE, ny * TILE), (240, 240, 240))

        def fetch_tile(tx, ty):
            url = (f'https://cyberjapandata.gsi.go.jp/'
                   f'xyz/std/{zoom}/{tx}/{ty}.png')
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    return tx, ty, Image.open(r).convert('RGB').copy()
            except Exception:
                return tx, ty, None

        tiles = [(tx, ty)
                 for ty in range(y_min, y_max + 1)
                 for tx in range(x_min, x_max + 1)]

        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {pool.submit(fetch_tile, tx, ty): (tx, ty)
                    for tx, ty in tiles}
            for f in as_completed(futs):
                tx, ty, img = f.result()
                if img:
                    col = (tx - x_min) * TILE
                    row = (ty - y_min) * TILE
                    mosaic.paste(img, (col, row))

        # ── タイルモザイクを DEM の正確な地理範囲にクロップ ──────────
        # satellite と同じ理由：タイル境界ずれを補正する
        n = 2 ** zoom
        def deg2px_frac_topo(lat, lon):
            lat_r = math.radians(lat)
            tx_frac = (lon + 180) / 360 * n
            ty_frac = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
            return (tx_frac - x_min) * TILE, (ty_frac - y_min) * TILE

        crop_l, crop_t = deg2px_frac_topo(bounds.top,    bounds.left)
        crop_r, crop_b = deg2px_frac_topo(bounds.bottom, bounds.right)
        mosaic = mosaic.crop((
            max(0, int(round(crop_l))),
            max(0, int(round(crop_t))),
            min(mosaic.width,  int(round(crop_r))),
            min(mosaic.height, int(round(crop_b))),
        ))

        self._topo_image = mosaic
        return mosaic

    def _ensure_satellite_image(self, progress_callback=None):
        """
        ESRI World Imagery (satellite) tiles を DEM と同じ範囲で合成して
        self._satellite_image (PIL.Image, RGB) にキャッシュする。
        URL: .../World_Imagery/MapServer/tile/{z}/{y}/{x}  (y/x 順注意)

        ★ タイルモザイク組み立て後、DEM の正確な地理的範囲に Web Mercator で
           クロップする。これにより等高線パスと画像の座標が一致する。
           （タイルはタイル境界から始まるため、クロップなしでは x/y の
             スケールが異なり位置ずれが生じる）

        progress_callback : callable(done, total, message) | None
            タイル取得の進捗を通知する。
        """
        if hasattr(self, '_satellite_image') and self._satellite_image is not None:
            return self._satellite_image

        import rasterio
        from PIL import Image
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import urllib.request
        import math

        with rasterio.open(self._src_path) as src:
            bounds = src.bounds

        # ── 衛星タイルのズームを DEM 解像度から自動計算 ─────────────────
        # DEM の orig_shape（ダウンサンプル前ピクセル数）と bounds から
        # 1px あたりのメートル数を求め、タイル解像度が同等以上になる
        # 最小ズームを選ぶ。ESRIの実用上限を17に制限。
        if hasattr(self, '_satellite_zoom'):
            zoom = self._satellite_zoom
        else:
            orig_h, orig_w = self.orig_shape
            lon_span = bounds.right - bounds.left
            lat_center = (bounds.top + bounds.bottom) / 2
            # DEM 1px あたりの経度幅 → メートル換算
            deg_per_dem_px = lon_span / orig_w
            m_per_dem_px = deg_per_dem_px * 111320 * math.cos(math.radians(lat_center))
            TILE = 256
            # zoom z での 1px = (360/2^z * 111320 * cos(lat)) / 256 m
            # m_per_dem_px >= m_per_tile_px となる最小 z を探す
            zoom = 10  # 下限
            for z in range(10, 18):
                m_per_tile_px = (360 / (2 ** z)) * 111320 * math.cos(math.radians(lat_center)) / TILE
                if m_per_tile_px <= m_per_dem_px:
                    zoom = z
                    break
            # DEM より 1段階上にして画質に余裕を持たせる（上限17）
            zoom = min(zoom + 1, 17)
            print(f"  衛星タイル自動ズーム: {zoom} "
                  f"(DEM {m_per_dem_px:.1f} m/px → "
                  f"tile {(360/(2**zoom))*111320*math.cos(math.radians(lat_center))/TILE:.1f} m/px)")
        n    = 2 ** zoom
        TILE = 256

        def deg2num_int(lat, lon):
            """lat/lon → 整数タイル番号"""
            lat_r = math.radians(lat)
            x = int((lon + 180) / 360 * n)
            y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
            return x, y

        def deg2px_frac(lat, lon):
            """lat/lon → モザイク内の小数ピクセル座標（クロップ計算用）"""
            lat_r = math.radians(lat)
            tx_frac = (lon + 180) / 360 * n
            ty_frac = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
            px = (tx_frac - x_min) * TILE
            py = (ty_frac - y_min) * TILE
            return px, py

        x_min, y_min = deg2num_int(bounds.top,    bounds.left)
        x_max, y_max = deg2num_int(bounds.bottom, bounds.right)
        nx = x_max - x_min + 1
        ny = y_max - y_min + 1
        mosaic = Image.new('RGB', (nx * TILE, ny * TILE), (30, 30, 30))

        def fetch_tile(tx, ty):
            # ESRI World Imagery: /{z}/{y}/{x}  (y と x が逆順)
            url = (f'https://server.arcgisonline.com/ArcGIS/rest/services/'
                   f'World_Imagery/MapServer/tile/{zoom}/{ty}/{tx}')
            req = urllib.request.Request(url, headers={
                'User-Agent': 'terrain-layer-generator/1.0 (educational use)'
            })
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    return tx, ty, Image.open(r).convert('RGB').copy()
            except Exception:
                return tx, ty, None

        tiles = [(tx, ty)
                 for ty in range(y_min, y_max + 1)
                 for tx in range(x_min, x_max + 1)]

        total_tiles = len(tiles)
        done_count  = 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {pool.submit(fetch_tile, tx, ty): (tx, ty)
                    for tx, ty in tiles}
            for f in as_completed(futs):
                tx, ty, img = f.result()
                if img:
                    col = (tx - x_min) * TILE
                    row = (ty - y_min) * TILE
                    mosaic.paste(img, (col, row))
                done_count += 1
                if progress_callback:
                    progress_callback(done_count, total_tiles,
                                      f'Satellite tiles: {done_count}/{total_tiles}')

        # ── タイルモザイクを DEM の正確な地理範囲にクロップ ──────────
        # タイルはタイル境界から始まるため、モザイクは DEM より広い。
        # Web Mercator 投影で DEM の lat/lon 境界をモザイクのピクセル座標に
        # 変換し、クロップすることで等高線座標と完全に一致させる。
        crop_l, crop_t = deg2px_frac(bounds.top,    bounds.left)
        crop_r, crop_b = deg2px_frac(bounds.bottom, bounds.right)
        crop_box = (
            max(0, int(round(crop_l))),
            max(0, int(round(crop_t))),
            min(mosaic.width,  int(round(crop_r))),
            min(mosaic.height, int(round(crop_b))),
        )
        mosaic = mosaic.crop(crop_box)

        self._satellite_image = mosaic
        return mosaic


    # ──────────────────────────────────────────
    # SVG 生成
    # ──────────────────────────────────────────

    def generate_svg(self, level, output_path, scale=1.0, stroke_width=0.1,
                     simplify_eps=1.5, colormap='terrain',
                     next_level=None, next_next_level=None,
                     contour_cache=None):
        """
        1レイヤーのSVGを生成する。

        Parameters
        ----------
        next_level : float | None
            1つ上のレイヤー標高。その輪郭をジャスト赤破線で描く。
        next_next_level : float | None
            2つ上のレイヤー標高。その領域を薄グレーでインク削減。
        contour_cache : dict | None
            {level: contours} の事前計算キャッシュ。
            渡された場合は extract_contours() を呼ばず参照する。
        """
        def _get_contours(lv):
            if contour_cache is not None and lv in contour_cache:
                return contour_cache[lv]
            return self.extract_contours(lv, simplify_eps)

        contours = _get_contours(level)
        if not contours:
            return False

        h, w = self.elevation.shape
        # gsi_topoはorig_shape解像度でSVGを定義（ダウンサンプルによる画質劣化を回避）
        orig_h, orig_w = self.orig_shape
        if colormap in ('gsi_topo', 'satellite'):
            svg_h, svg_w = orig_h, orig_w
            coord_sx = orig_w / w   # 輪郭座標のスケール係数（x）
            coord_sy = orig_h / h   # 輪郭座標のスケール係数（y）
        else:
            svg_h, svg_w = h, w
            coord_sx = coord_sy = 1.0

        dwg = svgwrite.Drawing(
            str(output_path),
            size=(f'{svg_w * scale}mm', f'{svg_h * scale}mm'),
            viewBox=f'0 0 {svg_w} {svg_h}',
            profile='full'
        )

        # ── 背景 ─────────────────────────────────────────────────────
        is_base_layer = (level <= max(0.0, self.base_elev) + 0.01)
        bg_color = '#40cbc8' if is_base_layer else 'white'
        dwg.add(dwg.rect(insert=(0, 0), size=(svg_w, svg_h), fill=bg_color))

        # ── gsi_topo / satellite モード：実写タイル画像をPILで事前マスク合成して埋め込む ──
        if colormap in ('gsi_topo', 'satellite'):
            import base64, io
            from PIL import Image as PILImage
            topo_img = (self._ensure_satellite_image() if colormap == 'satellite'
                        else self._ensure_topo_image())  # tile mosaic
            tw, th = topo_img.size  # タイル合成後のオリジナル解像度

            # DEMマスク（ダウンサンプル済みh×w）をtopo解像度に拡大して合成
            if hasattr(self.elevation, 'data'):
                _data = self.elevation.data.copy()
            else:
                _data = self.elevation.copy()
            valid_mask = ~np.isnan(_data)
            binary_mask = (valid_mask & (_data >= level)).astype(np.uint8)
            binary_mask = binary_fill_holes(binary_mask).astype(np.uint8)

            if next_next_level is not None:
                nn_mask = (valid_mask & (_data >= next_next_level)).astype(np.uint8)
            else:
                nn_mask = None

            # マスクをtopo解像度に拡大（NEAREST = シャープなエッジ）
            mask_img = PILImage.fromarray(binary_mask * 255, mode='L')
            mask_img = mask_img.resize((tw, th), PILImage.NEAREST)

            # 白背景キャンバス（orig_shape解像度）に地形図を合成
            if colormap == 'satellite':
                bg_color_rgb = (10, 20, 60) if is_base_layer else (255, 255, 255)
            else:
                bg_color_rgb = (64, 203, 200) if is_base_layer else (255, 255, 255)
            canvas = PILImage.new('RGB', (tw, th), bg_color_rgb)
            canvas.paste(topo_img, mask=mask_img)

            # nn_mask領域をグレー上塗り
            if nn_mask is not None:
                nn_mask_img = PILImage.fromarray(nn_mask * 255, mode='L')
                nn_mask_img = nn_mask_img.resize((tw, th), PILImage.NEAREST)
                grey = PILImage.new('RGB', (tw, th), (240, 240, 240))
                canvas.paste(grey, mask=nn_mask_img)

            # base64に変換してSVGに埋め込む
            buf = io.BytesIO()
            canvas.save(buf, format='PNG')
            b64 = base64.b64encode(buf.getvalue()).decode()

            dwg.add(dwg.image(
                href=f'data:image/png;base64,{b64}',
                insert=(0, 0),
                size=(svg_w, svg_h),
            ))

            # 輪郭線（黒細線）と赤破線
            # 輪郭座標はDEM解像度なのでsvg_w/svg_h比率でスケールアップ
            def contours_to_paths(contour_list, fill, stroke, sw, extra=None):
                for c in contour_list:
                    pts = [(float(col) * coord_sx, float(row) * coord_sy)
                           for row, col in c]
                    if len(pts) < 3:
                        continue
                    d = 'M ' + ' L '.join(f'{x:.2f},{y:.2f}' for x, y in pts) + ' Z'
                    kw = dict(d=d, fill=fill, stroke=stroke, stroke_width=sw)
                    if extra:
                        kw.update(extra)
                    dwg.add(dwg.path(**kw))

            contours_to_paths(contours, 'none', 'black', stroke_width)

            if next_level is not None:
                next_contours = _get_contours(next_level)
                dash_len = max(stroke_width * 6, 3.0)
                contours_to_paths(next_contours, 'none', 'red',
                                  stroke_width * 1.5,
                                  {'stroke_dasharray': f'{dash_len},{dash_len}'})

            dwg.save()
            return True

        # ── 段彩色を計算 ─────────────────────────────────────────────
        import matplotlib.pyplot as plt
        cmap = plt.get_cmap(colormap)
        # terrain cmapは1.0付近が白になるため上限をクランプ。
        # topo / その他は全域を使う。
        CMAP_MAX = 0.75 if colormap == 'terrain' else 1.0
        norm_value = (level - self.min_elev) / (self.max_elev - self.min_elev) \
                     if self.max_elev > self.min_elev else 0.5
        norm_value = max(0.0, min(CMAP_MAX, norm_value))
        rgba = cmap(norm_value)
        fill_color = f'#{int(rgba[0]*255):02x}{int(rgba[1]*255):02x}{int(rgba[2]*255):02x}'

        def contours_to_paths(contour_list, fill, stroke, sw, extra=None):
            for c in contour_list:
                pts = [(float(col), float(row)) for row, col in c]
                if len(pts) < 3:
                    continue
                d = 'M ' + ' L '.join(f'{x:.2f},{y:.2f}' for x, y in pts) + ' Z'
                kw = dict(d=d, fill=fill, stroke=stroke, stroke_width=sw)
                if extra:
                    kw.update(extra)
                dwg.add(dwg.path(**kw))

        # ── (1) このレイヤーの塗り描画 ───────────────────────────────
        contours_to_paths(contours, fill_color, 'black', stroke_width, {'opacity': '0.7'})

        # ── (2) 2レイヤー上の領域を薄グレーで塗り（インク削減） ────────
        if next_next_level is not None:
            nn_contours = _get_contours(next_next_level)
            if nn_contours:
                contours_to_paths(nn_contours, '#f0f0f0', 'none', 0)

        # ── (3) 1レイヤー上の輪郭をジャスト赤破線で描画 ─────────────
        if next_level is not None:
            next_contours = _get_contours(next_level)
            dash_len = max(stroke_width * 6, 3.0)
            contours_to_paths(next_contours, 'none', 'red',
                              stroke_width * 1.5,
                              {'stroke_dasharray': f'{dash_len},{dash_len}'})

        dwg.save()
        return True

    # ──────────────────────────────────────────
    # 全レイヤー生成
    # ──────────────────────────────────────────

    def generate_all(self, output_dir='output', scale=1.0, simplify_eps=1.5,
                     colormap='terrain', progress_callback=None):
        """
        progress_callback : callable(current, total, phase) | None
            phase='contour' → 輪郭事前計算フェーズ
            phase='svg'     → SVG書き出しフェーズ
        """
        out = Path(output_dir)
        out.mkdir(exist_ok=True)

        levels = self.get_levels()
        total  = len(levels)
        print(f"\n{total}層のSVGを生成します → {out}/")
        print(f"   layer_0001 = 最下層({int(levels[0])}m以上), layer_{total:04d} = 最上層({int(levels[-1])}m以上)")

        # ── 全レベルの輪郭を事前一括計算（キャッシュ） ─────────────────
        print("輪郭を事前計算中...")
        t_pre = time.time()
        contour_cache = {}
        for idx, lv in enumerate(tqdm(levels, desc='輪郭計算', unit='層'), 1):
            contour_cache[lv] = self.extract_contours(lv, simplify_eps)
            if progress_callback:
                progress_callback(idx, total, 'contour')
        print(f"  事前計算完了: {time.time() - t_pre:.1f}秒")

        t = time.time()
        ok_count = 0

        for i, level in enumerate(tqdm(levels, desc='SVG生成', unit='層'), 1):
            path = out / f'layer_{i:04d}_{int(level)}m.svg'
            next_level      = levels[i]     if i     < len(levels) else None
            next_next_level = levels[i + 1] if i + 1 < len(levels) else None
            if self.generate_svg(level, path, scale=scale, simplify_eps=simplify_eps,
                                 colormap=colormap,
                                 next_level=next_level,
                                 next_next_level=next_next_level,
                                 contour_cache=contour_cache):
                ok_count += 1
            if progress_callback:
                progress_callback(i, total, 'svg')

        elapsed = time.time() - t
        print(f"\n✅ {ok_count}/{total}層を生成  ({elapsed:.1f}秒)")

        self._write_guide(out, levels)

    # ──────────────────────────────────────────
    # 組み立てガイド
    # ──────────────────────────────────────────

    def _write_guide(self, out_dir, levels):
        guide = out_dir / 'assembly_guide.txt'
        with open(guide, 'w', encoding='utf-8') as f:
            f.write("地形積層模型 組み立てガイド\n")
            f.write("=" * 50 + "\n\n")
            f.write(f"等高線間隔: {self.interval}m\n")
            f.write(f"レイヤー数: {len(levels)}\n")
            f.write(f"標高範囲:   {levels[0]:.1f}m ～ {levels[-1]:.1f}m\n\n")
            f.write("■ 各レイヤーの意味\n")
            f.write("  各SVGは「その標高以上の全領域」の累積シルエットです。\n")
            f.write("  下の層ほど広く、上の層ほど狭くなります。\n")
            f.write("  赤破線 = 1つ上のレイヤーの輪郭（島状切片の配置ガイド）\n\n")
            f.write("■ 材料\n")
            f.write(f"  厚紙 {len(levels)}枚（厚さ: {self.interval}mm相当）\n")
            f.write("  接着剤（木工用ボンドなど）\n\n")
            f.write("■ 組み立て手順\n")
            f.write(f"  1. 各SVGを印刷 or カッティングマシンでカット\n")
            f.write(f"  2. layer_0001（{int(levels[0])}m以上・最下層）から順に積み重ねて接着\n")
            f.write(f"  3. layer_{len(levels):04d}（{int(levels[-1])}m以上・最上層）で完成\n")
            f.write(f"  ※ 島状の切片は赤破線を位置合わせの基準にしてください\n\n")
            f.write("■ レイヤー一覧（下から順に積み重ね）\n")
            for i, lv in enumerate(levels, 1):
                marker = " ← 最下層" if i == 1 else (" ← 最上層" if i == len(levels) else "")
                f.write(f"  {i:4d}層: {lv:8.1f}m以上  layer_{i:04d}_{int(lv)}m.svg{marker}\n")
        print(f"組み立てガイド: {guide}")

    # ──────────────────────────────────────────
    # GUI互換エイリアス
    # ──────────────────────────────────────────

    def get_elevation_levels(self):
        """GUIとの互換性のため get_levels() のエイリアス"""
        return self.get_levels()

    def generate_all_layers(self, output_dir='output', scale=1.0,
                            simplify_tolerance=None, colormap='terrain',
                            progress_callback=None):
        """GUIとの互換性のため generate_all() のエイリアス"""
        eps = simplify_tolerance if simplify_tolerance is not None else self.simplify_tolerance
        self.generate_all(output_dir=output_dir, scale=scale, simplify_eps=eps,
                          colormap=colormap, progress_callback=progress_callback)

    def preview(self, colormap='terrain', output_file='preview.png', progress_callback=None):
        """標高マップのプレビュー画像を生成（SVGと同じカラーマップ範囲を使用）"""
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors

        # -9999をNaNに変換してマスク配列を作成
        data = self.elevation.data.copy()
        if self.nodata is not None:
            data[data == self.nodata] = np.nan
        data = np.ma.masked_invalid(data)

        # gsi_topo / satellite モード：実写タイル画像をプレビューに使う
        if colormap in ('gsi_topo', 'satellite'):
            try:
                from PIL import Image as _PILImage
                topo_img = (self._ensure_satellite_image(progress_callback=progress_callback)
                            if colormap == 'satellite'
                            else self._ensure_topo_image())
                tw, th = topo_img.size

                # nan_mask（elevation解像度）をtopo解像度に拡大
                nan_mask_small = np.isnan(self.elevation.data)
                nan_mask_img = _PILImage.fromarray(nan_mask_small.astype(np.uint8) * 255, mode='L')
                nan_mask_img = nan_mask_img.resize((tw, th), _PILImage.NEAREST)
                nan_mask = np.array(nan_mask_img) > 128

                topo_rgb = topo_img.copy()
                topo_arr = np.array(topo_rgb)
                nodata_color = [10, 20, 60] if colormap == 'satellite' else [64, 203, 200]
                topo_arr[nan_mask] = nodata_color

                fig, ax = plt.subplots(figsize=(10, 7))
                ax.imshow(topo_arr, aspect='equal')
                valid = data.compressed()
                mode_label = 'Satellite' if colormap == 'satellite' else 'GSI Topo'
                ax.set_title(f'{mode_label}  {np.min(valid):.1f}m - {np.max(valid):.1f}m')
                ax.axis('off')
                plt.tight_layout()
                plt.savefig(output_file, dpi=150, bbox_inches='tight')
                plt.close(fig)
                return
            except Exception as e:
                import traceback
                print(f"gsi_topo プレビューエラー: {e}")
                print(traceback.format_exc())
                colormap = 'topo'
            # end satellite/gsi_topo fallback

        fig, ax = plt.subplots(figsize=(10, 7))

        # SVGと同じCMAP_MAXを適用してカラーマップを切り詰める
        CMAP_MAX = 0.75 if colormap == 'terrain' else 1.0
        # 0.0〜CMAP_MAX の範囲だけを使う新しいcmapを生成
        base_cmap = plt.get_cmap(colormap)
        colors = base_cmap(np.linspace(0.0, CMAP_MAX, 256))
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list(
            f'{colormap}_clipped', colors, N=256
        )
        cmap.set_bad(color='#40cbc8')  # NoData（海）→ ターコイズブルー

        # 有効データの範囲でカラースケールを設定
        valid = data.compressed()
        vmin, vmax = np.percentile(valid, 2), np.percentile(valid, 98)

        im = ax.imshow(data, cmap=cmap, aspect='equal', vmin=vmin, vmax=vmax)
        plt.colorbar(im, ax=ax, label='Elevation (m)')
        ax.set_title(f'Elevation  {np.min(valid):.1f}m - {np.max(valid):.1f}m')
        ax.axis('off')
        plt.tight_layout()
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        plt.close(fig)


# ──────────────────────────────────────────────
# SVG後処理: ハッチングパターンを生XMLで注入
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# RDP 簡略化（外部ライブラリ不要）
# ──────────────────────────────────────────────

def _rdp_simplify(points, epsilon):
    """Ramer–Douglas–Peucker アルゴリズム"""
    if len(points) < 3:
        return points

    # 最遠点を探す
    start, end = points[0], points[-1]
    d = end - start
    norm = np.linalg.norm(d)

    if norm == 0:
        dists = np.linalg.norm(points - start, axis=1)
    else:
        dists = np.abs(np.cross(d, start - points)) / norm

    idx = np.argmax(dists)

    if dists[idx] > epsilon:
        left  = _rdp_simplify(points[:idx + 1], epsilon)
        right = _rdp_simplify(points[idx:],     epsilon)
        return np.vstack([left[:-1], right])
    else:
        return np.vstack([start, end])


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='GeoTIFF → 積層模型SVG生成',
        epilog="""
使用例:
  python terrain_layering.py dem.tif --interval 10
  python terrain_layering.py dem.tif --interval 20 --output layers/
  python terrain_layering.py dem.tif --interval 10 --downsample 0.5 --simplify 2.0
"""
    )
    parser.add_argument('dem_file',            help='GeoTIFFファイル')
    parser.add_argument('--interval',  type=float, default=10,  help='等高線間隔[m] (デフォルト: 10)')
    parser.add_argument('--output',    default='output',         help='出力ディレクトリ (デフォルト: output)')
    parser.add_argument('--scale',     type=float, default=1.0, help='スケール mm/px (デフォルト: 1.0)')
    parser.add_argument('--downsample',type=float, default=0.5, help='解像度係数 (デフォルト: 0.5)')
    parser.add_argument('--simplify',  type=float, default=1.5, help='輪郭簡略化許容誤差 (デフォルト: 1.5)')
    parser.add_argument('--base',      type=float, default=None,help='基準標高 (デフォルト: 自動)')
    args = parser.parse_args()

    gen = TerrainLayerGenerator(
        args.dem_file,
        interval=args.interval,
        base_elevation=args.base,
        downsample=args.downsample
    )
    gen.generate_all(
        output_dir=args.output,
        scale=args.scale,
        simplify_eps=args.simplify
    )


if __name__ == '__main__':
    main()
