// Claude sign-in dialog. Sign-in happens on Claude's own page, and this dialog relays the
// one-time code Claude shows there. It never prints the sign-in URL, so screenshots do not
// capture its state or code_challenge parameters.
const statusPath = '/api/auth/claude';

function el(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text) item.textContent = text;
  return item;
}
function action(label, kind, onClick) {
  const item = el('button', `button ${kind}`, label);
  item.type = 'button';
  if (onClick) item.addEventListener('click', onClick);
  return item;
}

export function openClaudeLogin({ api, settings, onDone }) {
  if (document.querySelector('dialog.claude-login')) return;
  const opener = document.activeElement;
  const dialog = el('dialog', 'modal claude-login');
  dialog.setAttribute('aria-labelledby', 'claude-login-title');
  const title = el('h2', '', 'Sign in to Claude');
  title.id = 'claude-login-title';
  const content = el('div');
  const status = el('p', 'form-status');
  status.setAttribute('role', 'status');
  const actions = el('div', 'modal-actions');
  const body = el('div', 'modal-body');
  body.append(title, content, status, actions);
  dialog.append(body);
  let view = null;
  let saved = null;
  let timer = 0;

  function step(nodes, buttons, message = '') {
    content.replaceChildren(...nodes);
    actions.replaceChildren(...buttons);
    status.textContent = message;
    status.className = 'form-status';
    (content.querySelector('a, input') || actions.lastElementChild).focus();
  }
  function fail(error) {
    status.textContent = error.message;
    status.className = 'form-status error-text';
  }
  async function send(path, payload, trigger) {
    if (trigger) trigger.disabled = true;
    try { show(await api(path, payload ? { method: 'POST', body: JSON.stringify(payload) } : {})); }
    catch (error) { if (dialog.open) fail(error); }
    finally { if (trigger) trigger.disabled = false; }
  }
  function startButton(label) {
    const start = action(label, 'primary', () => send(`${statusPath}/login`, {}, start));
    return start;
  }
  function intro(message) {
    step([el('p', '', 'Sign-in happens on Claude\'s own page in a new tab. Claude then shows a one-time code for you to paste here.'),
      el('p', '', 'The factory never asks for your password. The credential file stays in a private folder on this Mac.')],
    [action('Cancel', 'secondary', () => dialog.close()), startButton('Start Claude sign-in')], message);
  }
  function codeStep() {
    const link = el('a', 'button secondary claude-link', 'Open Claude sign-in page');
    link.href = view.authorizeUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const form = el('form', 'claude-code-form');
    form.id = 'claude-code-form';
    const code = el('input', 'input');
    code.name = 'code';
    code.required = true;
    code.maxLength = 512;
    code.autocomplete = 'off';
    code.spellcheck = false;
    code.setAttribute('autocapitalize', 'off');
    const label = el('label', 'field');
    label.append(el('span', 'field-label', 'Full code from Claude\'s page'), code);
    form.append(label);
    const submit = action('Submit code', 'primary');
    submit.type = 'submit';
    submit.setAttribute('form', form.id);
    form.addEventListener('submit', event => {
      event.preventDefault();
      const value = code.value.trim();
      if (value) send(`${statusPath}/login/code`, { code: value }, submit);
    });
    step([el('p', '', 'Open the Claude sign-in page and approve access. Claude then shows a one-time code that contains #. A 6-digit email code belongs on Claude\'s sign-in page, not here.'), link, form],
      [action('Cancel sign-in', 'secondary', () => dialog.close()), submit]);
  }
  async function readyStep() {
    step([el('p', '', view.account?.subscriptionType ? `Claude sign-in is ready. Plan ${view.account.subscriptionType}.` : 'Claude sign-in is ready.')],
      [action('Done', 'primary', () => dialog.close())]);
    const current = settings.authHomes?.claude;
    if (current) {
      if (current !== view.authHome) content.append(el('p', '', `Your saved Claude auth directory is a different folder. Set it to ${view.authHome} in Connections to use this sign-in.`));
      return;
    }
    try {
      const next = structuredClone(settings);
      next.authHomes = { ...next.authHomes, claude: view.authHome };
      saved = (await api('/api/settings', { method: 'PUT', body: JSON.stringify(next) })).settings;
      content.append(el('p', '', 'Saved as your Claude auth directory. Existing runs keep the profile they were created with.'));
    } catch (error) { fail(error); }
  }
  function show(next) {
    if (!dialog.open) return;
    const previous = view?.status;
    view = next;
    if (view.status === 'awaiting_code') codeStep();
    else if (view.status === 'verifying') {
      if (previous !== 'verifying') step([el('p', 'modal-wait', 'Checking the sign-in with Claude.')], [action('Close', 'secondary', () => dialog.close())]);
      timer = window.setTimeout(() => send(statusPath), 1000);
    } else if (view.status === 'ready') readyStep();
    else if (view.status === 'failed') step([el('p', '', view.message)], [action('Close', 'secondary', () => dialog.close()), startButton('Try again')]);
    else intro(view.message);
  }

  dialog.addEventListener('close', async () => {
    window.clearTimeout(timer);
    dialog.remove();
    if (opener?.isConnected) opener.focus();
    if (view?.status === 'awaiting_code') {
      try { view = await api(`${statusPath}/login/cancel`, { method: 'POST', body: '{}' }); }
      catch (error) { view = { ...view, message: error.message }; }
    }
    onDone(view, saved);
  });
  document.body.append(dialog);
  dialog.showModal();
  intro();
}
