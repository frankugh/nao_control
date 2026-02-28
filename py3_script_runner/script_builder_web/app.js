(function () {
  "use strict";

  const PLACEHOLDER_FILE_LABEL = "Niet opgeslagen";

  const editorJson = document.getElementById("editorJson");
  const previewJson = document.getElementById("previewJson");
  const selCategory = document.getElementById("selCategory");
  const selTemplate = document.getElementById("selTemplate");
  const btnNew = document.getElementById("btnNew");
  const btnLoad = document.getElementById("btnLoad");
  const btnSave = document.getElementById("btnSave");
  const btnSaveAs = document.getElementById("btnSaveAs");
  const btnCopyTemplate = document.getElementById("btnCopyTemplate");
  const btnInsertTemplate = document.getElementById("btnInsertTemplate");
  const fileLabel = document.getElementById("fileLabel");
  const saveState = document.getElementById("saveState");
  const statusMessage = document.getElementById("statusMessage");
  const quickOpenButtons = Array.from(document.querySelectorAll(".btnQuickOpen"));

  let templatesData = null;
  let catalog = [];
  let defaultScript = null;
  let currentCategoryKey = "";
  let currentTemplateKey = "";
  let previewBaseline = "";
  let currentFileHandle = null;
  let currentFileLabel = PLACEHOLDER_FILE_LABEL;
  let editorDirty = false;

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function cloneJson(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function formatJson(value) {
    return JSON.stringify(value, null, 2);
  }

  function setStatus(message, level) {
    statusMessage.textContent = message;
    statusMessage.classList.remove("status-info", "status-ok", "status-warn", "status-error");
    if (level === "ok") {
      statusMessage.classList.add("status-ok");
      return;
    }
    if (level === "warn") {
      statusMessage.classList.add("status-warn");
      return;
    }
    if (level === "error") {
      statusMessage.classList.add("status-error");
      return;
    }
    statusMessage.classList.add("status-info");
  }

  function updateFileMeta() {
    fileLabel.textContent = currentFileLabel;
    saveState.textContent = editorDirty ? "Onopgeslagen wijzigingen" : "Clean";
  }

  function setDirty(value) {
    editorDirty = !!value;
    updateFileMeta();
  }

  function setFileLabel(name) {
    currentFileLabel = name || PLACEHOLDER_FILE_LABEL;
    updateFileMeta();
  }

  function setEditorText(text, options) {
    const dirty = options && options.dirty === true;
    editorJson.value = text;
    setDirty(dirty);
  }

  function getCategoryByKey(categoryKey) {
    return catalog.find((item) => item.category_key === categoryKey) || null;
  }

  function getTemplateByKey(category, templateKey) {
    if (!category || !Array.isArray(category.templates)) {
      return null;
    }
    return category.templates.find((item) => item.template_key === templateKey) || null;
  }

  function confirmDiscardEditorChanges() {
    if (!editorDirty) {
      return true;
    }
    return window.confirm("Je hebt onopgeslagen wijzigingen. Doorgaan en wijzigingen weggooien?");
  }

  function previewIsEdited() {
    return previewJson.value !== previewBaseline;
  }

  function confirmReplacePreviewIfNeeded() {
    if (!previewIsEdited()) {
      return true;
    }
    return window.confirm("Preview is aangepast. Wil je die vervangen door het nieuwe template?");
  }

  function parseJson(text, label) {
    try {
      return { ok: true, value: JSON.parse(text) };
    } catch (err) {
      const message = err && err.message ? err.message : "Onbekende parsefout";
      setStatus(label + " JSON parsefout: " + message, "error");
      return { ok: false, value: null };
    }
  }

  function renderSelectedTemplate(categoryKey, templateKey) {
    const category = getCategoryByKey(categoryKey);
    const template = getTemplateByKey(category, templateKey);
    if (!category || !template) {
      setStatus("Template selectie is ongeldig.", "error");
      return;
    }
    const text = formatJson(template.snippet);
    previewJson.value = text;
    previewBaseline = text;
    currentCategoryKey = categoryKey;
    currentTemplateKey = templateKey;
  }

  function populateCategorySelect() {
    selCategory.innerHTML = "";
    catalog.forEach((category) => {
      const option = document.createElement("option");
      option.value = category.category_key;
      option.textContent = category.category_label;
      selCategory.appendChild(option);
    });
  }

  function populateTemplateSelect(categoryKey) {
    const category = getCategoryByKey(categoryKey);
    selTemplate.innerHTML = "";
    if (!category || !Array.isArray(category.templates) || category.templates.length === 0) {
      return "";
    }
    category.templates.forEach((template) => {
      const option = document.createElement("option");
      option.value = template.template_key;
      option.textContent = template.template_label;
      selTemplate.appendChild(option);
    });
    return category.templates[0].template_key;
  }

  function fileApiSupported() {
    return (
      typeof window.showOpenFilePicker === "function" &&
      typeof window.showSaveFilePicker === "function"
    );
  }

  function suggestedFileName() {
    if (currentFileLabel && currentFileLabel !== PLACEHOLDER_FILE_LABEL) {
      const clean = currentFileLabel.replace(" (voorbeeld)", "").trim();
      if (clean) {
        return clean;
      }
    }
    return "script.json";
  }

  async function writeToFileHandle(fileHandle, text) {
    const writable = await fileHandle.createWritable();
    await writable.write(text);
    await writable.close();
  }

  async function saveAs() {
    if (!fileApiSupported()) {
      setStatus("Deze browser ondersteunt de file picker API niet.", "error");
      return;
    }
    try {
      const fileHandle = await window.showSaveFilePicker({
        suggestedName: suggestedFileName(),
        types: [
          {
            description: "JSON files",
            accept: { "application/json": [".json"] },
          },
        ],
      });
      await writeToFileHandle(fileHandle, editorJson.value);
      currentFileHandle = fileHandle;
      setFileLabel(fileHandle.name || suggestedFileName());
      setDirty(false);
      setStatus("Bestand opgeslagen.", "ok");
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      const message = err && err.message ? err.message : String(err);
      setStatus("Opslaan als mislukt: " + message, "error");
    }
  }

  async function save() {
    if (!currentFileHandle) {
      await saveAs();
      return;
    }
    try {
      await writeToFileHandle(currentFileHandle, editorJson.value);
      setDirty(false);
      setStatus("Bestand opgeslagen.", "ok");
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      const message = err && err.message ? err.message : String(err);
      setStatus("Opslaan mislukt: " + message, "error");
    }
  }

  function applyNewDefaultScript() {
    setEditorText(formatJson(cloneJson(defaultScript)), { dirty: false });
    currentFileHandle = null;
    setFileLabel(PLACEHOLDER_FILE_LABEL);
    setStatus("Nieuw script geladen met default configuratie.", "ok");
  }

  async function loadFromDisk() {
    if (!fileApiSupported()) {
      setStatus("Deze browser ondersteunt de file picker API niet.", "error");
      return;
    }
    try {
      const handles = await window.showOpenFilePicker({
        multiple: false,
        types: [
          {
            description: "JSON files",
            accept: { "application/json": [".json"] },
          },
        ],
      });
      if (!handles || handles.length === 0) {
        return;
      }
      const fileHandle = handles[0];
      const file = await fileHandle.getFile();
      const text = await file.text();
      setEditorText(text, { dirty: false });
      currentFileHandle = fileHandle;
      setFileLabel(file.name);
      setStatus("Bestand geladen: " + file.name, "ok");
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      const message = err && err.message ? err.message : String(err);
      setStatus("Laden mislukt: " + message, "error");
    }
  }

  async function loadExample(exampleName) {
    try {
      const response = await fetch("/examples/" + encodeURIComponent(exampleName), { cache: "no-store" });
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      const text = await response.text();
      setEditorText(text, { dirty: false });
      currentFileHandle = null;
      setFileLabel(exampleName + " (voorbeeld)");
      setStatus("Voorbeeld geladen: " + exampleName, "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Voorbeeld laden mislukt: " + message, "error");
    }
  }

  function insertPreviewIntoEditor() {
    const previewParsed = parseJson(previewJson.value, "Preview");
    if (!previewParsed.ok) {
      return;
    }
    const snippet = previewParsed.value;
    if (!isObject(snippet) && !Array.isArray(snippet)) {
      setStatus("Preview moet een JSON object of array zijn.", "error");
      return;
    }
    if (Array.isArray(snippet)) {
      const invalid = snippet.some((item) => !isObject(item));
      if (invalid) {
        setStatus("Preview array moet alleen step objecten bevatten.", "error");
        return;
      }
    }

    const editorParsed = parseJson(editorJson.value, "Editor");
    if (!editorParsed.ok) {
      return;
    }
    const root = editorParsed.value;
    if (!isObject(root)) {
      setStatus("Editor root moet een JSON object zijn.", "error");
      return;
    }
    if (typeof root.steps === "undefined") {
      root.steps = [];
    }
    if (!Array.isArray(root.steps)) {
      setStatus("Editor field 'steps' moet een array zijn.", "error");
      return;
    }

    let added = 0;
    if (Array.isArray(snippet)) {
      root.steps.push(...cloneJson(snippet));
      added = snippet.length;
    } else {
      root.steps.push(cloneJson(snippet));
      added = 1;
    }
    setEditorText(formatJson(root), { dirty: true });
    setStatus("Template toegevoegd aan steps (" + String(added) + ").", "ok");
  }

  async function copyPreview() {
    const text = previewJson.value || "";
    if (!text.trim()) {
      setStatus("Preview is leeg, niets om te kopieren.", "warn");
      return;
    }
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      setStatus("Clipboard API is niet beschikbaar in deze browser.", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setStatus("Template naar clipboard gekopieerd.", "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Kopieren mislukt: " + message, "error");
    }
  }

  function handleCategoryChange() {
    const requestedCategoryKey = selCategory.value;
    if (!requestedCategoryKey || requestedCategoryKey === currentCategoryKey) {
      return;
    }
    if (!confirmReplacePreviewIfNeeded()) {
      selCategory.value = currentCategoryKey;
      return;
    }
    const templateKey = populateTemplateSelect(requestedCategoryKey);
    selTemplate.value = templateKey;
    renderSelectedTemplate(requestedCategoryKey, templateKey);
    setStatus("Template categorie gewijzigd.", "info");
  }

  function handleTemplateChange() {
    const requestedTemplateKey = selTemplate.value;
    if (!requestedTemplateKey || requestedTemplateKey === currentTemplateKey) {
      return;
    }
    if (!confirmReplacePreviewIfNeeded()) {
      selTemplate.value = currentTemplateKey;
      return;
    }
    renderSelectedTemplate(currentCategoryKey, requestedTemplateKey);
    setStatus("Template gewijzigd.", "info");
  }

  async function init() {
    try {
      const response = await fetch("./templates.json", { cache: "no-store" });
      if (!response.ok) {
        throw new Error("templates.json not available (HTTP " + response.status + ")");
      }
      const json = await response.json();
      if (!isObject(json)) {
        throw new Error("templates.json root must be an object");
      }
      if (!isObject(json.default_script)) {
        throw new Error("templates.json.default_script must be an object");
      }
      if (!Array.isArray(json.catalog) || json.catalog.length === 0) {
        throw new Error("templates.json.catalog must be a non-empty array");
      }

      templatesData = json;
      catalog = templatesData.catalog;
      defaultScript = templatesData.default_script;

      populateCategorySelect();
      const firstCategory = catalog[0].category_key;
      selCategory.value = firstCategory;
      const firstTemplate = populateTemplateSelect(firstCategory);
      selTemplate.value = firstTemplate;
      renderSelectedTemplate(firstCategory, firstTemplate);

      applyNewDefaultScript();
      setStatus("Script Builder klaar.", "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Initialisatie mislukt: " + message, "error");
    }
  }

  editorJson.addEventListener("input", function () {
    setDirty(true);
  });

  selCategory.addEventListener("change", handleCategoryChange);
  selTemplate.addEventListener("change", handleTemplateChange);

  btnNew.addEventListener("click", function () {
    if (!confirmDiscardEditorChanges()) {
      return;
    }
    applyNewDefaultScript();
  });

  btnLoad.addEventListener("click", async function () {
    if (!confirmDiscardEditorChanges()) {
      return;
    }
    await loadFromDisk();
  });

  btnSave.addEventListener("click", async function () {
    await save();
  });

  btnSaveAs.addEventListener("click", async function () {
    await saveAs();
  });

  btnCopyTemplate.addEventListener("click", async function () {
    await copyPreview();
  });

  btnInsertTemplate.addEventListener("click", function () {
    insertPreviewIntoEditor();
  });

  quickOpenButtons.forEach(function (button) {
    button.addEventListener("click", async function () {
      const exampleName = button.getAttribute("data-example");
      if (!exampleName) {
        return;
      }
      if (!confirmDiscardEditorChanges()) {
        return;
      }
      await loadExample(exampleName);
    });
  });

  window.addEventListener("beforeunload", function (event) {
    if (!editorDirty) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  });

  updateFileMeta();
  init();
})();
