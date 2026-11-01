import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router';
import type { ActivityReview, CatalogOccurrence, ManualActivity, PublicDocument, PublicOccurrence, ReaderState } from '../src/types';
import { defaultFilters } from '../src/filter';
import { EditionPage } from '../src/components/EditionPage';
import { IssueArchive } from '../src/components/IssueArchive';
import { ItemDetail } from '../src/components/ItemDetail';
import { encodePartitionedFacts, decodePartition, decodeItemDetail } from '../src/public-format.mjs';

const ITEM_ID = `item-${'1'.repeat(24)}`;
const OCC_ID = `occ-${'2'.repeat(24)}`;
const ISSUE_ID = `issue-${'3'.repeat(24)}`;
const SOURCE_URL = 'https://example.edu/source';
const PARTICIPATION_URL = 'https://example.edu/register';
const RULES = 'current-test-rules';

const DOCUMENT_ID = 'a'.repeat(64);

type FullManualOccurrence = Omit<PublicOccurrence, 'manual_review'> & {
  manual_review: NonNullable<PublicOccurrence['manual_review']> & { activity: ManualActivity };
};

function hasFullManualActivity(row: PublicOccurrence): row is FullManualOccurrence {
  return row.manual_review?.activity !== undefined;
}
const field = (value: string) => ({ value, evidence: [{ url: SOURCE_URL, text: value, image_index: null }] });
const unknown = { value: null, evidence: [] };

function aiActivity(): ActivityReview {
  return { title: field('AI activity title'), summary: field('AI activity summary'), content: field('AI full body'),
    time: unknown, venue: unknown, organizer: unknown, audience: unknown, participation: unknown,
    colleges: [], image_indices: [] };
}

function manualActivity(): ManualActivity {
  return { title: 'Human activity title', summary: 'Human activity summary', content: 'Human full body', category: 'Human category',
    venue: 'Human venue', time: 'Human time', organizer: 'Human organizer', audience: 'Human audience',
    participation: `Register at ${PARTICIPATION_URL}`, source_urls: [SOURCE_URL], dates: [{ kind: 'event', date: '2026-11-02' }] };
}

function occurrence(patch: Partial<PublicOccurrence> = {}): PublicOccurrence {
  return { id: OCC_ID, item_id: ITEM_ID, issue_id: ISSUE_ID, document_id: DOCUMENT_ID, kind: 'detail',
    title: 'Archive title', preview: 'Archive summary', body_text: 'Archive full body', category: 'Archive category',
    issue_date: '2026-10-17', source_urls: [SOURCE_URL], source_hosts: ['example.edu'], colleges: [],
    content_status: 'verified', expiry_date: null, image_count: 0,
    dates: [{ kind: 'deadline', normalized_date: '2026-10-01', resolution: 'explicit', literal_source: 'primary', raw_text: '10月1日', evidence: 'Archive date evidence' },
      { kind: 'event', normalized_date: '2026-11-02', resolution: 'contextual', literal_source: 'primary', raw_text: '11月2日', evidence: 'Context evidence' }],
    date_review: { status: 'reviewed', reason: 'Review reason', rules_version: RULES, claims: [],
      source_review: { status: 'source_verified', reason: 'Source reason', sources: [{ url: SOURCE_URL, resolved_url: SOURCE_URL,
        title: 'Source title', sha256: 'b'.repeat(64), fetched_at: '2026-10-17T00:00:00Z', published_date: null,
        quotes: ['AI activity title', 'AI activity summary', 'AI full body'] }], activity: aiActivity() } },
    college_mentions: [], images: [], locator: {}, table_cells: [], ...patch };
}

function humanOccurrence(status: 'verified' | 'needs_review' = 'verified'): FullManualOccurrence {
  const row = occurrence({ content_status: status === 'verified' ? 'human_verified' : 'needs_review',
    manual_review: { revision: 'c'.repeat(64), status, reviewed_at: '2026-10-25T09:00:00+08:00', reason: 'Public correction reason', activity: manualActivity() },
    dates: status === 'verified' ? [{ kind: 'event', normalized_date: '2026-11-02', resolution: 'explicit', literal_source: 'manual', raw_text: '2026-11-02', evidence: 'Public correction reason' }] : [],
    activity_facts: status === 'verified' ? { venue: 'Human venue', audience: 'Human audience', participation: `Register at ${PARTICIPATION_URL}`, time: 'Human time' } : undefined,
  });
  if (!hasFullManualActivity(row)) throw new Error('Human UI fixture must include its full activity payload.');
  return row;
}

