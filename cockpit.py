import customtkinter as ctk
import pydicom
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import os, shutil, glob, sys, json, yaml
import threading
from backend.engine import QAEngine
from backend.dicom_sender import send_dicom_series

# Load configuration
def normalise_storage_path(path: str) -> str:
    """
    Normalise the storage path, falling back to mapped drive letter on Windows if needed.
    """
    if not path:
        return "./data/rtct"
    if os.name == 'nt':
        norm = os.path.normpath(path)
        unc_prefix = "\\\\imgserver\\DICOM"
        if norm.startswith(unc_prefix):
            try:
                # If network path is accessible directly, use it
                if os.path.exists(unc_prefix):
                    return norm
            except Exception:
                pass
            
            # Try S: drive fallback if UNC path is not accessible but S: exists
            s_fallback = norm.replace(unc_prefix, "S:")
            try:
                if os.path.exists("S:\\") or os.path.exists("S:"):
                    return s_fallback
            except Exception:
                pass
    return path

try:
    with open("webApp.yaml", "r") as f:
        config_web = yaml.safe_load(f)
    STORAGE_DIR = normalise_storage_path(config_web.get("backend", {}).get("storage", {}).get("path", "./data/rtct"))
except Exception as e:
    print(f"Error loading webApp.yaml: {e}")
    STORAGE_DIR = "./data/rtct"

try:
    with open("ctqa.yaml", "r") as f:
        config_ctqa = yaml.safe_load(f)
    METAL_THRESHOLD = config_ctqa.get("thresholds", {}).get("implants", {}).get("metal_threshold_hu", 2000)
except Exception:
    METAL_THRESHOLD = 2000

EXPORT_DIR = "./TPS_EXPORT"
os.makedirs(EXPORT_DIR, exist_ok=True)

