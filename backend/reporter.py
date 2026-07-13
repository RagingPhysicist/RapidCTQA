from fpdf import FPDF
import datetime
import os
from .models import QAResult

class QAPDFReport(FPDF):
    def header(self):
        # Logo placeholder or Title
        self.set_font('helvetica', 'B', 20)
        self.set_text_color(0, 210, 255) # RapidCTQA Blue
        self.cell(0, 10, 'RapidCTQA Clinical Report', ln=True, align='L')
        self.set_font('helvetica', 'I', 10)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f'Generated on: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', ln=True, align='L')
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font('helvetica', 'I', 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f'Page {self.page_no()}/{{nb}} - Confidential Clinical QA Document', align='C')

def format_slices(slices) -> str:
    if not slices:
        return "None"
    if isinstance(slices, str):
        return slices
    if not isinstance(slices, list):
        return str(slices)
    
    try:
        slices = sorted(list(set(int(s) for s in slices)))
    except (ValueError, TypeError):
        return str(slices)
        
    if not slices:
        return "None"

    start = slices[0]
    end = slices[0]
    ranges = []

    for i in range(1, len(slices)):
        if slices[i] == end + 1:
            end = slices[i]
        else:
            if start == end:
                ranges.append(f"{start}")
            else:
                ranges.append(f"{start}-{end}")
            start = slices[i]
            end = slices[i]

    if start == end:
        ranges.append(f"{start}")
    else:
        ranges.append(f"{start}-{end}")

    return f"Slices {', '.join(ranges)}"

