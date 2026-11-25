/* AWS Architecture Mapper — Interactive Pan/Zoom/Drag Architecture Studio */

const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  config: null,
  graph: null,
  layout: null,

  // Active AWS Scanner inputs
  availableRegions: [],
  selectedScanRegions: new Set(["us-east-1", "us-west-2"]),
  activeJobId: null,
  pollTimer: null,

  // Diagram Filter & Layer state
  hideEmptyDefaultVpcs: true,
  hideEmptySubnets: false,
  showEdges: true,
  showEdgeLabels: true,
  enabledEdgeKinds: new Set(["routing", "traffic", "event", "storage", "security_group"]),
  activeRegionsFilter: new Set(), // empty = all regions shown
  activeCategoriesFilter: new Set(), // empty = all categories shown
  searchQuery: "",

  // Interactive Canvas Camera & Node Dragging
  cam: { x: 40, y: 40, scale: 0.75 },
  selectedItem: null, // { kind: 'node' | 'box', id: string }
  hoveredNodeId: null,
  nodeOverrides: new Map(), // id -> { x, y }

  // DOM Element caches for fast 60fps drag & highlight updates
  nodeEls: new Map(),
  edgeEls: [],
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function $(id) {
  return document.getElementById(id);
}

function showToast(msg, duration = 2800) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), duration);
}

function escapeXml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function truncateText(str, maxLen) {
  const s = String(str ?? "");
  return s.length <= maxLen ? s : s.slice(0, maxLen - 1) + "…";
}

function createSvgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null && v !== "") {
      el.setAttribute(k, String(v));
    }
  }
  return el;
}

// ---------------------------------------------------------------------------
// Initialization & API Calls
// ---------------------------------------------------------------------------
async function init() {
  bindUiEvents();
  bindCanvasInteractions();

  try {
    const cfgRes = await fetch("/api/config");
    state.config = await cfgRes.json();

    if (state.config.group_icons?.aws_cloud_dark) {
      $("brandCloudIcon").src = state.config.group_icons.aws_cloud_dark;
    }
    if (state.config.group_icons?.aws_cloud_alt) {
      $("badgeAwsLogo").src = state.config.group_icons.aws_cloud_alt;
    }

    populateProfiles(state.config.profiles || []);
    renderSnapshotList(state.config.snapshots || []);
  } catch (err) {
    console.error("Config load failed:", err);
  }

  await loadSampleTopology(true);
  fetchProfileRegions($("profileSelect").value || "default");
}

function populateProfiles(profiles) {
  const sel = $("profileSelect");
  sel.innerHTML = "";
  for (const p of profiles) {
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = p.label;
    sel.appendChild(opt);
  }
}

async function fetchProfileRegions(profile) {
  try {
    const res = await fetch(`/api/regions?profile=${encodeURIComponent(profile)}`);
    const data = await res.json();
    state.availableRegions = data.regions || [];
    renderScanRegionChips();
  } catch (e) {
    console.warn("Could not fetch profile regions:", e);
  }
}

function renderScanRegionChips() {
  const wrap = $("regionChips");
  wrap.innerHTML = "";
  for (const reg of state.availableRegions) {
    const chip = document.createElement("span");
    chip.className = "region-chip" + (state.selectedScanRegions.has(reg) ? " selected" : "");
    chip.textContent = reg;
    chip.addEventListener("click", () => {
      if (state.selectedScanRegions.has(reg)) {
        state.selectedScanRegions.delete(reg);
      } else {
        state.selectedScanRegions.add(reg);
      }
      renderScanRegionChips();
    });
    wrap.appendChild(chip);
  }
}

async function loadSampleTopology(initial = false) {
  const res = await fetch("/api/sample");
  const data = await res.json();
  applyNewGraphAndLayout(data.graph, data.layout, true);
  if (!initial) {
    showToast("Loaded sample multi-region AWS architecture");
  }
}

function applyNewGraphAndLayout(graph, layout, fitAfter = true) {
  state.graph = graph;
  state.layout = layout;
  state.nodeOverrides.clear();
  state.selectedItem = null;
  state.hoveredNodeId = null;
  state.activeRegionsFilter.clear();
  state.activeCategoriesFilter.clear();
  $("inspectorDrawer").classList.add("hidden");

  // Update Header & Sample Banner
  const badge = $("envBadge");
  if (graph.is_sample) {
    badge.textContent = "Sample Topology";
    badge.className = "env-badge sample";
    $("sampleBanner").classList.remove("hidden");
  } else {
    badge.textContent = `Live: ${graph.account_alias || graph.account_id}`;
    badge.className = "env-badge live";
    $("sampleBanner").classList.add("hidden");
  }

  // Warnings
  const warnBox = $("scanWarningsBox");
  if (graph.warnings && graph.warnings.length > 0) {
    warnBox.innerHTML = `<strong>Partial Scan Notes (${graph.warnings.length}):</strong><br/>` +
      graph.warnings.map(escapeXml).join("<br/>");
    warnBox.classList.remove("hidden");
  } else {
    warnBox.classList.add("hidden");
  }

  updateKpiStrip();
  buildFilterSidebarControls();
  renderDiagram();
  if (fitAfter) {
    requestAnimationFrame(() => fitDiagramToViewport());
  }
}

async function requestLayoutRefresh(fitAfter = false) {
  if (!state.graph) return;
  const payload = {
    hide_empty_default_vpcs: state.hideEmptyDefaultVpcs,
    hide_empty_subnets: state.hideEmptySubnets,
    filter_regions: state.activeRegionsFilter.size > 0 ? Array.from(state.activeRegionsFilter) : null,
    filter_categories: state.activeCategoriesFilter.size > 0 ? Array.from(state.activeCategoriesFilter) : null,
    search_query: state.searchQuery,
    graph: state.graph,
  };
  const res = await fetch("/api/layout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  state.layout = data.layout;
  state.nodeOverrides.clear();
  renderDiagram();
  if (fitAfter) {
    fitDiagramToViewport();
  }
}

// ---------------------------------------------------------------------------
// Live AWS Scan Execution & Progress Polling
// ---------------------------------------------------------------------------
async function startLiveAwsScan() {
  const profile = $("profileSelect").value || "default";
  const regionMode = document.querySelector('input[name="regionMode"]:checked')?.value || "all";
  let regions = null;
  if (regionMode === "selected") {
    regions = Array.from(state.selectedScanRegions);
    if (regions.length === 0) {
      showToast("Please select at least one AWS region to scan.");
      return;
    }
  }

  $("scanErrorBox").classList.add("hidden");
  $("scanWarningsBox").classList.add("hidden");
  $("scanProgressBox").classList.remove("hidden");
  $("startScanBtn").disabled = true;
  $("scanProgressBar").style.width = "6%";
  $("scanPctLabel").textContent = "6%";
  $("scanStepMsg").textContent = `Connecting to AWS profile '${profile}'...`;

  try {
    const res = await fetch("/api/scan/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile,
        regions,
        include_global: $("chkGlobal").checked,
        include_tag_sweep: $("chkTagSweep").checked,
      }),
    });
    const data = await res.json();
    state.activeJobId = data.job_id;
    pollScanJob(data.job_id);
  } catch (err) {
    $("startScanBtn").disabled = false;
    $("scanProgressBox").classList.add("hidden");
    const errBox = $("scanErrorBox");
    errBox.textContent = `Failed to start scan: ${err.message}`;
    errBox.classList.remove("hidden");
  }
}

