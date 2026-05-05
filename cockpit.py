import customtkinter as ctk
import pydicom
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import os, shutil, glob
from backend.engine import QAEngine

# Settings
STORAGE_DIR = "./data/rtct"
EXPORT_DIR = "./TPS_EXPORT"
os.makedirs(EXPORT_DIR, exist_ok=True)

class DICOMViewer(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.files = []
        self.current_index = 0
        
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

    def display_slice(self, index):
        if not self.files: return
        self.current_index = int(index)
        self.slider.set(self.current_index)
        
        try:
            ds = pydicom.dcmread(self.files[self.current_index])
            self.ax.clear()
            self.ax.imshow(ds.pixel_array, cmap='gray')
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

        # Polling for new scans
        self.check_for_scans()

    def check_for_scans(self):
        if not self.current_series_path:
            series_dirs = [os.path.join(STORAGE_DIR, d) for d in os.listdir(STORAGE_DIR) if os.path.isdir(os.path.join(STORAGE_DIR, d))]
            # Simple logic: pick the first one that hasn't been exported yet
            for s_path in series_dirs:
                if not os.path.exists(os.path.join(EXPORT_DIR, os.path.basename(s_path))):
                    self.load_series(s_path)
                    break
        
        self.after(5000, self.check_for_scans)

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
        if self.current_series_path:
            dest = os.path.join(EXPORT_DIR, self.current_series_uid)
            if os.path.exists(dest): shutil.rmtree(dest)
            shutil.copytree(self.current_series_path, dest)
            
            self.status_lbl.configure(text="APPROVED - EXPORTED", text_color="#28a745")
            self.current_series_path = None
            self.flag_box.delete("0.0", "end")

    def reject(self):
        if self.current_series_path:
            self.status_lbl.configure(text="REJECTED - NOTIFIED", text_color="#dc3545")
            # Log rejection
            with open("rejections.log", "a") as f:
                f.write(f"{self.current_series_uid} rejected\n")
            self.current_series_path = None
            self.flag_box.delete("0.0", "end")

if __name__ == "__main__":
    app = ClinicalTriageApp()
    app.mainloop()
