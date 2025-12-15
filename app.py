from __future__ import annotations

import io
import re
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, flash, redirect, url_for, jsonify


APP_TZ = ZoneInfo("America/Los_Angeles")
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PDF_PATH = str(BASE_DIR / "F-ENG-001_template.pdf")

# AcroForm field names discovered from your F-ENG-001 PDF
FIELD_JOB_NUMBER = "Text1"
FIELD_JOB_NAME = "Text2"
FIELD_REQUESTER = "Text3"     # leave blank
FIELD_REQUEST_DATE = "Text4"
FIELD_DUE_DATE = "Text5"
FIELD_VOLTAGE = "Text6"
FIELD_END_CUST_CONTACT = "Text12"
FIELD_END_CUST_PHONE = "Text13"
# GC Contact row (Name & Email may be split across two fields)
FIELD_GC_CONTACT_NAME = "Text15"
FIELD_GC_CONTACT_EMAIL = "Text16"
FIELD_GC_CONTACT_PHONE = "Text17"
FIELD_PROJECT_SITE_ADDRESS = "Text14"

FIELD_AMPERAGE = "Text7"
FIELD_WATER_SERVICE_SIZE_PRESSURE = "Text8"
FIELD_MAX_VEHICLE_HEIGHT = "Text9"
FIELD_BRUSH_SYSTEM_MODEL = "Text10"
FIELD_PROJECT_NOTES = "Text18"

# Checkbox field names in the F-ENG-001 PDF (AcroForm)
CHECKBOX_TYPE_REQUEST = {
    "BID": "Check Box1",
    "JOB_ORDER": "Check Box2",
    "CHANGE_ORDER": "Check Box3",
    "OM": "Check Box4",  # O&M (Customer Service Use Only)
}

CHECKBOX_DOCUMENT_TYPE = {
    "EQUIPMENT_LAYOUT": "Check Box11",
    "PLUMBING_LAYOUT": "Check Box12",
    "ELECTRICAL_LAYOUT": "Check Box13",
    "SLAB_LAYOUT": "Check Box14",
    "DATA_SHEET_SUBMITTAL": "Check Box15",  # Not available for BID
    "OTHER": "Check Box16",
}

CHECKBOX_PROJECT_TYPE = {
    "AUTO_VEHICLE": "Check Box5",
    "TUNNEL": "Check Box6",
    "TRUCK_BUS": "Check Box7",
    "TRAIN": "Check Box8",
    "LEAK_TEST": "Check Box9",
    "OTHER": "Check Box10",
}


@dataclass
class ReleaseData:
    job_number: str = ""
    job_name: str = ""
    voltage: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    ship_to_lines: list[str] | None = None

    @property
    def end_customer_contact_name_email(self) -> str:
        parts = []
        if self.contact_name.strip():
            parts.append(self.contact_name.strip())
        if self.contact_email.strip():
            parts.append(self.contact_email.strip())
        # Match your preferred style: "Name | email"
        return " | ".join(parts).strip()

    @property
    def project_site_address(self) -> str:
        if not self.ship_to_lines:
            return ""
        return "\n".join([ln for ln in self.ship_to_lines if ln.strip()]).strip()


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # keep newlines, but clean trailing spaces
    text = "\n".join([ln.rstrip() for ln in text.split("\n")])
    return text