function standaloneOccurrence(factsOnly = false): PublicOccurrence {
  const base = humanOccurrence();
  const stamp = base.manual_review!;
  return { ...base, id: `occ-${'7'.repeat(24)}`, item_id: `item-${'8'.repeat(24)}`,
    activity_id: `item-${'8'.repeat(24)}`, source_id: `source-${'9'.repeat(24)}`,
    issue_id: null, document_id: null, kind: 'standalone', issue_date: null, body_text: '',
    title: 'Human activity title', preview: 'Human activity summary', category: 'Human category',
    date_review: null, images: [], primary_images: [], locator: {}, table_cells: [], college_mentions: [],
    manual_review: factsOnly ? { revision: stamp.revision, status: stamp.status, reviewed_at: stamp.reviewed_at, reason: stamp.reason } : stamp,
  };
}

function state(row: PublicOccurrence, documents: PublicDocument[] = [], factsOnly = false): ReaderState {
  const issue = { id: ISSUE_ID, title: 'Issue title', issue_date: '2026-10-17', issue_number: 1, occurrence_count: 1, documents };
  return { status: 'ready', error: null, favorites: new Set(), followedColleges: new Set(), today: '2026-10-25', filters: defaultFilters(),
    selectedOccurrence: row, selectedItem: { id: row.item_id, latest_occurrence_id: row.id, occurrences: [row] },
    issues: [issue], editionIssue: issue, editionRecords: [row], records: [row], detailStatus: 'ready', view: 'edition', mode: 'all',
    selectedIssue: null, detailError: null, preferenceError: null, total: 1, page: 1, pages: 1, perPage: 20, searching: false,
    manifest: { schema_version: 2, generator: 'webnews-static-v2', data_version: 'f'.repeat(64),
      ...(factsOnly ? { public_facts_only: true } : { generated_at: '2026-10-25T00:00:00Z' }), content_rules_version: RULES,
      counts: { items: 1, occurrences: 1, issues: factsOnly ? 0 : 1, documents: documents.length, images: 0, pdfs: documents.filter((document) => document.format === 'pdf' && document.pdf_path).length }, files: { catalog: '', issues: '', search_documents: '', search_index: '' } },
    listHref: '/history', options: { categories: [], colleges: [], sources: [] },
    setFilters() {}, resetFilters() {}, setPage() {}, toggleFavorite() {}, toggleFollowedCollege() {}, selectOccurrence() {}, retry() {},
    itemHref: (id: string, occ?: string) => `/items/${id}?occ=${occ}`, issueHref: (id: string) => `/issues/${id}`, assetUrl: (path: string) => `/data/${path}`,
  };
}

function documentFixture(format: PublicDocument['format'], patch: Partial<PublicDocument> = {}): PublicDocument {
  return { id: DOCUMENT_ID, format, filename: format === 'pdf' ? 'issue.pdf' : 'issue.html',
    claimed_issue_date: '2026-10-17', issue_date: '2026-10-17', pdf_path: format === 'pdf' ? 'issue.pdf' : null,
    source_urls: [SOURCE_URL], warnings: [], ...patch };
}


function detail(row: PublicOccurrence, documents: PublicDocument[] = [], factsOnly = false) {
  return renderToStaticMarkup(<MemoryRouter><ItemDetail state={state(row, documents, factsOnly)} /></MemoryRouter>);
}
function archive(row: PublicOccurrence, documents: PublicDocument[] = [], factsOnly = false) {
  return renderToStaticMarkup(<MemoryRouter><IssueArchive state={state(row, documents, factsOnly)} /></MemoryRouter>);
}
function header(html: string) { return html.match(/<header\b[^>]*>([\s\S]*?)<\/header>/)?.[1] ?? ''; }