async function pollScanJob(jobId) {
  clearTimeout(state.pollTimer);
  try {
    const res = await fetch(`/api/scan/status/${jobId}`);
    const job = await res.json();

    const pct = Math.min(100, Math.max(5, Math.round((job.completed / Math.max(job.total, 1)) * 100)));
    $("scanProgressBar").style.width = `${pct}%`;
    $("scanPctLabel").textContent = `${pct}%`;
    $("scanStepMsg").textContent = job.message || "";

    if (job.status === "running") {
      state.pollTimer = setTimeout(() => pollScanJob(jobId), 550);
    } else if (job.status === "completed") {
      $("startScanBtn").disabled = false;
      $("scanStatusLabel").textContent = "Completed";
      setTimeout(() => $("scanProgressBox").classList.add("hidden"), 1800);
      if (job.snapshots) {
        renderSnapshotList(job.snapshots);
      }
      applyNewGraphAndLayout(job.graph, job.layout, true);
      showToast(`Mapped ${job.graph.resources.length} AWS resources!`);
    } else {
      $("startScanBtn").disabled = false;
      $("scanProgressBox").classList.add("hidden");
      const errBox = $("scanErrorBox");
      errBox.textContent = job.error || "AWS scan failed.";
      errBox.classList.remove("hidden");
    }
  } catch (err) {
    $("startScanBtn").disabled = false;
    $("scanProgressBox").classList.add("hidden");
  }
}

// ---------------------------------------------------------------------------
// Sidebar Controls & KPI Strip
// ---------------------------------------------------------------------------
function updateKpiStrip() {
  if (!state.graph) return;
  const g = state.graph;
  const st = g.stats || {};
  $("kpiAccount").textContent = g.account_id || "-";
  $("kpiRegions").textContent = Object.keys(st.by_region || {}).length;
  $("kpiVpcs").textContent = st.total_vpcs ?? 0;
  $("kpiSubnets").textContent = st.total_subnets ?? 0;
  $("kpiResources").textContent = g.resources.length;
  $("kpiEdges").textContent = g.edges.length;
}

function buildFilterSidebarControls() {
  if (!state.graph || !state.layout) return;

  // 1. Edge Kind Filter Checkboxes
  const edgeWrap = $("edgeKindFilters");
  edgeWrap.innerHTML = "";
  const edgeStyles = state.layout.edge_styles || {};
  for (const [kind, meta] of Object.entries(edgeStyles)) {
    const row = document.createElement("label");
    row.className = "edge-filter-row";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = state.enabledEdgeKinds.has(kind);
    chk.addEventListener("change", () => {
      if (chk.checked) state.enabledEdgeKinds.add(kind);
      else state.enabledEdgeKinds.delete(kind);
      renderEdgesLayer();
    });
    const swatch = document.createElement("span");
    swatch.className = "edge-swatch";
    swatch.style.background = meta.color;
    const txt = document.createElement("span");
    txt.textContent = meta.label;
    row.append(chk, swatch, txt);
    edgeWrap.appendChild(row);
  }

  // 2. Diagram Region Filter Pills
  const regWrap = $("diagramRegionPills");
  regWrap.innerHTML = "";
  const byRegion = state.graph.stats?.by_region || {};
  const regionsPresent = Object.keys(byRegion).sort((a, b) => (a === "global" ? -1 : b === "global" ? 1 : a.localeCompare(b)));
  for (const reg of regionsPresent) {
    const pill = document.createElement("button");
    const isAll = state.activeRegionsFilter.size === 0;
    const isActive = isAll || state.activeRegionsFilter.has(reg);
    pill.className = "filter-pill" + (isActive && !isAll ? " active" : "");
    pill.textContent = `${reg} (${byRegion[reg]})`;
    pill.addEventListener("click", () => {
      if (state.activeRegionsFilter.has(reg)) {
        state.activeRegionsFilter.delete(reg);
      } else {
        state.activeRegionsFilter.add(reg);
      }
      buildFilterSidebarControls();
      requestLayoutRefresh(true);
    });
    regWrap.appendChild(pill);
  }

  // 3. Official AWS Service Category Filter List
  const catWrap = $("categoryFilterList");
  catWrap.innerHTML = "";
  const byCat = state.graph.stats?.by_category || {};
  const cfgCats = state.config?.categories || {};
  for (const [catKey, catInfo] of Object.entries(cfgCats)) {
    const count = byCat[catKey] || 0;
    if (count === 0) continue;
    const isAll = state.activeCategoriesFilter.size === 0;
    const enabled = isAll || state.activeCategoriesFilter.has(catKey);
    const row = document.createElement("div");
    row.className = "cat-row" + (enabled ? "" : " inactive");
    row.innerHTML = `
      <div class="cat-left">
        <img src="${catInfo.data_uri}" alt="${escapeXml(catInfo.label)}" />
        <span>${escapeXml(catInfo.label)}</span>
      </div>
      <span class="cat-count">${count}</span>
    `;
    row.addEventListener("click", () => {
      if (state.activeCategoriesFilter.size === 0) {
        state.activeCategoriesFilter.add(catKey);
      } else if (state.activeCategoriesFilter.has(catKey)) {
        state.activeCategoriesFilter.delete(catKey);
      } else {
        state.activeCategoriesFilter.add(catKey);
      }
      buildFilterSidebarControls();
      requestLayoutRefresh(false);
    });
    catWrap.appendChild(row);
  }
}

