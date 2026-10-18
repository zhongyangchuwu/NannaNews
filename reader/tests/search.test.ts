import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import MiniSearch from 'minisearch';
import { execFile } from 'node:child_process';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { beijingToday, defaultFilters, FilterError, getFreshness, parseFilters, queryCatalog, serializeFilters, sourceTypeForHost } from '../src/filter';
import { searchOptions } from '../src/tokenize.mjs';
import type { Catalog, CatalogOccurrence, Filters, Manifest, PublicIssue, QueryOptions } from '../src/types';

const run = promisify(execFile);
const builder = fileURLToPath(new URL('../scripts/build-search.mjs', import.meta.url));
const version = 'a'.repeat(64);
const temporaryRoots: string[] = [];

function occurrence(id: string, itemId: string, patch: Partial<CatalogOccurrence> = {}): CatalogOccurrence {
  return { id, item_id: itemId, issue_id: 'issue-new', document_id: 'document-new', kind: 'detail', title: '消息',
    category: '活动', preview: '预览不包含正文关键词', issue_date: '2026-10-14', source_urls: ['https://example.edu/news'],
    source_hosts: ['example.edu'], colleges: ['计算机学院'], dates: [], expiry_date: null, image_count: 0, ...patch };
}

function catalog(occurrences: CatalogOccurrence[]): Catalog {
  const byItem = new Map<string, Catalog['items'][number]>();
  for (const row of occurrences) {
    const existing = byItem.get(row.item_id);
    if (existing) existing.occurrence_count += 1;
    else byItem.set(row.item_id, { id: row.item_id, aliases: [], latest_occurrence_id: row.id, occurrence_count: 1,
      detail_path: `versions/${version}/items/aa.json` });
  }
  return { items: [...byItem.values()], occurrences };
}

function options(patch: Partial<QueryOptions> = {}): QueryOptions {
  return { mode: 'all', favorites: new Set(), followedColleges: new Set(), asOf: '2026-10-14', searchHits: null, ...patch };
}

const rows = [
  occurrence('new', 'history', { title: '本周更新' }),
  occurrence('title-hit', 'title', { title: '讲座' }),
  occurrence('body-hit', 'body', { title: '报告' }),
  occurrence('wildcard', 'wildcard'),
  occurrence('old', 'history', { title: '往期通知', issue_id: 'issue-old', document_id: 'document-old', issue_date: '2025-05-01',
    category: '招聘', colleges: ['物理学院'], source_hosts: ['old.example.edu'] }),
];
const fullTexts: Record<string, string> = {
  new: '普通更新，没有历史关键词。',
  'title-hit': '报名',
  'body-hit': '讲座',
  wildcard: '优惠AB报名，百分之百完成。',
  old: '正文深处的量子AI cafe\u0301研究岗位。优惠%_报名。',
};

async function dataset(): Promise<{ root: string; manifest: Manifest }> {
  const root = await mkdtemp(join(tmpdir(), 'webnews-reader-search-'));
  temporaryRoots.push(root);
  await mkdir(join(root, `versions/${version}`), { recursive: true });
  const prefix = `versions/${version}`;
  const manifest: Manifest = { schema_version: 2, generator: 'webnews-static-v2', data_version: version,
    generated_at: '2026-10-14T00:00:00Z', counts: { items: 4, occurrences: 5, issues: 2, documents: 2, images: 0, pdfs: 0 },
    files: { catalog: `${prefix}/catalog.json`, issues: `${prefix}/issues.json`,
      search_documents: `${prefix}/search-documents.json`, search_index: `${prefix}/search-index.json` } };
  await Promise.all([
    writeFile(join(root, 'manifest.json'), JSON.stringify(manifest)),
    writeFile(join(root, manifest.files.catalog), JSON.stringify(catalog(rows))),
    writeFile(join(root, manifest.files.search_documents), JSON.stringify(rows.map((row) =>
      ({ id: row.id, title: row.title, text: fullTexts[row.id] })))),
    writeFile(join(root, manifest.files.issues), JSON.stringify([
      { id: 'issue-new', title: '本期消息', issue_date: '2026-10-14', issue_number: 2, occurrence_count: 4,
        documents: [{ id: 'document-new', format: 'html', filename: 'new.html', claimed_issue_date: '2026-10-14',
          issue_date: '2026-10-14', pdf_path: null, source_urls: [], warnings: [] }] },
      { id: 'issue-old', title: '往期消息', issue_date: '2025-05-01', issue_number: 1, occurrence_count: 1,
        documents: [{ id: 'document-old', format: 'html', filename: 'old.html', claimed_issue_date: '2025-05-01',
          issue_date: '2025-05-01', pdf_path: null, source_urls: [], warnings: [] }] },
    ] satisfies PublicIssue[])),
  ]);
  return { root, manifest };
}

