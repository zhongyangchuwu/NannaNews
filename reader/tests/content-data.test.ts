import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import type { StaticDataset } from '../src/data';
import type { ActivityReview, Catalog, Manifest, PublicDate, PublicIssue, PublicOccurrence, SourceReview } from '../src/types';

const DATA_VERSION = 'a'.repeat(64);
const ITEM_ID = `item-${'1'.repeat(24)}`;
const OCCURRENCE_ID = `occ-${'2'.repeat(24)}`;
const ISSUE_ID = `issue-${'3'.repeat(24)}`;
const DOCUMENT_ID = 'b'.repeat(64);
const SOURCE_URL = 'https://example.edu/events/open-lecture';
const SOURCE_QUOTE = '本次公开讲座11月2日；计算机学院参与活动。';
const CURRENT_RULES = 'luna-content-evidence-v4';
const LEGACY_RULES = 'luna-content-evidence-v3';

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
      source_urls: [SOURCE_URL], source_hosts: ['example.edu'], colleges: ['计算机学院'], dates: [],
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
