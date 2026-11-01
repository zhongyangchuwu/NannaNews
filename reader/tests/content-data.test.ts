import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import type { StaticDataset } from '../src/data';
import type { ActivityReview, Catalog, Manifest, ManualActivity, PublicDate, PublicIssue, PublicOccurrence, SourceReview } from '../src/types';
import { currentActivityDate } from '../src/filter';
import { encodePartitionedFacts } from '../src/public-format.mjs';

const DATA_VERSION = 'a'.repeat(64);
const ITEM_ID = `item-${'1'.repeat(24)}`;
const OCCURRENCE_ID = `occ-${'2'.repeat(24)}`;
const ISSUE_ID = `issue-${'3'.repeat(24)}`;
const DOCUMENT_ID = 'b'.repeat(64);
const SOURCE_URL = 'https://example.edu/events/open-lecture';
const SOURCE_QUOTE = '本次公开讲座11月2日；计算机学院参与活动。';
const CURRENT_RULES = 'luna-content-evidence-v4';
const LEGACY_RULES = 'luna-content-evidence-v3';

type FullManualOccurrence = Omit<PublicOccurrence, 'manual_review'> & {
  manual_review: NonNullable<PublicOccurrence['manual_review']> & { activity: ManualActivity };
};

function hasFullManualActivity(row: PublicOccurrence): row is FullManualOccurrence {
  return row.manual_review?.activity !== undefined;
}

function fullManualActivity(row: PublicOccurrence): ManualActivity {
  const activity = row.manual_review?.activity;
  if (!activity) throw new Error('Fixture must contain full published manual activity.');
  return activity;
}

const citation = { url: SOURCE_URL, text: SOURCE_QUOTE, image_index: null };
const unknownField = { value: null, evidence: [] };
function knownField(value: string) {
  return { value, evidence: [citation] };
}

function activityFixture(): ActivityReview {
  return {
    title: knownField('公开讲座'),
    summary: knownField('面向高校的公开讲座'),
    content: knownField('介绍讲座安排和参与方式'),
    venue: unknownField,
    organizer: unknownField,
    audience: unknownField,
    participation: unknownField,
    time: unknownField,
    colleges: [{ name: '计算机学院', relation: 'associated', evidence: citation }],
    image_indices: [3],
  };
}

function dateFixture(): PublicDate {
  return { kind: 'event', raw_text: '11月2日', normalized_date: '2026-11-02', resolution: 'explicit', evidence: '原文明确给出日期。' };
}

function sourceReviewFixture(activity: ActivityReview | null = activityFixture()): SourceReview {
  return {
    status: 'source_verified',
    reason: '已对照对应的原始页面。',
    sources: [{ url: SOURCE_URL, resolved_url: SOURCE_URL, title: '公开讲座', sha256: DATA_VERSION,
      fetched_at: '2026-10-17T00:00:00Z', published_date: null, quotes: [SOURCE_QUOTE] }],
    activity,
  };
}

function occurrenceFixture(patch: Partial<PublicOccurrence> = {}): PublicOccurrence {
  const date = dateFixture();
  const claim = { ...date, literal_source: 'primary' as const };
  return {
    id: OCCURRENCE_ID, item_id: ITEM_ID, issue_id: ISSUE_ID, document_id: DOCUMENT_ID,
    kind: 'detail', title: '原刊标题', category: '活动', preview: '档案预览', issue_date: '2026-10-17',
    source_urls: [SOURCE_URL], source_hosts: ['example.edu'], colleges: ['计算机学院'], dates: [date],
    expiry_date: null, image_count: 0, date_review: { status: 'reviewed', reason: '当前规则核验。', rules_version: CURRENT_RULES,
      claims: [claim], source_review: sourceReviewFixture() },
    body_text: '原刊正文', original_dates: [date], college_mentions: [],
    images: [], primary_images: [{ id: 'primary-image-1', path: `versions/${DATA_VERSION}/images/primary.jpg`, mime: 'image/jpeg',
      width: 1200, height: 700, page: null, caption: '原文配图安全预览', source_url: SOURCE_URL }],
    locator: {}, table_cells: [], ...patch,
  };
}

function manualOccurrence(status: 'verified' | 'needs_review' = 'verified'): FullManualOccurrence {
  const activity = { title: '人工活动标题', summary: '人工活动摘要', content: '人工核实的完整活动正文', category: '讲座',
    venue: '人工确认地点', audience: null, participation: 'https://example.edu/register', time: null, organizer: null,
    source_urls: [SOURCE_URL], dates: [{ kind: 'event' as const, date: '2026-11-02' }] };
  const row = occurrenceFixture({ content_status: status === 'verified' ? 'human_verified' : 'needs_review',
    manual_review: { revision: 'c'.repeat(64), status, reviewed_at: '2026-10-25T10:00:00+08:00', reason: '公开更正说明', activity },
    dates: status === 'verified' ? [{ kind: 'event', normalized_date: '2026-11-02', resolution: 'explicit', literal_source: 'manual',
      raw_text: '2026-11-02', evidence: '公开更正说明' }] : [],
    activity_facts: status === 'verified' ? { venue: activity.venue, audience: activity.audience, participation: activity.participation, time: activity.time } : undefined,
  });
  if (!hasFullManualActivity(row)) throw new Error('Manual fixture must include its full activity payload.');
  return row;
}

