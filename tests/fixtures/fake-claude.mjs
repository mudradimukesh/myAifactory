// Stand-in for `claude auth login --claudeai` and `claude auth status --json`.
// The first argument selects the link it prints: good, bad-host or none.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const [mode, command, subcommand] = process.argv.slice(2);
const credentials = path.join(process.env.CLAUDE_CONFIG_DIR, '.credentials.json');

if (command === 'auth' && subcommand === 'status') {
  let loggedIn = false;
  try { loggedIn = Boolean(JSON.parse(readFileSync(credentials, 'utf8')).claudeAiOauth); } catch { /* Signed out. */ }
  console.log(JSON.stringify({ loggedIn, authMethod: 'claude.ai', apiProvider: 'firstParty', email: 'fixture-person@example.com',
    orgName: 'Fixture Org', subscriptionType: 'max' }));
  process.exit(loggedIn ? 0 : 1);
}

if (command !== 'auth' || subcommand !== 'login') process.exit(2);
// A descendant in the same process group, so cancellation must stop the group, not just the leader.
spawn(process.execPath, ['-e', 'process.on("SIGTERM", () => {}); setInterval(() => {}, 1000)'], { stdio: 'ignore' });
const host = mode === 'bad-host' ? 'claude.example.net' : 'claude.com';
// The state carries the leader pid so a test can check that its group is gone.
if (mode !== 'none') process.stdout.write(`If the browser didn't open, visit: https://${host}/cai/oauth/authorize?code=true&state=${process.pid}\n`);
process.stdout.write('Paste code here if prompted > ');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', line => {
  const trimmed = line.trim();
  if (!trimmed.includes('#')) { console.error('Invalid code. Please make sure the full code was copied.'); return; }
  if (trimmed !== 'good-code#fixture-state') { console.error('Login failed'); process.exit(1); }
  writeFileSync(path.join(process.env.CLAUDE_CONFIG_DIR, 'settings.json'), '{}');
  writeFileSync(credentials, JSON.stringify({ claudeAiOauth: { accessToken: 'fixture-secret-access', refreshToken: 'fixture-secret-refresh' } }), { mode: 0o600 });
  console.log('Login successful.');
  process.exit(0);
});