class DICOMViewer(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.files = []
        self.current_index = 0
        self.window_width = 1000 # Default
        self.window_level = 0    # Default
        
        self.fig, self.ax = plt.subplots(figsize=(5, 5), facecolor='#1a1a1a')
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        
        # Slider for navigation
        self.slider = ctk.CTkSlider(self, from_=0, to=100, command=self._on_slider_move)
        self.slider.pack(fill="x", padx=20, pady=10)
        self.slider.set(0)

        # Bind mouse wheel
        self.canvas.get_tk_widget().bind("<MouseWheel>", self._on_mousewheel)

    def load_series(self, files):
        self.files = sorted(files) # Sort by filename (usually contains instance number)
        if self.files:
            self.slider.configure(from_=0, to=len(self.files) - 1)
            self.display_slice(len(self.files) // 2)

    def set_window_level(self, width, level):
        self.window_width = width
        self.window_level = level
        self.display_slice(self.current_index)

    def display_slice(self, index):
        if not self.files: return
        self.current_index = int(index)
        self.slider.set(self.current_index)
        
        try:
            ds = pydicom.dcmread(self.files[self.current_index])
            img = ds.pixel_array
            
            # Apply rescale
            rescale_slope = getattr(ds, 'RescaleSlope', 1.0)
            rescale_intercept = getattr(ds, 'RescaleIntercept', 0.0)
            img = img * rescale_slope + rescale_intercept

            # Calculate vmin/vmax
            vmin = self.window_level - (self.window_width / 2)
            vmax = self.window_level + (self.window_width / 2)

            self.ax.clear()
            self.ax.imshow(img, cmap='gray', vmin=vmin, vmax=vmax)
            
            # Outline metal/implants if present (> METAL_THRESHOLD HU)
            metal_mask = img > METAL_THRESHOLD
            if np.any(metal_mask):
                self.ax.contour(metal_mask, colors='#ff3b30', levels=[0.5], linewidths=1.2)

            self.ax.set_title(f"Slice: {self.current_index + 1} / {len(self.files)}", color="white")
            self.ax.axis('off')
            self.canvas.draw()
        except Exception as e:
            print(f"Viewer Error: {e}")

    def _on_slider_move(self, value):
        self.display_slice(int(value))

    def _on_mousewheel(self, event):
        if event.delta > 0:
            new_idx = max(0, self.current_index - 1)
        else:
            new_idx = min(len(self.files) - 1, self.current_index + 1)
        self.display_slice(new_idx)

class ClinicalTriageApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("RT-CT Clinical Triage Station")
        self.geometry("1100x800")
        ctk.set_appearance_mode("dark")
        
        self.engine = QAEngine("ctqa.yaml")
        self.current_series_path = None
        self.current_series_uid = None
        self.after_id = None
        
        # Load WL Presets
        try:
            with open("WL.json", "r") as f:
                self.wl_presets = json.load(f)["ct_window_level_presets"]
        except Exception as e:
            print(f"Error loading WL.json: {e}")
            self.wl_presets = {}

        # Left Panel: Status & Logs
        self.sidebar = ctk.CTkFrame(self, width=350)
        self.sidebar.pack(side="left", fill="y", padx=10, pady=10)
        
        self.status_lbl = ctk.CTkLabel(self.sidebar, text="SYSTEM READY", text_color="green", font=("Roboto", 24, "bold"))
        self.status_lbl.pack(pady=20)
        
        self.info_lbl = ctk.CTkLabel(self.sidebar, text="Pending Scans: 0", font=("Roboto", 14))
        self.info_lbl.pack(pady=5)

        ctk.CTkLabel(self.sidebar, text="AGENT FINDINGS", font=("Roboto", 12, "bold")).pack(pady=(20, 0))
        self.flag_box = ctk.CTkTextbox(self.sidebar, height=400, width=300, font=("Consolas", 12))
        self.flag_box.pack(pady=10)

        # Window/Level Presets
        ctk.CTkLabel(self.sidebar, text="WINDOW / LEVEL", font=("Roboto", 12, "bold")).pack(pady=(20, 0))
        preset_names = ["Manual"] + list(self.wl_presets.keys())
        self.wl_menu = ctk.CTkOptionMenu(self.sidebar, values=preset_names, command=self._on_wl_change)
        self.wl_menu.pack(pady=10)
        self.wl_menu.set("Manual")

        # Manual Sliders
        self.ww_lbl = ctk.CTkLabel(self.sidebar, text="Width: 1000", font=("Roboto", 11))
        self.ww_lbl.pack()
        self.ww_slider = ctk.CTkSlider(self.sidebar, from_=1, to=3000, command=self._on_manual_wl_change)
        self.ww_slider.set(1000)
        self.ww_slider.pack(pady=(0, 10))

        self.wl_lbl = ctk.CTkLabel(self.sidebar, text="Level: 0", font=("Roboto", 11))
        self.wl_lbl.pack()
        self.wl_slider = ctk.CTkSlider(self.sidebar, from_=-1000, to=1000, command=self._on_manual_wl_change)
        self.wl_slider.set(0)
        self.wl_slider.pack(pady=(0, 10))

        # Right Panel: Viewer & Controls
        self.main_view = ctk.CTkFrame(self)
        self.main_view.pack(side="right", fill="both", expand=True, padx=10, pady=10)
        
        self.viewer = DICOMViewer(self.main_view)
        self.viewer.pack(fill="both", expand=True)

        self.btn_frame = ctk.CTkFrame(self.main_view)
        self.btn_frame.pack(fill="x", pady=10)
        
        self.approve_btn = ctk.CTkButton(self.btn_frame, text="APPROVE SCAN", fg_color="#28a745", hover_color="#218838", command=self.approve, height=50, font=("Roboto", 16, "bold"))
        self.approve_btn.pack(side="left", padx=20, expand=True)
        
        self.reject_btn = ctk.CTkButton(self.btn_frame, text="REJECT / RE-SCAN", fg_color="#dc3545", hover_color="#c82333", command=self.reject, height=50, font=("Roboto", 16, "bold"))
        self.reject_btn.pack(side="left", padx=20, expand=True)

        # Handle command-line arguments or start polling
        if len(sys.argv) > 1:
            series_uid = sys.argv[1]
            series_path = os.path.join(STORAGE_DIR, series_uid)
            if os.path.exists(series_path):
                self.load_series(series_path)
            else:
                self.flag_box.insert("end", f"Error: Series {series_uid} not found.\n")
        else:
            self.check_for_scans()
            
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def on_closing(self):
        if self.after_id:
            self.after_cancel(self.after_id)
        try:
            plt.close('all')
        except Exception:
            pass
        self.withdraw()
        self.quit()

    def _on_wl_change(self, preset_name):
        if preset_name in self.wl_presets:
            p = self.wl_presets[preset_name]
            ww, wl = p["window_width"], p["window_level"]
            self.ww_slider.set(ww)
            self.wl_slider.set(wl)
            self.ww_lbl.configure(text=f"Width: {int(ww)}")
            self.wl_lbl.configure(text=f"Level: {int(wl)}")
            self.viewer.set_window_level(ww, wl)

    def _on_manual_wl_change(self, _):
        self.wl_menu.set("Manual")
        ww = self.ww_slider.get()
        wl = self.wl_slider.get()
        self.ww_lbl.configure(text=f"Width: {int(ww)}")
        self.wl_lbl.configure(text=f"Level: {int(wl)}")
        self.viewer.set_window_level(ww, wl)

    def check_for_scans(self):
        if not self.current_series_path:
            series_dirs = [os.path.join(STORAGE_DIR, d) for d in os.listdir(STORAGE_DIR) if os.path.isdir(os.path.join(STORAGE_DIR, d))]
            # Simple logic: pick the first one that hasn't been exported yet
            for s_path in series_dirs:
                if not os.path.exists(os.path.join(EXPORT_DIR, os.path.basename(s_path))):
                    self.load_series(s_path)
                    break
        
        self.after_id = self.after(5000, self.check_for_scans)

    def load_series(self, series_path):
        self.current_series_path = series_path
        self.current_series_uid = os.path.basename(series_path)
        
        files = glob.glob(os.path.join(series_path, "*.dcm"))
        if not files:
            return

        # Load into viewer
        self.viewer.load_series(files)
        
        # Run Analysis
        try:
            result = self.engine.analyze_series(files)
            
            self.flag_box.delete("0.0", "end")
            self.flag_box.insert("end", f"PATIENT: {result.patient_name}\n")
            self.flag_box.insert("end", f"PROTOCOL: {result.protocol}\n")
            self.flag_box.insert("end", "-"*30 + "\n")
            
            for flag in result.flags:
                color = "RED" if flag.status == "REJECT" else "YELLOW" if flag.status == "CONDITIONAL" else "GREEN"
                self.flag_box.insert("end", f"[{flag.status}] {flag.name}\n")
                self.flag_box.insert("end", f" >> {flag.message}\n\n")
            
            self.status_lbl.configure(text="REVIEW REQUIRED", text_color="#ffc107")
        except Exception as e:
            self.flag_box.insert("end", f"Analysis Error: {e}")

    def approve(self):
        if not self.current_series_path:
            self.status_lbl.configure(text="No series loaded", text_color="#ff0000")
            return
        # Copy series to export directory
        dest = os.path.join(EXPORT_DIR, self.current_series_uid)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(self.current_series_path, dest)
        # Debug logs
        print(f"[DEBUG] Copied series {self.current_series_uid} to {dest}")
        # Send DICOM series synchronously with error handling
        try:
            print(f"Manual approval for {self.current_series_uid} - Routing to DICOM destinations...")
            send_dicom_series(self.current_series_path)
            print("[DEBUG] DICOM routing completed successfully.")
            self.status_lbl.configure(text="APPROVED - EXPORTED", text_color="#28a745")
        except Exception as e:
            print(f"[ERROR] DICOM routing failed: {e}")
            self.status_lbl.configure(text="APPROVAL FAILED", text_color="#ff0000")
        # Clean up UI state
        self.current_series_path = None
        self.flag_box.delete("0.0", "end")
        self.after(1000, self.on_closing)  # Brief delay to show success or failure

    def reject(self):
        if self.current_series_path:
            self.status_lbl.configure(text="REJECTED - NOTIFIED", text_color="#dc3545")
            # Log rejection
            with open("rejections.log", "a") as f:
                f.write(f"{self.current_series_uid} rejected\n")
            self.current_series_path = None
            self.flag_box.delete("0.0", "end")
            self.after(1000, self.on_closing) # Brief delay to show rejection status

if __name__ == "__main__":
    app = ClinicalTriageApp()
    app.mainloop()
