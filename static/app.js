const state = {
  systems: [],
  profiles: [],
  backendStatus: null,
  selectedSystem: null,
  selectedSystemDetail: null,
  selectedFilePath: null,
  selectedFileMeta: null,
  fileCatalog: {},
  jobs: [],
  selectedJobId: null,
  selectedJobResultContext: null,
  jobSelectionRequestId: 0,
  systemDetailRequestId: 0,
  showAllJobs: false,
  jobsPanelCollapsed: false,
  activeView: "workbench",
  activeFlow: "overview",
  systemsQuery: "",
  hideUtilitySystems: true,
  beginnerMode: true,
  bandSpinFilters: {
    up: true,
    down: true,
  },
  dosSpinFilters: {
    total: true,
    up: true,
    down: true,
  },
};

let refreshJobsPromise = null;
let refreshJobsQueued = false;
let refreshSystemDetailPromise = null;
let refreshSystemDetailTarget = null;

const JOB_PREVIEW_COUNT = 10;
const DEFAULT_REQUEST_TIMEOUT_MS = 60000;
const LONG_TASK_TIMEOUT_MS = 360000;

const MATERIAL_CLASS_HELP = {
  bulk: "Bulk means a normal 3D crystal with no large vacuum layer.",
  "2d": "2D means a layered material with vacuum in one direction.",
  slab: "Slab means a surface model with vacuum for surface calculations.",
  molecule: "Molecule means an isolated cluster placed in a large box.",
};

const viewerState = {
  rotX: -0.7,
  rotY: 0.8,
  zoom: 1.0,
  dragging: false,
  lastX: 0,
  lastY: 0,
  initialized: false,
};

