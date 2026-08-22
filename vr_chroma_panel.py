"""
vr_chroma_panel.py  v1.0.0

Virtual Desktop のクロマキーパススルー用に、
「緑の板」を SteamVR オーバーレイとして空中に配置するツール。

できること:
  - コントローラーのトリガー/グリップでパネルをつかんで動かせる
  - 追従モード切り替え（空間固定 Standing / 空間固定 Raw / 頭に追従）
  - 入力の受信状況を表示する診断欄

動作の前提:
  - SteamVR が起動していること
  - Virtual Desktop 側でクロマキーパススルーを有効にしていること
    （Quest Link など、クロマキー機能のない環境では板が見えるだけです）

このツールは VRChat のファイルを一切改変せず、SteamVR の
オーバーレイ機能だけを使います。リスクは低いと考えていますが、
利用は自己責任でお願いします。

必要なもの:
    pip install openvr pillow
"""

import json
import math
import os
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import openvr
except ImportError:
    sys.exit("openvr が見つかりません。 pip install openvr")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow が見つかりません。 pip install pillow")


if getattr(sys, "frozen", False):
    # PyInstaller で exe 化した場合、__file__ は展開先の一時フォルダを指す。
    # 設定ファイルが消えてしまうので、exe 本体のある場所を基準にする。
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "chroma_panels.json")


def _pick_texture_dir():
    """SteamVR は非ASCIIパスの画像読み込みに失敗することがあるので、
    ASCII のみで書き込めるディレクトリを選ぶ。"""
    candidates = [
        os.path.join(tempfile.gettempdir(), "vr_chroma_panel"),
        os.path.join(SCRIPT_DIR, "_tex"),
        os.path.join(os.environ.get("SystemDrive", "C:") + os.sep,
                     "vr_chroma_panel_tex"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"),
                     "vr_chroma_panel_tex"),
    ]
    for d in candidates:
        try:
            d.encode("ascii")
        except UnicodeEncodeError:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return d
        except OSError:
            continue
    d = os.path.join(tempfile.gettempdir(), "vr_chroma_panel")
    os.makedirs(d, exist_ok=True)
    return d


TEX_DIR = _pick_texture_dir()

DEFAULT_PANEL = {
    "name": "keyboard",
    "color": "#00FF00",
    "x": 0.0, "y": 0.75, "z": -0.45,
    "yaw": 0.0, "pitch": -55.0, "roll": 0.0,
    "width": 0.45,      # 実寸メートル
    "aspect": 3.0,      # 横 / 縦
    "visible": True,
}

MODES = {
    "空間固定 (Standing)": "standing",
    "空間固定 (Raw / リセンター無効)": "raw",
    "頭に追従": "head",
}
MODE_LABELS = list(MODES.keys())


def _grab_button_mask():
    """つかみに使うボタンのビットマスク。

    openvr.ButtonMaskFromId はバージョンによって存在しないので、
    ボタンIDから自前でビットを立てる（マスクの定義は 1 << id）。
    """
    mask = 0
    for name in ("k_EButton_SteamVR_Trigger", "k_EButton_Grip", "k_EButton_Axis1"):
        bid = getattr(openvr, name, None)
        if bid is not None:
            mask |= (1 << int(bid))
    if mask == 0:                      # 定数名すら無い場合の保険
        mask = (1 << 33) | (1 << 2)    # Trigger | Grip
    return mask


GRAB_MASK = _grab_button_mask()


# --------------------------------------------------------------------------
# 行列ユーティリティ
#   3x4 の剛体変換を [[r00,r01,r02,tx],[...],[...]] のリストで扱う。
# --------------------------------------------------------------------------
def euler_to_rot(yaw_deg, pitch_deg, roll_deg):
    """R = Ry(yaw) * Rx(pitch) * Rz(roll)"""
    cy, sy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))

    ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rx = [[1, 0, 0], [0, cp, -sp], [0, sp, cp]]
    rz = [[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]]

    def mul3(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
                for i in range(3)]

    return mul3(mul3(ry, rx), rz)


