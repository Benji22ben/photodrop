const $ = (id) => document.getElementById(id);
let config, files = [], job = null, busy = false, controller = null, generation = 0;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const size = (bytes) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;

function notice(message = '') {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, signal: options.signal || controller?.signal });
  if (!response.ok) {
    let message = `Request failed (${response.status}). Check your connection and try again.`;
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch {}
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.json();
}

function remember() {
  try { if (job) sessionStorage.setItem('photo-drop-job', job.id); else sessionStorage.removeItem('photo-drop-job'); } catch {}
}

function selection() {
  $('selection').hidden = !files.length || !!job;
  $('selection-count').textContent = `${files.length} photo${files.length === 1 ? '' : 's'} selected`;
  $('selection-size').textContent = `${size(files.reduce((sum, f) => sum + f.size, 0))} total`;
  $('convert').disabled = !config || !files.length || busy;
  $('convert').firstChild.textContent = files.length ? `Convert ${files.length} photos to JPG ` : 'Choose photos to get started ';
  $('dropzone').disabled = busy || !!job;
  $('quality').disabled = busy || !!job;
  $('clear').disabled = busy;
  $('convert').hidden = !!job;
  $('file-details').hidden = !files.length && !job;
}

function fileList(items) {
  $('file-summary').textContent = `Your photos (${items.length})`;
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const row = document.createElement('li');
    if (item.status === 'failed') row.className = 'failed';
    const name = document.createElement('span'); name.textContent = item.name;
    const status = document.createElement('span');
    status.textContent = item.error || (item.preservedJpeg ? '✓ Already JPG · preserved' : ({ done: '✓ JPG ready', converting: 'Converting…', uploaded: 'Uploaded', waiting: 'Waiting', uploading: 'Uploading…' }[item.status] || size(item.size)));
    row.append(name, status); fragment.append(row);
  }
  $('file-list').replaceChildren(fragment);
}

function selectFiles(incoming) {
  if (busy || job || !config) return;
  let skipped = 0;
  for (const file of incoming) {
    if (!/\.(heic|heif)$/i.test(file.name) || !file.size || file.size > config.maxFileBytes) { skipped++; continue; }
    files.push(file);
  }
  if (files.length > config.maxFiles || files.reduce((sum, f) => sum + f.size, 0) > config.maxBatchBytes) {
    files = []; notice(`Select at most ${config.maxFiles} photos and ${size(config.maxBatchBytes)} per batch.`);
  } else notice(skipped ? `${skipped} file(s) skipped. Choose non-empty HEIC / HEIF files up to ${size(config.maxFileBytes)} each.` : '');
  selection(); fileList(files);
  $('picker').value = '';
}

function render() {
  if (!job) return;
  $('progress-panel').hidden = false;
  const total = job.files.length;
  const finished = job.files.filter((f) => ['done', 'failed'].includes(f.status)).length;
  const failed = job.files.filter((f) => f.status === 'failed').length;
  const uploaded = job.files.reduce((sum, f) => sum + f.uploaded, 0);
  const bytes = job.files.reduce((sum, f) => sum + f.size, 0);
  let percent = 0, phase = '', detail = '';
  switch (job.state) {
    case 'uploading':
      percent = uploaded / bytes * 100; phase = 'Uploading your photos';
      detail = `${size(uploaded)} of ${size(bytes)} uploaded. Keep this tab open until uploads finish.`; break;
    case 'queued': phase = 'Your batch is in the queue'; detail = 'Conversion will start as soon as a worker is available.'; break;
    case 'converting':
      percent = finished / total * 100; phase = 'Making your JPGs';
      detail = `${finished} of ${total} photos processed${failed ? ` · ${failed} failed` : ''}. You can return to this tab later.`; break;
    case 'zipping': percent = 100; phase = 'Packing your ZIP'; detail = 'Your JPGs are ready. Finishing the download file…'; break;
    case 'done':
      percent = 100; phase = 'Your photos are ready';
      detail = `${total - failed} of ${total} photos converted${failed ? ` · ${failed} failed (details included in the ZIP)` : ''}. Download within ${Math.round(config.retentionSeconds / 60)} minutes.`; break;
    case 'failed': phase = 'This batch could not finish'; detail = job.error; break;
    case 'cancelling': phase = 'Deleting your batch'; detail = 'Cleaning up the uploaded files…'; break;
  }
  $('phase').textContent = phase; $('progress-detail').textContent = detail;
  $('progress').value = percent; $('percentage').textContent = `${Math.floor(percent)}%`;
  $('download').hidden = job.state !== 'done';
  if (job.downloadUrl) $('download').href = job.downloadUrl;
  $('reset').textContent = ['done', 'failed'].includes(job.state) ? 'Delete batch & start again' : 'Cancel & delete batch';
  selection(); fileList(job.files);
}

