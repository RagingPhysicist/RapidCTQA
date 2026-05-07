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

def generate_pdf_report(result: QAResult, output_path: str):
    pdf = QAPDFReport()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Patient Information Section
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, ' Patient Information', ln=True, fill=True)
    pdf.ln(5)
    
    pdf.set_font('helvetica', '', 11)
    info = [
        ('Patient Name:', result.patient_name),
        ('Series UID:', result.series_uid),
        ('Protocol:', result.protocol),
        ('Final Status:', result.status)
    ]
    
    for label, value in info:
        pdf.set_font('helvetica', 'B', 11)
        pdf.cell(40, 8, label)
        pdf.set_font('helvetica', '', 11)
        if label == 'Final Status:':
            if value == 'ACCEPT': pdf.set_text_color(16, 185, 129)
            elif value == 'CONDITIONAL': pdf.set_text_color(245, 158, 11)
            else: pdf.set_text_color(239, 68, 68)
        pdf.cell(0, 8, str(value), ln=True)
        pdf.set_text_color(0, 0, 0) # Reset

    pdf.ln(10)

    # Agent Findings Section
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, ' Agent Findings & Flags', ln=True, fill=True)
    pdf.ln(5)

    # Table Header
    pdf.set_font('helvetica', 'B', 11)
    pdf.set_fill_color(30, 41, 59)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(40, 10, 'Agent', border=1, fill=True)
    pdf.cell(30, 10, 'Status', border=1, fill=True)
    pdf.cell(120, 10, 'Message', border=1, fill=True)
    pdf.ln()

    # Table Content
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('helvetica', '', 10)
    for flag in result.flags:
        # Status Color
        if flag.status == 'ACCEPT': pdf.set_text_color(16, 185, 129)
        elif flag.status == 'CONDITIONAL': pdf.set_text_color(245, 158, 11)
        else: pdf.set_text_color(239, 68, 68)
        
        pdf.cell(40, 10, flag.name, border=1)
        pdf.cell(30, 10, flag.status, border=1)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(120, 10, flag.message, border=1)
        pdf.ln()

    pdf.ln(10)

    # Metrics Section
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, ' Quantitative Metrics', ln=True, fill=True)
    pdf.ln(5)
    
    pdf.set_font('helvetica', '', 10)
    col_width = pdf.epw / 2
    items = list(result.metrics.items())
    for i in range(0, len(items), 2):
        k1, v1 = items[i]
        val1 = f"{v1:.3f}" if isinstance(v1, float) else str(v1)
        pdf.cell(col_width, 8, f"{k1}: {val1}", border='B')
        if i+1 < len(items):
            k2, v2 = items[i+1]
            val2 = f"{v2:.3f}" if isinstance(v2, float) else str(v2)
            pdf.cell(col_width, 8, f"{k2}: {val2}", border='B', ln=True)
        else:
            pdf.ln()

    pdf.output(output_path)
