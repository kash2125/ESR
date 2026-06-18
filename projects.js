const ENGINEER_OPTIONS = ["Behrad", "Kash", "Shawn", "Xiaohan"];

const SALES_PERSON_OPTIONS = [
  "Victor Sanchez",
  "Michael Howlett",
  "David Newell",
  "Brad Quay",
  "Max Diaz",
  "Francis Tenggardjaja"
];

const PROJECT_STATUS_OPTIONS = ["Contract", "No Contract"];

const DEFAULT_DESCRIPTION_COLOR = "#000000";
const ALLOWED_DESCRIPTION_COLORS = ["#000000", "#c62828"];

function normalizeProjectStatus(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "contract") return "Contract";
  if (raw === "no contract") return "No Contract";
  return "No Contract";
}

function normalizeSalesPerson(value) {
  const raw = String(value || "").trim();
  return SALES_PERSON_OPTIONS.includes(raw) ? raw : "Unassigned";
}

function normalizeEngineer(value) {
  const raw = String(value || "").trim();
  return raw || "Unassigned";
}

function isValidEngineerChoice(value) {
  return ENGINEER_OPTIONS.includes(String(value || "").trim());
}

function isValidSalesPersonChoice(value) {
  return SALES_PERSON_OPTIONS.includes(String(value || "").trim());
}

function isValidProjectStatusChoice(value) {
  return PROJECT_STATUS_OPTIONS.includes(String(value || "").trim());
}

function normalizeDescriptionColor(value) {
  let raw = String(value || "").trim().toLowerCase();

  if (["black", "rgb(0,0,0)", "rgb(0, 0, 0)"].includes(raw)) {
    raw = "#000000";
  }

  if (
    ["red", "rgb(198,40,40)", "rgb(198, 40, 40)", "rgb(255,0,0)", "rgb(255, 0, 0)"].includes(raw)
  ) {
    raw = "#c62828";
  }

  return ALLOWED_DESCRIPTION_COLORS.includes(raw) ? raw : DEFAULT_DESCRIPTION_COLOR;
}

function textContentFromHtml(value) {
  const temp = document.createElement("div");
  temp.innerHTML = String(value || "");
  return temp.textContent.replace(/\s+/g, " ").trim();
}

function sanitizeDescriptionHtml(value) {
  const temp = document.createElement("div");
  temp.innerHTML = String(value || "").trim();

  temp.querySelectorAll("*").forEach(node => {
    const allowedTags = ["SPAN", "STRONG", "B", "EM", "I", "U", "BR", "DIV", "P", "UL", "OL", "LI"];

    if (!allowedTags.includes(node.tagName)) {
      node.replaceWith(...node.childNodes);
      return;
    }

    [...node.attributes].forEach(attr => {
      if (node.tagName === "SPAN" && attr.name === "style") {
        const style = attr.value.toLowerCase();
        if (style.includes("#c62828") || style.includes("red") || style.includes("rgb(198, 40, 40)")) {
          node.setAttribute("style", "color: #c62828;");
        } else if (style.includes("#000000") || style.includes("black") || style.includes("rgb(0, 0, 0)")) {
          node.setAttribute("style", "color: #000000;");
        } else {
          node.removeAttribute("style");
        }
      } else {
        node.removeAttribute(attr.name);
      }
    });
  });

  return temp.innerHTML.trim();
}

function prepareProjectPayload(formValues, existingCreatedAt = null) {
  const projectNumber = String(formValues.project_number || "").trim();
  const projectName = String(formValues.project_name || "").trim();
  const engineer = String(formValues.engineer || "").trim();
  const projectStatus = String(formValues.project_status || "").trim();
  const salesPerson = String(formValues.sales_person || "").trim();
  const startDate = String(formValues.start_date || "").trim();
  const projectedEndDate = String(formValues.projected_end_date || "").trim();
  const description = sanitizeDescriptionHtml(formValues.description || "");
  const descriptionColor = normalizeDescriptionColor(formValues.description_color || DEFAULT_DESCRIPTION_COLOR);

  if (!projectNumber || !projectName || !engineer || !projectStatus || !salesPerson || !startDate || !projectedEndDate) {
    return { project: null, error: "Please fill in all project fields before saving." };
  }

  if (!textContentFromHtml(description)) {
    return { project: null, error: "Please add a description before saving." };
  }

  if (!isValidEngineerChoice(engineer)) {
    return { project: null, error: "Please select a valid Project Engineer from the dropdown." };
  }

  if (!isValidProjectStatusChoice(projectStatus)) {
    return { project: null, error: "Please select a valid Project Status from the dropdown." };
  }

  if (!isValidSalesPersonChoice(salesPerson)) {
    return { project: null, error: "Please select a valid Sales Person from the dropdown." };
  }

  if (new Date(projectedEndDate) < new Date(startDate)) {
    return { project: null, error: "Projected End Date cannot be earlier than Start Date." };
  }

  return {
    project: {
      project_number: projectNumber,
      project_name: projectName,
      engineer: normalizeEngineer(engineer),
      project_status: normalizeProjectStatus(projectStatus),
      sales_person: normalizeSalesPerson(salesPerson),
      start_date: startDate,
      projected_end_date: projectedEndDate,
      description,
      description_color: descriptionColor,
      description_html: description || "No description added.",
      created_at: existingCreatedAt || new Date().toISOString()
    },
    error: null
  };
}