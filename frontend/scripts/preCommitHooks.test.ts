/**
 * Integration tests for frontend pre-commit formatting behavior.
 * Exercises prettier and lint-staged through their public CLIs, not hook internals.
 */
// @vitest-environment node

import { execSync } from 'node:child_process';
import { cpSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter as pathDelimiter, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, '..');
const lintStagedBin = join(frontendRoot, 'node_modules', '.bin', 'lint-staged');
const prettierBin = join(frontendRoot, 'node_modules', '.bin', 'prettier');

/**
 * Run a shell command and return combined stdout/stderr.
 */
function run(command: string, cwd: string): string {
  // Git exports repository variables to hooks. The fixture's git commands must
  // operate on its temporary repository rather than the parent commit's index.
  const isolatedEnv = Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !key.startsWith('GIT_'))
  );
  return execSync(command, {
    cwd,
    encoding: 'utf8',
    env: {
      ...isolatedEnv,
      PATH: `${join(frontendRoot, 'node_modules', '.bin')}${pathDelimiter}${process.env.PATH ?? ''}`,
    },
  });
}

/**
 * Create an isolated git repo with lint-staged config for hook smoke tests.
 */
function createStagedRepo(unformattedSource: string): {
  dir: string;
  filePath: string;
} {
  const dir = mkdtempSync(join(tmpdir(), 'enclave-lint-staged-'));
  const filePath = join(dir, 'sample.ts');

  writeFileSync(filePath, unformattedSource);
  cpSync(join(frontendRoot, '.lintstagedrc'), join(dir, '.lintstagedrc'));
  cpSync(join(frontendRoot, '.prettierrc'), join(dir, '.prettierrc'));

  run('git init', dir);
  run('git config user.email "test@example.com"', dir);
  run('git config user.name "Test User"', dir);
  run('git add sample.ts', dir);

  return { dir, filePath };
}

describe('pre-commit formatting', () => {
  it('prettier reformats unstyled source to match project config', () => {
    const dir = mkdtempSync(join(tmpdir(), 'enclave-prettier-'));
    const filePath = join(dir, 'sample.ts');

    writeFileSync(filePath, 'const x={a:1}\n');
    run(`"${prettierBin}" --write "${filePath}"`, frontendRoot);

    expect(readFileSync(filePath, 'utf8')).toBe('const x = { a: 1 };\n');
  });

  it('format:check fails before prettier and passes after write', () => {
    const dir = mkdtempSync(join(tmpdir(), 'enclave-format-check-'));
    const filePath = join(dir, 'sample.ts');

    writeFileSync(filePath, 'const x={a:1}\n');

    expect(() =>
      run(`"${prettierBin}" --check "${filePath}"`, frontendRoot)
    ).toThrow();

    run(`"${prettierBin}" --write "${filePath}"`, frontendRoot);

    expect(() =>
      run(`"${prettierBin}" --check "${filePath}"`, frontendRoot)
    ).not.toThrow();
  });

  it('lint-staged applies prettier to staged files', () => {
    const { dir, filePath } = createStagedRepo('const x={a:1}\n');

    run(`"${lintStagedBin}"`, dir);

    expect(readFileSync(filePath, 'utf8')).toBe('const x = { a: 1 };\n');
  });

  it('verify:pre-commit script matches the husky hook entrypoint', () => {
    const hook = readFileSync(
      join(frontendRoot, '.husky', 'pre-commit'),
      'utf8'
    );
    const packageJson = JSON.parse(
      readFileSync(join(frontendRoot, 'package.json'), 'utf8')
    ) as { scripts: Record<string, string> };

    expect(hook.trim()).toBe('cd frontend\nnpm run verify:pre-commit');
    expect(packageJson.scripts['verify:pre-commit']).toBe(
      'lint-staged && npm run test'
    );
  });
});
