import { firebaseConfig, farmId } from './firebase-config.js';

const $ = id => document.getElementById(id);
const pages = ['overview', 'camera', 'history', 'controls', 'settings'];
const data = { telemetry: {}, settings: {}, photo: null,
  local: Boolean(window.SIFS_LOCAL), demo: !window.SIFS_LOCAL && !firebaseConfig.apiKey };
let auth, database, user, refs, firebaseFns;
let toastTimer;

function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}
function timeLabel(value) {
  if (!value) return 'No readings yet';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}
function setConnection(label, state = '') {
  $('connection').className = `connection ${state}`;
  $('connection').innerHTML = `<i></i>${label}`;
}
function route() {
  const page = pages.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  for (const name of pages) $(name + '-page').classList.toggle('hidden', name !== page);
  document.querySelectorAll('[data-page]').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  $('page-label').textContent = ({ overview: 'Overview', camera: 'Plant camera', history: 'History', controls: 'Controls', settings: 'Settings' })[page];
  if (page === 'history') loadHistory();
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);

function displayTelemetry() {
  const t = data.telemetry;
  $('temperature').textContent = t.temperature ?? '—';
  $('humidity').textContent = t.humidity ?? '—';
  $('soil').textContent = t.soilMoisture ?? (t.soilState ? t.soilState[0].toUpperCase() + t.soilState.slice(1) : '—');
  $('soil-unit').textContent = t.soilMoisture == null ? '' : '%';
  $('co2').textContent = t.co2 ?? '—';
  $('soil-foot').textContent = t.soilState ? `Soil is ${t.soilState}` : 'Waiting for sensor';
  $('co2-foot').textContent = t.errors?.co2 || 'MH-Z19B sensor';
  $('temp-foot').textContent = t.errors?.dht || 'DHT11 sensor';
  $('updated').textContent = t.updatedAt ? `Updated ${timeLabel(t.updatedAt)}` : 'No readings yet';
  $('hero-update').textContent = t.updatedAt ? `Last update ${timeLabel(t.updatedAt)}` : 'Waiting for Raspberry Pi';
  const stale = !t.updatedAt || Date.now() - new Date(t.updatedAt).getTime() > 20000;
  if (data.demo) setConnection('Preview data', 'offline');
  else if (data.local) setConnection(stale ? 'Pi offline' : 'Direct to Pi', stale ? 'offline' : 'online');
  else setConnection(stale ? 'Pi offline' : 'Pi connected', stale ? 'offline' : 'online');
  for (const [name, stateId, modeId] of [['pump', 'pump-state', 'pump-mode'], ['light', 'light-state', 'light-mode']]) {
    const state = $(stateId);
    state.textContent = stale ? 'Offline' : t[name + 'On'] ? 'On' : 'Off';
    state.className = `device-state ${stale ? '' : t[name + 'On'] ? 'on' : 'off'}`;
    $(modeId).textContent = `${(data.settings[name + 'Mode'] || 'auto') === 'auto' ? 'Automatic' : 'Manual'} mode`;
  }
}
setInterval(displayTelemetry, 5000);

function displaySettings() {
  const s = data.settings;
  for (const name of ['pump', 'light']) {
    const mode = s[name + 'Mode'] || 'auto';
    document.querySelectorAll(`[data-mode="${name}"]`).forEach(b => b.classList.toggle('active', b.dataset.value === mode));
    const input = $('manual-' + name);
    input.checked = s['manual' + name[0].toUpperCase() + name.slice(1)] === true;
    input.disabled = mode !== 'manual' || data.demo;
    input.closest('.manual-row').classList.toggle('disabled', mode !== 'manual');
  }
  $('threshold').value = s.co2Threshold ?? 1000;
  $('threshold-display').textContent = s.co2Threshold ?? 1000;
  $('photo-interval').value = s.photoIntervalMinutes ?? 5;
  displayTelemetry();
}
function displayPhoto() {
  const photo = data.photo;
  const html = photo?.image ? `<img src="data:image/jpeg;base64,${photo.image}" alt="Latest plant snapshot">` : '<div class="photo-empty"><span>✿</span><strong>Waiting for the first photo</strong><small>Connect your Pi to start capturing plant images.</small></div>';
  $('overview-photo').innerHTML = html;
  $('camera-image').innerHTML = html;
  $('photo-time').textContent = photo?.capturedAt ? `Captured ${timeLabel(photo.capturedAt)}` : 'No snapshot yet';
  $('camera-time').textContent = $('photo-time').textContent;
  $('camera-source').textContent = photo?.camera ? `Camera ${photo.camera}` : 'Raspberry Pi camera';
}