function renderSnapshotList(snapshots) {
  const wrap = $("snapshotList");
  if (!snapshots || snapshots.length === 0) {
    wrap.innerHTML = `<p class="field-hint">No saved scans yet. Run a live AWS scan to save a snapshot automatically.</p>`;
    return;
  }
  wrap.innerHTML = "";
  for (const snap of snapshots) {
    const div = document.createElement("div");
    div.className = "snap-item";
    div.innerHTML = `
      <strong>${escapeXml(snap.filename)}</strong>
      <span>${escapeXml(snap.modified_at)} &bull; ${snap.size_kb} KB</span>
    `;
    div.addEventListener("click", async () => {
      const res = await fetch(`/api/snapshots/${encodeURIComponent(snap.filename)}`);
      if (res.ok) {
        const data = await res.json();
        applyNewGraphAndLayout(data.graph, data.layout, true);
        showToast(`Loaded snapshot ${snap.filename}`);
      }
    });
    wrap.appendChild(div);
  }
}

// ---------------------------------------------------------------------------
// Interactive SVG Architecture Canvas Rendering
// ---------------------------------------------------------------------------
function getEffectiveNodeRect(node) {
  const ov = state.nodeOverrides.get(node.id);
  return {
    x: ov ? ov.x : node.x,
    y: ov ? ov.y : node.y,
    w: node.w,
    h: node.h,
  };
}

function computeEdgeGeometry(srcRect, tgtRect) {
  const sx = srcRect.x + srcRect.w / 2;
  const sy = srcRect.y + srcRect.h / 2;
  const tx = tgtRect.x + tgtRect.w / 2;
  const ty = tgtRect.y + tgtRect.h / 2;
  const dx = tx - sx;
  const dy = ty - sy;

  let x1, y1, x2, y2, path;
  if (Math.abs(dy) >= Math.abs(dx) * 0.62) {
    if (dy >= 0) {
      x1 = sx; y1 = srcRect.y + srcRect.h - 3;
      x2 = tx; y2 = tgtRect.y + 2;
    } else {
      x1 = sx; y1 = srcRect.y + 2;
      x2 = tx; y2 = tgtRect.y + tgtRect.h - 3;
    }
    const cy1 = y1 + (y2 - y1) * 0.45;
    const cy2 = y2 - (y2 - y1) * 0.45;
    path = `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${x1.toFixed(1)} ${cy1.toFixed(1)}, ${x2.toFixed(1)} ${cy2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`;
  } else {
    if (dx >= 0) {
      x1 = srcRect.x + srcRect.w - 6; y1 = srcRect.y + 44;
      x2 = tgtRect.x + 6; y2 = tgtRect.y + 44;
    } else {
      x1 = srcRect.x + 6; y1 = srcRect.y + 44;
      x2 = tgtRect.x + tgtRect.w - 6; y2 = tgtRect.y + 44;
    }
    const cx1 = x1 + (x2 - x1) * 0.45;
    const cx2 = x2 - (x2 - x1) * 0.45;
    path = `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${cx1.toFixed(1)} ${y1.toFixed(1)}, ${cx2.toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`;
  }
  return { x1, y1, x2, y2, mx: (x1 + x2) / 2, my: (y1 + y2) / 2, path };
}

function renderDiagram() {
  const layout = state.layout;
  if (!layout) return;

  $("visibleCountPill").textContent = `${layout.nodes.length} nodes`;

  // 1. SVG Defs (Shadow + Arrow Markers)
  const defs = $("svgDefs");
  defs.innerHTML = `
    <filter id="cardShadow" x="-10%" y="-10%" width="120%" height="124%">
      <feDropShadow dx="0" dy="2" stdDeviation="2.5" flood-color="#0F172A" flood-opacity="0.08" />
    </filter>
  `;
  for (const [kind, est] of Object.entries(layout.edge_styles || {})) {
    const marker = createSvgEl("marker", {
      id: `arrow-${kind}`,
      viewBox: "0 0 10 10",
      refX: "8.5",
      refY: "5",
      markerWidth: "6.5",
      markerHeight: "6.5",
      orient: "auto-start-reverse",
    });
    marker.appendChild(createSvgEl("path", { d: "M 0 1.5 L 10 5 L 0 8.5 z", fill: est.color }));
    defs.appendChild(marker);
  }

  // 2. Render Official AWS Container Boxes
  const boxesLayer = $("boxesLayer");
  boxesLayer.innerHTML = "";
  for (const box of layout.boxes) {
    const st = layout.container_styles[box.kind] || layout.container_styles.category_box;
    const g = createSvgEl("g", { class: "aws-box-group", "data-id": box.id });
    const rect = createSvgEl("rect", {
      x: box.x,
      y: box.y,
      width: box.w,
      height: box.h,
      rx: st.radius,
      fill: st.fill,
      stroke: st.stroke,
      "stroke-width": st.stroke_width,
      "stroke-dasharray": st.dash || undefined,
    });
    g.appendChild(rect);

    const iconUri = layout.icons[box.icon_key];
    const isz = st.icon_size;
    if (iconUri) {
      g.appendChild(createSvgEl("image", {
        x: box.x + 10,
        y: box.y + 8,
        width: isz,
        height: isz,
        href: iconUri,
      }));
    }

    const titleY = box.y + 8 + isz * 0.72;
    const fsize = ["aws_cloud", "account", "region"].includes(box.kind) ? 14 : 12.5;
    const title = createSvgEl("text", {
      x: box.x + 10 + isz + 8,
      y: titleY,
      fill: st.title_color,
      "font-size": fsize,
      "font-weight": "700",
    });
    title.textContent = box.name;
    g.appendChild(title);

    if (box.subtitle) {
      const sub = createSvgEl("text", {
        x: box.x + box.w - 12,
        y: titleY,
        "text-anchor": "end",
        fill: "#64748B",
        "font-size": "11",
        "font-weight": "500",
      });
      sub.textContent = box.subtitle;
      g.appendChild(sub);
    }

    g.addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectBox(box);
    });
    boxesLayer.appendChild(g);
  }

  // 3. Render Official AWS Resource Nodes (Draggable + Clickable)
  const nodesLayer = $("nodesLayer");
  nodesLayer.innerHTML = "";
  state.nodeEls.clear();

  for (const node of layout.nodes) {
    const r = getEffectiveNodeRect(node);
    const g = createSvgEl("g", {
      class: "aws-node-group",
      transform: `translate(${r.x}, ${r.y})`,
      "data-id": node.id,
    });

    const card = createSvgEl("rect", {
      class: "aws-node-card",
      x: 6,
      y: 2,
      width: r.w - 12,
      height: r.h - 4,
      rx: 8,
      fill: "#FFFFFF",
      "fill-opacity": "0.97",
      stroke: "#E2E8F0",
      "stroke-width": "1.1",
      filter: "url(#cardShadow)",
    });
    g.appendChild(card);

    const iconUri = layout.icons[node.icon_key];
    if (iconUri) {
      g.appendChild(createSvgEl("image", {
        x: (r.w - 48) / 2,
        y: 6,
        width: 48,
        height: 48,
        href: iconUri,
      }));
    }

    const nameText = createSvgEl("text", {
      x: r.w / 2,
      y: 69,
      "text-anchor": "middle",
      fill: "#0F172A",
      "font-size": "11",
      "font-weight": "600",
    });
    nameText.textContent = truncateText(node.name, 19);
    g.appendChild(nameText);

    if (node.sublabel) {
      const subText = createSvgEl("text", {
        x: r.w / 2,
        y: 84,
        "text-anchor": "middle",
        fill: "#64748B",
        "font-size": "9.5",
      });
      subText.textContent = truncateText(node.sublabel, 21);
      g.appendChild(subText);
    }

    const tooltip = createSvgEl("title");
    tooltip.textContent = `${node.name}\n${node.resource_type} (${node.region})`;
    g.appendChild(tooltip);

    bindNodeInteractions(g, node);
    nodesLayer.appendChild(g);
    state.nodeEls.set(node.id, { el: g, node });
  }

  // 4. Render Edges & Minimap
  renderEdgesLayer();
  renderMinimap();
  applyCamera();
}

