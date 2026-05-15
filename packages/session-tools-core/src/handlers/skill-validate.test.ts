import { afterEach, describe, expect, it } from 'bun:test';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { SessionToolContext } from '../context.ts';
import { handleSkillValidate } from './skill-validate.ts';

let tempDirs: string[] = [];

function makeWorkspace(): string {
  const dir = mkdtempSync(join(tmpdir(), 'skill-validate-'));
  tempDirs.push(dir);
  mkdirSync(join(dir, 'skills'), { recursive: true });
  return dir;
}

function makeCtx(workspacePath: string, onNotify: (relativePath: string) => void): SessionToolContext {
  return {
    sessionId: 'test-session',
    workspacePath,
    get sourcesPath() { return join(workspacePath, 'sources'); },
    get skillsPath() { return join(workspacePath, 'skills'); },
    plansFolderPath: join(workspacePath, 'plans'),
    workingDirectory: workspacePath,
    callbacks: {
      onPlanSubmitted: () => {},
      onAuthRequest: () => {},
    },
    fs: {
      exists: existsSync,
      readFile: (path: string) => readFileSync(path, 'utf-8'),
      readFileBuffer: (path: string) => readFileSync(path),
      writeFile: (path: string, content: string) => writeFileSync(path, content, 'utf-8'),
      isDirectory: (path: string) => existsSync(path) && statSync(path).isDirectory(),
      readdir: (path: string) => readdirSync(path),
      stat: (path: string) => {
        const stats = statSync(path);
        return {
          size: stats.size,
          isDirectory: () => stats.isDirectory(),
        };
      },
    },
    loadSourceConfig: () => null,
    notifyConfigFileChange: onNotify,
  };
}

afterEach(() => {
  for (const dir of tempDirs) {
    rmSync(dir, { recursive: true, force: true });
  }
  tempDirs = [];
});

describe('handleSkillValidate', () => {
  it('notifies the host after a valid workspace skill validates', async () => {
    const workspace = makeWorkspace();
    const skillDir = join(workspace, 'skills', 'travel-notes');
    mkdirSync(skillDir, { recursive: true });
    writeFileSync(join(skillDir, 'SKILL.md'), `---
name: Travel Notes
description: Write concise travel journal entries.
---
Turn raw trip notes into short dated travel entries.
`);

    const notified: string[] = [];
    const result = await handleSkillValidate(makeCtx(workspace, path => notified.push(path)), {
      skillSlug: 'travel-notes',
    });

    expect(result.isError).toBe(false);
    expect(notified).toEqual(['skills/travel-notes/SKILL.md']);
  });
});