const WORKFLOW_VIEWS = {
  overview: {
    label: "Overview",
    step: null,
    submitterTitle: "Unified Submitter",
    submitterNote: "Open a workflow stage to pre-load the corresponding submission step.",
    treeNote: "Use Setup to edit inputs, then switch to a workflow stage to inspect step-specific files.",
    jobsTitle: "Jobs",
    jobsNote: "Workflow-specific job history appears when you enter a calculation stage.",
    logTitle: "Job Log",
    logNote: "Select a workflow stage to focus the runtime log on one calculation type.",
    highlightTitle: "Result Highlights",
    highlightNote: "Surface the most important completed outputs without opening raw files.",
  },
  setup: {
    label: "Setup",
    step: null,
    submitterTitle: "Unified Submitter",
    submitterNote: "Complete setup, then switch to a workflow stage to submit the corresponding task.",
    treeNote: "Setup view shows the full template tree, editable sources, and generated inputs.",
    jobsTitle: "Jobs",
    jobsNote: "Workflow-specific job history appears when you enter a calculation stage.",
    logTitle: "Job Log",
    logNote: "Workflow-specific logs appear when you enter a calculation stage.",
    highlightTitle: "Result Highlights",
    highlightNote: "Overview carries the cross-workflow results snapshot.",
  },
  relax: {
    label: "Relax",
    step: "relax",
    description: "Optimize the crystal structure before any production property calculation.",
    guidance: "Start here for a new material. Confirm POSCAR, MAGMOM, POTCAR mapping, and relaxation INCAR before running. After relax finishes, you can generate a primitive cell from CONTCAR for review, then adopt it into a dedicated child project if needed.",
    prerequisites: [
      { label: "POSCAR present", type: "file", files: [/^POSCAR$/i] },
      { label: "INCAR.relax ready", type: "file", files: [/^INCAR\.relax$/i] },
      { label: "Relax mesh KPOINTS ready", type: "file", files: [/^KPOINTS\.relax$/i, /^KPOINTS\.scf$/i] },
    ],
    files: ["POSCAR", "INCAR.relax", "KPOINTS.relax", "runs/relax/CONTCAR", "runs/relax/PRIMCELL.vasp", "runs/relax/"],
    treeMatchers: [/^POSCAR$/i, /^INCAR\.relax$/i, /^KPOINTS\.(relax|scf)$/i, /^runs\/relax\/(CONTCAR|PRIMCELL\.vasp)$/i, /^runs\/relax\//i],
    highlightMatchers: [/relaxed structure/i, /primitive cell/i],
  },
  scf: {
    label: "SCF",
    step: "scf",
    description: "Generate the ground-state charge density used by downstream electronic-property steps.",
    guidance: "Run after relaxation. This stage reuses the relaxed structure and uses KPOINTS.scf as the static SCF mesh. KPOINTS.relax is reserved for relaxation.",
    prerequisites: [
      { label: "Relax finished", type: "step", stepName: "relax" },
      { label: "INCAR.scf ready", type: "file", files: [/^INCAR\.scf$/i] },
      { label: "KPOINTS.scf ready", type: "file", files: [/^KPOINTS\.scf$/i] },
    ],
    files: ["INCAR.scf", "KPOINTS.scf", "KPOINTS.downstream", "runs/relax/CONTCAR", "runs/relax/PRIMCELL.vasp", "runs/scf/"],
    treeMatchers: [/^INCAR\.scf$/i, /^KPOINTS\.(scf|downstream)$/i, /^runs\/relax\/(CONTCAR|PRIMCELL\.vasp)$/i, /^runs\/scf\//i],
    highlightMatchers: [/electronic character/i, /band gap/i],
  },
  dos: {
    label: "DOS",
    step: "dos",
    description: "Run a static density-of-states calculation after SCF to inspect total and projected electronic states.",
    guidance: "Run after SCF. DOS is usually more useful than crystal band paths for molecules and still useful for bulk systems when you want a quick electronic picture.",
    prerequisites: [
      { label: "SCF finished", type: "step", stepName: "scf" },
      { label: "INCAR.dos ready", type: "file", files: [/^INCAR\.dos$/i] },
      { label: "KPOINTS.dos ready", type: "file", files: [/^KPOINTS\.dos$/i] },
    ],
    files: ["INCAR.dos", "KPOINTS.dos", "runs/scf/CHGCAR", "runs/dos/"],
    treeMatchers: [/^INCAR\.dos$/i, /^KPOINTS\.dos$/i, /^runs\/scf\/(CHGCAR|WAVECAR|OUTCAR|OSZICAR)$/i, /^runs\/dos\//i],
    highlightMatchers: [/density of states/i],
  },
  converge: {
    label: "Converge",
    step: "converge",
    description: "Check whether cutoff and k-mesh choices are reliable before committing to production settings.",
    guidance: "Use this stage when you are moving to a new chemistry or a new class of structure and need a formal convergence baseline.",
    prerequisites: [
      { label: "POSCAR present", type: "file", files: [/^POSCAR$/i] },
      { label: "INCAR.converge ready", type: "file", files: [/^INCAR\.converge$/i] },
      { label: "KPOINTS.scf ready", type: "file", files: [/^KPOINTS\.scf$/i] },
    ],
    files: ["INCAR.converge", "KPOINTS.scf", "runs/relax/PRIMCELL.vasp", "runs/converge/"],
    treeMatchers: [/^INCAR\.converge$/i, /^KPOINTS\.scf$/i, /^runs\/relax\/PRIMCELL\.vasp$/i, /^runs\/converge\//i],
    highlightMatchers: [/electronic character/i],
  },
  band: {
    label: "Band",
    step: "band",
    description: "Trace the electronic band dispersion after a converged SCF reference is available.",
    guidance: "Run after SCF. Standard band uses a line-mode path directly; hybrid/HSE band reuses the SCF mesh as runtime KPOINTS and copies KPOINTS.band to runtime KPOINTS_OPT.",
    prerequisites: [
      { label: "SCF finished", type: "step", stepName: "scf" },
      { label: "INCAR.band ready", type: "file", files: [/^INCAR\.band$/i] },
      { label: "KPATH.in ready", type: "file", files: [/^KPATH\.in$/i] },
      { label: "KPOINTS.band ready", type: "file", files: [/^KPOINTS\.band$/i] },
      { label: "band.conf ready", type: "file", files: [/^band\.conf$/i] },
    ],
    files: ["INCAR.band", "KPATH.in", "KPOINTS.band", "band.conf", "runs/relax/PRIMCELL.vasp", "runs/scf/CHGCAR", "runs/band/"],
    treeMatchers: [/^INCAR\.band$/i, /^KPATH\.in$/i, /^KPOINTS\.band$/i, /^band\.conf$/i, /^runs\/relax\/PRIMCELL\.vasp$/i, /^runs\/scf\/(CHGCAR|WAVECAR|OUTCAR|OSZICAR)$/i, /^runs\/band\//i],
    highlightMatchers: [/electronic character/i, /band gap/i],
  },
  elastic: {
    label: "Elastic",
    step: "elastic",
    description: "Derive a 3D bulk elastic tensor and macroscopic moduli from finite distortions of the relaxed structure.",
    guidance: "Run after relaxation for 3D bulk systems. This built-in path is not the default route for 2D/slab models with vacuum, where a custom in-plane strain workflow is usually required.",
    prerequisites: [
      { label: "Relax finished", type: "step", stepName: "relax" },
      { label: "INCAR.elastic ready", type: "file", files: [/^INCAR\.elastic$/i] },
    ],
    files: ["INCAR.elastic", "runs/relax/CONTCAR", "runs/relax/PRIMCELL.vasp", "runs/elastic/"],
    treeMatchers: [/^INCAR\.elastic$/i, /^runs\/relax\/(CONTCAR|PRIMCELL\.vasp)$/i, /^runs\/elastic\//i],
    highlightMatchers: [/elastic tensor/i, /bulk modulus/i, /shear modulus/i, /young'?s modulus/i, /pugh/i, /poisson/i, /hardness/i, /anisotropy/i, /cauchy/i, /debye/i],
  },
  charge: {
    label: "Charge",
    step: "charge",
    description: "Prepare charge-density outputs and Bader partitioning for bonding and charge-transfer analysis.",
    guidance: "Run after SCF so that charge density, AECCAR, and Bader reference files are internally consistent.",
    prerequisites: [
      { label: "SCF finished", type: "step", stepName: "scf" },
      { label: "INCAR.charge ready", type: "file", files: [/^INCAR\.charge$/i] },
    ],
    files: ["INCAR.charge", "runs/relax/PRIMCELL.vasp", "runs/scf/CHGCAR", "runs/charge/"],
    treeMatchers: [/^INCAR\.charge$/i, /^runs\/relax\/PRIMCELL\.vasp$/i, /^runs\/scf\/(CHGCAR|AECCAR0|AECCAR2|OUTCAR|OSZICAR)$/i, /^runs\/charge\//i],
    highlightMatchers: [/bader charge/i, /charge/i],
  },
  phonon: {
    label: "Phonon",
    step: "phonon",
    description: "Evaluate vibrational stability from displaced supercells and phonopy post-processing.",
    guidance: "Run after relaxation. This stage is memory-sensitive; use strict single-point force settings, and enable NAC only when band.conf and BORN are prepared consistently.",
    prerequisites: [
      { label: "Relax finished", type: "step", stepName: "relax" },
      { label: "INCAR.phonon ready", type: "file", files: [/^INCAR\.phonon$/i] },
      { label: "KPOINTS.phonon ready", type: "file", files: [/^KPOINTS\.phonon$/i] },
    ],
    files: ["INCAR.phonon", "KPOINTS.phonon", "band.conf", "runs/relax/PRIMCELL.vasp", "runs/phonon/"],
    treeMatchers: [/^INCAR\.phonon$/i, /^KPOINTS\.phonon$/i, /^band\.conf$/i, /^BORN$/i, /^runs\/phonon\//i, /^runs\/relax\/(CONTCAR|PRIMCELL\.vasp)$/i],
    highlightMatchers: [/phonon stability/i, /phonon/i],
  },
};

function numericValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function hasNonzeroFloatList(text) {
  return String(text || "")
    .split(/\s+/)
    .filter(Boolean)
    .some((item) => {
      const parsed = Number(item);
      return Number.isFinite(parsed) && Math.abs(parsed) > 1e-9;
    });
}

function formatMetric(value, unit = "", digits = 3) {
  if (value == null || value === "") {
    return "n/a";
  }
  const parsed = numericValue(value);
  const suffix = unit ? ` ${unit}` : "";
  if (parsed == null) {
    return `${value}${suffix}`;
  }
  const precision = Math.abs(parsed) >= 100 ? 1 : digits;
  const text = parsed.toFixed(precision).replace(/\.?0+$/, "");
  return `${text}${suffix}`;
}

function quantileFromSorted(values, quantile) {
  if (!Array.isArray(values) || !values.length) {
    return null;
  }
  const q = Math.min(1, Math.max(0, Number(quantile) || 0));
  const position = (values.length - 1) * q;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) {
    return values[lower];
  }
  const weight = position - lower;
  return values[lower] * (1 - weight) + values[upper] * weight;
}

function dosYAxisWindow(yValues, { symmetric = false } = {}) {
  const finiteValues = (yValues || []).filter((value) => Number.isFinite(value));
  if (!finiteValues.length) {
    return {
      yMin: -1,
      yMax: 1,
      clipped: false,
      rawAbsMax: 1,
      displayAbsMax: 1,
    };
  }

  const rawMin = Math.min(...finiteValues);
  const rawMax = Math.max(...finiteValues);
  const absValues = finiteValues.map((value) => Math.abs(value)).sort((left, right) => left - right);
  const rawAbsMax = absValues[absValues.length - 1] || 1;
  const robustAbs = Math.max(
    quantileFromSorted(absValues, 0.995) || 0,
    quantileFromSorted(absValues, 0.99) || 0,
    0.5,
  );

  const clipped = finiteValues.length >= 200 && rawAbsMax > robustAbs * 4;
  const displayAbsMax = clipped ? Math.max(robustAbs * 1.15, 0.5) : rawAbsMax;

  let yMin = clipped ? -displayAbsMax : rawMin;
  let yMax = clipped ? displayAbsMax : rawMax;
  if (symmetric) {
    const symmetricMax = clipped ? displayAbsMax : Math.max(Math.abs(rawMin), Math.abs(rawMax), 0.5);
    yMin = -symmetricMax;
    yMax = symmetricMax;
  }
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMin === yMax) {
    yMin = -1;
    yMax = 1;
  }
  return {
    yMin,
    yMax,
    clipped,
    rawAbsMax,
    displayAbsMax,
  };
}

async function fetchJSON(url, options = {}) {
  const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeoutHandle = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      ...fetchOptions,
      signal: controller.signal,
    });
  } catch (error) {
    window.clearTimeout(timeoutHandle);
    if (error?.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s.`);
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`Request did not reach the backend. The service may be restarting or this page may be stale. Refresh and retry. (${message})`);
  }
  window.clearTimeout(timeoutHandle);
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail =
      (payload && typeof payload.detail === "string" && payload.detail)
      || text
      || `Request failed: ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function selectedSystemReady() {
  return Boolean(
    state.selectedSystem
    && state.selectedSystemDetail
    && state.selectedSystemDetail.name === state.selectedSystem
  );
}

function selectedSystemActionMessage(action) {
  if (!state.selectedSystem) {
    return `Select a system before ${action}.`;
  }
  return `Wait for ${state.selectedSystem} to finish loading before ${action}.`;
}

function clearSelectedFileSelection(note = "No editable input file selected.", isError = false) {
  state.selectedFilePath = null;
  state.selectedFileMeta = null;
  document.getElementById("file-editor").value = "";
  syncGenerateButton();
  syncIncarFormFromEditor();
  setFileStatus(note, isError);
}

function statusBadge(status) {
  const raw = String(status || "unknown");
  const className = raw.replace(/[^A-Za-z0-9_-]+/g, "-") || "unknown";
  return `<span class="status ${className}">${escapeHtml(raw.replace(/_/g, " "))}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function splitBandSegments(points) {
  const segments = [];
  let current = [];
  let previousX = null;
  for (const pair of points || []) {
    const x = Number(pair?.[0]);
    const y = Number(pair?.[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      continue;
    }
    if (previousX != null && x <= previousX + 1e-9) {
      if (current.length > 1) {
        segments.push(current);
      }
      current = [[x, y]];
    } else {
      current.push([x, y]);
    }
    previousX = x;
  }
  if (current.length > 1) {
    segments.push(current);
  }
  return segments;
}

function flowConfig(flowName = state.activeFlow) {
  return WORKFLOW_VIEWS[flowName] || WORKFLOW_VIEWS.overview;
}

function plainStepLabel(step) {
  return {
    relax: "Relax the structure",
    scf: "Run the ground-state SCF",
    dos: "Calculate the density of states",
    converge: "Check convergence",
    band: "Calculate the band structure",
    elastic: "Calculate elastic constants",
    charge: "Calculate charge and Bader results",
    phonon: "Calculate the phonon spectrum",
  }[step] || step;
}

function localProfileName() {
  const localProfile = (state.profiles || []).find((item) => item.kind === "local");
  return localProfile?.name || document.getElementById("submit-target")?.value || "local";
}

function jobDisplayState(job) {
  return String(job?.display_state || job?.state || "unknown");
}

function jobTerminal(job) {
  return Boolean(job?.is_terminal) || ["finished", "failed", "paused", "stopped"].includes(String(job?.state || "").toLowerCase());
}

function jobRefreshKey(job) {
  return [
    job?.id || "",
    jobDisplayState(job),
    job?.result_ready ? "1" : "0",
    job?.sync_status || "",
    job?.finished_at || "",
  ].join(":");
}

function selectedSystemJobsSignature(jobs = []) {
  if (!state.selectedSystem) {
    return "";
  }
  return jobs
    .filter((job) => job.system === state.selectedSystem)
    .map((job) => jobRefreshKey(job))
    .sort()
    .join("|");
}

function jobsListSignature(jobs = []) {
  return jobs
    .map((job) => [job?.system || "", job?.step || "", jobRefreshKey(job)].join(":"))
    .sort()
    .join("|");
}

function defaultJobLogNote() {
  return flowConfig().logNote || "Live process output and AiiDA reports for the selected job.";
}

function setJobLogNote(note = "") {
  document.getElementById("job-log-note").textContent = note || defaultJobLogNote();
}

function jobTimestamp(job) {
  if (jobTerminal(job)) {
    return job.finished_at || job.created_at || "";
  }
  return job.created_at || "";
}

function fileList(detail) {
  return detail?.tree?.map((item) => item.path) || [];
}

function isHistoricalTreeEntryForFlow(item, flowName = state.activeFlow) {
  if (!item?.historical_step) {
    return false;
  }
  if (flowName === "setup") {
    return true;
  }
  const config = flowConfig(flowName);
  return Boolean(config.step && item.historical_step === config.step);
}

function flowTreeEntries(detail, flowName = state.activeFlow) {
  const items = detail?.tree || [];
  if (flowName === "overview") {
    return [];
  }
  if (flowName === "setup") {
    return items;
  }
  const config = flowConfig(flowName);
  return items.filter((item) => matchesAnyPattern(item.path, config.treeMatchers || []) || isHistoricalTreeEntryForFlow(item, flowName));
}

function matchesAnyPattern(path, patterns = []) {
  return patterns.some((pattern) => pattern.test(path));
}

function hasMatchingFile(detail, patterns = []) {
  return fileList(detail).some((path) => matchesAnyPattern(path, patterns));
}

function isFinishedStatus(status) {
  return ["finished", "completed"].includes(String(status || "").toLowerCase());
}

function formatCheckItem(label, ok, note) {
  return `
    <div class="audit-item audit-${ok ? "pass" : "review"}">
      <div class="panel-head">
        <strong>${label}</strong>
        ${statusBadge(ok ? "pass" : "review")}
      </div>
      <div class="small-label">${note}</div>
    </div>
  `;
}

function flowCheckItems(detail, flowName = state.activeFlow) {
  const config = flowConfig(flowName);
  const steps = detail?.steps || {};
  return (config.prerequisites || []).map((item) => {
    if (item.type === "step") {
      const status = steps[item.stepName];
      const ok = isFinishedStatus(status);
      return {
        label: item.label,
        ok,
        note: ok ? `${item.stepName} is complete.` : `${item.stepName} is currently ${status || "not started"}.`,
      };
    }
    const ok = hasMatchingFile(detail, item.files || []);
    return {
      label: item.label,
      ok,
      note: ok ? "Required input is present in the project tree." : "Expected input is not present yet.",
    };
  });
}

function flowFiles(detail, flowName = state.activeFlow) {
  const config = flowConfig(flowName);
  const scopedEntries = flowTreeEntries(detail, flowName);
  const allPaths = fileList(detail);
  if (!config.treeMatchers?.length) {
    return allPaths.slice(0, 12);
  }
  const matched = scopedEntries.map((item) => item.path);
  return matched.length ? matched.slice(0, 12) : config.files || [];
}

function relevantHighlights(detail, flowName = state.activeFlow) {
  const highlights = detail?.result_highlights || [];
  if (flowName === "overview") {
    return highlights;
  }
  const config = flowConfig(flowName);
  if (!config.highlightMatchers?.length) {
    return [];
  }
  return highlights.filter((item) => {
    const haystack = `${item.title || ""} ${item.note || ""} ${item.value || ""}`;
    return config.highlightMatchers.some((pattern) => pattern.test(haystack));
  });
}

function filteredTreeEntries(detail, flowName = state.activeFlow) {
  return flowTreeEntries(detail, flowName);
}

function setOverview(data) {
  const cpu = data.cpu || {};
  const hostCores = cpu.physical_cores && cpu.logical_cores
    ? `${cpu.physical_cores} physical / ${cpu.logical_cores} logical`
    : cpu.logical_cores
      ? `${cpu.logical_cores} logical`
      : "n/a";
  const usableCores = cpu.available_cores ? `${cpu.available_cores} usable` : "n/a";
  document.getElementById("overview").innerHTML = `
    <div class="metric">
      <div class="metric-label">Workspace</div>
      <div class="metric-value">${data.workspace_root}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Systems</div>
      <div class="metric-value">${data.systems_count}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Memory</div>
      <div class="metric-value">${data.memory.available_gb} / ${data.memory.total_gb} GB free</div>
    </div>
    <div class="metric">
      <div class="metric-label">CPU</div>
      <div class="metric-value">${usableCores}</div>
      <div class="small-label">${hostCores}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Provider</div>
      <div class="metric-value">${data.backend_provider}</div>
    </div>
  `;
}

function formatRabbitMqTimeout(value) {
  const timeout = Number(value);
  if (!Number.isFinite(timeout) || timeout <= 0) {
    return "n/a";
  }
  const hours = timeout / (1000 * 60 * 60);
  if (hours >= 24) {
    const days = hours / 24;
    return `${days.toFixed(days >= 10 ? 0 : 1).replace(/\.0$/, "")} d`;
  }
  return `${hours.toFixed(hours >= 10 ? 0 : 1).replace(/\.0$/, "")} h`;
}

function setElementHidden(id, hidden) {
  const element = document.getElementById(id);
  if (element) {
    element.hidden = hidden;
  }
}

function renderBackendStatus(payload) {
  state.backendStatus = payload;
  const aiida = payload.aiida || {};
  const features = payload.features || {};
  const installAudit = payload.installation_audit || { overall: "review", counts: {}, items: [] };
  const aiidaEnabled = Boolean(features.aiida_enabled);
  const rabbitmq = aiida.rabbitmq || {};
  const rabbitmqLabel = rabbitmq.version
    ? `${rabbitmq.version}${rabbitmq.compatibility === "mitigated" ? " mitigated" : rabbitmq.compatibility === "risk" ? " risky" : ""}`
    : "n/a";
  const aiidaValue = !aiidaEnabled
    ? "disabled"
    : aiida.available ? aiida.aiida_version || "installed" : "not available";
  const profileValue = aiidaEnabled ? (aiida.profile || "n/a") : "disabled";
  const storageValue = aiidaEnabled ? (aiida.storage_backend || "n/a") : "disabled";
  const brokerValue = !aiidaEnabled ? "disabled" : aiida.has_broker ? "configured" : "missing";
  const daemonValue = !aiidaEnabled
    ? "disabled"
    : aiida.daemon_available ? (aiida.daemon_running ? "running" : "available") : "unavailable";
  document.getElementById("backend-chip").textContent = payload.provider;
  document.getElementById("backend-summary").innerHTML = `
    <div class="metric">
      <div class="metric-label">AiiDA</div>
      <div class="metric-value">${aiidaValue}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Profile</div>
      <div class="metric-value">${profileValue}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Storage</div>
      <div class="metric-value">${storageValue}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Broker</div>
      <div class="metric-value">${brokerValue}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Daemon</div>
      <div class="metric-value">${daemonValue}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Local Submit</div>
      <div class="metric-value">${features.local_submission_available ? "ready" : "not configured"}</div>
    </div>
    <div class="metric">
      <div class="metric-label">VASPKIT</div>
      <div class="metric-value">${features.vaspkit_available ? "ready" : "optional"}</div>
    </div>
    <div class="metric">
      <div class="metric-label">POTCAR Archive</div>
      <div class="metric-value">${features.potcar_generation_available ? "ready" : "optional"}</div>
    </div>
    <div class="metric">
      <div class="metric-label">RabbitMQ</div>
      <div class="metric-value">${rabbitmqLabel}</div>
    </div>
    <div class="metric">
      <div class="metric-label">consumer_timeout</div>
      <div class="metric-value">${formatRabbitMqTimeout(rabbitmq.consumer_timeout_ms)}</div>
    </div>
  `;

  setElementHidden("aiida-workchains-panel", !aiidaEnabled);
  setElementHidden("aiida-codes-panel", !aiidaEnabled);
  setElementHidden("aiida-potcars-panel", !aiidaEnabled);

  const workchainRoot = document.getElementById("aiida-workchains");
  workchainRoot.innerHTML = (aiida.workchains || []).map((item) => `
    <div class="job-card">
      <div class="panel-head">
        <strong>${item.entry_point}</strong>
        <span class="chip">${item.class_name}</span>
      </div>
      <div class="small-label">${item.summary || item.module}</div>
    </div>
  `).join("") || `<div class="small-label">No AiiDA workchains detected.</div>`;

  const codeRoot = document.getElementById("aiida-codes");
  codeRoot.innerHTML = (aiida.codes || []).map((item) => `
    <div class="job-card">
      <div class="panel-head">
        <strong>${item.full_label}</strong>
        <span class="chip">${item.default_calc_job_plugin}</span>
      </div>
      <div class="small-label">${item.filepath_executable}</div>
    </div>
  `).join("") || `<div class="small-label">No AiiDA codes detected.</div>`;

  const potcarRoot = document.getElementById("aiida-potcars");
  potcarRoot.innerHTML = (aiida.potcar_families || []).map((item) => `
    <div class="job-card">
      <div class="panel-head">
        <strong>${item.label}</strong>
        <span class="chip">${item.count}</span>
      </div>
      <div class="small-label">${item.type_string}</div>
    </div>
  `).join("") || `<div class="small-label">No AiiDA POTCAR families detected.</div>`;

  if (!aiidaEnabled) {
    document.getElementById("backend-summary").insertAdjacentHTML(
      "beforeend",
      `
      <div class="metric">
        <div class="metric-label">Extension Note</div>
        <div class="metric-value">AiiDA is disabled. Set VASP_STUDIO_ENABLE_AIIDA_BACKEND=1 and install requirements-aiida.txt to enable it.</div>
      </div>
      `
    );
  } else if (aiida.error) {
    document.getElementById("backend-summary").insertAdjacentHTML(
      "beforeend",
      `
      <div class="metric">
        <div class="metric-label">AiiDA Error</div>
        <div class="metric-value">${aiida.error}</div>
      </div>
      `
    );
  }

  if (aiidaEnabled && aiida.warnings?.length) {
    const warningMarkup = aiida.warnings.map((warning) => `
      <div class="metric">
        <div class="metric-label">Warning</div>
        <div class="metric-value">${warning}</div>
      </div>
    `).join("");
    document.getElementById("backend-summary").insertAdjacentHTML("beforeend", warningMarkup);
  }
  if (rabbitmq.note && !aiida.warnings?.includes(rabbitmq.note)) {
    document.getElementById("backend-summary").insertAdjacentHTML(
      "beforeend",
      `
      <div class="metric">
        <div class="metric-label">RabbitMQ Note</div>
        <div class="metric-value">${rabbitmq.note}</div>
      </div>
      `
    );
  }

  document.getElementById("installation-audit-chip").textContent =
    `${installAudit.overall || "review"} | ${installAudit.counts?.pass || 0}/${(installAudit.items || []).length}`;
  document.getElementById("installation-audit-chip").className = `chip audit-${installAudit.overall || "review"}`;
  document.getElementById("installation-audit").innerHTML = (installAudit.items || []).map((item) => `
    <div class="audit-item audit-${item.status || "review"}">
      <div class="panel-head">
        <strong>${item.title}</strong>
        ${statusBadge(item.status || "review")}
      </div>
      <div class="small-label">${item.note || ""}</div>
    </div>
  `).join("") || `<div class="small-label">No installation audit items are available.</div>`;
  document.getElementById("installation-audit-note").textContent = installAudit.troubleshooting_path
    ? `Troubleshooting guide: ${installAudit.troubleshooting_path}`
    : "";
  renderInstallationBanner(payload);
}

function renderInstallationBanner(payload = state.backendStatus) {
  const audit = payload?.installation_audit || { overall: "review", counts: {}, items: [] };
  const items = Array.isArray(audit.items) ? audit.items : [];
  const actionable = items.filter((item) => ["fail", "review"].includes(String(item.status || "review")));
  const displayItems = (actionable.length ? actionable : items).slice(0, 4);
  const failCount = Number(audit.counts?.fail || 0);
  const reviewCount = Number(audit.counts?.review || 0);
  const panelTitle = document.getElementById("installation-banner-title");
  const panelSummary = document.getElementById("installation-banner-summary");
  const panelChip = document.getElementById("installation-banner-chip");
  const panelItems = document.getElementById("installation-banner-items");
  const panelNote = document.getElementById("installation-banner-note");

  if (!panelTitle || !panelSummary || !panelChip || !panelItems || !panelNote) {
    return;
  }

  if (!payload) {
    panelTitle.textContent = "Installation Check";
    panelSummary.textContent = "Backend status has not loaded yet.";
    panelChip.textContent = "checking";
    panelChip.className = "chip audit-review";
    panelItems.innerHTML = `<div class="small-label">Waiting for backend status...</div>`;
    panelNote.textContent = "Open Backend after the status probe completes.";
    return;
  }

  if (audit.overall === "pass") {
    panelTitle.textContent = "Installation Ready";
    panelSummary.textContent = "The main runtime integrations currently look usable from the UI.";
  } else if (failCount > 0) {
    panelTitle.textContent = "Installation Needs Fixes";
    panelSummary.textContent = `${failCount} blocking issue(s) detected${reviewCount > 0 ? `, plus ${reviewCount} follow-up item(s)` : ""}.`;
  } else {
    panelTitle.textContent = "Installation Needs Review";
    panelSummary.textContent = `${reviewCount} configuration item(s) still need manual completion before the full workflow is ready.`;
  }

  panelChip.textContent = `${audit.overall || "review"} | ${Number(audit.counts?.pass || 0)}/${items.length}`;
  panelChip.className = `chip audit-${audit.overall || "review"}`;
  panelItems.innerHTML = displayItems.map((item) => `
    <div class="audit-item audit-${item.status || "review"}">
      <div class="panel-head">
        <strong>${item.title}</strong>
        ${statusBadge(item.status || "review")}
      </div>
      <div class="small-label">${item.note || ""}</div>
    </div>
  `).join("") || `<div class="small-label">No installation findings are available.</div>`;
  panelNote.textContent = audit.troubleshooting_path
    ? `Open Backend for the full environment breakdown. Troubleshooting file: ${audit.troubleshooting_path}`
    : "Open Backend for the full environment breakdown.";
}

function latticeMetricConfig(summary) {
  const primitiveValue = summary.primitive_a_A ?? summary.a_A ?? null;
  const conventionalValue = summary.conventional_cubic_a_A ?? null;
  const hasDistinctConventional =
    Number.isFinite(primitiveValue) &&
    Number.isFinite(conventionalValue) &&
    Math.abs(conventionalValue - primitiveValue) > 1e-3;
  return {
    displayLabel: summary.a_display_label || (hasDistinctConventional ? "Cubic a" : "Lattice a"),
    displayValue: summary.a_display_A ?? conventionalValue ?? primitiveValue,
    primitiveValue,
    showPrimitiveRow: hasDistinctConventional,
    note: summary.a_display_note || (hasDistinctConventional
      ? `Primitive |a1| = ${formatMetric(primitiveValue, "A", 3)}`
      : ""),
  };
}

function renderSystems() {
  const root = document.getElementById("systems-list");
  const systems = visibleSystems();

  root.innerHTML = systems.map((item) => {
    const latticeInfo = latticeMetricConfig(item.summary || {});
    const workflowState = classifySystemState(item);
    const finished = countFinishedSteps(item.steps);
    const total = Object.keys(item.steps || {}).length;
    const chips = [];
    if (latticeInfo.displayValue !== null && latticeInfo.displayValue !== undefined) {
      const latticeLabel = latticeInfo.showPrimitiveRow ? "cubic a" : "a";
      chips.push(`<span class="mini-chip">${latticeLabel} ${formatMetric(latticeInfo.displayValue, "A", 3)}</span>`);
    }
    if (item.summary?.density_g_cm3 !== null && item.summary?.density_g_cm3 !== undefined) {
      chips.push(`<span class="mini-chip">rho ${formatMetric(item.summary.density_g_cm3, "g/cm^3", 3)}</span>`);
    }
    if (!chips.length) {
      chips.push(`<span class="mini-chip">No summary metrics yet</span>`);
    }
    if (item.quarantined) {
      chips.push(`<span class="mini-chip utility-chip">quarantined</span>`);
    }

    return `
      <button class="system-card compact-system-card ${state.selectedSystem === item.name ? "active" : ""}" data-system="${escapeHtml(item.name)}">
        <div class="system-card-head">
          <strong>${escapeHtml(item.name)}</strong>
          ${statusBadge(workflowState)}
        </div>
        <div class="system-card-subhead">
          <span class="small-label">${finished}/${total || 0} workflow steps done</span>
          ${isUtilitySystem(item.name) ? `<span class="mini-chip utility-chip">template/test</span>` : ""}
        </div>
        <div class="system-stats compact">
          ${chips.join("")}
        </div>
      </button>
    `;
  }).join("");

  root.querySelectorAll("[data-system]").forEach((node) => {
    node.addEventListener("click", () => selectSystem(node.dataset.system));
  });

  const summary = document.getElementById("systems-summary");
  const hiddenUtilityCount = state.systems.filter((item) => isUtilitySystem(item.name)).length;
  if (!systems.length) {
    summary.textContent = state.systemsQuery
      ? `No systems match "${state.systemsQuery}".`
      : "No systems available in the workspace.";
    root.innerHTML = `<div class="metric"><div class="metric-label">Systems</div><div class="metric-value">Nothing to show with the current filter.</div></div>`;
    return;
  }
  summary.textContent = state.hideUtilitySystems && hiddenUtilityCount
    ? `Showing ${systems.length} systems. ${hiddenUtilityCount} test/demo projects hidden.`
    : `Showing ${systems.length} systems.`;
}

function isUtilitySystem(name) {
  return /(?:_ui_|smoke|demo|test)/i.test(String(name || ""));
}

function countFinishedSteps(steps = {}) {
  return Object.values(steps).filter((status) => ["finished", "completed"].includes(String(status || "").toLowerCase())).length;
}

function classifySystemState(item) {
  const states = Object.values(item.steps || {}).map((status) => String(status || "").toLowerCase());
  if (states.some((status) => ["running", "running_or_partial", "submitted_remote", "queued", "pausing", "stopping"].includes(status))) {
    return "running_or_partial";
  }
  if (states.some((status) => ["failed", "error", "blocked"].includes(status))) {
    return "review";
  }
  if (states.length && states.every((status) => ["finished", "completed"].includes(status))) {
    return "completed";
  }
  if (states.some((status) => ["finished", "completed"].includes(status))) {
    return "partial";
  }
  return "not_started";
}

function systemSortRank(item) {
  const stateName = classifySystemState(item);
  if (stateName === "running_or_partial") {
    return 0;
  }
  if (stateName === "review") {
    return 1;
  }
  if (stateName === "partial") {
    return 2;
  }
  if (stateName === "completed") {
    return 3;
  }
  return 4;
}

function visibleSystems() {
  const query = state.systemsQuery.trim().toLowerCase();
  return [...state.systems]
    .filter((item) => {
      if (!query) {
        return true;
      }
      return item.name.toLowerCase().includes(query);
    })
    .filter((item) => {
      if (item.name === state.selectedSystem) {
        return true;
      }
      if (!state.hideUtilitySystems) {
        return true;
      }
      return !isUtilitySystem(item.name);
    })
    .sort((left, right) => {
      if (left.name === state.selectedSystem) {
        return -1;
      }
      if (right.name === state.selectedSystem) {
        return 1;
      }
      const utilityDelta = Number(isUtilitySystem(left.name)) - Number(isUtilitySystem(right.name));
      if (utilityDelta) {
        return utilityDelta;
      }
      const rankDelta = systemSortRank(left) - systemSortRank(right);
      if (rankDelta) {
        return rankDelta;
      }
      const finishedDelta = countFinishedSteps(right.steps) - countFinishedSteps(left.steps);
      if (finishedDelta) {
        return finishedDelta;
      }
      return left.name.localeCompare(right.name);
    });
}

function setModeNote() {
  document.getElementById("mode-chip").textContent = state.beginnerMode ? "beginner" : "expert override";
  document.getElementById("mode-note").textContent = state.beginnerMode
    ? "Beginner mode keeps the interface local-first: project, files, quick submit, and logs. Advanced backend and remote panels stay hidden."
    : "Expert Override shows backend and history details, and unlocks generated files for manual editing. Saving material parameters can still overwrite regenerated inputs.";
  document.getElementById("mode-beginner").classList.toggle("active", state.beginnerMode);
  document.getElementById("mode-advanced").classList.toggle("active", !state.beginnerMode);
  document.body.classList.toggle("beginner-mode", state.beginnerMode);
  document.body.classList.toggle("advanced-mode", !state.beginnerMode);
  const workspaceChip = document.getElementById("workspace-mode-chip");
  if (workspaceChip) {
    workspaceChip.textContent = state.beginnerMode ? "Local personal workbench" : "Local workbench + advanced tools";
  }
  const submitButton = document.getElementById("submit-job");
  if (submitButton) {
    submitButton.textContent = state.beginnerMode ? "Run Job" : "Submit Job";
  }
  const submitTarget = document.getElementById("submit-target");
  if (state.beginnerMode && submitTarget && Array.from(submitTarget.options).some((item) => item.value === "local")) {
    submitTarget.value = "local";
  }
}

function setBeginnerMode(enabled) {
  state.beginnerMode = Boolean(enabled);
  try {
    localStorage.setItem("vasp-studio-mode", state.beginnerMode ? "beginner" : "advanced");
  } catch (error) {
    console.warn("Unable to persist UI mode", error);
  }
  setModeNote();
  renderBeginnerGuide(state.selectedSystemDetail);
}

function recommendedBeginnerAction(detail) {
  if (!detail) {
    return {
      type: "project",
      title: "Create your first project",
      note: "Open Project Lab, drop a CIF or POSCAR file, choose the material type, and click Create Project.",
    };
  }

  const steps = detail.steps || {};
  const materialClass = detail.material_settings?.material_class || detail.metadata?.material_class || "bulk";
  if (!isFinishedStatus(steps.relax)) {
    return {
      type: "step",
      step: "relax",
      title: "Optimize Structure",
      note: "This is the normal first step. It relaxes the structure before later property calculations.",
    };
  }
  if (!isFinishedStatus(steps.scf)) {
    return {
      type: "step",
      step: "scf",
      title: "Generate Ground State",
      note: "This builds the charge density used by later workflows. Review the generated INCAR and KPOINTS if you care about production-quality settings.",
    };
  }
  if (materialClass === "molecule") {
    if (!isFinishedStatus(steps.dos)) {
      return {
        type: "step",
        step: "dos",
        title: "Inspect molecular levels / DOS",
        note: "For isolated molecules, DOS is a more meaningful default follow-up than a crystal band path.",
      };
    }
    return {
      type: "focus",
      flow: "charge",
      actionLabel: "Open Charge Workflow",
      title: "Core molecule workflow completed",
      note: "For isolated molecules, relax + scf + dos is the main exploratory path here. Open Charge for one more property page, or switch to Expert Override for custom post-processing.",
    };
  }
  if (!isFinishedStatus(steps.dos)) {
    return {
      type: "step",
      step: "dos",
      title: "Inspect density of states",
      note: "Run DOS after SCF if you want a quick static electronic picture before moving on to band or elastic workflows.",
    };
  }
  if (!isFinishedStatus(steps.band)) {
    return {
      type: "step",
      step: "band",
      title: "Electronic Properties",
      note: "Open the band workflow after SCF if you want crystal band dispersion. This is not the same thing as a separate DOS workflow.",
    };
  }
  if (!isFinishedStatus(steps.elastic)) {
    return {
      type: "focus",
      flow: "elastic",
      actionLabel: "Open Elastic Workflow",
      title: "Core electronic workflow completed",
      note: "Band is done. Open the Elastic page if you want to continue with mechanical properties.",
    };
  }
  if (!isFinishedStatus(steps.charge)) {
    return {
      type: "focus",
      flow: "charge",
      actionLabel: "Open Charge Workflow",
      title: "Core electronic workflow completed",
      note: "Band is done. Open the Charge page if you want charge density and Bader-style post-processing.",
    };
  }
  if (!isFinishedStatus(steps.phonon)) {
    return {
      type: "focus",
      flow: "phonon",
      actionLabel: "Open Phonon Workflow",
      title: "Core workflow already completed",
      note: "Open the Phonon page if you want vibrational stability and phonon-related outputs.",
    };
  }
  return {
    type: "focus",
    flow: "overview",
    actionLabel: "Back to Overview",
    title: "Main beginner workflow completed",
    note: "The main beginner path is done. Use Overview to review results, and switch to Expert Override whenever you need tighter method control.",
  };
}

function renderBeginnerGuide(detail) {
  const title = document.getElementById("beginner-next-title");
  const note = document.getElementById("beginner-next-note");
  const defaults = document.getElementById("beginner-defaults");
  const actionStatus = document.getElementById("beginner-action-status");
  const runButton = document.getElementById("beginner-run-next");
  const setupButton = document.getElementById("beginner-open-setup");
  if (!title || !note || !defaults || !actionStatus || !runButton || !setupButton) {
    return;
  }

  const next = recommendedBeginnerAction(detail);
  title.textContent = next.title;
  note.textContent = next.note;

  if (!detail) {
    defaults.textContent = "No project selected yet";
    runButton.disabled = true;
    setupButton.disabled = true;
    actionStatus.textContent = "Pick a project from the sidebar or create one from Project Lab.";
    return;
  }

  const material = detail.material_settings || {};
  defaults.textContent = [
    `Type: ${material.material_class || "bulk"}`,
    `Spin: ${material.spin_polarized ? "on" : "off"}`,
    `SCF mesh: ${material.kmesh_text || "auto"}`,
  ].join(" | ");
  runButton.disabled = !["step", "focus"].includes(next.type);
  runButton.textContent = next.type === "step"
    ? `Check and ${plainStepLabel(next.step)}`
    : next.type === "focus"
      ? (next.actionLabel || "Open Suggested Page")
      : "Check and Run Next Step";
  setupButton.disabled = false;
  actionStatus.textContent = next.type === "focus"
    ? "The next beginner action is now to open the relevant workflow page. Review the generated inputs again before you submit anything new."
    : "This button always performs a pre-submit check first. It is a convenience path, not a guarantee that the settings are scientifically optimal.";
}

function updateHero(detail) {
  const summary = detail.summary || {};
  const latticeInfo = latticeMetricConfig(summary);
  const atomsCount = detail.structure?.atoms?.length || 0;
  const steps = Object.entries(detail.steps || {});
  const finishedCount = steps.filter(([, status]) => ["finished", "completed"].includes(status)).length;
  const composition = detail.composition_formula || detail.material_settings?.formula || detail.name;
  const quarantineNote = detail.quarantined ? " This system is quarantined until lineage issues are resolved." : "";
  document.getElementById("system-subtitle").textContent =
    `${composition} | ${atomsCount} atoms in the current cell.${quarantineNote}`;
  document.getElementById("hero-lattice-label").textContent = latticeInfo.displayLabel;
  document.getElementById("hero-lattice").textContent = formatMetric(latticeInfo.displayValue, "A", 3);
  document.getElementById("hero-lattice-note").textContent = latticeInfo.note;
  document.getElementById("hero-density").textContent = formatMetric(summary.density_g_cm3, "g/cm^3", 3);
  document.getElementById("hero-moment").textContent = formatMetric(summary.magnetic_moment_muB, "uB", 3);
  document.getElementById("hero-workflow").textContent = `${finishedCount} / ${steps.length} done`;
}

function renderSummary(detail) {
  document.getElementById("system-title").textContent = detail.name;
  document.getElementById("selected-name").textContent = detail.name;
  updateHero(detail);
  const summary = detail.summary || {};
  const latticeInfo = latticeMetricConfig(summary);
  const metrics = [
    [latticeInfo.displayLabel, latticeInfo.displayValue, "A"],
    ...(latticeInfo.showPrimitiveRow ? [["Primitive |a1|", latticeInfo.primitiveValue, "A"]] : []),
    ["Volume", summary.volume_A3, "A^3"],
    ["Density", summary.density_g_cm3, "g/cm^3"],
    ["Moment", summary.magnetic_moment_muB, "uB"],
    ["Delta Hf", summary.formation_enthalpy_eV_per_atom, "eV/atom"],
    ["H wt%", summary.hydrogen_wt_percent, "%"],
  ];
  document.getElementById("summary-grid").innerHTML = metrics.map(([label, value, unit]) => `
    <div class="metric">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${formatMetric(value, unit, 3)}</div>
    </div>
  `).join("");
}

function renderInputReview(detail) {
  const review = detail.input_review || {};
  const entries = review.entries || [];
  const combinedText = review.combined_text || "";
  const textNode = document.getElementById("input-review-text");
  const statusNode = document.getElementById("input-review-status");
  if (!textNode || !statusNode) {
    return;
  }
  textNode.textContent = combinedText || "";
  if (!entries.length) {
    statusNode.textContent = "No generated input bundle is available yet. Save parameters to regenerate and inspect the complete text here.";
    return;
  }
  const fileList = entries.map((item) => item.path).join(", ");
  statusNode.textContent =
    `Showing ${entries.length} generated input file(s): ${fileList}. Review this bundle after every parameter save before you submit a job.`;
}

function renderSteps(detail) {
  document.getElementById("step-status").innerHTML = Object.entries(detail.steps).map(([step, status], index) => `
    <div class="step-card compact">
      <div class="panel-head">
        <strong>${step}</strong>
        ${statusBadge(status)}
      </div>
    </div>
  `).join("");
}

function renderResultHighlights(detail) {
  const root = document.getElementById("result-highlights");
  const highlights = relevantHighlights(detail, state.activeFlow);
  if (!highlights.length) {
    root.innerHTML = `
      <div class="metric result-card result-review">
        <div class="panel-head">
          <div class="metric-label">${flowConfig().label}</div>
          ${statusBadge("review")}
        </div>
        <div class="metric-value">No stage-specific outputs yet</div>
        <div class="small-label">Complete this workflow stage to populate focused result cards here.</div>
      </div>
    `;
    return;
  }
  root.innerHTML = highlights.map((item) => `
    <div class="metric result-card result-${item.status || "review"}">
      <div class="panel-head">
        <div class="metric-label">${item.title}</div>
        ${statusBadge(item.status || "review")}
      </div>
      <div class="metric-value">${item.value || "n/a"}</div>
      <div class="small-label">${item.note || ""}</div>
    </div>
  `).join("");
}

function renderBandVisualization(detail) {
  const root = document.getElementById("band-visualization");
  const chip = document.getElementById("band-visualization-chip");
  const note = document.getElementById("band-visualization-note");
  if (!root || !chip || !note) {
    return;
  }

  const payload = detail.band_visualization;
  if (!payload) {
    chip.textContent = "review";
    chip.className = "chip";
    note.textContent = "Render the current band-path result or hybrid-band artifacts without opening raw files.";
    root.innerHTML = `<div class="small-label">No band result is available yet.</div>`;
    return;
  }

  const artifacts = payload.artifacts || [];
  const isHybrid = String(payload.mode || "").toLowerCase() === "hybrid";
  const modeLabel = isHybrid ? "Hybrid Band" : "Standard Band";
  const headerSummary = isHybrid
    ? "Hybrid/HSE band uses the SCF mesh as KPOINTS and the line path as KPOINTS_OPT."
    : "Standard band uses a single line-mode KPOINTS path for the plotted dispersion.";
  const availableSpins = Array.from(
    new Set((payload.series || []).map((item) => String(item.spin || "").toLowerCase()).filter(Boolean))
  );
  const showSpinControls = availableSpins.includes("up") && availableSpins.includes("down");
  const artifactHtml = artifacts.length
    ? `<div class="band-artifact-strip">${artifacts.map((item) => `<span class="mini-chip">${escapeHtml(item)}</span>`).join("")}</div>`
    : `<div class="small-label">No band artifacts detected.</div>`;
  const inputButtons = isHybrid
    ? [
      { path: "INCAR.band", label: "INCAR.band" },
      { path: "KPOINTS.scf", label: "KPOINTS.scf" },
      { path: "KPOINTS.band", label: "KPOINTS.band" },
      ...(artifacts.includes("KPOINTS_OPT") ? [{ path: "runs/band/KPOINTS_OPT", label: "runs/band/KPOINTS_OPT" }] : []),
      ...(artifacts.includes("KPOINTS") ? [{ path: "runs/band/KPOINTS", label: "runs/band/KPOINTS" }] : []),
    ]
    : [
      { path: "INCAR.band", label: "INCAR.band" },
      { path: "KPOINTS.band", label: "KPOINTS.band" },
      ...(artifacts.includes("KPOINTS") ? [{ path: "runs/band/KPOINTS", label: "runs/band/KPOINTS" }] : []),
    ];
  const renderBandPreviewButtons = (items) => `
    <div class="editor-buttons">
      ${items.map((item) => `
        <button type="button" class="ghost" data-open-preview="${escapeHtml(item.path)}">${escapeHtml(item.label)}</button>
      `).join("")}
    </div>
  `;
  const workflowExplainerHtml = isHybrid
    ? `
      <div class="band-workflow-card">
        <div class="band-workflow-head">
          <div class="metric-label">${escapeHtml(modeLabel)}</div>
          <span class="mini-chip recommended">KPOINTS + KPOINTS_OPT</span>
        </div>
        <div class="small-label"><code>KPOINTS.scf</code> is copied into the runtime <code>KPOINTS</code> mesh for the expensive hybrid sampling. <code>KPOINTS.band</code> is copied into runtime <code>KPOINTS_OPT</code>, which is the line-path file used to reconstruct the plotted band.</div>
        ${renderBandPreviewButtons(inputButtons)}
      </div>
    `
    : `
      <div class="band-workflow-card">
        <div class="band-workflow-head">
          <div class="metric-label">${escapeHtml(modeLabel)}</div>
          <span class="mini-chip">line-mode path</span>
        </div>
        <div class="small-label"><code>KPOINTS.band</code> is the high-symmetry line path used for the plotted dispersion. No separate <code>KPOINTS_OPT</code> file is needed for the standard workflow.</div>
        ${renderBandPreviewButtons(inputButtons)}
      </div>
    `;

  chip.textContent = `${modeLabel} | ${payload.status || "review"}`;
  chip.className = `chip band-chip-${payload.status || "review"}`;
  note.textContent = headerSummary;

  const hasSeries = Array.isArray(payload.series) && payload.series.length;
  if (!hasSeries) {
    root.innerHTML = `
      ${workflowExplainerHtml}
      <div class="band-plot-empty">
        <div class="metric-value">${escapeHtml(payload.title || "Band artifacts ready")}</div>
        <div class="small-label">${escapeHtml(payload.note || "No plot-ready band dataset was reconstructed.")}</div>
        <div class="small-label">Artifacts detected:</div>
        ${artifactHtml}
      </div>
    `;
    root.querySelectorAll("[data-open-preview]").forEach((node) => {
      node.addEventListener("click", async () => {
        await openRuntimePreview(node.dataset.openPreview);
      });
    });
    return;
  }

  const visibleSeries = payload.series.filter((item) => {
    const spin = String(item.spin || "").toLowerCase();
    if (spin === "up") {
      return state.bandSpinFilters.up;
    }
    if (spin === "down") {
      return state.bandSpinFilters.down;
    }
    return true;
  });

  const width = 760;
  const height = 360;
  const margin = { top: 18, right: 18, bottom: 42, left: 52 };
  const xTicks = payload.display_x_ticks || payload.x_ticks || [];
  const verticals = payload.verticals || [];
  const xMax = Math.max(...payload.series.flatMap((item) => (item.points || []).map((pair) => Number(pair[0]) || 0)), 1);
  const [yMin, yMax] = payload.y_range || [-6, 6];
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const sx = (x) => margin.left + ((x || 0) / xMax) * plotWidth;
  const sy = (y) => margin.top + ((yMax - y) / (yMax - yMin || 1)) * plotHeight;

  const yTicks = [];
  const yStep = (yMax - yMin) / 4;
  for (let index = 0; index <= 4; index += 1) {
    yTicks.push(yMin + yStep * index);
  }

  const seriesSvg = visibleSeries.map((item) => {
    const segments = splitBandSegments(item.points || []);
    return segments.map((segment) => {
      const points = segment
        .map((pair) => `${sx(Number(pair[0]) || 0).toFixed(2)},${sy(Number(pair[1]) || 0).toFixed(2)}`)
        .join(" ");
      return `<polyline class="band-series band-series-${escapeHtml(String(item.spin || "generic").toLowerCase())}" points="${points}" stroke="${escapeHtml(item.color || "#b85a2d")}"></polyline>`;
    }).join("");
  }).join("");

  const verticalSvg = verticals.map((x) => `
    <line class="band-grid band-vertical" x1="${sx(Number(x) || 0)}" x2="${sx(Number(x) || 0)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>
  `).join("");

  const yGridSvg = yTicks.map((value) => `
    <line class="band-grid" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(value)}" y2="${sy(value)}"></line>
    <text class="band-axis-label" x="${margin.left - 8}" y="${sy(value) + 4}" text-anchor="end">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const tickSvg = xTicks.map((item) => `
    <line class="band-grid band-vertical" x1="${sx(Number(item.x) || 0)}" x2="${sx(Number(item.x) || 0)}" y1="${height - margin.bottom}" y2="${height - margin.bottom + 6}"></line>
    <text class="band-axis-label" x="${sx(Number(item.x) || 0)}" y="${height - 10}" text-anchor="middle">${escapeHtml(item.label)}</text>
  `).join("");

  const zeroLine = yMin <= 0 && yMax >= 0
    ? `<line class="band-zero-line" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(0)}" y2="${sy(0)}"></line>`
    : "";

  const spinControlsHtml = showSpinControls
    ? `
      <div class="band-spin-toolbar">
        <div class="band-spin-legend">
          <span class="band-legend-item">
            <span class="band-legend-line band-legend-up"></span>
            <span>Spin up</span>
          </span>
          <span class="band-legend-item">
            <span class="band-legend-line band-legend-down"></span>
            <span>Spin down</span>
          </span>
        </div>
        <div class="band-spin-controls">
          <label class="band-spin-toggle">
            <input id="band-spin-up-toggle" type="checkbox" ${state.bandSpinFilters.up ? "checked" : ""}>
            <span>Show up</span>
          </label>
          <label class="band-spin-toggle">
            <input id="band-spin-down-toggle" type="checkbox" ${state.bandSpinFilters.down ? "checked" : ""}>
            <span>Show down</span>
          </label>
        </div>
      </div>
    `
    : "";

  const exportControlsHtml = state.selectedSystem
    ? (payload.is_historical
      ? `<div class="small-label">Band export links stay bound to the live run directory, so they are hidden while viewing a historical job.</div>`
      : `
      <div class="band-export-actions">
        <a class="ghost band-export-link" href="/api/systems/${encodeURIComponent(state.selectedSystem)}/band/export?kind=data">Export Origin CSV</a>
        <a class="ghost band-export-link" href="/api/systems/${encodeURIComponent(state.selectedSystem)}/band/export?kind=ticks">Export Tick Labels</a>
      </div>
    `)
    : "";

  root.innerHTML = `
    ${workflowExplainerHtml}
    <div class="band-plot-meta">
      <div class="small-label">${escapeHtml(payload.title || "Band path")}</div>
      <div class="small-label">Reference: ${escapeHtml(payload.energy_reference || "E - E_F (eV)")}${payload.fermi_eV != null ? ` | E_F = ${formatMetric(payload.fermi_eV, "eV", 4)}` : ""}</div>
    </div>
    <div class="small-label">${escapeHtml(payload.note || "Band outputs are available for quick review.")}</div>
    ${exportControlsHtml}
    ${spinControlsHtml}
    <div class="band-plot-frame">
      <svg class="band-plot-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Band plot">
        <rect class="band-plot-bg" x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${plotHeight}"></rect>
        ${yGridSvg}
        ${verticalSvg}
        ${zeroLine}
        ${seriesSvg}
        ${tickSvg}
      </svg>
    </div>
    ${artifactHtml}
  `;

  if (showSpinControls) {
    const upToggle = document.getElementById("band-spin-up-toggle");
    const downToggle = document.getElementById("band-spin-down-toggle");
    upToggle?.addEventListener("change", (event) => {
      state.bandSpinFilters.up = Boolean(event.target.checked);
      renderBandVisualization(detail);
    });
    downToggle?.addEventListener("change", (event) => {
      state.bandSpinFilters.down = Boolean(event.target.checked);
      renderBandVisualization(detail);
    });
  }
  root.querySelectorAll("[data-open-preview]").forEach((node) => {
    node.addEventListener("click", async () => {
      await openRuntimePreview(node.dataset.openPreview);
    });
  });
}

function renderStackedProjectedDos(root, payload, artifactHtml) {
  const panels = Array.isArray(payload.panels) ? payload.panels : [];
  if (!panels.length) {
    return false;
  }
  const clipIdPrefix = `pdos-${Math.random().toString(36).slice(2, 10)}`;

  let xMin = Number(payload.x_range?.[0]);
  let xMax = Number(payload.x_range?.[1]);
  if (!Number.isFinite(xMin) || !Number.isFinite(xMax) || xMin === xMax) {
    xMin = -6;
    xMax = 6;
  }

  const width = 660;
  const margin = { top: 20, right: 18, bottom: 50, left: 72 };
  const panelHeights = panels.map((panel) => (panel.kind === "total" ? 84 : 90));
  const plotHeight = panelHeights.reduce((sum, value) => sum + value, 0);
  const height = margin.top + plotHeight + margin.bottom;
  const plotWidth = width - margin.left - margin.right;
  const sx = (value) => margin.left + (((value || 0) - xMin) / (xMax - xMin || 1)) * plotWidth;

  const layoutPanels = [];
  let cursorY = margin.top;
  for (let index = 0; index < panels.length; index += 1) {
    const panel = panels[index] || {};
    const panelHeight = panelHeights[index];
    const panelValues = (panel.series || [])
      .flatMap((series) => (series.points || []).map((pair) => Number(pair[1]) || 0))
      .filter((value) => Number.isFinite(value) && value >= 0)
      .sort((left, right) => left - right);
    const rawMax = panelValues.length ? panelValues[panelValues.length - 1] : 1;
    const robustMax = Math.max(
      quantileFromSorted(panelValues, 0.995) || 0,
      quantileFromSorted(panelValues, 0.99) || 0,
      0.5,
    );
    const clipped = panelValues.length >= 200 && rawMax > robustMax * 4;
    const yMin = 0;
    const yMax = Math.max(clipped ? robustMax * 1.12 : rawMax, 0.5);
    layoutPanels.push({
      ...panel,
      clipId: `${clipIdPrefix}-panel-${index}`,
      top: cursorY,
      bottom: cursorY + panelHeight,
      height: panelHeight,
      yMin,
      yMax,
      clipped,
      rawMax,
      sy(value) {
        return this.top + ((this.yMax - value) / (this.yMax - this.yMin || 1)) * this.height;
      },
    });
    cursorY += panelHeight;
  }

  const buildTicks = (min, max, segments, includeZero = false) => {
    const values = [];
    for (let index = 0; index <= segments; index += 1) {
      values.push(min + ((max - min) * index) / segments);
    }
    if (includeZero && min < 0 && max > 0) {
      values.push(0);
    }
    return Array.from(new Set(values.map((value) => Number(value.toFixed(6))))).sort((left, right) => left - right);
  };

  const xTicks = buildTicks(xMin, xMax, 4, true);
  const xGridSvg = xTicks.map((value) => `
    <line class="band-grid" x1="${sx(value)}" x2="${sx(value)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>
    <text class="band-axis-label" x="${sx(value)}" y="${height - 10}" text-anchor="middle">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const fermiLine = xMin <= 0 && xMax >= 0
    ? `<line class="band-zero-line" x1="${sx(0)}" x2="${sx(0)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>`
    : "";

  const panelFrameSvg = layoutPanels.map((panel) => `
    <rect class="band-plot-bg" x="${margin.left}" y="${panel.top}" width="${plotWidth}" height="${panel.height}"></rect>
  `).join("");

  const panelClipDefs = layoutPanels.map((panel) => `
    <clipPath id="${panel.clipId}">
      <rect x="${margin.left}" y="${panel.top}" width="${plotWidth}" height="${panel.height}"></rect>
    </clipPath>
  `).join("");

  const panelSeparatorSvg = layoutPanels.slice(0, -1).map((panel) => `
    <line class="band-divider-line" x1="${margin.left}" x2="${width - margin.right}" y1="${panel.bottom}" y2="${panel.bottom}"></line>
  `).join("");

  const panelTicksSvg = layoutPanels.map((panel) => {
    const tickValues = [panel.yMax / 2, panel.yMax]
      .filter((value, index, array) => value > 1e-6 && array.findIndex((candidate) => Math.abs(candidate - value) < 1e-6) === index);
    return tickValues.map((value) => `
      <line class="band-grid" x1="${margin.left - 4}" x2="${margin.left}" y1="${panel.sy(value)}" y2="${panel.sy(value)}"></line>
      <text class="band-axis-label" x="${margin.left - 8}" y="${panel.sy(value) + 4}" text-anchor="end">${escapeHtml(formatMetric(value, "", value >= 10 ? 0 : 2))}</text>
    `).join("");
  }).join("");

  const panelTitleSvg = layoutPanels.map((panel) => `
    <text class="band-axis-label" x="${margin.left + 10}" y="${panel.top + 16}" font-weight="600">${escapeHtml(panel.title || "")}</text>
  `).join("");

  const panelLegendSvg = layoutPanels.map((panel) => {
    const legendX = width - margin.right - 108;
    const startY = panel.top + 16;
    return (panel.series || []).map((series, index) => {
      const y = startY + index * 15;
      return `
        <line x1="${legendX}" x2="${legendX + 14}" y1="${y}" y2="${y}" stroke="${escapeHtml(series.color || "#2e8b57")}" stroke-width="${panel.kind === "total" ? 2.4 : 2}"></line>
        <text class="band-axis-label" x="${legendX + 19}" y="${y + 4}">${escapeHtml(series.label || "")}</text>
      `;
    }).join("");
  }).join("");

  const seriesSvg = layoutPanels.map((panel) => {
    const content = (panel.series || []).map((series) => {
      const points = (series.points || [])
        .map((pair) => {
          const xValue = Number(pair[0]) || 0;
          const rawY = Number(pair[1]) || 0;
          const yValue = Math.max(panel.yMin, Math.min(rawY, panel.yMax));
          return `${sx(xValue).toFixed(2)},${panel.sy(yValue).toFixed(2)}`;
        })
        .join(" ");
      return `<polyline class="dos-series" points="${points}" stroke="${escapeHtml(series.color || "#2e8b57")}" stroke-width="${panel.kind === "total" ? 2.4 : 2}"></polyline>`;
    }).join("");
    return `<g clip-path="url(#${panel.clipId})">${content}</g>`;
  }).join("");

  const sourceLabel = escapeHtml((payload.source_step || "n/a").toUpperCase());
  const elementsLabel = Array.isArray(payload.projected_elements) && payload.projected_elements.length
    ? ` | Elements: ${escapeHtml(payload.projected_elements.join(", "))}`
    : "";
  const spinNote = payload.spin_collapsed
    ? `<div class="small-label">Spin-up and spin-down PDOS channels are summed into each orbital trace for this compact panel layout.</div>`
    : "";
  const clippedPanels = layoutPanels.filter((panel) => panel.clipped);
  const scaleNote = clippedPanels.length
    ? `<div class="small-label">Some panel scales are clipped to keep narrow DOS spikes from flattening the rest of the orbital features.</div>`
    : "";

  root.innerHTML = `
    <div class="band-plot-meta">
      <div class="small-label">${escapeHtml(payload.title || "Projected density of states")}</div>
      <div class="small-label">
        Source: ${sourceLabel}
        ${payload.fermi_eV != null ? ` | E_F = ${formatMetric(payload.fermi_eV, "eV", 4)}` : ""}
        ${payload.dos_at_fermi != null ? ` | DOS(E_F) = ${formatMetric(Math.abs(payload.dos_at_fermi), "states/eV", 4)}` : ""}
        ${elementsLabel}
      </div>
      ${spinNote}
      ${scaleNote}
    </div>
    <div class="band-plot-frame">
      <svg class="band-plot-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Projected density of states plot">
        <defs>
          ${panelClipDefs}
        </defs>
        ${panelFrameSvg}
        ${xGridSvg}
        ${fermiLine}
        ${panelSeparatorSvg}
        ${panelTicksSvg}
        ${seriesSvg}
        ${panelTitleSvg}
        ${panelLegendSvg}
        <text class="band-axis-label" x="${margin.left + plotWidth / 2}" y="${height - 6}" text-anchor="middle">${escapeHtml("Energy (eV)")}</text>
        <text class="band-axis-label" x="18" y="${margin.top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 18 ${margin.top + plotHeight / 2})">${escapeHtml("PDOS (States/eV)")}</text>
      </svg>
    </div>
    ${artifactHtml}
  `;

  return true;
}

function renderDosVisualization(detail) {
  const root = document.getElementById("dos-visualization");
  const chip = document.getElementById("dos-visualization-chip");
  const note = document.getElementById("dos-visualization-note");
  if (!root || !chip || !note) {
    return;
  }

  const payload = detail?.dos_visualization;
  if (!payload) {
    chip.textContent = "review";
    chip.className = "chip";
    note.textContent = "Render the current total DOS directly from DOSCAR without opening raw files.";
    root.innerHTML = `<div class="small-label">No DOS result is available yet.</div>`;
    return;
  }

  const artifacts = payload.artifacts || [];
  const artifactHtml = artifacts.length
    ? `<div class="band-artifact-strip">${artifacts.map((item) => `<span class="mini-chip">${escapeHtml(item)}</span>`).join("")}</div>`
    : `<div class="small-label">No DOS artifacts detected.</div>`;

  chip.textContent = `${payload.mode || "dos"} | ${payload.status || "review"}`;
  chip.className = `chip dos-chip-${payload.status || "review"}`;
  note.textContent = payload.note || "DOS outputs are available for quick review.";

  const hasSeries = Array.isArray(payload.series) && payload.series.length;
  const renderedProjected = Array.isArray(payload.panels) && payload.panels.length
    ? renderStackedProjectedDos(root, payload, artifactHtml)
    : false;
  if (renderedProjected) {
    return;
  }
  if (!hasSeries) {
    root.innerHTML = `
      <div class="band-plot-empty">
        <div class="metric-value">${escapeHtml(payload.title || "DOS artifacts ready")}</div>
        <div class="small-label">${escapeHtml(payload.note || "No plot-ready DOS dataset was reconstructed.")}</div>
        <div class="small-label">Artifacts detected:</div>
        ${artifactHtml}
      </div>
    `;
    return;
  }

  const visibleSeries = payload.series.filter((item) => {
    const spin = String(item.spin || "total").toLowerCase();
    return state.dosSpinFilters[spin] !== false;
  });
  if (!visibleSeries.length) {
    root.innerHTML = `
      <div class="band-plot-empty">
        <div class="metric-value">All DOS traces are hidden</div>
        <div class="small-label">Re-enable at least one DOS trace to render the plot again.</div>
      </div>
    `;
    return;
  }

  const allPoints = visibleSeries.flatMap((item) => item.points || []);
  const xValues = allPoints.map((point) => Number(point[0]) || 0);
  const yValues = allPoints.map((point) => Number(point[1]) || 0);
  let xMin = payload.x_range?.[0] ?? Math.min(...xValues);
  let xMax = payload.x_range?.[1] ?? Math.max(...xValues);
  const visibleSpins = new Set(visibleSeries.map((item) => String(item.spin || "total").toLowerCase()));
  const symmetricDosScale = payload.mirrored_spin && visibleSpins.has("up") && visibleSpins.has("down") && !visibleSpins.has("total");
  const yAxis = dosYAxisWindow(yValues, { symmetric: symmetricDosScale });
  let yMin = yAxis.yMin;
  let yMax = yAxis.yMax;
  if (!Number.isFinite(xMin) || !Number.isFinite(xMax) || xMin === xMax) {
    xMin = -6;
    xMax = 6;
  }
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMin === yMax) {
    yMin = -1;
    yMax = 1;
  }
  yMin = Math.min(yMin, 0);
  yMax = Math.max(yMax, 0);
  const yPadding = Math.max((yMax - yMin) * 0.08, 0.2);
  yMin -= yPadding;
  yMax += yPadding;
  const scaleNote = yAxis.clipped
    ? `Plot scale clipped to ${symmetricDosScale ? "±" : ""}${formatMetric(yAxis.displayAbsMax, "states/eV", 3)} for readability; raw peak = ${formatMetric(yAxis.rawAbsMax, "states/eV", 3)}.`
    : "";

  const width = 760;
  const height = 360;
  const margin = { top: 18, right: 18, bottom: 42, left: 56 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const sx = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * plotWidth;
  const sy = (value) => height - margin.bottom - ((value - yMin) / (yMax - yMin)) * plotHeight;

  const makeTicks = (min, max, count) => Array.from({ length: count }, (_, index) => min + ((max - min) * index) / (count - 1));
  const xTicks = makeTicks(xMin, xMax, 6);
  const yTicks = makeTicks(yMin, yMax, 5);

  const seriesSvg = visibleSeries.map((item) => {
    const points = (item.points || [])
      .map((point) => `${sx(Number(point[0]) || 0).toFixed(2)},${sy(Number(point[1]) || 0).toFixed(2)}`)
      .join(" ");
    return `<polyline class="dos-series dos-series-${escapeHtml(String(item.spin || "total").toLowerCase())}" points="${points}" stroke="${escapeHtml(item.color || "#2e8b57")}"></polyline>`;
  }).join("");

  const xGridSvg = xTicks.map((value) => `
    <line class="band-grid" x1="${sx(value)}" x2="${sx(value)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>
    <text class="band-axis-label" x="${sx(value)}" y="${height - 10}" text-anchor="middle">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const yGridSvg = yTicks.map((value) => `
    <line class="band-grid" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(value)}" y2="${sy(value)}"></line>
    <text class="band-axis-label" x="${margin.left - 8}" y="${sy(value) + 4}" text-anchor="end">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const zeroXLine = xMin <= 0 && xMax >= 0
    ? `<line class="band-zero-line" x1="${sx(0)}" x2="${sx(0)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>`
    : "";
  const zeroYLine = yMin <= 0 && yMax >= 0
    ? `<line class="band-zero-line" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(0)}" y2="${sy(0)}"></line>`
    : "";

  const availableSpins = Array.from(new Set((payload.series || []).map((item) => String(item.spin || "total").toLowerCase())));
  const showControls = availableSpins.length > 1;
  const legendLabel = payload.mirrored_spin ? "Spin down (mirrored)" : "Spin down";
  const controlsHtml = showControls
    ? `
      <div class="band-spin-toolbar">
        <div class="band-spin-legend">
          ${availableSpins.includes("total") ? `
            <span class="band-legend-item">
              <span class="band-legend-line dos-legend-total"></span>
              <span>Total DOS</span>
            </span>
          ` : ""}
          ${availableSpins.includes("up") ? `
            <span class="band-legend-item">
              <span class="band-legend-line band-legend-up"></span>
              <span>Spin up</span>
            </span>
          ` : ""}
          ${availableSpins.includes("down") ? `
            <span class="band-legend-item">
              <span class="band-legend-line band-legend-down"></span>
              <span>${legendLabel}</span>
            </span>
          ` : ""}
        </div>
        <div class="band-spin-controls">
          ${availableSpins.includes("total") ? `
            <label class="band-spin-toggle">
              <input id="dos-spin-total-toggle" type="checkbox" ${state.dosSpinFilters.total ? "checked" : ""}>
              <span>Show total</span>
            </label>
          ` : ""}
          ${availableSpins.includes("up") ? `
            <label class="band-spin-toggle">
              <input id="dos-spin-up-toggle" type="checkbox" ${state.dosSpinFilters.up ? "checked" : ""}>
              <span>Show up</span>
            </label>
          ` : ""}
          ${availableSpins.includes("down") ? `
            <label class="band-spin-toggle">
              <input id="dos-spin-down-toggle" type="checkbox" ${state.dosSpinFilters.down ? "checked" : ""}>
              <span>Show down</span>
            </label>
          ` : ""}
        </div>
      </div>
    `
    : "";

  root.innerHTML = `
    <div class="band-plot-meta">
      <div class="small-label">${escapeHtml(payload.title || "Total density of states")}</div>
      <div class="small-label">
        Source: ${escapeHtml((payload.source_step || "n/a").toUpperCase())}
        ${payload.fermi_eV != null ? ` | E_F = ${formatMetric(payload.fermi_eV, "eV", 4)}` : ""}
        ${payload.dos_at_fermi != null ? ` | DOS(E_F) = ${formatMetric(Math.abs(payload.dos_at_fermi), "states/eV", 4)}` : ""}
      </div>
      ${scaleNote ? `<div class="small-label">${escapeHtml(scaleNote)}</div>` : ""}
    </div>
    ${controlsHtml}
    <div class="band-plot-frame">
      <svg class="band-plot-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Density of states plot">
        <rect class="band-plot-bg" x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${plotHeight}"></rect>
        ${xGridSvg}
        ${yGridSvg}
        ${zeroXLine}
        ${zeroYLine}
        ${seriesSvg}
        <text class="band-axis-label" x="${margin.left + plotWidth / 2}" y="${height - 6}" text-anchor="middle">${escapeHtml(payload.energy_reference || "E - E_F (eV)")}</text>
        <text class="band-axis-label" x="16" y="${margin.top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 16 ${margin.top + plotHeight / 2})">${escapeHtml(payload.density_reference || "states/eV")}</text>
      </svg>
    </div>
    ${artifactHtml}
  `;

  document.getElementById("dos-spin-total-toggle")?.addEventListener("change", (event) => {
    state.dosSpinFilters.total = Boolean(event.target.checked);
    renderDosVisualization(detail);
  });
  document.getElementById("dos-spin-up-toggle")?.addEventListener("change", (event) => {
    state.dosSpinFilters.up = Boolean(event.target.checked);
    renderDosVisualization(detail);
  });
  document.getElementById("dos-spin-down-toggle")?.addEventListener("change", (event) => {
    state.dosSpinFilters.down = Boolean(event.target.checked);
    renderDosVisualization(detail);
  });
}

function renderPhononVisualization(detail) {
  const root = document.getElementById("phonon-visualization");
  const chip = document.getElementById("phonon-visualization-chip");
  const note = document.getElementById("phonon-visualization-note");
  if (!root || !chip || !note) {
    return;
  }

  const payload = detail?.phonon_visualization;
  const dosPayload = detail?.phonon_dos_visualization;
  if (!payload) {
    chip.textContent = "review";
    chip.className = "chip";
    note.textContent = "Render the current phonon dispersion with element-resolved phonon DOS on a shared frequency axis.";
    root.innerHTML = `<div class="small-label">No phonon result is available yet.</div>`;
    return;
  }

  const artifacts = Array.from(new Set([...(payload.artifacts || []), ...(dosPayload?.artifacts || [])]));
  const artifactHtml = artifacts.length
    ? `<div class="band-artifact-strip">${artifacts.map((item) => `<span class="mini-chip">${escapeHtml(item)}</span>`).join("")}</div>`
    : `<div class="small-label">No phonon artifacts detected.</div>`;

  const dosStatus = dosPayload?.status || "review";
  let displayStatus = payload.status || "review";
  if (dosPayload && displayStatus === "plot_ready" && dosStatus !== "plot_ready") {
    displayStatus = dosStatus;
  }
  chip.textContent = `phonon | ${displayStatus}`;
  chip.className = `chip dos-chip-${displayStatus}`;
  const noteParts = [];
  if (payload.note) {
    noteParts.push(payload.note);
  }
  if (dosPayload?.note) {
    noteParts.push(dosPayload.note);
  }
  note.textContent = noteParts.join(" ") || "Phonon outputs are available for quick review.";

  const hasSeries = Array.isArray(payload.series) && payload.series.length;
  const dosSeries = Array.isArray(dosPayload?.projected_series) && dosPayload.projected_series.length
    ? dosPayload.projected_series
    : (Array.isArray(dosPayload?.series) ? dosPayload.series : []);
  if (!hasSeries) {
    root.innerHTML = `
      <div class="band-plot-empty">
        <div class="metric-value">${escapeHtml(payload.title || "Phonon artifacts ready")}</div>
        <div class="small-label">${escapeHtml(payload.note || "No plot-ready phonon dataset was reconstructed.")}</div>
        <div class="small-label">Artifacts detected:</div>
        ${artifactHtml}
      </div>
    `;
    return;
  }

  if (!dosSeries.length) {
    root.innerHTML = `
      <div class="band-plot-meta">
        <div class="small-label">${escapeHtml(payload.title || "Phonon dispersion")}</div>
        <div class="small-label">${escapeHtml(payload.frequency_reference || "Frequency (THz)")}</div>
      </div>
      <div class="band-plot-frame">
        <div class="small-label">Projected phonon DOS is not available yet, so only the dispersion is shown.</div>
      </div>
      ${artifactHtml}
    `;
    return;
  }

  const width = 860;
  const height = 380;
  const margin = { top: 18, right: 18, bottom: 42, left: 56 };
  const xTicks = payload.display_x_ticks || payload.x_ticks || [];
  const verticals = payload.verticals || [];
  const xMax = Math.max(...payload.series.flatMap((item) => (item.points || []).map((pair) => Number(pair[0]) || 0)), 1);
  const [yMin, yMax] = payload.y_range || [-1, 10];
  const densityRange = dosPayload?.density_range || [0, 1];
  const densityMin = Number(densityRange[0]) || 0;
  const densityMax = Math.max(Number(densityRange[1]) || 1, densityMin + 1e-6);
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const dosWidth = Math.max(150, plotWidth * 0.24);
  const dividerGap = 18;
  const bandWidth = Math.max(220, plotWidth - dosWidth - dividerGap);
  const dosStart = margin.left + bandWidth + dividerGap;
  const dividerX = margin.left + bandWidth + dividerGap / 2;
  const sx = (x) => margin.left + ((x || 0) / xMax) * bandWidth;
  const sdos = (value) => dosStart + (((value || 0) - densityMin) / (densityMax - densityMin || 1)) * dosWidth;
  const sy = (y) => margin.top + ((yMax - y) / (yMax - yMin || 1)) * plotHeight;

  const yTicks = [];
  const yStep = (yMax - yMin) / 4;
  for (let index = 0; index <= 4; index += 1) {
    yTicks.push(yMin + yStep * index);
  }

  const seriesSvg = payload.series.map((item) => {
    const segments = splitBandSegments(item.points || []);
    return segments.map((segment) => {
      const points = segment
        .map((pair) => `${sx(Number(pair[0]) || 0).toFixed(2)},${sy(Number(pair[1]) || 0).toFixed(2)}`)
        .join(" ");
      return `<polyline class="band-series" points="${points}" stroke="${escapeHtml(item.color || "#2e8b57")}"></polyline>`;
    }).join("");
  }).join("");

  const dosSvg = dosSeries.map((item) => {
    const points = (item.points || [])
      .map((pair) => `${sdos(Number(pair[1]) || 0).toFixed(2)},${sy(Number(pair[0]) || 0).toFixed(2)}`)
      .join(" ");
    return `<polyline class="dos-series" points="${points}" stroke="${escapeHtml(item.color || "#2e8b57")}"></polyline>`;
  }).join("");

  const verticalSvg = verticals.map((x) => `
    <line class="band-grid band-vertical" x1="${sx(Number(x) || 0)}" x2="${sx(Number(x) || 0)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>
  `).join("");

  const yGridSvg = yTicks.map((value) => `
    <line class="band-grid" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(value)}" y2="${sy(value)}"></line>
    <text class="band-axis-label" x="${margin.left - 8}" y="${sy(value) + 4}" text-anchor="end">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const tickSvg = xTicks.map((item) => `
    <line class="band-grid band-vertical" x1="${sx(Number(item.x) || 0)}" x2="${sx(Number(item.x) || 0)}" y1="${height - margin.bottom}" y2="${height - margin.bottom + 6}"></line>
    <text class="band-axis-label" x="${sx(Number(item.x) || 0)}" y="${height - 10}" text-anchor="middle">${escapeHtml(item.label)}</text>
  `).join("");

  const dosTicks = Array.from({ length: 4 }, (_, index) => densityMin + ((densityMax - densityMin) * index) / 3);
  const dosTickSvg = dosTicks.map((value) => `
    <line class="band-grid" x1="${sdos(value)}" x2="${sdos(value)}" y1="${height - margin.bottom}" y2="${height - margin.bottom + 6}"></line>
  `).join("");

  const legendX = dosStart + Math.max(14, dosWidth - 86);
  const legendY = margin.top + 14;
  const legendSvg = dosSeries.map((item, index) => {
    const y = legendY + index * 18;
    return `
      <line x1="${legendX}" x2="${legendX + 16}" y1="${y}" y2="${y}" stroke="${escapeHtml(item.color || "#2e8b57")}" stroke-width="2"></line>
      <text class="band-axis-label" x="${legendX + 22}" y="${y + 4}">${escapeHtml(item.label || "PDOS")}</text>
    `;
  }).join("");

  const zeroLine = yMin <= 0 && yMax >= 0
    ? `<line class="band-zero-line" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(0)}" y2="${sy(0)}"></line>`
    : "";

  const meshText = Array.isArray(dosPayload?.mesh) && dosPayload.mesh.length === 3
    ? dosPayload.mesh.join(" x ")
    : null;

  root.innerHTML = `
    <div class="band-plot-meta">
      <div class="small-label">${escapeHtml("Phonon dispersion + projected DOS")}</div>
      <div class="small-label">
        ${escapeHtml(payload.frequency_reference || "Frequency (THz)")}
        ${meshText ? ` | Mesh: ${escapeHtml(meshText)}` : ""}
        ${dosPayload?.max_density != null ? ` | Max PDOS = ${formatMetric(dosPayload.max_density, "states/THz", 4)}` : ""}
      </div>
    </div>
    <div class="band-plot-frame">
      <svg class="band-plot-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Phonon dispersion with projected density of states plot">
        <rect class="band-plot-bg" x="${margin.left}" y="${margin.top}" width="${bandWidth}" height="${plotHeight}"></rect>
        <rect class="band-plot-bg" x="${dosStart}" y="${margin.top}" width="${dosWidth}" height="${plotHeight}"></rect>
        ${yGridSvg}
        ${verticalSvg}
        ${zeroLine}
        ${seriesSvg}
        ${dosSvg}
        ${dosTickSvg}
        <line class="band-divider-line" x1="${dividerX}" x2="${dividerX}" y1="${margin.top}" y2="${height - margin.bottom}"></line>
        ${tickSvg}
        ${legendSvg}
        <text class="band-axis-label" x="${margin.left + bandWidth / 2}" y="${height - 6}" text-anchor="middle">k-path</text>
        <text class="band-axis-label" x="${dosStart + dosWidth / 2}" y="${height - 6}" text-anchor="middle">Phonon DOS</text>
        <text class="band-axis-label" x="16" y="${margin.top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 16 ${margin.top + plotHeight / 2})">${escapeHtml(payload.frequency_reference || "Frequency (THz)")}</text>
      </svg>
    </div>
    ${artifactHtml}
  `;
}

function renderPhononDosVisualization(detail) {
  const root = document.getElementById("phonon-dos-visualization");
  const chip = document.getElementById("phonon-dos-visualization-chip");
  const note = document.getElementById("phonon-dos-visualization-note");
  if (!root || !chip || !note) {
    return;
  }

  const payload = detail?.phonon_dos_visualization;
  if (!payload) {
    chip.textContent = "review";
    chip.className = "chip";
    note.textContent = "Render the current phonon total DOS directly from total_dos.dat without opening raw files.";
    root.innerHTML = `<div class="small-label">No phonon DOS result is available yet.</div>`;
    return;
  }

  const artifacts = payload.artifacts || [];
  const artifactHtml = artifacts.length
    ? `<div class="band-artifact-strip">${artifacts.map((item) => `<span class="mini-chip">${escapeHtml(item)}</span>`).join("")}</div>`
    : `<div class="small-label">No phonon DOS artifacts detected.</div>`;

  chip.textContent = `${payload.mode || "phonon_dos"} | ${payload.status || "review"}`;
  chip.className = `chip dos-chip-${payload.status || "review"}`;
  note.textContent = payload.note || "Phonon DOS outputs are available for quick review.";

  const hasSeries = Array.isArray(payload.series) && payload.series.length;
  if (!hasSeries) {
    root.innerHTML = `
      <div class="band-plot-empty">
        <div class="metric-value">${escapeHtml(payload.title || "Phonon DOS artifacts ready")}</div>
        <div class="small-label">${escapeHtml(payload.note || "No plot-ready phonon DOS dataset was reconstructed.")}</div>
        <div class="small-label">Artifacts detected:</div>
        ${artifactHtml}
      </div>
    `;
    return;
  }

  const width = 760;
  const height = 360;
  const margin = { top: 18, right: 18, bottom: 42, left: 56 };
  const [xMin, xMax] = payload.x_range || [-1, 10];
  const [yMin, yMax] = payload.y_range || [0, 1];
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const sx = (x) => margin.left + (((x || 0) - xMin) / (xMax - xMin || 1)) * plotWidth;
  const sy = (y) => margin.top + ((yMax - y) / (yMax - yMin || 1)) * plotHeight;
  const makeTicks = (min, max, count) => Array.from({ length: count }, (_, index) => min + ((max - min) * index) / (count - 1));
  const xTicks = makeTicks(xMin, xMax, 6);
  const yTicks = makeTicks(yMin, yMax, 5);

  const seriesSvg = (payload.series || []).map((item) => {
    const points = (item.points || [])
      .map((pair) => `${sx(Number(pair[0]) || 0).toFixed(2)},${sy(Number(pair[1]) || 0).toFixed(2)}`)
      .join(" ");
    return `<polyline class="dos-series dos-series-total" points="${points}" stroke="${escapeHtml(item.color || "#2e8b57")}"></polyline>`;
  }).join("");

  const xGridSvg = xTicks.map((value) => `
    <line class="band-grid" x1="${sx(value)}" x2="${sx(value)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>
    <text class="band-axis-label" x="${sx(value)}" y="${height - 10}" text-anchor="middle">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const yGridSvg = yTicks.map((value) => `
    <line class="band-grid" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(value)}" y2="${sy(value)}"></line>
    <text class="band-axis-label" x="${margin.left - 8}" y="${sy(value) + 4}" text-anchor="end">${escapeHtml(formatMetric(value, "", 2))}</text>
  `).join("");

  const zeroXLine = xMin <= 0 && xMax >= 0
    ? `<line class="band-zero-line" x1="${sx(0)}" x2="${sx(0)}" y1="${margin.top}" y2="${height - margin.bottom}"></line>`
    : "";
  const zeroYLine = yMin <= 0 && yMax >= 0
    ? `<line class="band-zero-line" x1="${margin.left}" x2="${width - margin.right}" y1="${sy(0)}" y2="${sy(0)}"></line>`
    : "";

  const meshText = Array.isArray(payload.mesh) && payload.mesh.length === 3
    ? payload.mesh.join(" x ")
    : null;

  root.innerHTML = `
    <div class="band-plot-meta">
      <div class="small-label">${escapeHtml(payload.title || "Phonon density of states")}</div>
      <div class="small-label">
        ${escapeHtml(payload.frequency_reference || "Frequency (THz)")}
        ${meshText ? ` | Mesh: ${escapeHtml(meshText)}` : ""}
        ${payload.max_density != null ? ` | Max DOS = ${formatMetric(payload.max_density, "states/THz", 4)}` : ""}
      </div>
    </div>
    <div class="band-plot-frame">
      <svg class="band-plot-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Phonon density of states plot">
        <rect class="band-plot-bg" x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${plotHeight}"></rect>
        ${xGridSvg}
        ${yGridSvg}
        ${zeroXLine}
        ${zeroYLine}
        ${seriesSvg}
        <text class="band-axis-label" x="${margin.left + plotWidth / 2}" y="${height - 6}" text-anchor="middle">${escapeHtml(payload.frequency_reference || "Frequency (THz)")}</text>
        <text class="band-axis-label" x="16" y="${margin.top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 16 ${margin.top + plotHeight / 2})">${escapeHtml(payload.density_reference || "states/THz")}</text>
      </svg>
    </div>
    ${artifactHtml}
  `;
}

function setDosArtifactsStatus(message, isError = false) {
  const node = document.getElementById("dos-artifacts-status");
  if (!node) {
    return;
  }
  node.textContent = message;
  node.classList.toggle("error", isError);
}

async function openRuntimePreview(path) {
  if (!state.selectedSystem || !path || !state.selectedSystemDetail) {
    return;
  }
  const modeChanged = state.beginnerMode;
  if (modeChanged) {
    setBeginnerMode(false);
  }
  switchView("workbench");
  setFlowView("setup");
  const previewFiles = Array.isArray(state.selectedSystemDetail.preview_files)
    ? state.selectedSystemDetail.preview_files
    : [];
  if (!previewFiles.includes(path)) {
    state.selectedSystemDetail.preview_files = [...previewFiles, path];
  }
  state.selectedFilePath = path;
  await renderFileSelector(state.selectedSystemDetail);
  document.getElementById("file-select")?.scrollIntoView({ behavior: "smooth", block: "start" });
  setFileStatus(
    `Loaded runtime artifact ${path}.${modeChanged ? " Switched to Expert Override so the Input Editor is visible." : ""}`
  );
}

function renderDosArtifacts(detail) {
  const root = document.getElementById("dos-artifacts");
  const chip = document.getElementById("dos-artifacts-chip");
  const note = document.getElementById("dos-artifacts-note");
  const button = document.getElementById("generate-pdos");
  if (!root || !chip || !note || !button) {
    return;
  }

  const payload = detail?.dos_artifacts || {};
  const pdosFiles = payload.pdos_files || [];
  const ipdosFiles = payload.ipdos_files || [];
  const sourceLabel = payload.source_run_dir || "n/a";

  chip.textContent = `pdos | ${payload.status || "review"}`;
  chip.className = `chip${payload.status === "ready" ? " primary" : ""}`;
  note.textContent = payload.note || "Generate element-resolved PDOS from the current DOSCAR without rerunning VASP.";
  button.disabled = !state.selectedSystem || !payload.can_generate_pdos;
  button.textContent = pdosFiles.length ? "Regenerate Element PDOS" : "Generate Element PDOS";

  const renderPreviewButtons = (paths, emptyText) => {
    if (!paths.length) {
      return `<div class="small-label">${emptyText}</div>`;
    }
    return `
      <div class="editor-buttons">
        ${paths.map((path) => `
          <button type="button" class="ghost" data-open-preview="${escapeHtml(path)}">${escapeHtml(path.split("/").pop())}</button>
        `).join("")}
      </div>
    `;
  };

  const logHtml = payload.log_path
    ? `
      <div class="editor-buttons">
        <button type="button" class="ghost" data-open-preview="${escapeHtml(payload.log_path)}">${escapeHtml(payload.log_path.split("/").pop())}</button>
      </div>
    `
    : `<div class="small-label">No PDOS log yet.</div>`;

  root.innerHTML = `
    <div class="summary-grid">
      <div class="metric">
        <div class="metric-label">Source</div>
        <div class="metric-value">${escapeHtml(sourceLabel)}</div>
        <div class="small-label">${payload.source_step ? `Using ${payload.source_step.toUpperCase()} DOSCAR as the PDOS source.` : "PDOS source is not available yet."}</div>
      </div>
      <div class="metric">
        <div class="metric-label">PDOS Files</div>
        <div class="metric-value">${pdosFiles.length || 0}</div>
        ${renderPreviewButtons(pdosFiles, "No PDOS_*.dat files are available yet.")}
      </div>
      <div class="metric">
        <div class="metric-label">Integrated PDOS</div>
        <div class="metric-value">${ipdosFiles.length || 0}</div>
        ${renderPreviewButtons(ipdosFiles, "No IPDOS_*.dat files are available yet.")}
      </div>
      <div class="metric">
        <div class="metric-label">VASPKIT Log</div>
        <div class="metric-value">${payload.log_path ? "ready" : "n/a"}</div>
        ${logHtml}
      </div>
    </div>
  `;

  root.querySelectorAll("[data-open-preview]").forEach((node) => {
    node.addEventListener("click", async () => {
      await openRuntimePreview(node.dataset.openPreview);
    });
  });
}

function setPrimitiveCellStatus(message, isError = false) {
  const node = document.getElementById("primitive-cell-status");
  if (!node) {
    return;
  }
  node.textContent = message;
  node.classList.toggle("error", Boolean(isError));
}

function renderPrimitiveCell(detail) {
  const root = document.getElementById("primitive-cell");
  const chip = document.getElementById("primitive-cell-chip");
  const note = document.getElementById("primitive-cell-note");
  const button = document.getElementById("generate-primitive-cell");
  if (!root || !chip || !note || !button) {
    return;
  }

  const payload = detail?.primitive_cell || {};
  const primitivePath = payload.primitive_path || null;
  const previewPaths = [payload.source_path, primitivePath, payload.symmetry_path, payload.log_path].filter(Boolean);
  const renderPreviewButtons = (paths, emptyText) => {
    if (!paths.length) {
      return `<div class="small-label">${emptyText}</div>`;
    }
    return `
      <div class="editor-buttons">
        ${paths.map((path) => `
          <button type="button" class="ghost" data-open-preview="${escapeHtml(path)}">${escapeHtml(path.split("/").pop())}</button>
        `).join("")}
      </div>
    `;
  };

  chip.textContent = `primitive | ${payload.status || "review"}`;
  chip.className = payload.status === "ready" ? "chip primary" : "chip";
  note.textContent = payload.note || "Generate a primitive cell from the relaxed CONTCAR when you want to inspect it or adopt it into a dedicated child project.";
  button.disabled = !state.selectedSystem || !payload.can_generate;
  button.textContent = primitivePath ? "Regenerate Primitive Cell" : "Generate Primitive Cell";
  setPrimitiveCellStatus(payload.note || "Generate a primitive cell from the relaxed CONTCAR when you want to inspect it or adopt it into a dedicated child project.", payload.status === "blocked");

  root.innerHTML = `
    <div class="summary-grid">
      <div class="metric">
        <div class="metric-label">Relax Source</div>
        <div class="metric-value">${payload.source_atoms ? `${payload.source_atoms} atoms` : "n/a"}</div>
        ${renderPreviewButtons(payload.source_path ? [payload.source_path] : [], "Relaxed CONTCAR is not available yet.")}
      </div>
      <div class="metric">
        <div class="metric-label">Primitive Cell</div>
        <div class="metric-value">${payload.primitive_atoms ? `${payload.primitive_atoms} atoms` : (primitivePath ? "ready" : "not generated")}</div>
        ${renderPreviewButtons(primitivePath ? [primitivePath] : [], "No PRIMCELL.vasp file is available yet.")}
      </div>
      <div class="metric">
        <div class="metric-label">Downstream Structure</div>
        <div class="metric-value">${escapeHtml(payload.downstream_structure_path || "POSCAR")}</div>
        <div class="small-label">This project keeps using its own resolved downstream structure. PRIMCELL.vasp is preview-only until you adopt it into a dedicated child project.</div>
      </div>
      <div class="metric">
        <div class="metric-label">VASPKIT Outputs</div>
        <div class="metric-value">${payload.log_path || payload.symmetry_path ? "ready" : "n/a"}</div>
        ${renderPreviewButtons([payload.symmetry_path, payload.log_path].filter(Boolean), "No primitive-cell log or symmetry report is available yet.")}
      </div>
    </div>
  `;

  root.querySelectorAll("[data-open-preview]").forEach((node) => {
    node.addEventListener("click", async () => {
      await openRuntimePreview(node.dataset.openPreview);
    });
  });
}

async function generatePrimitiveCell() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    setPrimitiveCellStatus(selectedSystemActionMessage("generating the primitive cell"), true);
    return;
  }
  let detailRefreshed = false;
  const button = document.getElementById("generate-primitive-cell");
  if (button) {
    button.disabled = true;
    button.textContent = "Generating...";
  }
  setPrimitiveCellStatus("Generating a primitive cell from runs/relax/CONTCAR...");
  try {
    const payload = await fetchJSON(`/api/systems/${encodeURIComponent(state.selectedSystem)}/relax/primitive`, {
      method: "POST",
      timeoutMs: LONG_TASK_TIMEOUT_MS,
    });
    if (payload.primitive_path) {
      state.selectedFilePath = payload.primitive_path;
    }
    await applySelectedSystemDetail(payload.detail);
    detailRefreshed = true;
    const regenerationErrors = Array.isArray(payload.regeneration_errors) ? payload.regeneration_errors : [];
    const errorSummary = regenerationErrors.length
      ? ` Review the VASPKIT outputs carefully: ${regenerationErrors[0]}`
      : "";
    setPrimitiveCellStatus(
      `Generated primitive cell at ${payload.generated_at || "now"}. Review it here, then create a primitive child project if you want a separate downstream workflow.${errorSummary}`,
      false,
    );
  } catch (error) {
    console.error(error);
    setPrimitiveCellStatus(`Primitive-cell generation failed: ${error.message}`, true);
  } finally {
    if (!detailRefreshed && button) {
      const payload = state.selectedSystemDetail?.primitive_cell || {};
      button.disabled = !state.selectedSystem || !payload.can_generate;
      button.textContent = payload.primitive_path ? "Regenerate Primitive Cell" : "Generate Primitive Cell";
    }
  }
}

function renderSystemAudit(detail) {
  const audit = detail.system_audit || { overall: "review", counts: {}, items: [] };
  document.getElementById("audit-chip").textContent =
    `${audit.overall} | ${audit.counts?.pass || 0}/${(audit.items || []).length}`;
  document.getElementById("audit-chip").className = `chip audit-${audit.overall || "review"}`;
  const root = document.getElementById("system-audit");
  root.innerHTML = (audit.items || []).map((item) => `
    <div class="audit-item audit-${item.status}">
      <div class="panel-head">
        <strong>${item.title}</strong>
        ${statusBadge(item.status)}
      </div>
      <div class="small-label">${item.note}</div>
    </div>
  `).join("");
}

function renderReportSnapshot(detail) {
  const report = detail.report_snapshot || { metrics: [] };
  const root = document.getElementById("report-snapshot");
  root.innerHTML = (report.metrics || []).map((item) => `
    <div class="report-item">
      <div class="panel-head">
        <strong>${item.label}</strong>
        <span class="mini-chip">${item.value}</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill tone-${item.tone || "review"}" style="width: ${Math.max(6, Math.round((item.fraction || 0) * 100))}%"></div>
      </div>
    </div>
  `).join("");
}

function renderStepFocus(detail) {
  const config = flowConfig();
  if (!config.step) {
    return;
  }
  const stepStatus = detail?.steps?.[config.step] || "not_started";
  const checks = flowCheckItems(detail, state.activeFlow);
  const files = flowFiles(detail, state.activeFlow);
  document.getElementById("step-focus-title").textContent = `${config.label} Focus`;
  document.getElementById("step-focus-summary").textContent = config.description || "Review this stage before submitting the next calculation.";
  document.getElementById("step-focus-chip").textContent = `${config.step} | ${stepStatus}`;
  document.getElementById("step-focus-guidance").textContent = config.guidance || "Review inputs and prerequisites before submitting.";
  document.getElementById("step-focus-checks").innerHTML = checks.length
    ? checks.map((item) => formatCheckItem(item.label, item.ok, item.note)).join("")
    : `<div class="small-label">No prerequisite checklist is defined for this stage.</div>`;
  document.getElementById("step-focus-files").innerHTML = files.length
    ? files.map((path) => `<div class="mini-chip">${path}</div>`).join("")
    : `<div class="small-label">No step-specific files detected yet.</div>`;
}

async function runBeginnerAction() {
  const detail = state.selectedSystemDetail;
  const next = recommendedBeginnerAction(detail);
  const status = document.getElementById("beginner-action-status");

  if (next.type === "project") {
    switchView("project-lab");
    status.textContent = "Project Lab is open. Import your structure there.";
    return;
  }
  if (next.type === "focus") {
    switchView("workbench");
    setFlowView(next.flow || "overview");
    status.textContent = `${next.actionLabel || "The suggested page is open"}.`;
    return;
  }
  if (next.type !== "step") {
    status.textContent = "No guided one-click step is pending right now.";
    return;
  }

  switchView("workbench");
  setFlowView(next.step);
  status.textContent = `Running a pre-submit check before ${plainStepLabel(next.step)}...`;

  const target = localProfileName();
  const mpi = 1;
  try {
    const preview = await fetchJSON("/api/jobs/validate-submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        step: next.step,
        target,
        mpi_np: mpi,
      }),
    });
    renderSubmitReview(preview);
    if (preview.blocking_errors?.length) {
      status.textContent = `Blocked: ${preview.blocking_errors[0]}`;
      return;
    }
    if (preview.warnings?.length) {
      const confirmed = window.confirm(
        `${plainStepLabel(next.step)} has ${preview.warnings.length} warning(s). Click OK to continue anyway.`
      );
      if (!confirmed) {
        status.textContent = "Submission cancelled after warning review.";
        return;
      }
    }

    const payload = await fetchJSON("/api/jobs/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        step: next.step,
        target,
        mpi_np: mpi,
      }),
    });
    state.selectedJobId = payload.id;
    await refreshJobs();
    await selectJob(payload.id);
    status.textContent = `Submitted ${plainStepLabel(next.step)} for ${payload.system}.`;
  } catch (error) {
    console.error(error);
    status.textContent = `Unable to submit ${plainStepLabel(next.step)}: ${error.message}`;
  }
}

function syncFlowPanels() {
  document.querySelectorAll("[data-flow-panels]").forEach((panel) => {
    const allowed = String(panel.dataset.flowPanels || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const visible = allowed.includes(state.activeFlow);
    panel.classList.toggle("flow-hidden", !visible);
  });
}

function syncFlowMeta() {
  const config = flowConfig();
  document.getElementById("workflow-chip").textContent = config.label;
  document.getElementById("submitter-title").textContent = config.step ? `${config.label} Submitter` : "Unified Submitter";
  document.getElementById("submitter-note").textContent = config.submitterNote || config.guidance || "Preview first, then submit to local or remote targets.";
  document.getElementById("result-highlights-title").textContent = config.step ? `${config.label} Highlights` : "Result Highlights";
  document.getElementById("result-highlights-note").textContent = config.highlightNote || "Surface the most important completed outputs without opening raw files.";
  document.getElementById("project-tree-note").textContent = config.treeNote || "Quick view of generated inputs and run outputs.";
  document.getElementById("jobs-title").textContent = config.step ? `${config.label} Jobs` : "Jobs";
  document.getElementById("jobs-note").textContent = config.jobsNote || "Recent local and remote submissions.";
  document.getElementById("job-log-title").textContent = config.step ? `${config.label} Log` : "Job Log";
  setJobLogNote();
  const submitStep = document.getElementById("submit-step");
  if (config.step) {
    submitStep.value = config.step;
    submitStep.disabled = true;
  } else {
    submitStep.disabled = false;
  }
  submitStep.parentElement.classList.toggle("hidden", Boolean(config.step));
}

function setFlowView(flowName) {
  state.activeFlow = WORKFLOW_VIEWS[flowName] ? flowName : "overview";
  document.querySelectorAll("[data-flow-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.flowTarget === state.activeFlow);
  });
  syncFlowPanels();
  syncFlowMeta();
  if (state.selectedSystemDetail) {
    renderSteps(state.selectedSystemDetail);
    renderResultHighlights(state.selectedSystemDetail);
    renderTree(state.selectedSystemDetail);
    renderJobs();
    if (flowConfig().step) {
      renderStepFocus(state.selectedSystemDetail);
    }
  }
  if (state.selectedSystem && (state.selectedJobId || state.selectedJobResultContext)) {
    void refreshSelectedSystemDetail();
  }
}

function flowConfigForStep(stepName) {
  return Object.values(WORKFLOW_VIEWS).find((item) => item.step === stepName) || null;
}

function selectedJobContextTarget(jobId = state.selectedJobId) {
  if (!jobId) {
    return null;
  }
  const job = (state.jobs || []).find((item) => item.id === jobId) || null;
  if (!job) {
    return null;
  }
  if (state.selectedSystem && job.system !== state.selectedSystem) {
    return null;
  }
  return job;
}

function withHistoryNote(payload, context) {
  if (!payload) {
    return null;
  }
  const noteParts = [payload.note, context?.source_note].filter(Boolean);
  return {
    ...payload,
    note: noteParts.join(" "),
    is_historical: true,
    historical_job_id: context?.job_id || null,
    source_kind: context?.source_kind || "history",
  };
}

function historicalPlaceholderPayload(stepName, context) {
  const config = flowConfigForStep(stepName);
  return {
    mode: stepName,
    status: "review",
    title: `${config?.label || stepName} result unavailable`,
    note: context?.source_note || `Historical ${stepName} results are not available for this job.`,
    series: [],
    projected_series: [],
    artifacts: [],
    is_historical: true,
    historical_job_id: context?.job_id || null,
    source_kind: context?.source_kind || "history",
  };
}

function mergeJobResultContext(detail, context) {
  state.selectedJobResultContext = context || null;
  if (!detail || !context || !context.step) {
    return detail;
  }

  const merged = { ...detail };
  const stepConfig = flowConfigForStep(context.step);
  const baseHighlights = Array.isArray(detail.result_highlights) ? detail.result_highlights : [];
  const historyHighlights = (
    Array.isArray(context.result_highlights) && context.result_highlights.length
      ? context.result_highlights
      : [{
        title: `${stepConfig?.label || context.step} History`,
        status: "review",
        value: context.available ? "Historical artifacts ready" : "No archived result",
        note: context.source_note || "",
      }]
  ).map((item) => ({
    ...item,
    note: [item.note, context.source_note].filter(Boolean).join(" "),
  }));

  if (stepConfig?.highlightMatchers?.length) {
    merged.result_highlights = [
      ...baseHighlights.filter((item) => {
        const haystack = `${item.title || ""} ${item.note || ""} ${item.value || ""}`;
        return !stepConfig.highlightMatchers.some((pattern) => pattern.test(haystack));
      }),
      ...historyHighlights,
    ];
  } else {
    merged.result_highlights = [...baseHighlights, ...historyHighlights];
  }

  const historyTree = (context.tree || []).map((item) => ({
    ...item,
    historical_job_id: item.historical_job_id || context.job_id,
    historical_step: item.historical_step || context.step,
    source_kind: item.source_kind || context.source_kind || "history",
  }));
  const historyPreviewFiles = Array.from(new Set(context.preview_files || []));
  const historyFileStates = Object.fromEntries(historyPreviewFiles.map((path) => [path, {
    ...(detail.file_states?.[path] || {}),
    historical: true,
    historical_job_id: context.job_id,
    historical_step: context.step,
    source_kind: context.source_kind || "history",
    writable: false,
    note: context.source_note || "Read-only result file from the selected job.",
  }]));
  merged.tree = [
    ...(Array.isArray(detail.tree) ? detail.tree.filter((item) => !item.historical_job_id) : []),
    ...historyTree,
  ];
  merged.preview_files = [
    ...(Array.isArray(detail.preview_files) ? detail.preview_files.filter((path) => !historyPreviewFiles.includes(path)) : []),
    ...historyPreviewFiles,
  ];
  merged.file_states = {
    ...(detail.file_states || {}),
    ...historyFileStates,
  };

  if (context.step === "band") {
    merged.band_visualization = context.band_visualization
      ? withHistoryNote(context.band_visualization, context)
      : historicalPlaceholderPayload("band", context);
  } else if (context.step === "dos" || context.step === "scf") {
    merged.dos_visualization = context.dos_visualization
      ? withHistoryNote(context.dos_visualization, context)
      : historicalPlaceholderPayload("dos", context);
    merged.dos_artifacts = context.dos_artifacts
      ? {
        ...context.dos_artifacts,
        note: [context.dos_artifacts.note, context.source_note].filter(Boolean).join(" "),
        can_generate_pdos: false,
        source_run_dir: context.source_run_dir || context.dos_artifacts.source_run_dir || "n/a",
        historical_job_id: context.job_id,
        is_historical: true,
      }
      : {
        status: "review",
        note: context.source_note || "Historical PDOS artifacts are not available for this job.",
        can_generate_pdos: false,
        source_run_dir: context.source_run_dir || "n/a",
        source_step: context.step,
        pdos_files: [],
        ipdos_files: [],
        log_path: null,
        historical_job_id: context.job_id,
        is_historical: true,
      };
  } else if (context.step === "phonon") {
    merged.phonon_visualization = context.phonon_visualization
      ? withHistoryNote(context.phonon_visualization, context)
      : historicalPlaceholderPayload("phonon", context);
    merged.phonon_dos_visualization = context.phonon_dos_visualization
      ? withHistoryNote(context.phonon_dos_visualization, context)
      : historicalPlaceholderPayload("phonon_dos", context);
  }

  return merged;
}

async function loadSelectedSystemDetail(systemName = state.selectedSystem) {
  if (!systemName) {
    state.selectedJobResultContext = null;
    return null;
  }
  const detail = await fetchJSON(`/api/systems/${encodeURIComponent(systemName)}`);
  const job = selectedJobContextTarget();
  if (!job || job.system !== systemName) {
    state.selectedJobResultContext = null;
    return detail;
  }
  try {
    const context = await fetchJSON(`/api/jobs/${encodeURIComponent(job.id)}/result-context`);
    return mergeJobResultContext(detail, context);
  } catch (error) {
    console.error(error);
    state.selectedJobResultContext = null;
    return detail;
  }
}

async function applySelectedSystemDetail(detail) {
  state.selectedSystemDetail = detail;
  renderSummary(detail);
  renderInputReview(detail);
  renderSteps(detail);
  renderResultHighlights(detail);
  renderBandVisualization(detail);
  renderDosVisualization(detail);
  renderPhononVisualization(detail);
  renderPhononDosVisualization(detail);
  renderDosArtifacts(detail);
  renderPrimitiveCell(detail);
  renderSystemAudit(detail);
  renderReportSnapshot(detail);
  renderMaterialSettings(detail);
  renderPhononNac(detail);
  renderTree(detail);
  await renderFileSelector(detail);
  renderStructure(detail.structure);
  renderBeginnerGuide(detail);
  syncDeleteProjectControls();
  if (flowConfig().step) {
    renderStepFocus(detail);
  }
  syncFlowPanels();
  syncFlowMeta();
  renderJobs();
}

async function generateElementPdos() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    setDosArtifactsStatus(selectedSystemActionMessage("generating PDOS"), true);
    return;
  }
  const button = document.getElementById("generate-pdos");
  if (button) {
    button.disabled = true;
    button.textContent = "Generating...";
  }
  setDosArtifactsStatus("Generating element-resolved PDOS from the existing DOSCAR...");
  try {
    const payload = await fetchJSON(`/api/systems/${encodeURIComponent(state.selectedSystem)}/dos/pdos`, {
      method: "POST",
      timeoutMs: LONG_TASK_TIMEOUT_MS,
    });
    if (payload.preview_path) {
      state.selectedFilePath = payload.preview_path;
    }
    await applySelectedSystemDetail(payload.detail);
    setDosArtifactsStatus(
      `Generated PDOS from ${payload.source_step || "dos"} at ${payload.generated_at || "now"}. Use the buttons above to open the PDOS files in Setup -> Input Editor.`
    );
  } catch (error) {
    console.error(error);
    setDosArtifactsStatus(`PDOS generation failed: ${error.message}`, true);
  } finally {
    if (state.selectedSystemDetail) {
      renderDosArtifacts(state.selectedSystemDetail);
    }
  }
}

function renderMaterialSettings(detail) {
  const material = detail.material_settings || {};
  const familySelect = document.getElementById("material-potcar-family");
  const profileSelect = document.getElementById("material-potcar-profile");
  const geometryFunctionalSelect = document.getElementById("material-xc-geometry");
  const electronicFunctionalSelect = document.getElementById("material-xc-electronic");
  const families = material.potcar_families || [];
  const profiles = material.potcar_profiles || [];
  const geometryFunctionalOptions = material.xc_geometry_options || [];
  const electronicFunctionalOptions = material.xc_electronic_options || [];
  familySelect.innerHTML = families.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
  if (!families.length && material.potcar_family) {
    familySelect.innerHTML = `<option value="${escapeHtml(material.potcar_family)}">${escapeHtml(material.potcar_family)}</option>`;
  }
  profileSelect.innerHTML = profiles.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  if (!profiles.length && material.potcar_profile) {
    profileSelect.innerHTML = `<option value="${escapeHtml(material.potcar_profile)}">${escapeHtml(material.potcar_profile)}</option>`;
  }
  geometryFunctionalSelect.innerHTML = geometryFunctionalOptions.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  if (!geometryFunctionalOptions.length && material.xc_geometry) {
    geometryFunctionalSelect.innerHTML = `<option value="${escapeHtml(material.xc_geometry)}">${escapeHtml(material.xc_geometry)}</option>`;
  }
  electronicFunctionalSelect.innerHTML = electronicFunctionalOptions.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  if (!electronicFunctionalOptions.length && material.xc_electronic) {
    electronicFunctionalSelect.innerHTML = `<option value="${escapeHtml(material.xc_electronic)}">${escapeHtml(material.xc_electronic)}</option>`;
  }

  const setValue = (id, value) => {
    const node = document.getElementById(id);
    if (node) {
      node.value = value;
    }
  };
  setValue("material-formula", material.formula || detail.name || "");
  setValue("material-class", material.material_class || "bulk");
  setValue("material-electronic-type", material.electronic_type || "auto");
  setValue("material-xc-geometry", material.xc_geometry || "PBE");
  setValue("material-xc-electronic", material.xc_electronic || "PBE");
  const spinToggle = document.getElementById("material-spin-polarized");
  if (spinToggle) {
    spinToggle.checked = Boolean(material.spin_polarized);
  }
  setValue("material-encut", material.encut_text || material.encut || 520);
  setValue("material-potcar-family", material.potcar_family || "");
  setValue("material-potcar-profile", material.potcar_profile || "conservative");
  setValue("material-kmesh", material.kmesh_text || "");
  setValue("material-dos-kmesh", material.dos_kmesh_text || "");
  setValue("material-phonon-kmesh", material.phonon_kmesh_text || "");
  setValue("material-phonon-dos-kmesh", material.phonon_dos_kmesh_text || "");
  setValue("material-phonon-supercell", material.phonon_supercell_text || "");
  setValue("material-magmom", material.magmom_text || "");
  setValue("material-band-points", material.band_points || 101);
  setValue("material-band-distance", material.band_kpoints_distance || 0.05);
  setValue("material-band-path-mode", material.band_path_mode || "auto");
  setValue("material-band-path-text", material.band_path_text || "");
  setValue("material-wallclock", material.wallclock_seconds || 43200);
  setValue("material-overrides", material.advanced_overrides_json || "");
  document.getElementById("material-class-help").textContent =
    MATERIAL_CLASS_HELP[material.material_class || "bulk"] || MATERIAL_CLASS_HELP.bulk;
  syncMaterialBandPathMode();
  renderPotcarSelectors(material.potcar_options || []);
  renderMaterialPresets(material.presets || []);
  document.getElementById("material-form-chip").textContent =
    `${material.species?.join("-") || "idle"} | ${material.total_atoms || 0} atoms`;
  const potcarStatus = material.potcar_generation_available
    ? "POTCAR, "
    : "";
  setMaterialFormStatus(`Material parameters loaded. Save Parameters will regenerate ${potcarStatus}KPOINTS.*, band.conf, and INCAR.* from the current settings.`);
}

function renderPhononNac(detail) {
  const payload = detail?.phonon_nac || {};
  const chip = document.getElementById("phonon-nac-chip");
  const enabledToggle = document.getElementById("phonon-nac-enabled");
  const qDirectionInput = document.getElementById("phonon-nac-q-direction");
  const summary = document.getElementById("phonon-nac-summary");
  const saveButton = document.getElementById("phonon-nac-save");
  const prepareChargeButton = document.getElementById("phonon-nac-prepare-charge");
  const buildBornButton = document.getElementById("phonon-nac-build-born");
  if (!chip || !enabledToggle || !qDirectionInput || !summary || !saveButton || !prepareChargeButton || !buildBornButton) {
    return;
  }

  enabledToggle.checked = Boolean(payload.enabled);
  qDirectionInput.value = payload.q_direction_text || "";
  chip.textContent = payload.enabled
    ? (payload.born_available ? "NAC ready" : "NAC needs BORN")
    : "NAC off";
  chip.className = `chip ${payload.enabled ? (payload.born_available ? "" : "risk-review") : ""}`.trim();

  const notes = [
    payload.enabled ? "band.conf requests NAC." : "band.conf keeps NAC disabled.",
    payload.charge_lepsilon ? "INCAR.charge already sets LEPSILON." : "INCAR.charge does not yet request LEPSILON.",
    payload.q_direction_text ? `Q_DIRECTION = ${payload.q_direction_text}.` : "Q_DIRECTION is unset.",
    payload.born_available ? `BORN is available at ${payload.born_path}.` : "No BORN file is available yet.",
    payload.charge_outcar_ready ? `Charge OUTCAR is available at ${payload.charge_outcar_path}.` : "Charge OUTCAR is not ready yet.",
    payload.born_tool_available ? `${payload.born_tool} is available for BORN generation.` : "phonopy-vasp-born is not available in PATH.",
  ];
  summary.textContent = notes.join(" ");

  saveButton.disabled = !state.selectedSystem;
  prepareChargeButton.disabled = !state.selectedSystem;
  buildBornButton.disabled = !state.selectedSystem || !payload.charge_outcar_ready || !payload.born_tool_available;
}

async function savePhononNacSettings() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    setPhononNacStatus(selectedSystemActionMessage("saving phonon NAC settings"), true);
    return;
  }
  try {
    const payload = await fetchJSON("/api/phonon/nac/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        enabled: document.getElementById("phonon-nac-enabled").checked,
        q_direction_text: document.getElementById("phonon-nac-q-direction").value,
      }),
    });
    state.selectedFilePath = "band.conf";
    await applySelectedSystemDetail(payload.detail);
    const qDirection = payload.phonon_nac?.q_direction_text ? ` Q_DIRECTION=${payload.phonon_nac.q_direction_text}.` : "";
    setPhononNacStatus(`Saved NAC settings to band.conf at ${payload.saved_at}. NAC=${payload.phonon_nac?.enabled ? "on" : "off"}.${qDirection}`);
  } catch (error) {
    setPhononNacStatus(`Saving NAC settings failed: ${error.message}`, true);
  }
}

async function prepareChargeForPhononNac() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    setPhononNacStatus(selectedSystemActionMessage("preparing BORN charge inputs"), true);
    return;
  }
  try {
    const payload = await fetchJSON("/api/phonon/nac/prepare-charge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ system: state.selectedSystem }),
    });
    state.selectedFilePath = "INCAR.charge";
    await applySelectedSystemDetail(payload.detail);
    setPhononNacStatus(`Prepared INCAR.charge for dielectric/BORN generation at ${payload.saved_at}. LEPSILON, EDIFF, LREAL, NSW, and IBRION are now aligned for the NAC helper path.`);
  } catch (error) {
    setPhononNacStatus(`Preparing INCAR.charge failed: ${error.message}`, true);
  }
}

async function buildBornFromCharge() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    setPhononNacStatus(selectedSystemActionMessage("building the BORN file"), true);
    return;
  }
  try {
    const payload = await fetchJSON("/api/phonon/nac/build-born", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ system: state.selectedSystem }),
      timeoutMs: LONG_TASK_TIMEOUT_MS,
    });
    state.selectedFilePath = payload.path || "BORN";
    await applySelectedSystemDetail(payload.detail);
    setPhononNacStatus(`Built BORN at ${payload.saved_at} from ${payload.source_outcar}. Preview ${payload.path} in the Input Editor before submitting phonon.`);
  } catch (error) {
    setPhononNacStatus(`BORN generation failed: ${error.message}`, true);
  }
}

function renderMaterialPresets(presets) {
  const root = document.getElementById("material-presets");
  if (!presets.length) {
    root.innerHTML = "";
    return;
  }
  root.innerHTML = presets.map((preset) => `
    <button class="preset-chip ghost" type="button" data-material-preset="${escapeHtml(preset.id)}" title="${escapeHtml(preset.description)}">
      ${escapeHtml(preset.label)}
    </button>
  `).join("");
  root.querySelectorAll("[data-material-preset]").forEach((node) => {
    node.addEventListener("click", () => applyMaterialPreset(node.dataset.materialPreset));
  });
}

function applyMaterialPreset(presetId) {
  const presets = state.selectedSystemDetail?.material_settings?.presets || [];
  const preset = presets.find((item) => item.id === presetId);
  if (!preset) {
    return;
  }
  const setValue = (id, value) => {
    const node = document.getElementById(id);
    if (node) {
      node.value = value;
    }
  };
  const currentMaterialClass = document.getElementById("material-class")?.value || "";
  const resolvedMaterialClass = presetId === "slab_2d" && ["2d", "slab"].includes(currentMaterialClass)
    ? currentMaterialClass
    : (preset.material_class || "bulk");
  setValue("material-class", resolvedMaterialClass);
  setValue("material-electronic-type", preset.electronic_type || "auto");
  setValue("material-xc-geometry", preset.xc_geometry || "PBE");
  setValue("material-xc-electronic", preset.xc_electronic || "PBE");
  const spinToggle = document.getElementById("material-spin-polarized");
  if (spinToggle) {
    spinToggle.checked = Boolean(preset.spin_polarized);
  }
  setValue("material-encut", preset.encut_text || preset.encut || 520);
  setValue("material-potcar-profile", preset.potcar_profile || "conservative");
  setValue("material-kmesh", preset.kmesh_text || "");
  setValue("material-dos-kmesh", preset.dos_kmesh_text || "");
  setValue("material-phonon-kmesh", preset.phonon_kmesh_text || "");
  setValue("material-phonon-dos-kmesh", preset.phonon_dos_kmesh_text || "");
  setValue("material-phonon-supercell", preset.phonon_supercell_text || "");
  setValue("material-magmom", preset.magmom_text || "");
  setValue("material-band-points", preset.band_points || 101);
  setValue("material-band-distance", preset.band_kpoints_distance || 0.05);
  setValue("material-band-path-mode", "auto");
  setValue("material-band-path-text", "");
  setValue("material-wallclock", preset.wallclock_seconds || 43200);
  document.getElementById("material-class-help").textContent =
    MATERIAL_CLASS_HELP[resolvedMaterialClass] || MATERIAL_CLASS_HELP.bulk;
  syncMaterialBandPathMode();
  applyPotcarProfileRecommendations();
  document.getElementById("material-preset-status").textContent =
    hasNonzeroFloatList(preset.magmom_text)
      ? `Applied preset ${preset.label}. Kept material type as ${resolvedMaterialClass}. An initial MAGMOM guess was loaded; review it before saving.`
      : `Applied preset ${preset.label}. Kept material type as ${resolvedMaterialClass}. MAGMOM still needs manual review before saving.`;
}

function syncMaterialBandPathMode() {
  const mode = document.getElementById("material-band-path-mode")?.value || "auto";
  const wrap = document.getElementById("material-band-path-wrap");
  const textarea = document.getElementById("material-band-path-text");
  if (!wrap || !textarea) {
    return;
  }
  const custom = mode === "custom";
  wrap.classList.toggle("hidden", !custom);
  textarea.disabled = !custom;
  if (!custom) {
    textarea.value = textarea.value || "";
  }
}

function renderPotcarSelectors(entries) {
  const root = document.getElementById("material-potcar-selectors");
  const activeProfile = document.getElementById("material-potcar-profile")?.value || "conservative";
  if (!entries.length) {
    root.innerHTML = `<div class="small-label">No species available for POTCAR mapping yet.</div>`;
    return;
  }
  root.innerHTML = entries.map((entry) => `
    <label class="mapping-item">
      <span>${escapeHtml(entry.species)}${entry.recommended_by_profile?.[activeProfile] ? ` <span class="mini-chip recommended">recommended ${escapeHtml(entry.recommended_by_profile[activeProfile])}</span>` : ""}</span>
      <select data-potcar-species="${escapeHtml(entry.species)}">
        ${(entry.options || []).map((option) => `
          <option value="${escapeHtml(option)}" ${option === entry.selected ? "selected" : ""}>${escapeHtml(option)}${option === entry.recommended_by_profile?.[activeProfile] ? " (recommended)" : ""}</option>
        `).join("")}
      </select>
    </label>
  `).join("");
}

function potcarSelectorForSpecies(species) {
  return Array.from(document.querySelectorAll("[data-potcar-species]"))
    .find((node) => node.dataset.potcarSpecies === String(species));
}

function applyPotcarProfileRecommendations() {
  const activeProfile = document.getElementById("material-potcar-profile").value || "conservative";
  const entries = state.selectedSystemDetail?.material_settings?.potcar_options || [];
  entries.forEach((entry) => {
    const select = potcarSelectorForSpecies(entry.species);
    const recommended = entry.recommended_by_profile?.[activeProfile];
    if (select && recommended) {
      select.value = recommended;
    }
  });
  renderPotcarSelectors(entries);
  entries.forEach((entry) => {
    const select = potcarSelectorForSpecies(entry.species);
    const recommended = entry.recommended_by_profile?.[activeProfile];
    if (select && recommended) {
      select.value = recommended;
    }
  });
  setMaterialFormStatus(`Applied ${activeProfile} POTCAR recommendations to the current selector set.`);
}

function currentPotcarMappingText() {
  const selectors = Array.from(document.querySelectorAll("[data-potcar-species]"));
  return selectors.map((node) => `${node.dataset.potcarSpecies} = ${node.value}`).join("\n");
}

function renderTree(detail) {
  const entries = filteredTreeEntries(detail, state.activeFlow);
  if (!entries.length) {
    document.getElementById("project-tree").innerHTML = `
      <div class="small-label">No files are being surfaced for the ${flowConfig().label} view yet.</div>
    `;
    return;
  }
  document.getElementById("project-tree").innerHTML = entries.map((item) => `
    <div class="tree-item">
      <span>${item.type === "dir" ? "[DIR]" : "[FILE]"} ${escapeHtml(item.path)}</span>
      <span class="small-label">${item.historical_job_id ? `history ${escapeHtml(item.source_kind || "result")} | ${escapeHtml(item.historical_job_id)}` : escapeHtml(item.size ?? "")}</span>
    </div>
  `).join("");
}

function setFileStatus(message, isError = false) {
  const node = document.getElementById("file-status");
  node.textContent = message;
  node.classList.toggle("error", isError);
}

function setIncarFormStatus(message, isError = false) {
  const node = document.getElementById("incar-form-status");
  node.textContent = message;
  node.classList.toggle("error", isError);
}

function setMaterialFormStatus(message, isError = false) {
  const node = document.getElementById("material-form-status");
  node.textContent = message;
  node.classList.toggle("error", isError);
}

function setPhononNacStatus(message, isError = false) {
  const node = document.getElementById("phonon-nac-status");
  node.textContent = message;
  node.classList.toggle("error", isError);
}

function currentEditorText() {
  return document.getElementById("file-editor").value || "";
}

function autogenerateSupported(path) {
  return Boolean(path) && (path.startsWith("INCAR.") || path.startsWith("KPOINTS.") || path === "KPATH.in" || path === "band.conf");
}

function currentFileMeta() {
  return state.selectedFileMeta || {};
}

function fileStateTags(meta = {}, { includePreview = true } = {}) {
  const tags = [];
  if (meta.historical) {
    tags.push("history");
  }
  if (meta.generated) {
    tags.push("generated");
  }
  if (meta.manual_override) {
    tags.push("manual");
  }
  if (meta.stale) {
    tags.push("stale");
  }
  if (meta.managed && !meta.tracked) {
    tags.push("untracked");
  }
  if (!tags.length && includePreview && meta.writable === false && !meta.generated) {
    tags.push("preview");
  }
  return tags;
}

function fileStateNote(meta = {}) {
  return meta.note ? ` ${meta.note}` : "";
}

function fileIsWritable() {
  return Boolean(currentFileMeta().writable);
}

function fileIsGenerated() {
  return Boolean(currentFileMeta().generated);
}

function incarFormSupported(path) {
  return Boolean(path) && path.startsWith("INCAR.");
}

function syncGenerateButton() {
  const meta = currentFileMeta();
  document.getElementById("generate-file").disabled = !autogenerateSupported(state.selectedFilePath);
  document.getElementById("save-file").disabled = !fileIsWritable();
  document.getElementById("file-editor").readOnly = !fileIsWritable();
  let chip = fileIsWritable() ? "editable source" : "generated preview";
  if (meta.historical) {
    chip = "historical result";
  }
  if (meta.template_driven && fileIsWritable()) {
    chip = "template source";
  }
  if (meta.manual_override) {
    chip = "manual override";
  } else if (meta.stale) {
    chip = "stale input";
  }
  document.getElementById("file-mode-chip").textContent = chip;
}

function parseIncarText(text) {
  const values = {};
  text.split(/\r?\n/).forEach((line) => {
    const body = line.split("!")[0].split("#")[0].trim();
    if (!body.includes("=")) {
      return;
    }
    const [rawKey, ...rest] = body.split("=");
    const key = rawKey.trim().toUpperCase();
    const value = rest.join("=").trim();
    if (key) {
      values[key] = value;
    }
  });
  return values;
}

function incarTruthy(value) {
  if (!value) {
    return false;
  }
  return [".TRUE.", "TRUE", "T", "1", "Y", "YES"].includes(String(value).trim().toUpperCase());
}

function setIncarFormEnabled(enabled) {
  [
    "incar-encut",
    "incar-ispin",
    "incar-ismear",
    "incar-sigma",
    "incar-ediff",
    "incar-ediffg",
    "incar-algo",
    "incar-lreal",
    "incar-magmom",
    "incar-ldau",
    "incar-ldautype",
    "incar-ldaul",
    "incar-ldauu",
    "incar-ldauj",
    "incar-load",
    "incar-apply",
  ].forEach((id) => {
    document.getElementById(id).disabled = !enabled;
  });
  document.getElementById("incar-form-chip").textContent = enabled ? "active" : "inactive";
}

function fillIncarFormFromValues(values = {}) {
  document.getElementById("incar-encut").value = values.ENCUT || "";
  document.getElementById("incar-ispin").value = values.ISPIN || "2";
  document.getElementById("incar-ismear").value = values.ISMEAR || "";
  document.getElementById("incar-sigma").value = values.SIGMA || "";
  document.getElementById("incar-ediff").value = values.EDIFF || "";
  document.getElementById("incar-ediffg").value = values.EDIFFG || "";
  document.getElementById("incar-algo").value = values.ALGO || "";
  document.getElementById("incar-lreal").value = values.LREAL || "";
  document.getElementById("incar-magmom").value = values.MAGMOM || "";
  document.getElementById("incar-ldau").checked = incarTruthy(values.LDAU);
  document.getElementById("incar-ldautype").value = values.LDAUTYPE || "2";
  document.getElementById("incar-ldaul").value = values.LDAUL || "";
  document.getElementById("incar-ldauu").value = values.LDAUU || "";
  document.getElementById("incar-ldauj").value = values.LDAUJ || "";
}

function clearIncarForm() {
  fillIncarFormFromValues({});
}

function syncIncarFormFromEditor() {
  if (!incarFormSupported(state.selectedFilePath)) {
    setIncarFormEnabled(false);
    clearIncarForm();
    setIncarFormStatus("Select an `INCAR.*` file to edit common parameters with the form.");
    return;
  }
  setIncarFormEnabled(true);
  fillIncarFormFromValues(parseIncarText(currentEditorText()));
  setIncarFormStatus(`Loaded parameters from ${state.selectedFilePath}.`);
}

function setOrReplaceIncarKey(lines, key, value) {
  const regex = new RegExp(`^\\s*${key}\\s*=`, "i");
  const replacement = `${key} = ${value}`;
  let replaced = false;
  const output = [];
  lines.forEach((line) => {
    if (regex.test(line)) {
      if (!replaced) {
        output.push(replacement);
        replaced = true;
      }
      return;
    }
    output.push(line);
  });
  if (!replaced) {
    while (output.length && output[output.length - 1].trim() === "") {
      output.pop();
    }
    output.push(replacement);
  }
  return output;
}

function removeIncarKey(lines, key) {
  const regex = new RegExp(`^\\s*${key}\\s*=`, "i");
  return lines.filter((line) => !regex.test(line));
}

function applyIncarFormToEditor() {
  if (!incarFormSupported(state.selectedFilePath)) {
    setIncarFormStatus("Select an `INCAR.*` file first.", true);
    return;
  }

  let lines = currentEditorText().split(/\r?\n/);
  const encut = document.getElementById("incar-encut").value.trim();
  const ispin = document.getElementById("incar-ispin").value.trim() || "2";
  const ismear = document.getElementById("incar-ismear").value.trim();
  const sigma = document.getElementById("incar-sigma").value.trim();
  const ediff = document.getElementById("incar-ediff").value.trim();
  const ediffg = document.getElementById("incar-ediffg").value.trim();
  const algo = document.getElementById("incar-algo").value.trim();
  const lreal = document.getElementById("incar-lreal").value.trim();
  const magmom = document.getElementById("incar-magmom").value.trim();
  const ldauEnabled = document.getElementById("incar-ldau").checked;
  const ldautype = document.getElementById("incar-ldautype").value.trim() || "2";
  const ldaul = document.getElementById("incar-ldaul").value.trim();
  const ldauu = document.getElementById("incar-ldauu").value.trim();
  const ldauj = document.getElementById("incar-ldauj").value.trim();

  if (encut) {
    lines = setOrReplaceIncarKey(lines, "ENCUT", encut);
  }
  lines = setOrReplaceIncarKey(lines, "ISPIN", ispin);
  if (ismear) {
    lines = setOrReplaceIncarKey(lines, "ISMEAR", ismear);
  }
  if (sigma) {
    lines = setOrReplaceIncarKey(lines, "SIGMA", sigma);
  }
  if (ediff) {
    lines = setOrReplaceIncarKey(lines, "EDIFF", ediff);
  }
  if (ediffg) {
    lines = setOrReplaceIncarKey(lines, "EDIFFG", ediffg);
  }
  if (algo) {
    lines = setOrReplaceIncarKey(lines, "ALGO", algo);
  }
  if (lreal) {
    lines = setOrReplaceIncarKey(lines, "LREAL", lreal);
  }
  if (magmom) {
    lines = setOrReplaceIncarKey(lines, "MAGMOM", magmom);
  }

  if (ldauEnabled) {
    lines = setOrReplaceIncarKey(lines, "LDAU", ".TRUE.");
    lines = setOrReplaceIncarKey(lines, "LDAUTYPE", ldautype);
    if (ldaul) {
      lines = setOrReplaceIncarKey(lines, "LDAUL", ldaul);
    }
    if (ldauu) {
      lines = setOrReplaceIncarKey(lines, "LDAUU", ldauu);
    }
    if (ldauj) {
      lines = setOrReplaceIncarKey(lines, "LDAUJ", ldauj);
    }
  } else {
    lines = setOrReplaceIncarKey(lines, "LDAU", ".FALSE.");
    lines = removeIncarKey(lines, "LDAUTYPE");
    lines = removeIncarKey(lines, "LDAUL");
    lines = removeIncarKey(lines, "LDAUU");
    lines = removeIncarKey(lines, "LDAUJ");
  }

  document.getElementById("file-editor").value = `${lines.join("\n").replace(/\n+$/, "")}\n`;
  setIncarFormStatus(`Applied form values to ${state.selectedFilePath}. Save Changes to write the file.`);
}

async function loadSelectedFile(path) {
  if (!state.selectedSystem || !path) {
    clearSelectedFileSelection("No editable input file selected.");
    return;
  }
  if (!selectedSystemReady()) {
    clearSelectedFileSelection(selectedSystemActionMessage("loading input files"));
    return;
  }
  try {
    const payload = await fetchJSON(`/api/file?system=${encodeURIComponent(state.selectedSystem)}&path=${encodeURIComponent(path)}&expert=${state.beginnerMode ? "0" : "1"}`);
    const nextMeta = {
      ...(state.fileCatalog[payload.path] || {}),
      ...(payload.generation_state || {}),
      path: payload.path,
      writable: !payload.read_only && !payload.truncated,
      generated: Boolean(payload.generated),
      truncated: Boolean(payload.truncated),
      size_bytes: payload.size_bytes,
      preview_limit_bytes: payload.preview_limit_bytes,
    };
    state.selectedFilePath = payload.path;
    state.selectedFileMeta = nextMeta;
    state.fileCatalog[payload.path] = nextMeta;
    document.getElementById("file-editor").value = payload.content || "";
    syncGenerateButton();
    syncIncarFormFromEditor();
    if (payload.truncated) {
      setFileStatus(`Loaded a truncated preview of ${payload.path} (${payload.preview_limit_bytes || "limited"} of ${payload.size_bytes || "unknown"} bytes). This view is read-only to avoid saving partial content.`, false);
    } else if (nextMeta.historical) {
      setFileStatus(`Loaded ${payload.path}. This is a read-only result file from ${nextMeta.source_kind === "archive" ? "the archived" : "the selected"} job.${fileStateNote(nextMeta)}`, false);
    } else if (payload.read_only) {
      setFileStatus(`Loaded ${payload.path}. This file is generated from material parameters and can only be regenerated.${fileStateNote(nextMeta)}`, false);
    } else if (!state.beginnerMode && payload.generated) {
      setFileStatus(`Loaded ${payload.path}. Expert Override is active, so this generated file can be edited directly. Save Parameters may regenerate and overwrite it.${fileStateNote(nextMeta)}`);
    } else if (nextMeta.template_driven) {
      setFileStatus(`Loaded ${payload.path}. Manual edits are allowed, but Save Parameters may regenerate this template from the current material settings.${fileStateNote(nextMeta)}`);
    } else {
      setFileStatus(`Loaded ${payload.path}. Manual edits are allowed.${fileStateNote(nextMeta)}`);
    }
  } catch (error) {
    clearSelectedFileSelection(`Failed to load ${path}: ${error.message}`, true);
  }
}

async function reloadSelectedFile() {
  if (!state.selectedFilePath) {
    setFileStatus("No editable input file selected.");
    return;
  }
  await loadSelectedFile(state.selectedFilePath);
}

async function saveSelectedFile() {
  if (!state.selectedSystem || !state.selectedFilePath) {
    alert("Select a system and an input file first.");
    return;
  }
  if (!selectedSystemReady()) {
    setFileStatus(selectedSystemActionMessage("saving input files"), true);
    return;
  }
  if (!fileIsWritable()) {
    setFileStatus(`'${state.selectedFilePath}' is generated from material settings. Use Auto Generate instead of manual save.`, true);
    return;
  }
  try {
    const payload = await fetchJSON("/api/file/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        path: state.selectedFilePath,
        content: document.getElementById("file-editor").value,
        expert_override: !state.beginnerMode,
      }),
    });
    await applySelectedSystemDetail(payload.detail);
    syncGenerateButton();
    syncIncarFormFromEditor();
    setFileStatus(`Saved ${payload.path} at ${payload.saved_at}.${fileStateNote(currentFileMeta())}`);
  } catch (error) {
    setFileStatus(`Save failed: ${error.message}`, true);
  }
}

async function generateSelectedFile() {
  if (!state.selectedSystem || !state.selectedFilePath) {
    alert("Select a system and an input file first.");
    return;
  }
  if (!selectedSystemReady()) {
    setFileStatus(selectedSystemActionMessage("regenerating input files"), true);
    return;
  }
  if (!autogenerateSupported(state.selectedFilePath)) {
    setFileStatus(`Auto-generation is not supported for ${state.selectedFilePath}.`, true);
    return;
  }
  try {
    const payload = await fetchJSON("/api/file/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        path: state.selectedFilePath,
      }),
    });
    document.getElementById("file-editor").value = payload.content || "";
    syncIncarFormFromEditor();
    const noteText = payload.notes?.length ? ` ${payload.notes.join(" ")}` : "";
    if (payload.written && payload.detail) {
      await applySelectedSystemDetail(payload.detail);
      setFileStatus(`Regenerated ${payload.path} and wrote it to disk.${noteText}${fileStateNote(currentFileMeta())}`);
    } else {
      setFileStatus(`Generated ${payload.path}. Review then save.${noteText}`);
    }
  } catch (error) {
    setFileStatus(`Generation failed: ${error.message}`, true);
  }
}

async function renderFileSelector(detail) {
  const select = document.getElementById("file-select");
  const previewNames = detail.preview_files || [];
  const writableNames = new Set(detail.writable_files || []);
  const generatedNames = new Set(detail.generated_files || []);
  const fileStates = detail.file_states || {};
  state.fileCatalog = Object.fromEntries(previewNames.map((name) => {
    const meta = fileStates[name] || {};
    return [name, {
      ...meta,
      path: name,
      writable: writableNames.has(name),
      generated: generatedNames.has(name),
    }];
  }));

  select.innerHTML = previewNames.map((name) => {
    const meta = state.fileCatalog[name] || {};
    const tags = fileStateTags(meta);
    const suffix = tags.length ? ` [${tags.join("] [")}]` : "";
    return `<option value="${escapeHtml(name)}">${escapeHtml(name)}${escapeHtml(suffix)}</option>`;
  }).join("");
  if (!previewNames.length) {
    state.selectedFilePath = null;
    state.selectedFileMeta = null;
    document.getElementById("file-editor").value = "";
    syncGenerateButton();
    setFileStatus("This project has no editable input templates yet.");
    return;
  }

  const preferred = previewNames.includes(state.selectedFilePath) ? state.selectedFilePath : previewNames[0];
  select.value = preferred;
  state.selectedFilePath = preferred;
  state.selectedFileMeta = state.fileCatalog[preferred] || null;
  select.onchange = async () => {
    await loadSelectedFile(select.value);
  };
  await loadSelectedFile(preferred);
}

function elementColor(element) {
  const colors = {
    H: "#e6edf5",
    Na: "#7d5fff",
    Mn: "#bd5d38",
    Fe: "#bb7a24",
    Co: "#2e7bb4",
    O: "#d94b45",
    C: "#5a5a5a",
    N: "#4276ff",
  };
  return colors[element] || "#b55b2d";
}

function elementRadius(element) {
  const radii = {
    H: 0.18,
    Na: 0.55,
    Mn: 0.42,
    Fe: 0.4,
    Co: 0.39,
    O: 0.32,
    C: 0.3,
    N: 0.31,
  };
  return radii[element] || 0.34;
}

function rotatePoint(point) {
  const [x, y, z] = point;
  const cy = Math.cos(viewerState.rotY);
  const sy = Math.sin(viewerState.rotY);
  const cx = Math.cos(viewerState.rotX);
  const sx = Math.sin(viewerState.rotX);

  const x1 = x * cy + z * sy;
  const z1 = -x * sy + z * cy;
  const y2 = y * cx - z1 * sx;
  const z2 = y * sx + z1 * cx;
  return [x1, y2, z2];
}

function renderStructure(structure) {
  const canvas = document.getElementById("structure-canvas");
  const ctx = canvas.getContext("2d");
  const width = canvas.clientWidth || 640;
  const height = canvas.clientHeight || 360;
  canvas.width = width * window.devicePixelRatio;
  canvas.height = height * window.devicePixelRatio;
  ctx.setTransform(window.devicePixelRatio, 0, 0, window.devicePixelRatio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  ctx.fillStyle = "#fdf6eb";
  ctx.fillRect(0, 0, width, height);

  if (!structure) {
    ctx.fillStyle = "#6e6558";
    ctx.font = "16px IBM Plex Sans";
    ctx.fillText("No POSCAR structure available.", 20, 40);
    return;
  }

  const lattice = structure.lattice;
  const atoms = structure.atoms || [];
  const corners = [
    [0, 0, 0],
    lattice[0],
    lattice[1],
    lattice[2],
    [lattice[0][0] + lattice[1][0], lattice[0][1] + lattice[1][1], lattice[0][2] + lattice[1][2]],
    [lattice[0][0] + lattice[2][0], lattice[0][1] + lattice[2][1], lattice[0][2] + lattice[2][2]],
    [lattice[1][0] + lattice[2][0], lattice[1][1] + lattice[2][1], lattice[1][2] + lattice[2][2]],
    [
      lattice[0][0] + lattice[1][0] + lattice[2][0],
      lattice[0][1] + lattice[1][1] + lattice[2][1],
      lattice[0][2] + lattice[1][2] + lattice[2][2],
    ],
  ];
  const center = corners[7].map((value) => value / 2);
  const points = corners.concat(atoms.map((atom) => atom.cartesian));
  const centered = points.map((point) => point.map((value, idx) => value - center[idx]));
  const maxSpan = Math.max(
    1,
    ...centered.flat().map((value) => Math.abs(value)),
  );
  const scale = (Math.min(width, height) * 0.33 * viewerState.zoom) / maxSpan;

  const project = (point) => {
    const [x, y, z] = rotatePoint(point);
    return { x: width / 2 + x * scale, y: height / 2 - y * scale, z };
  };

  const projectedCorners = centered.slice(0, 8).map(project);
  const projectedAtoms = atoms.map((atom, index) => {
    const p = project(centered[8 + index]);
    return { ...p, element: atom.element, radius: elementRadius(atom.element) * scale, color: elementColor(atom.element) };
  });

  const edges = [
    [0, 1], [0, 2], [0, 3],
    [1, 4], [1, 5], [2, 4],
    [2, 6], [3, 5], [3, 6],
    [4, 7], [5, 7], [6, 7],
  ];
  ctx.strokeStyle = "rgba(54, 39, 21, 0.55)";
  ctx.lineWidth = 1.2;
  edges.forEach(([a, b]) => {
    ctx.beginPath();
    ctx.moveTo(projectedCorners[a].x, projectedCorners[a].y);
    ctx.lineTo(projectedCorners[b].x, projectedCorners[b].y);
    ctx.stroke();
  });

  projectedAtoms
    .sort((a, b) => a.z - b.z)
    .forEach((atom) => {
      ctx.beginPath();
      ctx.fillStyle = atom.color;
      ctx.arc(atom.x, atom.y, Math.max(4, atom.radius), 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(20, 16, 12, 0.35)";
      ctx.stroke();
    });

  const uniqueElements = [...new Set(atoms.map((atom) => atom.element))];
  uniqueElements.forEach((element, index) => {
    const x = 18;
    const y = height - 18 - index * 24;
    ctx.beginPath();
    ctx.fillStyle = elementColor(element);
    ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#3d352b";
    ctx.font = "13px IBM Plex Sans";
    ctx.fillText(element, x + 16, y + 4);
  });
}

function initViewer() {
  if (viewerState.initialized) {
    return;
  }
  const canvas = document.getElementById("structure-canvas");
  canvas.addEventListener("mousedown", (event) => {
    viewerState.dragging = true;
    viewerState.lastX = event.clientX;
    viewerState.lastY = event.clientY;
  });
  window.addEventListener("mouseup", () => {
    viewerState.dragging = false;
  });
  window.addEventListener("mousemove", (event) => {
    if (!viewerState.dragging || !state.selectedSystemDetail?.structure) {
      return;
    }
    viewerState.rotY += (event.clientX - viewerState.lastX) * 0.01;
    viewerState.rotX += (event.clientY - viewerState.lastY) * 0.01;
    viewerState.lastX = event.clientX;
    viewerState.lastY = event.clientY;
    renderStructure(state.selectedSystemDetail.structure);
  });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    viewerState.zoom *= event.deltaY < 0 ? 1.08 : 0.92;
    viewerState.zoom = Math.max(0.35, Math.min(3.2, viewerState.zoom));
    if (state.selectedSystemDetail?.structure) {
      renderStructure(state.selectedSystemDetail.structure);
    }
  });
  window.addEventListener("resize", () => {
    if (state.selectedSystemDetail?.structure) {
      renderStructure(state.selectedSystemDetail.structure);
    }
  });
  viewerState.initialized = true;
}

async function selectSystem(systemName) {
  const previous = {
    system: state.selectedSystem,
    detail: state.selectedSystemDetail,
    filePath: state.selectedFilePath,
    fileMeta: state.selectedFileMeta,
    jobId: state.selectedJobId,
    jobContext: state.selectedJobResultContext,
  };
  const switchedSystems = previous.system !== systemName;
  state.selectedSystem = systemName;
  if (switchedSystems) {
    state.selectedSystemDetail = null;
    state.selectedJobId = null;
    state.selectedJobResultContext = null;
    state.fileCatalog = {};
    clearSelectedFileSelection(`Loading ${systemName} input files...`);
    setMaterialFormStatus(`Loading material parameters for ${systemName}...`);
    setPhononNacStatus(`Loading phonon NAC controls for ${systemName}...`);
    document.getElementById("submit-status").textContent = `Loading ${systemName}...`;
  }
  renderSystems();
  const detail = await refreshSelectedSystemDetail();
  if (detail || !switchedSystems) {
    return detail;
  }

  state.selectedSystem = previous.system;
  state.selectedSystemDetail = previous.detail;
  state.selectedFilePath = previous.filePath;
  state.selectedFileMeta = previous.fileMeta;
  state.selectedJobId = previous.jobId;
  state.selectedJobResultContext = previous.jobContext;
  renderSystems();

  if (previous.detail) {
    await applySelectedSystemDetail(previous.detail);
    const restoredSystem = previous.system || "the previous system";
    setFileStatus(`Failed to load ${systemName}. Restored ${restoredSystem}.`, true);
    setMaterialFormStatus(`Failed to load ${systemName}. Restored ${restoredSystem}.`, true);
    setPhononNacStatus(`Failed to load ${systemName}. Restored ${restoredSystem}.`, true);
    document.getElementById("submit-status").textContent = `Failed to load ${systemName}. Restored ${restoredSystem}.`;
    return previous.detail;
  }

  clearSelectedFileSelection("No editable input file selected.");
  setMaterialFormStatus("Select a system to load its material parameters.");
  setPhononNacStatus("Select a system to load its phonon NAC controls.");
  document.getElementById("submit-status").textContent = `Failed to load ${systemName}.`;
  return null;
}

async function refreshSelectedSystemDetail() {
  if (!state.selectedSystem) {
    return null;
  }
  const systemName = state.selectedSystem;
  if (refreshSystemDetailPromise && refreshSystemDetailTarget === systemName) {
    return refreshSystemDetailPromise;
  }

  const requestId = ++state.systemDetailRequestId;
  const promise = (async () => {
    try {
      const detail = await loadSelectedSystemDetail(systemName);
      if (requestId !== state.systemDetailRequestId) {
        return null;
      }
      if (detail) {
        await applySelectedSystemDetail(detail);
      }
      return detail;
    } catch (error) {
      console.error(error);
      if (requestId === state.systemDetailRequestId) {
        return null;
      }
      return null;
    }
  })();

  refreshSystemDetailPromise = promise;
  refreshSystemDetailTarget = systemName;

  try {
    return await promise;
  } finally {
    if (refreshSystemDetailPromise === promise) {
      refreshSystemDetailPromise = null;
      refreshSystemDetailTarget = null;
    }
  }
}

function syncDeleteProjectControls() {
  const button = document.getElementById("delete-project");
  const status = document.getElementById("delete-project-status");
  if (!button || !status) {
    return;
  }
  if (!state.selectedSystem) {
    button.disabled = true;
    status.textContent = "Select a project to enable deletion. Running projects cannot be deleted.";
    status.classList.remove("error");
    return;
  }
  button.disabled = false;
  status.textContent = `Selected project: ${state.selectedSystem}. Delete removes the project folder and its job history.`;
  status.classList.remove("error");
}

function renderJobs() {
  const allJobs = state.jobs || [];
  const config = flowConfig();
  let clearedHistoricalSelection = false;
  const hasSystemScope = Boolean(state.selectedSystem);
  let scopedJobs = allJobs;
  if (hasSystemScope) {
    scopedJobs = scopedJobs.filter((job) => job.system === state.selectedSystem);
  }
  if (config.step) {
    scopedJobs = scopedJobs.filter((job) => job.step === config.step);
  }
  if (state.selectedJobId && !scopedJobs.some((job) => job.id === state.selectedJobId)) {
    state.selectedJobId = null;
    state.selectedJobResultContext = null;
    clearedHistoricalSelection = true;
  }

  let visibleJobs = scopedJobs;
  let hiddenHistoryCount = 0;
  let latestGroupCount = scopedJobs.length;
  if (!state.showAllJobs) {
    const groups = new Map();
    scopedJobs.forEach((job) => {
      const key = `${job.system}::${job.step}`;
      const existing = groups.get(key);
      if (existing) {
        existing.historyCount += 1;
        hiddenHistoryCount += 1;
        return;
      }
      groups.set(key, {
        ...job,
        historyCount: 0,
      });
    });
    visibleJobs = Array.from(groups.values());
    latestGroupCount = visibleJobs.length;
  } else {
    visibleJobs = scopedJobs;
  }
  if (state.selectedJobId && !visibleJobs.some((job) => job.id === state.selectedJobId)) {
    state.selectedJobId = null;
    state.selectedJobResultContext = null;
    clearedHistoricalSelection = true;
  }

  const root = document.getElementById("jobs-list");
  root.innerHTML = visibleJobs.map((job) => `
    <button class="job-card ${state.selectedJobId === job.id ? "active" : ""}" data-job="${escapeHtml(job.id)}">
      <div class="panel-head">
        <strong>${escapeHtml(job.system)} | ${escapeHtml(job.step)}</strong>
        ${statusBadge(jobDisplayState(job))}
      </div>
      <div class="job-meta">
        <span class="mini-chip">${escapeHtml(job.target)}${job.profile ? ` | ${escapeHtml(job.profile)}` : ""}</span>
        <span class="mini-chip">${escapeHtml(job.backend || "legacy")}</span>
        <span class="mini-chip">${escapeHtml(jobTimestamp(job))}</span>
        ${job.result_ready && jobTerminal(job) ? `<span class="mini-chip recommended">result files</span>` : ""}
        ${job.historyCount ? `<span class="mini-chip">${escapeHtml(job.historyCount)} older</span>` : ""}
      </div>
      ${job.status_summary ? `<div class="small-label">${escapeHtml(job.status_summary)}</div>` : ""}
    </button>
  `).join("");

  if (!visibleJobs.length) {
    root.innerHTML = `<div class="metric"><div class="metric-label">${flowConfig().label}</div><div class="metric-value">No submissions in this workflow view yet.</div></div>`;
  }

  root.querySelectorAll("[data-job]").forEach((node) => {
    node.addEventListener("click", () => selectJob(node.dataset.job));
  });

  const summary = document.getElementById("jobs-summary");
  const cleanupButton = document.getElementById("cleanup-jobs");
  const cleanupCandidates = scopedJobs.filter((job) => Boolean(job.can_delete)).length;
  if (cleanupButton) {
    cleanupButton.disabled = !hasSystemScope || cleanupCandidates <= 1;
  }
  if (state.jobsPanelCollapsed) {
    summary.textContent = hasSystemScope
      ? `Jobs hidden. ${scopedJobs.length} recorded for this view.`
      : `Jobs hidden. ${scopedJobs.length} recorded for this workflow across all systems.`;
  } else if (!scopedJobs.length) {
    summary.textContent = hasSystemScope
      ? "No jobs loaded for this workflow view yet."
      : "No jobs loaded for this workflow across any system yet.";
  } else if (!hasSystemScope) {
    if (!state.showAllJobs) {
      summary.textContent = `No project selected. Showing latest ${latestGroupCount} job group(s) for ${flowConfig().label} across all systems. ${hiddenHistoryCount} older record(s) hidden.`;
    } else {
      summary.textContent = `No project selected. Showing all ${scopedJobs.length} ${flowConfig().label} jobs across all systems. Select a project to scope this panel.`;
    }
  } else if (!state.showAllJobs) {
    summary.textContent = `Showing latest ${latestGroupCount} job group(s) for ${flowConfig().label}. ${hiddenHistoryCount} older record(s) hidden.`;
  } else if (state.showAllJobs || scopedJobs.length <= JOB_PREVIEW_COUNT) {
    summary.textContent = `Showing all ${scopedJobs.length} jobs for ${flowConfig().label}. Scroll inside this panel to browse them.`;
  } else {
    summary.textContent = `Showing latest ${visibleJobs.length} of ${scopedJobs.length} ${flowConfig().label} jobs. Use Show All for the full history.`;
  }

  document.getElementById("jobs-region").classList.toggle("is-collapsed", state.jobsPanelCollapsed);
  document.getElementById("toggle-jobs-view").textContent =
    state.showAllJobs ? "Show Latest" : "Show All";
  document.getElementById("toggle-jobs-panel").textContent =
    state.jobsPanelCollapsed ? "Expand" : "Collapse";
  if (!visibleJobs.length && !state.selectedJobId) {
    document.getElementById("job-log-label").textContent = `${flowConfig().label} | none`;
    document.getElementById("job-log").textContent = "Select or submit a job in this workflow stage to inspect its runtime log here.";
  }
  if (!state.selectedJobId) {
    setJobLogNote();
  }

  renderJobControls();
  if (clearedHistoricalSelection && state.selectedSystem) {
    queueMicrotask(() => {
      void refreshSelectedSystemDetail();
    });
  }
}

function selectedJobRecord() {
  return (state.jobs || []).find((job) => job.id === state.selectedJobId) || null;
}

function setJobControlStatus(message, tone = "") {
  const node = document.getElementById("job-control-status");
  if (!node) {
    return;
  }
  node.textContent = message;
  node.className = `small-label${tone ? ` ${tone}` : ""}`;
}

function renderJobControls() {
  const pauseButton = document.getElementById("job-pause");
  const resumeButton = document.getElementById("job-resume");
  const stopButton = document.getElementById("job-stop");
  const deleteButton = document.getElementById("job-delete");
  if (!pauseButton || !resumeButton || !stopButton || !deleteButton) {
    return;
  }

  const job = selectedJobRecord();
  if (!job) {
    pauseButton.disabled = true;
    resumeButton.disabled = true;
    stopButton.disabled = true;
    deleteButton.disabled = true;
    setJobControlStatus("Select a job to control it.");
    return;
  }

  const actions = new Set(job.supported_actions || []);
  pauseButton.disabled = !actions.has("pause");
  resumeButton.disabled = !actions.has("resume");
  stopButton.disabled = !actions.has("stop");
  deleteButton.disabled = !job.can_delete;

  let message = `${job.id} | ${jobDisplayState(job)}`;
  if (job.control_state === "pause_requested") {
    message += " | graceful pause requested";
  } else if (job.control_state === "stop_requested") {
    message += " | stop requested";
  }
  if (job.resumed_by) {
    message += ` | resumed by ${job.resumed_by}`;
  }
  if (job.status_summary) {
    message += ` | ${job.status_summary}`;
  }
  if (["finished", "failed", "paused", "stopped"].includes(job.state)) {
    message += " | process is no longer running; the console below is archived output";
  }
  const tone = ["failed", "stopped"].includes(job.state) ? "error" : "";
  setJobControlStatus(message, tone);
}

async function controlSelectedJob(action) {
  const job = selectedJobRecord();
  if (!job) {
    setJobControlStatus("Select a job to control it.", "error");
    return;
  }

  const label = action.charAt(0).toUpperCase() + action.slice(1);
  setJobControlStatus(`${label} requested for ${job.id}...`);

  try {
    const payload = await fetchJSON(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, {
      method: "POST",
    });

    if (action === "resume") {
      state.selectedJobId = payload.id;
    }

    await refreshJobs();
    if (action === "resume") {
      await selectJob(payload.id);
    } else {
      await selectJob(job.id);
    }
    setJobControlStatus(`${label} completed for ${action === "resume" ? payload.id : job.id}.`);
  } catch (error) {
    console.error(error);
    setJobControlStatus(`Unable to ${action} ${job.id}: ${error.message}`, "error");
  }
}

async function handleJobStop() {
  if (!state.selectedJobId) {
    return;
  }
  if (!window.confirm("Are you sure you want to stop this job? This may discard unsaved results.")) {
    return;
  }
  await controlSelectedJob("stop");
}

async function deleteSelectedJob() {
  const job = selectedJobRecord();
  if (!job) {
    return;
  }
  if (!job.can_delete) {
    setJobControlStatus(`Job ${job.id} is still active and cannot be deleted.`, "error");
    return;
  }
  if (!window.confirm(`Delete record ${job.id}? This removes only the UI history entry and its launcher log, not runs/${job.step}.`)) {
    return;
  }
  try {
    await fetchJSON(`/api/jobs/${encodeURIComponent(job.id)}`, { method: "DELETE" });
    state.selectedJobId = null;
    await refreshJobs();
    setJobControlStatus(`Deleted record ${job.id}.`);
  } catch (error) {
    console.error(error);
    setJobControlStatus(`Unable to delete ${job.id}: ${error.message}`, "error");
  }
}

async function cleanupJobsHistory() {
  if (!state.selectedSystem) {
    alert("Select a project first.");
    return;
  }
  const config = flowConfig();
  const stepScope = config.step || null;
  const scopeLabel = stepScope ? `${state.selectedSystem} ${stepScope}` : `${state.selectedSystem}`;
  const selectedJob = selectedJobRecord();
  const selectedScopedTerminalJob =
    selectedJob &&
    selectedJob.system === state.selectedSystem &&
    (!stepScope || selectedJob.step === stepScope) &&
    selectedJob.is_terminal;
  const confirmMessage = selectedScopedTerminalJob
    ? `Clean old ended job records for ${scopeLabel}? The selected ended record ${selectedJob.id} will be removed first when possible, while the newest other ended record per step is kept. This does not delete runs/* outputs.`
    : `Clean old ended job records for ${scopeLabel}? The newest ended record per step will be kept. This does not delete runs/* outputs.`;
  if (!window.confirm(confirmMessage)) {
    return;
  }
  try {
    const payload = await fetchJSON("/api/jobs/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        step: stepScope,
        keep_latest: 1,
        selected_job_id: selectedScopedTerminalJob ? selectedJob.id : null,
      }),
    });
    if (payload.removed_count && state.selectedJobId && payload.removed_job_ids?.includes(state.selectedJobId)) {
      state.selectedJobId = null;
    }
    await refreshJobs();
    if (!payload.removed_count) {
      setJobControlStatus("No old job records were removed.");
      return;
    }
    const statusMessage = payload.selected_job_removed
      ? `Removed ${payload.removed_count} old job record(s), including selected ${selectedJob.id}.`
      : `Removed ${payload.removed_count} old job record(s).`;
    setJobControlStatus(statusMessage);
  } catch (error) {
    console.error(error);
    setJobControlStatus(`Unable to clean old records: ${error.message}`, "error");
  }
}

async function updateSelectedJobLog(jobId, { showLoading = false, refreshDetail = false, rerender = false } = {}) {
  const requestId = ++state.jobSelectionRequestId;
  if (state.selectedJobId !== jobId) {
    state.selectedJobId = jobId;
    rerender = true;
  }
  if (rerender) {
    renderJobs();
  }
  if (showLoading) {
    document.getElementById("job-log-label").textContent = `${jobId} | loading`;
    document.getElementById("job-log").textContent = "Loading job log...";
  }
  try {
    const tasks = [fetchJSON(`/api/jobs/${encodeURIComponent(jobId)}/log`)];
    if (refreshDetail) {
      tasks.push(refreshSelectedSystemDetail());
    }
    const [payload] = await Promise.all(tasks);
    if (requestId !== state.jobSelectionRequestId || state.selectedJobId !== jobId) {
      return;
    }
    document.getElementById("job-log-label").textContent = `${payload.job_id} | ${payload.display_state || payload.state}`;
    setJobLogNote(payload.log_notice || "");
    const terminalStates = new Set(["finished", "failed", "paused", "stopped"]);
    const prefix = terminalStates.has(payload.state)
      ? `[${payload.state}] Process already exited. The text below is the last captured log, not a live running console.\n\n`
      : "";
    const summary = payload.status_summary ? `${payload.status_summary}\n\n` : "";
    document.getElementById("job-log").textContent = `${summary}${prefix}${payload.content || ""}`;
    renderJobControls();
  } catch (error) {
    if (requestId !== state.jobSelectionRequestId || state.selectedJobId !== jobId) {
      return;
    }
    console.error(error);
    document.getElementById("job-log-label").textContent = `${jobId} | error`;
    document.getElementById("job-log").textContent = `Unable to load job log: ${error.message}`;
    setJobControlStatus(`Unable to load ${jobId}: ${error.message}`, "error");
  }
}

async function selectJob(jobId) {
  const job = (state.jobs || []).find((item) => item.id === jobId) || null;
  if (job?.system && job.system !== state.selectedSystem) {
    await selectSystem(job.system);
  }
  await updateSelectedJobLog(jobId, {
    showLoading: true,
    refreshDetail: true,
    rerender: true,
  });
}

async function refreshJobs() {
  if (refreshJobsPromise) {
    refreshJobsQueued = true;
    return refreshJobsPromise;
  }

  const promise = (async () => {
    const previousJobsSignature = jobsListSignature(state.jobs || []);
    const previousSignature = selectedSystemJobsSignature(state.jobs || []);
    state.jobs = await fetchJSON("/api/jobs");
    const nextJobsSignature = jobsListSignature(state.jobs || []);
    if (nextJobsSignature !== previousJobsSignature) {
      renderJobs();
    }
    const nextSignature = selectedSystemJobsSignature(state.jobs || []);
    if (state.selectedSystem && nextSignature !== previousSignature) {
      await refreshSelectedSystemDetail();
    }
    if (state.selectedJobId) {
      const active = state.jobs.find((item) => item.id === state.selectedJobId);
      if (active) {
        await updateSelectedJobLog(state.selectedJobId, {
          showLoading: false,
          refreshDetail: false,
          rerender: false,
        });
      }
    }
  })();

  refreshJobsPromise = promise;

  try {
    return await promise;
  } finally {
    if (refreshJobsPromise === promise) {
      refreshJobsPromise = null;
    }
    if (refreshJobsQueued) {
      refreshJobsQueued = false;
      queueMicrotask(() => {
        void refreshJobs();
      });
    }
  }
}

function renderProfiles() {
  const submitTarget = document.getElementById("submit-target");
  const templateSelect = document.getElementById("new-project-template");
  const profileList = document.getElementById("profile-list");

  submitTarget.innerHTML = state.profiles.map((profile) => `
    <option value="${escapeHtml(profile.name)}">${escapeHtml(profile.name)}${profile.kind === "remote" ? ` | ${escapeHtml(profile.host)}` : ""}</option>
  `).join("");

  templateSelect.innerHTML = [
    `<option value="__blank__">Blank Project (no template)</option>`,
    ...state.systems.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`),
  ].join("");

  profileList.innerHTML = state.profiles.map((profile) => `
    <div class="job-card">
      <div class="panel-head">
        <strong>${escapeHtml(profile.name)}</strong>
        ${statusBadge(profile.kind)}
      </div>
      <div class="job-meta">
        <span class="mini-chip">${profile.kind === "local" ? "local workspace" : `${profile.user ? `${escapeHtml(profile.user)}@` : ""}${escapeHtml(profile.host)}`}</span>
        <span class="mini-chip">${escapeHtml(profile.scheduler_kind || profile.kind)}</span>
        <span class="mini-chip">${escapeHtml(profile.workspace_root)}</span>
        ${profile.queue_name ? `<span class="mini-chip">${escapeHtml(profile.queue_name)}</span>` : ""}
        ${profile.walltime ? `<span class="mini-chip">${escapeHtml(profile.walltime)}</span>` : ""}
      </div>
    </div>
  `).join("");
}

function renderSubmitWarnings(warnings = [], riskLevel = "ok") {
  const root = document.getElementById("submit-warnings");
  if (!warnings.length) {
    root.className = "submit-warning-box hidden";
    root.innerHTML = "";
    return;
  }
  root.className = `submit-warning-box risk-${riskLevel || "review"}`;
  root.innerHTML = `
    <strong>${riskLevel === "high" ? "High-risk review" : "Pre-submit review"}</strong>
    <ul>
      ${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}
    </ul>
  `;
}

function renderSubmitReview(payload) {
  const warnings = payload?.warnings || [];
  const blockers = payload?.blocking_errors || [];
  const riskLevel = blockers.length ? "high" : payload?.risk_level || "ok";
  const root = document.getElementById("submit-warnings");
  if (!warnings.length && !blockers.length) {
    root.className = "submit-warning-box hidden";
    root.innerHTML = "";
    return;
  }
  root.className = `submit-warning-box risk-${riskLevel}`;
  root.innerHTML = `
    <strong>${blockers.length ? "Submission blocked" : riskLevel === "high" ? "High-risk review" : "Pre-submit review"}</strong>
    ${blockers.length ? `<div class="small-label">Resolve these blockers before submission:</div><ul>${blockers.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
    ${warnings.length ? `<div class="small-label">Warnings:</div><ul>${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
  `;
}

function detectStructureFormat(name, content) {
  const lowered = String(name || "").toLowerCase();
  const text = String(content || "").trim();
  if (lowered.endsWith(".cif")) {
    return "cif";
  }
  if (
    text.includes("_cell_length_a")
    || text.includes("_symmetry_space_group_name_h-m")
    || /^\s*data_/im.test(text)
    || /^\s*loop_/im.test(text)
  ) {
    return "cif";
  }
  return "poscar";
}

function suggestProjectName(name) {
  return String(name || "")
    .replace(/\.[^.]+$/, "")
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function updateProjectWizardState() {
  const name = document.getElementById("new-project-name").value.trim();
  const structureText = document.getElementById("new-project-poscar").value.trim();
  const materialClass = document.getElementById("new-project-material-class").value;
  const step1 = document.getElementById("wizard-step-1");
  const step2 = document.getElementById("wizard-step-2");
  const step3 = document.getElementById("wizard-step-3");

  step1.className = `wizard-step ${name && materialClass ? "done" : "active"}`;
  step2.className = `wizard-step ${structureText ? "done" : name && materialClass ? "active" : ""}`.trim();
  step3.className = `wizard-step ${name ? "active" : ""}`.trim();
}

function loadImportedStructure(content, sourceName = "imported structure") {
  const format = detectStructureFormat(sourceName, content);
  document.getElementById("new-project-poscar").value = content;
  document.getElementById("new-project-structure-format").value = format;
  if (!document.getElementById("new-project-name").value.trim()) {
    const suggested = suggestProjectName(sourceName);
    if (suggested) {
      document.getElementById("new-project-name").value = suggested;
    }
  }
  document.getElementById("new-project-structure-hint").textContent =
    `Loaded ${sourceName}. Detected format: ${format.toUpperCase()}.`;
  document.getElementById("create-project-status").textContent =
    `Imported ${sourceName}. Pick the material type, then create the project.`;
  updateProjectWizardState();
}

async function readImportedFile(file) {
  const text = await file.text();
  loadImportedStructure(text, file.name);
}

function attachProjectImportHandlers() {
  const dropzone = document.getElementById("new-project-dropzone");
  const fileInput = document.getElementById("new-project-file");
  const browse = document.getElementById("new-project-browse");
  const clear = document.getElementById("new-project-clear");
  const textarea = document.getElementById("new-project-poscar");

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "dragend", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });

  dropzone.addEventListener("drop", async (event) => {
    const [file] = Array.from(event.dataTransfer?.files || []);
    if (!file) {
      return;
    }
    await readImportedFile(file);
  });

  browse.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const [file] = Array.from(fileInput.files || []);
    if (!file) {
      return;
    }
    await readImportedFile(file);
    fileInput.value = "";
  });
  clear.addEventListener("click", () => {
    textarea.value = "";
    document.getElementById("new-project-structure-hint").textContent =
      "No imported structure loaded yet. Import or paste a POSCAR/CIF for a new project, or choose an existing system to fork its settings.";
    updateProjectWizardState();
  });
  textarea.addEventListener("input", updateProjectWizardState);
  document.getElementById("new-project-name").addEventListener("input", updateProjectWizardState);
  document.getElementById("new-project-template").addEventListener("change", updateProjectWizardState);
  document.getElementById("new-project-material-class").addEventListener("change", updateProjectWizardState);
  document.getElementById("new-project-material-class").addEventListener("change", (e) => {
    if (["slab", "2d"].includes(e.target.value)) {
      alert("Note: For slab/2d environments, ensure sufficient vacuum in the POSCAR and consider enabling dipole corrections (LDIPOL=True) if polar.");
    }
  });
  document.getElementById("new-project-material-class").addEventListener("change", (event) => {
    document.getElementById("create-project-status").textContent =
      MATERIAL_CLASS_HELP[event.target.value] || MATERIAL_CLASS_HELP.bulk;
  });
}

async function refreshSystemsAndProfiles() {
  state.systems = await fetchJSON("/api/systems");
  state.profiles = await fetchJSON("/api/submission-profiles");
  renderSystems();
  renderProfiles();
}

async function refreshBackendStatus() {
  const payload = await fetchJSON("/api/backend/status");
  renderBackendStatus(payload);
}

function expertSubmitConfirmationMessage(preview) {
  const summary = [
    `Submit ${plainStepLabel(preview.step)} for ${preview.system}?`,
    `Target: ${preview.target}`,
    `MPI NP: ${preview.mpi_np || 1}`,
  ];
  if (preview.structure_input) {
    summary.push(`Structure: ${preview.structure_input}`);
  }
  if (preview.mesh_kpoints_input) {
    summary.push(`Mesh KPOINTS: ${preview.mesh_kpoints_input}`);
  }
  if (preview.kpoints_opt_input) {
    summary.push(`KPOINTS_OPT source: ${preview.kpoints_opt_input}`);
  }
  if (preview.warnings?.length) {
    summary.push(`Warnings: ${preview.warnings.length}`);
  }
  summary.push("Review the generated inputs one more time before you continue.");
  return summary.join("\n");
}

async function submitJob() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    document.getElementById("submit-status").textContent = selectedSystemActionMessage("submitting a job");
    return;
  }
  try {
    const preview = await fetchJSON("/api/jobs/validate-submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        step: document.getElementById("submit-step").value,
        target: document.getElementById("submit-target").value,
        mpi_np: Number(document.getElementById("submit-mpi").value || 1),
      }),
    });
    renderSubmitReview(preview);
    if (preview.blocking_errors?.length) {
      document.getElementById("submit-status").textContent =
        `Submission blocked: ${preview.blocking_errors.length} issue(s) must be resolved before running ${preview.step}.`;
      return;
    }
    if (preview.warnings?.length) {
      const confirmed = window.confirm(
        `This submission has ${preview.warnings.length} warning(s). Review the warning box and click OK to continue.`
      );
      if (!confirmed) {
        document.getElementById("submit-status").textContent = "Submission cancelled after warning review.";
        return;
      }
    }
    if (!state.beginnerMode) {
      const confirmed = window.confirm(expertSubmitConfirmationMessage(preview));
      if (!confirmed) {
        document.getElementById("submit-status").textContent = "Submission cancelled in Expert Override.";
        return;
      }
    }
    const payload = await fetchJSON("/api/jobs/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        step: document.getElementById("submit-step").value,
        target: document.getElementById("submit-target").value,
        mpi_np: Number(document.getElementById("submit-mpi").value || 1),
      }),
    });
    document.getElementById("submit-status").textContent =
      `Submitted ${payload.system} ${payload.step} to ${payload.target} via ${payload.backend || payload.submission_mode || "legacy"}.`;
    renderSubmitReview(payload);
    state.selectedJobId = payload.id;
    await refreshJobs();
    await selectJob(payload.id);
  } catch (error) {
    console.error(error);
    document.getElementById("submit-status").textContent = `Submission failed: ${error.message}`;
  }
}

async function previewJob() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    document.getElementById("submit-status").textContent = selectedSystemActionMessage("previewing a submission");
    return;
  }
  try {
    const payload = await fetchJSON("/api/jobs/validate-submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system: state.selectedSystem,
        step: document.getElementById("submit-step").value,
        target: document.getElementById("submit-target").value,
        mpi_np: Number(document.getElementById("submit-mpi").value || 1),
      }),
    });
    renderSubmitReview(payload);
    const warningText = payload.warnings?.length ? ` warnings=${payload.warnings.length}` : "";
    const blockerText = payload.blocking_errors?.length ? ` blockers=${payload.blocking_errors.length}` : "";
    const structureText = payload.structure_input ? ` structure=${payload.structure_input}` : "";
    const meshText = payload.mesh_kpoints_input ? ` mesh=${payload.mesh_kpoints_input}` : "";
    const optText = payload.kpoints_opt_input ? ` kpoints_opt=${payload.kpoints_opt_input}` : "";
    document.getElementById("submit-status").textContent =
      `Preview ${payload.readiness || "ready"}: ${payload.system} ${payload.step} -> ${payload.target} (${payload.target_kind}, backend=${payload.submit_backend || payload.submission_mode || "legacy"}, np=${payload.mpi_np}, risk=${payload.risk_level || "ok"}).${structureText}${meshText}${optText}${warningText}${blockerText}`;
  } catch (error) {
    console.error(error);
    document.getElementById("submit-status").textContent = `Preview failed: ${error.message}`;
  }
}

async function createProject() {
  const name = document.getElementById("new-project-name").value.trim();
  const sourceSystem = document.getElementById("new-project-template").value;
  const materialClass = document.getElementById("new-project-material-class").value;
  const structureContent = document.getElementById("new-project-poscar").value;
  const structureFormat = document.getElementById("new-project-structure-format").value;
  const creationMode = sourceSystem && sourceSystem !== "__blank__"
    ? "fork_settings"
    : "new_from_structure";
  if (!name) {
    alert("Enter a project name.");
    return;
  }
  if (creationMode === "new_from_structure" && !String(structureContent || "").trim()) {
    alert("Import or paste a POSCAR/CIF structure first.");
    return;
  }
  const status = document.getElementById("create-project-status");
  status.classList.remove("error");
  status.textContent = creationMode === "new_from_structure"
    ? `Creating project ${name} as ${materialClass} from the imported structure...`
    : `Creating project ${name} by forking settings from ${sourceSystem}...`;
  try {
    const detail = await fetchJSON("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeoutMs: LONG_TASK_TIMEOUT_MS,
      body: JSON.stringify({
        name,
        creation_mode: creationMode,
        source_system: sourceSystem,
        material_class: materialClass,
        structure_content: structureContent,
        structure_format: structureFormat,
      }),
    });
    status.textContent = `Created project ${detail.name}. Initial defaults were seeded for ${materialClass}.`;
    document.getElementById("new-project-name").value = "";
    document.getElementById("new-project-template").value = "__blank__";
    document.getElementById("new-project-poscar").value = "";
    document.getElementById("new-project-structure-hint").textContent =
      "No imported structure loaded yet. Import or paste a POSCAR/CIF for a new project, or choose an existing system to fork its settings.";
    updateProjectWizardState();
    await refreshSystemsAndProfiles();
    await selectSystem(detail.name);
    switchView("workbench");
    setFlowView("setup");
  } catch (error) {
    status.classList.add("error");
    status.textContent = `Create project failed: ${error.message}`;
  }
}

async function deleteSelectedProject() {
  if (!state.selectedSystem) {
    return;
  }
  const systemName = state.selectedSystem;
  const status = document.getElementById("delete-project-status");
  if (!selectedSystemReady()) {
    status.classList.add("error");
    status.textContent = selectedSystemActionMessage("deleting the selected project");
    return;
  }
  const confirmed = window.confirm(
    `Delete project ${systemName}? This removes its project folder and job history. This cannot be undone.`
  );
  if (!confirmed) {
    status.classList.remove("error");
    status.textContent = `Deletion cancelled for ${systemName}.`;
    return;
  }
  try {
    const payload = await fetchJSON(`/api/projects/${encodeURIComponent(systemName)}`, {
      method: "DELETE",
    });
    status.classList.remove("error");
    status.textContent = `Deleted ${payload.deleted_system}. Removed ${payload.removed_job_count} related job record(s).`;
    state.selectedSystem = null;
    state.selectedSystemDetail = null;
    state.selectedJobId = null;
    await refreshSystemsAndProfiles();
    await refreshJobs();
    if (state.systems.length) {
      await selectSystem(state.systems[0].name);
    } else {
      window.location.reload();
    }
  } catch (error) {
    status.classList.add("error");
    status.textContent = `Delete failed: ${error.message}`;
  }
}

async function handleDeleteProject() {
  return deleteSelectedProject();
}

async function saveMaterialSettings() {
  if (!state.selectedSystem) {
    alert("Select a system first.");
    return;
  }
  if (!selectedSystemReady()) {
    setMaterialFormStatus(selectedSystemActionMessage("saving material parameters"), true);
    return;
  }
  try {
    const bandDistanceInput = document.getElementById("material-band-distance");
    const payload = await fetchJSON("/api/material-settings/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeoutMs: LONG_TASK_TIMEOUT_MS,
      body: JSON.stringify({
        system: state.selectedSystem,
        formula: document.getElementById("material-formula").value.trim(),
        material_class: document.getElementById("material-class").value,
        electronic_type: document.getElementById("material-electronic-type").value,
        xc_geometry: document.getElementById("material-xc-geometry").value,
        xc_electronic: document.getElementById("material-xc-electronic").value,
        spin_polarized: document.getElementById("material-spin-polarized").checked,
        encut: Number(document.getElementById("material-encut").value || 520),
        potcar_family: document.getElementById("material-potcar-family").value,
        potcar_profile: document.getElementById("material-potcar-profile").value,
        potcar_mapping_text: currentPotcarMappingText(),
        kmesh_text: document.getElementById("material-kmesh").value,
        dos_kmesh_text: document.getElementById("material-dos-kmesh").value,
        phonon_kmesh_text: document.getElementById("material-phonon-kmesh").value,
        phonon_dos_kmesh_text: document.getElementById("material-phonon-dos-kmesh").value,
        phonon_supercell_text: document.getElementById("material-phonon-supercell").value,
        magmom_text: document.getElementById("material-magmom").value,
        band_points: Number(document.getElementById("material-band-points").value || 101),
        ...(bandDistanceInput ? { band_kpoints_distance: Number(bandDistanceInput.value || 0.05) } : {}),
        band_path_mode: document.getElementById("material-band-path-mode")?.value || "auto",
        band_path_text: document.getElementById("material-band-path-text")?.value || "",
        wallclock_seconds: Number(document.getElementById("material-wallclock").value || 43200),
        advanced_overrides_json: document.getElementById("material-overrides").value,
      }),
    });
    await applySelectedSystemDetail(payload.detail);
    switchView("workbench");
    setFlowView("setup");
    const potcarNote = payload.potcar_path
      ? " Refreshed POTCAR from the configured local archive."
      : " POTCAR was not regenerated because no local POTCAR archive is configured.";
    setMaterialFormStatus(
      `Saved parameters at ${payload.saved_at}. Regenerated ${payload.generated_files.length} derived files.${potcarNote} Review the full generated input text below before submitting jobs.`
    );
    document.getElementById("input-review-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setMaterialFormStatus(`Save failed: ${error.message}`, true);
  }
}

async function refreshMaterialSettings() {
  if (!state.selectedSystem) {
    setMaterialFormStatus("Select a system first.", true);
    return;
  }
  if (!selectedSystemReady()) {
    setMaterialFormStatus(selectedSystemActionMessage("reloading material parameters"), true);
    return;
  }
  try {
    const payload = await fetchJSON(`/api/material-settings?system=${encodeURIComponent(state.selectedSystem)}`);
    renderMaterialSettings({
      ...(state.selectedSystemDetail || { name: state.selectedSystem }),
      material_settings: payload,
    });
  } catch (error) {
    setMaterialFormStatus(`Reload failed: ${error.message}`, true);
  }
}

async function saveProfile() {
  const payload = {
    name: document.getElementById("profile-name").value.trim(),
    host: document.getElementById("profile-host").value.trim(),
    user: document.getElementById("profile-user").value.trim(),
    workspace_root: document.getElementById("profile-root").value.trim(),
    scheduler_kind: document.getElementById("profile-scheduler").value,
    vasp_mpi_np: Number(document.getElementById("profile-mpi").value || 1),
    queue_name: document.getElementById("profile-queue").value.trim(),
    account: document.getElementById("profile-account").value.trim(),
    walltime: document.getElementById("profile-walltime").value.trim(),
    pre_command: document.getElementById("profile-pre").value.trim(),
    submit_options: document.getElementById("profile-submit-options").value.trim(),
  };
  if (!payload.name || !payload.host || !payload.workspace_root) {
    alert("Profile name, host, and remote workspace root are required.");
    return;
  }
  try {
    const record = await fetchJSON("/api/submission-profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("profile-status").textContent = `Saved remote profile ${record.name} (${record.scheduler_kind}).`;
    await refreshSystemsAndProfiles();
  } catch (error) {
    document.getElementById("profile-status").textContent = `Save failed: ${error.message}`;
  }
}

function switchView(viewName) {
  state.activeView = viewName;
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.viewTarget === viewName);
  });
  document.querySelectorAll(".page-view").forEach((section) => {
    section.classList.toggle("hidden", section.id !== `view-${viewName}`);
  });
}

function toggleJobsView() {
  state.showAllJobs = !state.showAllJobs;
  renderJobs();
}

function toggleJobsPanel() {
  state.jobsPanelCollapsed = !state.jobsPanelCollapsed;
  renderJobs();
}

async function bootstrap() {
  initViewer();
  try {
    state.beginnerMode = (localStorage.getItem("vasp-studio-mode") || "beginner") !== "advanced";
  } catch (error) {
    state.beginnerMode = true;
  }
  setModeNote();
  setIncarFormEnabled(false);
  renderSubmitWarnings([], "ok");
  const overview = await fetchJSON("/api/overview");
  setOverview(overview);
  const backendRefresh = refreshBackendStatus().catch((error) => {
    console.error(error);
  });

  await refreshSystemsAndProfiles();
  if (state.systems.length) {
    await selectSystem(state.systems[0].name);
  } else {
    renderStructure(null);
  }

  await refreshJobs();
  await backendRefresh;
  attachProjectImportHandlers();
  updateProjectWizardState();

  document.getElementById("refresh-systems").addEventListener("click", async () => {
    await refreshSystemsAndProfiles();
    if (state.selectedSystem) {
      await selectSystem(state.selectedSystem);
    }
  });
  document.getElementById("systems-search").addEventListener("input", (event) => {
    state.systemsQuery = event.target.value || "";
    renderSystems();
  });
  document.getElementById("systems-hide-utility").addEventListener("change", (event) => {
    state.hideUtilitySystems = Boolean(event.target.checked);
    renderSystems();
  });

  document.getElementById("refresh-jobs").addEventListener("click", refreshJobs);
  document.getElementById("toggle-jobs-view").addEventListener("click", toggleJobsView);
  document.getElementById("toggle-jobs-panel").addEventListener("click", toggleJobsPanel);
  document.getElementById("cleanup-jobs").addEventListener("click", cleanupJobsHistory);
  document.getElementById("mode-beginner").addEventListener("click", () => setBeginnerMode(true));
  document.getElementById("mode-advanced").addEventListener("click", () => setBeginnerMode(false));
  const addProjectBtn = document.getElementById("add-new-project");
  if (addProjectBtn) {
    addProjectBtn.addEventListener("click", () => switchView("project-lab"));
  }
  
  document.getElementById("back-to-workbench")?.addEventListener("click", () => switchView("workbench"));
  document.getElementById("beginner-open-project")?.addEventListener("click", () => {
    switchView("project-lab");
    document.getElementById("beginner-action-status").textContent = "Project Lab is open. Import your structure there.";
  });
  document.getElementById("beginner-open-setup")?.addEventListener("click", () => {
    switchView("workbench");
    setFlowView("setup");
    document.getElementById("beginner-action-status").textContent = "Setup is open. Review Material Parameters, then click Save Parameters if the defaults look reasonable.";
  });
  document.getElementById("beginner-run-next")?.addEventListener("click", runBeginnerAction);
  document.getElementById("job-pause").addEventListener("click", () => controlSelectedJob("pause"));
  document.getElementById("job-resume").addEventListener("click", () => controlSelectedJob("resume"));
  document.getElementById("job-stop").addEventListener("click", handleJobStop);
  document.getElementById("job-delete").addEventListener("click", deleteSelectedJob);
  document.getElementById("preview-job").addEventListener("click", previewJob);
  document.getElementById("submit-job").addEventListener("click", submitJob);
  document.getElementById("generate-pdos")?.addEventListener("click", generateElementPdos);
  document.getElementById("generate-primitive-cell")?.addEventListener("click", generatePrimitiveCell);
  document.getElementById("generate-file").addEventListener("click", generateSelectedFile);
  document.getElementById("reload-file").addEventListener("click", reloadSelectedFile);
  document.getElementById("save-file").addEventListener("click", saveSelectedFile);
  document.getElementById("incar-load").addEventListener("click", syncIncarFormFromEditor);
  document.getElementById("incar-apply").addEventListener("click", applyIncarFormToEditor);
  document.getElementById("phonon-nac-save").addEventListener("click", savePhononNacSettings);
  document.getElementById("phonon-nac-prepare-charge").addEventListener("click", prepareChargeForPhononNac);
  document.getElementById("phonon-nac-build-born").addEventListener("click", buildBornFromCharge);
  document.getElementById("create-project").addEventListener("click", createProject);
  document.getElementById("delete-project").addEventListener("click", deleteSelectedProject);
  document.getElementById("save-material-settings").addEventListener("click", saveMaterialSettings);
  document.getElementById("refresh-material-settings").addEventListener("click", refreshMaterialSettings);
  document.getElementById("material-potcar-profile").addEventListener("change", applyPotcarProfileRecommendations);
  document.getElementById("material-band-path-mode")?.addEventListener("change", syncMaterialBandPathMode);
  document.getElementById("save-profile").addEventListener("click", saveProfile);

  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.viewTarget));
  });

  document.querySelectorAll("[data-flow-target]").forEach((button) => {
    button.addEventListener("click", () => setFlowView(button.dataset.flowTarget));
  });

  switchView("workbench");
  setFlowView("overview");
  syncDeleteProjectControls();
  setInterval(() => {
    void refreshJobs();
  }, 7000);
}

bootstrap().catch((error) => {
  console.error(error);
  alert(`Failed to load VASP Input Studio: ${error.message}`);
});