let historyKind = 'telemetry';
document.querySelectorAll('[data-history]').forEach(button => button.addEventListener('click', () => {
  historyKind = button.dataset.history;
  document.querySelectorAll('[data-history]').forEach(item => item.classList.toggle('active', item === button));
  loadHistory();
}));
$('refresh-history').addEventListener('click', () => loadHistory());

async function loadHistory() {
  const kind = historyKind;
  const labels = { telemetry: ['Sensor readings', 'A timestamped reading is saved every minute'],
    settings: ['Settings changes', 'Every control or schedule change is recorded'],
    photos: ['Plant photos', 'Periodic and requested snapshots'],
    analyses: ['AI analyses', 'Saved results from plant photo analysis'] };
  $('history-title').textContent = labels[kind][0];
  $('history-subtitle').textContent = labels[kind][1];
  const target = $('history-content');
  target.replaceChildren();
  const loading = document.createElement('p'); loading.className = 'history-empty'; loading.textContent = 'Loading history…'; target.append(loading);
  try {
    let rows;
    if (data.local) {
      const response = await localFetch(`/history/${kind}?limit=${kind === 'photos' ? 24 : 100}`);
      if (!response.ok) throw new Error('Unable to load local history');
      rows = await response.json();
    } else if (user && database) {
      const query = firebaseFns.query(firebaseFns.ref(database, `farms/${farmId}/history/${kind}`),
        firebaseFns.limitToLast(kind === 'photos' ? 24 : 100));
      const snapshot = await firebaseFns.get(query);
      rows = Object.values(snapshot.val() || {}).reverse();
    } else { rows = []; }
    if (historyKind !== kind) return;
    renderHistory(kind, rows, target);
  } catch (error) {
    target.replaceChildren();
    const message = document.createElement('p'); message.className = 'history-empty';
    message.textContent = error.message || 'History is unavailable'; target.append(message);
  }
}

function renderHistory(kind, rows, target) {
  target.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('p'); empty.className = 'history-empty';
    empty.textContent = 'No history yet. Records will appear as the system runs.'; target.append(empty); return;
  }
  if (kind === 'photos') {
    const grid = document.createElement('div'); grid.className = 'history-photo-grid';
    for (const row of rows) {
      const item = document.createElement('div'); item.className = 'history-photo';
      const img = document.createElement('img'); img.src = `data:image/jpeg;base64,${row.image}`; img.alt = `Plant photo from ${timeLabel(row.timestamp)}`;
      const caption = document.createElement('span'); caption.textContent = timeLabel(row.timestamp);
      item.append(img, caption); grid.append(item);
    }
    target.append(grid); return;
  }
  if (kind === 'analyses') {
    for (const row of rows) {
      const item = document.createElement('div'); item.className = 'history-analysis';
      const title = document.createElement('strong'); title.textContent = row.crop_type || 'Plant assessment';
      const date = document.createElement('small'); date.textContent = timeLabel(row.timestamp);
      const health = document.createElement('p'); health.textContent = `Health: ${row.health_status || 'uncertain'} · Stage: ${row.growth_stage || 'uncertain'}`;
      const concerns = document.createElement('p'); concerns.textContent = `Concerns: ${(row.visible_issues || []).join(', ') || 'None visible'}`;
      const care = document.createElement('p'); care.textContent = `Care: ${(row.recommendations || []).join('; ') || 'No suggestions'}`;
      item.append(title, date, health, concerns, care); target.append(item);
    }
    return;
  }
  if (kind === 'telemetry') {
    const samples = rows.slice().reverse().filter(row => typeof row.temperature === 'number');
    if (samples.length > 1) {
      const values = samples.map(row => row.temperature), min = Math.min(...values) - 1, max = Math.max(...values) + 1;
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 600 125'); svg.setAttribute('preserveAspectRatio', 'none'); svg.classList.add('history-chart');
      const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      poly.setAttribute('points', values.map((v, i) => `${16 + i * 568 / (values.length - 1)},${108 - (v - min) / (max - min) * 88}`).join(' '));
      svg.append(poly); target.append(svg);
      const summary = document.createElement('p'); summary.className = 'history-summary'; summary.textContent = `Temperature trend · ${samples.length} recent readings`; target.append(summary);
    }
  }
  const columns = kind === 'settings' ? ['Time', 'Setting', 'Value'] : ['Time', 'Temperature', 'Humidity', 'Soil', 'CO₂', 'Pump', 'Light'];
  const wrapper = document.createElement('div'); wrapper.className = 'history-table-wrap';
  const table = document.createElement('table'); table.className = 'history-table';
  const head = document.createElement('thead'), header = document.createElement('tr');
  columns.forEach(label => { const th = document.createElement('th'); th.textContent = label; header.append(th); });
  head.append(header); table.append(head);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const cells = kind === 'settings' ? [timeLabel(row.timestamp), row.key, String(row.value)] :
      [timeLabel(row.timestamp), row.temperature == null ? '—' : `${row.temperature} °C`,
       row.humidity == null ? '—' : `${row.humidity}%`, row.soilState || '—',
       row.co2 == null ? '—' : `${row.co2} ppm`, row.pumpOn ? 'On' : 'Off', row.lightOn ? 'On' : 'Off'];
    const tr = document.createElement('tr');
    cells.forEach(value => { const td = document.createElement('td'); td.textContent = value; tr.append(td); });
    body.append(tr);
  }
  table.append(body); wrapper.append(table); target.append(wrapper);
}

