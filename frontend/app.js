const API_BASE = '/api';

async function fetchStatus() {
  try {
    const response = await fetch(`${API_BASE}/status`);
    const data = await response.json();
    document.getElementById('active-transfers').textContent = data.active_transfers;
    document.getElementById('queue-size').textContent = data.queue_size;
    document.getElementById('processed-today').textContent = data.processed_today;
    document.getElementById('dashboard-version').textContent = `v${data.version}`;
    cockpitState.version = data.version;
    document.getElementById('connection-status').textContent = 'Connected';
    document.getElementById('connection-status').className = 'badge badge-accept';
  } catch (error) {
    console.error('Failed to fetch status:', error);
    document.getElementById('connection-status').textContent = 'Offline';
    document.getElementById('connection-status').className = 'badge badge-reject';
  }
}

async function fetchStudies() {
  try {
    const response = await fetch(`${API_BASE}/studies`);
    const studies = await response.json();
    const tbody = document.getElementById('study-table-body');
    tbody.innerHTML = '';

    studies.forEach(study => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="font-weight: 600;">${study.patient_name}</td>
        <td style="color: var(--text-muted); font-size: 0.875rem;">${study.protocol}</td>
        <td style="font-family: monospace; font-size: 0.75rem;">${study.series_uid.substring(0, 16)}...</td>
        <td>${study.instance_count}</td>
        <td><span class="badge badge-${study.status.toLowerCase()}">${study.status}</span></td>
        <td>
          <div class="actions-cell">
            <button class="view-btn" onclick="viewStudy('${study.series_uid}')">View Report</button>
            <button class="view-btn" style="background: var(--secondary);" onclick="launchCockpit('${study.series_uid}')">View Scan</button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (error) {
    console.error('Failed to fetch studies:', error);
  }
}

async function viewStudy(seriesUid) {
  try {
    const response = await fetch(`${API_BASE}/studies/${seriesUid}`);
    const result = await response.json();
    
    const modal = document.getElementById('modal');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');

    title.textContent = `QA Report: ${result.patient_name}`;
    
    let flagsHtml = result.flags.map(flag => `
      <div class="flag-item">
        <div class="flag-icon" style="background: var(--${flag.status.toLowerCase()})"></div>
        <div>
          <div style="font-weight: 600;">${flag.name}</div>
          <div style="font-size: 0.875rem; color: var(--text-muted);">${flag.message || ''}</div>
        </div>
      </div>
    `).join('');

    body.innerHTML = `
      <div style="margin-bottom: 2rem; display: flex; justify-content: space-between; align-items: center;">
        <span class="badge badge-${result.status.toLowerCase()}" style="font-size: 1.25rem; padding: 0.5rem 1.5rem;">
          ${result.status}
        </span>
        <div style="text-align: right; color: var(--text-muted); font-size: 0.875rem;">
          <div>Protocol: ${result.protocol}</div>
          <div>UID: ${seriesUid}</div>
        </div>
      </div>
      
      <div class="qa-report-grid">
        <div>
          <h3 style="margin-bottom: 1rem;">Specialist Metrics</h3>
          <p><strong>Truncation:</strong> ${result.metrics.truncation_detected ? 'DETECTED' : 'CLEAR'}</p>
          <p><strong>Bkg Air Noise:</strong> ${result.metrics.background_air_sd.toFixed(2)} HU</p>
          <p><strong>Fluid Density:</strong> ${result.metrics.fluid_median_hu.toFixed(1)} HU</p>
          <p><strong>Gas Volume:</strong> ${result.metrics.gas_volume_cc.toFixed(1)} cc</p>
          <p><strong>Patient Tilt:</strong> ${result.metrics.max_tilt_deg ? result.metrics.max_tilt_deg.toFixed(1) : '0.0'}°</p>
          <p><strong>Slices:</strong> ${result.metrics.slice_count}</p>
        </div>
        <div>
          <h3 style="margin-bottom: 1rem;">Agent Findings</h3>
          ${flagsHtml || '<p style="color: var(--text-muted);">No issues detected.</p>'}
        </div>
      </div>
      <div style="margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); display: flex; gap: 1rem;">
        <a href="${API_BASE}/reports/${seriesUid}/pdf" target="_blank" class="view-btn" style="background: var(--success); text-decoration: none;">Download PDF</a>
        <button class="view-btn" style="background: var(--secondary);" onclick="rerunQA('${seriesUid}')">Re-run Analysis</button>
      </div>
    `;

    modal.style.display = 'flex';
  } catch (error) {
    console.error('Failed to fetch study detail:', error);
    alert('Failed to load report. Please ensure the analysis is complete.');
  }
}

async function rerunQA(seriesUid) {
  try {
    const response = await fetch(`${API_BASE}/validate/${seriesUid}`, { method: 'POST' });
    const data = await response.json();
    alert(data.message);
    closeModal();
    fetchStudies();
  } catch (error) {
    console.error('Failed to re-run QA:', error);
  }
}

// ── Cockpit state ────────────────────────────────────────────────
const cockpitState = {
  seriesUid: null,
  sliceIndex: 0,
  sliceCount: 0,
  wl_presets: {},
  loadTimer: null,
  zoom: 1.0,
  version: '0.0',
};

async function launchCockpit(seriesUid) {
  cockpitState.seriesUid = seriesUid;
  cockpitState.sliceIndex = 0;
  cockpitState.zoom = 1.0;
  _applyCockpitZoom();

  const overlay = document.getElementById('cockpit-overlay');
  overlay.classList.add('open');
  document.getElementById('cockpit-version').textContent = `v${cockpitState.version}`;

  // Disable buttons while loading
  _setCockpitButtonsEnabled(false);

  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/info`);
    if (!res.ok) throw new Error(await res.text());
    const info = await res.json();

    cockpitState.sliceCount = info.slice_count;
    cockpitState.wl_presets = info.wl_presets || {};
    cockpitState.sliceIndex = Math.floor(info.slice_count / 2);

    document.getElementById('cockpit-patient-name').textContent = info.patient_name;
    document.getElementById('cockpit-protocol').textContent = info.protocol;

    // Handle RTSS info
    const rtssSection = document.getElementById('cockpit-rtss-section');
    const refPtArea = document.getElementById('cockpit-ref-pt');
    if (info.has_rtss) {
      rtssSection.style.display = 'block';
      const refPtCoords = document.getElementById('cockpit-ref-pt-coords');
      if (info.reference_point) {
        const rp = info.reference_point;
        refPtCoords.innerHTML = `${rp.name || 'Point'}<br>X: ${rp.x.toFixed(1)}, Y: ${rp.y.toFixed(1)}, Z: ${rp.z.toFixed(1)}`;

        if (info.ref_point_slice_idx !== null && info.ref_point_slice_idx !== undefined) {
          refPtArea.classList.add('clickable');
          refPtArea.onclick = () => jumpToSlice(info.ref_point_slice_idx + 1);
        } else {
          refPtArea.classList.remove('clickable');
          refPtArea.onclick = null;
        }
      } else {
        refPtCoords.textContent = 'None detected';
        refPtArea.classList.remove('clickable');
        refPtArea.onclick = null;
      }
    } else {
      rtssSection.style.display = 'none';
    }

    // Configure nav slider
    const navSlider = document.getElementById('cockpit-nav-slider');
    navSlider.min = 0;
    navSlider.max = Math.max(0, info.slice_count - 1);
    navSlider.value = cockpitState.sliceIndex;

    // Populate W/L preset dropdown
    const select = document.getElementById('cockpit-wl-preset');
    select.innerHTML = '<option value="">Manual</option>';
    for (const [name, vals] of Object.entries(cockpitState.wl_presets)) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      select.appendChild(opt);
    }

    // Render QA flags
    const flagsEl = document.getElementById('cockpit-flags');
    if (info.flags && info.flags.length > 0) {
      const colours = { REJECT: '#ef4444', CONDITIONAL: '#f59e0b', ACCEPT: '#10b981', PASS: '#10b981' };
      flagsEl.innerHTML = info.flags.map(f => {
        // Detect slice indicators like "(Slice 5)" or "(Slices 10-15)"
        const match = f.message ? f.message.match(/\(Slices?\s+(\d+)/) : null;
        const clickable = match ? 'clickable' : '';
        const onclick = match ? `onclick="jumpToSlice(${match[1]})"` : '';

        return `
          <div class="cockpit-flag ${clickable}" ${onclick}>
            <div class="cockpit-flag-dot" style="background:${colours[f.status] || '#94a3b8'}"></div>
            <div>
              <div class="cockpit-flag-name">${f.name}</div>
              <div class="cockpit-flag-msg">${f.message || ''}</div>
            </div>
          </div>
        `;
      }).join('');
    } else {
      flagsEl.innerHTML = '<p style="font-size:0.8rem;color:var(--text-muted);">No issues detected.</p>';
    }

    _setCockpitButtonsEnabled(true);
    refreshCockpitSlice();
  } catch (err) {
    console.error('Cockpit load failed:', err);
    document.getElementById('cockpit-patient-name').textContent = 'Error loading series';
  }
}

function closeCockpit() {
  document.getElementById('cockpit-overlay').classList.remove('open');
  const img = document.getElementById('cockpit-image');
  if (img.src && img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
  img.src = '';
  img.style.transform = 'scale(1)';
  cockpitState.seriesUid = null;
  cockpitState.zoom = 1.0;
}

function jumpToSlice(sliceNum) {
  const idx = parseInt(sliceNum, 10) - 1;
  if (idx >= 0 && idx < cockpitState.sliceCount) {
    cockpitState.sliceIndex = idx;
    refreshCockpitSlice();
  }
}

function refreshCockpitSlice() {
  const { seriesUid, sliceIndex } = cockpitState;
  if (!seriesUid) return;

  const ww = document.getElementById('cockpit-ww').value;
  const wl = document.getElementById('cockpit-wl').value;
  const metal = document.getElementById('cockpit-metal-toggle').checked;
  const mask = document.getElementById('cockpit-mask-toggle').checked;

  // Update slice label
  document.getElementById('cockpit-slice-label').textContent =
    `Slice ${sliceIndex + 1} / ${cockpitState.sliceCount}`;

  // Sync nav slider
  document.getElementById('cockpit-nav-slider').value = sliceIndex;

  const url = `${API_BASE}/viewer/${seriesUid}/slice/${sliceIndex}?ww=${ww}&wl=${wl}&metal=${metal}&mask=${mask}`;
  const loading = document.getElementById('cockpit-loading');
  loading.classList.add('visible');

  const img = document.getElementById('cockpit-image');
  // Use a temporary Image to avoid flicker
  const tmp = new window.Image();
  tmp.onload = () => {
    if (img.src && img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
    img.src = tmp.src;
    loading.classList.remove('visible');
  };
  tmp.onerror = () => loading.classList.remove('visible');
  tmp.src = url;
}

function onCockpitNavSlider() {
  cockpitState.sliceIndex = parseInt(document.getElementById('cockpit-nav-slider').value, 10);
  refreshCockpitSlice();
}

function onCockpitWLChange() {
  document.getElementById('cockpit-wl-preset').value = '';
  document.getElementById('cockpit-ww-val').textContent = document.getElementById('cockpit-ww').value;
  document.getElementById('cockpit-wl-val').textContent = document.getElementById('cockpit-wl').value;
  _debouncedRefresh();
}

function applyCockpitPreset() {
  const name = document.getElementById('cockpit-wl-preset').value;
  if (!name || !cockpitState.wl_presets[name]) return;
  const { window_width, window_level } = cockpitState.wl_presets[name];
  document.getElementById('cockpit-ww').value = window_width;
  document.getElementById('cockpit-wl').value = window_level;
  document.getElementById('cockpit-ww-val').textContent = window_width;
  document.getElementById('cockpit-wl-val').textContent = window_level;
  refreshCockpitSlice();
}

function _debouncedRefresh() {
  clearTimeout(cockpitState.loadTimer);
  cockpitState.loadTimer = setTimeout(refreshCockpitSlice, 120);
}

function _setCockpitButtonsEnabled(enabled) {
  ['cockpit-approve-btn', 'cockpit-reject-btn'].forEach(id => {
    document.getElementById(id).disabled = !enabled;
  });
}

async function cockpitApprove() {
  const { seriesUid } = cockpitState;
  if (!seriesUid) return;
  _setCockpitButtonsEnabled(false);
  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/approve`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      closeCockpit();
      fetchStudies();
    } else {
      alert(data.detail || 'Approval failed');
      _setCockpitButtonsEnabled(true);
    }
  } catch (err) {
    alert('Approval request failed');
    _setCockpitButtonsEnabled(true);
  }
}

