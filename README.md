# 地形積層模型生成ツール（国内版）
### Terrain Layering Model Generator — Japan Edition

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

> **国際版はこちら →** [terrain-layering-model](https://github.com/hoshigarasu/terrain-layering-model)

---

地図模型は3Dデータから3Dプリンターでプリントアウトする時代だが、デジタルデータから極めてアナログな手仕事で工作するのも良いものだと思う。子どもたちのGIS教育にも役立ってくれると嬉しい。

---

## 日本語

### これは何？

国土地理院の標高タイルを使って**物理的な積層地形模型**を生成するデスクトップツールです。等高線ごとに切り出したシートを積み重ねることで、レーザーカッターや手切りで3D地形模型を作ることができます。

国内版は国土地理院（GSI）の高精度DEMデータ（1〜5m解像度）に最適化されています。

![渋谷周辺 積層モデルアニメーション](docs/shibuya.gif)
*渋谷駅周辺 — 地理院地図モード*

---

### 国際版との主な違い

| 機能 | 国内版（本リポジトリ） | 国際版 |
|---|---|---|
| DEMソース | 国土地理院 標高タイル | SRTM / Copernicus DEM |
| DEM解像度 | 1〜5 m | 約30 m |
| 地図UI言語 | 日本語 | 英語 |
| 地図レイヤー | 地理院標準・淡色・陰影起伏 | ESRI Topo・Relief・Street |
| 衛星写真 | ESRI World Imagery | ESRI World Imagery |
| 対応範囲 | 日本国内のみ | 全世界 |

---

### 機能

- **GUIアプリ** — 基本操作にコマンドラインは不要
- **地図から範囲選択** — ウェブ地図上で矩形を描くだけでDEMを自動取得
- **衛星写真オーバーレイ** — ESRI衛星写真を各レイヤーに合成
- **ズーム自動計算** — 衛星タイルの解像度をDEM解像度に自動マッチング
- **SVG + PDF書き出し** — 表紙・レイヤー番号付きの印刷用PDF
- **大容量SVG対応** — PIL ラスタライザによるフォールバックで描画失敗なし
- **各種パラメータ調整** — 等高線間隔・スムージング・簡略化・用紙サイズ・縮尺

---

### 模型制作の全体フロー

```
1. アプリを起動
        ↓
2. 地図セレクターを開く → 範囲を選択 → DEM（GeoTIFF）をダウンロード
        ↓
3. GeoTIFFを読み込む → 等高線間隔・カラーマップを設定
        ↓
4. プレビュー確認 → SVGレイヤーを一括生成
        ↓
5. PDFを書き出す → 厚紙に印刷
        ↓
6. 等高線に沿って切り抜く
        ↓
7. 最下層（最大）から最上層（最小）へ順に重ねる
        ↓
8. 地形積層模型の完成 ✓
```

---

### 必要環境

```
Python 3.9 以上
```

```
numpy
rasterio
scipy
Pillow
svgwrite
matplotlib
flask
svglib
reportlab
tqdm
requests
```

一括インストール：

```bash
pip install -r requirements.txt
```

Raspberry Pi / Linux環境の場合：

```bash
pip install -r requirements.txt --break-system-packages
```

---

### インストール

```bash
git clone https://github.com/hoshigarasu/terrain-layering-model-jp.git
cd terrain-layering-model-jp
pip install -r requirements.txt
```

---

### 使い方

**GUIを起動：**

```bash
python terrain_layering_gui.py
```

**ステップ1 — 地図から範囲を選択**

**「地図から範囲選択」** をクリックするとブラウザでLeaflet地図が開きます。対象エリアを矩形で描いて **「DEM取得」** をクリック。完了後 **「GUIで使用」** をクリックします。

地図レイヤーは以下から選択できます：

| レイヤー | 用途 |
|---|---|
| 🗾 地理院標準 | 等高線・地名・道路の確認 |
| 📋 地理院淡色 | 視認性重視 |
| 🛰 衛星写真 | 地表面・植生の確認（デフォルト） |
| 🏔 地理院陰影起伏 | 地形の凹凸を直感的に把握 |
| 📏 等高線 | 標高インターバルの確認 |

**ステップ2 — パラメータを設定**

| パラメータ | 説明 | 推奨値 |
|---|---|---|
| 標高間隔 (m) | 1層あたりの標高差 | 50〜200 m |
| カラーマップ | レイヤーの塗り方 | `satellite`（実写） |
| スムージング | 標高データのガウスぼかし | 1.0〜3.0 |
| 輪郭簡略化 | RDPパス削減 | 0.5〜2.0 |
| 用紙サイズ | 出力ページサイズ | A4 / A3 |
| スケール (mm/px) | 出力の物理サイズ | 1.0 |

**ステップ3 — 生成**

**「プレビュー更新」** で確認後、**「SVGファイル生成」** で全レイヤーを書き出し、**「PDFのみ生成」** で印刷用PDFを出力します。

---

### 出力ファイル

```
output/
├── print_all_layers.pdf       ← これを印刷（表紙＋全レイヤー）
├── layer_0001_XXXM.svg
├── layer_0002_XXXM.svg
└── ...
```

---

### DEMデータについて

国土地理院の標高タイルを使用しています。

- DEM5A（レーザー測量）: 約1 m解像度
- DEM5B（写真測量）: 約5 m解像度
- DEM10B（広域）: 約10 m解像度

取得時は上位ソースから優先的にダウンロードし、欠損ピクセルを下位ソースで補完します。

出典：[国土地理院](https://www.gsi.go.jp/) 標高タイル

---

### 模型組み立てのコツ

- **0.5〜1.0 mm の厚紙**に印刷すると立体感が出やすい
- レーザーカッターが最適。カッターナイフによる手切りも可
- 各レイヤーの**赤い破線**は一つ上のレイヤーの輪郭を示す位置合わせガイド
- レイヤー間の接着はスプレーのりか両面テープが扱いやすい
- 海域部分の台座には青いアクリル板やスチレンボードが見栄えよく仕上がる

---

### ライセンス

MIT License — [LICENSE](LICENSE) 参照

- 標高データ：[国土地理院](https://www.gsi.go.jp/)（測量法に基づく使用）
- 衛星写真：[ESRI World Imagery](https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9)（[利用規約](https://www.esri.com/en-us/legal/terms/full-master-agreement)参照）

---
---

## English

### What is this?

A desktop tool optimized for **Japan** that converts Digital Elevation Model data from the Geospatial Information Authority of Japan (GSI) into physical layered terrain models.

> **Global version (worldwide coverage) →** [terrain-layering-model](https://github.com/hoshigarasu/terrain-layering-model)

We live in an era where terrain models can be 3D-printed directly from digital data. But there is something deeply satisfying about crafting a physical model by hand from that same data. We hope this tool also finds use in GIS education for children.

---

### Key Differences from the Global Version

| Feature | Japan Edition (this repo) | Global Edition |
|---|---|---|
| DEM source | GSI Elevation Tiles | SRTM / Copernicus DEM |
| DEM resolution | 1–5 m | ~30 m |
| Map UI language | Japanese | English |
| Map layers | GSI Standard, Pale, Hillshade | ESRI Topo, Relief, Street |
| Satellite imagery | ESRI World Imagery | ESRI World Imagery |
| Coverage | Japan only | Worldwide |

---

### Installation

```bash
git clone https://github.com/hoshigarasu/terrain-layering-model-jp.git
cd terrain-layering-model-jp
pip install -r requirements.txt
```

**Launch:**

```bash
python terrain_layering_gui.py
```

---

### Data Sources

- Elevation: [Geospatial Information Authority of Japan](https://www.gsi.go.jp/) — DEM5A/5B/10B tiles
- Satellite imagery: [ESRI World Imagery](https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9)

### License

MIT License. See [LICENSE](LICENSE) for details.
