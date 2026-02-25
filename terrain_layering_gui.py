#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
地形積層模型生成ツール - GUIアプリケーション
Flask内蔵による地図選択機能付き
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import scrolledtext
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import threading
from pathlib import Path
import sys
import os
import uuid
import tempfile
import webbrowser

def get_base_dir() -> Path:
    """設定ファイル・出力先など書き込みが必要なパス（常にEXEの隣）"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent

def get_resource_dir() -> Path:
    """同梱リソース（templates等）の読み取り用パス
    EXE化時: PyInstallerが展開する _MEIPASS
    通常実行時: スクリプトの場所
    """
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent

# terrain_layering.pyをインポート
sys.path.insert(0, str(get_base_dir()))
from terrain_layering import TerrainLayerGenerator

# Flask（地図選択機能用）
try:
    from flask import Flask, render_template, request, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

MAP_SERVER_PORT = 5001


class TerrainLayerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("地形積層模型生成ツール")
        # 起動時に最大化
        import platform
        if platform.system() == 'Windows':
            self.root.state('zoomed')
        else:
            self.root.attributes('-zoomed', True)

        # ── テーマ＆スタイル設定 ──────────────────────────────────────
        style = ttk.Style()
        style.theme_use('clam')   # 立体感のある標準テーマ

        # アクセントカラー定義
        ACCENT   = '#2d6a9f'   # ヘッダーブルー
        ACCENT_H = '#1a4f7a'   # ホバー時
        BG       = '#f0f0f0'
        FRAME_BG = '#e8e8e8'

        # ── クロスプラットフォーム対応フォント選択 ────────────────────
        import platform as _platform
        _sys = _platform.system()
        if _sys == 'Windows':
            UI_FONT      = 'Meiryo UI'
            UI_FONT_MONO = UI_FONT_MONO
        elif _sys == 'Darwin':
            UI_FONT      = 'Hiragino Sans'
            UI_FONT_MONO = 'Menlo'
        else:
            # Linux: fonts-noto-cjk パッケージで導入
            # sudo apt install fonts-noto-cjk
            UI_FONT      = 'Noto Sans CJK JP'
            UI_FONT_MONO = 'Monospace'

        style.configure('TFrame',       background=BG)
        style.configure('TLabel',       background=BG,      font=(UI_FONT, 9))
        style.configure('TLabelframe',  background=FRAME_BG, font=(UI_FONT, 9, 'bold'),
                        relief='groove', borderwidth=2)
        style.configure('TLabelframe.Label', background=FRAME_BG, foreground=ACCENT,
                        font=(UI_FONT, 9, 'bold'))
        style.configure('TEntry',       fieldbackground='white', relief='sunken')
        style.configure('TCombobox',    fieldbackground='white')
        style.configure('TScrollbar',   background=BG, troughcolor=FRAME_BG)

        # ボタン共通
        style.configure('TButton',
                        font=(UI_FONT, 9, 'bold'),
                        padding=(8, 5),
                        relief='raised',
                        background='#dcdcdc')
        style.map('TButton',
                  background=[('active', '#c8c8c8'), ('pressed', '#b0b0b0')],
                  relief=[('pressed', 'sunken')])

        # アクセントボタン（実行系）
        style.configure('Action.TButton',
                        font=(UI_FONT, 9, 'bold'),
                        padding=(8, 6),
                        relief='raised',
                        foreground='white',
                        background=ACCENT)
        style.map('Action.TButton',
                  background=[('active', ACCENT_H), ('pressed', '#0f3356')],
                  relief=[('pressed', 'sunken')])

        # ── matplotlib 日本語フォント設定（文字化け対策） ─────────────
        import matplotlib
        if _sys == 'Windows':
            matplotlib.rcParams['font.family'] = 'Meiryo'
        elif _sys == 'Darwin':
            matplotlib.rcParams['font.family'] = 'Hiragino Sans'
        else:
            # Linux: 優先順位付きでフォントを検索
            import matplotlib.font_manager as fm
            _pref = ['Noto Sans CJK JP', 'Noto Sans CJK', 'IPAPGothic', 'IPAGothic',
                     'TakaoGothic', 'VL Gothic', 'Droid Sans Fallback']
            _avail = {f.name for f in fm.fontManager.ttflist}
            _found = next((f for f in _pref if f in _avail), None)
            if _found:
                matplotlib.rcParams['font.family'] = _found
        matplotlib.rcParams['axes.unicode_minus'] = False

        # 変数の初期化
        self.dem_file = None
        self.dem_pixel_m = None   # GeoTIFF 1ピクセルあたりの実距離(m)
        self.generator = None
        self.preview_image = None

        # 設定ファイル（スクリプトと同じディレクトリ）
        self._config_path = get_base_dir() / 'terrain_gui_config.json'

        # UIの構築
        self.create_widgets()

        # 設定を復元（UI構築後）
        self._load_settings()

    def _config_defaults(self):
        return {
            'dem_file':    '',
            'interval':    '100',
            'base':        '',
            'colormap':    'topo',
            'elev_min':    '',
            'elev_max':    '',
            'smoothing':   '3',
            'simplify':    '1',
            'output_dir':  'output',
            'paper_size':  'A4 (210x297mm)',
            'scale':       '1.0',
        }

    def _load_settings(self):
        cfg = self._config_defaults()
        if self._config_path.exists():
            try:
                import json
                saved = json.loads(self._config_path.read_text(encoding='utf-8'))
                # 旧デフォルト 'terrain' が保存されている場合は 'topo' に移行
                if saved.get('colormap') == 'terrain':
                    saved['colormap'] = 'topo'
                cfg.update(saved)
            except Exception:
                pass
        # UIに反映
        self.interval_var.set(cfg['interval'])
        self.base_var.set(cfg['base'])
        self.colormap_var.set(cfg['colormap'])
        self.elev_min_var.set(cfg['elev_min'])
        self.elev_max_var.set(cfg['elev_max'])
        self.smoothing_var.set(cfg['smoothing'])
        self.simplify_var.set(cfg['simplify'])
        self.output_dir_var.set(cfg['output_dir'])
        self.paper_size_var.set(cfg['paper_size'])
        self.scale_var.set(cfg['scale'])
        if cfg['dem_file'] and Path(cfg['dem_file']).exists():
            self.dem_file = cfg['dem_file']
            self.file_label.config(text=Path(cfg['dem_file']).name, foreground='black')

    def _save_settings(self):
        import json
        cfg = {
            'dem_file':   self.dem_file or '',
            'interval':   self.interval_var.get(),
            'base':       self.base_var.get(),
            'colormap':   self.colormap_var.get(),
            'elev_min':   self.elev_min_var.get(),
            'elev_max':   self.elev_max_var.get(),
            'smoothing':  self.smoothing_var.get(),
            'simplify':   self.simplify_var.get(),
            'output_dir': self.output_dir_var.get(),
            'paper_size': self.paper_size_var.get(),
            'scale':      self.scale_var.get(),
        }
        try:
            self._config_path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def create_widgets(self):
        CTRL_W = 280

        # ── シンプルなpack分割 ──────────────────────────────────────────
        # left: 固定幅、right: 残り全部。PanedWindowは使わない。
        left_panel = tk.Frame(self.root, width=CTRL_W, bd=0)
        left_panel.pack(side='left', fill='y')
        left_panel.pack_propagate(False)      # 内容に引きずられない

        # ドラッグ可能なセパレータ（3px幅）
        sep = tk.Frame(self.root, width=3, bg='#888888', cursor='sb_h_double_arrow')
        sep.pack(side='left', fill='y')

        right_panel = tk.Frame(self.root, bd=0)
        right_panel.pack(side='left', fill='both', expand=True)

        # セパレータのドラッグでリサイズ
        def _on_drag(event):
            x = sep.winfo_x() + event.x
            x = max(150, min(x, self.root.winfo_width() - 200))
            left_panel.configure(width=x)
        sep.bind('<B1-Motion>', _on_drag)

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.create_control_panel(left_panel)
        self.create_preview_panel(right_panel)

    def _on_close(self):
        self._save_settings()
        self.root.destroy()

    # ──────────────────────────────────────────
    # 地図選択（Flask内蔵サーバー）
    # ──────────────────────────────────────────

    def _start_map_server(self):
        """FlaskサーバーをバックグラウンドスレッドでポートMAP_SERVER_PORTに起動"""
        if not FLASK_AVAILABLE:
            messagebox.showerror("エラー",
                "flaskがインストールされていません。\n"
                "pip install flask  を実行してください。")
            return

        if getattr(self, '_flask_running', False):
            # すでに起動済み → ブラウザだけ開く
            webbrowser.open(f'http://localhost:{MAP_SERVER_PORT}')
            return

        try:
            from download_dem import create_geotiff
        except ImportError:
            messagebox.showerror("エラー",
                "download_dem.py が見つかりません。\n"
                "同じフォルダに配置してください。")
            return

        # Flask アプリ構築
        # バンドルリソース（templates）は _MEIPASS、書き込みは exe 隣を参照
        template_dir = get_resource_dir() / 'templates'
        flask_app = Flask(
            __name__,
            template_folder=str(template_dir),
            root_path=str(get_resource_dir()),
        )
        flask_app.logger.disabled = True
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

        # EXE化時のデバッグ用：エラーをログファイルに書き出す
        _log_path = get_base_dir() / 'flask_error.log'
        logging.basicConfig(
            filename=str(_log_path),
            level=logging.ERROR,
            format='%(asctime)s %(levelname)s %(message)s',
        )

        @flask_app.errorhandler(Exception)
        def _handle_error(e):
            import traceback
            msg = traceback.format_exc()
            flask_app.logger.error(msg)
            logging.error(msg)
            return f"<pre>Internal Server Error:\n{msg}</pre>", 500

        jobs = {}   # job_id → dict

        @flask_app.route('/')
        def index():
            html_path = get_resource_dir() / 'templates' / 'map_viewer.html'
            return html_path.read_text(encoding='utf-8')

        @flask_app.route('/api/download_dem', methods=['POST'])
        def api_download_dem():
            data = request.json
            try:
                north = float(data['north'])
                south = float(data['south'])
                east  = float(data['east'])
                west  = float(data['west'])
            except Exception as e:
                return jsonify(success=False, error=str(e)), 400

            job_id = str(uuid.uuid4())
            area = abs(north - south) * abs(east - west)
            zoom = 13 if area > 0.1 else 14

            # ── 保存先: スクリプト隣の dem_downloads/ ─────────────────
            out_dir = get_base_dir() / 'dem_downloads'
            out_dir.mkdir(exist_ok=True)
            # ファイル名に座標を含めて分かりやすくする
            fname = (f"dem_N{north:.3f}_S{south:.3f}"
                     f"_E{east:.3f}_W{west:.3f}.tif")
            output_file = out_dir / fname

            jobs[job_id] = dict(status='processing', progress=0,
                                output_file=str(output_file), error=None)

            def run():
                try:
                    def on_progress(done, total):
                        jobs[job_id]['progress'] = int(done / total * 100)

                    ok = create_geotiff(
                        south, north, west, east,
                        str(output_file),
                        zoom=zoom, skip_confirm=True, max_workers=15,
                        progress_callback=on_progress,
                    )
                    jobs[job_id]['status'] = 'completed' if ok else 'failed'
                    jobs[job_id]['progress'] = 100
                    if not ok:
                        jobs[job_id]['error'] = '有効なデータが取得できませんでした'
                except Exception as e:
                    jobs[job_id]['status'] = 'failed'
                    jobs[job_id]['error'] = str(e)

            threading.Thread(target=run, daemon=True).start()
            return jsonify(success=True, job_id=job_id)

        @flask_app.route('/api/job_status/<job_id>')
        def api_job_status(job_id):
            if job_id not in jobs:
                return jsonify(success=False, error='not found'), 404
            j = jobs[job_id]
            return jsonify(success=True, status=j['status'],
                           progress=j.get('progress', 0), error=j.get('error'))

        @flask_app.route('/api/load_in_gui/<job_id>', methods=['POST'])
        def api_load_in_gui(job_id):
            if job_id not in jobs or jobs[job_id]['status'] != 'completed':
                return jsonify(success=False, error='未完了またはジョブ不明'), 400
            fp = jobs[job_id]['output_file']
            if not Path(fp).exists():
                return jsonify(success=False, error='ファイルが見つかりません'), 404
            # GUIスレッドへ通知
            self.root.after(0, lambda: self._on_dem_ready(fp))
            return jsonify(success=True)

        def _serve():
            flask_app.run(host='127.0.0.1', port=MAP_SERVER_PORT,
                          debug=False, use_reloader=False)

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        self._flask_running = True

        # 起動を少し待ってからブラウザを開く
        def _open_browser():
            import time; time.sleep(0.8)
            webbrowser.open(f'http://localhost:{MAP_SERVER_PORT}')
        threading.Thread(target=_open_browser, daemon=True).start()
        self.log(f"地図サーバー起動: http://localhost:{MAP_SERVER_PORT}")

    def _on_dem_ready(self, filepath):
        """FlaskスレッドからGUIスレッドへDEMファイルを受け渡す"""
        self.dem_file = filepath
        p = Path(filepath)
        self.file_label.config(text=f"🗾 {p.name}", foreground='#27ae60')
        self.log(f"")
        self.log(f"=== GeoTIFF 取得完了 ===")
        self.log(f"保存先: {filepath}")
        self._save_settings()
        self._load_dem_pixel_m(filepath)
        try:
            gen = TerrainLayerGenerator(filepath)
            self.log(f"標高範囲: {gen.min_elev:.0f}m ～ {gen.max_elev:.0f}m")
            self.log(f"レイヤー数: {len(gen.get_levels())}層（間隔 {int(self.interval_var.get())}m）")
        except Exception:
            pass

        self.log(f"プレビューを自動生成中...")
        # プレビューを自動起動
        self.update_preview()
        
    def create_control_panel(self, parent):
        # canvas+scrollbar を1つのFrameに収めることで
        # left_panel の境界をはみ出さないようにする
        wrap = tk.Frame(parent, bd=0)
        wrap.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(wrap, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        PX, PY = 3, 3   # 共通padding

        # === 1. ファイル選択 ===
        file_frame = ttk.LabelFrame(scrollable_frame, text="1. ファイル選択", padding=6)
        file_frame.pack(fill=tk.X, padx=PX, pady=PY)

        ttk.Button(file_frame, text="GeoTIFFを選択",
                   command=self.select_file).pack(fill=tk.X)

        ttk.Button(file_frame, text="🗾 地図から範囲選択",
                   command=self._start_map_server,
                   style="Action.TButton").pack(fill=tk.X, pady=(3, 0))

        self.file_label = ttk.Label(file_frame, text="未選択", foreground="gray",
                                    font=(UI_FONT, 8), wraplength=240)
        self.file_label.pack(fill=tk.X, pady=(3, 0))

        # === 2. 基本設定 ===
        basic_frame = ttk.LabelFrame(scrollable_frame, text="2. 基本設定", padding=6)
        basic_frame.pack(fill=tk.X, padx=PX, pady=PY)
        basic_frame.columnconfigure(1, weight=1)

        ttk.Label(basic_frame, text="標高間隔 (m):").grid(row=0, column=0, sticky=tk.W, pady=1)
        self.interval_var = tk.StringVar(value="100")
        ttk.Entry(basic_frame, textvariable=self.interval_var, width=8).grid(row=0, column=1, sticky=tk.W, pady=1)

        ttk.Label(basic_frame, text="基準標高 (m):").grid(row=1, column=0, sticky=tk.W, pady=1)
        self.base_var = tk.StringVar(value="")
        ttk.Entry(basic_frame, textvariable=self.base_var, width=8).grid(row=1, column=1, sticky=tk.W, pady=1)
        ttk.Label(basic_frame, text="空白=自動", font=("", 7), foreground='gray').grid(row=1, column=2, sticky=tk.W)

        # 縮尺・1層厚さ（GeoTIFFロード後に自動計算）
        ttk.Separator(basic_frame, orient='horizontal').grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=(4,2))
        ttk.Label(basic_frame, text="縮尺:").grid(row=3, column=0, sticky=tk.W)
        self.scale_ratio_label = ttk.Label(basic_frame, text="—", foreground='#2d6a9f', font=(UI_FONT, 9, 'bold'))
        self.scale_ratio_label.grid(row=3, column=1, columnspan=2, sticky=tk.W)

        ttk.Label(basic_frame, text="1層の厚さ:").grid(row=4, column=0, sticky=tk.W)
        self.layer_thick_label = ttk.Label(basic_frame, text="—", foreground='#2d6a9f', font=(UI_FONT, 9, 'bold'))
        self.layer_thick_label.grid(row=4, column=1, columnspan=2, sticky=tk.W)

        # interval変更時に自動再計算（scale_var/paper_size_varは後で定義されるため
        # trace登録はcreate_control_panel末尾で行う）
        self.interval_var.trace_add('write', lambda *_: self._update_scale_info())

        # === 3. 段彩設定 ===
        color_frame = ttk.LabelFrame(scrollable_frame, text="3. 段彩設定", padding=6)
        color_frame.pack(fill=tk.X, padx=PX, pady=PY)
        color_frame.columnconfigure(1, weight=1)

        ttk.Label(color_frame, text="カラーマップ:").grid(row=0, column=0, sticky=tk.W, pady=1)
        self.colormap_var = tk.StringVar(value="topo")
        ttk.Combobox(color_frame, textvariable=self.colormap_var,
                     values=["topo", "terrain", "satellite", "viridis", "plasma", "coolwarm", "jet",
                             "gsi_topo"],
                     width=10, state="readonly").grid(row=0, column=1, sticky=tk.W, pady=1)

        ttk.Label(color_frame, text="段彩範囲 (m):").grid(row=1, column=0, sticky=tk.W, pady=1)
        range_frame = ttk.Frame(color_frame)
        range_frame.grid(row=1, column=1, columnspan=2, sticky=tk.W, pady=1)
        self.elev_min_var = tk.StringVar(value="")
        self.elev_max_var = tk.StringVar(value="")
        ttk.Entry(range_frame, textvariable=self.elev_min_var, width=5).pack(side=tk.LEFT)
        ttk.Label(range_frame, text="-").pack(side=tk.LEFT, padx=2)
        ttk.Entry(range_frame, textvariable=self.elev_max_var, width=5).pack(side=tk.LEFT)
        ttk.Label(color_frame, text="空白=自動", font=("", 7), foreground='gray').grid(row=2, column=1, sticky=tk.W)

        # === 4. スムージング設定 ===
        smooth_frame = ttk.LabelFrame(scrollable_frame, text="4. スムージング設定", padding=6)
        smooth_frame.pack(fill=tk.X, padx=PX, pady=PY)

        ttk.Label(smooth_frame, text="スムージング:").grid(row=0, column=0, sticky=tk.W, pady=1)
        self.smoothing_var = tk.StringVar(value="3")
        ttk.Scale(smooth_frame, from_=0, to=10, variable=self.smoothing_var,
                  orient=tk.HORIZONTAL, length=100).grid(row=0, column=1, pady=1)
        self.smooth_label = ttk.Label(smooth_frame, text="3.0", width=4)
        self.smooth_label.grid(row=0, column=2)

        ttk.Label(smooth_frame, text="輪郭簡略化:").grid(row=1, column=0, sticky=tk.W, pady=1)
        self.simplify_var = tk.StringVar(value="1")
        ttk.Scale(smooth_frame, from_=0, to=10, variable=self.simplify_var,
                  orient=tk.HORIZONTAL, length=100).grid(row=1, column=1, pady=1)
        self.simplify_label = ttk.Label(smooth_frame, text="1.0", width=4)
        self.simplify_label.grid(row=1, column=2)

        self.smoothing_var.trace_add("write", self.update_smooth_label)
        self.simplify_var.trace_add("write", self.update_simplify_label)

        # === 5. 出力設定 ===
        output_frame = ttk.LabelFrame(scrollable_frame, text="5. 出力設定", padding=6)
        output_frame.pack(fill=tk.X, padx=PX, pady=PY)
        output_frame.columnconfigure(1, weight=1)

        ttk.Label(output_frame, text="出力先:").grid(row=0, column=0, sticky=tk.W, pady=1)
        out_row = ttk.Frame(output_frame)
        out_row.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=1)
        self.output_dir_var = tk.StringVar(value="output")
        ttk.Entry(out_row, textvariable=self.output_dir_var, width=12).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(out_row, text="…", command=self.select_output_dir, width=3).pack(side=tk.LEFT, padx=(2, 0))

        ttk.Label(output_frame, text="用紙:").grid(row=1, column=0, sticky=tk.W, pady=1)
        self.paper_size_var = tk.StringVar(value="A4")
        ttk.Combobox(output_frame, textvariable=self.paper_size_var,
                     values=["A4 (210x297mm)", "A3 (297x420mm)", "B4 (257x364mm)"],
                     width=14, state="readonly").grid(row=1, column=1, sticky=tk.W, pady=1)

        ttk.Label(output_frame, text="スケール:").grid(row=2, column=0, sticky=tk.W, pady=1)
        self.scale_var = tk.StringVar(value="1.0")
        ttk.Entry(output_frame, textvariable=self.scale_var, width=8).grid(row=2, column=1, sticky=tk.W, pady=1)
        ttk.Label(output_frame, text="mm/px", font=("", 7), foreground='gray').grid(row=2, column=2, sticky=tk.W)

        # === 実行ボタン ===
        button_frame = ttk.Frame(scrollable_frame)
        button_frame.pack(fill=tk.X, padx=PX, pady=(6, 2))

        ttk.Button(button_frame, text="プレビュー更新",
                   command=self.update_preview,
                   style="Action.TButton").pack(fill=tk.X, pady=1)
        ttk.Button(button_frame, text="SVGファイル生成",
                   command=self.generate_layers,
                   style="Action.TButton").pack(fill=tk.X, pady=1)
        ttk.Button(button_frame, text="印刷用PDF生成 & 印刷",
                   command=self.generate_and_print,
                   style="Action.TButton").pack(fill=tk.X, pady=1)
        ttk.Button(button_frame, text="PDFのみ生成",
                   command=self.generate_print_pdf,
                   style="Action.TButton").pack(fill=tk.X, pady=1)

        # === 進捗表示 ===
        progress_frame = ttk.LabelFrame(scrollable_frame, text="進捗", padding=6)
        progress_frame.pack(fill=tk.BOTH, expand=True, padx=PX, pady=PY)

        bar_frame = ttk.Frame(progress_frame)
        bar_frame.pack(fill=tk.X, pady=(0, 3))
        self.progress_var   = tk.DoubleVar(value=0)
        self.progress_phase = tk.StringVar(value="")
        self.progress_bar   = ttk.Progressbar(
            bar_frame, variable=self.progress_var,
            maximum=100, mode='determinate'
        )
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(bar_frame, textvariable=self.progress_phase,
                  width=13, font=(UI_FONT, 8)).pack(side=tk.LEFT, padx=(4, 0))

        self.progress_text = scrolledtext.ScrolledText(progress_frame, height=8, width=30,
                                                       font=(UI_FONT_MONO, 8))
        self.progress_text.pack(fill=tk.BOTH, expand=True)

        # scale_var / paper_size_var は出力設定セクションで定義済みなのでここで登録
        self.scale_var.trace_add('write',     lambda *_: self._update_scale_info())
        self.paper_size_var.trace_add('write', lambda *_: self._update_scale_info())
        
    def create_preview_panel(self, parent):
        # 外枠フレーム（bd=0で余白なし）
        outer = tk.Frame(parent, bd=0)
        outer.pack(fill=tk.BOTH, expand=True)

        # "プレビュー" ラベルを自前で配置
        header = tk.Label(outer, text="プレビュー", anchor='w',
                          font=(UI_FONT, 9, 'bold'),
                          foreground='#2d6a9f', background='#e8e8e8',
                          padx=4, pady=2)
        header.pack(fill=tk.X)

        preview_frame = tk.Frame(outer, bd=1, relief='sunken', background='white')
        preview_frame.pack(fill=tk.BOTH, expand=True)

        # 初期プレースホルダー（tkinter Label = 文字化けなし）
        self._placeholder = ttk.Label(
            preview_frame,
            text="GeoTIFFファイルを選択してください",
            font=(UI_FONT, 14),
            foreground='#999999',
            anchor='center'
        )
        self._placeholder.place(relx=0.5, rely=0.5, anchor='center')

        # Matplotlibの図（初期は非表示、プレビュー生成時に表示）
        self.fig = Figure(dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=preview_frame)
        # canvas は最初 pack しない → placeholder が見える
        self._canvas_packed = False
        self._preview_frame = preview_frame

        def _on_preview_resize(e):
            # プレースホルダーを中央に追従
            self._placeholder.place(relx=0.5, rely=0.5, anchor='center')
            # canvas表示中ならfigureサイズも更新して再描画
            if self._canvas_packed and self.preview_image is not None:
                w_in = max(e.width  / self.fig.dpi, 1)
                h_in = max(e.height / self.fig.dpi, 1)
                self.fig.set_size_inches(w_in, h_in)
                self.canvas.draw_idle()

        preview_frame.bind('<Configure>', _on_preview_resize)
        
    def update_smooth_label(self, *args):
        try:
            val = float(self.smoothing_var.get())
            self.smooth_label.config(text=f"{val:.1f}")
        except:
            pass
            
    def update_simplify_label(self, *args):
        try:
            val = float(self.simplify_var.get())
            self.simplify_label.config(text=f"{val:.1f}")
        except:
            pass
    
    def _load_dem_pixel_m(self, filepath):
        """GeoTIFFのピクセルあたり実距離(m)とピクセル数をキャッシュ"""
        try:
            import rasterio, math
            with rasterio.open(filepath) as src:
                bounds = src.bounds
                self.dem_pixel_w = src.width
                self.dem_pixel_h = src.height
            lat_c = (bounds.top + bounds.bottom) / 2
            deg_per_px_lat = (bounds.top  - bounds.bottom) / self.dem_pixel_h
            deg_per_px_lon = (bounds.right - bounds.left)  / self.dem_pixel_w
            m_per_deg_lat = 111320.0
            m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_c))
            self.dem_pixel_m = (
                deg_per_px_lat * m_per_deg_lat +
                deg_per_px_lon * m_per_deg_lon
            ) / 2
            self._update_scale_info()
        except Exception:
            self.dem_pixel_m = None
            self.dem_pixel_w = None
            self.dem_pixel_h = None

    def _update_scale_info(self):
        """縮尺と1層厚さを計算してラベルに表示（PDF印刷時のフィット縮小を考慮）"""
        if not hasattr(self, 'scale_ratio_label'):
            return
        if self.dem_pixel_m is None or self.dem_pixel_w is None:
            self.scale_ratio_label.config(text="— (GeoTIFF未選択)")
            self.layer_thick_label.config(text="—")
            return
        try:
            scale_mm = float(self.scale_var.get())   # mm/px (SVG出力スケール)
            interval_m = float(self.interval_var.get())
        except ValueError:
            return

        # 用紙サイズ（mm）
        paper_str = self.paper_size_var.get()
        if 'A3' in paper_str:
            paper_w, paper_h = 297.0, 420.0
        elif 'B4' in paper_str:
            paper_w, paper_h = 257.0, 364.0
        else:
            paper_w, paper_h = 210.0, 297.0   # A4デフォルト

        # SVGのmm寸法
        svg_w_mm = self.dem_pixel_w * scale_mm
        svg_h_mm = self.dem_pixel_h * scale_mm

        # PDF生成と同じ向き判定（アスペクト比で横/縦を決定）
        svg_aspect  = svg_w_mm / svg_h_mm
        page_aspect = paper_w  / paper_h
        if (svg_aspect > 1.0) != (page_aspect > 1.0):
            # SVGと用紙の向きが違う → 用紙を回転
            paper_w, paper_h = paper_h, paper_w

        # マージン10mm × 2 を除いた有効領域
        MARGIN = 10.0
        avail_w = paper_w - 2 * MARGIN
        avail_h = paper_h - 2 * MARGIN

        # PDF生成と同じフィット縮小率
        fit = min(avail_w / svg_w_mm, avail_h / svg_h_mm)

        # 印刷時の実効スケール: 1px が何mm になるか
        actual_mm_per_px = scale_mm * fit

        # 縮尺比: 実距離(mm) / 印刷距離(mm)
        ratio = (self.dem_pixel_m * 1000.0) / actual_mm_per_px

        # 1層の物理的厚さ: interval_m を同じ縮尺でmm換算
        thick_mm = (interval_m * 1000.0) / ratio

        # 縮尺を100の倍数で丸めて表示
        rounded = round(ratio / 100) * 100
        ratio_str = f"1 : {int(rounded):,}"

        self.scale_ratio_label.config(text=ratio_str)
        self.layer_thick_label.config(text=f"{thick_mm:.2f} mm")

    def select_file(self):
        filename = filedialog.askopenfilename(
            title="GeoTIFFファイルを選択",
            filetypes=[("GeoTIFF files", "*.tif *.tiff"), ("All files", "*.*")]
        )
        if filename:
            self.dem_file = filename
            self.file_label.config(text=Path(filename).name, foreground="black")
            self.log(f"ファイル選択: {filename}")
            self._load_dem_pixel_m(filename)
            self._save_settings()

    def select_output_dir(self):
        dirname = filedialog.askdirectory(title="出力先フォルダを選択")
        if dirname:
            self.output_dir_var.set(dirname)
            self._save_settings()
            
    def log(self, message):
        self.progress_text.insert(tk.END, message + "\n")
        self.progress_text.see(tk.END)
        self.root.update()
        
    def get_parameters(self):
        """UIから設定値を取得"""
        try:
            params = {
                'interval': float(self.interval_var.get()),
                'base_elevation': float(self.base_var.get()) if self.base_var.get() else None,
                'elev_range_min': float(self.elev_min_var.get()) if self.elev_min_var.get() else None,
                'elev_range_max': float(self.elev_max_var.get()) if self.elev_max_var.get() else None,
                'smoothing_sigma': float(self.smoothing_var.get()),
                'simplify_tolerance': float(self.simplify_var.get()),
                'colormap': self.colormap_var.get(),
                'scale': float(self.scale_var.get()),
                'output_dir': self.output_dir_var.get(),
                'paper_size': self.paper_size_var.get(),
            }
            return params
        except ValueError as e:
            messagebox.showerror("入力エラー", f"パラメータの値が不正です: {e}")
            return None
            
    def create_generator(self):
        """TerrainLayerGeneratorインスタンスを作成"""
        if not self.dem_file:
            messagebox.showwarning("警告", "GeoTIFFファイルを選択してください")
            return None
            
        params = self.get_parameters()
        if not params:
            return None
            
        try:
            self.log("ジェネレータを初期化中...")
            generator = TerrainLayerGenerator(
                self.dem_file,
                interval=params['interval'],
                base_elevation=params['base_elevation'],
                downsample=1.0,  # フル解像度で処理
                elev_range_min=params['elev_range_min'],
                elev_range_max=params['elev_range_max'],
                smoothing_sigma=params['smoothing_sigma'],
                simplify_tolerance=params['simplify_tolerance']
            )
            return generator
        except Exception as e:
            messagebox.showerror("エラー", f"ファイルの読み込みに失敗しました:\n{e}")
            return None
            
    def update_preview(self):
        """プレビューを更新"""
        generator = self.create_generator()
        if not generator:
            return

        params = self.get_parameters()

        def preview_thread():
            try:
                colormap = params['colormap']
                is_tile_mode = colormap in ('satellite', 'gsi_topo')

                self.root.after(0, lambda: (
                    self.progress_var.set(0),
                    self.progress_phase.set("タイル取得中..." if is_tile_mode else "描画中...")
                ))

                def on_tile_progress(done, total, msg):
                    pct = done / total * 100
                    self.root.after(0, lambda p=pct, m=msg: (
                        self.progress_var.set(p),
                        self.progress_phase.set(m)
                    ))

                self.log(f"プレビューを生成中 ({colormap})...")
                import tempfile
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
                temp_path = temp_file.name
                temp_file.close()

                generator.preview(
                    colormap=colormap,
                    output_file=temp_path,
                    progress_callback=on_tile_progress if is_tile_mode else None,
                )

                self.root.after(0, lambda: (
                    self.progress_var.set(100),
                    self.progress_phase.set("完了")
                ))
                self.root.after(0, lambda: self.display_preview(temp_path))

            except Exception as e:
                self.root.after(0, lambda: (
                    self.progress_phase.set("エラー"),
                    messagebox.showerror("エラー", f"プレビュー生成エラー:\n{e}")
                ))

        thread = threading.Thread(target=preview_thread, daemon=True)
        thread.start()
        
    def display_preview(self, image_path):
        """プレビュー画像を表示"""
        try:
            from PIL import Image
            img = Image.open(image_path)
            self.preview_image = img  # リサイズ判定用に保持

            # 初回: プレースホルダーを隠してcanvasを展開
            if not self._canvas_packed:
                self._placeholder.place_forget()
                self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
                self._canvas_packed = True

            # ペインの現在サイズにfigureを合わせる
            pw = self._preview_frame.winfo_width()
            ph = self._preview_frame.winfo_height()
            if pw > 10 and ph > 10:
                self.fig.set_size_inches(pw / self.fig.dpi, ph / self.fig.dpi)

            self.ax.clear()
            self.ax.imshow(img)
            self.ax.axis('off')
            self.fig.tight_layout(pad=0)
            self.canvas.draw()

            self.log("プレビュー更新完了")

            import os
            os.unlink(image_path)

        except Exception as e:
            messagebox.showerror("エラー", f"プレビュー表示エラー:\n{e}")
            
    def generate_layers(self):
        """SVGレイヤーを生成"""
        generator = self.create_generator()
        if not generator:
            return

        params = self.get_parameters()

        levels = generator.get_elevation_levels()
        result = messagebox.askyesno(
            "確認",
            f"{len(levels)}個のレイヤーを生成します。\n出力先: {params['output_dir']}\n\nよろしいですか？"
        )
        if not result:
            return

        total_layers = len(levels)

        def on_progress(current, total, phase):
            pct = current / total * 100
            label = f"輪郭計算 {current}/{total}" if phase == 'contour' else f"SVG生成 {current}/{total}"
            # GUIスレッドへ安全に転送
            self.root.after(0, lambda p=pct, l=label: (
                self.progress_var.set(p),
                self.progress_phase.set(l)
            ))

        def generate_thread():
            try:
                # リセット
                self.root.after(0, lambda: (
                    self.progress_var.set(0),
                    self.progress_phase.set("開始中...")
                ))
                self.log(f"\n=== SVGレイヤー生成開始 ===")
                self.log(f"出力先: {params['output_dir']}")

                generator.generate_all_layers(
                    output_dir=params['output_dir'],
                    scale=params['scale'],
                    colormap=params['colormap'],
                    progress_callback=on_progress,
                )

                self.root.after(0, lambda: (
                    self.progress_var.set(100),
                    self.progress_phase.set("完了")
                ))

                self.log("プレビュー画像を生成中...")
                preview_file = Path(params['output_dir']) / 'preview.png'
                generator.preview(colormap=params['colormap'], output_file=str(preview_file))

                self.log("\n=== 完了 ===")
                self.log(f"出力先: {params['output_dir']}")
                self._save_settings()
                self.root.after(0, lambda: messagebox.showinfo(
                    "完了",
                    f"SVGファイルの生成が完了しました。\n\n出力先:\n{params['output_dir']}"
                ))

            except Exception as e:
                self.root.after(0, lambda: (
                    self.progress_phase.set("エラー"),
                    messagebox.showerror("エラー", f"生成エラー:\n{e}")
                ))
                import traceback
                self.log(f"エラー: {traceback.format_exc()}")

        thread = threading.Thread(target=generate_thread, daemon=True)
        thread.start()
    
    def generate_and_print(self):
        """SVGをPDFに変換して直接印刷"""
        params = self.get_parameters()
        if not params:
            return
            
        output_dir = Path(params['output_dir'])
        svg_files = sorted(output_dir.glob('layer_*.svg'))
        
        if not svg_files:
            messagebox.showwarning(
                "警告", 
                "SVGファイルが見つかりません。\n先に「SVGファイル生成」を実行してください。"
            )
            return
        
        # 確認ダイアログ
        result = messagebox.askyesno(
            "確認",
            f"{len(svg_files)}個のレイヤーをPDF変換して印刷します。\n\n"
            f"用紙サイズ: {params['paper_size']}\n"
            f"ページ数: {len(svg_files)}ページ\n\n"
            "よろしいですか？"
        )
        
        if not result:
            return
        
        # PDFを生成してから印刷
        def generate_and_print_thread():
            # まずPDFを生成
            pdf_path = self._generate_pdf_internal(svg_files, params)
            
            if pdf_path and pdf_path.exists():
                # 生成成功したら印刷
                self.root.after(0, lambda: self._print_pdf(pdf_path))
        
        thread = threading.Thread(target=generate_and_print_thread, daemon=True)
        thread.start()
    
    def _draw_cover_page(self, c, page_w, page_h, mm, params):
        """PDF表紙ページを描画（プレビュー画像＋タイトル・縮尺・1層厚さ）"""
        import tempfile, os
        from reportlab.lib.units import mm as _mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        MARGIN = 15 * mm

        # ── 日本語対応フォントを探して登録 ──────────────────────────────
        JP_FONT = 'Helvetica-Bold'   # フォールバック
        JP_FONT_PLAIN = 'Helvetica'
        _candidates = []
        import platform
        if platform.system() == 'Windows':
            _candidates = [
                r'C:\Windows\Fonts\msgothic.ttc',
                r'C:\Windows\Fonts\meiryo.ttc',
                r'C:\Windows\Fonts\YuGothM.ttc',
            ]
        elif platform.system() == 'Darwin':
            _candidates = [
                '/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
                '/Library/Fonts/Osaka.ttf',
            ]
        else:
            _candidates = [
                '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            ]
        for _fp in _candidates:
            if os.path.exists(_fp):
                try:
                    # ttcの場合はサブフォント0番を使用
                    pdfmetrics.registerFont(TTFont('JPFont', _fp, subfontIndex=0))
                    JP_FONT = JP_FONT_PLAIN = 'JPFont'
                except Exception:
                    try:
                        pdfmetrics.registerFont(TTFont('JPFont', _fp))
                        JP_FONT = JP_FONT_PLAIN = 'JPFont'
                    except Exception:
                        pass
                if JP_FONT == 'JPFont':
                    break

        def safe_draw_string(canvas_obj, x, y, text, font, size):
            """日本語フォントがあればそのまま、なければASCII外を?に置換して描画"""
            canvas_obj.setFont(font, size)
            if font in ('Helvetica', 'Helvetica-Bold'):
                # ASCII外の文字を?に置換してクラッシュ回避
                text = ''.join(ch if ord(ch) < 128 else '?' for ch in text)
            canvas_obj.drawString(x, y, text)

        # ── プレビュー画像を背景に描画 ─────────────────────────────────
        preview_drawn = False
        if self.preview_image is not None:
            preview_tmp = None
            try:
                fd, preview_tmp = tempfile.mkstemp(suffix='.png')
                os.close(fd)
                self.preview_image.save(preview_tmp)

                img_w_px, img_h_px = self.preview_image.size
                img_aspect = img_w_px / img_h_px

                text_area_h = 55 * mm
                avail_w = page_w - 2 * MARGIN
                avail_h = page_h - 2 * MARGIN - text_area_h

                fit_w = avail_w
                fit_h = avail_w / img_aspect
                if fit_h > avail_h:
                    fit_h = avail_h
                    fit_w = avail_h * img_aspect

                img_x = (page_w - fit_w) / 2
                img_y = MARGIN

                c.drawImage(preview_tmp, img_x, img_y,
                            width=fit_w, height=fit_h,
                            preserveAspectRatio=True)
                preview_drawn = True
            except Exception as e:
                self.log(f"  表紙プレビュー画像エラー: {e}")
            finally:
                if preview_tmp and os.path.exists(preview_tmp):
                    try: os.unlink(preview_tmp)
                    except Exception: pass

        if not preview_drawn:
            # プレビューなし → グレー枠＋メッセージ
            text_area_h = 55 * mm
            avail_w = page_w - 2 * MARGIN
            avail_h = page_h - 2 * MARGIN - text_area_h
            c.setStrokeColorRGB(0.8, 0.8, 0.8)
            c.setFillColorRGB(0.96, 0.96, 0.96)
            c.rect(MARGIN, MARGIN, avail_w, avail_h, fill=1, stroke=1)
            c.setFillColorRGB(0.6, 0.6, 0.6)
            c.setFont('Helvetica', 14)
            c.drawCentredString(page_w / 2,
                                MARGIN + avail_h / 2,
                                'No preview available')

        # ── テキスト領域（上部） ────────────────────────────────────────
        scale_str = self.scale_ratio_label.cget('text') \
                    if hasattr(self, 'scale_ratio_label') else '-'
        thick_str = self.layer_thick_label.cget('text') \
                    if hasattr(self, 'layer_thick_label') else '-'

        title    = Path(params['output_dir']).name or params['output_dir']
        interval = params.get('interval', '')

        text_top = page_h - MARGIN
        line_h   = 11 * mm

        # タイトル（大）
        c.setFillColorRGB(0.1, 0.1, 0.1)
        safe_draw_string(c, MARGIN, text_top - 18 * mm, title, JP_FONT, 22)

        # 区切り線
        c.setStrokeColorRGB(0.18, 0.42, 0.62)
        c.setLineWidth(1.5)
        c.line(MARGIN, text_top - 22 * mm, page_w - MARGIN, text_top - 22 * mm)

        # 3項目
        items = [
            ("Scale",           scale_str),
            ("Layer thickness", thick_str),
            ("Interval",        f"{int(interval)}m" if interval else "-"),
        ]
        for idx, (label, value) in enumerate(items):
            y = text_top - 28 * mm - idx * line_h
            c.setFillColorRGB(0.5, 0.5, 0.5)
            safe_draw_string(c, MARGIN,           y, label, JP_FONT_PLAIN, 10)
            c.setFillColorRGB(0.1, 0.1, 0.1)
            safe_draw_string(c, MARGIN + 30 * mm, y, value, JP_FONT, 12)

    def _generate_pdf_internal(self, svg_files, params):
        """内部用：PDFを生成して返す"""
        try:
            self.log("\n=== PDF生成開始 ===")
            
            from svglib.svglib import svg2rlg
            from reportlab.graphics import renderPDF
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4, A3, B4
            from reportlab.lib.units import mm
            
            output_dir = Path(params['output_dir'])
            
            # 用紙サイズの取得
            paper_size_name = params['paper_size']
            if 'A4' in paper_size_name:
                page_size = A4
            elif 'A3' in paper_size_name:
                page_size = A3
            elif 'B4' in paper_size_name:
                page_size = B4
            else:
                page_size = A4
            
            page_width, page_height = page_size
            
            # PDFファイルパス
            pdf_path = output_dir / 'print_all_layers.pdf'
            
            # PDFを作成（最初は仮のページサイズ）
            c = canvas.Canvas(str(pdf_path), pagesize=page_size)

            # ── 表紙ページ ──────────────────────────────────────────────
            # カバーページの向きをプレビュー画像のアスペクト比に合わせる
            cover_page_width  = page_width
            cover_page_height = page_height
            if self.preview_image is not None:
                img_w, img_h  = self.preview_image.size
                img_aspect    = img_w / img_h
                cover_aspect  = cover_page_width / cover_page_height
                if (img_aspect > 1.0) != (cover_aspect > 1.0):
                    # 画像と用紙の向きが違う → 用紙を90°回転
                    cover_page_width, cover_page_height = cover_page_height, cover_page_width
            c.setPageSize((cover_page_width, cover_page_height))
            self._draw_cover_page(c, cover_page_width, cover_page_height, mm, params)
            c.showPage()

            for i, svg_file in enumerate(svg_files, 1):
                self.log(f"[{i}/{len(svg_files)}] {svg_file.name} を変換中...")
                
                try:
                    # SVGをReportLab Drawing objectに変換
                    drawing = svg2rlg(str(svg_file))
                    
                    if drawing:
                        # SVGのサイズを取得
                        svg_width = drawing.width
                        svg_height = drawing.height
                        
                        # SVGと用紙のアスペクト比を比較
                        svg_aspect = svg_width / svg_height
                        page_aspect = page_width / page_height
                        
                        # 用紙を最大限活用するために向きを決定
                        use_landscape = False
                        
                        if svg_aspect > 1.0 and page_aspect < 1.0:
                            # SVGが横長、用紙が縦長 → 用紙を横向きに
                            use_landscape = True
                        elif svg_aspect < 1.0 and page_aspect > 1.0:
                            # SVGが縦長、用紙が横長 → 用紙を縦向きに（通常と逆）
                            use_landscape = True
                        
                        # 現在のページサイズを決定
                        if use_landscape:
                            current_page_width = page_height
                            current_page_height = page_width
                            self.log(f"  → 横向き配置 ({current_page_width:.0f}x{current_page_height:.0f})")
                        else:
                            current_page_width = page_width
                            current_page_height = page_height
                        
                        # ページサイズを設定（最初のページも含む）
                        c.setPageSize((current_page_width, current_page_height))
                        
                        # マージンを考慮（各辺10mm）
                        margin = 10 * mm
                        available_width = current_page_width - 2 * margin
                        available_height = current_page_height - 2 * margin
                        
                        # アスペクト比を保ちながらフィット
                        scale_x = available_width / svg_width
                        scale_y = available_height / svg_height
                        scale = min(scale_x, scale_y)
                        
                        # スケーリング適用
                        drawing.width = svg_width * scale
                        drawing.height = svg_height * scale
                        drawing.scale(scale, scale)
                        
                        # 中央配置
                        x = (current_page_width - drawing.width) / 2
                        y = (current_page_height - drawing.height) / 2
                        
                        # PDFに描画
                        renderPDF.draw(drawing, c, x, y)
                        
                        # ページ情報を追加
                        c.setFont("Helvetica", 10)
                        c.drawString(margin, margin / 2, 
                                   f"Layer {i}/{len(svg_files)}: {svg_file.stem}")
                        
                        # 次のページへ（最後のページ以外）
                        if i < len(svg_files):
                            c.showPage()
                    else:
                        self.log(f"  警告: {svg_file.name} の変換に失敗")
                        
                except Exception as e:
                    self.log(f"  エラー: {svg_file.name} - {e}")
                    continue
            
            # PDFを保存
            c.save()
            
            self.log(f"\n=== PDF生成完了 ===")
            self.log(f"ファイル: {pdf_path}")
            self.log(f"ページ数: {len(svg_files)}")
            
            return pdf_path
            
        except ImportError:
            self.root.after(0, lambda: messagebox.showerror(
                "エラー",
                "PDF生成に必要なライブラリがインストールされていません。\n\n"
                "以下のコマンドでインストールしてください：\n"
                "pip install svglib reportlab --break-system-packages"
            ))
            return None
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror(
                "エラー",
                f"PDF生成エラー:\n{e}"
            ))
            import traceback
            self.log(f"エラー: {traceback.format_exc()}")
            return None
    
    def _print_pdf(self, pdf_path):
        """PDFを印刷"""
        import platform
        import subprocess
        
        system = platform.system()
        
        try:
            if system == 'Windows':
                # Windowsの場合：デフォルトプリンターで印刷
                import os
                os.startfile(str(pdf_path), "print")
                messagebox.showinfo(
                    "印刷開始",
                    "印刷ジョブを送信しました。\n\n"
                    "プリンターの設定を確認してください。"
                )
            elif system == 'Darwin':  # macOS
                # macOSの場合：印刷ダイアログを開く
                subprocess.run(['lpr', str(pdf_path)])
                messagebox.showinfo(
                    "印刷開始",
                    "印刷ジョブを送信しました。"
                )
            else:  # Linux
                # Linuxの場合：印刷ダイアログを開く
                try:
                    # GTK印刷ダイアログを試す
                    subprocess.run(['evince', '--print', str(pdf_path)])
                except:
                    try:
                        # 代替：lpr
                        subprocess.run(['lpr', str(pdf_path)])
                    except:
                        # 最終手段：PDFビューアーで開く
                        subprocess.run(['xdg-open', str(pdf_path)])
                
                messagebox.showinfo(
                    "印刷",
                    "印刷ダイアログを開きました。"
                )
        except Exception as e:
            messagebox.showerror(
                "エラー",
                f"印刷エラー:\n{e}\n\n"
                f"PDFファイルを手動で開いて印刷してください：\n{pdf_path}"
            )
    
    def generate_print_pdf(self):
        """全SVGを統合PDFに変換（印刷なし）"""
        params = self.get_parameters()
        if not params:
            return
            
        output_dir = Path(params['output_dir'])
        svg_files = sorted(output_dir.glob('layer_*.svg'))
        
        if not svg_files:
            messagebox.showwarning(
                "警告", 
                "SVGファイルが見つかりません。\n先に「SVGファイル生成」を実行してください。"
            )
            return
        
        # 確認ダイアログ
        result = messagebox.askyesno(
            "確認",
            f"{len(svg_files)}個のレイヤーをPDFに変換します。\n\n"
            f"用紙サイズ: {params['paper_size']}\n"
            f"出力先: {output_dir}/print_all_layers.pdf\n\n"
            "よろしいですか？"
        )
        
        if not result:
            return
        
        def pdf_thread():
            pdf_path = self._generate_pdf_internal(svg_files, params)
            
            if pdf_path and pdf_path.exists():
                self.root.after(0, lambda: messagebox.showinfo(
                    "完了",
                    f"印刷用PDFを生成しました。\n\n"
                    f"ファイル: {pdf_path.name}\n"
                    f"ページ数: {len(svg_files)}\n\n"
                    f"ファイルの場所:\n{pdf_path}"
                ))
                
                # PDFを自動で開く
                import webbrowser
                webbrowser.open(f'file://{pdf_path.absolute()}')
        
        thread = threading.Thread(target=pdf_thread, daemon=True)
        thread.start()


def main():
    root = tk.Tk()
    app = TerrainLayerGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
