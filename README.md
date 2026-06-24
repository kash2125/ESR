# ESR Engineering Dashboard

This project is a browser-based engineering dashboard and ESR form generator.

## What The Website Does

- Shows active engineering projects from Supabase.
- Tracks project engineer, sales person, contract status, start date, and update history.
- Splits project notes into `Latest Update` and `Previous Updates`.
- Lets logged-in users add, edit, delete, complete, and restore projects.
- Keeps completed projects in a separate completed section.
- Provides presentation mode for filtered project reviews.
- Exports active projects as printable meeting notes.
- Creates an updates-only printout for email workflows.
- Generates ESR/F-ENG-001 forms from the ESR page.

## Main Pages

- `index.html`: dashboard, charts, active projects, completed projects, presentation mode, exports.
- `add_project.html`: add a new project.
- `edit_project.html`: add a new timestamped update to an existing project.
- `login.html`: Supabase login.
- `esr.html`: ESR form generator.

## Project Data

Projects are stored in the Supabase `projects` table.

The app expects fields like:

- `project_number`
- `project_name`
- `engineer`
- `project_status`
- `sales_person`
- `start_date`
- `completed_date`
- `description`
- `description_html`
- `description_color`
- `created_at`

Completed projects use `completed_date`. Run this once in the Supabase SQL editor if your table does not have it yet:

```sql
alter table public.projects
add column if not exists completed_date date;

alter table public.projects
alter column completed_date drop not null;
```

## Project Updates

The dashboard renders project descriptions as:

- `Latest Update`
- `Previous Updates`

New edits are timestamped and placed at the top. Imported old project notes can be moved into Previous Updates using the cleanup script below.

Red text from older project records is converted to bold text.

## Completed Projects

Logged-in users can click `Completed` on an active project. This:

- Changes `project_status` to `Completed`.
- Saves today as the completed date.
- Moves the project out of Active Projects and into Completed Projects.

Completed projects have a `Restore to Active` button in case something was marked complete by mistake.

## Exports

`Export Active PDF` creates a printable meeting-notes packet:

- One project per page when possible.
- Project information and update history at the top.
- Discussion notes and action items at the bottom.

`Email Updates` creates an updates-only printout without discussion notes or action items. Browser security does not allow the site to automatically attach a PDF to an email draft. For automatic sending with attachments, use a Supabase Edge Function and an email provider like Resend.

## Import Old getesr.com Data

The old hosted dashboard exposes its project list in the page source as `projectsData`.

Preview/export only:

```powershell
cd "C:\Users\KayleeMorales\Documents\GitHub\ESR"
& "C:\Users\KayleeMorales\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" .\tools\import-old-getesr-projects.mjs --dry-run
```

Import into Supabase:

```powershell
cd "C:\Users\KayleeMorales\Documents\GitHub\ESR"
$env:SUPABASE_EMAIL="your-login@example.com"
$env:SUPABASE_PASSWORD="your-password"
& "C:\Users\KayleeMorales\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" .\tools\import-old-getesr-projects.mjs
```

The import script writes a local backup:

```text
old-getesr-projects-export.json
```

## Cleanup Imported Project Notes

After importing old data, run this cleanup to:

- Convert old red text to bold.
- Move raw imported descriptions into Previous Updates.
- Keep newer timestamped update history intact.

Preview only:

```powershell
cd "C:\Users\KayleeMorales\Documents\GitHub\ESR"
$env:SUPABASE_EMAIL="your-login@example.com"
$env:SUPABASE_PASSWORD="your-password"
& "C:\Users\KayleeMorales\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" .\tools\cleanup-imported-project-updates.mjs --dry-run
```

Apply cleanup:

```powershell
cd "C:\Users\KayleeMorales\Documents\GitHub\ESR"
$env:SUPABASE_EMAIL="your-login@example.com"
$env:SUPABASE_PASSWORD="your-password"
& "C:\Users\KayleeMorales\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" .\tools\cleanup-imported-project-updates.mjs
```

## Supabase Configuration

The browser app reads Supabase settings from `supabaseClient.js`.

Do not put private service-role keys or email-provider API keys in browser files. Use Supabase Edge Function secrets for server-side keys.
