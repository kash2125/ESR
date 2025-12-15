# Release → F-ENG-001 Generator (Flask + PyMuPDF)

This is a small web app that:

1) Accepts an uploaded **Release_**-style PDF  
2) Extracts key fields (job number, customer name, voltage, contact info, ship-to address)  
3) Outputs a filled **F-ENG-001** PDF by writing into the *actual* form fields (AcroForm widgets).

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Open: http://localhost:5000

## Notes / realism

- Parsing is **regex-based** on extracted PDF text. It works best when the Release format is consistent.
- For production:
  - set `app.secret_key` from an environment variable
  - add an upload virus scan and tighter file validation
  - consider storing no files on disk (this app uses in-memory bytes)
  - put behind a reverse proxy (nginx) and set size/time limits

## Customize parsing

Edit `parse_release_pdf()` in `app.py`.

Common variations you may need to handle:
- "Job Order#" vs "Job order#" vs missing colon
- voltage formats: "208 VOLT", "208V", "208 VAC"
- contact location: sometimes on last page, sometimes near top
- ship-to block length (may include attn lines)