afterAll(async () => { await Promise.all(temporaryRoots.map((root) => rm(root, { recursive: true, force: true }))); });

describe('the offline index consumed with browser options', () => {
  let index: MiniSearch;
  beforeAll(async () => {
    const { root, manifest } = await dataset();
    await run(process.execPath, [builder, root]);
    index = MiniSearch.loadJSON(await readFile(join(root, manifest.files.search_index), 'utf8'), searchOptions);
  });

  it('finds two-character Chinese words deep in full body rather than just title/preview', () => {
    expect(index.search('量子').map((hit) => hit.id)).toEqual(['old']);
    expect(index.search('正文').map((hit) => hit.id)).toEqual(['old']);
  });

  it('finds mixed adjacent CJK/English and canonical/case-normalized English', () => {
    expect(index.search('量子ai CAFÉ').map((hit) => hit.id)).toEqual(['old']);
  });

  it('treats percent and underscore literally, never as wildcard syntax', () => {
    expect(index.search('%_').map((hit) => hit.id)).toEqual(['old']);
    expect(index.search('%').map((hit) => hit.id)).toEqual(['old']);
    expect(index.search('_').map((hit) => hit.id)).toEqual(['old']);
  });

  it('weights title evidence above the same word in a body', () => {
    expect(index.search('讲座').map((hit) => hit.id)).toEqual(['title-hit', 'body-hit']);
  });

  it('keeps historical search evidence available to issue/category/college/source filters', () => {
    const filters = { ...defaultFilters(), q: '量子ai', category: ['招聘'], college: ['物理学院'], source: ['官网'],
      issue_from: '2025-05-01', issue_to: '2025-05-01' };
    const searchHits = index.search(filters.q).map((hit) => String(hit.id));
    const result = queryCatalog(catalog(rows), filters, options({ searchHits, issueId: 'issue-old' }));
    expect(result.records.map((row) => row.id)).toEqual(['old']);
  });
});

describe('builder manifest trust boundary', () => {
  it('rejects counts that do not describe the exported search documents', async () => {
    const { root, manifest } = await dataset();
    manifest.counts.occurrences += 1;
    await writeFile(join(root, 'manifest.json'), JSON.stringify(manifest));
    await expect(run(process.execPath, [builder, root])).rejects.toThrow();
    await expect(readFile(join(root, manifest.files.search_index))).rejects.toThrow();
  });

  it('rejects traversal instead of writing outside the data version', async () => {
    const { root, manifest } = await dataset();
    manifest.files.search_index = `versions/${version}/../escape.json`;
    await writeFile(join(root, 'manifest.json'), JSON.stringify(manifest));
    await expect(run(process.execPath, [builder, root])).rejects.toThrow();
    await expect(readFile(join(root, 'versions/escape.json'))).rejects.toThrow();
  });

  it('rejects a symlink output and leaves its target untouched', async () => {
    const { root, manifest } = await dataset();
    const target = join(root, 'untouched.json');
    await writeFile(target, 'untouched');
    await symlink(target, join(root, manifest.files.search_index));
    await expect(run(process.execPath, [builder, root])).rejects.toThrow();
    expect(await readFile(target, 'utf8')).toBe('untouched');
  });
});

describe('shareable filters', () => {
  it('round-trips literal queries, repeated choices, booleans, and inclusive date ranges', () => {
    const filters: Filters = { ...defaultFilters(), q: '量子AI %_', category: ['活动', '招聘'], college: ['物理学院'],
      source: ['官网', '微信公众号'], following: true, page: 2, issue_from: '2024-02-29', issue_to: '2024-03-01',
      date_type: 'deadline', date_status: 'known', date_from: '2026-10-14', date_to: '2026-10-14' };
    expect(parseFilters(serializeFilters(filters))).toEqual(filters);
    expect(parseFilters(new URLSearchParams())).toEqual(defaultFilters());
  });

  it.each([
    'issue_from=2025-02-29', 'date_to=2026-04-31', 'date_from=2026-1-01', 'issue_from=0000-01-01',
    'issue_from=2026-10-15&issue_to=2026-10-14', 'date_from=2026-10-15&date_to=2026-10-14',
    'date_type=other', 'date_status=maybe', 'following=2', 'page=0', 'page=-1', 'page=1.5',
    'page=9007199254740992', 'date_from=2026-10-14&date_from=2026-10-15',
  ])('reports invalid filters rather than silently changing %s', (query) => {
    expect(() => parseFilters(new URLSearchParams(query))).toThrow(FilterError);
  });
});