function renderEdgesLayer() {
  const edgesLayer = $("edgesLayer");
  const labelsLayer = $("edgeLabelsLayer");
  edgesLayer.innerHTML = "";
  labelsLayer.innerHTML = "";
  state.edgeEls = [];

  if (!state.layout || !state.showEdges) return;

  const nodesById = new Map(state.layout.nodes.map((n) => [n.id, n]));
  for (const edge of state.layout.edges) {
    if (!state.enabledEdgeKinds.has(edge.kind)) continue;
    const srcNode = nodesById.get(edge.source);
    const tgtNode = nodesById.get(edge.target);
    if (!srcNode || !tgtNode) continue;

    const est = state.layout.edge_styles[edge.kind] || state.layout.edge_styles.traffic;
    const geom = computeEdgeGeometry(getEffectiveNodeRect(srcNode), getEffectiveNodeRect(tgtNode));

    const g = createSvgEl("g", { class: "aws-edge-group" });
    const pathEl = createSvgEl("path", {
      d: geom.path,
      fill: "none",
      stroke: est.color,
      "stroke-width": "1.65",
      "stroke-opacity": "0.78",
      "stroke-dasharray": est.dash || undefined,
      "marker-end": `url(#arrow-${edge.kind})`,
    });
    g.appendChild(pathEl);

    let labelGroup = null;
    let labelBg = null;
    let labelTxt = null;
    if (state.showEdgeLabels && edge.label) {
      labelGroup = createSvgEl("g", { class: "aws-edge-group" });
      const lbl = truncateText(edge.label, 18);
      const lw = lbl.length * 5.5 + 10;
      labelBg = createSvgEl("rect", {
        x: geom.mx - lw / 2,
        y: geom.my - 7.5,
        width: lw,
        height: 15,
        rx: 4,
        fill: "#FFFFFF",
        "fill-opacity": "0.95",
        stroke: est.color,
        "stroke-width": "0.9",
      });
      labelTxt = createSvgEl("text", {
        x: geom.mx,
        y: geom.my + 3,
        "text-anchor": "middle",
        fill: "#334155",
        "font-size": "9",
        "font-weight": "600",
      });
      labelTxt.textContent = lbl;
      labelGroup.append(labelBg, labelTxt);
      labelsLayer.appendChild(labelGroup);
    }

    edgesLayer.appendChild(g);
    state.edgeEls.push({ edge, g, labelGroup, pathEl, labelBg, labelTxt, srcNode, tgtNode });
  }

  refreshHighlightStates();
}

function updateEdgesForDraggedNode(nodeId) {
  for (const item of state.edgeEls) {
    if (item.edge.source !== nodeId && item.edge.target !== nodeId) continue;
    const geom = computeEdgeGeometry(getEffectiveNodeRect(item.srcNode), getEffectiveNodeRect(item.tgtNode));
    item.pathEl.setAttribute("d", geom.path);
    if (item.labelBg && item.labelTxt) {
      const lw = parseFloat(item.labelBg.getAttribute("width"));
      item.labelBg.setAttribute("x", String(geom.mx - lw / 2));
      item.labelBg.setAttribute("y", String(geom.my - 8));
      item.labelTxt.setAttribute("x", String(geom.mx));
      item.labelTxt.setAttribute("y", String(geom.my + 2.5));
    }
  }
}

function refreshHighlightStates() {
  const focusNodeId = state.hoveredNodeId || (state.selectedItem?.kind === "node" ? state.selectedItem.id : null);

  for (const [id, entry] of state.nodeEls.entries()) {
    entry.el.classList.toggle("selected", state.selectedItem?.kind === "node" && state.selectedItem.id === id);
  }

  if (!focusNodeId) {
    for (const entry of state.nodeEls.values()) entry.el.classList.remove("dimmed");
    for (const eItem of state.edgeEls) {
      eItem.g.classList.remove("dimmed", "highlighted");
      if (eItem.labelGroup) eItem.labelGroup.classList.remove("dimmed", "highlighted");
    }
    return;
  }

  const neighbors = new Set([focusNodeId]);
  for (const eItem of state.edgeEls) {
    const connected = eItem.edge.source === focusNodeId || eItem.edge.target === focusNodeId;
    eItem.g.classList.toggle("highlighted", connected);
    eItem.g.classList.toggle("dimmed", !connected);
    if (eItem.labelGroup) {
      eItem.labelGroup.classList.toggle("highlighted", connected);
      eItem.labelGroup.classList.toggle("dimmed", !connected);
    }
    if (connected) {
      neighbors.add(eItem.edge.source);
      neighbors.add(eItem.edge.target);
    }
  }

  for (const [id, entry] of state.nodeEls.entries()) {
    entry.el.classList.toggle("dimmed", !neighbors.has(id));
  }
}

