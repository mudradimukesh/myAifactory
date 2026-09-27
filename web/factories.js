const drafts = new Map();
let selectedFactory;
let selectedRole = 'developer';
let selectedHandoff = '';

function el(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function control(tag, value = '') {
  const item = el(tag, undefined, 'input');
  item.value = value;
  return item;
}
function field(label, item) {
  item.setAttribute('aria-label', label);
  const wrap = el('label', undefined, 'field');
  wrap.append(el('span', label, 'field-label'), item);
  return wrap;
}
function button(label, action, primary = false) {
  const item = el('button', label, `button ${primary ? 'primary' : 'secondary'} small`);
  item.type = 'button';
  item.addEventListener('click', action);
  return item;
}
function panel(title, description) {
  const item = el('section', undefined, 'panel factory-editor-panel');
  item.append(el('h2', title), el('p', description, 'section-description'));
  return item;
}
function options(item, values, current) {
  for (const [value, label] of values) {
    const option = el('option', label);
    option.value = value;
    option.selected = value === current;
    item.append(option);
  }
}
window.addEventListener('beforeunload', event => {
  if ([...drafts.values()].some(draft => draft.dirty)) { event.preventDefault(); event.returnValue = ''; }
});

export async function showObservedSkills(api, runId) {
  const dialog = el('dialog', undefined, 'modal');
  const body = el('div', undefined, 'modal-body');
  const title = el('h2', `Skill reads: ${runId}`);
  title.id = 'skill-reads-title';
  dialog.setAttribute('aria-labelledby', title.id);
  const status = el('p', 'Loading recorded reads…');
  body.append(title, status, button('Close', () => dialog.close()));
  dialog.append(body);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  try {
    const { factory, observed } = await api(`/api/runs/${encodeURIComponent(runId)}/skills`);
    status.textContent = observed.length ? 'Explicit file reads recorded in worker activity. Text comes from this run’s saved snapshot.' : 'No explicit skill reads recorded for this run.';
    for (const agent of observed) {
      for (const read of agent.skills) {
        const detail = el('details', undefined, 'skill-editor');
        detail.append(el('summary', `${agent.role}: ${read.name}`));
        const saved = factory?.agents.find(item => item.role === agent.role)?.skills.find(skill => skill.name === read.name);
        detail.append(el('p', read.path, 'field-help'));
        detail.append(el('pre', saved?.content || 'Saved skill text is unavailable for this run.', 'skill-source'));
        body.append(detail);
      }
    }
  } catch (error) { status.textContent = error.message; }
}

export async function showRunHandoff(api, runId, openFactories) {
  const dialog = el('dialog', undefined, 'modal handoff-dialog');
  const body = el('div', undefined, 'modal-body');
  const title = el('h2', `Run handoff: ${runId}`);
  title.id = 'run-handoff-title';
  dialog.setAttribute('aria-labelledby', title.id);
  const status = el('p', 'Loading handoff…');
  body.append(title, status, button('Close', () => dialog.close()));
  dialog.append(body);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  try {
    const { handoff } = await api(`/api/runs/${encodeURIComponent(runId)}/handoff`);
    status.textContent = `Revision ${handoff.revision} · ${handoff.status} · Updated ${handoff.updatedAt}`;
    body.append(el('p', handoff.objective));
    for (const [key, label] of [['completed', 'Completed'], ['current', 'In progress'], ['difficulties', 'Difficulties'], ['blocked', 'Blocked'], ['next', 'Next steps']]) {
      const section = el('section', undefined, `handoff-section handoff-${key}`);
      section.append(el('h3', label));
      const entries = handoff[key] || [];
      if (!entries.length) section.append(el('p', 'None recorded.'));
      for (const entry of entries) {
        const item = el('div', undefined, 'handoff-entry');
        item.append(el('p', entry.text));
        if (entry.evidence.length) item.append(el('p', `Evidence: ${entry.evidence.join(', ')}`, 'field-help'));
        section.append(item);
      }
      body.append(section);
    }
    if (handoff.changedFiles.length) {
      const files = el('details');
      files.append(el('summary', 'Changed files'), el('pre', handoff.changedFiles.join('\n'), 'skill-source'));
      body.append(files);
    }
    const actions = el('div', undefined, 'factory-editor-actions');
    const download = el('a', 'Download Markdown', 'button secondary small');
    download.href = `/api/runs/${encodeURIComponent(runId)}/handoff.md`;
    download.download = `${runId}-handoff.md`;
    actions.append(download, button('Create factory from this handoff', () => {
      selectedHandoff = runId;
      dialog.close();
      openFactories();
    }, true));
    body.append(actions, el('p', 'Imported handoffs provide context. They do not approve new work or replace verification.', 'field-help'));
  } catch (error) { status.textContent = error.message; }
}

export async function renderFactories(context) {
  const { content, api, settings, onSettings, onRun } = context;
  const page = el('div', undefined, 'settings-page');
  page.append(el('h1', 'Factories'), el('p', 'Create a factory for a problem, then edit the skills of each agent. Saved edits apply to new runs.', 'section-description'));
  content.replaceChildren(page);
  const notice = el('p', 'Loading factories…', 'form-status');
  notice.setAttribute('role', 'status');
  page.append(notice);
  try {
    const [listing, library] = await Promise.all([api('/api/factories'), api('/api/skill-library')]);
    if (!page.isConnected) return;
    notice.textContent = '';
    selectedFactory ||= settings.factoryId || listing.factories[0]?.id;
    if (!listing.factories.some(factory => factory.id === selectedFactory)) selectedFactory = listing.factories[0]?.id;
    const chooser = panel('Factory profile', 'Each factory keeps separate agent skills.');
    const factorySelect = control('select');
    options(factorySelect, listing.factories.map(factory => [factory.id, factory.name]), selectedFactory);
    factorySelect.addEventListener('change', () => { selectedFactory = factorySelect.value; renderFactories(context); });
    if (listing.factories.length) chooser.append(field('Factory', factorySelect));
    const use = button(settings.factoryId === selectedFactory ? 'Selected for new runs' : 'Use for new runs', async () => {
      use.disabled = true;
      try {
        const result = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ ...settings, factoryId: selectedFactory }) });
        onSettings(result.settings);
        context.settings = result.settings;
        renderFactories(context);
      } catch (error) { notice.textContent = error.message; use.disabled = false; }
    });
    use.disabled = !selectedFactory || settings.factoryId === selectedFactory;
    if (listing.factories.length) chooser.append(use);
    const create = el('details');
    create.open = Boolean(selectedHandoff);
    create.append(el('summary', 'Create factory'));
    const id = control('input');
    const name = control('input');
    const description = control('textarea');
    const handoffSource = control('select');
    options(handoffSource, [['', 'Start without a handoff'], ...(context.runs || []).map(run => [run.id, run.id])], selectedHandoff);
    handoffSource.addEventListener('change', () => { selectedHandoff = handoffSource.value; });
    create.append(field('Factory ID', id), field('Factory name', name), field('Problem this factory solves', description));
    create.append(field('Import previous run handoff', handoffSource), el('p', 'Copies the source run’s current handoff into this factory. Later source changes do not change the imported copy.', 'field-help'));
    const createButton = button('Create from default', async () => {
      createButton.disabled = true;
      try {
        const result = await api('/api/factories', { method: 'POST', body: JSON.stringify({ id: id.value.trim(), name: name.value.trim(), description: description.value.trim(), ...(handoffSource.value ? { handoffRunId: handoffSource.value } : {}) }) });
        selectedFactory = result.factory.id;
        selectedHandoff = '';
        renderFactories(context);
      } catch (error) { notice.textContent = error.message; createButton.disabled = false; }
    });
    create.append(createButton);
    chooser.append(create);
    page.append(chooser);
    const appendRunPanel = () => {
      const run = panel('Create run', 'Uses the factory selected for new runs. Supply an approved project JSON with its checks and execution configuration.');
      const runId = control('input');
      const project = control('textarea'); project.rows = 8;
      const owner = control('input');
      const statement = control('textarea');
      const referenceImageHashes = control('input');
      const runStatus = el('p', '', 'form-status'); runStatus.setAttribute('role', 'status');
      run.append(el('p', `Selected factory: ${settings.factoryId || 'General software factory (default)'}`), field('Run ID', runId), field('Approved project JSON', project), field('Reference image hashes', referenceImageHashes, 'Comma-separated hashes from the saved reference images.'), field('Approval owner', owner), field('Approval statement', statement));
      const start = button('Create run', async () => {
        start.disabled = true;
        try {
          if ([...drafts.values()].some(item => item.dirty)) throw new Error('Save or discard unsaved agent skill changes before creating a run.');
          const hashes = referenceImageHashes.value.split(',').map(value => value.trim()).filter(Boolean);
          await api('/api/runs', { method: 'POST', body: JSON.stringify({ id: runId.value.trim(), project: JSON.parse(project.value), referenceImageHashes: hashes, approval: { owner: owner.value.trim(), statement: statement.value.trim() } }) });
          await onRun(runId.value.trim());
        } catch (error) { runStatus.textContent = error.message; start.disabled = false; }
      }, true);
      run.append(runStatus, start); page.append(run);
    };
    if (!selectedFactory) {
      chooser.append(el('p', 'No saved factories yet. New runs use the General software factory until you create one.', 'field-help'));
      appendRunPanel();
      return;
    }
    const result = await api(`/api/factories/${encodeURIComponent(selectedFactory)}`);
    if (!page.isConnected) return;
    const factory = result.factory;
    if (factory.inheritedHandoff) {
      const handoff = factory.inheritedHandoff;
      const inherited = panel('Imported handoff', `Source run ${handoff.runId} · Revision ${handoff.revision}`);
      inherited.append(el('p', 'Read-only context for this factory. New work needs its own approval and checks.', 'field-help'));
      const detail = el('details');
      detail.append(el('summary', 'Read imported handoff'), el('pre', handoff.markdown, 'skill-source'));
      inherited.append(detail, el('p', `Digest: ${handoff.digest}`, 'handoff-digest'));
      page.append(inherited);
    }
    const editor = panel('Agent skills', 'Assignments here are editable copies. Live activity reports only skills the agent explicitly read.');
    const role = control('select');
    if (!factory.agents.some(agent => agent.role === selectedRole)) selectedRole = factory.agents[0]?.role;
    options(role, factory.agents.map(agent => [agent.role, agent.role]), selectedRole);
    role.addEventListener('change', () => { selectedRole = role.value; renderFactories(context); });
    editor.append(field('Agent', role));
    const agentRole = selectedRole;
    const key = `${factory.id}:${agentRole}`;
    let draft = drafts.get(key);
    if (!draft || !draft.dirty) {
      draft = { revision: factory.revision, skills: structuredClone(factory.agents.find(agent => agent.role === selectedRole).skills), dirty: false };
      drafts.set(key, draft);
    }
    const status = el('p', draft.dirty ? 'Unsaved changes' : 'Saved', 'form-status');
    status.setAttribute('role', 'status');
    const list = el('div', undefined, 'skill-edit-list');
    const markDirty = () => { draft.dirty = true; status.textContent = 'Unsaved changes'; save.disabled = false; };
    const renderSkills = () => {
      list.replaceChildren();
      if (!draft.skills.length) list.append(el('p', 'No skills assigned. Add a skill from the library below.'));
      for (const skill of draft.skills) {
        const detail = el('details', undefined, 'skill-editor');
        detail.append(el('summary', skill.name));
        const text = control('textarea', skill.content);
        text.rows = 16;
        text.spellcheck = false;
        text.addEventListener('input', () => { skill.content = text.value; markDirty(); });
        detail.append(field(`${skill.name} / SKILL.md`, text));
        if (skill.references?.length) detail.append(el('p', `${skill.references.length} supporting files preserved.`, 'field-help'));
        detail.append(button('Remove assignment', () => { draft.skills = draft.skills.filter(item => item !== skill); markDirty(); renderSkills(); renderLibrary(); }));
        list.append(detail);
      }
    };
    const save = button('Save agent skills', async () => {
      save.disabled = true;
      status.textContent = 'Saving…';
      const sent = JSON.stringify(draft.skills);
      try {
        const saved = await api(`/api/factories/${encodeURIComponent(factory.id)}/agents/${encodeURIComponent(agentRole)}`, { method: 'PUT', body: JSON.stringify({ revision: draft.revision, skills: JSON.parse(sent) }) });
        draft.revision = saved.factory.revision;
        draft.dirty = JSON.stringify(draft.skills) !== sent;
        save.disabled = !draft.dirty;
        status.textContent = draft.dirty ? 'Newer edits are unsaved.' : 'Saved. New runs will use these skills.';
      } catch (error) { status.textContent = `${error.message} Your draft is kept. Discard and reload to use the latest saved version.`; save.disabled = false; }
    }, true);
    save.disabled = !draft.dirty;
    const actions = el('div', undefined, 'factory-editor-actions');
    actions.append(save, button('Discard and reload', () => {
      if (draft.dirty && !window.confirm('Discard unsaved changes for this agent?')) return;
      drafts.delete(key); renderFactories(context);
    }));
    renderSkills();
    editor.append(list, status, actions);
    const source = el('details');
    source.append(el('summary', 'Add skills from library'));
    const search = control('input');
    search.type = 'search';
    const results = el('div', undefined, 'skill-library-results');
    const renderLibrary = () => {
      results.replaceChildren();
      const matches = library.skills.filter(skill => `${skill.name} ${skill.path}`.toLowerCase().includes(search.value.toLowerCase()));
      for (const skill of matches) {
        const detail = el('details', undefined, 'skill-editor');
        detail.append(el('summary', skill.name), el('p', skill.path, 'field-help'), el('pre', skill.content, 'skill-source'));
        const add = button('Add copy to agent', () => {
          draft.skills.push(structuredClone(skill)); markDirty(); renderSkills(); renderLibrary();
        });
        add.disabled = draft.skills.some(item => item.name === skill.name);
        detail.append(add); results.append(detail);
      }
      if (!matches.length) results.append(el('p', 'No matching skills.'));
    };
    search.addEventListener('input', renderLibrary);
    renderLibrary();
    source.append(field('Search source skills', search), results);
    editor.append(source);
    page.append(editor);
    appendRunPanel();
  } catch (error) { notice.textContent = error.message; }
}