function datasetPayload(occurrence: PublicOccurrence) {
  const prefix = `versions/${DATA_VERSION}`;
  const manifest: Manifest = {
    schema_version: 2, generator: 'webnews-static-v2', data_version: DATA_VERSION,
    generated_at: '2026-10-17T00:00:00Z', content_rules_version: CURRENT_RULES,
    counts: { items: 1, occurrences: 1, issues: 1, documents: 1, images: 0, pdfs: 0 },
    files: { catalog: `${prefix}/catalog.json`, issues: `${prefix}/issues.json`,
      search_documents: `${prefix}/search-documents.json`, search_index: `${prefix}/search-index.json` },
  };
  const catalog: Catalog = {
    items: [{ id: ITEM_ID, aliases: [], latest_occurrence_id: OCCURRENCE_ID, occurrence_count: 1, detail_path: `${prefix}/items/detail.json` }],
    occurrences: [{ id: OCCURRENCE_ID, item_id: ITEM_ID, issue_id: ISSUE_ID, document_id: DOCUMENT_ID,
      kind: 'detail', title: '原刊标题', category: '活动', preview: '档案预览', issue_date: '2026-10-17',
      source_urls: [SOURCE_URL], source_hosts: ['example.edu'], colleges: ['计算机学院'],
      dates: occurrence.dates.map(({ kind, normalized_date, resolution, literal_source }) => ({ kind, normalized_date, resolution, ...(literal_source ? { literal_source } : {}) })),
      content_status: occurrence.content_status, activity_facts: occurrence.activity_facts,
      ...(occurrence.manual_review ? { manual_review: { revision: occurrence.manual_review.revision, status: occurrence.manual_review.status,
        reason: occurrence.manual_review.reason, reviewed_at: occurrence.manual_review.reviewed_at } } : {}),
      expiry_date: null, image_count: 0 }],
  };
  const issues: PublicIssue[] = [{ id: ISSUE_ID, title: '档案期次', issue_date: '2026-10-17', issue_number: 1, occurrence_count: 1,
    documents: [{ id: DOCUMENT_ID, format: 'html', filename: 'issue.html', claimed_issue_date: '2026-10-17',
      issue_date: '2026-10-17', pdf_path: null, source_urls: [SOURCE_URL], warnings: [] }] }];
  const detail = { id: ITEM_ID, latest_occurrence_id: OCCURRENCE_ID, occurrences: [occurrence] };
  return new Map<string, unknown>([
    ['/data/manifest.json', manifest], [`/data/${manifest.files.catalog}`, catalog], [`/data/${manifest.files.issues}`, issues],
    [`/data/${prefix}/items/detail.json`, { [ITEM_ID]: detail }],
  ]);
}

let loadDataset: (signal?: AbortSignal) => Promise<StaticDataset>;
beforeAll(async () => {
  vi.stubGlobal('document', { baseURI: 'https://reader.test/' });
  // data.ts captures document.baseURI during module initialization, so install the test document before importing it.
  const dataModule = await import('../src/data');
  loadDataset = dataModule.loadDataset;
});
afterAll(() => vi.unstubAllGlobals());

async function loadOccurrence(occurrence: PublicOccurrence): Promise<PublicOccurrence> {
  const payloads = datasetPayload(occurrence);
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const payload = payloads.get(new URL(String(input)).pathname);
    return new Response(JSON.stringify(payload ?? null), { status: payload === undefined ? 404 : 200 });
  });
  const dataset = await loadDataset();
  return (await dataset.loadItem(ITEM_ID)).occurrences[0];
}

async function loadStandalone(factsOnly: boolean) {
  const version = 'd'.repeat(64);
  const itemId = `item-${'8'.repeat(24)}`;
  const occurrenceId = `occ-${'9'.repeat(24)}`;
  const sourceId = `source-${'a'.repeat(24)}`;
  const reason = 'Public standalone review';
  const activity = { title: 'Standalone activity', summary: 'Standalone summary', content: 'Standalone reviewed content',
    category: '活动', time: null, venue: 'Public hall', organizer: null, audience: null, participation: null,
    source_urls: [SOURCE_URL], dates: [{ kind: 'event' as const, date: '2026-11-02' }] };
  const stamp = { revision: 'c'.repeat(64), status: 'verified' as const,
    reviewed_at: '2026-10-25T10:00:00+08:00', reason };
  const catalogOccurrence = { id: occurrenceId, item_id: itemId, activity_id: itemId, source_id: sourceId,
    issue_id: null, document_id: null, kind: 'standalone', title: activity.title, category: activity.category,
    preview: activity.summary, issue_date: null, source_urls: [SOURCE_URL], source_hosts: ['example.edu'], colleges: [],
    dates: [{ kind: 'event', normalized_date: '2026-11-02', resolution: 'explicit', literal_source: 'manual' }],
    expiry_date: '2026-11-02', image_count: 0, content_status: 'human_verified',
    activity_facts: { venue: 'Public hall', audience: null, participation: null, time: null }, manual_review: stamp };
  const detailOccurrence = { ...catalogOccurrence, body_text: '', dates: [{ kind: 'event', raw_text: '2026-11-02',
    normalized_date: '2026-11-02', resolution: 'explicit', literal_source: 'manual', evidence: factsOnly ? '' : reason }],
    date_review: null, original_dates: [], college_mentions: [], images: [], primary_images: [], locator: {}, table_cells: [],
    manual_review: factsOnly ? stamp : { ...stamp, activity } };
  const prefix = `versions/${version}`;
  const manifest: Manifest = { schema_version: 2, generator: 'webnews-static-v2', data_version: version,
    ...(factsOnly ? { public_facts_only: true } : { generated_at: '2026-10-25T10:00:00+08:00' }),
    content_rules_version: factsOnly ? 'public-facts-v1' : CURRENT_RULES,
    counts: { items: 1, occurrences: 1, issues: 0, documents: 0, images: 0, pdfs: 0 },
    files: { catalog: `${prefix}/catalog.json`, issues: `${prefix}/issues.json`,
      search_documents: `${prefix}/search-documents.json`, search_index: `${prefix}/search-index.json` } };
  const payloads = new Map<string, unknown>([
    ['/data/manifest.json', manifest],
    [`/data/${manifest.files.catalog}`, { items: [{ id: itemId, aliases: [], latest_occurrence_id: occurrenceId,
      occurrence_count: 1, detail_path: `${prefix}/items/${itemId.slice(5, 7)}.json` }], occurrences: [catalogOccurrence] }],
    [`/data/${manifest.files.issues}`, []],
    [`/data/${prefix}/items/${itemId.slice(5, 7)}.json`, { [itemId]: { id: itemId,
      latest_occurrence_id: occurrenceId, occurrences: [detailOccurrence] } }],
  ]);
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const payload = payloads.get(new URL(String(input)).pathname);
    return new Response(JSON.stringify(payload ?? null), { status: payload === undefined ? 404 : 200 });
  });
  const dataset = await loadDataset();
  return { dataset, occurrence: (await dataset.loadItem(itemId)).occurrences[0] };
}

