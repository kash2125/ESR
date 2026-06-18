const ESR_TEMPLATE_PATH = "./files/F-ENG-001_template.pdf";

const FIELD_NAMES = {
  job_number: "Text1",
  job_name: "Text2",
  requester: "Text3",
  request_date: "Text4",
  due_date: "Text5",
  voltage: "Text6",
  amperage: "Text7",
  water_service_size_pressure: "Text8",
  max_vehicle_height: "Text9",
  brush_system_model: "Text10",
  end_customer_contact: "Text12",
  end_customer_phone: "Text13",
  project_site_address: "Text14",
  gc_contact_name: "Text15",
  gc_contact_email: "Text16",
  gc_contact_phone: "Text17",
  project_notes: "Text18"
};

const CHECKBOX_TYPE_REQUEST = {
  BID: "Check Box1",
  JOB_ORDER: "Check Box2",
  CHANGE_ORDER: "Check Box3",
  OM: "Check Box4"
};

const CHECKBOX_PROJECT_TYPE = {
  AUTO_VEHICLE: "Check Box5",
  TUNNEL: "Check Box6",
  TRUCK_BUS: "Check Box7",
  TRAIN: "Check Box8",
  LEAK_TEST: "Check Box9",
  OTHER: "Check Box10"
};

const CHECKBOX_DOCUMENT_TYPE = {
  EQUIPMENT_LAYOUT: "Check Box11",
  PLUMBING_LAYOUT: "Check Box12",
  ELECTRICAL_LAYOUT: "Check Box13",
  SLAB_LAYOUT: "Check Box14",
  DATA_SHEET_SUBMITTAL: "Check Box15",
  OTHER: "Check Box16"
};

function getTodayDate() {
  const now = new Date();
  return now.toLocaleDateString("en-US");
}

function getDefaultDueDate() {
  const due = new Date();
  due.setDate(due.getDate() + 15);
  return due.toLocaleDateString("en-US");
}

function safeText(value) {
  return String(value || "").trim();
}

function setTextField(form, fieldName, value) {
  try {
    form.getTextField(fieldName).setText(safeText(value));
  } catch (error) {
    console.warn(`Missing text field: ${fieldName}`);
  }
}

function setCheckbox(form, fieldName, checked) {
  try {
    const box = form.getCheckBox(fieldName);
    if (checked) {
      box.check();
    } else {
      box.uncheck();
    }
  } catch (error) {
    console.warn(`Missing checkbox field: ${fieldName}`);
  }
}

function downloadPdf(bytes, filename) {
  const blob = new Blob([bytes], { type: "application/pdf" });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();

  link.remove();
  URL.revokeObjectURL(url);
}

async function generateEsrPdf(formData) {
  const templateBytes = await fetch(ESR_TEMPLATE_PATH).then(response => {
    if (!response.ok) {
      throw new Error("Could not load F-ENG-001 template PDF.");
    }
    return response.arrayBuffer();
  });

  const pdfDoc = await PDFLib.PDFDocument.load(templateBytes);
  const form = pdfDoc.getForm();

  const requestDate = safeText(formData.request_date) || getTodayDate();
  const dueDate = safeText(formData.due_date) || getDefaultDueDate();

  const gcContactCombined =
    safeText(formData.gc_contact_name) && safeText(formData.gc_contact_email)
      ? `${safeText(formData.gc_contact_name)}\n${safeText(formData.gc_contact_email)}`
      : safeText(formData.gc_contact_name) || safeText(formData.gc_contact_email);

  setTextField(form, FIELD_NAMES.job_number, formData.job_number);
  setTextField(form, FIELD_NAMES.job_name, formData.job_name);
  setTextField(form, FIELD_NAMES.requester, formData.requester);
  setTextField(form, FIELD_NAMES.request_date, requestDate);
  setTextField(form, FIELD_NAMES.due_date, dueDate);
  setTextField(form, FIELD_NAMES.voltage, formData.voltage);
  setTextField(form, FIELD_NAMES.amperage, formData.amperage);
  setTextField(form, FIELD_NAMES.water_service_size_pressure, formData.water_service_size_pressure);
  setTextField(form, FIELD_NAMES.max_vehicle_height, formData.max_vehicle_height);
  setTextField(form, FIELD_NAMES.brush_system_model, formData.brush_system_model);
  setTextField(form, FIELD_NAMES.end_customer_contact, formData.end_customer_contact);
  setTextField(form, FIELD_NAMES.end_customer_phone, formData.end_customer_phone);
  setTextField(form, FIELD_NAMES.project_site_address, formData.project_site_address);
  setTextField(form, FIELD_NAMES.gc_contact_name, "");
  setTextField(form, FIELD_NAMES.gc_contact_email, gcContactCombined);
  setTextField(form, FIELD_NAMES.gc_contact_phone, formData.gc_contact_phone);
  setTextField(form, FIELD_NAMES.project_notes, formData.project_notes);

  const selectedTypeRequest = safeText(formData.type_request || "JOB_ORDER").toUpperCase();

  Object.entries(CHECKBOX_TYPE_REQUEST).forEach(([key, fieldName]) => {
    setCheckbox(form, fieldName, key === selectedTypeRequest);
  });

  const selectedProjectTypes = new Set((formData.project_types || []).map(value => safeText(value).toUpperCase()));

  Object.entries(CHECKBOX_PROJECT_TYPE).forEach(([key, fieldName]) => {
    setCheckbox(form, fieldName, selectedProjectTypes.has(key));
  });

  const selectedDocTypes = new Set((formData.doc_types || []).map(value => safeText(value).toUpperCase()));

  Object.entries(CHECKBOX_DOCUMENT_TYPE).forEach(([key, fieldName]) => {
    setCheckbox(form, fieldName, selectedDocTypes.has(key));
  });

  const pdfBytes = await pdfDoc.save();

  const fileJobNumber = safeText(formData.job_number) || "ESR";
  downloadPdf(pdfBytes, `F-ENG-001_${fileJobNumber}.pdf`);
}