// ---------------------------------------------------------------------------
// Node Dragging, Viewport Pan/Zoom & Minimap
// ---------------------------------------------------------------------------
function bindNodeInteractions(el, node) {
  el.addEventListener("mouseenter", () => {
    state.hoveredNodeId = node.id;
    refreshHighlightStates();
  });
  el.addEventListener("mouseleave", () => {
    if (state.hoveredNodeId === node.id) {
      state.hoveredNodeId = null;
      refreshHighlightStates();
    }
  });

  let dragStart = null;
  let moved = false;

  el.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.stopPropagation();
    el.setPointerCapture(ev.pointerId);
    const cur = getEffectiveNodeRect(node);
    dragStart = {
      px: ev.clientX,
      py: ev.clientY,
      nx: cur.x,
      ny: cur.y,
    };
    moved = false;
  });

  el.addEventListener("pointermove", (ev) => {
    if (!dragStart) return;
    const dx = (ev.clientX - dragStart.px) / state.cam.scale;
    const dy = (ev.clientY - dragStart.py) / state.cam.scale;
    if (Math.hypot(dx, dy) > 3) moved = true;
    if (moved) {
      const nx = Math.round(dragStart.nx + dx);
      const ny = Math.round(dragStart.ny + dy);
      state.nodeOverrides.set(node.id, { x: nx, y: ny });
      el.setAttribute("transform", `translate(${nx}, ${ny})`);
      updateEdgesForDraggedNode(node.id);
    }
  });

  el.addEventListener("pointerup", (ev) => {
    if (!dragStart) return;
    el.releasePointerCapture(ev.pointerId);
    dragStart = null;
    if (!moved) {
      selectNode(node);
    }
  });
}

function bindCanvasInteractions() {
  const svg = $("viewportSvg");
  let panStart = null;

  svg.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 && ev.button !== 1) return;
    svg.setPointerCapture(ev.pointerId);
    svg.classList.add("panning");
    panStart = {
      px: ev.clientX,
      py: ev.clientY,
      cx: state.cam.x,
      cy: state.cam.y,
      moved: false,
    };
  });

  svg.addEventListener("pointermove", (ev) => {
    if (!panStart) return;
    const dx = ev.clientX - panStart.px;
    const dy = ev.clientY - panStart.py;
    if (Math.hypot(dx, dy) > 4) panStart.moved = true;
    state.cam.x = panStart.cx + dx;
    state.cam.y = panStart.cy + dy;
    applyCamera();
  });

  svg.addEventListener("pointerup", (ev) => {
    if (!panStart) return;
    svg.releasePointerCapture(ev.pointerId);
    svg.classList.remove("panning");
    const wasClick = !panStart.moved;
    panStart = null;
    if (wasClick && ev.target === svg) {
      clearSelection();
    }
  });

  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;

    if (ev.ctrlKey || ev.metaKey || Math.abs(ev.deltaY) >= 40 || ev.deltaMode === 1) {
      // Cursor-centered zoom
      const zoomFactor = Math.exp(-ev.deltaY * 0.0018);
      zoomAroundPoint(mx, my, state.cam.scale * zoomFactor);
    } else {
      // Smooth trackpad two-finger pan OR wheel zoom
      const zoomFactor = Math.exp(-ev.deltaY * 0.0015);
      zoomAroundPoint(mx, my, state.cam.scale * zoomFactor);
    }
  }, { passive: false });

  // Minimap click-to-jump
  const miniSvg = $("minimapSvg");
  let miniDrag = false;
  const jumpFromMinimap = (ev) => {
    if (!state.layout) return;
    const rect = miniSvg.getBoundingClientRect();
    const rx = (ev.clientX - rect.left) / rect.width;
    const ry = (ev.clientY - rect.top) / rect.height;
    const targetX = rx * state.layout.canvas_width;
    const targetY = ry * state.layout.canvas_height;
    const stageRect = $("viewportSvg").getBoundingClientRect();
    state.cam.x = stageRect.width / 2 - targetX * state.cam.scale;
    state.cam.y = stageRect.height / 2 - targetY * state.cam.scale;
    applyCamera();
  };
  miniSvg.addEventListener("pointerdown", (ev) => {
    miniDrag = true;
    miniSvg.setPointerCapture(ev.pointerId);
    jumpFromMinimap(ev);
  });
  miniSvg.addEventListener("pointermove", (ev) => {
    if (miniDrag) jumpFromMinimap(ev);
  });
  miniSvg.addEventListener("pointerup", () => { miniDrag = false; });
}

function zoomAroundPoint(mx, my, nextScale) {
  const clamped = Math.min(2.8, Math.max(0.16, nextScale));
  const worldX = (mx - state.cam.x) / state.cam.scale;
  const worldY = (my - state.cam.y) / state.cam.scale;
  state.cam.scale = clamped;
  state.cam.x = mx - worldX * clamped;
  state.cam.y = my - worldY * clamped;
  applyCamera();
}

function applyCamera() {
  $("cameraGroup").setAttribute(
    "transform",
    `translate(${state.cam.x.toFixed(1)}, ${state.cam.y.toFixed(1)}) scale(${state.cam.scale.toFixed(4)})`
  );
  $("zoomPct").textContent = `${Math.round(state.cam.scale * 100)}%`;
  updateMinimapViewport();
}

function fitDiagramToViewport() {
  if (!state.layout) return;
  const svgRect = $("viewportSvg").getBoundingClientRect();
  const padX = 48;
  const padTop = 94;
  const padBottom = 64;
  const availW = Math.max(200, svgRect.width - padX * 2);
  const availH = Math.max(200, svgRect.height - padTop - padBottom);
  const scale = Math.min(
    1.15,
    availW / state.layout.canvas_width,
    availH / state.layout.canvas_height
  );
  state.cam.scale = scale;
  state.cam.x = (svgRect.width - state.layout.canvas_width * scale) / 2;
  state.cam.y = padTop + (availH - state.layout.canvas_height * scale) / 2;
  applyCamera();
}

function renderMinimap() {
  if (!state.layout) return;
  const miniSvg = $("minimapSvg");
  const cw = state.layout.canvas_width;
  const ch = state.layout.canvas_height;
  miniSvg.setAttribute("viewBox", `0 0 ${cw} ${ch}`);
  const content = $("minimapContent");
  content.innerHTML = "";
  for (const box of state.layout.boxes) {
    const st = state.layout.container_styles[box.kind] || state.layout.container_styles.category_box;
    content.appendChild(createSvgEl("rect", {
      x: box.x,
      y: box.y,
      width: box.w,
      height: box.h,
      fill: st.fill,
      stroke: st.stroke,
      "stroke-width": Math.max(3, cw * 0.002),
      rx: 8,
    }));
  }
  for (const n of state.layout.nodes) {
    const r = getEffectiveNodeRect(n);
    content.appendChild(createSvgEl("rect", {
      x: r.x + 20,
      y: r.y + 12,
      width: r.w - 40,
      height: r.h - 28,
      fill: "#334155",
      opacity: "0.45",
      rx: 6,
    }));
  }
  updateMinimapViewport();
}