async function save(path, value) {
  if (data.demo) { toast('Connect Firebase to control the Raspberry Pi'); return; }
  if (!data.local && !user) { toast('Sign in first'); return; }
  try {
    if (data.local) {
      const settingsKey = path.startsWith('settings/') ? path.slice(9) : null;
      const response = await localFetch(settingsKey ? '/settings' : '/capture', {
        method: settingsKey ? 'PUT' : 'POST', body: JSON.stringify(settingsKey ? { key: settingsKey, value } : {})
      });
      if (!response.ok) throw new Error((await response.json()).error || 'Local command failed');
      await pollLocal();
    } else if (path.startsWith('settings/')) {
      const key = path.slice(9), timestamp = Date.now();
      await firebaseFns.update(firebaseFns.ref(database, `farms/${farmId}/settings`), { [key]: value, updatedAt: timestamp });
      await firebaseFns.push(firebaseFns.ref(database, `farms/${farmId}/history/settings`), { timestamp, key, value });
    } else {
      await firebaseFns.set(firebaseFns.ref(database, `farms/${farmId}/${path}`), value);
    }
    toast('Saved successfully');
  }
  catch (error) { toast(error.message || 'Could not save'); }
}

document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
  save(`settings/${button.dataset.mode}Mode`, button.dataset.value);
}));
for (const name of ['pump', 'light']) $('manual-' + name).addEventListener('change', event => {
  save(`settings/manual${name[0].toUpperCase()}${name.slice(1)}`, event.target.checked);
});
$('threshold').addEventListener('input', event => $('threshold-display').textContent = event.target.value);
$('save-threshold').addEventListener('click', () => save('settings/co2Threshold', Number($('threshold').value)));
$('save-interval').addEventListener('click', () => save('settings/photoIntervalMinutes', Number($('photo-interval').value)));
$('capture-button').addEventListener('click', () => save('commands/captureRequest', Date.now().toString()));
$('toggle-key').addEventListener('click', () => $('api-key').type = $('api-key').type === 'password' ? 'text' : 'password');
$('save-key').addEventListener('click', () => {
  const key = $('api-key').value.trim();
  if (!key.startsWith('sk-')) { toast('Enter a valid OpenAI API key'); return; }
  sessionStorage.setItem('sifs-openai-key', key);
  $('api-key').value = '';
  $('key-status').textContent = 'Key saved for this browser tab';
  toast('API key saved for this session');
});
if (sessionStorage.getItem('sifs-openai-key')) $('key-status').textContent = 'Key saved for this browser tab';