describe('human detail projection and unchanged AI guards', () => {
  it('uses manual content and safe operator participation links, separating AI and raw reference', () => {
    const row = humanOccurrence();
    const html = detail(row);
    const current = header(html);
    expect(current).toContain(row.manual_review!.activity.title);
    expect(current).toContain(row.manual_review!.activity.summary);
    expect(current).toContain('Human venue');
    expect(current).toContain('Human organizer');
    expect(current).toContain(`href="${PARTICIPATION_URL}"`);
    expect(current).not.toContain('AI activity title');
    expect(html).toContain('Human full body');
    expect(html).toContain(row.body_text);
    expect(html).toContain('AI full body');
    expect(html).toContain('Source reason');
    expect(html).toContain('Review reason');
  });

  it('shows a registered standalone activity without an issue or missing-document warning', () => {
    const row = standaloneOccurrence();
    const html = detail(row);
    expect(header(html)).toContain('独立收录');
    expect(html).toContain('Human full body');
    expect(html).toContain(SOURCE_URL);
    expect(html).not.toContain('无法确认这条消息对应的期次原件');
    expect(html).not.toContain('该期原始 PDF 当前不可用');
    expect(header(html)).not.toContain('刊于 日期未明');
  });

  it('renders tracked standalone facts without raw content or archive alarms', () => {
    const row = standaloneOccurrence(true);
    const html = detail(row, [], true);
    expect(header(html)).toContain('人工核实');
    expect(html).toContain('独立收录的活动基本信息');
    expect(html).toContain(SOURCE_URL);
    expect(html).toContain('本精简版未纳入来源核验材料，不对未导出的证据作判断。');
    expect(html).not.toContain('Human full body');
    expect(html).not.toContain('无法确认这条消息对应的期次原件');
    expect(html).not.toContain('该期原始 PDF 当前不可用');
  });


  it('never renders pending participation as operator-confirmed', () => {
    const row = humanOccurrence('needs_review');
    expect(header(detail(row))).not.toContain(`href="${PARTICIPATION_URL}"`);
  });

  it.each(['javascript:alert(1)', 'http://127.0.0.1/register', 'https://user:pass@example.edu/register', 'http://service.internal/register'])
  ('does not activate an unsafe operator participation URL: %s', (url) => {
    const row = humanOccurrence();
    row.manual_review!.activity.participation = url;
    expect(header(detail(row))).not.toContain(`href="${url}"`);
  });

  it.each([
    ['old rules', (row: PublicOccurrence) => { row.date_review!.rules_version = 'old'; }],
    ['source conflict', (row: PublicOccurrence) => { row.date_review!.source_review.status = 'source_conflict'; }],
    ['corrected source', (row: PublicOccurrence) => { row.source_corrections = [{ original_url: SOURCE_URL, corrected_url: 'https://example.edu/corrected', reason: 'Correction', title: 'New source', sha256: 'd'.repeat(64), fetched_at: '2026-10-25T00:00:00Z' }]; }],
    ['pending status', (row: PublicOccurrence) => { row.content_status = 'needs_review'; }],
  ])('keeps AI content out of the primary detail for %s', (_label, mutate) => {
    const row = occurrence();
    mutate(row);
    const current = header(detail(row));
    expect(current).toContain(row.title);
    expect(current).not.toContain('AI activity title');
    expect(current).not.toContain('AI activity summary');
  });

  it('continues using current verified source-bound AI content without a manual review', () => {
    const current = header(detail(occurrence()));
    expect(current).toContain('AI activity title');
    expect(current).toContain('AI activity summary');
    expect(current).not.toContain('Archive title');
  });
});

