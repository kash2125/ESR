# Engineering Dashboard + ESR Generator

This Flask app now has two main areas:

1. **Dashboard homepage** at `/`
   - Shows current engineering workload
   - Displays a colorful doughnut chart of active projects by engineer
   - Lists active projects
   - Includes an **Add Project** button
   - Includes a **Generate ESR Form** button

2. **ESR generator** at `/form`
   - Keeps the existing Release/JO → F-ENG-001 workflow
   - Uses the same `/parse` and `/generate` endpoints as before

## Add Project protection

The Add Project page is protected separately from the rest of the app.

Default credentials:
- Username: `engineering@nswash.com`
- Password: `28309@Crocker`

You can override them with environment variables:

```bash
export PROJECT_ADMIN_USERNAME="engineering@nswash.com"
export PROJECT_ADMIN_PASSWORD_HASH="<werkzeug password hash>"
```

## Existing ESR flow

The ESR UI now lives at `/form`.

The dashboard button points to the local `/form` route.

## Optional whole-app auth

The older whole-app auth pattern is still in the code for backward compatibility, but it is **off by default**.

```bash
export AUTH_ENABLED=0
```

If you turn it on, the entire app uses the legacy login session:
- Username default: `NSCorp`
- Password default: `28309@Crocker`

## Project storage

Projects are now stored in a SQLite database file.

Default path:
- `projects.db` in the app folder

Optional environment variable:
- `PROJECTS_DB_PATH`

Examples:
```bash
# local
export PROJECTS_DB_PATH=./projects.db

# Render with a persistent disk mounted at /var/data
export PROJECTS_DB_PATH=/var/data/projects.db
```

The app will also automatically import any existing `projects.json` data into SQLite the first time it starts.

## Run locally

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

Open:
- Dashboard: `http://localhost:5000`
- ESR page: `http://localhost:5000/form`

## Notes

- The ESR parsing/filling logic is unchanged except that route redirects now return to `/esr` instead of `/`.
- The dashboard chart updates automatically after a project is added.
- Project numbers are treated as unique in the Add Project form.
