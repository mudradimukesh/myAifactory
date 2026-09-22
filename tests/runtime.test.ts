import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { DockerRuntime } from '../src/runtime.ts';

test('fixture Docker refuses foreign containers and addresses owned containers by immutable ID', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-docker-fixture-'));
  const previous = process.env.PATH;
  const owner = 'a'.repeat(64), container = 'b'.repeat(64);
  const calls = path.join(root, 'calls.jsonl');
  const inspection = path.join(root, 'inspection.json');
  try {
    await writeFile(path.join(root, 'docker'), `#!${process.execPath}\nconst fs=require('node:fs');const args=process.argv.slice(2);fs.appendFileSync(${JSON.stringify(calls)},JSON.stringify(args)+'\\n');if(args[0]==='container')process.stdout.write(fs.readFileSync(${JSON.stringify(inspection)}));`, { mode: 0o700 });
    process.env.PATH = root;
    const runtime = new DockerRuntime();
    await writeFile(inspection, JSON.stringify([{ Id: container, Name: '/factory-fixture', Config: { Labels: {} } }]));
    await assert.rejects(runtime.stop('factory-fixture', owner), /unowned container/);
    await assert.rejects(runtime.remove('factory-fixture', owner), /unowned container/);
    let commands = (await readFile(calls, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.ok(commands.every(args => args[0] === 'container'));
    await writeFile(inspection, JSON.stringify([{ Id: container, Name: '/factory-fixture', Config: { Labels: { 'dev.agent-factory.owner': owner } } }]));
    await runtime.stop('factory-fixture', owner);
    await runtime.remove('factory-fixture', owner);
    commands = (await readFile(calls, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.deepEqual(commands.filter(args => args[0] !== 'container'), [['stop', '--time', '2', container], ['rm', container]]);
  } finally {
    if (previous === undefined) delete process.env.PATH; else process.env.PATH = previous;
    await rm(root, { recursive: true, force: true });
  }
});