describe('public historical content compatibility', () => {
  it('keeps legacy date-only records readable without treating them as normalized activity', async () => {
    const legacy = occurrenceFixture();
    const legacySourceReview = sourceReviewFixture(null);
    delete legacySourceReview.activity;
    legacy.date_review = { status: 'reviewed', reason: '历史日期核查。', rules_version: LEGACY_RULES, source_review: legacySourceReview };
    delete legacy.original_dates;
    delete legacy.primary_images;

    const loaded = await loadOccurrence(legacy);

    expect(loaded.dates[0]?.raw_text).toBe('11月2日');
    expect(loaded.date_review?.claims).toBeUndefined();
    expect(loaded.date_review?.source_review.activity).toBeUndefined();
    expect(loaded.original_dates).toBeUndefined();
    expect(loaded.primary_images).toBeUndefined();
  });

  it('accepts fully shaped current activity, claim, original-date, and attributed preview fields', async () => {
    const loaded = await loadOccurrence(occurrenceFixture());

    expect(loaded.date_review?.source_review.activity?.title.value).toBe('公开讲座');
    expect(loaded.date_review?.claims?.[0]).toMatchObject({ normalized_date: '2026-11-02', literal_source: 'primary' });
    expect(loaded.original_dates?.[0]?.raw_text).toBe('11月2日');
    expect(loaded.primary_images?.[0]?.source_url).toBe(SOURCE_URL);
  });
  it('accepts current date claims without an optional literal origin', async () => {
    const occurrence = occurrenceFixture();
    const review = occurrence.date_review;
    if (!review) throw new Error('fixture must include its review');
    review.claims = [dateFixture()];

    const loaded = await loadOccurrence(occurrence);

    expect(loaded.date_review?.claims?.[0]?.literal_source).toBeUndefined();
  });

  it.each([
    ['unknown content confirmation status', (occurrence: Record<string, unknown>) => {
      occurrence.content_status = 'verified_without_evidence';
    }],
    ['array content confirmation status', (occurrence: Record<string, unknown>) => {
      occurrence.content_status = ['verified'];
    }],
    ['array date literal origin', (occurrence: Record<string, unknown>) => {
      occurrence.dates = [{ ...dateFixture(), literal_source: ['primary'] }];
    }],
    ['card facts on an unconfirmed record', (occurrence: Record<string, unknown>) => {
      occurrence.content_status = 'needs_review';
      occurrence.activity_facts = { venue: '报告厅', audience: null, participation: null, time: null };
    }],
    ['non-text card facts', (occurrence: Record<string, unknown>) => {
      occurrence.content_status = 'verified';
      occurrence.activity_facts = { venue: ['报告厅'], audience: null, participation: null, time: null };
    }],
    ['unrecognized card facts', (occurrence: Record<string, unknown>) => {
      occurrence.content_status = 'verified';
      occurrence.activity_facts = { venue: null, audience: null, participation: null, time: null, invented: 'field' };
    }],
    ['unexpected activity fields', (occurrence: Record<string, unknown>) => {
      const review = occurrence.date_review as Record<string, unknown>;
      const source = review.source_review as Record<string, unknown>;
      (source.activity as Record<string, unknown>).invented = 'extra';
    }],
    ['known fields without literal evidence', (occurrence: Record<string, unknown>) => {
      const review = occurrence.date_review as Record<string, unknown>;
      const source = review.source_review as Record<string, unknown>;
      (source.activity as Record<string, unknown>).venue = { value: '图书馆', evidence: [] };
    }],
    ['unbound college names', (occurrence: Record<string, unknown>) => {
      const review = occurrence.date_review as Record<string, unknown>;
      const source = review.source_review as Record<string, unknown>;
      (source.activity as Record<string, unknown>).colleges = [{ name: '数学学院', relation: 'associated', evidence: citation }];
    }],
    ['duplicate selected image indices', (occurrence: Record<string, unknown>) => {
      const review = occurrence.date_review as Record<string, unknown>;
      const source = review.source_review as Record<string, unknown>;
      (source.activity as Record<string, unknown>).image_indices = [3, 3];
    }],
    ['malformed current date claims', (occurrence: Record<string, unknown>) => {
      (occurrence.date_review as Record<string, unknown>).claims = [{ kind: 'event', raw_text: '11月2日', normalized_date: '2026-11-02', resolution: 'explicit' }];
    }],
    ['unsupported date literal origin', (occurrence: Record<string, unknown>) => {
      const claims = (occurrence.date_review as Record<string, unknown>).claims as Record<string, unknown>[];
      claims[0].literal_source = 'model';
    }],
    ['non-public image attribution URLs', (occurrence: Record<string, unknown>) => {
      (occurrence.primary_images as Record<string, unknown>[])[0].source_url = 'javascript:alert(1)';
    }],
    ['non-public archive image attribution URLs', (occurrence: Record<string, unknown>) => {
      occurrence.images = [{ id: 'archive-image', path: `versions/${DATA_VERSION}/images/archive.jpg`, mime: 'image/jpeg',
        width: 640, height: 480, page: 1, caption: null, source_url: 'javascript:alert(1)' }];
    }],
    ['activity attached to an unverified source status', (occurrence: Record<string, unknown>) => {
      const review = occurrence.date_review as Record<string, unknown>;
      (review.source_review as Record<string, unknown>).status = 'source_conflict';
    }],
  ])('rejects %s in present public fields', async (_label, mutate) => {
    const malformed = structuredClone(occurrenceFixture()) as unknown as Record<string, unknown>;
    mutate(malformed);
    await expect(loadOccurrence(malformed as unknown as PublicOccurrence)).rejects.toThrow();
  });
});

