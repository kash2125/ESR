import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PROJECTS_TABLE = "projects";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");

function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

async function readSupabaseConfig() {
  const source = await fs.readFile(path.join(repoRoot, "supabaseClient.js"), "utf8");
  const url = source.match(/SUPABASE_URL\s*=\s*"([^"]+)"/)?.[1];
  const anonKey = source.match(/SUPABASE_ANON_KEY\s*=\s*"([^"]+)"/)?.[1];

  if (!url || !anonKey) {
    throw new Error("Could not read SUPABASE_URL and SUPABASE_ANON_KEY from supabaseClient.js.");
  }

  return { url, anonKey };
}

async function signIn({ url, anonKey, email, password }) {
  const response = await fetch(`${url}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: {
      apikey: anonKey,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ email, password })
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.access_token) {
    throw new Error(`Supabase login failed: ${payload.error_description || payload.msg || response.statusText}`);
  }

  return payload.access_token;
}

function cleanString(value) {
  return String(value ?? "").trim();
}

function convertRedTextToBold(html) {
  const source = cleanString(html);
  const spanTagPattern = /<span\b[^>]*>|<\/span>/gi;
  const stack = [];
  let output = "";
  let lastIndex = 0;
  let match;

  while ((match = spanTagPattern.exec(source))) {
    output += source.slice(lastIndex, match.index);
    const tag = match[0];

    if (/^<\/span/i.test(tag)) {
      const replacement = stack.pop();
      output += replacement === "strong" ? "</strong>" : "";
    } else {
      const style = tag.match(/style=["']([^"']*)["']/i)?.[1]?.toLowerCase() || "";
      if (style.includes("#c62828") || style.includes("red") || style.includes("rgb(198, 40, 40)") || style.includes("rgb(198,40,40)")) {
        stack.push("strong");
        output += "<strong>";
      } else {
        stack.push("remove");
      }
    }

    lastIndex = spanTagPattern.lastIndex;
  }

  output += source.slice(lastIndex);
  return output;
}

function looksLikeRawImportedDescription(html) {
  const value = cleanString(html).toLowerCase();
  return !value.includes("latestupdatebox")
    && !value.includes("previousupdatesbox")
    && !value.includes("descriptionentry")
    && !value.includes("<hr");
}

function asPreviousUpdatesOnly(html) {
  return `
    <section class="previousUpdatesBox" data-all-previous-updates="true">
      <div class="projectUpdate">${html || "No description added."}</div>
    </section>
  `.trim();
}

function normalizeDescription(html) {
  const bolded = convertRedTextToBold(html || "No description added.");
  return looksLikeRawImportedDescription(bolded) ? asPreviousUpdatesOnly(bolded) : bolded;
}

async function loadProjects({ url, anonKey, accessToken }) {
  const response = await fetch(`${url}/rest/v1/${PROJECTS_TABLE}?select=project_number,description,description_html&order=created_at.desc`, {
    headers: {
      apikey: anonKey,
      Authorization: `Bearer ${accessToken}`
    }
  });

  if (!response.ok) {
    throw new Error(`Could not load projects: ${await response.text()}`);
  }

  return response.json();
}

async function updateProjectDescription({ url, anonKey, accessToken, projectNumber, description }) {
  const filter = `project_number=eq.${encodeURIComponent(projectNumber)}`;
  const response = await fetch(`${url}/rest/v1/${PROJECTS_TABLE}?${filter}`, {
    method: "PATCH",
    headers: {
      apikey: anonKey,
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      Prefer: "return=minimal"
    },
    body: JSON.stringify({
      description,
      description_html: description,
      description_color: "#000000"
    })
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }
}

async function main() {
  const dryRun = hasFlag("dry-run");
  const email = process.env.SUPABASE_EMAIL || "";
  const password = process.env.SUPABASE_PASSWORD || "";

  if (!email || !password) {
    throw new Error("Set SUPABASE_EMAIL and SUPABASE_PASSWORD before running this cleanup.");
  }

  const config = await readSupabaseConfig();
  const accessToken = await signIn({ ...config, email, password });
  const projects = await loadProjects({ ...config, accessToken });

  let changed = 0;

  for (const project of projects) {
    const current = cleanString(project.description_html || project.description);
    const next = normalizeDescription(current);

    if (next === current) continue;

    changed += 1;
    console.log(`${dryRun ? "Would update" : "Updating"} ${project.project_number}`);

    if (!dryRun) {
      await updateProjectDescription({
        ...config,
        accessToken,
        projectNumber: project.project_number,
        description: next
      });
    }
  }

  console.log(dryRun
    ? `Dry run complete. ${changed} project(s) would be updated.`
    : `Cleanup complete. Updated ${changed} project(s).`);
}

main().catch(error => {
  console.error(error.message || error);
  process.exitCode = 1;
});
