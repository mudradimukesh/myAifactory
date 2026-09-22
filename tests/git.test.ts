import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, symlink, unlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createStore, git, importCandidate, treeDigest } from '../src/git.ts';

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