describe('public human review boundaries', () => {
  it('admits an explicit human schedule while preserving original AI and archive references', async () => {
    const original = manualOccurrence();
    const loaded = await loadOccurrence(original);
    expect(currentActivityDate(loaded, '2026-10-25')).toEqual(original.dates[0]);
    expect(loaded.manual_review?.activity).toEqual(original.manual_review?.activity);
    expect(loaded.date_review).toEqual(original.date_review);
    expect(loaded.body_text).toEqual(original.body_text);
    expect(loaded.title).toEqual(original.title);
    expect(loaded.preview).toEqual(original.preview);
    expect(loaded.original_dates).toEqual(original.original_dates);
  });

  it('loads published standalone activities without issue or document relations', async () => {
    const loaded = await loadStandalone(false);
    expect(loaded.dataset.issues).toEqual([]);
    expect((await loaded.dataset.loadCatalog()).occurrences[0]).toMatchObject({ kind: 'standalone', issue_id: null,
      document_id: null, source_id: `source-${'a'.repeat(24)}`, content_status: 'human_verified' });
    expect(loaded.occurrence.manual_review?.activity?.content).toBe('Standalone reviewed content');
    expect(currentActivityDate(loaded.occurrence, '2026-10-25')?.normalized_date).toBe('2026-11-02');
  });

  it('loads compact tracked facts without restoring private evidence or long content', async () => {
    const loaded = await loadStandalone(true);
    expect(loaded.dataset.manifest.public_facts_only).toBe(true);
    expect((await loaded.dataset.loadCatalog()).occurrences[0].kind).toBe('standalone');
    expect(loaded.occurrence.manual_review?.activity).toBeUndefined();
    expect(loaded.occurrence.body_text).toBe('');
    expect(loaded.occurrence.date_review).toBeNull();
    expect(currentActivityDate(loaded.occurrence, '2026-10-25')?.normalized_date).toBe('2026-11-02');
  });

  it('loads Python-length emoji boundaries in both public stamps and projected facts', async () => {
    const row = manualOccurrence();
    const reason = `${'r'.repeat(1999)}😀`;
    const venue = `${'v'.repeat(499)}😀`;
    row.manual_review!.reason = reason;
    row.manual_review!.activity.venue = venue;
    row.activity_facts!.venue = venue;
    row.dates[0].evidence = reason;

    const loaded = await loadOccurrence(row);
    expect(loaded.manual_review!.reason).toBe(reason);
    expect(loaded.activity_facts!.venue).toBe(venue);
    expect(currentActivityDate(loaded, '2026-10-25')?.normalized_date).toBe('2026-11-02');

    row.manual_review!.reason += '😀';
    row.dates[0].evidence = row.manual_review!.reason;
    await expect(loadOccurrence(row)).rejects.toThrow();
  });

  it('keeps pending manual text readable without admitting dates or facts', async () => {
    const loaded = await loadOccurrence(manualOccurrence('needs_review'));
    expect(fullManualActivity(loaded).dates).toHaveLength(1);
    expect(loaded.dates).toEqual([]);
    expect(loaded.activity_facts).toBeUndefined();
    expect(currentActivityDate(loaded, '2026-10-25')).toBeNull();
  });

  it.each([
    ['missing stamp', (row: PublicOccurrence) => { delete row.manual_review; }],
    ['malformed revision', (row: PublicOccurrence) => { row.manual_review!.revision = 'unbound'; }],
    ['array-valued revision', (row: PublicOccurrence) => { Object.assign(row.manual_review!, { revision: ['c'.repeat(64)] }); }],
    ['missing timestamp timezone', (row: PublicOccurrence) => { row.manual_review!.reviewed_at = '2026-10-25T10:00:00'; }],
    ['impossible timestamp date', (row: PublicOccurrence) => { row.manual_review!.reviewed_at = '2026-02-30T10:00:00Z'; }],
    ['non-ISO timestamp hour', (row: PublicOccurrence) => { row.manual_review!.reviewed_at = '2026-10-25T24:00:00Z'; }],
    ['empty public reason', (row: PublicOccurrence) => { row.manual_review!.reason = ' '; }],
    ['overlong public reason', (row: PublicOccurrence) => { row.manual_review!.reason = 'x'.repeat(2001); }],
    ['pending stamp with verified status', (row: PublicOccurrence) => { row.manual_review!.status = 'needs_review'; }],
    ['AI status on manual edit', (row: PublicOccurrence) => { row.content_status = 'verified'; }],
    ['private audit stamp field', (row: PublicOccurrence) => { Object.assign(row.manual_review!, { actor: 'private-operator' }); }],
    ['unknown human activity field', (row: PublicOccurrence) => { Object.assign(fullManualActivity(row), { invented: 'extra' }); }],
    ['overlong human content', (row: PublicOccurrence) => { fullManualActivity(row).content = 'x'.repeat(60001); }],
    ['invalid manual activity date', (row: PublicOccurrence) => { fullManualActivity(row).dates[0].date = '2026-02-29'; }],
    ['duplicated manual date', (row: PublicOccurrence) => { fullManualActivity(row).dates.push(fullManualActivity(row).dates[0]); }],
    ['contextual manual schedule', (row: PublicOccurrence) => { row.dates[0].resolution = 'contextual'; }],
    ['AI provenance on manual schedule', (row: PublicOccurrence) => { row.dates[0].literal_source = 'primary'; }],
    ['manual provenance on original AI proof', (row: PublicOccurrence) => { row.date_review!.claims![0].literal_source = 'manual'; }],
    ['mismatched human schedule', (row: PublicOccurrence) => { row.dates[0].normalized_date = '2026-11-03'; }],
    ['mismatched human card fact', (row: PublicOccurrence) => { row.activity_facts!.venue = 'Not the reviewed value'; }],
    ['missing human card facts', (row: PublicOccurrence) => { delete row.activity_facts; }],
    ['overlong human summary', (row: PublicOccurrence) => { fullManualActivity(row).summary = 'x'.repeat(1501); }],
    ['empty human title', (row: PublicOccurrence) => { fullManualActivity(row).title = ' '; }],
    ['empty human body', (row: PublicOccurrence) => { fullManualActivity(row).content = ''; }],
    ['non-null unknown field', (row: PublicOccurrence) => { fullManualActivity(row).organizer = ''; }],
    ['overlong human category', (row: PublicOccurrence) => { fullManualActivity(row).category = 'x'.repeat(201); }],
    ['too many human sources', (row: PublicOccurrence) => { fullManualActivity(row).source_urls = Array.from({ length: 101 }, (_, index) => `https://example.edu/${index}`); }],
    ['too many human dates', (row: PublicOccurrence) => { fullManualActivity(row).dates = Array.from({ length: 101 }, (_, index) => ({ kind: 'event', date: `${2027 + index}-01-01` })); }],
    ['duplicate human sources', (row: PublicOccurrence) => { fullManualActivity(row).source_urls = [SOURCE_URL, SOURCE_URL]; }],
    ['unexpected schedule field', (row: PublicOccurrence) => { Object.assign(row.dates[0], { citation: 'invented' }); }],
    ['unbound manual schedule explanation', (row: PublicOccurrence) => { row.dates[0].evidence = 'not the public reason'; }],
  ])('rejects %s', async (_name, mutate) => {
    const row = manualOccurrence();
    mutate(row);
    await expect(loadOccurrence(row)).rejects.toThrow();
  });

  it.each(['javascript:alert(1)', 'https://user:password@example.edu/path', 'http://127.0.0.1/',
    'http://10.0.0.1/', 'http://[::1]/', 'http://[::ffff:192.168.1.1]/', 'http://localhost/',
    'https://service.internal/', 'https://service.local/', 'https://intranet/', 'https://example.edu/\npath'])
  ('rejects unsafe human source links: %s', async (url) => {
    const row = manualOccurrence();
    row.manual_review!.activity.source_urls = [url];
    await expect(loadOccurrence(row)).rejects.toThrow();
  });

  it('accepts the full manual content bound without changing the smaller AI bound', async () => {
    const row = manualOccurrence();
    row.manual_review!.activity.content = 'x'.repeat(60000);
    expect(fullManualActivity(await loadOccurrence(row)).content).toHaveLength(60000);
    const ai = occurrenceFixture();
    ai.date_review!.source_review.activity!.content = knownField('x'.repeat(8001));
    await expect(loadOccurrence(ai)).rejects.toThrow();
  });

  it('rejects schedule or facts promotion on pending edits', async () => {
    const row = manualOccurrence('needs_review');
    row.dates = manualOccurrence().dates;
    await expect(loadOccurrence(row)).rejects.toThrow();
    row.dates = [];
    row.activity_facts = manualOccurrence().activity_facts;
    await expect(loadOccurrence(row)).rejects.toThrow();
  });

  it('rejects a catalog stamp containing full manual body or private audit fields', async () => {
    for (const extra of [{ activity: manualOccurrence().manual_review!.activity }, { actor: 'private-operator' }]) {
      const payloads = datasetPayload(manualOccurrence());
      const catalog = payloads.get(`/data/versions/${DATA_VERSION}/catalog.json`) as Catalog;
      Object.assign(catalog.occurrences[0].manual_review!, extra);
      vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
        const payload = payloads.get(new URL(String(input)).pathname);
        return new Response(JSON.stringify(payload ?? null), { status: payload === undefined ? 404 : 200 });
      });
      const dataset = await loadDataset();
      await expect(dataset.loadCatalog()).rejects.toThrow();
    }
  });
});