async function cockpitReject() {
  const { seriesUid } = cockpitState;
  if (!seriesUid) return;

  if (!confirm("Are you sure you want to REJECT and PERMANENTLY DELETE this series and all its results?")) {
    return;
  }

  _setCockpitButtonsEnabled(false);
  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/reject`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      closeCockpit();
      fetchStudies();
    } else {
      alert(data.detail || 'Rejection failed');
      _setCockpitButtonsEnabled(true);
    }
  } catch (err) {
    alert('Rejection request failed');
    _setCockpitButtonsEnabled(true);
  }
}

// ── Zoom ─────────────────────────────────────────────────────────
function zoomCockpit(delta) {
  cockpitState.zoom = Math.min(4.0, Math.max(0.25, cockpitState.zoom + delta));
  _applyCockpitZoom();
}

function resetCockpitZoom() {
  cockpitState.zoom = 1.0;
  _applyCockpitZoom();
}

function _applyCockpitZoom() {
  const img = document.getElementById('cockpit-image');
  img.style.transform = `scale(${cockpitState.zoom})`;
  img.style.transformOrigin = 'center center';
  document.getElementById('cockpit-zoom-label').textContent = `${Math.round(cockpitState.zoom * 100)}%`;
}

// Mouse-wheel: Ctrl+scroll = zoom, plain scroll = slice nav
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('cockpit-image-pane').addEventListener('wheel', e => {
    e.preventDefault();
    if (!cockpitState.seriesUid) return;
    if (e.ctrlKey || e.metaKey) {
      zoomCockpit(e.deltaY < 0 ? 0.1 : -0.1);
    } else {
      if (e.deltaY > 0) {
        cockpitState.sliceIndex = Math.min(cockpitState.sliceCount - 1, cockpitState.sliceIndex + 1);
      } else {
        cockpitState.sliceIndex = Math.max(0, cockpitState.sliceIndex - 1);
      }
      refreshCockpitSlice();
    }
  }, { passive: false });
});

function closeModal() {
  document.getElementById('modal').style.display = 'none';
}

// Initial fetch and polling
fetchStatus();
fetchStudies();
setInterval(fetchStatus, 5000);
setInterval(fetchStudies, 5000);