describe('occurrence-first catalogue selection', () => {
  it('chooses latest matching evidence, not latest overall or highest scored search hit', () => {
    const data = catalog(rows);
    expect(queryCatalog(data, defaultFilters(), options()).records[0]?.id).toBe('new');
    expect(queryCatalog(data, { ...defaultFilters(), q: '岗位' }, options({ searchHits: ['old'] })).records.map((row) => row.id)).toEqual(['old']);
    expect(queryCatalog(data, { ...defaultFilters(), q: '消息' }, options({ searchHits: ['old', 'new'] })).records.map((row) => row.id)).toEqual(['new']);
    expect(queryCatalog(data, { ...defaultFilters(), q: '不存在' }, options({ searchHits: [] })).total).toBe(0);
  });

  it('uses OR within choices and AND across choices, following, favorites, and issue', () => {
    const filters = { ...defaultFilters(), category: ['活动', '招聘'], college: ['物理学院', '其他学院'],
      source: ['官网'], following: true };
    const prefs = options({ mode: 'favorites', favorites: new Set(['history']), followedColleges: new Set(['物理学院']), issueId: 'issue-old' });
    expect(queryCatalog(catalog(rows), filters, prefs).records.map((row) => row.id)).toEqual(['old']);
    expect(queryCatalog(catalog(rows), filters, { ...prefs, followedColleges: new Set() }).total).toBe(0);
    expect(queryCatalog(catalog(rows), filters, { ...prefs, favorites: new Set() }).total).toBe(0);
    expect(queryCatalog(catalog(rows), { ...filters, category: ['活动'] }, prefs).total).toBe(0);
  });

  it('paginates twenty collapsed items and clamps an out-of-range page', () => {
    const occurrences = Array.from({ length: 21 }, (_, i) => occurrence(`occ-${i}`, `item-${i}`));
    occurrences.push(occurrence('duplicate-old', 'item-0', { issue_date: '2020-01-01' }));
    const data = catalog(occurrences);
    const first = queryCatalog(data, defaultFilters(), options());
    expect([first.total, first.page, first.pages, first.per_page, first.records.length]).toEqual([21, 1, 2, 20, 20]);
    expect(queryCatalog(data, { ...defaultFilters(), page: 99 }, options()).records.map((row) => row.id)).toEqual(['occ-20']);
    expect(queryCatalog(catalog([]), defaultFilters(), options())).toEqual({ records: [], total: 0, page: 1, pages: 1, per_page: 20 });
  });
});

describe('source type filtering', () => {
  it.each([
    ['mp.weixin.qq.com', '微信公众号'],
    ['news.mp.weixin.qq.com', '微信公众号'],
    ['mp.weixin.qq.com.evil.example', '其他'],
    ['nju.edu.cn.evil.example', '其他'],
    ['MATH.NJU.EDU.CN.', '官网'],
    ['', '其他'],
    ['nju.edu.cn', '官网'],
    ['math.nju.edu.cn', '官网'],
    ['fake-nju.edu.cn', '其他'],
    ['mit.edu', '官网'],
    ['cet.neea.edu.cn', '官网'],
    ['www.moe.gov.cn', '官网'],
    ['hpcgame.pku.edu.cn', '官网'],
    ['www.csc.edu.cn', '官网'],
    ['nik-nul.github.io', '其他'],
  ])('classifies %s as %s', (host, type) => {
    expect(sourceTypeForHost(host)).toBe(type);
  });

  it('matches any selected source type before collapsing duplicate items', () => {
    const data = catalog([
      occurrence('other-first', 'shared', { source_hosts: ['nik-nul.github.io'], source_urls: ['https://nik-nul.github.io/news'] }),
      occurrence('official-later', 'shared', { source_hosts: ['math.nju.edu.cn'], source_urls: ['https://math.nju.edu.cn/news'] }),
      occurrence('official-only', 'official', { source_hosts: ['sme.nju.edu.cn', 'nik-nul.github.io'],
        source_urls: ['https://sme.nju.edu.cn/news', 'https://nik-nul.github.io/news'] }),
      occurrence('wechat-only', 'wechat', { source_hosts: ['mp.weixin.qq.com', 'nik-nul.github.io'],
        source_urls: ['https://mp.weixin.qq.com/s/example', 'https://nik-nul.github.io/news'] }),
      occurrence('multi-source', 'mixed', { source_hosts: ['mp.weixin.qq.com', 'math.nju.edu.cn'],
        source_urls: ['https://mp.weixin.qq.com/s/example', 'https://math.nju.edu.cn/news'] }),
      occurrence('no-source', 'unknown', { source_hosts: [], source_urls: [] }),
    ]);
    const selected = (source: string[]) => queryCatalog(data, { ...defaultFilters(), source }, options()).records;

    expect(selected(['官网']).map((row) => row.id)).toEqual(['official-later', 'official-only', 'multi-source']);
    expect(selected(['微信公众号']).map((row) => row.id)).toEqual(['wechat-only', 'multi-source']);
    expect(selected(['其他']).map((row) => row.id)).toEqual(['other-first', 'no-source']);
    expect(selected(['官网', '微信公众号']).map((row) => row.id)).toEqual([
      'official-later', 'official-only', 'wechat-only', 'multi-source',
    ]);
    expect(selected(['微信公众号']).find((row) => row.id === 'multi-source')).toMatchObject({
      source_hosts: ['mp.weixin.qq.com', 'math.nju.edu.cn'],
      source_urls: ['https://mp.weixin.qq.com/s/example', 'https://math.nju.edu.cn/news'],
    });
  });
});

