import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, unlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { checkout, createStore, git, importCandidate, treeDigest } from '../src/git.ts';

test('changing a source symlink invalidates verification evidence', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-source-'));
  try {
    await writeFile(path.join(root, 'safe'), 'safe');
    await writeFile(path.join(root, 'changed'), 'changed');
    const link = path.join(root, 'entry');
    await symlink('safe', link);
    const before = await treeDigest(root);
    await unlink(link);
    await symlink('changed', link);
    assert.notEqual(await treeDigest(root), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('candidate import preserves exact filenames when enforcing approved paths', async () => {
  for (const filename of [' src/escape.txt', 'src/résumé.txt']) {
    const root = await mkdtemp(path.join(tmpdir(), 'factory-candidate-'));
    try {
      const source = path.join(root, 'source'), store = path.join(root, 'candidates.git');
      await mkdir(source);
      await git(source, ['init']);
      await git(source, ['config', 'user.name', 'Fixture']);
      await git(source, ['config', 'user.email', 'fixture@localhost']);
      await writeFile(path.join(source, 'README.md'), 'fixture');
      await git(source, ['add', '.']);
      await git(source, ['commit', '-m', 'fixture base']);
      const base = await git(source, ['rev-parse', 'HEAD']);
      await createStore(source, store, base);
      await mkdir(path.dirname(path.join(source, filename)), { recursive: true });
      await writeFile(path.join(source, filename), 'candidate');
      await git(source, ['add', '.']);
      await git(source, ['commit', '-m', 'fixture change']);
      if (filename.startsWith(' ')) {
        await assert.rejects(importCandidate(store, source, base, ['src']), /disallowed path/);
        assert.equal(await git(store, ['for-each-ref', 'refs/candidates']), '');
      } else {
        const result = await importCandidate(store, source, base, ['src']);
        assert.deepEqual(result.files, ['src/résumé.txt']);
        assert.equal(await git(store, ['show', `${result.candidate}:src/résumé.txt`]), 'candidate');
      }
    } finally { await rm(root, { recursive: true, force: true }); }
  }
});

test('imported sequential candidates check out independently with exact filenames and contents', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-candidate-checkout-'));
  try {
    const source = path.join(root, 'source'), store = path.join(root, 'candidates.git');
    await mkdir(source);
    await git(source, ['init']);
    await git(source, ['config', 'user.name', 'Fixture']);
    await git(source, ['config', 'user.email', 'fixture@localhost']);
    await writeFile(path.join(source, 'README.md'), 'base');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'base']);
    const base = await git(source, ['rev-parse', 'HEAD']);
    await createStore(source, store, base);
    await mkdir(path.join(source, 'src'));
    const filenames = ['src/ leading.txt', 'src/résumé.txt', 'src/tab\tline\n.txt'];
    const candidates: string[] = [];
    for (const version of [1, 2]) {
      for (const filename of filenames) await writeFile(path.join(source, filename), `version ${version}\n`);
      await git(source, ['add', '.']);
      await git(source, ['commit', '-m', `candidate ${version}`]);
      const imported = await importCandidate(store, source, base, ['src']);
      candidates.push(imported.candidate);
      assert.deepEqual([...imported.files].sort(), [...filenames].sort());
      const verifier = path.join(root, `verifier-${version}`);
      await checkout(store, verifier, imported.candidate);
      assert.equal(await git(verifier, ['rev-parse', 'HEAD']), imported.candidate);
      assert.equal(await git(verifier, ['status', '--porcelain', '--untracked-files=all']), '');
      assert.equal(await git(verifier, ['remote']), '');
      for (const filename of filenames) assert.equal(await readFile(path.join(verifier, filename), 'utf8'), `version ${version}\n`);
    }
    assert.notEqual(candidates[0], candidates[1]);
    const oldVerifier = path.join(root, 'verifier-first-again');
    await checkout(store, oldVerifier, candidates[0]!);
    assert.equal(await git(oldVerifier, ['rev-parse', 'HEAD']), candidates[0]);
    for (const filename of filenames) assert.equal(await readFile(path.join(oldVerifier, filename), 'utf8'), 'version 1\n');
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('dirty candidates are rejected before publishing their commit', async () => {
  for (const dirty of ['unstaged', 'staged', 'staged-only', 'untracked']) {
    const root = await mkdtemp(path.join(tmpdir(), 'factory-dirty-candidate-'));
    try {
      const source = path.join(root, 'source'), store = path.join(root, 'candidates.git');
      await mkdir(source);
      await git(source, ['init']);
      await git(source, ['config', 'user.name', 'Fixture']);
      await git(source, ['config', 'user.email', 'fixture@localhost']);
      await writeFile(path.join(source, 'source.txt'), 'base');
      await git(source, ['add', '.']);
      await git(source, ['commit', '-m', 'base']);
      const base = await git(source, ['rev-parse', 'HEAD']);
      await createStore(source, store, base);
      await writeFile(path.join(source, 'source.txt'), 'committed candidate');
      await git(source, ['add', '.']);
      await git(source, ['commit', '-m', 'candidate']);
      await writeFile(path.join(source, dirty === 'untracked' ? 'new.txt' : 'source.txt'), 'uncommitted');
      if (dirty.startsWith('staged')) await git(source, ['add', '.']);
      if (dirty === 'staged-only') await writeFile(path.join(source, 'source.txt'), 'committed candidate');
      await assert.rejects(importCandidate(store, source, base, ['.']), /staged|unstaged|untracked|uncommitted|dirty/i);
      assert.equal(await git(store, ['for-each-ref', 'refs/candidates']), '');
    } finally { await rm(root, { recursive: true, force: true }); }
  }
});

test('submission uses object data without loading worker configuration or replacement refs', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-git-metadata-'));
  try {
    const source = path.join(root, 'source'), store = path.join(root, 'store.git');
    await mkdir(source);
    await git(source, ['init']);
    await git(source, ['config', 'user.name', 'Fixture']);
    await git(source, ['config', 'user.email', 'fixture@localhost']);
    await writeFile(path.join(source, 'allowed'), 'base');
    await writeFile(path.join(source, 'protected'), 'base');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'base']);
    const base = await git(source, ['rev-parse', 'HEAD']);
    await createStore(source, store, base);
    await writeFile(path.join(source, 'allowed'), 'candidate');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'allowed change']);
    const allowed = await git(source, ['rev-parse', 'HEAD']);
    await writeFile(path.join(source, 'protected'), 'changed');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'protected change']);
    const candidate = await git(source, ['rev-parse', 'HEAD']);
    await mkdir(path.join(source, '.git/refs/replace'));
    await writeFile(path.join(source, '.git/refs/replace', candidate), allowed + '\n');
    // Invalid configuration proves the submission path never loads it at all.
    await writeFile(path.join(source, '.git/config'), '[invalid configuration');
    await assert.rejects(importCandidate(store, source, base, ['allowed']), /disallowed path protected/);
    assert.equal(await git(store, ['for-each-ref', 'refs/candidates']), '');
    const imported = await importCandidate(store, source, base, ['allowed', 'protected']);
    assert.equal(imported.candidate, candidate);
    assert.deepEqual(imported.files, ['allowed', 'protected']);
    assert.equal(await git(store, ['show', candidate + ':protected']), 'changed');
    await writeFile(path.join(source, 'protected'), 'uncommitted');
    await assert.rejects(importCandidate(store, source, base, ['.']), /staged|unstaged|untracked/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('packed repositories and retention metadata preserve clean candidate import', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-packed-candidate-'));
  try {
    const source = path.join(root, 'source'), store = path.join(root, 'store.git');
    await mkdir(source);
    await git(source, ['init']);
    await git(source, ['config', 'user.name', 'Fixture']);
    await git(source, ['config', 'user.email', 'fixture@localhost']);
    await writeFile(path.join(source, 'data'), 'base');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'base']);
    const base = await git(source, ['rev-parse', 'HEAD']);
    await createStore(source, store, base);
    await writeFile(path.join(source, 'data'), 'candidate');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'candidate']);
    await git(source, ['repack', '-ad']);
    await git(source, ['pack-refs', '--all']);
    await git(source, ['multi-pack-index', 'write']);
    const packs = path.join(source, '.git/objects/pack');
    const pack = (await readdir(packs)).find(name => name.endsWith('.pack'))!;
    await writeFile(path.join(packs, pack.replace(/\.pack$/, '.keep')), 'retained fixture pack');
    const candidate = await importCandidate(store, source, base, ['data']);
    await checkout(store, path.join(root, 'verifier'), candidate.candidate);
    assert.equal(await readFile(path.join(root, 'verifier/data'), 'utf8'), 'candidate');
  } finally { await rm(root, { recursive: true, force: true }); }
});
