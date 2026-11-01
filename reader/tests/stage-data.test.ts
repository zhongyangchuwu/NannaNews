import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { afterAll, describe, expect, it } from 'vitest';
import type { Catalog, Manifest, PublicImage, PublicIssue, PublicOccurrence } from '../src/types';
type SourceManifest = Extract<Manifest, { schema_version: 2 }>;

const run = promisify(execFile);
const stageData = fileURLToPath(new URL('../scripts/stage-data.mjs', import.meta.url));
const stagePublicFacts = fileURLToPath(new URL('../scripts/stage-public-facts.mjs', import.meta.url));
const buildSearch = fileURLToPath(new URL('../scripts/build-search.mjs', import.meta.url));
const version = 'a'.repeat(64);
const prefix = `versions/${version}`;
const sourceUrl = 'https://example.edu/news';
const temporaryRoots: string[] = [];

function image(id: string, bytes: Buffer): { ref: PublicImage; bytes: Buffer } {
  const digest = createHash('sha256').update(bytes).digest('hex');
  return {
    ref: { id, path: `${prefix}/images/${digest}.png`, mime: 'image/png', width: 120, height: 80,
      page: null, caption: `${id} public preview`, source_url: sourceUrl },
    bytes,
  };
}

async function writeFixtureFile(root: string, relative: string, contents: string | Buffer): Promise<void> {
  const filename = join(root, relative);
  await mkdir(dirname(filename), { recursive: true });
  await writeFile(filename, contents);
}