describe('original document formats', () => {
  it('presents HTML as an original without treating absent PDF as missing', () => {
    const row = occurrence();
    const htmlOriginal = documentFixture('html');
    const unrelatedPdf = documentFixture('pdf', { id: 'd'.repeat(64), filename: 'unrelated.pdf', pdf_path: 'unrelated.pdf' });
    const archiveMarkup = archive(row, [htmlOriginal]);
    const detailMarkup = detail(row, [htmlOriginal, unrelatedPdf]);
    const editionMarkup = renderToStaticMarkup(<MemoryRouter><EditionPage state={state(row, [htmlOriginal])} /></MemoryRouter>);

    expect(archiveMarkup).toContain('网页原件（HTML）');
    expect(archiveMarkup).not.toContain('未提供原始 PDF');
    expect(archiveMarkup).not.toContain('暂不可用');
    expect(detailMarkup).toContain('网页原件（HTML）');
    expect(detailMarkup).toContain('issue.html');
    expect(detailMarkup).not.toContain('暂不可用');
    expect(detailMarkup).not.toContain('/data/unrelated.pdf');
    expect(editionMarkup).toContain('网页原件（HTML）');
    expect(editionMarkup).toContain('issue.html');
    expect(editionMarkup).not.toContain('暂不可用');
  });

  it('keeps an unavailable warning for a matched PDF original', () => {
    const row = occurrence();
    const missingPdf = documentFixture('pdf', { pdf_path: null });
    const editionMarkup = renderToStaticMarkup(<MemoryRouter><EditionPage state={state(row, [missingPdf])} /></MemoryRouter>);

    expect(archive(row, [missingPdf])).toContain('原始 PDF 文件暂不可用');
    expect(detail(row, [missingPdf])).toContain('该期原始 PDF 当前不可用');
    expect(editionMarkup).toContain('原始 PDF 文件暂不可用');
  });

  it('explains excluded compact facts attachments without calling them missing', () => {
    const row = occurrence();
    const omittedPdf = documentFixture('pdf', { pdf_path: null });
    const compact = state(row, [omittedPdf], true);
    const editionMarkup = renderToStaticMarkup(<MemoryRouter><EditionPage state={compact} /></MemoryRouter>);
    const archiveMarkup = archive(row, [omittedPdf], true);

    expect(editionMarkup).toContain('精简公开事实版仅含基本信息；本构建不含原刊附件。');
    expect(archiveMarkup).toContain('精简公开事实版仅含基本信息；本构建不含原刊附件。');
    expect(editionMarkup).not.toContain('原始 PDF 文件暂不可用');
    expect(archiveMarkup).not.toContain('原始 PDF 文件暂不可用');
    expect(archiveMarkup).not.toContain('未提供原始文件');
  });

  it('distinguishes no original from an unmatched document without borrowing a same-date PDF', () => {
    const row = occurrence();
    const unrelatedPdf = documentFixture('pdf', { id: 'd'.repeat(64), filename: 'unrelated.pdf', pdf_path: 'unrelated.pdf' });

    expect(archive(row)).toContain('未提供原始文件');
    expect(detail(row)).toContain('未提供原始文件');
    const unmatchedMarkup = detail(row, [unrelatedPdf]);
    expect(unmatchedMarkup).toContain('未找到与这条消息匹配的原始文件');
    expect(unmatchedMarkup).not.toContain('/data/unrelated.pdf');
    expect(unmatchedMarkup).not.toContain('打开历史原始 PDF');
  });
});

describe('legacy dates through normalized detail reconstruction', () => {
  it.each([true, false])('preserves the historical-date fallback when original_dates is absent: %s', (absent) => {
    const row = occurrence({ content_status: 'legacy', date_review: null,
      dates: [{ kind: 'deadline', normalized_date: '2026-10-01', resolution: 'explicit',
        raw_text: '10月1日', evidence: 'Legacy calendar evidence' }] });
    if (absent) delete row.original_dates;
    else row.original_dates = [];
    const reader = state(row, [documentFixture('html')]);
    const fact: CatalogOccurrence = {
      id: row.id, item_id: row.item_id, issue_id: row.issue_id, document_id: row.document_id,
      kind: row.kind, title: row.title, preview: row.preview, category: row.category,
      issue_date: row.issue_date, source_urls: row.source_urls, source_hosts: row.source_hosts,
      colleges: row.colleges, dates: row.dates.map(({ raw_text, evidence, ...date }) => date),
      expiry_date: row.expiry_date, image_count: row.image_count, content_status: row.content_status,
    };
    const catalog = { items: [{ id: ITEM_ID, aliases: [], latest_occurrence_id: OCC_ID,
      occurrence_count: 1, detail_path: `versions/${reader.manifest!.data_version}/items/11.json` }],
      occurrences: [fact] };
    const wire = encodePartitionedFacts(catalog, reader.issues,
      { [ITEM_ID]: reader.selectedItem! }, reader.manifest!);
    const part = wire.partitions[ISSUE_ID];
    const records = decodePartition(part, ISSUE_ID, new Map(part.messages.map((message) => [message.id, message])));
    const rebuilt = decodeItemDetail(wire.itemIndex[0], records, wire.detailShards['11']?.[ITEM_ID]).occurrences[0];
    const html = detail(rebuilt);
    if (absent) {
      expect(rebuilt.original_dates).toBeUndefined();
      expect(html).toContain('Legacy calendar evidence');
    } else {
      expect(rebuilt.original_dates).toEqual([]);
      expect(html).not.toContain('Legacy calendar evidence');
    }
  });
});