function addResult(label, value, parent) {
  const item = document.createElement('div'); item.className = 'result-item';
  const small = document.createElement('small'); small.textContent = label;
  const strong = document.createElement('strong'); strong.textContent = value || 'Uncertain';
  item.append(small, strong); parent.append(item);
}
function addTextSection(title, content, parent) {
  const heading = document.createElement('h3'); heading.textContent = title; parent.append(heading);
  if (Array.isArray(content)) {
    const list = document.createElement('ul');
    content.forEach(value => { const li = document.createElement('li'); li.textContent = value; list.append(li); });
    parent.append(list);
  } else { const p = document.createElement('p'); p.textContent = content || 'No observations.'; parent.append(p); }
}
$('analyze-button').addEventListener('click', async () => {
  const key = sessionStorage.getItem('sifs-openai-key');
  if (!key) { location.hash = 'settings'; toast('Add your OpenAI API key in Settings first'); return; }
  if (!data.photo?.image) { toast('Capture a plant photo first'); return; }
  const button = $('analyze-button'); button.disabled = true; button.textContent = 'Analyzing photo…';
  try {
    const response = await fetch('https://api.openai.com/v1/responses', {
      method: 'POST', headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'gpt-4.1-mini', max_output_tokens: 700,
        input: [{ role: 'user', content: [
          { type: 'input_text', text: 'Assess this plant photo for a home grower. Return ONLY valid JSON with keys crop_type (string), growth_stage (string), health_status (string), visible_issues (array of short strings), recommendations (array of short strings), confidence (string: low/medium/high). Identify only what is visually supported. Do not claim a disease diagnosis from an image alone. If uncertain, say so.' },
          { type: 'input_image', image_url: `data:image/jpeg;base64,${data.photo.image}`, detail: 'high' }
        ] }] })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || `OpenAI request failed (${response.status})`);
    const raw = (result.output || []).flatMap(item => item.content || []).filter(item => item.type === 'output_text').map(item => item.text).join('\n');
    const parsed = JSON.parse(raw.replace(/^```(?:json)?\s*|\s*```$/g, ''));
    const target = $('analysis-result'); target.replaceChildren();
    const grid = document.createElement('div'); grid.className = 'result-grid';
    addResult('Crop type', parsed.crop_type, grid);
    addResult('Growth stage', parsed.growth_stage, grid);
    addResult('Visible health', parsed.health_status, grid);
    target.append(grid);
    addTextSection('Visible concerns', parsed.visible_issues, target);
    addTextSection('Suggested care', parsed.recommendations, target);
    addTextSection('Confidence', parsed.confidence, target);
    $('result-card').classList.remove('hidden');
    const saved = { ...parsed, timestamp: Date.now(), photoCapturedAt: data.photo.capturedAt || null };
    if (data.local) {
      const response = await localFetch('/analysis', { method: 'POST', body: JSON.stringify({ result: saved }) });
      if (!response.ok) throw new Error('Analysis succeeded, but local history could not be saved');
    } else if (user) {
      await firebaseFns.push(firebaseFns.ref(database, `farms/${farmId}/history/analyses`), saved);
    }
    toast('Plant analysis complete');
  } catch (error) { toast(error.message || 'Analysis failed'); }
  finally { button.disabled = false; button.textContent = '✦   Analyze photo'; }
});

$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('login-error').textContent = '';
  try {
    if (data.local) {
      sessionStorage.setItem('sifs-local-pin', $('login-password').value);
      await pollLocal();
    } else {
      const result = await firebaseFns.signInWithEmailAndPassword(auth, $('login-email').value, $('login-password').value);
      await firebaseFns.reload(result.user);
      await result.user.getIdToken(true);
      if (!result.user.emailVerified) $('login-error').textContent = 'Verify your email, then sign in again.';
    }
  }
  catch (error) { $('login-error').textContent = error.message || 'Sign-in failed'; }
});
$('create-account').addEventListener('click', async () => {
  $('login-error').textContent = '';
  try {
    const result = await firebaseFns.createUserWithEmailAndPassword(auth, $('login-email').value, $('login-password').value);
    await firebaseFns.sendEmailVerification(result.user);
    await firebaseFns.signOut(auth);
    $('login-error').textContent = 'Account created. Open your email verification link, then sign in.';
  } catch (error) { $('login-error').textContent = error.message || 'Could not create account'; }
});
$('guest-button').addEventListener('click', async () => {
  $('login-error').textContent = '';
  try { await firebaseFns.signInAnonymously(auth); }
  catch (error) { $('login-error').textContent = error.message || 'Guest sign-in failed'; }
});
$('reset-password').addEventListener('click', async () => {
  try {
    await firebaseFns.sendPasswordResetEmail(auth, $('login-email').value);
    $('login-error').textContent = 'If this account exists, a reset email has been sent.';
  } catch (error) { $('login-error').textContent = error.message || 'Could not request reset'; }
});
$('sign-out').addEventListener('click', () => {
  if (data.local) { sessionStorage.removeItem('sifs-local-pin'); $('login-modal').classList.remove('hidden'); }
  else if (auth) firebaseFns.signOut(auth);
});
$('account-button').addEventListener('click', () => { location.hash = 'settings'; });