describe('lazy static datasets', () => {
  function partitioned() {
    const payloads = datasetPayload(occurrenceFixture());
    const manifest = payloads.get('/data/manifest.json') as Extract<Manifest, { schema_version: 2 }>;
    const catalog = payloads.get(`/data/${manifest.files.catalog}`) as Catalog;
    const prefix = `versions/${DATA_VERSION}`;
    const alias = `item-${'4'.repeat(24)}`;
    catalog.items[0].aliases = [alias];
    manifest.files.item_index = `${prefix}/item-index.json`;
    manifest.files.issue_catalogs = { [ISSUE_ID]: `${prefix}/issue-catalogs/${ISSUE_ID}.json` };
    payloads.set(`/data/${manifest.files.item_index}`, catalog.items);
    payloads.set(`/data/${manifest.files.issue_catalogs[ISSUE_ID]}`, structuredClone(catalog));
    const requests: string[] = [];
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      requests.push(path);
      const payload = payloads.get(path);
      return new Response(JSON.stringify(payload ?? null), { status: payload === undefined ? 404 : 200 });
    });
    return { payloads, manifest, alias, requests };
  }

  it('bootstraps metadata only, loads a single issue, and defers the global catalog', async () => {
    const { manifest, requests } = partitioned();
    const dataset = await loadDataset();
    expect(requests).toEqual(['/data/manifest.json', `/data/${manifest.files.issues}`]);
    expect((await dataset.loadIssue(ISSUE_ID)).occurrences).toHaveLength(1);
    expect(requests).toContain(`/data/${manifest.files.issue_catalogs![ISSUE_ID]}`);
    expect(requests).not.toContain(`/data/${manifest.files.catalog}`);
    expect(requests).not.toContain(`/data/${manifest.files.item_index}`);
    await dataset.loadCatalog();
    expect(requests).toContain(`/data/${manifest.files.catalog}`);
  });

  it('resolves direct aliases using only the index and detail shard', async () => {
    const { alias, manifest, requests } = partitioned();
    const dataset = await loadDataset();
    expect(dataset.canonicalItemId(alias)).toBe(alias);
    expect((await dataset.loadItem(alias)).id).toBe(ITEM_ID);
    expect(dataset.canonicalItemId(alias)).toBe(ITEM_ID);
    expect(requests).toContain(`/data/${manifest.files.item_index}`);
    expect(requests).not.toContain(`/data/${manifest.files.catalog}`);
    expect(requests).not.toContain(`/data/${manifest.files.issue_catalogs![ISSUE_ID]}`);
  });

  it('accepts a global latest occurrence outside the issue subset', async () => {
    const { payloads, manifest } = partitioned();
    const subset = payloads.get(`/data/${manifest.files.issue_catalogs![ISSUE_ID]}`) as Catalog;
    subset.items[0].latest_occurrence_id = `occ-${'5'.repeat(24)}`;
    subset.items[0].occurrence_count = 2;
    const dataset = await loadDataset();
    expect((await dataset.loadIssue(ISSUE_ID)).items[0].latest_occurrence_id).toBe(`occ-${'5'.repeat(24)}`);
  });

  it('rejects incomplete pairs and unversioned paths at bootstrap', async () => {
    const { manifest } = partitioned();
    delete manifest.files.item_index;
    await expect(loadDataset()).rejects.toThrow();
    manifest.files.item_index = 'item-index.json';
    await expect(loadDataset()).rejects.toThrow();
  });

  it('rejects incorrect issue membership and duplicate aliases lazily', async () => {
    const { payloads, manifest } = partitioned();
    const subset = payloads.get(`/data/${manifest.files.issue_catalogs![ISSUE_ID]}`) as Catalog;
    subset.occurrences[0].issue_id = `issue-${'6'.repeat(24)}`;
    const dataset = await loadDataset();
    await expect(dataset.loadIssue(ISSUE_ID)).rejects.toThrow();
    const index = payloads.get(`/data/${manifest.files.item_index}`) as Catalog['items'];
    index[0].aliases.push(index[0].aliases[0]);
    await expect(dataset.loadItem(ITEM_ID)).rejects.toThrow();
  });

  it('falls back to the legacy catalog lazily and retries after an aborted load', async () => {
    const { manifest, requests } = partitioned();
    delete manifest.files.issue_catalogs;
    delete manifest.files.item_index;
    const dataset = await loadDataset();
    expect(requests).not.toContain(`/data/${manifest.files.catalog}`);
    const controller = new AbortController();
    controller.abort();
    await expect(dataset.loadIssue(ISSUE_ID, controller.signal)).rejects.toThrow();
    expect((await dataset.loadIssue(ISSUE_ID)).occurrences).toHaveLength(1);
    expect(requests.filter((path) => path === `/data/${manifest.files.catalog}`)).toHaveLength(2);
  });
});