def generate_pdf_report(result: QAResult, output_path: str):
    pdf = QAPDFReport()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Patient Information Section
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, ' Patient Information', new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(5)
    
    info = [
        ('Patient Name:', result.patient_name),
        ('Series UID:', result.series_uid),
        ('Protocol:', result.protocol),
        ('Final Status:', result.status)
    ]
    
    pdf.set_font('helvetica', '', 11)
    with pdf.table(borders_layout="NONE", first_row_as_headings=False, col_widths=(35, 155)) as table:
        for label, value in info:
            row = table.row()
            
            # Label cell (bold)
            pdf.set_font('helvetica', 'B', 11)
            row.cell(label)
            
            # Value cell (regular, colored for final status)
            pdf.set_font('helvetica', '', 11)
            if label == 'Final Status:':
                if value == 'ACCEPT': pdf.set_text_color(16, 185, 129)
                elif value == 'CONDITIONAL': pdf.set_text_color(245, 158, 11)
                else: pdf.set_text_color(239, 68, 68)
            row.cell(str(value))
            pdf.set_text_color(0, 0, 0) # Reset

    pdf.ln(5)

    # Agent Findings Section
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, ' Agent Findings & Flags', new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(5)

    # Use native table to prevent text overlapping and handle wrapping
    with pdf.table(col_widths=(40, 30, 120)) as table:
        # Header Row
        pdf.set_font('helvetica', 'B', 11)
        row = table.row()
        row.cell('Agent')
        row.cell('Status')
        row.cell('Message')
        
        # Table Content
        pdf.set_font('helvetica', '', 10)
        for flag in result.flags:
            row = table.row()
            row.cell(flag.name)
            
            # Status Color
            if flag.status == 'ACCEPT': pdf.set_text_color(16, 185, 129)
            elif flag.status == 'CONDITIONAL': pdf.set_text_color(245, 158, 11)
            else: pdf.set_text_color(239, 68, 68)
            row.cell(flag.status)
            
            # Reset Color for message
            pdf.set_text_color(0, 0, 0)
            row.cell(flag.message)

    pdf.ln(5)

    # Metrics Section
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, ' Quantitative Metrics', new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(5)
    
    # Define clean, quantitative key metrics
    key_metrics = [
        ("Slice Count", result.metrics.get("slice_count")),
        ("Slice Thickness", f"{result.metrics.get('slice_thickness', 0.0):.1f} mm"),
        ("Slice Spacing Variation", f"{result.metrics.get('slice_spacing_var', 0.0):.2f} mm"),
        ("Monotonic Z-Positions", "Yes" if result.metrics.get("monotonic_z") else "No"),
        ("Duplicate Slices", "Yes" if result.metrics.get("duplicate_slices") else "No"),
        ("Gantry Tilt", f"{result.metrics.get('gantry_tilt', 0.0):.1f}°"),
        ("Background Air Noise (SD)", f"{result.metrics.get('background_air_sd', 0.0):.1f} HU"),
        ("Center ROI Noise (SD)", f"{result.metrics.get('center_noise_std', 0.0):.1f} HU"),
        ("Air HU Calibration Est.", f"{result.metrics.get('air_hu_estimate', 0.0):.1f} HU"),
        ("Fluid Median Density", f"{result.metrics.get('fluid_median_hu', 0.0):.1f} HU"),
        ("Gas Pockets Volume", f"{result.metrics.get('gas_volume_cc', 0.0):.1f} cc"),
        ("Total Metal Volume", f"{result.metrics.get('metal_volume_cc', 0.0):.2f} cc"),
        ("Internal Metal Vol.", f"{result.metrics.get('metal_internal_cc', 0.0):.2f} cc"),
        ("Surface Metal Vol.", f"{result.metrics.get('metal_surface_cc', 0.0):.2f} cc"),
        ("External Metal Vol.", f"{result.metrics.get('metal_external_cc', 0.0):.2f} cc"),
        ("Max Patient Rotation", f"{result.metrics.get('max_tilt_deg', 0.0):.1f}°"),
        ("Truncation Detected", "Yes" if result.metrics.get("truncation_detected") else "No"),
        ("Truncation Error Detected", "Yes" if result.metrics.get("truncation_error") else "No"),
        ("Tolerated Truncation Detected", "Yes" if result.metrics.get("tolerated_truncated_slices") else "No"),
    ]

    pdf.set_font('helvetica', '', 10)
    with pdf.table(col_widths=(60, 40)) as table:
        # Header Row
        pdf.set_font('helvetica', 'B', 10)
        row = table.row()
        row.cell("Metric Name")
        row.cell("Measured Value")
        
        # Data Rows
        pdf.set_font('helvetica', '', 10)
        for label, val in key_metrics:
            if val is not None:
                row = table.row()
                row.cell(label)
                row.cell(str(val))

    # Affected Slice Locations Section
    slice_metrics = [
        ("Truncated Slices", result.metrics.get("truncated_slices")),
        ("Truncated Slices (Tolerated)", result.metrics.get("tolerated_truncated_slices")),
        ("Tilted/Rotated Slices", result.metrics.get("tilted_slices")),
        ("Gas Pockets Slices", result.metrics.get("gas_slices")),
        ("Metal Implants Slices", result.metrics.get("metal_slices")),
        ("Internal Metal Slices", result.metrics.get("metal_internal_slices")),
        ("Surface Metal Slices", result.metrics.get("metal_surface_slices")),
        ("External Metal Slices", result.metrics.get("metal_external_slices")),
    ]
    
    active_slices = [(label, slices) for label, slices in slice_metrics if slices]
    if active_slices:
        pdf.ln(5)
        pdf.set_fill_color(240, 240, 240)
        pdf.set_font('helvetica', 'B', 14)
        pdf.cell(0, 10, ' Affected Slice Locations', new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.ln(5)
        
        pdf.set_font('helvetica', '', 10)
        with pdf.table(col_widths=(60, 130)) as table:
            # Header Row
            pdf.set_font('helvetica', 'B', 10)
            row = table.row()
            row.cell("Location Category")
            row.cell("Affected Slices")
            
            # Data Rows
            pdf.set_font('helvetica', '', 10)
            for label, slices in active_slices:
                row = table.row()
                row.cell(label)
                row.cell(format_slices(slices))

    pdf.output(output_path)
