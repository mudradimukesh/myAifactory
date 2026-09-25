# Sign in to Claude from the dashboard

Claude workers need a dedicated auth home that holds only `.credentials.json` with a `claudeAiOauth` entry (`validateAuthHome` in `src/runtime.ts`). Before this decision the operator had to create it by hand in a terminal. The dashboard now runs the Claude CLI's own login and saves the file it writes. ADR 0001's no-token-handling rule and ADR 0002's sandbox rules stay in force.

## Decision

`src/claude-login.ts` runs `claude auth login --claudeai` under `/usr/bin/sandbox-exec` with the worker profile from `sandboxProfile`. The profile allows outbound network and loopback, and it has no SecurityServer mach access. The CLI therefore cannot use the Keychain and writes its plaintext fallback file under `CLAUDE_CONFIG_DIR`. The environment is only `PATH`, `HOME`, `CLAUDE_CONFIG_DIR`, `TMPDIR` and `LANG`, all pointing into a fresh `<privateDir>/claude-login/<random>` directory.

The CLI prints an authorize address and waits for a code. The module reads stdout in memory, capped at 64 KiB, and accepts only an `https` address on `claude.com` or `claude.ai` whose path ends in `/oauth/authorize`. Any other address fails the login. The operator opens the address in a browser, signs in on Claude's page and pastes the one-time code shown there. The module writes the code to the CLI's stdin and never logs or stores it. The code is a single-use PKCE authorization code, not a credential.

When the CLI exits with code 0 and its file has `claudeAiOauth`, the module copies the file to `<privateDir>/auth/claude/.credentials.json` through a temp file and a rename. The directory is 0700 and the file is 0600. The module then runs `validateAuthHome` and removes the login directory. Every other outcome sets a fixed failure message and removes the directory. Neither stdout nor stderr reaches disk, logs or the browser.

A status check copies the saved file into a fresh directory and runs `claude auth status --json` in the same sandbox. The user's own Keychain login therefore cannot report ready. The view returns only `authMethod` and `subscriptionType`. Email, organisation and token fields are dropped by the parse. A check result stays cached for 60 s, and the dashboard poll never runs one.

A login runs in its own process group. Cancel and server shutdown send SIGTERM to that group while its leader is unreaped, and SIGKILL after 1 s. When the leader exits, the module sends SIGKILL to the group to remove descendants that kept running, which matches `runProcess`. A second start while a login is pending returns the same login. Each login times out after 10 minutes, and a login that prints no address within 30 s fails.

Workers use this login only through a run profile whose `runtime.authHomes.claude` is the saved auth home. Existing runs keep their frozen profile.

## Rejected alternative

`claude setup-token` prints a long-lived token. The dashboard would have to capture it from stdout and inject it as `CLAUDE_CODE_OAUTH_TOKEN`. That puts a secret in the factory's hands and breaks ADR 0001, so the factory does not use it.

## Verification and open risks

`tests/claude-login.test.ts` drives `tests/fixtures/fake-claude.mjs` through the real sandbox. It checks the full flow, second-start reuse, code validation, a refused host, a rejected code, cancel and close. It asserts that no view contains the fixture secret or email, that the auth home holds only the 0600 credential file, and that no login directory or process group remains.

No real login has run. In Claude Code 2.1.280, `auth login` reads the pasted value from stdin as a single line of `code#state` and rejects a line without `#` as invalid without exiting, so `submitCode` must reject any code missing its `#state` suffix before it reaches the CLI; a value that reaches the CLI's stdin without `#` cannot be retried, because the module already closed stdin after the one write. The plaintext fallback under a denied Keychain runs through a strict `security find-generic-password` read first: exit 44 (not found) or 36 (locked) falls through to the plaintext write, but any other exit code is treated as transient and aborts the write, so the CLI can print "Login successful." and exit 0 while saving no file. Under the worker sandbox profile, `security find-generic-password` for a missing item exits 44, so the plaintext write runs. An earlier attempt through `LocalRuntime` ended with exit 143 and no file. One human login still has to confirm the saved file end to end. If the provider rotates refresh tokens, a worker attempt or status check that refreshes its copy can invalidate the saved file, as ADR 0002 records for workers.