function updateMinimapViewport() {
  if (!state.layout) return;
  const svgRect = $("viewportSvg").getBoundingClientRect();
  const vx = -state.cam.x / state.cam.scale;
  const vy = -state.cam.y / state.cam.scale;
  const vw = svgRect.width / state.cam.scale;
  const vh = svgRect.height / state.cam.scale;
  const vp = $("minimapViewport");
  vp.setAttribute("x", String(vx));
  vp.setAttribute("y", String(vy));
  vp.setAttribute("width", String(Math.max(40, vw)));
  vp.setAttribute("height", String(Math.max(40, vh)));
  vp.setAttribute("stroke-width", String(Math.max(6, state.layout.canvas_width * 0.006)));
}

// ---------------------------------------------------------------------------
// Resource & Container Inspector Drawer
// ---------------------------------------------------------------------------
function clearSelection() {
  state.selectedItem = null;
  $("inspectorDrawer").classList.add("hidden");
  refreshHighlightStates();
}

function focusAndSelectNodeById(nodeId) {
  const entry = state.nodeEls.get(nodeId);
  if (!entry) return;
  const r = getEffectiveNodeRect(entry.node);
  const svgRect = $("viewportSvg").getBoundingClientRect();
  const targetScale = Math.max(state.cam.scale, 0.9);
  state.cam.scale = targetScale;
  state.cam.x = svgRect.width / 2 - (r.x + r.w / 2) * targetScale;
  state.cam.y = svgRect.height / 2 - (r.y + r.h / 2) * targetScale;
  applyCamera();
  selectNode(entry.node);
}

