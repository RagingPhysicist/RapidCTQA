const API_BASE = '/api';

async function fetchStatus() {
  try {
    const response = await fetch(`${API_BASE}/status`);
    const data = await response.json();
    document.getElementById('active-transfers').textContent = data.active_transfers;
    document.getElementById('queue-size').textContent = data.queue_size;
    document.getElementById('processed-today').textContent = data.processed_today;
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
            <button class="view-btn" style="background: var(--secondary);" onclick="launchCockpit('${study.series_uid}')">Cockpit</button>
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

async function launchCockpit(seriesUid) {
  try {
    const response = await fetch(`${API_BASE}/launch_cockpit/${seriesUid}`, { method: 'POST' });
    const data = await response.json();
    if (!response.ok) {
        alert(data.detail || 'Failed to launch cockpit');
    }
  } catch (error) {
    console.error('Failed to launch cockpit:', error);
    alert('Failed to launch cockpit. Is the backend running?');
  }
}

function closeModal() {
  document.getElementById('modal').style.display = 'none';
}

// Initial fetch and polling
fetchStatus();
fetchStudies();
setInterval(fetchStatus, 5000);
setInterval(fetchStudies, 5000);
