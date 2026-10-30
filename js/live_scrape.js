// ============================================================
// LIVE SCRAPE
// Starts a scrape on the local API server (server.py) and streams
// log lines and matching jobs into the table as each company finishes.
// ============================================================

import { showToast, escape } from './ui_utils.js';
import { updateStats } from './jobs_loader.js';
import { enableMap } from './map_view.js';
import { readFilterInputs } from './filters.js';

const MAX_LOG_LINES = 2000;
// escape() leaves quotes alone, which attribute values need.
const escAttr = s => escape(s).replace(/"/g, '&quot;');
const SEARCH_TEXT_FIELDS = ['title', 'company', 'location', 'ats', 'skill_level', 'exclude', 'include'];
const DISCOVER_KEY = 'liveScrape.discover';
const ROLES_KEY = 'liveScrape.roles';
const DEFAULT_ROLES = ['swe'];
const MAX_DISCOVER = 200;

let picker = null;   // { setChecked } from setupCompanyPicker

/**
 * Show the live-scrape panel and wire it to the API.
 * @param {Object} app - JobBoardApp instance
 * @param {Object} apiData - Response from GET /api/jobs
 */
export async function setupLiveScrape(app, apiData) {
    const panel = document.getElementById('live-scrape');
    const companiesEl = document.getElementById('live-companies');
    const filtersEl = document.getElementById('live-filters');
    const runBtn = document.getElementById('live-run');
    panel.classList.remove('d-none');

    const config = await fetch('/api/config').then(r => r.json());
    writeCompanies(config.default_companies);
    companiesEl.placeholder = 'Leave empty to use the default companies';
    picker = setupCompanyPicker(config);
    setupMyList();
    setupDiscover();
    setupRoles(config.roles);
    filtersEl.innerHTML = Object.entries(config.filters).map(([key, label]) => `
        <div class="form-check form-check-inline">
            <input class="form-check-input live-filter" type="checkbox" id="live-filter-${key}" value="${key}" checked>
            <label class="form-check-label" for="live-filter-${key}">${label}</label>
        </div>`).join('');

    runBtn.addEventListener('click', () => startRun(app));

    // A run was already going when the page loaded: replay its stream from the start.
    if (apiData.running) connect(app, apiData.run_id);
}

function writeCompanies(companies) {
    document.getElementById('live-companies').value = Object.entries(companies)
        .filter(([, slugs]) => slugs.length)
        .map(([platform, slugs]) => `${platform}: ${slugs.join(', ')}`)
        .join('\n');
}

/**
 * Checkbox picker over the companies textarea. The textarea stays the source
 * of truth: ticking a box edits it, and editing it re-ticks the boxes.
 * Shows the curated tech list, plus matches from every company in data/
 * once the search has 2+ characters.
 */
function setupCompanyPicker(config) {
    const companiesEl = document.getElementById('live-companies');
    const searchEl = document.getElementById('live-company-search');
    const pickerEl = document.getElementById('live-company-picker');
    const countEl = document.getElementById('live-picked-count');

    const toItems = companies => Object.entries(companies)
        .flatMap(([platform, slugs]) => slugs.map(slug => ({ platform, slug, label: slug })));
    const key = item => `${item.platform}:${item.slug}`;
    const curated = new Map([...toItems(config.tech_companies), ...toItems(config.default_companies)]
        .map(item => [key(item), item]));
    const labels = new Map();   // paylocity GUID -> company name, from search results
    let searchResults = [];
    let shownTech = [];
    let searchTimer = null;

    const selectedKeys = () => new Set(toItems(parseCompanies(companiesEl.value)).map(key));

    const itemHtml = (item, checked) => `
        <div class="form-check" title="${escAttr(item.platform)}: ${escAttr(item.label)}">
            <input class="form-check-input" type="checkbox" id="pick-${escAttr(key(item))}"
                data-key="${escAttr(key(item))}" ${checked ? 'checked' : ''}>
            <label class="form-check-label" for="pick-${escAttr(key(item))}">
                ${escAttr(labels.get(item.slug) || item.label)}
                <span class="picker-platform">${escAttr(item.platform)}</span>
            </label>
        </div>`;

    const render = () => {
        const q = searchEl.value.trim().toLowerCase();
        const selected = selectedKeys();
        const match = item => !q || item.label.toLowerCase().includes(q) || item.slug.toLowerCase().includes(q);

        const tech = [...curated.values()].filter(match);
        // Picked companies outside the curated list stay visible so they can be unticked.
        const extraPicked = [...selected].filter(k => !curated.has(k))
            .map(k => { const [platform, slug] = k.split(/:(.*)/); return { platform, slug, label: labels.get(slug) || slug }; })
            .filter(match);
        const extraKeys = new Set(extraPicked.map(key));
        const others = q.length >= 2
            ? searchResults.filter(item => !curated.has(key(item)) && !extraKeys.has(key(item)))
            : [];
        shownTech = tech;

        let html = '';
        if (tech.length) html += '<div class="picker-heading">Tech companies</div>' + tech.map(i => itemHtml(i, selected.has(key(i)))).join('');
        if (extraPicked.length) html += '<div class="picker-heading">Also selected</div>' + extraPicked.map(i => itemHtml(i, true)).join('');
        if (others.length) html += '<div class="picker-heading">All companies</div>' + others.map(i => itemHtml(i, false)).join('');
        pickerEl.innerHTML = html || '<div class="picker-heading">No companies match</div>';
        countEl.textContent = selected.size ? `${selected.size} selected` : 'None selected: My list will be used';
    };

    const setChecked = (keys, checked) => {
        const companies = parseCompanies(companiesEl.value);
        for (const k of keys) {
            const [platform, slug] = k.split(/:(.*)/);
            const slugs = companies[platform] ||= [];
            if (checked && !slugs.includes(slug)) slugs.push(slug);
            if (!checked) companies[platform] = slugs.filter(s => s !== slug);
        }
        writeCompanies(companies);
        render();
    };

    pickerEl.addEventListener('change', e => {
        if (e.target.dataset.key) setChecked([e.target.dataset.key], e.target.checked);
    });
    // Only the curated list: a search over all companies can return dozens of unrelated names.
    document.getElementById('live-select-all').addEventListener('click', () => setChecked(shownTech.map(key), true));
    document.getElementById('live-select-none').addEventListener('click', () => { companiesEl.value = ''; render(); });
    companiesEl.addEventListener('input', render);

    searchEl.addEventListener('input', () => {
        clearTimeout(searchTimer);
        const q = searchEl.value.trim();
        if (q.length < 2) { searchResults = []; render(); return; }
        render();
        searchTimer = setTimeout(async () => {
            const res = await fetch(`/api/companies?q=${encodeURIComponent(q)}`);
            if (!res.ok || searchEl.value.trim() !== q) return;
            searchResults = (await res.json()).companies;
            searchResults.forEach(item => { if (item.label !== item.slug) labels.set(item.slug, item.label); });
            render();
        }, 250);
    });

    render();
    return { setChecked };
}

const myListKey = e => `${e.platform}:${e.id}`;

/** ★ My list panel: the saved list Run scrape uses by default. */
function setupMyList() {
    const rowsEl = document.getElementById('my-list-rows');

    rowsEl.addEventListener('click', async e => {
        const btn = e.target.closest('[data-remove]');
        if (!btn) return;
        const [platform, id] = btn.dataset.remove.split(/:(.*)/);
        const res = await fetch(`/api/my-list/${encodeURIComponent(platform)}/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const body = await res.json();
        if (!res.ok) { showToast(body.error || 'Could not remove company.', 'danger'); return; }
        picker.setChecked([btn.dataset.remove], false);   // also drop it from the next scrape
        renderMyList(body.companies);
    });

    document.getElementById('my-list-add').addEventListener('click', async () => {
        const companies = parseCompanies(document.getElementById('live-companies').value);
        if (!Object.keys(companies).length) { showToast('Tick some companies first.', 'warning'); return; }
        const res = await fetch('/api/my-list', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ companies }),
        });
        const body = await res.json();
        if (!res.ok) { showToast(body.error || 'Could not add companies.', 'danger'); return; }
        showToast(`${body.added} companies added to My list.`, 'success');
        renderMyList(body.companies);
    });

    return loadMyList();
}

async function loadMyList() {
    const res = await fetch('/api/my-list');
    const { companies } = await res.json();
    renderMyList(companies);
    return companies;
}

function renderMyList(entries) {
    document.getElementById('my-list-count').textContent = entries.length;
    document.getElementById('my-list-rows').innerHTML = entries.length ? entries.map(e => `
        <div class="my-list-row">
            <span class="my-list-name" title="${escAttr(e.id)}">${escAttr(e.id)}</span>
            <span class="picker-platform">${escAttr(e.platform)}</span>
            <span class="badge ${e.source === 'auto' ? 'text-bg-success' : 'text-bg-secondary'}">${escAttr(e.source)}</span>
            <span class="picker-platform">${e.matched_total} matches</span>
            <span class="picker-platform">${e.last_matched_at ? new Date(e.last_matched_at).toLocaleDateString() : '—'}</span>
            <button type="button" class="btn btn-sm btn-link text-danger p-0" data-remove="${escAttr(myListKey(e))}"
                aria-label="Remove ${escAttr(e.id)} from My list">✕</button>
        </div>`).join('')
        : '<div class="picker-heading">My list is empty</div>';
}

/** Role dropdown: multi-select presets, remembered per browser. */
function setupRoles(roles) {
    const menu = document.getElementById('live-roles');
    let saved = DEFAULT_ROLES;
    try {
        const raw = localStorage.getItem(ROLES_KEY);
        if (raw !== null) saved = JSON.parse(raw);
    } catch { /* storage blocked or bad value: keep the default */ }

    menu.innerHTML = Object.entries(roles).map(([key, label]) => `
        <div class="form-check text-nowrap">
            <input class="form-check-input live-role" type="checkbox" id="live-role-${key}" value="${key}"
                ${saved.includes(key) ? 'checked' : ''}>
            <label class="form-check-label" for="live-role-${key}">${label}</label>
        </div>`).join('')
        + '<div class="form-text mt-1">None ticked = any role</div>';

    const update = () => {
        const picked = readRoles();
        const button = document.getElementById('live-role-button');
        button.textContent = picked.length
            ? `Role: ${roles[picked[0]]}${picked.length > 1 ? ` +${picked.length - 1}` : ''}`
            : 'Role: any';
        try { localStorage.setItem(ROLES_KEY, JSON.stringify(picked)); } catch { /* not remembered */ }
    };
    menu.addEventListener('change', update);
    update();
}

function readRoles() {
    return [...document.querySelectorAll('.live-role:checked')].map(el => el.value);
}

/** "Try N new companies" input, remembered per browser. */
function setupDiscover() {
    const input = document.getElementById('live-discover');
    try {
        const saved = localStorage.getItem(DISCOVER_KEY);
        if (saved !== null) input.value = saved;
    } catch { /* storage blocked: keep the default */ }
    input.addEventListener('change', () => {
        input.value = readDiscover();
        try { localStorage.setItem(DISCOVER_KEY, input.value); } catch { /* not remembered */ }
    });
}

function readDiscover() {
    const n = parseInt(document.getElementById('live-discover').value, 10);
    return Number.isNaN(n) ? 0 : Math.min(MAX_DISCOVER, Math.max(0, n));
}

function parseCompanies(text) {
    const companies = {};
    for (const line of text.split('\n')) {
        const [platform, rest] = line.split(':');
        if (!rest) continue;
        const slugs = rest.split(/[\s,]+/).filter(Boolean);
        if (!slugs.length) continue;
        // Several lines for one platform add up instead of replacing each other.
        const list = companies[platform.trim().toLowerCase()] ||= [];
        for (const slug of slugs) if (!list.includes(slug)) list.push(slug);
    }
    return companies;
}

/**
 * Filters panel values for the server, without empty fields.
 * Status and hide-applied stay browser-only: they read localStorage.
 * Hide-recruiters is on by default, so it also stays browser-only; sending it
 * would make an untouched panel scrape differently from no search at all.
 */
function readSearch() {
    const f = readFilterInputs();
    const search = {};
    for (const key of SEARCH_TEXT_FIELDS) {
        if (f[key]) search[key] = f[key];
    }
    const salary = parseInt(f.salary, 10);
    if (salary > 0) search.salary = salary;
    const posted = parseInt(f.posted, 10);
    if (posted > 0) search.posted = posted;
    if (f.remoteOnly) search.remoteOnly = true;
    return search;
}

async function startRun(app) {
    // An empty box sends no companies, so the server scrapes its default list.
    const parsed = parseCompanies(document.getElementById('live-companies').value);
    const companies = Object.keys(parsed).length ? parsed : undefined;
    const filters = [...document.querySelectorAll('.live-filter:checked')].map(el => el.value);
    const search = readSearch();
    const auto_add = document.getElementById('live-auto-add').checked;
    const discover = readDiscover();
    const roles = readRoles();

    const res = await fetch('/api/scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ companies, filters, search, roles, auto_add, discover }),
    });
    const body = await res.json();
    if (!res.ok) {
        const message = !companies && body.error === 'no companies given'
            ? 'My list is empty; tick companies under Companies (advanced) or try some new companies.'
            : body.error || 'Could not start scrape.';
        showToast(message, 'danger');
        if (res.status === 409) connect(app, body.run_id);
        return;
    }
    connect(app, body.run_id);
}

function myListKeysShown() {
    return [...document.querySelectorAll('#my-list-rows [data-remove]')].map(el => el.dataset.remove);
}

function connect(app, runId) {
    const logEl = document.getElementById('live-log');
    const statusEl = document.getElementById('live-status');
    const runBtn = document.getElementById('live-run');
    let companiesDone = 0;

    logEl.textContent = '';
    app.allJobs = [];
    app.isFullyLoaded = false;
    document.querySelector('.job-table thead')?.classList.add('sorting-locked');
    app.refilter();
    updateStats(app.allJobs);
    runBtn.disabled = true;
    statusEl.textContent = 'Running…';

    const appendLog = (text, cls = '') => {
        const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
        const line = document.createElement('div');
        line.textContent = text;
        if (cls) line.className = cls;
        logEl.appendChild(line);
        while (logEl.childElementCount > MAX_LOG_LINES) logEl.firstChild.remove();
        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
    };

    const source = new EventSource(`/api/scrape/${runId}/events`);

    source.addEventListener('log', e => appendLog(JSON.parse(e.data).line));

    source.addEventListener('company', e => {
        const c = JSON.parse(e.data);
        companiesDone++;
        const ok = c.scraped > 0;
        appendLog(
            `${ok ? '✓' : '✗'} ${c.tag ? `[${c.tag}] ` : ''}${c.platform}/${c.slug}: ${c.scraped} tech jobs → ${c.matched} matched`
            + (ok ? '' : ` (no jobs, HTTP ${c.status ?? 'error'})`)
            + (c.added ? '  ★ added to My list' : ''),
            c.matched ? 'log-match' : (ok ? 'log-company' : 'log-fail'),
        );
        statusEl.textContent = `Running… ${companiesDone} companies done, ${app.allJobs.length} jobs`;
    });

    source.addEventListener('jobs', e => {
        const { jobs } = JSON.parse(e.data);
        for (const job of jobs) {
            const reasons = Object.values(job.filter_reasons || {}).join(' · ');
            appendLog(`   + ${job.company}: ${job.title}${reasons ? `  [${reasons}]` : ''}`, 'log-match');
        }
        app.allJobs.push(...jobs);
        app.refilter();
        updateStats(app.allJobs);
        statusEl.textContent = `Running… ${companiesDone} companies done, ${app.allJobs.length} jobs`;
    });

    source.addEventListener('done', async e => {
        const { matched, error } = JSON.parse(e.data);
        source.close();
        // Refresh My list, and tick companies that were just added so the next run includes them.
        const before = new Set(myListKeysShown());
        const entries = await loadMyList();
        picker.setChecked(entries.map(myListKey).filter(k => !before.has(k)), true);
        app.isFullyLoaded = true;
        document.querySelector('.job-table thead')?.classList.remove('sorting-locked');
        enableMap();
        runBtn.disabled = false;
        statusEl.textContent = error ? `Failed: ${error}` : `Done: ${matched} matching jobs from ${companiesDone} companies`;
        showToast(error ? 'Scrape failed, see log.' : `Scrape finished: ${matched} jobs.`, error ? 'danger' : 'success');
    });

    source.onerror = () => {
        if (source.readyState === EventSource.CLOSED) {
            runBtn.disabled = false;
            statusEl.textContent = 'Lost connection to server.';
        }
    };
}