describe('source-grounded dates and freshness', () => {
  const dates = catalog([
    occurrence('same-day', 'same-day', { dates: [{ kind: 'event', resolution: 'explicit', normalized_date: '2026-11-01' },
      { kind: 'deadline', resolution: 'explicit', normalized_date: '2026-10-14' }], expiry_date: '2026-10-14' }),
    occurrence('expired-deadline', 'expired', { dates: [{ kind: 'event', resolution: 'explicit', normalized_date: '2026-11-01' },
      { kind: 'deadline', resolution: 'explicit', normalized_date: '2026-10-13' }], expiry_date: '2026-10-13' }),
    occurrence('unknown-year', 'unknown', { dates: [{ kind: 'event', resolution: 'year_unknown', normalized_date: '2026-11-01' }] }),
    occurrence('ambiguous', 'ambiguous', { dates: [{ kind: 'event', resolution: 'ambiguous', normalized_date: '2026-11-01' }] }),
    occurrence('listed-only', 'listed', { dates: [{ kind: 'listed', resolution: 'explicit', normalized_date: '2026-10-14' }] }),
    occurrence('no-dates', 'none'),
    occurrence('contextual', 'contextual', { dates: [{ kind: 'event', resolution: 'contextual', normalized_date: '2026-10-15' }], expiry_date: '2026-10-15' }),
    occurrence('opening', 'opening', { dates: [{ kind: 'event', resolution: 'explicit', normalized_date: '2026-01-01' }], expiry_date: null }),
  ]);

  it('expires after, not on, the cutoff day, and computes Beijing calendar midnight', () => {
    expect(getFreshness('2026-10-14', '2026-10-14')).toBe('not_expired');
    expect(getFreshness('2026-10-13', '2026-10-14')).toBe('expired');
    expect(getFreshness(null, '2026-10-14')).toBe('unknown');
    expect(getFreshness('2026-02-30', '2026-10-14')).toBe('unknown');
    expect(beijingToday(new Date('2026-10-13T15:59:59Z'))).toBe('2026-10-13');
    expect(beijingToday(new Date('2026-10-13T16:00:00Z'))).toBe('2026-10-14');
  });

  it('uses reviewed expiry, retaining an ongoing service opening as a fact rather than an ending', () => {
    expect(queryCatalog(dates, defaultFilters(), options({ mode: 'upcoming' })).records.map((row) => row.id)).toEqual(['same-day', 'contextual']);
    expect(queryCatalog(dates, defaultFilters(), options({ mode: 'unknown' })).records.map((row) => row.id)).toEqual(['unknown-year', 'ambiguous', 'listed-only', 'no-dates', 'opening']);
  });

  it('accepts evidence-backed contextual dates but not unresolved dates for the selected type', () => {
    const known = { ...defaultFilters(), date_type: 'event' as const, date_status: 'known' as const };
    expect(queryCatalog(dates, known, options()).records.map((row) => row.id)).toEqual(['same-day', 'expired-deadline', 'contextual', 'opening']);
    expect(queryCatalog(dates, { ...known, date_status: 'unknown' }, options()).total).toBe(4);
    expect(queryCatalog(dates, { ...known, date_from: '2026-11-01', date_to: '2026-11-01' }, options()).total).toBe(2);
    expect(queryCatalog(dates, { ...known, date_status: 'unknown', date_from: '2026-11-01' }, options()).total).toBe(0);
    expect(queryCatalog(dates, { ...known, date_type: 'listed', date_from: '2026-10-14', date_to: '2026-10-14' }, options()).records.map((row) => row.id)).toEqual(['listed-only']);
  });
});
