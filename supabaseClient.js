const SUPABASE_URL = "https://mckmcozsmbmbrybxydfq.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_lpV4Epc2dYVmsKd5s6QDKQ_hnnnxYKe";

const supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true
  }
});

const PROJECTS_TABLE = "projects";

async function loadSupabaseProjects() {
  const { data, error } = await supabaseClient
    .from(PROJECTS_TABLE)
    .select("*")
    .order("created_at", { ascending: false });

  if (error) throw error;
  return data || [];
}

async function getProjectByNumber(projectNumber) {
  const { data, error } = await supabaseClient
    .from(PROJECTS_TABLE)
    .select("*")
    .ilike("project_number", projectNumber)
    .maybeSingle();

  if (error) throw error;
  return data;
}

async function insertProject(project) {
  const existing = await getProjectByNumber(project.project_number);

  if (existing) {
    throw new Error("That project number already exists. Use a different project number.");
  }

  const { data, error } = await supabaseClient
    .from(PROJECTS_TABLE)
    .insert(project)
    .select()
    .single();

  if (error) throw error;
  return data;
}

async function updateProject(originalProjectNumber, project) {
  const existing = await getProjectByNumber(originalProjectNumber);

  if (!existing) {
    throw new Error("Project not found. Nothing was updated.");
  }

  const { data, error } = await supabaseClient
    .from(PROJECTS_TABLE)
    .update(project)
    .ilike("project_number", originalProjectNumber)
    .select()
    .single();

  if (error) throw error;
  return data;
}

async function deleteProjectByNumber(projectNumber) {
  const { error } = await supabaseClient
    .from(PROJECTS_TABLE)
    .delete()
    .ilike("project_number", projectNumber);

  if (error) throw error;
  return true;
}