async function dataset() {
  const root = await mkdtemp(join(tmpdir(), 'webnews-reader-stage-'));
  temporaryRoots.push(root);
  const source = join(root, 'export');
  const reader = join(root, 'reader');
  const cli = join(reader, 'scripts/stage-data.mjs');
  const output = join(reader, 'public/data');
  await mkdir(dirname(cli), { recursive: true });
  await mkdir(join(reader, 'public'), { recursive: true });
  await Promise.all([copyFile(stageData, cli), copyFile(stagePublicFacts, join(reader, 'scripts/stage-public-facts.mjs'))]);

  const ordinary = image('ordinary-image', Buffer.from('ordinary preview bytes'));
  const shared = image('shared-image', Buffer.from('shared preview bytes'));
  const primaryOnly = image('primary-only-image', Buffer.from('primary preview bytes'));
  const occurrence: PublicOccurrence = {
    id: 'occurrence-1', item_id: 'item-1', issue_id: 'issue-1', document_id: 'document-1', kind: 'detail',
    title: '公开讲座', category: '活动', preview: '公开预览', issue_date: '2026-10-24', source_urls: [sourceUrl],
    source_hosts: ['example.edu'], colleges: [], dates: [{ kind: 'event', normalized_date: '2026-12-20',
      resolution: 'explicit', raw_text: 'December 20', evidence: 'detail evidence' }], expiry_date: null, image_count: 4,
    date_review: null, body_text: '公开正文', college_mentions: [], images: [ordinary.ref, shared.ref],
    primary_images: [shared.ref, primaryOnly.ref], locator: {}, table_cells: [],
  };
  const detailPath = `${prefix}/items/item-1.json`;
  const catalog: Catalog = {
    items: [{ id: 'item-1', aliases: [], latest_occurrence_id: occurrence.id, occurrence_count: 1, detail_path: detailPath }],
    occurrences: [occurrence],
  };
  const issues: PublicIssue[] = [{ id: 'issue-1', title: '归档期次', issue_date: '2026-10-24', issue_number: 1,
    occurrence_count: 1, documents: [{ id: 'document-1', format: 'html', filename: 'issue.html',
      claimed_issue_date: '2026-10-24', issue_date: '2026-10-24', pdf_path: null, source_urls: [sourceUrl], warnings: [] }] }];
  const manifest: SourceManifest = {
    schema_version: 2, generator: 'webnews-static-v2', data_version: version, generated_at: '2026-10-24T00:00:00Z',
    counts: { items: 1, occurrences: 1, issues: 1, documents: 1, images: 3, pdfs: 0 },
    files: { catalog: `${prefix}/catalog.json`, issues: `${prefix}/issues.json`,
      search_documents: `${prefix}/search-documents.json`, search_index: `${prefix}/search-index.json` },
  };

  await Promise.all([
    writeFixtureFile(source, 'manifest.json', JSON.stringify(manifest)),
    writeFixtureFile(source, manifest.files.catalog, JSON.stringify(catalog)),
    writeFixtureFile(source, manifest.files.issues, JSON.stringify(issues)),
    writeFixtureFile(source, manifest.files.search_documents, JSON.stringify([{ id: occurrence.id, title: occurrence.title, text: occurrence.body_text }])),
    writeFixtureFile(source, detailPath, JSON.stringify({ 'item-1': { id: 'item-1', latest_occurrence_id: occurrence.id, occurrences: [occurrence] } })),
    ...[ordinary, shared, primaryOnly].map(({ ref, bytes }) => writeFixtureFile(source, ref.path, bytes)),
  ]);

  return { source, cli, output, manifest, resources: [ordinary, shared, primaryOnly] };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function factsFixture() {
  const sourceUrl = 'https://example.edu/new-activity';
  const olderIssueId = `issue-${'3'.repeat(24)}`;
  const latestIssueId = `issue-${'9'.repeat(24)}`;
  const olderDocumentId = 'b'.repeat(64);
  const latestDocumentId = 'c'.repeat(64);
  const existingItemId = `item-${'1'.repeat(24)}`;
  const existingOccurrenceId = `occ-${'2'.repeat(24)}`;
  const standaloneItemId = `item-${'4'.repeat(24)}`;
  const standaloneOccurrenceId = `occ-${'5'.repeat(24)}`;
  const facts = {
    schema_version: 1,
    items: [
      { id: existingItemId, aliases: [`item-${'7'.repeat(24)}`], latest_occurrence_id: existingOccurrenceId, occurrence_count: 2,
        occurrences: [{ id: `occ-${'8'.repeat(24)}`, item_id: existingItemId, issue_id: olderIssueId, document_id: olderDocumentId,
          kind: 'summary', title: 'Older public activity', category: '活动', preview: 'Older summary',
          issue_date: '2026-03-22', source_urls: [sourceUrl], source_hosts: ['example.edu'], colleges: [], dates: [],
          expiry_date: null, image_count: 0, content_status: 'verified' },
          { id: existingOccurrenceId, item_id: existingItemId, issue_id: latestIssueId, document_id: latestDocumentId,
          kind: 'detail', title: 'Existing public activity', category: '活动', preview: 'Existing public summary',
          issue_date: '2026-10-13', source_urls: [sourceUrl], source_hosts: ['example.edu'], colleges: [],
          dates: [{ kind: 'event', normalized_date: '2026-12-20', resolution: 'explicit', literal_source: 'primary' }],
          expiry_date: null, image_count: 0, content_status: 'verified' }],
      },
      { id: standaloneItemId, aliases: [], latest_occurrence_id: standaloneOccurrenceId, occurrence_count: 1,
        occurrences: [{ id: standaloneOccurrenceId, item_id: standaloneItemId, activity_id: standaloneItemId,
          source_id: `source-${'6'.repeat(24)}`, issue_id: null, document_id: null, kind: 'standalone',
          title: 'Standalone public activity', category: '活动', preview: 'Standalone public summary', issue_date: null,
          source_urls: [sourceUrl], source_hosts: ['example.edu'], colleges: [],
          dates: [{ kind: 'event', normalized_date: '2026-12-21', resolution: 'explicit', literal_source: 'manual' }],
          expiry_date: '2026-12-21', image_count: 0, content_status: 'human_verified',
          activity_facts: { venue: 'Public hall', audience: null, participation: null, time: null },
          manual_review: { revision: 'c'.repeat(64), status: 'verified', reviewed_at: '2026-10-25T10:00:00+08:00',
            reason: 'Synthetic approved public review' } }],
      },
    ],
    issues: [
      { id: olderIssueId, title: 'Older issue', issue_date: '2026-03-22', issue_number: 450,
        occurrence_count: 1, documents: [{ id: olderDocumentId, format: 'pdf', filename: 'older.pdf',
          claimed_issue_date: '2026-03-22', issue_date: '2026-03-22', pdf_path: null, source_urls: [sourceUrl], warnings: [] }] },
      { id: latestIssueId, title: 'Latest issue', issue_date: '2026-10-13', issue_number: 451,
        occurrence_count: 1, documents: [{ id: latestDocumentId, format: 'html', filename: 'notice.html',
          claimed_issue_date: '2026-10-13', issue_date: '2026-10-13', pdf_path: null, source_urls: [sourceUrl], warnings: [] }] },
      { id: `issue-${'a'.repeat(24)}`, title: 'Empty issue', issue_date: '2026-01-01', issue_number: 449,
        occurrence_count: 0, documents: [] },
    ],
  };
  return { ...facts, data_version: createHash('sha256').update(stableJson(facts)).digest('hex') };
}

afterAll(async () => { await Promise.all(temporaryRoots.map((root) => rm(root, { recursive: true, force: true }))); });

describe('public-data staging CLI', () => {
  it('stages ordinary and primary-only previews, deduplicating their shared resource', async () => {
    const fixture = await dataset();
    const { stdout } = await run(process.execPath, [fixture.cli, fixture.source]);
    const report = JSON.parse(stdout);
    const stagedManifest = JSON.parse(await readFile(join(fixture.output, 'manifest.json'), 'utf8')) as SourceManifest;
    const expectedPaths = ['manifest.json', fixture.manifest.files.catalog, fixture.manifest.files.issues,
      fixture.manifest.files.search_documents, `${prefix}/items/item-1.json`, ...fixture.resources.map(({ ref }) => ref.path)];
    const stagedFiles = await Promise.all(expectedPaths.map((relative) => readFile(join(fixture.output, relative))));
    const stagedCatalog = JSON.parse(await readFile(join(fixture.output, stagedManifest.files.catalog), 'utf8'));
    expect(stagedCatalog.occurrences[0]).toMatchObject({ title: '公开讲座', source_urls: [sourceUrl] });

    expect(stagedManifest.counts.images).toBe(fixture.resources.length);
    expect(report).toMatchObject({ data_version: version, files: expectedPaths.length, counts: stagedManifest.counts,
      output: 'public/data' });
    expect(report.bytes).toBe(stagedFiles.reduce((total, file) => total + file.byteLength, 0));
    for (const resource of fixture.resources) {
      await expect(readFile(join(fixture.output, resource.ref.path))).resolves.toEqual(resource.bytes);
    }
  });

  it('builds a searchable compact reader from only tracked public facts', async () => {
    const fixture = await dataset();
    const reader = dirname(dirname(fixture.cli));
    await writeFixtureFile(reader, 'public/activity-facts.json', JSON.stringify(factsFixture()));
    const { stdout } = await run(process.execPath, [fixture.cli], { env: { ...process.env, STATIC_READER_DATA_SOURCE: '' } });
    const report = JSON.parse(stdout);
    const manifest = JSON.parse(await readFile(join(fixture.output, 'manifest.json'), 'utf8')) as SourceManifest;
    const catalog = JSON.parse(await readFile(join(fixture.output, manifest.files.catalog), 'utf8')) as Catalog;
    const issues = JSON.parse(await readFile(join(fixture.output, manifest.files.issues), 'utf8')) as PublicIssue[];
    const factsOccurrence = catalog.occurrences.find((row) => row.kind === 'standalone');
    const standaloneItem = catalog.items.find((item) => item.id === factsOccurrence?.item_id);
    const shard = JSON.parse(await readFile(join(fixture.output, standaloneItem!.detail_path!), 'utf8'));
    const standaloneDetail = shard[standaloneItem!.id].occurrences[0];

    expect(report.source).toBe('activity-facts');
    expect(manifest.public_facts_only).toBe(true);
    expect(manifest.generated_at).toBeUndefined();
    expect(manifest.counts).toEqual({ items: 2, occurrences: 3, issues: 3, documents: 2, images: 0, pdfs: 0 });
    expect(issues.map((issue) => issue.id)).toEqual([`issue-${'9'.repeat(24)}`, `issue-${'3'.repeat(24)}`, `issue-${'a'.repeat(24)}`]);
    expect(issues[0].issue_date).toBe('2026-10-13');
    expect(factsOccurrence).toMatchObject({ kind: 'standalone', issue_id: null, document_id: null,
      content_status: 'human_verified', source_id: `source-${'6'.repeat(24)}` });
    expect(standaloneDetail.body_text).toBe('');
    expect(standaloneDetail.date_review).toBeNull();
    expect(standaloneDetail.manual_review).not.toHaveProperty('activity');
    const older = catalog.occurrences.find((row) => row.issue_id === `issue-${'3'.repeat(24)}`);
    expect(older).toEqual(factsFixture().items[0].occurrences[0]);
    expect(catalog.items[0]).toMatchObject({ latest_occurrence_id: `occ-${'2'.repeat(24)}`, occurrence_count: 2,
      aliases: [`item-${'7'.repeat(24)}`] });
    expect(issues.find((issue) => issue.id === `issue-${'a'.repeat(24)}`)?.occurrence_count).toBe(0);
    await run(process.execPath, [buildSearch, fixture.output]);
    const index = JSON.parse(await readFile(join(fixture.output, manifest.files.search_index), 'utf8'));
    expect(index.documentCount).toBe(3);
    const searchDocuments = JSON.parse(await readFile(join(fixture.output, manifest.files.search_documents), 'utf8'));
    expect(searchDocuments.map((row: { title: string }) => row.title)).toContain('Standalone public activity');
  });


  it('does not retain obsolete browser projections in the operator source contract', async () => {
    const fixture = await dataset();
    const expectedFiles = { ...fixture.manifest.files };
    fixture.manifest.files.issue_catalogs = { 'issue-1': '../../untrusted.json' };
    fixture.manifest.files.item_index = '../../untrusted-index.json';
    await writeFixtureFile(fixture.source, 'manifest.json', JSON.stringify(fixture.manifest));
    await run(process.execPath, [fixture.cli, fixture.source]);
    const manifest = JSON.parse(await readFile(join(fixture.output, 'manifest.json'), 'utf8')) as SourceManifest;
    expect(manifest.files).toEqual(expectedFiles);
  });

  it('rejects mismatched issue membership instead of dropping occurrences', async () => {
    const fixture = await dataset();
    const issuePath = join(fixture.source, fixture.manifest.files.issues);
    const issues = JSON.parse(await readFile(issuePath, 'utf8'));
    issues[0].occurrence_count = 0;
    await writeFile(issuePath, JSON.stringify(issues));
    await expect(run(process.execPath, [fixture.cli, fixture.source])).rejects.toThrow();
    await expect(readFile(join(fixture.output, 'manifest.json'))).rejects.toThrow();
  });

  it('rejects an incomplete projection pair', async () => {
    const fixture = await dataset();
    fixture.manifest.files.item_index = `${prefix}/item-index.json`;
    await writeFixtureFile(fixture.source, 'manifest.json', JSON.stringify(fixture.manifest));
    await expect(run(process.execPath, [fixture.cli, fixture.source])).rejects.toThrow();
    await expect(readFile(join(fixture.output, 'manifest.json'))).rejects.toThrow();
  });

  it.each(['tampered', 'missing'] as const)('rejects a %s primary-only preview resource', async (failure) => {
    const fixture = await dataset();
    const primaryOnly = fixture.resources[2];
    const primarySource = join(fixture.source, primaryOnly.ref.path);
    if (failure === 'tampered') await writeFile(primarySource, Buffer.from('modified preview bytes'));
    else await rm(primarySource);

    await expect(run(process.execPath, [fixture.cli, fixture.source])).rejects.toThrow();
    await expect(readFile(join(fixture.output, 'manifest.json'))).rejects.toThrow();
  });
});