def _first_email(text: str) -> str:
    m = re.search(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", text, flags=re.I)
    return m.group(0).strip() if m else ""


def _format_us_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    return raw.strip()



def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract raw text from a PDF using PyMuPDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return "\n".join([doc[i].get_text("text") for i in range(doc.page_count)])


def parse_release_pdf(release_pdf_bytes: bytes, *, pre_extracted_text: str | None = None) -> ReleaseData:
    full_text = pre_extracted_text if pre_extracted_text is not None else _extract_pdf_text(release_pdf_bytes)
    text = _normalize_text(full_text)

    # IMPORTANT:
    # These Release PDFs are effectively "tables". PyMuPDF's text extraction tends
    # to output *labels* first (e.g., "Job Order #:") and then output the values
    # a few lines later (or even *above* the label) depending on the PDF's internal
    # text order. So simple regex like "Voltage: (.*)" will often capture the NEXT
    # label (e.g., "Release Status:") instead of the value.
    #
    # Strategy used here:
    # - Convert to a line list.
    # - For key fields, look for a value that matches a *value regex* within a small
    #   window after the label (Job Order #, Voltage), skipping obvious label lines.
    # - For Phone, pick the *closest* phone number to the "Phone:" label so we don't
    #   accidentally grab an unrelated number like the one shown under "Comment:".
    lines = [ln.strip() for ln in text.split("\n")]

    def _is_label_line(s: str) -> bool:
        return bool(re.match(r"^[A-Za-z0-9 ./'&\-]+:\s*$", s))

    def _find_after(label_re: str, value_re: str, *, max_look: int = 25) -> str:
        lab = re.compile(label_re, flags=re.I)
        val = re.compile(value_re, flags=re.I)
        for i, ln in enumerate(lines):
            if not lab.search(ln):
                continue
            # same line first
            m0 = val.search(ln)
            if m0:
                return m0.group(1) if m0.lastindex else m0.group(0)
            # then look ahead
            for j in range(1, max_look + 1):
                if i + j >= len(lines):
                    break
                cand = (lines[i + j] or "").strip()
                if not cand or _is_label_line(cand):
                    continue
                m = val.search(cand)
                if m:
                    return m.group(1) if m.lastindex else m.group(0)
        return ""

    def _find_closest(label_re: str, value_re: str, *, window: int = 30) -> str:
        lab = re.compile(label_re, flags=re.I)
        val = re.compile(value_re, flags=re.I)
        for i, ln in enumerate(lines):
            if not lab.search(ln):
                continue
            best_dist = 10**9
            best_val = ""
            for j in range(max(0, i - window), min(len(lines), i + window + 1)):
                if j == i:
                    continue
                cand = (lines[j] or "").strip()
                if not cand or _is_label_line(cand):
                    continue
                m = val.search(cand)
                if not m:
                    continue
                dist = abs(j - i)
                if dist < best_dist:
                    best_dist = dist
                    best_val = m.group(1) if m.lastindex else m.group(0)
            if best_val:
                return best_val
        return ""

    data = ReleaseData()

    # JOB NUMBER (e.g., 2_3074)
    # The value is frequently a few lines BELOW "Job Order #:".
    job = _find_after(r"Job\s*Order\s*#\s*:?", r"(\d+\s*[_-]\s*\d+)")
    if job:
        data.job_number = re.sub(r"\s+", "", job)
    else:
        # fallback: "Job Order Requirements 2_3074"
        m2 = re.search(r"Job\s*Order\s*Requirements\s+([0-9][0-9_\-\/]+)", text, flags=re.I)
        if m2:
            data.job_number = m2.group(1).strip()

    # JOB NAME (Ship To Name)
    # Per your updated requirement, "Job Name" in F-ENG-001 should come from "Ship To Name:" on the Release PDF.
    # If Ship To Name can't be found, fall back to Customer Name.
    ship_to_name = ""
    m = re.search(r"Ship\s*To\s*Name:\s*([^\n]+)", text, flags=re.I)
    if m:
        ship_to_name = m.group(1).strip()

    if ship_to_name:
        data.job_name = ship_to_name
    else:
        m = re.search(r"Customer\s*Name:\s*([^\n]+)", text, flags=re.I)
        if m:
            data.job_name = m.group(1).strip()

    # VOLTAGE
    # Same issue as Job Order: often not on the same line as "Voltage:".
    v = _find_after(r"\bVoltage\s*:?", r"(\d{3,4}\s*(?:V(?:OLT)?|VAC))")
    if v:
        data.voltage = re.sub(r"\s+", " ", v).strip().upper()
    else:
        # Conservative fallback (but try to avoid matching voltages in item descriptions)
        m2 = re.search(r"\b(\d{3,4})\s*(V(?:OLT)?|VAC)\b", text, flags=re.I)
        if m2:
            data.voltage = f"{m2.group(1)} {m2.group(2).upper()}"

    # CONTACT NAME
    contacts = [c.strip() for c in re.findall(r"Contact:\s*([^\n]+)", text, flags=re.I)]
    contacts = [c for c in contacts if c and c.lower() not in {"released"}]
    if contacts:
        data.contact_name = contacts[-1]

    # CONTACT EMAIL
    data.contact_email = _first_email(text)

    # CONTACT PHONE
    # Don't grab the phone number under "Comment:". Prefer the number closest to the
    # "Phone:" label on the last page.
    p = _find_closest(r"\bPhone\s*:?", r"(\(?\d{3}\)?[-\.\s]?\d{3}[-\.\s]?\d{4})")
    if not p:
        # fallback (some releases only say "TELEPHONE")
        p = _find_closest(r"\bTelephone\b", r"(\(?\d{3}\)?[-\.\s]?\d{3}[-\.\s]?\d{4})")
    if p:
        data.contact_phone = _format_us_phone(p)

    # SHIP TO ADDRESS BLOCK
    # Grab lines from "Ship To Name:" down to (but not including) the next header block.
    m = re.search(
        r"Ship\s*To\s*Name:\s*(.+?)(?:\n\s*Job\s*Order\s*#|\n\s*Order\s*Date|\Z)",
        text,
        flags=re.I | re.S,
    )
    if m:
        block = m.group(1).strip()
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        # Often first line is name; keep up to 4 lines to avoid grabbing unrelated text
        data.ship_to_lines = lines[:4]
    else:
        data.ship_to_lines = []

    return data


def _looks_like_quote_contract(text: str) -> bool:
    t = text.lower()
    return ("quotation number" in t) and ("prepared for" in t) and ("order contract" in t)


def _first_email(s: str) -> str:
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", s, flags=re.I)
    return m.group(0).strip() if m else ""


def _first_phone(s: str) -> str:
    m = re.search(r"(\(?\d{3}\)?[-\.\s]?\d{3}[-\.\s]?\d{4})", s)
    return m.group(1).strip() if m else ""


def _extract_value_near_label(lines: list[str], label_re: str, *, max_ahead: int = 10, skip_re: str | None = None) -> str:
    lab = re.compile(label_re, flags=re.I)
    skip = re.compile(skip_re, flags=re.I) if skip_re else None
    for i, ln in enumerate(lines):
        if not lab.search(ln):
            continue

        # Same-line value after ":" if present
        if ":" in ln:
            tail = ln.split(":", 1)[1].strip()
            if tail and (not skip or not skip.search(tail)):
                return tail

        # Look ahead for the first plausible value line
        for j in range(i + 1, min(len(lines), i + 1 + max_ahead)):
            v = lines[j].strip()
            if not v:
                continue
            if skip and skip.search(v):
                continue
            # avoid grabbing other labels
            if v.endswith(":") or re.match(r"^[A-Za-z][A-Za-z /#&.-]*:$", v):
                continue
            return v
    return ""


def _extract_ship_to_block(lines: list[str]) -> list[str]:
    # Find the "Sold To: Ship To:" or "Ship To:" line, then capture until "Description" (or similar)
    start = -1
    for i, ln in enumerate(lines):
        if re.search(r"\bShip\s*To\b", ln, flags=re.I) and ":" in ln:
            start = i + 1
            break
    if start < 0:
        return []

    block: list[str] = []
    for j in range(start, len(lines)):
        ln = lines[j].strip()
        if not ln:
            continue
        if re.match(r"^(Description|Delivery Date|Revision)\b", ln, flags=re.I):
            break
        block.append(ln)

    # Clean up obvious noise
    block = [b for b in block if not re.match(r"^Sold\s*To\b", b, flags=re.I)]
    return block


def _extract_voltage_before_model(text: str) -> str:
    prefix = text
    if re.search(r"\bModel\s+Number\b", text, flags=re.I):
        prefix = re.split(r"\bModel\s+Number\b", text, flags=re.I)[0]

    # Look specifically for a voltage label in the header area
    m = re.search(r"\bVoltage\b\s*(?:Available)?\s*[:\-]?\s*([^\n\r]+)", prefix, flags=re.I)
    if m:
        v = m.group(1).strip()
        # Don't return garbage like the next label
        if v and len(v) <= 60 and not re.search(r"\b(Model\s+Number|Qty|Amount|Prepared\s+for|Quotation)\b", v, flags=re.I):
            return v

    # Otherwise try a conservative pattern: "208V", "480V 3PH", etc.
    m2 = re.search(r"\b(1\d\d|2\d\d|4\d\d)\s*V\b(?:\s*[;,/]?\s*(?:1PH|3PH))?", prefix, flags=re.I)
    return m2.group(0).strip() if m2 else ""


def parse_quote_contract_text(full_text: str) -> ReleaseData:
    """Parse the 'Quotation / Order Contract' style PDF."""
    text = _normalize_text(full_text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    data = ReleaseData()

    # Job Number from "Quotation Number:"
    data.job_number = _extract_value_near_label(
        lines,
        r"\bQuotation\s+Number\s*:",
        max_ahead=12,
        skip_re=r"\bQuotation\s+Date\b|\bPrepared\s+for\b",
    )

    # Ship To block
    ship_block = _extract_ship_to_block(lines)
    if ship_block:
        data.job_name = ship_block[0].strip()
        # Project Site Address = everything AFTER the first line (per your requirement)
        data.ship_to_lines = [ln.strip() for ln in ship_block[1:] if ln.strip()]
    else:
        data.ship_to_lines = []

    # Contact details from text after "Prepared for" but before "Model Number"
    lower = text.lower()
    a = lower.find("prepared for")
    b = lower.find("model number")
    contact_block = ""
    if a != -1 and b != -1 and b > a:
        contact_block = text[a:b]

    # Contact name: first non-empty line after the label
    cb_lines = [ln.strip() for ln in contact_block.split("\n") if ln.strip()]
    name = ""
    for i, ln in enumerate(cb_lines):
        if re.search(r"\bPrepared\s+for\b", ln, flags=re.I):
            # same-line value
            if ":" in ln and ln.split(":", 1)[1].strip():
                name = ln.split(":", 1)[1].strip()
                break
            # next line
            if i + 1 < len(cb_lines):
                name = cb_lines[i + 1].strip()
                break
    data.contact_name = name
    data.contact_email = _first_email(contact_block)
    data.contact_phone = _first_phone(contact_block)

    # Voltage: only if it's found BEFORE "Model Number"
    data.voltage = _extract_voltage_before_model(text)

    return data


def parse_jo_pdf_with_type(pdf_bytes: bytes) -> tuple[ReleaseData, str]:
    """Parse either a Release_-style PDF or the Quotation/Order-Contract style PDF.

    Returns: (ReleaseData, pdf_type_string)
    """
    full_text = _extract_pdf_text(pdf_bytes)
    normalized = _normalize_text(full_text)

    if _looks_like_quote_contract(normalized):
        return parse_quote_contract_text(full_text), "Quote/Contract"

    # Default to Release_-style parser
    return parse_release_pdf(pdf_bytes, pre_extracted_text=full_text), "Release"


def parse_jo_pdf(pdf_bytes: bytes) -> ReleaseData:
    """Back-compat: returns only the parsed data."""
    data, _pdf_type = parse_jo_pdf_with_type(pdf_bytes)
    return data




def fill_f_eng_001(
    data: ReleaseData,
    type_request: str,
    doc_types: list[str],
    project_types: list[str],
    requester: str,
    amperage: str,
    water_service_size_pressure: str,
    max_vehicle_height: str,
    brush_system_model: str,
    project_notes: str,
    gc_contact_name: str,
    gc_contact_email: str,
    gc_contact_phone: str,
    request_date_override: str,
    due_date_override: str,
) -> bytes:
    now = datetime.now(APP_TZ)
    request_date = (request_date_override or "").strip() or now.strftime("%m/%d/%Y")
    due_date = (due_date_override or "").strip() or (now + timedelta(days=15)).strftime("%m/%d/%Y")

    # Normalize selections
    selected_type_request = (type_request or "JOB_ORDER").strip().upper()
    selected_doc_types = set([dt.strip().upper() for dt in (doc_types or []) if dt and dt.strip()])
    selected_project_types = set([pt.strip().upper() for pt in (project_types or []) if pt and pt.strip()])

    requester_val = (requester or '').strip()
    amperage_val = (amperage or '').strip()
    water_service_val = (water_service_size_pressure or '').strip()
    max_vehicle_height_val = (max_vehicle_height or '').strip()
    brush_system_model_val = (brush_system_model or '').strip()
    project_notes_val = (project_notes or '').strip()

    gc_contact_name_val = (gc_contact_name or '').strip()
    gc_contact_email_val = (gc_contact_email or '').strip()
    gc_contact_phone_val = (gc_contact_phone or '').strip()

    # Combine GC name + email into the single 'GC CONTACT NAME & EMAIL' field.
    if gc_contact_name_val and gc_contact_email_val:
        gc_contact_combined_val = f"{gc_contact_name_val}\n{gc_contact_email_val}"
    else:
        gc_contact_combined_val = gc_contact_name_val or gc_contact_email_val

    doc = fitz.open(TEMPLATE_PDF_PATH)
    page = doc[0]

    # Fill only the fields you specified
    for w in page.widgets():
        if w.field_name == FIELD_JOB_NUMBER:
            w.field_value = data.job_number
            w.update()
        elif w.field_name == FIELD_JOB_NAME:
            w.field_value = data.job_name
            w.update()
        elif w.field_name == FIELD_REQUESTER:
            w.field_value = requester_val
            w.update()
        elif w.field_name == FIELD_REQUEST_DATE:
            w.field_value = request_date
            w.update()
        elif w.field_name == FIELD_DUE_DATE:
            w.field_value = due_date
            w.update()
        elif w.field_name == FIELD_VOLTAGE:
            w.field_value = data.voltage
            w.update()
        elif w.field_name == FIELD_AMPERAGE:
            w.field_value = amperage_val
            w.update()
        elif w.field_name == FIELD_WATER_SERVICE_SIZE_PRESSURE:
            w.field_value = water_service_val
            w.update()
        elif w.field_name == FIELD_MAX_VEHICLE_HEIGHT:
            w.field_value = max_vehicle_height_val
            w.update()
        elif w.field_name == FIELD_BRUSH_SYSTEM_MODEL:
            w.field_value = brush_system_model_val
            w.update()
        elif w.field_name == FIELD_END_CUST_CONTACT:
            w.field_value = data.end_customer_contact_name_email
            w.update()
        elif w.field_name == FIELD_END_CUST_PHONE:
            w.field_value = data.contact_phone
            w.update()
        
        elif w.field_name == FIELD_PROJECT_SITE_ADDRESS:
            w.field_value = data.project_site_address
            w.update()


        elif w.field_name == FIELD_GC_CONTACT_NAME:
            # Write GC name+email into a single field (FIELD_GC_CONTACT_EMAIL). Leave this internal field empty.
            w.field_value = ""
            w.update()
        elif w.field_name == FIELD_GC_CONTACT_EMAIL:
            w.field_value = gc_contact_combined_val
            w.update()
        elif w.field_name == FIELD_GC_CONTACT_PHONE:
            w.field_value = gc_contact_phone_val
            w.update()

        elif w.field_name == FIELD_PROJECT_NOTES:
            w.field_value = project_notes_val
            w.update()

        # --- Type of Request (single) ---
        elif w.field_name in CHECKBOX_TYPE_REQUEST.values():
            # Default all to OFF, then set selected one to ON.
            w.field_value = "Off"
            if CHECKBOX_TYPE_REQUEST.get(selected_type_request) == w.field_name:
                w.field_value = w.on_state()
            w.update()

        # --- Project Type (multi) ---
        elif w.field_name in CHECKBOX_PROJECT_TYPE.values():
            w.field_value = "Off"
            for key, fname in CHECKBOX_PROJECT_TYPE.items():
                if fname == w.field_name and key in selected_project_types:
                    w.field_value = w.on_state()
                    break
            w.update()

        # --- Document Type Required (multi) ---
        elif w.field_name in CHECKBOX_DOCUMENT_TYPE.values():
            w.field_value = "Off"
            # Turn on any selected doc types
            for key, fname in CHECKBOX_DOCUMENT_TYPE.items():
                if fname == w.field_name and key in selected_doc_types:
                    w.field_value = w.on_state()
                    break
            w.update()

    # Return as bytes (keep it as an editable form)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


app = Flask(__name__)
app.secret_key = "dev-secret-change-me"  # change for production


@app.get("/")
def index():
    return render_template("index.html")



@app.post("/parse")
def parse_endpoint():
    """
    Parse the uploaded JO/Release PDF and return extracted fields as JSON.
    Used to auto-populate editable inputs on the page.
    """
    if "release_pdf" not in request.files:
        return jsonify({"ok": False, "error": "Missing file field: release_pdf"}), 400

    f = request.files["release_pdf"]
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    try:
        release_bytes = f.read()
        data, pdf_type = parse_jo_pdf_with_type(release_bytes)

        now = datetime.now(APP_TZ)
        request_date = now.strftime("%m/%d/%Y")
        due_date = (now + timedelta(days=15)).strftime("%m/%d/%Y")

        return jsonify({
            "ok": True,
            "job_number": data.job_number,
            "job_name": data.job_name,
            "pdf_type": pdf_type,
            "voltage": data.voltage,
            "contact_name": data.contact_name,
            "contact_email": data.contact_email,
            "contact_phone": data.contact_phone,
            "project_site_address": data.project_site_address,
            "request_date": request_date,
            "due_date": due_date,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/generate")
def generate():
    """Generate a filled F-ENG-001 PDF.

    A JO PDF upload is optional. If none is provided, the auto-filled JO fields remain blank and
    the user can still generate an ESR using manual inputs/overrides.
    """
    f = request.files.get("release_pdf")

    try:
        extracted = ReleaseData()

        # If a JO PDF is provided, parse it; otherwise keep extracted fields empty.
        if f and getattr(f, "filename", ""):
            if not f.filename.lower().endswith(".pdf"):
                flash("Please upload a PDF file.")
                return redirect(url_for("index"))

            release_bytes = f.read()
            if len(release_bytes) > 15 * 1024 * 1024:
                flash("File too large (max 15 MB).")
                return redirect(url_for("index"))

            extracted = parse_jo_pdf(release_bytes)
        type_request = request.form.get('type_request', 'JOB_ORDER')
        doc_types = request.form.getlist('doc_types')
        project_types = request.form.getlist('project_types')

        requester = request.form.get('requester', '')
        amperage = request.form.get('amperage', '')

        # Editable JO fields (auto-populated from /parse, but user can override)
        jo_job_number = request.form.get('jo_job_number', '')
        jo_job_name = request.form.get('jo_job_name', '')
        jo_voltage = request.form.get('jo_voltage', '')
        jo_contact_name = request.form.get('jo_contact_name', '')
        jo_contact_email = request.form.get('jo_contact_email', '')
        jo_contact_phone = request.form.get('jo_contact_phone', '')
        jo_project_site_address = request.form.get('jo_project_site_address', '')
        jo_request_date = request.form.get('jo_request_date', '')
        jo_due_date = request.form.get('jo_due_date', '')

        # Apply overrides only if user entered something (keeps old behavior if they skip parsing)
        if jo_job_number.strip():
            extracted.job_number = jo_job_number.strip()
        if jo_job_name.strip():
            extracted.job_name = jo_job_name.strip()
        if jo_voltage.strip():
            extracted.voltage = jo_voltage.strip()
        if jo_contact_name.strip():
            extracted.contact_name = jo_contact_name.strip()
        if jo_contact_email.strip():
            extracted.contact_email = jo_contact_email.strip()
        if jo_contact_phone.strip():
            extracted.contact_phone = jo_contact_phone.strip()
        if jo_project_site_address.strip():
            extracted.ship_to_lines = [ln.strip() for ln in jo_project_site_address.splitlines() if ln.strip()]
        water_service_size_pressure = request.form.get('water_service_size_pressure', '')
        max_vehicle_height = request.form.get('max_vehicle_height', '')
        brush_system_model = request.form.get('brush_system_model', '')
        gc_contact_name = request.form.get('gc_contact_name', '')
        gc_contact_email = request.form.get('gc_contact_email', '')
        gc_contact_phone = request.form.get('gc_contact_phone', '')
        project_notes = request.form.get('project_notes', '')

        filled_pdf = fill_f_eng_001(
            extracted,
            type_request=type_request,
            doc_types=doc_types,
            project_types=project_types,
            requester=requester,
            amperage=amperage,
            water_service_size_pressure=water_service_size_pressure,
            max_vehicle_height=max_vehicle_height,
            brush_system_model=brush_system_model,
            project_notes=project_notes,
            gc_contact_name=gc_contact_name,
            gc_contact_email=gc_contact_email,
            gc_contact_phone=gc_contact_phone,
            request_date_override=jo_request_date,
            due_date_override=jo_due_date,
        )
    except Exception as e:
        flash(f"Failed to parse/fill PDF: {e}")
        return redirect(url_for("index"))

    # Name output with job number when available
    out_name = f"F-ENG-001_{extracted.job_number or 'filled'}.pdf"
    return send_file(
        io.BytesIO(filled_pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=out_name,
    )


if __name__ == "__main__":
    # For local dev only
    app.run(host="0.0.0.0", port=5000, debug=True)
