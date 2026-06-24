import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_OLD_ESR_URL = "https://www.getesr.com/?engineer=__all__&sort=default";
const PROJECTS_TABLE = "projects";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");

function readArg(name, fallback = "") {
  const prefix = `--${name}=`;
  const found = process.argv.find(arg => arg.startsWith(prefix));
  return found ? found.slice(prefix.length) : fallback;
}

function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

async function readSupabaseConfig() {
  const configPath = path.join(repoRoot, "supabaseClient.js");
  const source = await fs.readFile(configPath, "utf8");
  const url = source.match(/SUPABASE_URL\s*=\s*"([^"]+)"/)?.[1];
  const anonKey = source.match(/SUPABASE_ANON_KEY\s*=\s*"([^"]+)"/)?.[1];

  if (!url || !anonKey) {
    throw new Error("Could not read SUPABASE_URL and SUPABASE_ANON_KEY from supabaseClient.js.");
  }

  return { url, anonKey };
}

function findMatchingBracket(source, startIndex) {
  let depth = 0;
  let quote = "";
  let escaped = false;

  for (let index = startIndex; index < source.length; index += 1) {
    const char = source[index];

    if (quote) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === quote) {
        quote = "";
      }
      continue;
    }

    if (char === '"' || char === "'" || char === "`") {
      quote = char;
      continue;
    }

    if (char === "[") {
      depth += 1;
    } else if (char === "]") {
      depth -= 1;
      if (depth === 0) return index;
    }
  }

  return -1;
}

function extractProjectsData(html) {
  const markerMatch = html.match(/\b(?:let|const|var)\s+projectsData\s*=/);
  if (!markerMatch || markerMatch.index === undefined) {
    throw new Error("Could not find projectsData in the old dashboard page.");
  }

  const afterMarker = markerMatch.index + markerMatch[0].length;
  const arrayStart = html.indexOf("[", afterMarker);
  if (arrayStart < 0) {
    throw new Error("Found projectsData, but could not find the array start.");
  }

  const arrayEnd = findMatchingBracket(html, arrayStart);
  if (arrayEnd < 0) {
    throw new Error("Found projectsData, but could not find the array end.");
  }

  const jsonText = html.slice(arrayStart, arrayEnd + 1);
  const projects = JSON.parse(jsonText);
  if (!Array.isArray(projects)) {
    throw new Error("projectsData was not an array.");
  }

  return projects;
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

function asPreviousUpdatesOnly(html) {
  const cleaned = convertRedTextToBold(html || "No description added.");
  return `
    <section class="previousUpdatesBox" data-all-previous-updates="true">
      <div class="projectUpdate">${cleaned}</div>
    </section>
  `.trim();
}

function normalizeProject(project) {
  const description = asPreviousUpdatesOnly(project.description_html || project.description || "No description added.");
  const completedDate = cleanString(project.completed_date);

  return {
    project_number: cleanString(project.project_number),
    project_name: cleanString(project.project_name),
    engineer: cleanString(project.engineer) || "Unassigned",
    project_status: cleanString(project.project_status) || "No Contract",
    sales_person: cleanString(project.sales_person) || "Unassigned",
    start_date: cleanString(project.start_date),
    completed_date: completedDate || null,
    description,
    description_color: cleanString(project.description_color) || "#000000",
    description_html: description,
    created_at: cleanString(project.created_at) || new Date().toISOString()
  };
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
  if (!response.ok) {
    throw new Error(`Supabase login failed: ${payload.error_description || payload.msg || response.statusText}`);
  }

  if (!payload.access_token) {
    throw new Error("Supabase login did not return an access token.");
  }

  return payload.access_token;
}

async function insertProject({ url, anonKey, accessToken, project }) {
  const response = await fetch(`${url}/rest/v1/${PROJECTS_TABLE}`, {
    method: "POST",
    headers: {
      apikey: anonKey,
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      Prefer: "return=minimal"
    },
    body: JSON.stringify(project)
  });

  if (response.ok || response.status === 201) {
    return { status: "inserted" };
  }

  const text = await response.text();
  if (response.status === 409 || text.includes("duplicate key")) {
    return { status: "skipped_duplicate", details: text };
  }

  return { status: "failed", details: text || response.statusText };
}

async function main() {
  const oldUrl = readArg("old-url", process.env.OLD_ESR_URL || DEFAULT_OLD_ESR_URL);
  const email = readArg("email", process.env.SUPABASE_EMAIL || "");
  const password = readArg("password", process.env.SUPABASE_PASSWORD || "");
  const dryRun = hasFlag("dry-run");

  console.log(`Fetching old projects from: ${oldUrl}`);
  const oldResponse = await fetch(oldUrl);
  if (!oldResponse.ok) {
    throw new Error(`Could not fetch old ESR page: ${oldResponse.status} ${oldResponse.statusText}`);
  }

  const html = await oldResponse.text();
  const rawProjects = extractProjectsData(html);
  const projects = rawProjects
    .map(normalizeProject)
    .filter(project => project.project_number && project.project_name);

  console.log(`Found ${rawProjects.length} old records; ${projects.length} have project number and name.`);

  const outputPath = path.join(repoRoot, "old-getesr-projects-export.json");
  await fs.writeFile(outputPath, JSON.stringify(projects, null, 2), "utf8");
  console.log(`Saved normalized backup: ${outputPath}`);

  if (dryRun) {
    console.log("Dry run only. No Supabase changes were made.");
    return;
  }

  if (!email || !password) {
    throw new Error("Set SUPABASE_EMAIL and SUPABASE_PASSWORD, or pass --email=... --password=...");
  }

  const config = await readSupabaseConfig();
  const accessToken = await signIn({ ...config, email, password });

  let inserted = 0;
  let skipped = 0;
  const failures = [];

  for (const project of projects) {
    const result = await insertProject({ ...config, accessToken, project });
    if (result.status === "inserted") {
      inserted += 1;
      console.log(`Inserted ${project.project_number} - ${project.project_name}`);
    } else if (result.status === "skipped_duplicate") {
      skipped += 1;
      console.log(`Skipped duplicate ${project.project_number} - ${project.project_name}`);
    } else {
      failures.push({ project_number: project.project_number, details: result.details });
      console.error(`Failed ${project.project_number}: ${result.details}`);
    }
  }

  console.log("");
  console.log(`Done. Inserted: ${inserted}. Skipped duplicates: ${skipped}. Failed: ${failures.length}.`);

  if (failures.length) {
    const failurePath = path.join(repoRoot, "old-getesr-import-failures.json");
    await fs.writeFile(failurePath, JSON.stringify(failures, null, 2), "utf8");
    console.log(`Failure details saved to: ${failurePath}`);
    process.exitCode = 1;
  }
}

main().catch(error => {
  console.error(error.message || error);
  process.exitCode = 1;
});
