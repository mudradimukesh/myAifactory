import { openClaudeLogin } from './claude-login.js';

(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const content = $('#page-content');
  const pageNames = { overview: 'Overview', project: 'Project setup', connections: 'Connections', budgets: 'Budgets' };
  const roles = [
    ['business', 'Business', 'Owns approved business requirements', 'inspector'], ['domain', 'Domain', 'Owns domain rules', 'inspector'],
    ['architect', 'Architect', 'Owns ticket progress', 'inspector'], ['developer', 'Developer', 'Implements isolated changes', 'developer'],
    ['reviewer', 'Reviewer', 'Checks the candidate', 'reviewer'], ['tester', 'Tester', 'Plans independent checks', 'inspector'],
    ['coordinator', 'Coordinator', 'Plans work and manages handoffs', 'coordinator'],
  ];
  const state = { csrf: '', data: null, page: 'overview', selectedRun: null, refreshing: false, controlBusy: null };
  const answerDrafts = new Map();

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }
  function append(parent, ...children) { children.forEach(child => child && parent.append(child)); return parent; }
  function setStatus(message, kind = 'info') {
    const region = $('#toast-region');
    region.replaceChildren();
    if (!message) return;
    const toast = node('div', `toast ${kind}`, message);
    region.append(toast);
    if (kind === 'success') window.setTimeout(() => { if (toast.isConnected) toast.remove(); }, 4500);
  }
  function escDate(value) {
    if (!value) return 'Not recorded';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  }
  function number(value) { return value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? 'Unknown' : new Intl.NumberFormat().format(Number(value)); }
  function duration(ms) {
    if (!Number.isFinite(Number(ms)) || Number(ms) < 0) return 'Unknown';
    const seconds = Math.floor(Number(ms) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }
  function statusClass(value) { return String(value || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '-'); }
  function badge(value) { return node('span', `badge ${statusClass(value)}`, String(value || 'unknown').replaceAll('_', ' ')); }
  function field(labelText, control, help) {
    const wrap = node('label', 'field');
    append(wrap, node('span', 'field-label', labelText), control);
    if (help) wrap.append(node('span', 'field-help', help));
    return wrap;
  }
  function input(name, value, type = 'text', attrs = {}) {
    const el = node('input', 'input');
    el.name = name;
    el.type = type;
    if (value !== undefined && value !== null) el.value = String(value);
    if (attrs.placeholder) el.placeholder = attrs.placeholder;
    if (attrs.required) el.required = true;
    if (attrs.min !== undefined) el.min = String(attrs.min);
    if (attrs.max !== undefined) el.max = String(attrs.max);
    if (attrs.step !== undefined) el.step = String(attrs.step);
    if (attrs.autocomplete) el.autocomplete = attrs.autocomplete;
    if (attrs.rows) el.rows = attrs.rows;
    return el;
  }
  function textarea(name, value, rows = 6, placeholder = '') {
    const el = node('textarea', 'input textarea');
    el.name = name;
    el.rows = rows;
    el.value = value || '';
    el.placeholder = placeholder;
    return el;
  }
  function select(name, value, choices) {
    const el = node('select', 'input select');
    el.name = name;
    for (const [optionValue, label] of choices) {
      const option = node('option', '', label);
      option.value = optionValue;
      option.selected = optionValue === value;
      el.append(option);
    }
    return el;
  }
  function button(label, kind = 'secondary', action) {
    const el = node('button', `button ${kind}`, label);
    el.type = 'button';
    if (action) el.addEventListener('click', action);
    return el;
  }
  function sectionHeader(eyebrow, title, description, action) {
    const head = node('div', 'section-header');
    const copy = node('div');
    append(copy, node('div', 'eyebrow', eyebrow), node('h1', '', title), node('p', 'section-description', description));
    append(head, copy, action);
    return head;
  }
  function card(title, subtitle, className = '') {
    const panel = node('section', `panel ${className}`.trim());
    const head = node('div', 'panel-head');
    append(head, node('div', 'panel-title', title), subtitle ? node('div', 'panel-subtitle', subtitle) : null);
    panel.append(head);
    return panel;
  }
  function submitRow(label = 'Save changes') {
    const row = node('div', 'form-footer');
    const status = node('span', 'form-status');
    const submit = node('button', 'button primary', label);
    submit.type = 'submit';
    append(row, status, submit);
    return { row, status, submit };
  }
  function settings() { return state.data?.settings || {}; }
  function showPage(page) {
    if (!pageNames[page]) return;
    state.page = page;
    $('#page-title').textContent = pageNames[page];
    document.querySelectorAll('.nav-link').forEach(link => {
      const active = link.dataset.page === page;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    if (!state.data) return;
    render();
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (state.csrf) headers.set('X-CSRF-Token', state.csrf);
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    const type = response.headers.get('content-type') || '';
    const body = type.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) {
      const message = typeof body === 'object' && body ? [body.error || body.message, body.recommendation].filter(Boolean).join(' ') : body;
      const error = new Error(message || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }
  async function loadData({ quiet = false } = {}) {
    if (state.refreshing) return;
    state.refreshing = true;
    if (!quiet && !state.data) content.setAttribute('aria-busy', 'true');
    try {
      const data = await api('/api/dashboard');
      const { generatedAt: previousTime, ...previous } = state.data || {};
      const { generatedAt: currentTime, ...current } = data;
      const unchanged = JSON.stringify(previous) === JSON.stringify(current);
      const priorSelection = state.selectedRun;
      state.data = data;
      const runs = Array.isArray(data.runs) ? data.runs : [];
      if (!runs.some(run => run.id === priorSelection)) state.selectedRun = runs[0]?.id || null;
      const execution = data.capabilities?.execution === true;
      $('#runtime-status').textContent = execution ? 'Execution controls on' : 'Monitoring only';
      $('.status-pill').classList.toggle('monitoring', !execution);
      state.lastRefreshed = data.generatedAt || new Date().toISOString();
      state.refreshError = null;
      $('.refresh-problem', content)?.remove();
      if (state.page === 'overview' && (!unchanged || state.needsRender || !quiet) && (!quiet || !isInteractiveFocus())) {
        state.needsRender = false;
        renderOverview();
      } else if (state.page === 'overview' && (!unchanged || state.needsRender)) state.needsRender = true;
      else if (!quiet) render();
      content.setAttribute('aria-busy', 'false');
      if (!quiet) setStatus('Workspace refreshed', 'success');
    } catch (error) {
      content.setAttribute('aria-busy', 'false');
      if (!state.data) renderLoadError(error);
      else if (quiet) { state.refreshError = error.message; showRefreshProblem(); }
      else setStatus(error.message, 'error');
    } finally { state.refreshing = false; }
  }
  function isInteractiveFocus() {
    const active = document.activeElement;
    return Boolean(active && content.contains(active) && active.matches('button, a, input, textarea, select') && !active.closest('.factory-bar'));
  }
  function refocus(key) {
    const target = key && $(`[data-focus-key="${key}"]`);
    if (target) (target.disabled ? target.closest('.factory-bar') : target).focus();
  }
  function showRefreshProblem() {
    if (!state.refreshError || state.page !== 'overview') return;
    const overview = $('.overview-page', content);
    if (!overview || $('.refresh-problem', overview)) return;
    const problem = node('div', 'refresh-problem');
    append(problem, node('span', '', `Updates paused. Last refreshed ${escDate(state.lastRefreshed)}. ${state.refreshError}`), button('Retry', 'secondary small', () => loadData()));
    overview.prepend(problem);
  }
  async function initialize() {
    try {
      const session = await api('/api/session');
      state.csrf = session.csrfToken;
      await loadData();
    } catch (error) { renderLoadError(error); }
  }
  function renderLoadError(error) {
    content.replaceChildren();
    const panel = node('section', 'load-error');
    append(panel, node('span', 'error-mark', '!'), node('h1', '', 'We could not load this workspace'), node('p', '', error.message || 'Check that the local dashboard server is running.'), button('Try again', 'primary', initialize));
    content.append(panel);
    content.setAttribute('aria-busy', 'false');
  }
  function render() {
    if (state.page === 'overview') renderOverview();
    if (state.page === 'project') renderProject();
    if (state.page === 'connections') renderConnections();
    if (state.page === 'budgets') renderBudgets();
  }

  function metric(label, value, detail, icon) {
    const item = node('article', 'metric-card');
    append(item, node('span', 'metric-icon', icon), node('span', 'metric-label', label), node('strong', 'metric-value', value), node('span', 'metric-detail', detail));
    return item;
  }
  function roleNode([key, name, detail, slot], data, index) {
    const selected = (data.runs || []).find(run => run.id === state.selectedRun);
    const selectedRole = selected?.roles?.find(role => role.role === key);
    const configured = data.settings?.models?.[slot] || {};
    const model = selected ? selectedRole?.model || 'Model unavailable' : configured.model || slot;
    const provider = selected ? selectedRole?.provider || 'Provider unavailable' : configured.provider || 'codex';
    const person = node('div', `role-card role-${key}`);
    const avatar = node('span', `role-avatar ${key}`, { business: 'B', domain: 'Dm', architect: 'A', developer: 'D', reviewer: 'R', tester: 'T', coordinator: 'C' }[key]);
    const info = node('div', 'role-info');
    append(info, node('strong', '', name), node('span', '', detail));
    const tag = node('span', 'model-tag', `${provider} · ${model}`);
    const live = Boolean(selected?.factory?.supervisor && selected.factory.state === 'running' && (selected.attempts || []).some(attempt => attempt.role === key && attempt.status === 'running'));
    append(person, avatar, info, node('span', `badge role-status ${live ? 'running' : ''}`, live ? 'Live' : 'Idle'), tag);
    if (index > 0) person.dataset.indented = 'true';
    return person;
  }
  function renderOverview() {
    if (!state.data) return;
    const focusKey = document.activeElement?.dataset?.focusKey;
    content.replaceChildren();
    const data = state.data;
    const runs = Array.isArray(data.runs) ? data.runs : [];
    const issuesByKey = new Map();
    for (const issue of (Array.isArray(data.issues) ? data.issues : [])) issuesByKey.set(`${issue.code || issue.message}:${issue.runId || ''}`, issue);
    for (const run of runs) for (const issue of (Array.isArray(run.issues) ? run.issues : [])) issuesByKey.set(`${issue.code || issue.message}:${issue.runId || run.id}`, { ...issue, runId: issue.runId || run.id });
    const issues = [...issuesByKey.values()];
    const selectedRun = runs.find(run => run.id === state.selectedRun) || runs[0];
    if (selectedRun) issues.sort((left, right) => Number(right.runId === selectedRun.id) - Number(left.runId === selectedRun.id));
    const totalAttempts = runs.reduce((sum, run) => sum + (Array.isArray(run.attempts) ? run.attempts.length : Number(run.attemptCount) || 0), 0);
    const tokens = runs.reduce((sum, run) => sum + (Number(run.reportedTokens) || 0), 0);
    const overview = node('div', 'overview-page');
    append(overview, sectionHeader('WORKSPACE OVERVIEW', 'Control room', 'A clear view of saved work, configured limits, and the next useful step.', button('Refresh', 'secondary', () => loadData())));
    const metrics = node('div', 'metrics-grid');
    append(metrics,
      metric('Tracked runs', number(runs.length), 'Saved run records', '◫'),
      metric('Recorded attempts', number(totalAttempts), 'Across all saved runs', '↗'),
      metric('Reported tokens', number(tokens), 'Metered: uncached input + output + cache reads at 1/10', '◈'),
      metric('Open recommendations', number(issues.length), issues.length ? 'Review the suggested next steps' : 'No flagged issues', '✳'));
    overview.append(metrics);

    const mainGrid = node('div', 'overview-grid');
    const activity = card('Run activity', 'Select a run to inspect its attempts and saved event history.', 'run-panel');
    const runList = node('div', 'run-list');
    if (!runs.length) {
      const empty = node('div', 'empty-state');
      append(empty, node('span', 'empty-art', '◌'), node('h2', '', 'Your workspace is ready to set up'), node('p', '', 'Add a repository and brief to prepare the project profile. Saved run records will appear here when the coordinator creates them.'), button('Set up project', 'primary', () => navigate('project')));
      activity.append(empty);
    } else {
      for (const run of runs) runList.append(runRow(run));
      activity.append(runList);
      const selected = runs.find(run => run.id === state.selectedRun) || runs[0];
      state.selectedRun = selected.id;
      activity.append(runDetail(selected));
    }
    const rail = node('div', 'overview-rail');
    const org = card('Your team', selectedRun ? 'Models and live status for the selected run.' : 'Configured for future runs. Saved runs may use different models.', 'team-panel');
    const teamList = node('div', 'team-list');
    roles.forEach((role, index) => teamList.append(roleNode(role, data, index)));
    org.append(teamList);
    org.append(node('p', 'panel-note', 'Business, domain, architect, and tester cards use the configured Inspector model slot.'));
    const caveat = node('p', 'panel-note', 'These roles describe the workflow. The control bar of the selected run shows whether its supervisor and worker are running.');
    org.append(caveat);
    rail.append(org);
    const issueCard = card('Recommended next steps', `${issues.length} item${issues.length === 1 ? '' : 's'} to review`, 'recommendation-panel');
    issueCard.append(issueList(issues));
    rail.append(issueCard);
    append(mainGrid, activity, rail);
    overview.append(mainGrid);
    content.replaceChildren(overview);
    content.setAttribute('aria-busy', 'false');
    refocus(focusKey);
  }
  function runRow(run) {
    const selected = run.id === state.selectedRun;
    const row = node('button', `run-row${selected ? ' selected' : ''}`);
    row.type = 'button';
    row.setAttribute('aria-pressed', String(selected));
    row.addEventListener('click', () => { state.selectedRun = run.id; renderOverview(); });
    const identity = node('span', 'run-identity');
    append(identity, node('strong', '', run.id));
    const stateCol = node('span', 'run-state');
    append(stateCol, badge(run.status), node('small', '', `Updated ${escDate(run.updatedAt || run.lastUpdated)}`));
    append(row, identity, stateCol, node('span', 'run-arrow', '›'));
    return row;
  }
  function runDetail(run) {
    const detail = node('div', 'run-detail');
    const head = node('div', 'detail-heading');
    const title = node('div');
    append(title, node('span', 'eyebrow', 'SELECTED RUN'), node('h2', '', run.id));
    const actions = node('div', 'detail-actions');
    actions.append(button('Recovery packet', 'secondary small', () => downloadRecovery(run.id)));
    append(head, title, actions);
    append(detail, head, factoryBar(run));
    const stats = node('div', 'run-stat-strip');
    const attemptCount = Array.isArray(run.attempts) ? run.attempts.length : Number(run.attemptCount) || 0;
    const passedChecks = Number(run.checks?.passed);
    const totalChecks = Number(run.checks?.total);
    const maxTokens = run.limits?.maxReportedTokens;
    const tokenRemaining = run.unknownUsage || !Number.isFinite(Number(maxTokens)) ? 'Unknown' : number(Math.max(0, Number(maxTokens) - Number(run.reportedTokens || 0)));
    const maxAttempts = run.limits?.maxAttempts;
    const attemptsRemaining = Number.isFinite(Number(maxAttempts)) ? number(Math.max(0, Number(maxAttempts) - attemptCount)) : 'Unknown';
    append(stats, smallStat('Attempts left', attemptsRemaining), smallStat('Tokens left', tokenRemaining), smallStat('Checks passed', Number.isFinite(passedChecks) && Number.isFinite(totalChecks) ? `${passedChecks} / ${totalChecks}` : 'Unknown'), smallStat('Elapsed', duration(run.elapsedMs)));
    detail.append(stats, tokenPanel(run));
    const coordinator = run.coordinator || {};
    const coordinatorProvider = coordinator.provider;
    const coordinatorModel = coordinator.model;
    if (coordinatorProvider || coordinatorModel) detail.append(node('p', 'run-model-note', `Coordinator for this run: ${coordinatorProvider || 'Provider unknown'} · ${coordinatorModel || 'Model unknown'}`));
    if (run.candidate) detail.append(node('p', 'candidate-note', `Candidate ${String(run.candidate).slice(0, 12)}`));
    if (run.unknownUsage) detail.append(node('p', 'inline-warning', 'Token usage is incomplete. Unknown usage is not counted as zero.'));
    detail.append(questionSection(run), specificationSection(run), ticketSection(run));
    const lower = node('div', 'history-grid');
    const attempts = node('div', 'history-column');
    attempts.append(node('h3', '', 'Attempts'));
    const attemptItems = Array.isArray(run.attempts) ? run.attempts : [];
    if (!attemptItems.length) attempts.append(node('p', 'muted', 'No attempt details recorded.'));
    for (const attempt of attemptItems.slice().reverse()) attempts.append(historyItem(attempt, 'attempt'));
    const events = node('div', 'history-column');
    events.append(node('h3', '', 'Event history'));
    const eventItems = Array.isArray(run.events) ? run.events : [];
    if (!eventItems.length) events.append(node('p', 'muted', 'No event history recorded.'));
    for (const event of eventItems.slice(-8).reverse()) events.append(historyItem(event, 'event'));
    append(lower, attempts, events);
    detail.append(lower);
    return detail;
  }
  function workflowSection(title, description) {
    const section = node('section', 'workflow-section');
    append(section, node('h3', '', title), node('p', 'workflow-description', description));
    return section;
  }
  function specificationSection(run) {
    const section = workflowSection('Approved specifications', 'Business and domain decisions approved for this run.');
    if (!run.specifications?.length) section.append(node('p', 'muted', 'No approved specifications recorded.'));
    for (const spec of run.specifications || []) {
      const disclosure = node('details', 'specification');
      append(disclosure, node('summary', '', `${spec.kind === 'business' ? 'Business Analyst' : 'Domain Architect'}: ${spec.title} · revision ${spec.revision}`),
        node('p', 'workflow-meta', `Approved by ${spec.approval.owner} · ${escDate(spec.approval.approvedAt)}`),
        node('p', '', spec.approval.statement), node('pre', 'specification-content', spec.content),
        node('p', 'workflow-meta', `Requirements: ${spec.requirements.join(', ') || 'None recorded'}`),
        node('p', 'workflow-meta', `Digest: ${spec.digest}`));
      section.append(disclosure);
    }
    return section;
  }
  function ticketSection(run) {
    const section = workflowSection('GitHub tickets', 'The architect records progress against worker evidence. Sync publishes these records to GitHub.');
    if (!run.tickets?.length) section.append(node('p', 'muted', 'No architect-owned tickets recorded.'));
    for (const ticket of run.tickets || []) {
      const item = node('article', 'ticket');
      const heading = node('div', 'ticket-heading');
      append(heading, node('h4', '', ticket.title), badge(ticket.status));
      append(item, heading, node('p', '', ticket.architectNote || 'No architect progress note recorded.'),
        node('p', 'workflow-meta', `Requirements: ${ticket.requirements.join(', ') || 'None'} · Specifications: ${ticket.specificationIds.join(', ') || 'None'}`),
        node('p', 'workflow-meta', `Worker attempts: ${ticket.attemptIds.join(', ') || 'None recorded'} · Updated ${escDate(ticket.updatedAt)}`));
      if (ticket.github && /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/issues\/\d+$/.test(ticket.github.url)) {
        const link = node('a', 'text-link', `Open GitHub issue #${ticket.github.number}`);
        link.href = ticket.github.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        append(item, link, node('p', 'workflow-meta', `Last synced ${escDate(ticket.github.syncedAt)}`));
      } else item.append(node('p', 'workflow-meta', 'Not synced to GitHub.'));
      section.append(item);
    }
    if (run.tickets?.length) {
      const status = node('p', 'form-status');
      status.setAttribute('role', 'status');
      const sync = button('Sync tickets to GitHub', 'secondary small', async () => {
        sync.disabled = true;
        status.textContent = 'Syncing tickets…';
        try {
          const result = await api(`/api/runs/${encodeURIComponent(run.id)}/tickets/sync`, { method: 'POST', body: JSON.stringify({ expectedRevision: run.revision }) });
          setStatus(result.message || 'Tickets synced to GitHub.', 'success');
          await loadData();
        } catch (error) { status.textContent = error.message; status.className = 'form-status error-text'; }
        finally { sync.disabled = false; }
      });
      append(section, sync, status);
    }
    return section;
  }
  function questionSection(run) {
    const section = workflowSection('Consequential questions', 'Review the impact and recommendation, then send the complete batch to the factory.');
    if (!run.questionBatches?.length) section.append(node('p', 'muted', 'No consequential questions recorded.'));
    for (const batch of run.questionBatches || []) {
      const form = node(batch.answer ? 'details' : 'form', 'question-batch');
      if (batch.answer) form.append(node('summary', '', `${batch.title} · answered by ${batch.answer.owner}`));
      if (!batch.answer) append(form, node('h4', '', batch.title), badge('awaiting_input'));
      const key = `${run.id}/${batch.id}`;
      const canAnswer = !['cancelled', 'failed', 'handoff_ready'].includes(run.status) && !batch.questions.some(question => question.visualEvidenceStale);
      const draft = answerDrafts.get(key) || { owner: '', answers: {}, expectedRevision: run.revision };
      if (!batch.answer) answerDrafts.set(key, draft);
      for (const question of batch.questions) {
        const group = node('fieldset', 'question');
        append(group, node('legend', '', question.prompt), node('p', '', `Impact: ${question.impact}`),
          node('p', '', `Recommendation: ${question.recommendation}`),
          node('p', 'workflow-meta', `Decision owner: ${question.owner} · Affected tickets: ${question.affectedTicketIds.join(', ') || 'None'}`));
        if (question.visualEvidence?.length) {
          group.append(node('p', 'visual-intro', `Review ${question.visualEvidence.length} render${question.visualEvidence.length === 1 ? '' : 's'}. Open any image for its full size.`));
          const gallery = node('div', 'visual-gallery');
          for (const evidence of question.visualEvidence) {
            const figure = node('figure', 'visual-item');
            const link = node('a', 'visual-image-link');
            link.href = evidence.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.setAttribute('aria-label', `Open full image: ${evidence.label}, source ${evidence.sourceId}`);
            const thumbnail = node('img', 'visual-thumbnail');
            thumbnail.src = evidence.url;
            thumbnail.alt = `${evidence.label}, source ${evidence.sourceId}`;
            thumbnail.loading = 'lazy';
            thumbnail.decoding = 'async';
            link.append(thumbnail);
            append(figure, link, node('figcaption', 'visual-caption', `${evidence.label} · Source ${evidence.sourceId}`));
            gallery.append(figure);
          }
          group.append(gallery);
        }
        if (question.visualEvidenceStale) group.append(node('p', 'inline-warning', 'These images belong to an earlier candidate. Review the current run before answering.'));
        if (batch.answer) group.append(node('p', 'saved-answer', batch.answer.answers.find(answer => answer.questionId === question.id)?.value || 'No answer recorded.'));
        else {
          const optionLabel = option => option === 'approve' ? 'Approve renders' : option === 'request_changes' ? 'Request changes' : option;
          const answer = question.options.length
            ? select(question.id, draft.answers[question.id] || '', [['', 'Choose an answer'], ...question.options.map(option => [option, optionLabel(option)])])
            : textarea(question.id, draft.answers[question.id], 3, 'Enter your answer');
          answer.required = true;
          answer.disabled = !canAnswer;
          answer.maxLength = 10000;
          answer.addEventListener('input', () => { draft.answers[question.id] = answer.value; });
          group.append(field('Your answer', answer));
        }
        form.append(group);
      }
      if (batch.answer) form.append(node('p', 'workflow-meta', `Sent by ${batch.answer.owner} · ${escDate(batch.answer.answeredAt)}. Answers are saved for the coordinator; this does not confirm execution resumed.`));
      else {
        const owner = input('owner', draft.owner, 'text', { required: true });
        owner.maxLength = 300;
        owner.disabled = !canAnswer;
        owner.addEventListener('input', () => { draft.owner = owner.value; });
        form.append(field('Your name', owner));
        const footer = submitRow('Send answers to factory');
        footer.submit.disabled = !canAnswer;
        if (!canAnswer) footer.status.textContent = 'This run no longer accepts answers.';
        footer.status.setAttribute('role', 'status');
        form.append(footer.row);
        form.addEventListener('submit', async event => {
          event.preventDefault();
          if (!form.reportValidity()) return;
          footer.submit.disabled = true;
          footer.status.textContent = 'Sending answers…';
          try {
            const result = await api(`/api/runs/${encodeURIComponent(run.id)}/answers`, { method: 'POST', body: JSON.stringify({
              expectedRevision: draft.expectedRevision, batchId: batch.id, owner: draft.owner,
              answers: batch.questions.map(question => ({ questionId: question.id, value: draft.answers[question.id] || '' })),
            }) });
            answerDrafts.delete(key);
            setStatus(result.message || 'Answers saved for the factory coordinator.', 'success');
            state.needsRender = true;
            await loadData();
            render();
          } catch (error) {
            footer.status.textContent = error.message;
            footer.status.className = 'form-status error-text';
            if (error.status === 409 && !$('.review-latest', form)) {
              form.append(button('Refresh and review latest run', 'secondary small review-latest', async () => {
                await loadData();
                const latest = state.data.runs.find(item => item.id === run.id);
                if (latest) draft.expectedRevision = latest.revision;
                render();
              }));
            }
          } finally { footer.submit.disabled = !canAnswer; }
        });
      }
      section.append(form);
    }
    return section;
  }
  function smallStat(label, value) {
    const item = node('div', 'run-stat');
    append(item, node('span', '', label), node('strong', '', value));
    return item;
  }
  function historyItem(item, kind) {
    const row = node('div', 'history-item');
    const marker = node('span', `timeline-marker ${kind}`);
    const copy = node('div', 'history-copy');
    const title = kind === 'attempt' ? `${item.role || 'Attempt'} · ${item.status || 'unknown'}` : item.name || item.kind || item.type || item.status || 'Event';
    append(copy, node('strong', '', String(title).replaceAll('_', ' ')), node('small', '', escDate(item.startedAt || item.at || item.timestamp || item.createdAt)));
    if (item.id) copy.append(node('small', '', `ID ${item.id}`));
    const model = item.model || item.choice || {};
    const modelLabel = [model.provider, model.model].filter(Boolean).join(' · ');
    if (modelLabel) copy.append(node('small', '', modelLabel));
    if (kind === 'attempt') copy.append(node('small', '', `Input ${number(item.inputTokens)} · Output ${number(item.outputTokens)} tokens`));
    const reason = item.reason || item.result?.reason || item.message || safeSummary(item.detail);
    if (reason) copy.append(node('span', 'history-reason', reason));
    append(row, marker, copy, item.status ? badge(item.status) : null);
    return row;
  }
  function tokenPanel(run) {
    const panel = node('section', 'token-panel');
    panel.setAttribute('aria-label', 'Token usage');
    panel.append(node('h3', '', 'Token usage'));
    const totalText = (value, lower) => value == null ? lower > 0 ? `≥ ${number(lower)}` : 'Unknown' : number(value);
    const totalMetric = (label, value, lower, className = 'token-total-item') => {
      const item = node('span', className);
      append(item, node('span', 'token-total-label', label), node('span', 'token-total-value', totalText(value, lower)));
      return item;
    };
    for (const group of run.usageByRole || []) {
      const role = group.role.charAt(0).toUpperCase() + group.role.slice(1).replaceAll('_', ' ');
      const total = node('div', 'token-row token-total');
      const summary = node('span', 'token-summary');
      for (const [key, label] of [['input', 'Input'], ['cached', 'Cached'], ['output', 'Output']])
        summary.append(totalMetric(label, group.total[key], group.lowerBound[key]));
      append(total, node('strong', '', `${role} total`), summary, totalMetric('Metered (budget)', group.total.metered, group.lowerBound.metered, 'token-metered'));
      panel.append(total);
      for (const agent of group.agents) {
        const row = node('div', 'token-row');
        const label = node('div', 'token-agent');
        append(label, node('strong', '', agent.segment == null ? 'No segment usage recorded' : `Segment ${agent.segment} - ${agent.kind === 'handoff' ? 'hand-off' : 'work'}`),
          node('small', '', agent.attemptId), node('small', '', `Model ${agent.model}`), badge(agent.status));
        if (agent.reason && agent.reason !== agent.status) label.append(node('small', '', agent.reason));
        if (agent.lastActivityAt) label.append(node('small', '', `Last activity ${duration(Math.max(0, Date.now() - Date.parse(agent.lastActivityAt)))} ago`));
        else if (agent.live && !agent.providerStartedAt && agent.raw === null && agent.input === null && agent.output === null && agent.cached === null) label.append(node('small', '', 'Waiting for provider start'));
        const fields = node('dl', 'token-fields');
        if (agent.raw === null) fields.append(node('span', '', 'Provider usage Unknown'));
        else for (const [key, value] of Object.entries(agent.raw)) {
          const field = node('div', 'token-field');
          append(field, node('dt', '', key), node('dd', '', number(value))); fields.append(field);
        }
        append(row, label, fields, node('strong', 'token-metered', `Metered (budget) ${number(agent.metered)}`));
        if (agent.live && agent.context) {
          const context = agent.context, block = node('div', 'token-context');
          append(block, node('span', '', `Max context before compaction ${number(context.contextMax)} · Hand-off trigger ${number(context.trigger)}`),
            node('span', '', `Current context ${number(context.contextTokens)} · Peak ${number(context.peakContext)}`));
          const progress = node('progress', 'token-progress');
          progress.max = 1;
          if (context.progress !== null) progress.value = Math.min(1, Math.max(0, context.progress));
          progress.setAttribute('aria-label', 'Current context toward hand-off trigger');
          append(block, progress, node('small', '', context.progress === null ? 'Context progress Unknown' : `${number(Math.round(context.progress * 100))}% of hand-off trigger`));
          row.append(block);
        }
        panel.append(row);
      }
    }
    if (!run.usageByRole?.length) panel.append(node('p', 'muted', 'No worker usage recorded.'));
    return panel;
  }
  function safeSummary(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object') {
      const summary = value.reason || value.message || value.next || value.action;
      if (typeof summary === 'string') return summary;
    }
    return '';
  }
  function issueList(issues) {
    const list = node('div', 'issue-list');
    if (!issues.length) {
      const clear = node('div', 'all-clear');
      append(clear, node('span', 'all-clear-mark', '✓'), node('strong', '', 'No recommendations right now'), node('span', '', 'The dashboard will surface useful follow-up when saved evidence points to one.'));
      list.append(clear);
      return list;
    }
    issues.forEach(issue => {
      const item = node('article', `issue-item ${statusClass(issue.severity || issue.level || 'info')}`);
      const top = node('div', 'issue-top');
      append(top, node('span', 'issue-icon', issue.severity === 'error' ? '!' : 'i'), badge(issue.severity));
      append(item, top, node('h3', '', issue.message), node('p', '', issue.recommendation));
      if (issue.runId) item.append(node('span', 'issue-source', `Run ${issue.runId}`));
      list.append(item);
    });
    return list;
  }

  async function downloadRecovery(runId) {
    try {
      const packet = await api(`/api/runs/${encodeURIComponent(runId)}/recovery`);
      const blob = new Blob([JSON.stringify(packet, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = node('a');
      link.href = url;
      link.download = `factory-recovery-${runId}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setStatus('Recovery packet downloaded. It records saved evidence and does not restart work.', 'success');
    } catch (error) { setStatus(error.message, 'error'); }
  }
  const factoryText = {
    idle: 'No supervisor is running, so no work executes.',
    starting: 'Launching the supervisor.',
    pausing: 'Waiting for the supervisor to freeze the worker.',
    paused: 'Worker processes are stopped with SIGSTOP. No new work starts. A long pause can break the provider connection, and the attempt then records as failed.',
    resuming: 'Waiting for the supervisor to resume the worker.',
    stopping: 'Stopping worker process groups.',
  };
  const fastPollStates = ['starting', 'pausing', 'resuming', 'stopping'];
  function factoryBar(run) {
    const factory = run.factory;
    const busy = state.controlBusy?.run === run.id ? state.controlBusy.state : null;
    const current = busy || factory.state;
    const workRunning = current === 'running' && Boolean(factory.activeJob || factory.supervisor);
    const operatorState = run.status === 'awaiting_input' ? 'awaiting_input' : run.status === 'failed' ? 'failed' : current;
    const stall = (run.issues || []).find(issue => issue.code === 'stall_start' || issue.code === 'stall_idle');
    const job = factory.activeJob;
    const bar = node('div', 'factory-bar');
    bar.tabIndex = -1;
    bar.dataset.focusKey = `bar:${run.id}`;
    bar.setAttribute('aria-busy', String(Boolean(busy)));
    const status = node('div', 'factory-status');
    status.setAttribute('role', 'status');
    const text = run.status === 'awaiting_input' ? `Awaiting input${stall ? ` after ${stall.code}.` : '.'}` : run.status === 'failed' ? `${String(stall?.code || 'Failed').replaceAll('_', ' ')}.` : current === 'running' ? (job ? `Running the ${job.kind} job since ${escDate(job.startedAt)}.` : 'The supervisor is running. No worker job is active.')
      : current === 'exited' ? factory.reason || 'The supervisor exited.'
      : current === 'terminal' ? `This run is ${String(run.status).replaceAll('_', ' ')}.`
      : factoryText[current];
    const meta = [
      factory.supervisor && `Supervisor pid ${factory.supervisor.pid}, launched ${escDate(factory.supervisor.launchedAt)}`,
      current !== 'running' && job && `${job.kind} job since ${escDate(job.startedAt)}${job.frozen ? ', frozen' : ''}`,
      current !== 'exited' && factory.reason,
    ].filter(Boolean).join(' · ');
    append(status, badge(operatorState === 'terminal' ? 'Finished' : operatorState), node('span', 'factory-text', text), meta ? node('span', 'factory-meta', meta) : null);
    if (factory.orphans.length) status.append(node('span', 'factory-warning', `Processes ${factory.orphans.join(', ')} outlived their job leader. The factory cannot prove it owns them, so it does not signal them. Inspect them with ps.`));
    if (factory.activity && factory.activity.length) {
      const age = factory.activity[factory.activity.length - 1].ageSeconds;
      const activity = node('ul', 'activity');
      for (const entry of factory.activity) activity.append(node('li', '', `${entry.job}: ${entry.summary}`));
      status.append(node('small', 'activity-title', `Worker activity${age === null ? '' : `, last write ${duration(age * 1000)} ago`}`), activity);
    }
    const clarification = factory.clarification || {};
    for (const side of ['developer', 'tester']) {
      const view = clarification[side];
      if (!view || !view.items.length) continue;
      const list = node('ul', 'clarification');
      for (const item of view.items) {
        const verdict = item.verdict ? ` - ${item.verdict}${item.correction ? `: ${item.correction}` : ''}` : ' - awaiting answer';
        list.append(node('li', '', `${item.question} (assumption: ${item.assumption})${verdict}`));
      }
      status.append(node('small', 'clarification-title', `${side === 'developer' ? 'Developer' : 'Tester'} clarification, round ${view.round}${view.admitted ? ', admitted' : ''}`), list);
    }
    const actions = node('div', 'factory-actions');
    append(actions,
      controlButton(run, 'start', factory.startLabel, 'primary', factory.canStart && !busy),
      controlButton(run, 'pause', 'Pause', 'secondary', factory.canPause && workRunning && !busy),
      controlButton(run, 'reset', 'Reset', 'secondary', factory.canReset && !busy),
      controlButton(run, 'stop', 'Stop', 'secondary stop', factory.canStop && !busy));
    append(bar, status, actions);
    return bar;
  }
  function controlButton(run, action, label, kind, enabled) {
    const el = button(label, kind, () => action === 'stop' ? confirmStop(run) : action === 'reset' ? confirmReset(run) : controlRun(run, action));
    el.dataset.focusKey = `${action}:${run.id}`;
    el.disabled = !enabled;
    return el;
  }
  function confirmReset(run) {
    const dialog = node('dialog', 'modal');
    const title = node('h2', '', `Reset ${run.id}?`); title.id = 'reset-title'; dialog.setAttribute('aria-labelledby', 'reset-title');
    const body = node('div', 'modal-body');
    const keep = button('Keep run', 'secondary', () => dialog.close()); keep.autofocus = true;
    const reset = button('Reset run', 'danger', () => { dialog.close(); controlRun(run, 'reset'); });
    append(body, title, node('p', '', 'Reset clears live execution records and returns this run to Ready. Attempts and history remain.'), node('div', 'modal-actions'));
    append(body.lastChild, keep, reset); dialog.append(body);
    dialog.addEventListener('close', () => { dialog.remove(); refocus(`reset:${run.id}`); }); document.body.append(dialog); dialog.showModal();
  }
  function confirmStop(run) {
    const dialog = node('dialog', 'modal');
    dialog.setAttribute('aria-labelledby', 'stop-title');
    const title = node('h2', '', `Stop ${run.id}?`);
    title.id = 'stop-title';
    const keep = button('Keep run', 'secondary', () => dialog.close());
    keep.autofocus = true;
    const stop = button('Stop run', 'danger', () => { dialog.close(); controlRun(run, 'stop'); });
    const body = node('div', 'modal-body');
    const actions = node('div', 'modal-actions');
    append(actions, keep, stop);
    append(body, title, node('p', '', 'Stop cancels this run permanently and ends its worker processes. A stopped run cannot resume.'),
      node('p', '', 'To continue this work, create a new run from the same profile.'), actions);
    dialog.append(body);
    dialog.addEventListener('close', () => { dialog.remove(); refocus(`stop:${run.id}`); });
    document.body.append(dialog);
    dialog.showModal();
  }
  async function controlRun(run, action) {
    const focusKey = `${action}:${run.id}`;
    const busyState = { start: run.factory.startLabel === 'Resume' ? 'resuming' : 'starting', pause: 'pausing', reset: 'starting', stop: 'stopping' }[action];
    state.controlBusy = { run: run.id, state: busyState };
    renderOverview();
    refocus(focusKey);
    try {
      const result = await api(`/api/runs/${encodeURIComponent(run.id)}/control`, {
        method: 'POST', body: JSON.stringify(action === 'start' ? { action, expectedRevision: run.revision } : { action }),
      });
      setStatus(result.message, 'success');
    } catch (error) { setStatus(error.message, 'error'); }
    finally {
      state.controlBusy = null;
      state.needsRender = true;
      await loadData({ quiet: true });
      refocus(focusKey);
    }
  }
  function schedulePoll() {
    const fast = state.controlBusy || state.data?.runs?.some(run => fastPollStates.includes(run.factory.state));
    window.setTimeout(async () => {
      if (state.page === 'overview' && document.visibilityState === 'visible') await loadData({ quiet: true });
      schedulePoll();
    }, fast ? 1000 : 5000);
  }

  function navigate(page) {
    state.page = page;
    history.pushState({ page }, '', `#${page}`);
    showPage(page);
  }
  function renderProject() {
    content.replaceChildren();
    const s = settings();
    const page = node('div', 'settings-page');
    append(page, sectionHeader('PROJECT PROFILE', 'Project setup', 'Give the coordinator the repository, approved brief, and the person who receives a completed handoff.'));
    const panel = card('Project details', 'These settings are stored locally with this dashboard.', 'settings-panel');
    const form = node('form', 'settings-form');
    append(form,
      field('GitHub repository URL', input('repositoryUrl', s.repositoryUrl, 'url', { placeholder: 'https://github.com/team/project', required: false }), 'Use a public GitHub URL without embedded credentials. This setting does not grant repository access.'),
      field('Project brief', textarea('brief', s.brief, 8, 'Describe the outcome, users, constraints, and acceptance examples.'), 'Keep product decisions and expected behavior clear. The dashboard does not approve a brief for you.'),
      field('Handoff recipient', input('recipient', s.recipient, 'text', { placeholder: 'Name or team' }), 'The named person or team responsible for reviewing the finished handoff.'));
    const modelTitle = node('div', 'subsection-heading');
    append(modelTitle, node('h2', '', 'Role models'), node('p', '', 'Choose the provider, model, and effort used for each role.'));
    const modelGrid = node('div', 'model-grid');
    for (const [key, roleName, detail] of [['coordinator', 'Coordinator', 'Plans and routes work'], ['developer', 'Developer', 'Implements changes'], ['reviewer', 'Reviewer', 'Reviews the candidate'], ['inspector', 'Inspector', 'Runs independent checks']]) {
      const configured = s.models?.[key] || {};
      const modelCard = node('div', 'model-card');
      append(modelCard, node('strong', '', roleName), node('span', 'model-description', detail));
      modelCard.append(field('Provider', select(`${key}Provider`, configured.provider || 'codex', [['codex', 'Codex'], ['claude', 'Claude Code']])));
      modelCard.append(field('Model', input(`${key}Model`, configured.model || ({ coordinator: 'gpt-6-astra', developer: 'gpt-6-sol', reviewer: 'gpt-6-sol', inspector: 'gpt-6-luna' })[key], 'text', { required: true })));
      modelCard.append(field('Effort', select(`${key}Effort`, configured.effort || 'medium', [['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Extra high']])));
      modelGrid.append(modelCard);
    }
    append(form, modelTitle, modelGrid);
    const footer = submitRow('Save project profile');
    form.append(footer.row);
    form.addEventListener('submit', event => saveSettings(event, form, footer));
    panel.append(form);
    page.append(panel);
    const note = node('div', 'info-callout');
    append(note, node('span', 'callout-icon', 'i'), node('p', '', 'This profile prepares project context only. Saving it does not start a worker, fetch a repository, or approve a release.'));
    page.append(note);
    content.append(page);
  }
  function renderConnections() {
    content.replaceChildren();
    const s = settings();
    const page = node('div', 'settings-page');
    append(page, sectionHeader('ACCESS & PROVIDERS', 'Connections', 'Configure dedicated provider credential locations and store secrets in private local files.'));
    const providerPanel = card('Provider sign-in', 'Sign in with each provider CLI, then configure its dedicated credential location.', 'settings-panel');
    const form = node('form', 'settings-form');
    form.append(providerInfo('Codex', s.authHomes?.codex ? 'Dedicated location configured' : 'Needs setup', 'Sign in with codex login, then configure a dedicated credentials-only directory. Keep your normal Codex configuration intact.'));
    const codexCheck = button('Check credentials', 'secondary small');
    codexCheck.addEventListener('click', () => checkAuth('codex', codexCheck));
    form.append(codexCheck);
    form.append(field('Dedicated Codex auth directory', input('codexHome', s.authHomes?.codex, 'text', { placeholder: '/Users/you/.factory-auth/codex' }), 'A path only. The file check does not test live login or model access.'));
    const claudeInfo = providerInfo('Claude Code', 'Loading sign-in status', 'Sign-in happens on Claude\'s own page. The factory never asks for your password, and the credential file stays in a private folder on this Mac.');
    const claudeStatus = $('.connection-indicator', claudeInfo);
    const claudeMessage = node('p', 'provider-status');
    claudeInfo.append(claudeMessage);
    const claudeHome = input('claudeHome', s.authHomes?.claude, 'text', { placeholder: '/Users/you/.factory-auth/claude' });
    const showClaude = view => {
      claudeStatus.textContent = { signed_out: 'Signed out', awaiting_code: 'Waiting for code', verifying: 'Verifying', ready: 'Ready', failed: 'Sign-in failed' }[view.status];
      claudeStatus.className = `connection-indicator ${statusClass(view.status)}`;
      claudeMessage.textContent = [view.message, view.account?.subscriptionType && `Plan ${view.account.subscriptionType}`, view.checkedAt && `Checked ${escDate(view.checkedAt)}`].filter(Boolean).join(' · ');
      if (view.status === 'ready' && !claudeHome.value) claudeHome.value = view.authHome;
    };
    const loadClaude = async (check, trigger) => {
      if (trigger) trigger.disabled = true;
      try { showClaude(await api(`/api/auth/claude${check ? '?check=1' : ''}`)); }
      catch (error) { claudeStatus.textContent = 'Status unavailable'; claudeMessage.textContent = error.message; }
      finally { if (trigger) trigger.disabled = false; }
    };
    const claudeActions = node('div', 'provider-actions');
    const claudeCheck = button('Check', 'secondary small', () => loadClaude(true, claudeCheck));
    append(claudeActions, button('Sign in to Claude', 'secondary small', () => openClaudeLogin({ api, settings: settings(), onDone: (view, saved) => {
      if (saved) { state.data.settings = saved; claudeHome.value = saved.authHomes.claude; }
      if (view) showClaude(view);
    } })), claudeCheck);
    append(form, claudeInfo, claudeActions);
    loadClaude(false);
    form.append(field('Dedicated Claude auth directory', claudeHome, 'Sign-in fills this folder when it is empty. Never paste a password, token, or credential JSON here.'));
    const doc = node('p', 'field-help');
    const anchor = node('a', 'text-link', 'Claude authentication details');
    anchor.href = 'https://code.claude.com/docs/en/authentication';
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    doc.append(anchor);
    form.append(doc);
    const footer = submitRow('Save provider locations');
    form.append(footer.row);
    form.addEventListener('submit', event => saveSettings(event, form, footer));
    providerPanel.append(form);
    page.append(providerPanel);

    const credentials = state.data.credentials || {};
    const appPanel = card('Application keys', 'Values are stored in private files managed by the local dashboard.', 'settings-panel');
    const appKeys = Array.isArray(credentials.applicationKeys) ? credentials.applicationKeys : [];
    if (appKeys.length) {
      const saved = node('div', 'saved-keys');
      appKeys.forEach(name => saved.append(savedKeyRow('application', name, `Application · ${name}`)));
      appPanel.append(saved);
    } else appPanel.append(node('p', 'empty-keys', 'No application keys saved.'));
    appPanel.append(secretControl('Application key', 'application', 'Use a lowercase name with letters, digits, and hyphens. The value is masked and will not be displayed again.'));
    appPanel.append(node('p', 'security-note', 'Stored on this machine in files restricted to your operating-system account.'));
    page.append(appPanel);

    const githubPanel = card('GitHub access', credentials.github ? 'Secret saved' : 'No secret saved', 'connection-status-panel');
    append(githubPanel, node('p', '', 'The repository URL identifies project context only. This dashboard does not test a GitHub token, clone code, create pull requests, or push changes.'));
    githubPanel.append(secretControl('GitHub secret', 'github', 'Stored in a private local file. It is not shown again and does not enable repository operations here.'));
    if (credentials.github) githubPanel.append(savedKeyRow('github', '', 'GitHub secret'));
    page.append(githubPanel);
    content.append(page);
  }
  function providerInfo(name, status, guide) {
    const block = node('div', 'provider-info');
    const top = node('div', 'provider-top');
    append(top, node('strong', '', name), node('span', 'connection-indicator', status));
    append(block, top, node('p', '', guide));
    return block;
  }
  function secretControl(label, kind, helpText) {
    const wrap = node('div', 'secret-control');
    const copy = node('div', 'secret-copy');
    append(copy, node('strong', '', label), node('span', '', helpText));
    const fieldWrap = node('div', 'secret-entry');
    const name = kind === 'application' ? input('applicationName', '', 'text', { placeholder: 'Application name' }) : null;
    const secret = input('secret', '', 'password', { placeholder: 'Enter secret value', autocomplete: 'new-password' });
    secret.setAttribute('autocomplete', 'new-password');
    const save = button('Save secret', 'secondary small');
    save.addEventListener('click', async () => {
      if (!secret.value || (name && !name.value.trim())) { setStatus(name ? 'Enter an application name and key.' : 'Enter a GitHub secret.', 'error'); return; }
      save.disabled = true;
      try {
        const payload = { kind, value: secret.value };
        if (name) payload.name = name.value.trim();
        await api('/api/credentials', { method: 'PUT', body: JSON.stringify(payload) });
        secret.value = '';
        if (name) name.value = '';
        setStatus('Secret saved to a private local file. The value is not displayed again.', 'success');
        await loadData({ quiet: true });
        if (state.page === 'connections') renderConnections();
      } catch (error) { setStatus(error.message, 'error'); }
      finally { save.disabled = false; }
    });
    append(fieldWrap, name, secret, save);
    append(wrap, copy, fieldWrap);
    return wrap;
  }
  function savedKeyRow(kind, name, label) {
    const row = node('div', 'saved-key-row');
    append(row, node('span', 'saved-key-dot', '•'), node('strong', '', label), node('span', 'masked-value', '••••••••'));
    const remove = button('Remove', 'quiet small');
    remove.addEventListener('click', async () => {
      try {
        const payload = { kind, value: '' };
        if (kind === 'application') payload.name = name;
        await api('/api/credentials', { method: 'PUT', body: JSON.stringify(payload) });
        setStatus('Secret removed from private local storage.', 'success');
        await loadData({ quiet: true });
        if (state.page === 'connections') renderConnections();
      } catch (error) { setStatus(error.message, 'error'); }
    });
    row.append(remove);
    return row;
  }
  async function checkAuth(provider, buttonEl) {
    buttonEl.disabled = true;
    try {
      const result = await api('/api/auth/check', { method: 'POST', body: JSON.stringify({ provider }) });
      if (result.ok) setStatus(result.message || `${provider} credential file shape check passed. Live login and model access were not tested.`, 'success');
      else setStatus([result.message || `${provider} credential file check failed.`, result.recommendation].filter(Boolean).join(' '), 'error');
    } catch (error) { setStatus(error.message, 'error'); }
    finally { buttonEl.disabled = false; }
  }

  function renderBudgets() {
    content.replaceChildren();
    const s = settings();
    const budget = s.budget || {};
    const recovery = s.recovery || {};
    const page = node('div', 'settings-page');
    append(page, sectionHeader('LIMITS & RECOVERY', 'Budgets', 'Set finite run limits and reserve room for independent verification. Unknown provider usage remains unknown.'));
    const panel = card('Run limits', 'These are local guardrails, not a view of your provider subscription quota.', 'settings-panel');
    const form = node('form', 'settings-form budget-form');
    const budgetGrid = node('div', 'form-grid two');
    budgetGrid.append(field('Maximum reported tokens', input('maxReportedTokens', budget.maxReportedTokens, 'number', { min: 1, step: 1, required: true }), 'Configured for future runs. A worker is stopped when it exceeds its share. Unknown usage cannot be treated as zero.'));
    budgetGrid.append(field('Maximum attempts', input('maxAttempts', budget.maxAttempts, 'number', { min: 1, max: 100, step: 1, required: true }), 'Finite total across the run. A retry does not reset this count.'));
    budgetGrid.append(field('Verification reserve attempts', input('verificationReserveAttempts', budget.verificationReserveAttempts, 'number', { min: 1, max: 100, step: 1, required: true }), 'Attempts held for independent review and checks.'));
    budgetGrid.append(field('Maximum run time (minutes)', input('maxWallMinutes', budget.maxWallMinutes, 'number', { min: 1, step: 1, required: true }), 'Total elapsed time allowed for the run.'));
    budgetGrid.append(field('Attempt timeout (minutes)', input('attemptTimeoutMinutes', budget.attemptTimeoutMinutes, 'number', { min: 1, max: 30, step: 1, required: true }), 'Upper time limit for one worker or check attempt.'));
    budgetGrid.append(field('Application budget (USD)', input('applicationBudgetUsd', budget.applicationBudgetUsd, 'number', { min: 0, step: '0.01', required: true }), 'A planned application API allocation. This dashboard does not measure spend. Subscription usage is not priced here.'));
    const attemptPanel = node('div', 'budget-breakdown');
    const dev = Math.max(0, Number(budget.maxAttempts || 0) - Number(budget.verificationReserveAttempts || 0));
    const total = Number(budget.maxAttempts || 0);
    const reservePercent = total ? Math.min(100, (Number(budget.verificationReserveAttempts || 0) / total) * 100) : 0;
    const bar = node('div', 'budget-bar');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', `${dev} implementation and rework attempts, ${number(budget.verificationReserveAttempts || 0)} verification reserve attempts`);
    const work = node('span', 'budget-work');
    work.style.width = `${100 - reservePercent}%`;
    const reserve = node('span', 'budget-reserve');
    reserve.style.width = `${reservePercent}%`;
    append(bar, work, reserve);
    const legend = node('div', 'budget-legend');
    append(legend, legendItem('Implementation & rework', `${dev} attempts`, 'work'), legendItem('Verification reserve', `${number(budget.verificationReserveAttempts || 0)} attempts`, 'reserve'));
    append(attemptPanel, node('strong', '', 'Attempt allocation'), bar, legend);
    const recoveryTitle = node('div', 'subsection-heading');
    append(recoveryTitle, node('h2', '', 'Recovery signals'), node('p', '', 'These thresholds surface records that may need attention. They do not declare a worker stalled.'));
    const recoveryGrid = node('div', 'form-grid three');
    recoveryGrid.append(field('Stale after (minutes)', input('staleMinutes', recovery.staleMinutes, 'number', { min: 1, step: 1, required: true }), 'Age since the last saved event.'));
    recoveryGrid.append(field('Repeated failures', input('maxFailures', recovery.maxFailures, 'number', { min: 1, step: 1, required: true }), 'Surface a recommendation after this many failures.'));
    recoveryGrid.append(field('Context token threshold', input('contextTokenThreshold', recovery.contextTokenThreshold, 'number', { min: 1, step: 1, required: true }), 'Flag reported usage near this threshold.'));
    const footer = submitRow('Save budgets');
    append(form, budgetGrid, attemptPanel, recoveryTitle, recoveryGrid, footer.row);
    form.addEventListener('input', event => {
      if (event.target.name === 'maxAttempts' || event.target.name === 'verificationReserveAttempts') updateBudgetAllocation(form);
    });
    form.addEventListener('submit', event => saveSettings(event, form, footer));
    panel.append(form);
    page.append(panel);
    const costNote = node('div', 'info-callout');
    append(costNote, node('span', 'callout-icon', 'i'), node('p', '', 'Token counts come from provider reports. Any API-equivalent cost estimate is not an invoice and does not represent remaining subscription quota.'));
    page.append(costNote);
    content.append(page);
  }
  function legendItem(label, value, kind) {
    const item = node('span', `legend-item ${kind}`);
    append(item, node('i', ''), node('span', '', label), node('strong', '', value));
    return item;
  }
  function updateBudgetAllocation(form) {
    const maxAttempts = Number(form.elements.namedItem('maxAttempts')?.value);
    const reserveAttempts = Number(form.elements.namedItem('verificationReserveAttempts')?.value);
    if (!Number.isFinite(maxAttempts) || !Number.isFinite(reserveAttempts) || maxAttempts < 0 || reserveAttempts < 0) return;
    const reservePercent = maxAttempts ? Math.min(100, reserveAttempts / maxAttempts * 100) : 0;
    const reserve = $('.budget-reserve', form);
    const work = $('.budget-work', form);
    if (!reserve || !work) return;
    reserve.style.width = `${reservePercent}%`;
    work.style.width = `${100 - reservePercent}%`;
    const available = Math.max(0, maxAttempts - reserveAttempts);
    $('.legend-item.work strong', form).textContent = `${available} attempts`;
    $('.legend-item.reserve strong', form).textContent = `${reserveAttempts} attempts`;
    const bar = $('.budget-bar', form);
    bar.setAttribute('aria-label', `${available} implementation and rework attempts, ${reserveAttempts} verification reserve attempts`);
  }
  async function saveSettings(event, form, footer) {
    event.preventDefault();
    const fields = new FormData(form);
    const old = settings();
    const next = structuredClone(old);
    if (state.page === 'project') {
      next.repositoryUrl = String(fields.get('repositoryUrl') || '').trim();
      next.brief = String(fields.get('brief') || '').trim();
      next.recipient = String(fields.get('recipient') || '').trim();
      next.models = { ...(next.models || {}) };
      for (const key of ['coordinator', 'developer', 'reviewer', 'inspector']) {
        next.models[key] = {
          ...(next.models[key] || {}),
          provider: String(fields.get(`${key}Provider`)),
          model: String(fields.get(`${key}Model`) || '').trim(),
          effort: String(fields.get(`${key}Effort`)),
        };
      }
    }
    if (state.page === 'connections') {
      next.authHomes = { ...(next.authHomes || {}), codex: String(fields.get('codexHome') || '').trim(), claude: String(fields.get('claudeHome') || '').trim() };
    }
    if (state.page === 'budgets') {
      next.budget = { ...(next.budget || {}) };
      for (const key of ['maxReportedTokens', 'maxAttempts', 'verificationReserveAttempts', 'maxWallMinutes', 'attemptTimeoutMinutes', 'applicationBudgetUsd']) {
        const value = Number(fields.get(key));
        if (!Number.isFinite(value) || value < 0 || (key !== 'applicationBudgetUsd' && !Number.isInteger(value))) {
          footer.status.textContent = 'Enter a valid finite number in every budget field.';
          footer.status.className = 'form-status error-text';
          return;
        }
        next.budget[key] = value;
      }
      if (next.budget.verificationReserveAttempts > next.budget.maxAttempts) {
        footer.status.textContent = 'Verification reserve cannot exceed maximum attempts.';
        footer.status.className = 'form-status error-text';
        return;
      }
      next.recovery = { ...(next.recovery || {}) };
      for (const key of ['staleMinutes', 'maxFailures', 'contextTokenThreshold']) {
        const value = Number(fields.get(key));
        if (!Number.isInteger(value) || value < 1) {
          footer.status.textContent = 'Recovery thresholds must be positive whole numbers.';
          footer.status.className = 'form-status error-text';
          return;
        }
        next.recovery[key] = value;
      }
    }
    footer.submit.disabled = true;
    footer.status.textContent = 'Saving…';
    footer.status.className = 'form-status';
    try {
      const result = await api('/api/settings', { method: 'PUT', body: JSON.stringify(next) });
      state.data.settings = result.settings || next;
      if (state.page === 'budgets') updateBudgetAllocation(form);
      footer.status.textContent = result.message || 'Saved to this local workspace.';
      footer.status.className = 'form-status success-text';
      setStatus('Settings saved.', 'success');
      if (state.page === 'overview') renderOverview();
    } catch (error) {
      footer.status.textContent = error.message;
      footer.status.className = 'form-status error-text';
    } finally { footer.submit.disabled = false; }
  }

  document.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', event => {
    event.preventDefault();
    navigate(link.dataset.page);
  }));
  $('#refresh-button').addEventListener('click', () => loadData());
  $('.notice-dismiss').addEventListener('click', () => $('#global-notice').remove());
  window.addEventListener('hashchange', () => showPage(location.hash.slice(1) || 'overview'));
  window.addEventListener('popstate', () => showPage(location.hash.slice(1) || 'overview'));
  schedulePoll();
  showPage(location.hash.slice(1) || 'overview');
  initialize();
})();
