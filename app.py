from __future__ import annotations

import io
import json
import re
import os
import secrets
from pathlib import Path
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from functools import wraps

import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, flash, redirect, url_for, jsonify, session
from werkzeug.security import check_password_hash


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
    """Extract raw text from ONLY the FIRST page of a PDF using PyMuPDF.

    Per your requirement, we assume the first page contains all information
    needed to populate the F-ENG-001 form, and we intentionally ignore pages 2+.

    This also reduces the chance of accidentally picking up unrelated voltages,
    contacts, etc. from later pages (item descriptions, notes, terms, etc.).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count <= 0:
        return ""
    return doc[0].get_text("text")


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

    def _looks_like_person_name(s: str) -> bool:
        """Heuristic: detect an all-caps personal name line like 'JEFF HARRIS'.

        We keep this conservative to avoid grabbing labels and other header text.
        """
        s = (s or "").strip()
        if not s:
            return False
        if "@" in s:
            return False
        if ":" in s:
            return False
        if re.search(r"\d", s):
            return False
        if len(s) > 60:
            return False
        if not re.match(r"^[A-Z][A-Z .\-']+$", s):
            return False

        tokens = [t for t in re.split(r"\s+", s) if t]
        if len(tokens) < 2:
            return False

        # Common non-name tokens we don't want to treat as a contact.
        bad = {
            "RELEASED",
            "PREPAID",
            "TERMS",
            "CONFIRMED",
            "SCOPE",
            "PRINT",
            "REQUESTED",
            "BUY",
            "AMERICA",
            "ORDER",
            "CONTRACT",
            "JOB",
            "REQUIREMENTS",
        }
        if any(t in bad for t in tokens):
            return False

        return True

    def _infer_contact_name_near_email(email: str) -> str:
        """Infer end-customer contact name by looking near the email line.

        This specifically addresses Release PDFs where the first page has:
          <email>
          <CONTACT NAME>
        while the only explicit 'Contact:' label may contain 'RELEASED'.
        """
        e = (email or "").strip()
        if not e:
            return ""

        # Find the line index that contains the email.
        idxs = [i for i, ln in enumerate(lines) if e.lower() in (ln or "").lower()]
        if not idxs:
            # Fallback: find any line that contains an email, and use that.
            for i, ln in enumerate(lines):
                if "@" in (ln or "") and _first_email(ln):
                    idxs = [i]
                    break

        for i in idxs:
            # Prefer the next line (most common in the provided Release PDF).
            for offset in (1, -1, 2, -2, 3, -3):
                j = i + offset
                if j < 0 or j >= len(lines):
                    continue
                cand = (lines[j] or "").strip()
                if _looks_like_person_name(cand):
                    return cand

            # Also handle cases where name and email appear on the same line.
            ln = (lines[i] or "").strip()
            before = re.split(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", ln, flags=re.I)[0].strip()
            if _looks_like_person_name(before):
                return before

        return ""

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


    # If Voltage label parsing missed it (common in quote/contract PDFs), try a supported-voltage scan
    if not data.voltage:
        data.voltage = _extract_supported_3ph_voltage(text)

    # Canonicalize supported site voltages to our standard output
    if data.voltage:
        nv = _extract_supported_3ph_voltage(data.voltage)
        if nv:
            data.voltage = nv

    # CONTACT EMAIL
    # In Release PDFs, the end-customer email is often present on page 1.
    data.contact_email = _first_email(text)

    # CONTACT NAME
    # Some releases contain a literal "Contact: JEFF HARRIS" (often on later pages),
    # but on page 1 we may only have "Contact: RELEASED" plus the name printed near
    # the email line. Because we intentionally parse only the first page, we:
    #   1) use any explicit Contact: values except "RELEASED"
    #   2) otherwise infer name from the line adjacent to the email.
    contacts = [c.strip() for c in re.findall(r"Contact:\s*([^\n]+)", text, flags=re.I)]
    contacts = [c for c in contacts if c and c.strip().lower() not in {"released"}]
    if contacts:
        data.contact_name = contacts[-1]
    if not data.contact_name:
        data.contact_name = _infer_contact_name_near_email(data.contact_email)

    # CONTACT PHONE
    # Strategy:
    # 1) Prefer a phone number closest to an explicit "Phone:" / "Telephone" label (when present).
    # 2) If missing (common on some Release PDFs), fall back to the number that appears under "Comment:".
    # 3) As a last resort, pick the phone closest to the detected contact email/name.
    phone_re = r"(\(?\d{3}\)?[-\.\s]?\d{3}[-\.\s]?\d{4})"

    p = _find_closest(r"\bPhone\s*:?", phone_re)
    if not p:
        p = _find_closest(r"\bTelephone\b", phone_re)

    if not p:
        # Many Release PDFs place the contact phone under "Comment:" on the next line.
        p = _find_after(r"\bComment\s*:?", phone_re, max_look=10)

    if not p and data.contact_email:
        p = _find_closest(re.escape(data.contact_email), phone_re)

    if not p and data.contact_name:
        p = _find_closest(re.escape(data.contact_name), phone_re)

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


SUPPORTED_3PH_VOLTAGES = {208, 240, 460, 480}


def _canonical_3ph_voltage(v: int) -> str:
    return f"{v}V - 3 Phase"


def _extract_supported_3ph_voltage(text: str) -> str:
    """Find a supported 3-phase voltage in text and return canonical form.

    Examples recognized:
      - 208 VOLT, 208 VOLTS, 208 V, 208V
      - 480 VAC, 480VAC
      - 240 3 PHASE, 460 3PH

    Returns: e.g., "208V - 3 Phase" (empty string if not found)
    """
    t = _normalize_text(text or "")

    # Prefer explicit units like 208V, 208 VOLT, 208 VAC, etc.
    unit_pat = re.compile(r"\b(208|240|460|480)\s*(?:V(?:AC)?|VOLTS?|VOLT)(?![A-Z])", flags=re.I)
    m = unit_pat.search(t)
    if m:
        return _canonical_3ph_voltage(int(m.group(1)))

    # Support cases like: "208 3 PHASE" / "208 3PH" without a unit
    phase_pat = re.compile(
        r"\b(208|240|460|480)\b(?=[^\n\r]{0,30}\b(?:3\s*PH(?:ASE)?|3PH|THREE\s*PH(?:ASE)?)\b)",
        flags=re.I,
    )
    m2 = phase_pat.search(t)
    if m2:
        return _canonical_3ph_voltage(int(m2.group(1)))

    return ""


def _extract_voltage_before_model(text: str) -> str:
    """Extract voltage from Quote/Contract PDFs.

    Notes:
    - These PDFs behave like tables; PyMuPDF text order is often non-intuitive.
    - We therefore scan the header chunk (typically everything before "Description").

    Supported site voltages (all treated as 3-phase): 208V, 240V, 460V, 480V.
    We canonicalize to: '<voltage>V - 3 Phase'.
    """
    header = text
    if re.search(r"\bDescription\b", text, flags=re.I):
        header = re.split(r"\bDescription\b", text, flags=re.I)[0]
    else:
        # Fallback: keep scanning bounded to the front of the document
        header = text[:4000]

    # 1) Best effort: find a supported voltage in the header chunk
    supported = _extract_supported_3ph_voltage(header)
    if supported:
        return supported

    # 2) If there is an explicit Voltage label, try parsing its value
    m = re.search(r"\bVoltage\b\s*(?:Available)?\s*[:\-]?\s*([^\n\r]+)", header, flags=re.I)
    if m:
        v = m.group(1).strip()
        if v and len(v) <= 60 and not re.search(r"\b(Model\s+Number|Qty|Amount|Prepared\s+for|Quotation)\b", v, flags=re.I):
            supported2 = _extract_supported_3ph_voltage(v)
            if supported2:
                return supported2
            return v

    # 3) Final conservative fallback
    m2 = re.search(r"\b(208|240|460|480)\s*(?:V(?:AC)?|VOLTS?|VOLT)\b", header, flags=re.I)
    if m2:
        return _canonical_3ph_voltage(int(m2.group(1)))

    return ""


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


AUTH_USERNAME_DEFAULT = "NSCorp"
# Password hash for the default password (28309@Crocker). Uses Werkzeug's scrypt.
AUTH_PASSWORD_HASH_DEFAULT = (
    "scrypt:32768:8:1$RCEiro0Hkor5EXyd$"
    "9c6122a946281e22edce2f62219ef29d"
    "04489c0b23954f4f288abd3207d033b8"
    "2baad51d330da3a93f339b0458e8aecd"
    "b39573097be05a56c74a3b1ae3044e9b"
)

PROJECTS_JSON_PATH = BASE_DIR / "projects.json"
PROJECT_ADMIN_USERNAME_DEFAULT = "engineering@nswash.com"
PROJECT_ADMIN_PASSWORD_HASH_DEFAULT = AUTH_PASSWORD_HASH_DEFAULT

SALES_PERSON_OPTIONS = [
    "Victor Sanchez",
    "Michael Howlett",
    "David Newell",
    "Brad Quay",
    "Max Diaz",
    "Francis Tenggardjaja",
]
PROJECT_STATUS_OPTIONS = ["Contract", "No Contract"]
FIELD_ENGINEER_OPTIONS = ["Behrad", "Kash", "Shawn", "Xiaohan"]


def _is_logged_in() -> bool:
    return bool(session.get("logged_in"))


def _is_project_admin_logged_in() -> bool:
    return bool(session.get("project_admin_logged_in"))


def _safe_next_path(next_path: str | None) -> str:
    """Prevent open-redirects. Only allow local paths like '/'."""
    if not next_path:
        return "/"
    next_path = next_path.strip()
    if not next_path.startswith("/"):
        return "/"
    if next_path.startswith("//"):
        return "/"
    return next_path


def _normalize_project_status(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw == "contract":
        return "Contract"
    if raw == "no contract":
        return "No Contract"
    return "No Contract"


def _normalize_sales_person(value: str) -> str:
    raw = (value or "").strip()
    if raw in SALES_PERSON_OPTIONS:
        return raw
    return "Unassigned"


def _normalize_engineer(value: str) -> str:
    raw = (value or "").strip()
    if raw:
        return raw
    return "Unassigned"


def _is_valid_engineer_choice(value: str) -> bool:
    return (value or "").strip() in FIELD_ENGINEER_OPTIONS


def _parse_iso_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_projects() -> list[dict]:
    if not PROJECTS_JSON_PATH.exists():
        return []

    try:
        payload = json.loads(PROJECTS_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(payload, list):
        return []

    cleaned: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        cleaned.append({
            "project_number": str(row.get("project_number", "")).strip(),
            "project_name": str(row.get("project_name", "")).strip(),
            "engineer": _normalize_engineer(str(row.get("engineer", ""))),
            "project_status": _normalize_project_status(str(row.get("project_status", ""))),
            "sales_person": _normalize_sales_person(str(row.get("sales_person", ""))),
            "start_date": str(row.get("start_date", "")).strip(),
            "projected_end_date": str(row.get("projected_end_date", "")).strip(),
            "description": str(row.get("description", "")).strip(),
            "created_at": str(row.get("created_at", "")).strip(),
        })

    cleaned.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return cleaned


def _save_projects(projects: list[dict]) -> None:
    PROJECTS_JSON_PATH.write_text(json.dumps(projects, indent=2), encoding="utf-8")


def _counter_chart_data(items: list[dict], key: str, default_label: str) -> dict[str, list]:
    counts = Counter((item.get(key) or default_label) for item in items)
    labels = list(counts.keys())
    values = [counts[label] for label in labels]
    return {"labels": labels, "values": values}


def _build_timeline_rows(projects: list[dict]) -> tuple[list[dict], str | None, str | None, float | None, str]:
    rows: list[dict] = []
    parsed_ranges: list[tuple[date, date]] = []
    today = datetime.now(APP_TZ).date()

    for project in projects:
        start_dt = _parse_iso_date(project.get("start_date", ""))
        end_dt = _parse_iso_date(project.get("projected_end_date", ""))

        if start_dt and end_dt and end_dt < start_dt:
            end_dt = start_dt

        if start_dt and not end_dt:
            end_dt = start_dt
        if end_dt and not start_dt:
            start_dt = end_dt

        row = dict(project)
        row["timeline_start"] = start_dt
        row["timeline_end"] = end_dt
        row["timeline_progress_pct"] = None
        row["timeline_total_days"] = None
        row["timeline_elapsed_days"] = None
        row["timeline_remaining_days"] = None
        row["timeline_status_text"] = "No timeline data"
        rows.append(row)

        if start_dt and end_dt:
            parsed_ranges.append((start_dt, end_dt))

    if not parsed_ranges:
        return rows, None, None, None, today.isoformat()

    overall_start = min(min(start for start, _ in parsed_ranges), today)
    overall_end = max(max(end for _, end in parsed_ranges), today)

    if overall_end < overall_start:
        overall_end = overall_start

    total_days = max((overall_end - overall_start).days, 1)
    today_pct = ((today - overall_start).days / total_days) * 100 if total_days else 0
    today_pct = round(min(max(today_pct, 0), 100), 2)

    for row in rows:
        start_dt = row.get("timeline_start")
        end_dt = row.get("timeline_end")
        if start_dt and end_dt:
            left_pct = ((start_dt - overall_start).days / total_days) * 100
            span_days = max((end_dt - start_dt).days + 1, 1)
            width_pct = max((span_days / total_days) * 100, 2.2)
            if left_pct + width_pct > 100:
                width_pct = max(100 - left_pct, 2.2)
            row["timeline_left_pct"] = round(left_pct, 2)
            row["timeline_width_pct"] = round(width_pct, 2)
            row["timeline_total_days"] = span_days

            if today < start_dt:
                row["timeline_elapsed_days"] = 0
                row["timeline_remaining_days"] = span_days
                row["timeline_progress_pct"] = 0
                row["timeline_status_text"] = f"Starts in {(start_dt - today).days} day(s)"
            elif today > end_dt:
                row["timeline_elapsed_days"] = span_days
                row["timeline_remaining_days"] = 0
                row["timeline_progress_pct"] = 100
                row["timeline_status_text"] = f"Ended {(today - end_dt).days} day(s) ago"
            else:
                elapsed_days = max((today - start_dt).days + 1, 1)
                remaining_days = max((end_dt - today).days, 0)
                progress_pct = round((elapsed_days / span_days) * 100, 2)
                row["timeline_elapsed_days"] = elapsed_days
                row["timeline_remaining_days"] = remaining_days
                row["timeline_progress_pct"] = min(max(progress_pct, 0), 100)
                row["timeline_status_text"] = f"{elapsed_days} day(s) elapsed | {remaining_days} day(s) remaining"
        else:
            row["timeline_left_pct"] = None
            row["timeline_width_pct"] = None

    rows.sort(
        key=lambda item: (
            item.get("timeline_start") is None,
            item.get("timeline_start") or date.max,
            item.get("project_name", ""),
        )
    )

    return rows, overall_start.isoformat(), overall_end.isoformat(), today_pct, today.isoformat()


def _dashboard_data(projects: list[dict]) -> dict:
    engineer_chart = _counter_chart_data(projects, "engineer", "Unassigned")
    sales_chart = _counter_chart_data(projects, "sales_person", "Unassigned")

    status_counts = Counter(_normalize_project_status(project.get("project_status", "")) for project in projects)
    status_labels = PROJECT_STATUS_OPTIONS
    status_values = [status_counts.get(label, 0) for label in status_labels]

    timeline_rows, timeline_start, timeline_end, timeline_today_pct, today_iso = _build_timeline_rows(projects)

    return {
        "engineer_chart": engineer_chart,
        "sales_chart": sales_chart,
        "status_chart": {"labels": status_labels, "values": status_values},
        "timeline_rows": timeline_rows,
        "timeline_start": timeline_start,
        "timeline_end": timeline_end,
        "timeline_today_pct": timeline_today_pct,
        "timeline_today": today_iso,
    }


app = Flask(__name__)

# Session / cookie hardening (still requires HTTPS in production)
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# Optional whole-app auth (kept for backward compatibility with the existing app)
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", AUTH_USERNAME_DEFAULT)
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", AUTH_PASSWORD_HASH_DEFAULT)
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}

# Dedicated Add Project credentials
PROJECT_ADMIN_USERNAME = os.environ.get("PROJECT_ADMIN_USERNAME", PROJECT_ADMIN_USERNAME_DEFAULT)
PROJECT_ADMIN_PASSWORD_HASH = os.environ.get("PROJECT_ADMIN_PASSWORD_HASH", PROJECT_ADMIN_PASSWORD_HASH_DEFAULT)


@app.before_request
def _require_login_for_everything():
    # This preserves the old optional whole-app gate. Leave AUTH_ENABLED unset/0
    # if you only want Add Project to be password protected.
    if not AUTH_ENABLED:
        return None

    public_endpoints = {
        "login",
        "login_post",
        "logout",
        "project_admin_login",
        "project_admin_login_post",
        "static",
    }
    if request.endpoint in public_endpoints:
        return None

    if _is_logged_in():
        return None

    wants_json = request.accept_mimetypes.best == "application/json" or request.path.startswith("/parse")
    if wants_json:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    return redirect(url_for("login", next=_safe_next_path(request.full_path or request.path)))


@app.get("/login")
def login():
    if _is_logged_in():
        return redirect(url_for("index"))
    next_path = _safe_next_path(request.args.get("next"))
    return render_template("login.html", next=next_path)


@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_path = _safe_next_path(request.form.get("next"))

    if username == AUTH_USERNAME and check_password_hash(AUTH_PASSWORD_HASH, password):
        session["logged_in"] = True
        session["username"] = username
        return redirect(next_path or url_for("index"))

    flash("Invalid username or password.")
    return redirect(url_for("login", next=next_path))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    projects = _load_projects()
    dashboard = _dashboard_data(projects)
    esr_link = url_for("esr_index")
    contract_count = sum(1 for project in projects if project.get("project_status") == "Contract")
    return render_template(
        "dashboard.html",
        projects=projects,
        total_projects=len(projects),
        total_engineers=len(dashboard["engineer_chart"]["labels"]),
        total_sales_people=len([label for label in dashboard["sales_chart"]["labels"] if label != "Unassigned"]),
        contract_count=contract_count,
        engineer_chart_labels=dashboard["engineer_chart"]["labels"],
        engineer_chart_values=dashboard["engineer_chart"]["values"],
        sales_chart_labels=dashboard["sales_chart"]["labels"],
        sales_chart_values=dashboard["sales_chart"]["values"],
        status_chart_labels=dashboard["status_chart"]["labels"],
        status_chart_values=dashboard["status_chart"]["values"],
        timeline_rows=dashboard["timeline_rows"],
        timeline_start=dashboard["timeline_start"],
        timeline_end=dashboard["timeline_end"],
        timeline_today_pct=dashboard["timeline_today_pct"],
        timeline_today=dashboard["timeline_today"],
        esr_link=esr_link,
        add_project_logged_in=_is_project_admin_logged_in(),
    )


@app.get("/form")
def esr_index():
    return render_template("esr.html")


@app.get("/esr")
def esr_legacy_redirect():
    return redirect(url_for("esr_index"), code=301)


@app.get("/project-admin/login")
def project_admin_login():
    if _is_project_admin_logged_in():
        return redirect(url_for("add_project"))
    next_path = _safe_next_path(request.args.get("next") or url_for("add_project"))
    return render_template("add_project_login.html", next=next_path)


@app.post("/project-admin/login")
def project_admin_login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_path = _safe_next_path(request.form.get("next") or url_for("add_project"))

    if username == PROJECT_ADMIN_USERNAME and check_password_hash(PROJECT_ADMIN_PASSWORD_HASH, password):
        session["project_admin_logged_in"] = True
        session["project_admin_username"] = username
        return redirect(next_path)

    flash("Invalid Add Project username or password.")
    return redirect(url_for("project_admin_login", next=next_path))


@app.get("/project-admin/logout")
def project_admin_logout():
    session.pop("project_admin_logged_in", None)
    session.pop("project_admin_username", None)
    return redirect(url_for("index"))


@app.route("/add-project", methods=["GET", "POST"])
def add_project():
    if not _is_project_admin_logged_in():
        return redirect(url_for("project_admin_login", next=url_for("add_project")))

    if request.method == "POST":
        project_number = (request.form.get("project_number") or "").strip()
        project_name = (request.form.get("project_name") or "").strip()
        raw_engineer = (request.form.get("engineer") or "").strip()
        engineer = _normalize_engineer(raw_engineer)
        raw_project_status = (request.form.get("project_status") or "").strip()
        raw_sales_person = (request.form.get("sales_person") or "").strip()
        project_status = _normalize_project_status(raw_project_status)
        sales_person = _normalize_sales_person(raw_sales_person)
        start_date = (request.form.get("start_date") or "").strip()
        projected_end_date = (request.form.get("projected_end_date") or "").strip()
        description = (request.form.get("description") or "").strip()

        if not all([project_number, project_name, raw_engineer, raw_project_status, raw_sales_person, start_date, projected_end_date, description]):
            flash("Please fill in all project fields before saving.")
            return render_template(
                "add_project.html",
                sales_person_options=SALES_PERSON_OPTIONS,
                project_status_options=PROJECT_STATUS_OPTIONS,
                engineer_options=FIELD_ENGINEER_OPTIONS,
                project_number=project_number,
                project_name=project_name,
                engineer=engineer,
                project_status=project_status,
                sales_person=sales_person,
                start_date=start_date,
                projected_end_date=projected_end_date,
                description=description,
            )

        if not _is_valid_engineer_choice(raw_engineer):
            flash("Please select a valid Project Engineer from the dropdown.")
            return render_template(
                "add_project.html",
                sales_person_options=SALES_PERSON_OPTIONS,
                project_status_options=PROJECT_STATUS_OPTIONS,
                engineer_options=FIELD_ENGINEER_OPTIONS,
                project_number=project_number,
                project_name=project_name,
                engineer=engineer,
                project_status=project_status,
                sales_person=sales_person,
                start_date=start_date,
                projected_end_date=projected_end_date,
                description=description,
            )

        start_dt = _parse_iso_date(start_date)
        end_dt = _parse_iso_date(projected_end_date)
        if not start_dt or not end_dt:
            flash("Please select valid start and end dates.")
            return render_template(
                "add_project.html",
                sales_person_options=SALES_PERSON_OPTIONS,
                project_status_options=PROJECT_STATUS_OPTIONS,
                engineer_options=FIELD_ENGINEER_OPTIONS,
                project_number=project_number,
                project_name=project_name,
                engineer=engineer,
                project_status=project_status,
                sales_person=sales_person,
                start_date=start_date,
                projected_end_date=projected_end_date,
                description=description,
            )

        if end_dt < start_dt:
            flash("Projected End Date cannot be earlier than Start Date.")
            return render_template(
                "add_project.html",
                sales_person_options=SALES_PERSON_OPTIONS,
                project_status_options=PROJECT_STATUS_OPTIONS,
                engineer_options=FIELD_ENGINEER_OPTIONS,
                project_number=project_number,
                project_name=project_name,
                engineer=engineer,
                project_status=project_status,
                sales_person=sales_person,
                start_date=start_date,
                projected_end_date=projected_end_date,
                description=description,
            )

        projects = _load_projects()
        duplicate = next((p for p in projects if p.get("project_number", "").lower() == project_number.lower()), None)
        if duplicate:
            flash("That project number already exists. Use a different project number.")
            return render_template(
                "add_project.html",
                sales_person_options=SALES_PERSON_OPTIONS,
                project_status_options=PROJECT_STATUS_OPTIONS,
                engineer_options=FIELD_ENGINEER_OPTIONS,
                project_number=project_number,
                project_name=project_name,
                engineer=engineer,
                project_status=project_status,
                sales_person=sales_person,
                start_date=start_date,
                projected_end_date=projected_end_date,
                description=description,
            )

        projects.append({
            "project_number": project_number,
            "project_name": project_name,
            "engineer": engineer,
            "project_status": project_status,
            "sales_person": sales_person,
            "start_date": start_date,
            "projected_end_date": projected_end_date,
            "description": description,
            "created_at": datetime.now(APP_TZ).isoformat(),
        })
        _save_projects(projects)
        flash("Project added successfully.")
        return redirect(url_for("index"))

    return render_template(
        "add_project.html",
        sales_person_options=SALES_PERSON_OPTIONS,
        project_status_options=PROJECT_STATUS_OPTIONS,
        engineer_options=FIELD_ENGINEER_OPTIONS,
    )


@app.post("/delete-project/<project_number>")
def delete_project(project_number: str):
    if not _is_project_admin_logged_in():
        return redirect(url_for("project_admin_login", next=url_for("index")))

    original = _load_projects()
    remaining = [
        project for project in original
        if project.get("project_number", "").lower() != project_number.lower()
    ]

    if len(remaining) == len(original):
        flash("Project not found. Nothing was deleted.")
    else:
        _save_projects(remaining)
        flash(f"Project {project_number} deleted.")

    return redirect(url_for("index"))


@app.post("/parse")
def parse_endpoint():
    """
    Parse the uploaded JO/Release PDF and return extracted fields as JSON.
    Used to auto-populate editable inputs on the ESR page.
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
                return redirect(url_for("esr_index"))

            release_bytes = f.read()
            if len(release_bytes) > 15 * 1024 * 1024:
                flash("File too large (max 15 MB).")
                return redirect(url_for("esr_index"))

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
        return redirect(url_for("esr_index"))

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