describe('normalized public datasets', () => {
  function fixture(detailRow = occurrenceFixture(), noExtras = false) {
    const payloads = datasetPayload(detailRow);
    const sourceManifest = payloads.get('/data/manifest.json') as Extract<Manifest, { schema_version: 2 }>;
    const catalog = JSON.parse(JSON.stringify(payloads.get(`/data/${sourceManifest.files.catalog}`))) as Catalog;
    const issues = structuredClone(payloads.get(`/data/${sourceManifest.files.issues}`)) as PublicIssue[];
    const oldIssue = `issue-${'7'.repeat(24)}`;
    const oldDoc = 'c'.repeat(64);
    const oldOccurrence = `occ-${'8'.repeat(24)}`;
    const alias = `item-${'4'.repeat(24)}`;
    issues.push({ ...issues[0], id: oldIssue, documents: [{ ...issues[0].documents[0], id: oldDoc }] });
    catalog.items[0].aliases = [alias];
    catalog.items[0].occurrence_count = 2;
    const oldRow = { ...catalog.occurrences[0], id: oldOccurrence, issue_id: oldIssue, document_id: oldDoc, title: '旧标题' };
    catalog.occurrences.unshift(oldRow);
    const detail = { id: ITEM_ID, latest_occurrence_id: OCCURRENCE_ID, occurrences: [detailRow,
      { ...detailRow, id: oldOccurrence, issue_id: oldIssue, document_id: oldDoc, title: '旧标题', body_text: noExtras ? '' : '旧正文' }] };
    sourceManifest.counts.occurrences = 2;
    sourceManifest.counts.issues = 2;
    sourceManifest.counts.documents = 2;
    const encoded = encodePartitionedFacts(catalog, issues, { [ITEM_ID]: detail }, sourceManifest);
    const prefix = `versions/${DATA_VERSION}/reader-v3`;
    const manifest: Extract<Manifest, { schema_version: 3 }> = {
      schema_version: 3, generator: 'nannanews-reader-v3', data_version: DATA_VERSION,
      counts: sourceManifest.counts,
      files: { issues: `${prefix}/issues.json`, item_index: `${prefix}/item-index.json`, search_index: `${prefix}/search-index.json`,
        partitions: Object.fromEntries(Object.keys(encoded.partitions).map((key) => [key, `${prefix}/facts/${key}.json`])) },
    };
    const data = new Map<string, unknown>([['/data/manifest.json', manifest], [`/data/${manifest.files.issues}`, issues],
      [`/data/${manifest.files.item_index}`, encoded.itemIndex]]);
    for (const [key, part] of Object.entries(encoded.partitions)) data.set(`/data/${manifest.files.partitions[key]}`, part);
    for (const [bucket, shard] of Object.entries(encoded.detailShards)) data.set(`/data/${prefix}/details/${bucket}.json`, shard);
    const requests: string[] = [];
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      requests.push(path);
      const value = data.get(path);
      return new Response(JSON.stringify(value ?? null), { status: value === undefined ? 404 : 200 });
    });
    return { data, manifest, encoded, requests, oldIssue, oldOccurrence, alias };
  }

  it('loads metadata and only needed owner partitions, preserving global order', async () => {
    const { requests, manifest, oldIssue, oldOccurrence } = fixture();
    const dataset = await loadDataset();
    expect(requests).toEqual(['/data/manifest.json', `/data/${manifest.files.issues}`]);
    expect((await dataset.loadIssue(ISSUE_ID)).occurrences.map((row) => row.id)).toEqual([OCCURRENCE_ID]);
    expect(requests).not.toContain(`/data/${manifest.files.partitions[oldIssue]}`);
    expect(requests).not.toContain(`/data/${manifest.files.item_index}`);
    expect((await dataset.loadIssue(oldIssue)).occurrences.map((row) => row.id)).toEqual([oldOccurrence]);
    expect((await dataset.loadCatalog()).occurrences.map((row) => row.id)).toEqual([oldOccurrence, OCCURRENCE_ID]);
    expect(requests.some((path) => /catalog\.json|search-documents\.json/.test(path))).toBe(false);
  });

  it('loads a historical partition and its owner without leaking foreign rows', async () => {
    const { requests, manifest, oldIssue, oldOccurrence } = fixture();
    const dataset = await loadDataset();
    const issue = await dataset.loadIssue(oldIssue);
    expect(issue.occurrences.map((row) => row.id)).toEqual([oldOccurrence]);
    expect(requests).toContain(`/data/${manifest.files.partitions[oldIssue]}`);
    expect(requests).toContain(`/data/${manifest.files.partitions[ISSUE_ID]}`);
    expect(requests).not.toContain(`/data/${manifest.files.item_index}`);
  });

  it('rejects duplicate global ordinals', async () => {
    const { encoded, oldIssue } = fixture();
    encoded.partitions[oldIssue].occurrences[0].order = encoded.partitions[ISSUE_ID].occurrences[0].order;
    const dataset = await loadDataset();
    await expect(dataset.loadCatalog()).rejects.toThrow();
  });

  it('fetches every partition at most once during a global load beyond cache capacity', async () => {
    const { data, manifest, encoded, requests, oldIssue } = fixture();
    const issues = data.get(`/data/${manifest.files.issues}`) as PublicIssue[];
    const paths: Record<string, string> = { [ISSUE_ID]: manifest.files.partitions[ISSUE_ID] };
    for (let i = 0; i < 12; i++) {
      const id = `issue-${(100 + i).toString(16).padStart(24, '0')}`;
      const path = `versions/${DATA_VERSION}/reader-v3/facts/${id}.json`;
      issues.push({ ...issues[0], id, occurrence_count: 0, documents: [] });
      paths[id] = path;
      data.set(`/data/${path}`, { messages: [], owners: {}, occurrences: [] });
    }
    paths[oldIssue] = manifest.files.partitions[oldIssue];
    manifest.files.partitions = paths;
    manifest.counts.issues = issues.length;
    const dataset = await loadDataset();
    expect((await dataset.loadCatalog()).occurrences).toHaveLength(2);
    for (const path of Object.values(paths)) expect(requests.filter((request) => request === `/data/${path}`)).toHaveLength(1);
    expect(encoded.partitions[oldIssue].owners[ITEM_ID]).toBe(ISSUE_ID);
  });

  it('does not fetch unrelated owners when loading a direct item', async () => {
    const { data, manifest, encoded, requests, alias } = fixture();
    const foreignIssue = `issue-${'9'.repeat(24)}`;
    const foreignItem = `item-${'a'.repeat(24)}`;
    const foreignOld = `occ-${'b'.repeat(24)}`;
    const foreignLatest = `occ-${'c'.repeat(24)}`;
    const issues = data.get(`/data/${manifest.files.issues}`) as PublicIssue[];
    issues[0].occurrence_count++;
    issues.push({ ...issues[0], id: foreignIssue, occurrence_count: 1,
      documents: [{ ...issues[0].documents[0], id: 'd'.repeat(64) }] });
    manifest.counts.items++;
    manifest.counts.occurrences += 2;
    manifest.counts.issues++;
    manifest.counts.documents++;
    const foreignPath = `versions/${DATA_VERSION}/reader-v3/facts/${foreignIssue}.json`;
    manifest.files.partitions[foreignIssue] = foreignPath;
    const current = encoded.partitions[ISSUE_ID];
    current.owners[foreignItem] = foreignIssue;
    current.occurrences.push({ ...current.occurrences[0], id: foreignOld, item_id: foreignItem, order: 2 });
    data.set(`/data/${foreignPath}`, {
      messages: [{ ...current.messages[0], id: foreignItem, aliases: [], latest_occurrence_id: foreignLatest, occurrence_count: 2 }],
      owners: { [foreignItem]: foreignIssue },
      occurrences: [{ ...current.occurrences[0], id: foreignLatest, item_id: foreignItem, document_id: 'd'.repeat(64), order: 3 }],
    });
    encoded.itemIndex.push({ id: foreignItem, aliases: [], latest_occurrence_id: foreignLatest, occurrence_count: 2,
      owner: foreignIssue, occurrences: [{ id: foreignOld, part: ISSUE_ID }, { id: foreignLatest, part: foreignIssue }] });
    const dataset = await loadDataset();
    expect((await dataset.loadItem(alias)).id).toBe(ITEM_ID);
    expect(requests).not.toContain(`/data/${foreignPath}`);
  });

  it('resolves aliases and reconstructs each selected occurrence with its own details', async () => {
    const { alias, oldOccurrence, manifest, requests } = fixture();
    const dataset = await loadDataset();
    const detail = await dataset.loadItem(alias);
    expect(dataset.canonicalItemId(alias)).toBe(ITEM_ID);
    expect(detail.occurrences.map((row) => row.id)).toEqual([OCCURRENCE_ID, oldOccurrence]);
    expect(detail.occurrences[1].body_text).toBe('旧正文');
    expect(requests).toContain(`/data/${manifest.files.item_index}`);
    expect(requests.some((path) => /catalog\.json|search-documents\.json/.test(path))).toBe(false);
  });

  it('rejects malformed owners and retries aborted requests', async () => {
    const { encoded, oldIssue } = fixture();
    const dataset = await loadDataset();
    const controller = new AbortController();
    controller.abort();
    await expect(dataset.loadIssue(oldIssue, controller.signal)).rejects.toThrow();
    encoded.partitions[oldIssue].owners[ITEM_ID] = 'missing';
    await expect(dataset.loadIssue(oldIssue)).rejects.toThrow();
    encoded.partitions[oldIssue].owners[ITEM_ID] = ISSUE_ID;
    await expect(dataset.loadIssue(oldIssue)).resolves.toMatchObject({ occurrences: [{ id: expect.any(String) }] });
  });

  it('refetches a repaired owner partition after a missing entity', async () => {
    const { encoded, oldIssue, manifest, requests } = fixture();
    const owner = encoded.partitions[ISSUE_ID];
    const message = owner.messages.pop()!;
    const dataset = await loadDataset();
    await expect(dataset.loadIssue(oldIssue)).rejects.toThrow();
    owner.messages.push(message);
    await expect(dataset.loadIssue(oldIssue)).resolves.toMatchObject({ occurrences: [{ id: expect.any(String) }] });
    expect(requests.filter((path) => path === `/data/${manifest.files.partitions[ISSUE_ID]}`)).toHaveLength(2);
    expect(requests.filter((path) => path === `/data/${manifest.files.partitions[oldIssue]}`)).toHaveLength(2);
  });

  it('coalesces a partition fetch without cancelling another caller', async () => {
    const { data, manifest } = fixture();
    const dataset = await loadDataset();
    const gate = Promise.withResolvers<Response>();
    const fetcher = vi.fn(() => gate.promise);
    vi.stubGlobal('fetch', fetcher);
    const controller = new AbortController();
    const cancelled = dataset.loadIssue(ISSUE_ID, controller.signal);
    const survivor = dataset.loadIssue(ISSUE_ID);
    controller.abort();
    await expect(cancelled).rejects.toThrow();
    gate.resolve(new Response(JSON.stringify(data.get(`/data/${manifest.files.partitions[ISSUE_ID]}`))));
    await expect(survivor).resolves.toMatchObject({ occurrences: [{ id: OCCURRENCE_ID }] });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('does not request a details shard for fact-only rows', async () => {
    const date = { ...dateFixture(), raw_text: '2026-11-02', evidence: '' };
    const row = occurrenceFixture({ dates: [date], body_text: '', date_review: null, original_dates: [], primary_images: [] });
    const { encoded, requests } = fixture(row, true);
    expect(Object.keys(encoded.detailShards)).toHaveLength(0);
    const dataset = await loadDataset();
    expect((await dataset.loadItem(ITEM_ID)).occurrences).toHaveLength(2);
    expect(requests.some((path) => path.includes('/details/'))).toBe(false);
  });

  it('preserves reviewed manual activity in normalized details', async () => {
    fixture(manualOccurrence());
    const dataset = await loadDataset();
    const detail = await dataset.loadItem(ITEM_ID);
    expect(detail.occurrences[0].manual_review?.activity?.content).toBe('人工核实的完整活动正文');
    expect(detail.occurrences[0].content_status).toBe('human_verified');
  });
});
