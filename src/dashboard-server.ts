import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';
import { ClaudeLogin } from './claude-login.ts';
import { CollaborationError, Dashboard, FactoryConflict, LaunchFailed, StaleRevision, StopIncomplete } from './dashboard.ts';

const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../web');
const files: Record<string, [string, string]> = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
  '/claude-login.js': ['claude-login.js', 'text/javascript; charset=utf-8'],
  '/style.css': ['style.css', 'text/css; charset=utf-8'],
};

function json(res: ServerResponse, status: number, value: unknown) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(value));
}
function failure(res: ServerResponse, status: number, code: string, message: string, recommendation: string) {
  json(res, status, { code, severity: 'error', message, recommendation });
}
async function body(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > 65536) throw new BodyTooLarge();
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
class BodyTooLarge extends Error {}

export function createDashboardServer(root: string, dashboard = new Dashboard(root)): Server {
  const login = new ClaudeLogin(dashboard.privateDir);
  const csrfToken = randomBytes(32).toString('hex');
  const server = createServer(async (req, res) => {
    const localPort = req.socket.localPort;
    const expectedHost = `127.0.0.1:${localPort}`;
    const host = req.headers.host;
    const origin = req.headers.origin;
    if (host !== expectedHost || (origin && origin !== `http://${expectedHost}`)) {
      failure(res, 403, 'origin_rejected', 'Request origin is not this dashboard.', 'Open the local dashboard URL shown when the server started.');
      return;
    }
    const mutation = req.method === 'PUT' || req.method === 'POST' || req.method === 'DELETE';
    if (mutation && (origin !== `http://${expectedHost}` || req.headers['x-csrf-token'] !== csrfToken)) {
      failure(res, 403, 'csrf_rejected', 'This request was not authorized by the local dashboard session.', 'Reload the dashboard and retry.');
      return;
    }
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    try {
      const pathname = new URL(req.url ?? '/', `http://${expectedHost}`).pathname;
      if (req.method === 'GET' && pathname === '/api/session') return json(res, 200, { csrfToken });
      if (req.method === 'GET' && pathname === '/api/dashboard') return json(res, 200, await dashboard.snapshot());
      if (req.method === 'PUT' && pathname === '/api/settings') return json(res, 200, { settings: await dashboard.saveSettings(await body(req)) });
      if (req.method === 'PUT' && pathname === '/api/credentials') return json(res, 200, { credentials: await dashboard.saveCredential(await body(req)) });
      if (req.method === 'POST' && pathname === '/api/auth/check') return json(res, 200, await dashboard.authCheck(await body(req)));
      if (req.method === 'GET' && pathname === '/api/auth/claude') return json(res, 200, await login.status(new URL(req.url ?? '/', `http://${expectedHost}`).searchParams.get('check') === '1'));
      if (req.method === 'POST' && pathname === '/api/auth/claude/login') { z.object({}).strict().parse(await body(req)); return json(res, 200, await login.start()); }
      if (req.method === 'POST' && pathname === '/api/auth/claude/login/code') return json(res, 200, await login.submitCode(z.object({ code: z.string() }).strict().parse(await body(req)).code));
      if (req.method === 'POST' && pathname === '/api/auth/claude/login/cancel') { z.object({}).strict().parse(await body(req)); return json(res, 200, await login.cancel()); }
      const recovery = /^\/api\/runs\/([a-z0-9][a-z0-9-]{0,63})\/recovery$/.exec(pathname);
      if (req.method === 'GET' && recovery) {
        res.setHeader('Content-Disposition', `attachment; filename="${recovery[1]}-recovery.json"`);
        return json(res, 200, await dashboard.recovery(recovery[1]));
      }
      const control = /^\/api\/runs\/([a-z0-9][a-z0-9-]{0,63})\/control$/.exec(pathname);
      if (req.method === 'POST' && control) return json(res, 200, await dashboard.control(control[1], await body(req)));
      const answers = /^\/api\/runs\/([a-z0-9][a-z0-9-]{0,63})\/answers$/.exec(pathname);
      if (req.method === 'POST' && answers) return json(res, 200, await dashboard.answerQuestions(answers[1], await body(req)));
      const image = /^\/api\/runs\/([a-z0-9][a-z0-9-]{0,63})\/questions\/([a-z0-9][a-z0-9-]{0,63})\/([a-z0-9][a-z0-9-]{0,63})\/images\/([a-z0-9][a-z0-9-]{0,63})$/.exec(pathname);
      if (req.method === 'GET' && image) {
        const evidence = await dashboard.visualImage(image[1], image[2], image[3], image[4]);
        if (!evidence) return failure(res, 404, 'image_not_found', 'This image is not linked to the current candidate.', 'Review the current question batch.');
        res.writeHead(200, { 'Content-Type': evidence.mimeType, 'Content-Length': evidence.bytes.length,
          'Cache-Control': 'no-store', ETag: `"${evidence.sha256}"`, 'Content-Disposition': 'inline' });
        return res.end(evidence.bytes);
      }
      const tickets = /^\/api\/runs\/([a-z0-9][a-z0-9-]{0,63})\/tickets\/sync$/.exec(pathname);
      if (req.method === 'POST' && tickets) return json(res, 200, await dashboard.syncTickets(tickets[1], await body(req)));
      if (req.method === 'GET' && files[pathname]) {
        const [name, type] = files[pathname];
        const content = await readFile(path.join(webDir, name));
        res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
        return res.end(content);
      }
      return failure(res, 404, 'not_found', 'This dashboard path does not exist.', 'Use the navigation in the dashboard.');
    } catch (error) {
      if (error instanceof FactoryConflict) return failure(res, 409, 'factory_conflict', error.message, 'Refresh the run and review its factory state.');
      if (error instanceof LaunchFailed) return failure(res, 500, 'launch_failed', error.message, 'Read the supervisor log, fix the cause, then start again.');
      if (error instanceof StopIncomplete) return failure(res, 500, 'stop_incomplete', error.message, 'Inspect these processes before starting any other work. The dashboard signals only processes it can prove it owns.');
      if (error instanceof StaleRevision) return failure(res, 409, 'stale_revision', 'The run changed since it was displayed.', 'Refresh the run and review its latest state before submitting again.');
      if (error instanceof CollaborationError) return failure(res, 400, 'collaboration_unavailable', error.message, 'Review the run and GitHub connection before retrying.');
      if (error instanceof BodyTooLarge) return failure(res, 413, 'body_too_large', 'Request exceeds 64 KiB.', 'Shorten the submitted value.');
      if (error instanceof z.ZodError || error instanceof SyntaxError || error instanceof TypeError || error instanceof Error && /^(Invalid GitHub token format|Credentials must be one line)$/.test(error.message))
        return failure(res, 400, 'invalid_input', 'The submitted values are invalid.', error instanceof z.ZodError ? error.issues.map(x => x.path.length ? `${x.path.join('.')}: ${x.message}` : x.message).join('; ') : 'Review the form values and retry.');
      if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return failure(res, 404, 'run_not_found', 'The requested run or record does not exist.', 'Refresh the dashboard.');
      return failure(res, 500, 'record_unavailable', 'The requested record could not be read safely.', 'Inspect the local state files before trying another action.');
    }
  });
  // A pending sign-in runs in its own process group and must not outlive the dashboard.
  server.on('close', () => { void login.close(); });
  return server;
}

export async function startDashboard({ root, port = 4317 }: { root: string; port?: number }) {
  const server = createDashboardServer(root);
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No local dashboard port');
  return { url: `http://127.0.0.1:${address.port}`, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const rootFlag = process.argv.indexOf('--root');
  const portFlag = process.argv.indexOf('--port');
  const root = rootFlag >= 0 ? process.argv[rootFlag + 1] : '.factory';
  const port = portFlag >= 0 ? Number(process.argv[portFlag + 1]) : 4317;
  if (!root || !Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Use --root PATH --port NUMBER');
  const app = await startDashboard({ root, port });
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => { void app.close(); });
  process.stdout.write(`${app.url}\n`);
}