async function poll(token) {
  let errors = 0;
  while (job && generation === token) {
    try {
      job = await api(`/api/jobs/${job.id}`);
      if (generation !== token) return;
      if (errors) notice('');
      errors = 0; render();
      if (['done', 'failed'].includes(job.state)) { busy = false; return; }
    } catch (error) {
      if (error.name === 'AbortError' || generation !== token) return;
      if (error.status === 404) { notice(error.message); busy = false; return; }
      errors++; notice('Connection interrupted. Reconnecting to your batch…');
    }
    await delay(Math.min(1500 * (errors + 1), 10000));
  }
}

async function uploadOne(index, token) {
  let failures = 0;
  while (generation === token && job.files[index].uploaded < files[index].size) {
    const offset = job.files[index].uploaded;
    const chunk = files[index].slice(offset, offset + config.chunkBytes);
    try {
      const result = await api(`/api/jobs/${job.id}/files/${index}?offset=${offset}`, { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: chunk });
      if (generation !== token) return;
      job.files[index].uploaded = result.uploaded;
      job.files[index].status = result.uploaded === files[index].size ? 'uploaded' : 'uploading';
      failures = 0; render();
    } catch (error) {
      if (error.name === 'AbortError' || generation !== token) throw error;
      if (++failures > 4 || [400, 403, 404, 413, 422, 507].includes(error.status)) throw error;
      await delay(500 * 2 ** failures);
      const current = await api(`/api/jobs/${job.id}`);
      // A response may have been lost after the chunk was saved. Resume at the server's offset.
      job.files[index] = current.files[index];
    }
  }
}

async function run() {
  if (busy) return;
  busy = true; controller = new AbortController(); const token = ++generation;
  notice(''); $('retry').hidden = true; selection();
  try {
    if (!job) {
      job = await api('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quality: Number($('quality').value), files: files.map(({ name, size }) => ({ name, size })) }) });
      remember();
    } else job = await api(`/api/jobs/${job.id}`);
    render();
    let next = 0;
    const uploaders = Array.from({ length: Math.min(3, files.length) }, async () => {
      while (next < files.length && generation === token) { const index = next++; await uploadOne(index, token); }
    });
    // Abort other uploaders on failure, then settle them before allowing a retry.
    try { await Promise.all(uploaders); } catch (error) { controller.abort(); await Promise.allSettled(uploaders); throw error; }
    if (generation !== token) return;
    controller = new AbortController();
    job = await api(`/api/jobs/${job.id}/start`, { method: 'POST' });
    render(); await poll(token);
  } catch (error) {
    if (generation !== token) return;
    controller = null;
    notice(error.name === 'AbortError' ? 'Upload interrupted. Retry to continue.' : error.message);
    $('retry').hidden = !job || files.length !== job.files.length;
    busy = false; selection();
  }
}

async function reset() {
  ++generation; controller?.abort(); controller = null;
  $('reset').disabled = true;
  if (job) {
    try { await api(`/api/jobs/${job.id}`, { method: 'DELETE' }); }
    catch (error) {
      if (error.status !== 404) { notice(error.message); $('reset').disabled = false; busy = false; return; }
    }
  }
  job = null; files = []; busy = false; remember(); notice('');
  $('progress-panel').hidden = true; $('retry').hidden = true; $('reset').disabled = false;
  selection(); fileList([]);
}

$('dropzone').addEventListener('click', () => $('picker').click());
$('picker').addEventListener('change', (event) => selectFiles(event.target.files));
for (const type of ['dragenter', 'dragover']) $('dropzone').addEventListener(type, (event) => { event.preventDefault(); if (!busy && !job) $('dropzone').classList.add('dragging'); });
for (const type of ['dragleave', 'drop']) $('dropzone').addEventListener(type, () => $('dropzone').classList.remove('dragging'));
$('dropzone').addEventListener('drop', (event) => { event.preventDefault(); selectFiles(event.dataTransfer.files); });
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());
$('quality').addEventListener('input', () => { $('quality-value').value = `${$('quality').value}%`; });
$('clear').addEventListener('click', () => { files = []; selection(); fileList([]); notice(''); });
$('convert').addEventListener('click', run);
$('retry').addEventListener('click', run);
$('reset').addEventListener('click', reset);
window.addEventListener('beforeunload', (event) => { if (busy && (!job || job.state === 'uploading')) { event.preventDefault(); event.returnValue = ''; } });

(async () => {
  try {
    config = await api('/api/config');
    $('limits').textContent = `HEIC / HEIF · Up to ${config.maxFiles.toLocaleString()} photos · ${size(config.maxBatchBytes)} per batch`;
    $('privacy').textContent = `Photos are uploaded to this server. Originals are removed as they’re processed; results expire after ${Math.round(config.retentionSeconds / 60)} minutes. You can delete your batch sooner.`;
    let saved; try { saved = sessionStorage.getItem('photo-drop-job'); } catch {}
    if (saved && /^[a-f0-9]{48}$/.test(saved)) {
      job = await api(`/api/jobs/${saved}`); render();
      if (job.state === 'uploading') notice('This upload was interrupted by a page reload. Delete the batch and select your photos again.');
      else { busy = true; await poll(generation); }
    }
  } catch (error) { notice(error.message); }
  selection();
})();