function selectNode(node) {
  state.selectedItem = { kind: "node", id: node.id };
  refreshHighlightStates();

  const drawer = $("inspectorDrawer");
  drawer.classList.remove("hidden");
  $("inspIcon").src = state.layout.icons[node.icon_key] || "";
  $("inspTypeBadge").textContent = node.resource_type;
  $("inspName").textContent = node.name;

  const inbound = state.layout.edges.filter((e) => e.target === node.id);
  const outbound = state.layout.edges.filter((e) => e.source === node.id);
  const nodesById = new Map(state.layout.nodes.map((n) => [n.id, n]));

  const coreRows = [
    ["Service", node.service],
    ["Category", node.category],
    ["Region", node.region],
    ["Details", node.sublabel],
    ["State", node.state || "active"],
    ["VPC", node.vpc_id || "—"],
    ["Subnet", node.subnet_id || "—"],
    ["AZ", node.az || "—"],
    ["Security Groups", (node.security_group_ids || []).join(", ") || "—"],
  ];

  let html = "";
  if (node.arn) {
    html += `
      <div class="insp-block">
        <h4>Amazon Resource Name (ARN)</h4>
        <div class="arn-box">
          <span>${escapeXml(node.arn)}</span>
          <button id="copyArnBtn" class="btn btn-secondary" style="height:26px;font-size:11px;align-self:flex-start;">Copy ARN</button>
        </div>
      </div>
    `;
  }

  html += `
    <div class="insp-block">
      <h4>Properties</h4>
      <table class="kv-table">
        ${coreRows.map(([k, v]) => `<tr><td>${escapeXml(k)}</td><td>${escapeXml(v)}</td></tr>`).join("")}
      </table>
    </div>
  `;

  if (inbound.length > 0) {
    html += `<div class="insp-block"><h4>Inbound Connections (${inbound.length})</h4>`;
    for (const e of inbound) {
      const peer = nodesById.get(e.source);
      if (!peer) continue;
      html += `
        <div class="conn-pill" data-jump="${escapeXml(peer.id)}">
          <img src="${state.layout.icons[peer.icon_key] || ""}" alt="" />
          <div class="conn-pill-meta">
            <strong>${escapeXml(peer.name)}</strong>
            <span>&larr; ${escapeXml(e.label || e.kind)} (${escapeXml(peer.resource_type)})</span>
          </div>
        </div>
      `;
    }
    html += `</div>`;
  }

  if (outbound.length > 0) {
    html += `<div class="insp-block"><h4>Outbound Connections (${outbound.length})</h4>`;
    for (const e of outbound) {
      const peer = nodesById.get(e.target);
      if (!peer) continue;
      html += `
        <div class="conn-pill" data-jump="${escapeXml(peer.id)}">
          <img src="${state.layout.icons[peer.icon_key] || ""}" alt="" />
          <div class="conn-pill-meta">
            <strong>${escapeXml(peer.name)}</strong>
            <span>&rarr; ${escapeXml(e.label || e.kind)} (${escapeXml(peer.resource_type)})</span>
          </div>
        </div>
      `;
    }
    html += `</div>`;
  }

  const metaEntries = Object.entries(node.metadata || {}).filter(([, v]) => v !== "" && v !== null && v !== undefined);
  if (metaEntries.length > 0) {
    html += `
      <div class="insp-block">
        <h4>Service Metadata</h4>
        <table class="kv-table">
          ${metaEntries.map(([k, v]) => `<tr><td>${escapeXml(k)}</td><td>${escapeXml(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}
        </table>
      </div>
    `;
  }

  const tagEntries = Object.entries(node.tags || {});
  if (tagEntries.length > 0) {
    html += `
      <div class="insp-block">
        <h4>AWS Resource Tags (${tagEntries.length})</h4>
        <table class="kv-table">
          ${tagEntries.map(([k, v]) => `<tr><td>${escapeXml(k)}</td><td>${escapeXml(v)}</td></tr>`).join("")}
        </table>
      </div>
    `;
  }

  const body = $("inspectorBody");
  body.innerHTML = html;

  const copyBtn = $("copyArnBtn");
  if (copyBtn) {
    copyBtn.addEventListener("click", () => {
      navigator.clipboard.writeText(node.arn);
      showToast("Copied ARN to clipboard");
    });
  }

  body.querySelectorAll("[data-jump]").forEach((pill) => {
    pill.addEventListener("click", () => focusAndSelectNodeById(pill.getAttribute("data-jump")));
  });
}

function selectBox(box) {
  state.selectedItem = { kind: "box", id: box.id };
  refreshHighlightStates();

  const drawer = $("inspectorDrawer");
  drawer.classList.remove("hidden");
  $("inspIcon").src = state.layout.icons[box.icon_key] || "";
  $("inspTypeBadge").textContent = `group:${box.kind}`;
  $("inspName").textContent = box.name;

  const metaEntries = Object.entries(box.metadata || {}).filter(([k]) => k !== "tags");
  const tags = box.metadata?.tags || {};

  let html = `
    <div class="insp-block">
      <h4>AWS Architecture Group</h4>
      <table class="kv-table">
        <tr><td>Boundary Type</td><td>${escapeXml(box.kind)}</td></tr>
        ${box.subtitle ? `<tr><td>Info</td><td>${escapeXml(box.subtitle)}</td></tr>` : ""}
        ${metaEntries.map(([k, v]) => `<tr><td>${escapeXml(k)}</td><td>${escapeXml(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}
      </table>
    </div>
  `;

  const tagEntries = Object.entries(tags);
  if (tagEntries.length > 0) {
    html += `
      <div class="insp-block">
        <h4>Group Tags</h4>
        <table class="kv-table">
          ${tagEntries.map(([k, v]) => `<tr><td>${escapeXml(k)}</td><td>${escapeXml(v)}</td></tr>`).join("")}
        </table>
      </div>
    `;
  }

  $("inspectorBody").innerHTML = html;
}

// ---------------------------------------------------------------------------
// Standalone Official-AWS-Icon SVG & High-DPI Retina PNG Exporters
// ---------------------------------------------------------------------------
function buildStandaloneSvgMarkup() {
  const layout = state.layout;
  const w = layout.canvas_width;
  const h = layout.canvas_height;
  const icons = layout.icons;
  const nodesById = new Map(layout.nodes.map((n) => [n.id, n]));

  const out = [];
  out.push(
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="background:#EEF2F6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">`
  );
  out.push(`<defs>`);
  out.push(`  <filter id="cardShadow" x="-10%" y="-10%" width="120%" height="124%"><feDropShadow dx="0" dy="2" stdDeviation="2.5" flood-color="#0F172A" flood-opacity="0.08"/></filter>`);
  for (const [kind, est] of Object.entries(layout.edge_styles || {})) {
    out.push(
      `  <marker id="arrow-${kind}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse"><path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="${est.color}"/></marker>`
    );
  }
  out.push(`</defs>`);
  out.push(`<rect x="0" y="0" width="${w}" height="${h}" fill="#EEF2F6"/>`);

  // Containers
  for (const box of layout.boxes) {
    const st = layout.container_styles[box.kind] || layout.container_styles.category_box;
    const dash = st.dash ? ` stroke-dasharray="${st.dash}"` : "";
    out.push(`<g>`);
    out.push(`<rect x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" rx="${st.radius}" fill="${st.fill}" stroke="${st.stroke}" stroke-width="${st.stroke_width}"${dash}/>`);
    const iconUri = icons[box.icon_key];
    const isz = st.icon_size;
    if (iconUri) {
      out.push(`<image x="${box.x + 10}" y="${box.y + 8}" width="${isz}" height="${isz}" href="${iconUri}"/>`);
    }
    const ty = (box.y + 8 + isz * 0.72).toFixed(1);
    const fsize = ["aws_cloud", "account", "region"].includes(box.kind) ? 14 : 12.5;
    out.push(`<text x="${(box.x + 10 + isz + 8).toFixed(1)}" y="${ty}" fill="${st.title_color}" font-size="${fsize}" font-weight="700">${escapeXml(box.name)}</text>`);
    if (box.subtitle) {
      out.push(`<text x="${(box.x + box.w - 12).toFixed(1)}" y="${ty}" text-anchor="end" fill="#64748B" font-size="11" font-weight="500">${escapeXml(box.subtitle)}</text>`);
    }
    out.push(`</g>`);
  }

  // Edges (lines below cards, labels collected above cards)
  const deferredLabels = [];
  if (state.showEdges) {
    out.push(`<g>`);
    for (const edge of layout.edges) {
      if (!state.enabledEdgeKinds.has(edge.kind)) continue;
      const src = nodesById.get(edge.source);
      const tgt = nodesById.get(edge.target);
      if (!src || !tgt) continue;
      const est = layout.edge_styles[edge.kind] || layout.edge_styles.traffic;
      const geom = computeEdgeGeometry(getEffectiveNodeRect(src), getEffectiveNodeRect(tgt));
      const dash = est.dash ? ` stroke-dasharray="${est.dash}"` : "";
      out.push(`<path d="${geom.path}" fill="none" stroke="${est.color}" stroke-width="1.65" stroke-opacity="0.82"${dash} marker-end="url(#arrow-${edge.kind})"/>`);
      if (state.showEdgeLabels && edge.label) {
        const lbl = truncateText(edge.label, 18);
        const lw = lbl.length * 5.5 + 10;
        deferredLabels.push(
          `<rect x="${(geom.mx - lw / 2).toFixed(1)}" y="${(geom.my - 7.5).toFixed(1)}" width="${lw.toFixed(1)}" height="15" rx="4" fill="#FFFFFF" fill-opacity="0.95" stroke="${est.color}" stroke-width="0.9"/>` +
          `<text x="${geom.mx.toFixed(1)}" y="${(geom.my + 3).toFixed(1)}" text-anchor="middle" fill="#334155" font-size="9" font-weight="600">${escapeXml(lbl)}</text>`
        );
      }
    }
    out.push(`</g>`);
  }

  // Official AWS Resource Nodes
  out.push(`<g>`);
  for (const node of layout.nodes) {
    const r = getEffectiveNodeRect(node);
    const iconUri = icons[node.icon_key];
    out.push(`<g transform="translate(${r.x}, ${r.y})">`);
    out.push(`<rect x="6" y="2" width="${r.w - 12}" height="${r.h - 4}" rx="8" fill="#FFFFFF" fill-opacity="0.98" stroke="#DCE3EC" stroke-width="1.1" filter="url(#cardShadow)"/>`);
    if (iconUri) {
      out.push(`<image x="${(r.w - 48) / 2}" y="6" width="48" height="48" href="${iconUri}"/>`);
    }
    out.push(`<text x="${r.w / 2}" y="69" text-anchor="middle" fill="#0F172A" font-size="11" font-weight="600">${escapeXml(truncateText(node.name, 19))}</text>`);
    if (node.sublabel) {
      out.push(`<text x="${r.w / 2}" y="84" text-anchor="middle" fill="#64748B" font-size="9.5">${escapeXml(truncateText(node.sublabel, 21))}</text>`);
    }
    out.push(`</g>`);
  }
  out.push(`</g>`);
  if (deferredLabels.length > 0) {
    out.push(`<g>${deferredLabels.join("\n")}</g>`);
  }
  out.push(`</svg>`);
  return out.join("\n");
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function exportSvgDiagram() {
  if (!state.layout) return;
  const svgStr = buildStandaloneSvgMarkup();
  const blob = new Blob([svgStr], { type: "image/svg+xml;charset=utf-8" });
  const acct = state.graph?.account_id || "aws";
  downloadBlob(blob, `aws-architecture-${acct}.svg`);
  showToast("Exported standalone vector SVG (with embedded official AWS icons)");
}

async function exportPngDiagram() {
  if (!state.layout) return;
  showToast("Rendering high-resolution PNG...");
  const svgStr = buildStandaloneSvgMarkup();
  const w = state.layout.canvas_width;
  const h = state.layout.canvas_height;

  // Choose 2x Retina scale while respecting browser max canvas dimension (~14000px)
  const scale = Math.min(2.2, 12000 / Math.max(w, h, 1));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext("2d");

  const img = new Image();
  const dataUri = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgStr);
  img.src = dataUri;

  try {
    await img.decode();
    ctx.fillStyle = "#EEF2F6";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (blob) {
        const acct = state.graph?.account_id || "aws";
        downloadBlob(blob, `aws-architecture-${acct}.png`);
        showToast(`Exported ${canvas.width}x${canvas.height} HD PNG!`);
      }
    }, "image/png");
  } catch (err) {
    console.error("PNG export failed:", err);
    showToast("PNG export encountered an error; try Export SVG.");
  }
}

// ---------------------------------------------------------------------------
// UI Event Bindings
// ---------------------------------------------------------------------------
function bindUiEvents() {
  // Sidebar Tabs
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === target));
    });
  });

  $("focusScanBtn").addEventListener("click", () => {
    document.querySelector('[data-tab="scanTab"]').click();
    $("startScanBtn").focus();
  });

  $("refreshProfilesBtn").addEventListener("click", async () => {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    populateProfiles(cfg.profiles || []);
    showToast("Reloaded local ~/.aws profiles");
  });

  $("profileSelect").addEventListener("change", (ev) => {
    fetchProfileRegions(ev.target.value);
  });

  document.querySelectorAll('input[name="regionMode"]').forEach((rad) => {
    rad.addEventListener("change", () => {
      const mode = document.querySelector('input[name="regionMode"]:checked')?.value;
      $("regionPickerWrap").classList.toggle("hidden", mode !== "selected");
    });
  });

  document.querySelectorAll(".chip-action").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = btn.getAttribute("data-preset");
      state.selectedScanRegions.clear();
      for (const r of state.availableRegions) {
        if (preset === "all") state.selectedScanRegions.add(r);
        else if (preset === "us" && (r.startsWith("us-") || r.startsWith("ca-"))) state.selectedScanRegions.add(r);
        else if (preset === "eu" && r.startsWith("eu-")) state.selectedScanRegions.add(r);
        else if (preset === "ap" && r.startsWith("ap-")) state.selectedScanRegions.add(r);
      }
      renderScanRegionChips();
    });
  });

  $("startScanBtn").addEventListener("click", startLiveAwsScan);
  $("loadSampleBtn").addEventListener("click", () => loadSampleTopology(false));
  $("dismissBannerBtn").addEventListener("click", () => $("sampleBanner").classList.add("hidden"));

  // Filter Checkboxes
  $("chkHideDefaultVpcs").addEventListener("change", (ev) => {
    state.hideEmptyDefaultVpcs = ev.target.checked;
    requestLayoutRefresh(false);
  });
  $("chkHideEmptySubnets").addEventListener("change", (ev) => {
    state.hideEmptySubnets = ev.target.checked;
    requestLayoutRefresh(false);
  });
  $("chkShowEdges").addEventListener("change", (ev) => {
    state.showEdges = ev.target.checked;
    renderEdgesLayer();
  });
  $("chkShowEdgeLabels").addEventListener("change", (ev) => {
    state.showEdgeLabels = ev.target.checked;
    renderEdgesLayer();
  });

  $("resetRegionsFilterBtn").addEventListener("click", () => {
    state.activeRegionsFilter.clear();
    buildFilterSidebarControls();
    requestLayoutRefresh(true);
  });

  $("resetCategoriesBtn").addEventListener("click", () => {
    state.activeCategoriesFilter.clear();
    buildFilterSidebarControls();
    requestLayoutRefresh(false);
  });

  // Search Input (debounced)
  let searchDebounce = null;
  $("searchInput").addEventListener("input", (ev) => {
    state.searchQuery = ev.target.value;
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => requestLayoutRefresh(false), 200);
  });

  // Snapshots JSON Export / Import
  $("downloadJsonBtn").addEventListener("click", () => {
    if (!state.graph) return;
    const blob = new Blob([JSON.stringify(state.graph, null, 2)], { type: "application/json" });
    downloadBlob(blob, `aws-topology-${state.graph.account_id || "snapshot"}.json`);
  });

  $("importJsonInput").addEventListener("change", async (ev) => {
    const file = ev.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const graph = JSON.parse(text);
      const res = await fetch("/api/layout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph, hide_empty_default_vpcs: state.hideEmptyDefaultVpcs }),
      });
      const data = await res.json();
      applyNewGraphAndLayout(graph, data.layout, true);
      showToast(`Imported topology from ${file.name}`);
    } catch (err) {
      showToast("Invalid topology JSON file.");
    }
    ev.target.value = "";
  });

  // Zoom & Layout HUD
  $("zoomInBtn").addEventListener("click", () => {
    const r = $("viewportSvg").getBoundingClientRect();
    zoomAroundPoint(r.width / 2, r.height / 2, state.cam.scale * 1.25);
  });
  $("zoomOutBtn").addEventListener("click", () => {
    const r = $("viewportSvg").getBoundingClientRect();
    zoomAroundPoint(r.width / 2, r.height / 2, state.cam.scale / 1.25);
  });
  $("fitViewBtn").addEventListener("click", fitDiagramToViewport);
  $("resetLayoutBtn").addEventListener("click", () => {
    state.nodeOverrides.clear();
    renderDiagram();
    fitDiagramToViewport();
    showToast("Reset resource positions to auto-layout");
  });

  // Inspector close
  $("closeInspectorBtn").addEventListener("click", clearSelection);

  // Export Buttons
  $("exportSvgBtn").addEventListener("click", exportSvgDiagram);
  $("exportPngBtn").addEventListener("click", exportPngDiagram);

  // Keyboard shortcuts
  window.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") {
      if (ev.key === "Escape") ev.target.blur();
      return;
    }
    if (ev.key === "/") {
      ev.preventDefault();
      $("searchInput").focus();
    } else if (ev.key === "Escape") {
      clearSelection();
    } else if (ev.key === "f" || ev.key === "F") {
      fitDiagramToViewport();
    } else if (ev.key === "=" || ev.key === "+") {
      $("zoomInBtn").click();
    } else if (ev.key === "-") {
      $("zoomOutBtn").click();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
