import csv
import io
import tempfile
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from fpdf import FPDF

def generate_csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO()
    if not rows:
        return b""
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()), delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")

def generate_report_pdf(report_type: str, payload: dict) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"Rapport Communautaire - {report_type.upper()}", ln=True)

    pdf.set_font("Helvetica", "", 10)
    p = payload["period"]
    pdf.cell(0, 8, f"Periode : {p['start']} au {p['end']} | Genere le : {payload['generated_at']}", ln=True)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Indicateurs et evolution", ln=True)
    pdf.set_font("Helvetica", "", 10)

    labels = {
        "messages": "Messages",
        "media_posts": "Publications medias",
        "active_members": "Membres actifs",
        "joins": "Arrivees",
        "leaves": "Departs",
    }
    for key, label in labels.items():
        c = payload["comparisons"][key]
        line = f"- {label} : {c['current']} (precedent : {c['previous']} | {c['evolution']})"
        pdf.cell(0, 7, line, ln=True)

    top_channels = payload["metrics"].get("top_channels", [])
    if top_channels:
        fig, ax = plt.subplots(figsize=(6, 3))
        names = [c["channel"] for c in top_channels[:6]]
        vals = [c["messages"] for c in top_channels[:6]]
        ax.barh(names[::-1], vals[::-1], color="#5865F2")
        ax.set_title("Top salons (messages)")
        plt.tight_layout()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
            fig.savefig(tmp.name, dpi=150)
            plt.close(fig)
            pdf.ln(5)
            pdf.image(tmp.name, w=170)

    return bytes(pdf.output())