def rot_to_euler(m):
    """euler_to_rot の逆変換。この合成順だと m[1][2] = -sin(pitch)。"""
    sp = max(-1.0, min(1.0, -float(m[1][2])))
    pitch = math.asin(sp)
    if abs(sp) < 0.9999:
        yaw = math.atan2(m[0][2], m[2][2])
        roll = math.atan2(m[1][0], m[1][1])
    else:                                    # 真上/真下でのジンバルロック
        yaw = math.atan2(-m[2][0], m[0][0])
        roll = 0.0
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def pose_to_list(m):
    """openvr の HmdMatrix34_t を Python のリストに移す。"""
    return [[float(m[i][j]) for j in range(4)] for i in range(3)]


def list_to_pose(a):
    m = openvr.HmdMatrix34_t()
    for i in range(3):
        for j in range(4):
            m[i][j] = a[i][j]
    return m


def rigid_mul(a, b):
    """3x4 同士の合成 (a の後に b、つまり a * b)。"""
    out = [[0.0] * 4 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = sum(a[i][k] * b[k][j] for k in range(3))
        out[i][3] = sum(a[i][k] * b[k][3] for k in range(3)) + a[i][3]
    return out


def rigid_inverse(a):
    """回転が正規直交である前提の高速な逆変換。"""
    out = [[0.0] * 4 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = a[j][i]                       # R^T
    for i in range(3):
        out[i][3] = -sum(a[k][i] * a[k][3] for k in range(3))   # -R^T * t
    return out


def compose(x, y, z, yaw, pitch, roll):
    r = euler_to_rot(yaw, pitch, roll)
    return [[r[0][0], r[0][1], r[0][2], float(x)],
            [r[1][0], r[1][1], r[1][2], float(y)],
            [r[2][0], r[2][1], r[2][2], float(z)]]


def decompose(a):
    yaw, pitch, roll = rot_to_euler(a)
    return a[0][3], a[1][3], a[2][3], yaw, pitch, roll


# --------------------------------------------------------------------------
# テクスチャ生成
# --------------------------------------------------------------------------
_tex_counter = [0]


def make_texture(hex_color, aspect):
    """SteamVR がファイル内容をキャッシュすることがあるので毎回別名で書き出す。"""
    _tex_counter[0] += 1
    w = 1024
    h = max(8, min(4096, int(round(w / max(0.05, float(aspect))))))
    hex_color = str(hex_color).lstrip("#")
    try:
        rgb = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        rgb = (0, 255, 0)      # config を手で書き換えて壊した時の保険
    path = os.path.join(TEX_DIR, f"panel_{os.getpid()}_{_tex_counter[0]}.png")
    Image.new("RGB", (w, h), rgb).save(path)
    return path


def cleanup_stale_textures(max_age_sec=86400):
    """前回クラッシュ・強制終了で残った古いテクスチャを起動時に掃除する。
    1日以上前のものだけ消すので、同時起動中の別インスタンスは壊さない。"""
    now = time.time()
    try:
        names = os.listdir(TEX_DIR)
    except OSError:
        return
    for f in names:
        if not (f.startswith("panel_") and f.endswith(".png")):
            continue
        p = os.path.join(TEX_DIR, f)
        try:
            if now - os.path.getmtime(p) > max_age_sec:
                os.remove(p)
        except OSError:
            pass


def cleanup_textures():
    """このプロセスが作った残骸だけ消す。他インスタンスの分は触らない。"""
    prefix = f"panel_{os.getpid()}_"
    try:
        names = os.listdir(TEX_DIR)
    except OSError:
        return
    for f in names:
        if f.startswith(prefix) and f.endswith(".png"):
            try:
                os.remove(os.path.join(TEX_DIR, f))
            except OSError:
                pass


# --------------------------------------------------------------------------
# オーバーレイ
# --------------------------------------------------------------------------
_overlay_serial = [0]


class Panel:
    def __init__(self, api, data, mode="standing"):
        self.api = api
        self.mode = mode
        self.data = dict(DEFAULT_PANEL)
        self.data.update(data or {})
        # キーを使い回すと KeyInUse になるので必ず新規発番
        _overlay_serial[0] += 1
        self.key = f"yururu.chroma.panel.{os.getpid()}.{_overlay_serial[0]}"
        self.handle = self.api.createOverlay(self.key, str(self.data["name"]))
        self.api.setOverlayAlpha(self.handle, 1.0)
        self.api.setOverlayColor(self.handle, 1.0, 1.0, 1.0)
        try:
            self.api.setOverlayCurvature(self.handle, 0.0)
        except Exception:
            pass  # 古い SteamVR には無い
        self.saved_abs = None      # 頭追従モードへ移る前の絶対座標を退避する
        self.tex_path = None
        self.apply_texture()
        self.apply_transform()
        self.apply_visibility()

    # --- 見た目 ---
    def apply_texture(self):
        old = self.tex_path
        path = make_texture(self.data["color"], self.data["aspect"])
        self.api.setOverlayFromFile(self.handle, path)
        self.tex_path = path
        self.apply_size()
        if old and old != path:
            try:
                os.remove(old)
            except OSError:
                pass

    def apply_size(self):
        self.api.setOverlayWidthInMeters(self.handle, float(self.data["width"]))

    def apply_visibility(self):
        if self.data["visible"]:
            self.api.showOverlay(self.handle)
        else:
            self.api.hideOverlay(self.handle)

    # --- 位置 ---
    def universe(self):
        if self.mode == "raw":
            return openvr.TrackingUniverseRawAndUncalibrated
        return openvr.TrackingUniverseStanding

    def matrix(self):
        d = self.data
        return compose(d["x"], d["y"], d["z"], d["yaw"], d["pitch"], d["roll"])

    def apply_transform(self):
        m = list_to_pose(self.matrix())
        if self.mode == "head":
            self.api.setOverlayTransformTrackedDeviceRelative(
                self.handle, openvr.k_unTrackedDeviceIndex_Hmd, m)
        else:
            self.api.setOverlayTransformAbsolute(self.handle, self.universe(), m)

    def set_absolute_matrix(self, mat):
        """つかんでいる最中に毎フレーム呼ぶ用。data も更新する。"""
        x, y, z, yaw, pitch, roll = decompose(mat)
        self.data.update({"x": x, "y": y, "z": z,
                          "yaw": yaw, "pitch": pitch, "roll": roll})
        self.api.setOverlayTransformAbsolute(
            self.handle, self.universe(), list_to_pose(mat))

    def set_mode(self, mode):
        self.mode = mode
        self.apply_transform()

    def destroy(self):
        try:
            self.api.destroyOverlay(self.handle)
        except Exception:
            pass
        if self.tex_path:
            try:
                os.remove(self.tex_path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App:
    SLIDERS = [
        ("x",      "左右 X (m)",        -3.0,   3.0, 0.005),
        ("y",      "高さ Y (m)",         0.0,   2.5, 0.005),
        ("z",      "前後 Z (m)",        -3.0,   3.0, 0.005),
        ("yaw",    "水平回転 Yaw",     -180.0, 180.0, 0.5),
        ("pitch",  "上下回転 Pitch",    -90.0,  90.0, 0.5),
        ("roll",   "傾き Roll",        -180.0, 180.0, 0.5),
        ("width",  "横幅 (m)",           0.05,   2.0, 0.005),
        ("aspect", "横 / 縦 比",          0.2,   8.0, 0.05),
    ]
    RANGES = {k: (lo, hi) for k, _, lo, hi, _ in SLIDERS}
    GRAB_RADIUS = 0.30      # コントローラーがこの距離以内ならつかめる (m)
    TICK_MS = 33            # 約30Hz

    def __init__(self, root):
        self.root = root
        root.title("VR クロマキーパネル  v1.0.0")

        openvr.init(openvr.VRApplication_Overlay)
        self.system = openvr.VRSystem()
        self.api = openvr.VROverlay()

        self.panels = []
        self.current = None
        self.vars = {}
        self.text_vars = {}
        self.entries = {}
        self._loading = False
        self._aspect_job = None
        self._tick_job = None
        self._grabbed = None        # (panel, 相対行列)
        self._diag_count = 0

        self.mode_var = tk.StringVar(value=MODE_LABELS[0])
        self.grab_var = tk.BooleanVar(value=True)

        self.build_ui()
        self.load_config()
        self._prev_mode = self.mode   # 座標変換の基準になるので必ず初期化する
        self.start_tick()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    @property
    def mode(self):
        return MODES.get(self.mode_var.get(), "standing")

    # ---- UI ------------------------------------------------------------
    def build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="both", expand=True)

        left = ttk.Frame(top)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(left, text="パネル一覧").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=18, height=10, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.select_panel())
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="追加", command=self.add_panel).pack(
            side="left", expand=True, fill="x")
        ttk.Button(btns, text="削除", command=self.remove_panel).pack(
            side="left", expand=True, fill="x")

        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True)

        mode_row = ttk.Frame(right)
        mode_row.pack(fill="x", pady=2)
        ttk.Label(mode_row, text="追従モード", width=12).pack(side="left")
        cb = ttk.Combobox(mode_row, values=MODE_LABELS, state="readonly",
                          textvariable=self.mode_var)
        cb.pack(side="left", fill="x", expand=True)
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_mode_change())

        name_row = ttk.Frame(right)
        name_row.pack(fill="x", pady=2)
        ttk.Label(name_row, text="名前", width=12).pack(side="left")
        self.name_var = tk.StringVar()
        e = ttk.Entry(name_row, textvariable=self.name_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<KeyRelease>", lambda ev: self.on_name_change())

        color_row = ttk.Frame(right)
        color_row.pack(fill="x", pady=2)
        ttk.Label(color_row, text="色 (HEX)", width=12).pack(side="left")
        self.color_var = tk.StringVar()
        ce = ttk.Entry(color_row, textvariable=self.color_var)
        ce.pack(side="left", fill="x", expand=True)
        ce.bind("<Return>", lambda ev: self.on_color_change())
        ttk.Button(color_row, text="適用",
                   command=self.on_color_change).pack(side="left")

        for key, label, lo, hi, step in self.SLIDERS:
            row = ttk.Frame(right)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=16).pack(side="left")
            var = tk.DoubleVar()
            self.vars[key] = var
            s = ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal",
                          command=lambda v, k=key: self.on_slider(k))
            s.pack(side="left", fill="x", expand=True)
            txt = tk.StringVar(value="0.000")
            self.text_vars[key] = txt
            ent = ttk.Entry(row, width=9, justify="right", textvariable=txt)
            ent.pack(side="left")
            self.entries[key] = ent
            ent.bind("<Return>",   lambda ev, k=key: self.commit_entry(k))
            ent.bind("<KP_Enter>", lambda ev, k=key: self.commit_entry(k))
            ent.bind("<FocusOut>", lambda ev, k=key: self.commit_entry(k))
            ent.bind("<Escape>",   lambda ev, k=key: self.revert_entry(k))
            ent.bind("<Up>",    lambda ev, k=key, st=step: self.nudge(k,  st))
            ent.bind("<Down>",  lambda ev, k=key, st=step: self.nudge(k, -st))
            ent.bind("<Prior>", lambda ev, k=key, st=step: self.nudge(k,  st * 10))
            ent.bind("<Next>",  lambda ev, k=key, st=step: self.nudge(k, -st * 10))

            var.trace_add("write", lambda *a, k=key: self.sync_entry(k))
            s.bind("<Button-1>", lambda ev, w=s: w.focus_set(), add="+")
            s.bind("<Left>",  lambda ev, k=key, st=step: self.nudge(k, -st))
            s.bind("<Right>", lambda ev, k=key, st=step: self.nudge(k,  st))

        self.visible_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="表示する", variable=self.visible_var,
                        command=self.on_visible).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(right, text="コントローラーでつかんで動かす",
                        variable=self.grab_var).pack(anchor="w")

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=6)
        ttk.Button(actions, text="HMDの正面へ",
                   command=self.place_in_front).pack(side="left", expand=True, fill="x")
        ttk.Button(actions, text="保存",
                   command=self.save_config).pack(side="left", expand=True, fill="x")

        self.status = ttk.Label(self.root, text="", anchor="w", padding=(8, 2))
        self.status.pack(fill="x")
        self.diag = ttk.Label(self.root, text="", anchor="w", padding=(8, 0, 8, 6),
                              foreground="#555")
        self.diag.pack(fill="x")

    # ---- 一覧 ----------------------------------------------------------
    def refresh_list(self, select=None):
        self.listbox.delete(0, "end")
        for p in self.panels:
            mark = "" if p.data["visible"] else "  (非表示)"
            self.listbox.insert("end", str(p.data["name"]) + mark)
        if select is not None and 0 <= select < len(self.panels):
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(select)
            self.select_panel()
        elif not self.panels:
            self.current = None
            self._loading = True
            self.name_var.set("")
            self.color_var.set("")
            self._loading = False
            self.status.config(text="パネルがありません。「追加」で作成してください")

    def select_panel(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.current = self.panels[sel[0]]
        self.sync_sliders()

    def sync_sliders(self):
        if not self.current:
            return
        self._loading = True
        d = self.current.data
        self.name_var.set(d["name"])
        self.color_var.set(d["color"])
        for key, *_ in self.SLIDERS:
            lo, hi = self.RANGES[key]
            self.vars[key].set(max(lo, min(hi, float(d[key]))))
            self.sync_entry(key, force=True)
        self.visible_var.set(bool(d["visible"]))
        self._loading = False

    def add_panel(self):
        data = dict(DEFAULT_PANEL)
        data["name"] = f"panel{len(self.panels) + 1}"
        try:
            p = Panel(self.api, data, self.mode)
        except Exception as ex:
            messagebox.showerror("パネルを作れません", f"{type(ex).__name__}: {ex}")
            return
        self.panels.append(p)
        self.refresh_list(select=len(self.panels) - 1)

    def remove_panel(self):
        if not self.current:
            return
        if self._grabbed and self._grabbed[0] is self.current:
            self._grabbed = None
        idx = self.panels.index(self.current)
        self.current.destroy()
        self.panels.pop(idx)
        self.current = None
        if self.panels:
            self.refresh_list(select=min(idx, len(self.panels) - 1))
        else:
            self.refresh_list()

    # ---- 値変更 --------------------------------------------------------
    def universe_transform(self, from_mode, to_mode):
        """Standing 座標と Raw 座標の間の変換行列を求める。

        同じ HMD を両方の座標系で取得すれば、その差が座標系同士のズレになる。
        """
        uni = {"standing": openvr.TrackingUniverseStanding,
               "raw": openvr.TrackingUniverseRawAndUncalibrated}
        try:
            src = self.system.getDeviceToAbsoluteTrackingPose(
                uni[from_mode], 0, openvr.k_unMaxTrackedDeviceCount)
            dst = self.system.getDeviceToAbsoluteTrackingPose(
                uni[to_mode], 0, openvr.k_unMaxTrackedDeviceCount)
        except Exception:
            return None
        h_src = src[openvr.k_unTrackedDeviceIndex_Hmd]
        h_dst = dst[openvr.k_unTrackedDeviceIndex_Hmd]
        if not (h_src.bPoseIsValid and h_dst.bPoseIsValid):
            return None
        return rigid_mul(pose_to_list(h_dst.mDeviceToAbsoluteTracking),
                         rigid_inverse(pose_to_list(h_src.mDeviceToAbsoluteTracking)))

    def on_mode_change(self):
        prev, new = self._prev_mode, self.mode
        self._prev_mode = new
        self._grabbed = None
        if prev == new:
            return

        note = ""
        abs_modes = ("standing", "raw")

        if prev in abs_modes and new in abs_modes:
            # 座標系が違うので、そのまま入れると板が飛ぶ。変換して引き継ぐ。
            t = self.universe_transform(prev, new)
            if t is None:
                note = "（HMDが取れず座標変換できませんでした。位置を確認してください）"
            else:
                for p in self.panels:
                    x, y, z, yaw, pitch, roll = decompose(rigid_mul(t, p.matrix()))
                    p.data.update({"x": x, "y": y, "z": z,
                                   "yaw": yaw, "pitch": pitch, "roll": roll})

        elif new == "head":
            # 絶対座標を頭相対に持ち込むと遠方へ飛ぶので、元の値を退避して既定位置へ
            for p in self.panels:
                p.saved_abs = dict(p.data)
                p.data.update({"x": 0.0, "y": -0.12, "z": -0.55,
                               "yaw": 0.0, "pitch": 0.0, "roll": 0.0})
            note = "（このモードではつかめません）"

        elif prev == "head":
            for p in self.panels:
                if getattr(p, "saved_abs", None):
                    p.data.update(p.saved_abs)
                    p.saved_abs = None

        for p in self.panels:
            p.set_mode(new)
        self.sync_sliders()
        self.status.config(text=f"追従モード: {self.mode_var.get()} {note}")

    def on_slider(self, key):
        if self._loading or not self.current:
            return
        self.current.data[key] = float(self.vars[key].get())
        if key in ("x", "y", "z", "yaw", "pitch", "roll"):
            self.current.apply_transform()
        elif key == "width":
            self.current.apply_size()
        elif key == "aspect":
            if self._aspect_job is not None:
                try:
                    self.root.after_cancel(self._aspect_job)
                except Exception:
                    pass
            panel = self.current
            self._aspect_job = self.root.after(
                180, lambda p=panel: self._deferred_texture(p))

    def _deferred_texture(self, panel):
        self._aspect_job = None
        if panel in self.panels:
            panel.apply_texture()

    def sync_entry(self, key, force=False):
        """スライダー側の値を入力欄へ反映する。入力中は上書きしない。"""
        ent = self.entries.get(key)
        if ent is None:
            return
        if not force:
            try:
                if self.root.focus_get() is ent:
                    return
            except (KeyError, tk.TclError):
                pass
        self.text_vars[key].set(f"{self.vars[key].get():.3f}")

    def revert_entry(self, key):
        self.sync_entry(key, force=True)
        return "break"

    def commit_entry(self, key):
        """入力欄の値を確定する。数値でなければ元に戻す。"""
        raw = self.text_vars[key].get().strip().replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            self.sync_entry(key, force=True)
            self.status.config(text="数値として読めない入力だったので元に戻しました")
            return "break"
        lo, hi = self.RANGES[key]
        clamped = max(lo, min(hi, val))
        self.vars[key].set(clamped)
        self.sync_entry(key, force=True)
        self.on_slider(key)
        if abs(clamped - val) > 1e-9:
            self.status.config(text=f"入力できる範囲は {lo} 〜 {hi} です")
        return "break"

    def nudge(self, key, step):
        if not self.current:
            return "break"
        lo, hi = self.RANGES[key]
        self.vars[key].set(max(lo, min(hi, self.vars[key].get() + step)))
        self.sync_entry(key, force=True)
        self.on_slider(key)
        return "break"

    def on_name_change(self):
        if self._loading or not self.current:
            return
        self.current.data["name"] = self.name_var.get()
        idx = self.panels.index(self.current)
        mark = "" if self.current.data["visible"] else "  (非表示)"
        self.listbox.delete(idx)
        self.listbox.insert(idx, self.name_var.get() + mark)
        self.listbox.selection_set(idx)

    def on_color_change(self):
        if not self.current:
            return
        c = self.color_var.get().strip().lstrip("#")
        if len(c) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in c):
            messagebox.showerror("色が不正", "00FF00 のように6桁で入力してください")
            self.color_var.set(self.current.data["color"])
            return
        self.current.data["color"] = "#" + c.upper()
        self.color_var.set("#" + c.upper())
        self.current.apply_texture()

    def on_visible(self):
        if self._loading or not self.current:
            return
        self.current.data["visible"] = bool(self.visible_var.get())
        self.current.apply_visibility()
        self.refresh_list(select=self.panels.index(self.current))

    # ---- トラッキング --------------------------------------------------
    def poses(self):
        universe = (openvr.TrackingUniverseRawAndUncalibrated
                    if self.mode == "raw" else openvr.TrackingUniverseStanding)
        return self.system.getDeviceToAbsoluteTrackingPose(
            universe, 0, openvr.k_unMaxTrackedDeviceCount)

    def controllers(self, poses):
        """(index, 3x4行列, つかみ中か, 生の押下ビット) のリスト。"""
        out = []
        for i in range(openvr.k_unMaxTrackedDeviceCount):
            try:
                if self.system.getTrackedDeviceClass(i) != \
                        openvr.TrackedDeviceClass_Controller:
                    continue
            except Exception:
                continue
            pose = poses[i]
            if not pose.bDeviceIsConnected or not pose.bPoseIsValid:
                continue
            bits = self.button_bits(i)
            pressed = bool(bits and (bits & GRAB_MASK))
            out.append((i, pose_to_list(pose.mDeviceToAbsoluteTracking),
                        pressed, bits))
        return out

    def button_bits(self, index):
        """レガシー入力APIで押下ビットを取る。取れなければ None。

        SteamVR Input が有効だと常に 0 が返ることがある（診断欄で判別できる）。
        """
        try:
            res = self.system.getControllerState(index)
        except Exception:
            return None
        state = res[1] if isinstance(res, tuple) else res
        if state is None:
            return None
        try:
            return int(state.ulButtonPressed)
        except Exception:
            return None

    def start_tick(self):
        self._tick_job = self.root.after(self.TICK_MS, self.tick)

    def tick(self):
        try:
            self.update_grab()
        except Exception as ex:
            self.diag.config(text=f"トラッキング取得エラー: {type(ex).__name__}: {ex}")
        self._tick_job = self.root.after(self.TICK_MS, self.tick)

    def update_grab(self):
        poses = self.poses()
        ctrls = self.controllers(poses)

        # 診断欄は 10 tick に 1 回だけ更新（点滅防止）
        self._diag_count += 1
        if self._diag_count % 10 == 0:
            hmd = poses[openvr.k_unTrackedDeviceIndex_Hmd]
            hmd_ok = "OK" if hmd.bPoseIsValid else "NG"
            if ctrls:
                parts = []
                for i, _, p, bits in ctrls:
                    if bits is None:
                        parts.append(f"#{i}:入力取得不可")
                    else:
                        parts.append(f"#{i}:{'押下' if p else '-'}(0x{bits:X})")
                btn = ", ".join(parts)
            else:
                btn = "検出なし"
            self.diag.config(text=f"HMD:{hmd_ok}  コントローラー: {btn}")

        if not self.grab_var.get() or self.mode == "head":
            self._grabbed = None
            return

        # つかんでいる最中
        if self._grabbed:
            panel, rel, idx = self._grabbed
            match = [c for c in ctrls if c[0] == idx]
            if not match or not match[0][2] or panel not in self.panels:
                self._grabbed = None
                self.sync_sliders()
                self.status.config(text="パネルを離しました")
                return
            panel.set_absolute_matrix(rigid_mul(match[0][1], rel))
            return

        # つかみ開始の判定
        for idx, cmat, pressed, _bits in ctrls:
            if not pressed:
                continue
            cpos = (cmat[0][3], cmat[1][3], cmat[2][3])
            best, best_d = None, self.GRAB_RADIUS
            for p in self.panels:
                if not p.data["visible"]:
                    continue
                pm = p.matrix()
                d = math.dist(cpos, (pm[0][3], pm[1][3], pm[2][3]))
                if d < best_d:
                    best, best_d = p, d
            if best is not None:
                rel = rigid_mul(rigid_inverse(cmat), best.matrix())
                self._grabbed = (best, rel, idx)
                self.current = best
                self.refresh_list(select=self.panels.index(best))
                self.status.config(text=f"「{best.data['name']}」をつかんでいます")
                return

    # ---- HMD 正面へ ----------------------------------------------------
    def place_in_front(self):
        if not self.current:
            return
        d = self.current.data
        if self.mode == "head":
            # このモードの座標は HMD からの相対値。絶対座標を入れると遠方へ飛ぶ。
            d.update({"x": 0.0, "y": -0.12, "z": -0.55,
                      "yaw": 0.0, "pitch": 0.0, "roll": 0.0})
            self.current.apply_transform()
            self.sync_sliders()
            self.status.config(text="視界の 55cm 前に配置しました（頭に追従）")
            return
        poses = self.poses()
        hmd = poses[openvr.k_unTrackedDeviceIndex_Hmd]
        if not hmd.bDeviceIsConnected or not hmd.bPoseIsValid:
            self.status.config(text="HMDの位置が取れません。SteamVRの状態を確認してください")
            return
        m = pose_to_list(hmd.mDeviceToAbsoluteTracking)
        fwd = (-m[0][2], -m[1][2], -m[2][2])   # HMD の前方は -Z
        dist = 0.55
        d["x"] = m[0][3] + fwd[0] * dist
        d["y"] = m[1][3] + fwd[1] * dist
        d["z"] = m[2][3] + fwd[2] * dist
        d["yaw"], d["pitch"], d["roll"] = rot_to_euler(m)
        self.current.apply_transform()
        self.sync_sliders()
        self.status.config(text="HMDの正面 55cm に配置しました")

    # ---- 保存 / 読み込み -----------------------------------------------
    def save_config(self):
        payload = {"mode": self.mode_var.get(),
                   "panels": [p.data for p in self.panels]}
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as ex:
            self.status.config(text=f"保存に失敗: {ex}")
            return
        self.status.config(text=f"保存しました: {CONFIG_PATH}")

    def load_config(self):
        raw = None
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError) as ex:
                self.status.config(text=f"config を読めませんでした: {ex}")
        if isinstance(raw, dict):                 # 新形式
            if raw.get("mode") in MODE_LABELS:
                self.mode_var.set(raw["mode"])
            items = raw.get("panels") or []
        elif isinstance(raw, list):               # 旧形式
            items = raw
        else:
            items = []
        if not items:
            items = [dict(DEFAULT_PANEL)]
        for d in items:
            if not isinstance(d, dict):
                continue
            try:
                self.panels.append(Panel(self.api, d, self.mode))
            except Exception as ex:
                self.status.config(text=f"パネルの復元に失敗: {ex}")
        self.refresh_list(select=0 if self.panels else None)

    def on_close(self):
        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except Exception:
                pass
        for p in self.panels:
            p.destroy()
        try:
            openvr.shutdown()
        except Exception:
            pass
        cleanup_textures()
        self.root.destroy()


def main():
    cleanup_stale_textures()
    root = tk.Tk()
    try:
        App(root)
    except Exception as ex:
        root.withdraw()
        messagebox.showerror(
            "起動できません",
            "SteamVR が起動しているか確認してください。\n"
            "（SteamVR を先に立ち上げてから、もう一度 run.bat を実行）\n\n"
            f"{type(ex).__name__}: {ex}")
        return
    try:
        root.mainloop()
    except Exception:
        # exe 版はコンソールが無いため、原因が分かるようログに残す
        import traceback
        detail = traceback.format_exc()
        try:
            with open(os.path.join(SCRIPT_DIR, "error_log.txt"),
                      "a", encoding="utf-8") as f:
                f.write(detail + "\n")
        except OSError:
            pass
        messagebox.showerror(
            "エラー",
            "予期しないエラーが発生しました。\n"
            "error_log.txt を添えて作者に報告してください。\n\n" + detail[-500:])


if __name__ == "__main__":
    main()