function localFetch(path, options = {}) {
  return fetch('/api/local' + path, { ...options, headers: { 'Content-Type': 'application/json', 'X-SIFS-PIN': sessionStorage.getItem('sifs-local-pin') || '', ...options.headers } });
}
async function pollLocal() {
  const response = await localFetch('/state');
  if (response.status === 401) {
    $('login-modal').classList.remove('hidden');
    throw new Error('Invalid local PIN');
  }
  if (!response.ok) throw new Error('Pi connection unavailable');
  const snapshot = await response.json();
  const justConnected = !$('login-modal').classList.contains('hidden');
  data.telemetry = snapshot.telemetry || {};
  data.settings = snapshot.settings || {};
  data.photo = snapshot.photo || null;
  $('login-modal').classList.add('hidden');
  $('account-detail').textContent = 'Direct local connection to Raspberry Pi';
  displayTelemetry(); displaySettings(); displayPhoto();
  if (justConnected && location.hash === '#history') loadHistory();
}
async function connectLocal() {
  $('login-modal').classList.remove('hidden');
  $('login-modal').querySelector('h2').textContent = 'Connect to your Pi';
  $('login-modal').querySelector('p').textContent = 'Enter the local access PIN configured on the Raspberry Pi.';
  $('login-email').closest('label').classList.add('hidden');
  $('login-email').required = false;
  $('login-password').closest('label').firstChild.textContent = 'Local PIN';
  $('login-password').placeholder = 'Local access PIN';
  $('login-modal').querySelector('.auth-links').classList.add('hidden');
  $('guest-button').classList.add('hidden');
  $('guest-note').classList.add('hidden');
  $('login-form').querySelector('button[type="submit"]').textContent = 'Connect';
  if (sessionStorage.getItem('sifs-local-pin')) {
    try { await pollLocal(); } catch (_) { sessionStorage.removeItem('sifs-local-pin'); }
  }
  setInterval(() => {
    if (sessionStorage.getItem('sifs-local-pin')) pollLocal().catch(() => setConnection('Pi offline', 'offline'));
  }, 2000);
}
async function connectFirebase() {
  if (data.local) return connectLocal();
  if (data.demo) {
    data.settings = { pumpMode: 'auto', lightMode: 'auto', co2Threshold: 1000, photoIntervalMinutes: 5 };
    data.telemetry = { temperature: 24.6, humidity: 64, soilState: 'moist', co2: 780,
      pumpOn: false, lightOn: false, updatedAt: new Date().toISOString() };
    displaySettings(); displayPhoto();
    $('account-detail').textContent = 'Preview mode · Firebase project setup required for live control.';
    $('sign-out').classList.add('hidden');
    return;
  }
  const [{ initializeApp }, authModule, dbModule] = await Promise.all([
    import('https://www.gstatic.com/firebasejs/13.1.0/firebase-app.js'),
    import('https://www.gstatic.com/firebasejs/13.1.0/firebase-auth.js'),
    import('https://www.gstatic.com/firebasejs/13.1.0/firebase-database.js')
  ]);
  firebaseFns = { ...authModule, ...dbModule };
  const app = initializeApp(firebaseConfig);
  auth = authModule.getAuth(app);
  database = dbModule.getDatabase(app);
  authModule.onIdTokenChanged(auth, nextUser => {
    if (refs) { refs.forEach(unsubscribe => unsubscribe()); refs = null; }
    user = nextUser;
    const signedIn = Boolean(user?.isAnonymous || user?.emailVerified);
    $('login-modal').classList.toggle('hidden', signedIn);
    if (!signedIn) { setConnection(user ? 'Verify email' : 'Sign in required', 'offline'); return; }
    $('account-detail').textContent = user.isAnonymous ? 'Anonymous session' : user.email;
    displaySettings();
    const base = `farms/${farmId}`;
    refs = [
      dbModule.onValue(dbModule.ref(database, `${base}/telemetry`), snap => { data.telemetry = snap.val() || {}; displayTelemetry(); }, () => setConnection('Access denied', 'offline')),
      dbModule.onValue(dbModule.ref(database, `${base}/settings`), snap => { data.settings = snap.val() || {}; displaySettings(); }),
      dbModule.onValue(dbModule.ref(database, `${base}/photos/latest`), snap => { data.photo = snap.val(); displayPhoto(); })
    ];
    if (location.hash === '#history') loadHistory();
  });
}
route();
connectFirebase().catch(error => { console.error(error); setConnection('Setup needed', 'offline'); toast('Firebase connection failed'); });
