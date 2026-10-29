import type { Catalog, Manifest, PublicDate, PublicIssue, PublicItemDetail, PublicOccurrence } from './types';
import { manualPublicUrl } from './filter';
import { manualReviewStampData, validDay, withinTextLimit } from './review-validation.mjs';

const VERSION = /^[a-f0-9]{64}$/;
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const OCC_ID = /^occ-[a-f0-9]{24}$/;
const ISSUE_ID = /^issue-[a-f0-9]{24}$/;
const SOURCE_ID = /^source-[a-f0-9]{24}$/;
const dataRoot = new URL('data/', new URL(import.meta.env.BASE_URL, document.baseURI));

type JsonObject = Record<string, unknown>;
function object(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function requireData(condition: unknown, message = '静态数据格式不兼容，请重新生成数据。'): asserts condition {
  if (!condition) throw new Error(message);
}
function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === 'string');
}
function nullableString(value: unknown): value is string | null { return value === null || typeof value === 'string'; }

const ACTIVITY_FIELD_LENGTHS = {
  title: 250, summary: 1500, content: 8000, venue: 500,
  organizer: 500, audience: 1500, participation: 3000, time: 1000,
} as const;
const ACTIVITY_KEYS = [...Object.keys(ACTIVITY_FIELD_LENGTHS), 'colleges', 'image_indices'];
const CATALOG_FACT_KEYS = ['venue', 'audience', 'participation', 'time'] as const;

function catalogFactsData(value: unknown, status: unknown): boolean {
  if (value === undefined) return true;
  if (!['verified', 'human_verified'].includes(String(status)) || !object(value) || !exactKeys(value, CATALOG_FACT_KEYS)) return false;
  return CATALOG_FACT_KEYS.every((key) => {
    const field = value[key];
    return field === null || (typeof field === 'string' && field.trim().length > 0
      && withinTextLimit(field, ACTIVITY_FIELD_LENGTHS[key]));
  });
}

function exactKeys(value: JsonObject, expected: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === expected.length && expected.every((key) => Object.hasOwn(value, key));
}

function manualActivityData(value: unknown): boolean {
  if (!object(value) || !exactKeys(value, [...Object.keys(ACTIVITY_FIELD_LENGTHS), 'category', 'source_urls', 'dates'])) return false;
  for (const [key, limit] of Object.entries({ ...ACTIVITY_FIELD_LENGTHS, content: 60000, category: 200 })) {
    const field = value[key];
    if (['title', 'summary', 'content'].includes(key)) {
      if (typeof field !== 'string' || !withinTextLimit(field, limit) || key !== 'summary' && !field.trim()) return false;
    } else if (field !== null && (typeof field !== 'string' || !field.trim() || !withinTextLimit(field, limit))) return false;
  }
  if (!stringArray(value.source_urls) || value.source_urls.length > 100
    || !value.source_urls.every((url) => manualPublicUrl(url))
    || new Set(value.source_urls).size !== value.source_urls.length
    || !Array.isArray(value.dates) || value.dates.length > 100) return false;
  const seen = new Set<string>();
  return value.dates.every((date) => {
    if (!object(date) || !exactKeys(date, ['kind', 'date'])
      || !['event', 'deadline', 'registration', 'application'].includes(String(date.kind))
      || typeof date.kind !== 'string' || typeof date.date !== 'string' || !validDay(date.date)) return false;
    const key = `${date.kind}/${date.date}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function manualOccurrenceData(value: JsonObject, detail: boolean, factsOnly = false): boolean {
  const review = value.manual_review;
  if (review === undefined) return value.content_status !== 'human_verified';
  const compactStamp = detail && factsOnly && object(review) && !Object.hasOwn(review, 'activity');
  if (!manualReviewStampData(review, detail && !compactStamp) || !object(review)
    || value.content_status !== (review.status === 'verified' ? 'human_verified' : 'needs_review')) return false;
  if (detail && !compactStamp && !manualActivityData(review.activity)) return false;
  if (review.status === 'needs_review') return Array.isArray(value.dates) && value.dates.length === 0
    && value.activity_facts === undefined && value.expiry_date === null;
  if (!object(value.activity_facts)) return false;
  if (!Array.isArray(value.dates) || value.dates.length > 100 || !value.dates.every((date) => object(date)
    && exactKeys(date, ['kind', 'normalized_date', 'resolution', 'literal_source', ...(detail ? ['raw_text', 'evidence'] : [])])
    && typeof date.kind === 'string' && ['event', 'deadline', 'registration', 'application'].includes(date.kind)
    && date.literal_source === 'manual' && date.resolution === 'explicit'
    && typeof date.normalized_date === 'string' && validDay(date.normalized_date)
    && (!detail || compactStamp || date.raw_text === date.normalized_date
      && (date.evidence === review.reason || value.kind === 'standalone' && date.evidence === '')))) return false;
  if (new Set(value.dates.map((date) => `${date.kind}/${date.normalized_date}`)).size !== value.dates.length) return false;
  if (detail && !compactStamp && object(review.activity)) {
    const activity = review.activity;
    const facts = value.activity_facts;
    if (!CATALOG_FACT_KEYS.every((key) => facts[key] === activity[key])) return false;
    const dates = activity.dates as JsonObject[];
    if (value.dates.length !== dates.length || !value.dates.every((date) => object(date)
      && dates.some((entry) => entry.kind === date.kind && entry.date === date.normalized_date))) return false;
  }
  return true;
}

function publicHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:';
  } catch {
    return false;
  }
}

function activityCitationData(value: unknown, sources: unknown[]): boolean {
  return object(value) && exactKeys(value, ['url', 'text', 'image_index'])
    && typeof value.url === 'string' && publicHttpUrl(value.url) && typeof value.text === 'string'
    && value.text.length > 0 && value.text.trim().length > 0 && withinTextLimit(value.text, 4000)
    && (value.image_index === null || (Number.isSafeInteger(value.image_index) && Number(value.image_index) >= 0))
    && sources.some((source) => object(source) && source.url === value.url && Array.isArray(source.quotes)
      && source.quotes.some((quote) => typeof quote === 'string' && quote.includes(value.text as string)));
}

function activityFieldData(value: unknown, maxLength: number, sources: unknown[]): boolean {
  if (!object(value) || !exactKeys(value, ['value', 'evidence']) || !nullableString(value.value)
    || !Array.isArray(value.evidence) || value.evidence.length > 8
    || !value.evidence.every((citation) => activityCitationData(citation, sources))) return false;
  return value.value === null
    ? value.evidence.length === 0
    : value.value.trim().length > 0 && withinTextLimit(value.value, maxLength) && value.evidence.length > 0;
}

function activityData(value: unknown, sources: unknown[]): boolean {
  if (!object(value) || !exactKeys(value, ACTIVITY_KEYS)) return false;
  for (const [name, maxLength] of Object.entries(ACTIVITY_FIELD_LENGTHS)) {
    if (!activityFieldData(value[name], maxLength, sources)) return false;
  }
  if (!object(value.title) || value.title.value === null
    || !object(value.summary) || value.summary.value === null
    || !object(value.content) || value.content.value === null) return false;
  if (!Array.isArray(value.colleges) || value.colleges.length > 30) return false;
  const colleges = new Set<string>();
  for (const college of value.colleges) {
    if (!object(college) || !exactKeys(college, ['name', 'relation', 'evidence'])
      || typeof college.name !== 'string' || college.name.trim().length === 0
      || !['associated', 'audience'].includes(String(college.relation))
      || !activityCitationData(college.evidence, sources)) return false;
    const citation = college.evidence as JsonObject;
    if (typeof citation.text !== 'string' || !citation.text.includes(college.name)) return false;
    const key = JSON.stringify([college.name, college.relation, citation.url, citation.text, citation.image_index]);
    if (colleges.has(key)) return false;
    colleges.add(key);
  }
  return Array.isArray(value.image_indices) && value.image_indices.length <= 64
    && value.image_indices.every((index) => Number.isSafeInteger(index) && Number(index) >= 0)
    && new Set(value.image_indices).size === value.image_indices.length;
}

function publicDateData(value: unknown): value is PublicDate {
  if (!object(value)) return false;
  const expected = Object.hasOwn(value, 'literal_source')
    ? ['kind', 'raw_text', 'normalized_date', 'resolution', 'evidence', 'literal_source']
    : ['kind', 'raw_text', 'normalized_date', 'resolution', 'evidence'];
  return exactKeys(value, expected)
    && typeof value.kind === 'string' && value.kind.trim().length > 0 && typeof value.raw_text === 'string'
    && nullableString(value.normalized_date)
    && ['explicit', 'contextual', 'year_unknown', 'invalid', 'ambiguous'].includes(String(value.resolution))
    && typeof value.evidence === 'string'
    && (!Object.hasOwn(value, 'literal_source') || ['primary', 'bulletin'].includes(String(value.literal_source)));
}

function primaryImageData(value: unknown): value is JsonObject {
  return object(value) && exactKeys(value, ['id', 'path', 'mime', 'width', 'height', 'page', 'caption', ...(Object.hasOwn(value, 'source_url') ? ['source_url'] : [])])
    && typeof value.id === 'string' && typeof value.path === 'string'
    && ['image/avif', 'image/bmp', 'image/gif', 'image/jpeg', 'image/png', 'image/webp'].includes(String(value.mime))
    && (value.width === null || (Number.isSafeInteger(value.width) && Number(value.width) > 0))
    && (value.height === null || (Number.isSafeInteger(value.height) && Number(value.height) > 0))
    && (value.page === null || (Number.isSafeInteger(value.page) && Number(value.page) > 0))
    && nullableString(value.caption)
    && (value.source_url === undefined || (typeof value.source_url === 'string' && publicHttpUrl(value.source_url)));
}

function sourceReviewData(value: unknown): boolean {
  if (!object(value) || !['source_verified', 'source_conflict', 'source_unavailable', 'source_mismatch', 'secondary_only'].includes(String(value.status))
    || typeof value.reason !== 'string' || !Array.isArray(value.sources)
    || !value.sources.every((source) => object(source) && typeof source.url === 'string'
      && typeof source.resolved_url === 'string' && typeof source.title === 'string'
      && typeof source.sha256 === 'string' && VERSION.test(source.sha256)
      && typeof source.fetched_at === 'string' && nullableString(source.published_date) && stringArray(source.quotes))
    || (['source_verified', 'source_conflict'].includes(String(value.status)) && value.sources.length === 0)) return false;
  return value.activity === undefined || value.activity === null
    || (value.status === 'source_verified' && activityData(value.activity, value.sources));
}


function sourceCorrectionsData(value: unknown): boolean {
  return value === undefined || (Array.isArray(value) && value.every((correction: unknown) => object(correction)
    && typeof correction.original_url === 'string' && typeof correction.corrected_url === 'string'
    && typeof correction.reason === 'string' && typeof correction.title === 'string'
    && typeof correction.sha256 === 'string' && VERSION.test(correction.sha256)
    && typeof correction.fetched_at === 'string'));
}

export function staticUrl(path: string, version?: string): string {
  requireData(typeof path === 'string' && /^[a-zA-Z0-9_./-]+$/.test(path)
    && !path.startsWith('/') && !path.split('/').some((part) => !part || part === '.' || part === '..'), '静态资源路径无效。');
  if (version) requireData(path.startsWith(`versions/${version}/`), '静态资源版本不一致。');
  const url = new URL(path, dataRoot);
  requireData(url.origin === dataRoot.origin && url.pathname.startsWith(dataRoot.pathname), '静态资源路径越界。');
  return url.href;
}

async function fetchJson(url: string, signal?: AbortSignal, manifest = false): Promise<unknown> {
  const response = await fetch(url, { signal, credentials: 'omit', cache: manifest ? 'no-cache' : 'default' });
  if (!response.ok) throw new Error(`静态数据加载失败（${response.status}），请检查发布是否完整后重试。`);
  try { return await response.json(); }
  catch { throw new Error('静态数据不是有效的 JSON，请检查发布文件后重试。'); }
}

function manifestData(value: unknown): Manifest {
  const compactFacts = object(value) && value.public_facts_only === true;
  requireData(object(value) && value.schema_version === 2 && value.generator === 'webnews-static-v2'
    && typeof value.data_version === 'string' && VERSION.test(value.data_version)
    && (compactFacts ? value.generated_at === undefined : typeof value.generated_at === 'string')
    && object(value.files) && object(value.counts));
  requireData(value.public_facts_only === undefined || typeof value.public_facts_only === 'boolean');
  requireData(value.content_rules_version === undefined || typeof value.content_rules_version === 'string');
  for (const name of ['catalog', 'issues', 'search_documents', 'search_index']) {
    requireData(typeof value.files[name] === 'string');
    staticUrl(value.files[name], value.data_version);
  }
  for (const name of ['items', 'occurrences', 'issues', 'documents', 'images', 'pdfs']) {
    requireData(Number.isSafeInteger(value.counts[name]) && Number(value.counts[name]) >= 0);
  }
  return value as unknown as Manifest;
}

function occurrenceData(value: unknown, detail = false, factsOnly = false): boolean {
  if (!object(value) || typeof value.id !== 'string' || !OCC_ID.test(value.id)
    || typeof value.item_id !== 'string' || !ITEM_ID.test(value.item_id)) return false;
  const standalone = value.kind === 'standalone';
  const validReferences = standalone
    ? value.issue_id === null && value.document_id === null && value.issue_date === null
      && value.activity_id === value.item_id && typeof value.source_id === 'string' && SOURCE_ID.test(value.source_id)
    : typeof value.issue_id === 'string' && ISSUE_ID.test(value.issue_id)
      && typeof value.document_id === 'string' && VERSION.test(value.document_id);
  return validReferences && (value.activity_id === undefined || value.activity_id === value.item_id)
    && (value.source_id === undefined || (typeof value.source_id === 'string' && SOURCE_ID.test(value.source_id)))
    && ['summary', 'detail', 'notice', 'standalone'].includes(String(value.kind))
    && typeof value.title === 'string' && nullableString(value.category)
    && typeof value.preview === 'string' && nullableString(value.issue_date)
    && nullableString(value.expiry_date) && Number.isSafeInteger(value.image_count)
    && stringArray(value.source_urls) && stringArray(value.source_hosts) && stringArray(value.colleges)
    && sourceCorrectionsData(value.source_corrections)
    && (value.content_status === undefined || (typeof value.content_status === 'string'
      && ['verified', 'human_verified', 'conflict', 'unconfirmed', 'unreviewed', 'legacy', 'source_corrected', 'needs_review'].includes(value.content_status)))
    && manualOccurrenceData(value, detail, factsOnly)
    && catalogFactsData(value.activity_facts, value.content_status)
    && Array.isArray(value.dates) && value.dates.every((date) => object(date)
      && typeof date.kind === 'string' && nullableString(date.normalized_date)
      && ['explicit', 'contextual', 'year_unknown', 'invalid', 'ambiguous'].includes(String(date.resolution))
      && (date.literal_source === undefined || (typeof date.literal_source === 'string'
        && (['primary', 'bulletin'].includes(date.literal_source)
          || date.literal_source === 'manual' && value.content_status === 'human_verified' && object(value.manual_review)
            && value.manual_review.status === 'verified'))));
}

function catalogData(value: unknown, manifest: Manifest): Catalog {
  requireData(object(value) && Array.isArray(value.items) && Array.isArray(value.occurrences)
    && value.items.length === manifest.counts.items && value.occurrences.length === manifest.counts.occurrences);
  const itemIds = new Set<string>();
  const occurrenceIds = new Set<string>();
  for (const item of value.items) {
    requireData(object(item) && typeof item.id === 'string' && ITEM_ID.test(item.id)
      && typeof item.latest_occurrence_id === 'string' && OCC_ID.test(item.latest_occurrence_id)
      && Number.isSafeInteger(item.occurrence_count) && Number(item.occurrence_count) > 0
      && typeof item.detail_path === 'string' && stringArray(item.aliases) && !itemIds.has(item.id));
    staticUrl(item.detail_path, manifest.data_version);
    itemIds.add(item.id);
  }
  const aliases = new Set<string>();
  for (const item of value.items) for (const alias of item.aliases) {
    requireData(ITEM_ID.test(alias) && !itemIds.has(alias) && !aliases.has(alias));
    aliases.add(alias);
  }
  for (const occurrence of value.occurrences) {
    requireData(occurrenceData(occurrence, false, manifest.public_facts_only) && itemIds.has(occurrence.item_id) && !occurrenceIds.has(occurrence.id));
    occurrenceIds.add(occurrence.id);
  }
  for (const item of value.items) requireData(occurrenceIds.has(item.latest_occurrence_id));
  return value as unknown as Catalog;
}

function issuesData(value: unknown, manifest: Manifest): PublicIssue[] {
  requireData(Array.isArray(value) && value.length === manifest.counts.issues);
  const ids = new Set<string>();
  let documentCount = 0;
  for (const issue of value) {
    requireData(object(issue) && typeof issue.id === 'string' && ISSUE_ID.test(issue.id) && !ids.has(issue.id)
      && typeof issue.title === 'string' && nullableString(issue.issue_date)
      && (issue.issue_number === null || Number.isSafeInteger(issue.issue_number))
      && Number.isSafeInteger(issue.occurrence_count) && Array.isArray(issue.documents));
    ids.add(issue.id);
    for (const document of issue.documents) {
      requireData(object(document) && typeof document.id === 'string' && VERSION.test(document.id)
        && ['html', 'pdf'].includes(String(document.format)) && typeof document.filename === 'string'
        && nullableString(document.claimed_issue_date) && nullableString(document.issue_date)
        && nullableString(document.pdf_path) && stringArray(document.source_urls) && stringArray(document.warnings));
      if (typeof document.pdf_path === 'string') staticUrl(document.pdf_path, manifest.data_version);
      documentCount++;
    }
  }
  requireData(documentCount === manifest.counts.documents);
  return value as PublicIssue[];
}

function detailData(value: unknown, id: string, manifest: Manifest): PublicItemDetail {
  requireData(object(value) && value.id === id && typeof value.latest_occurrence_id === 'string'
    && Array.isArray(value.occurrences) && value.occurrences.length > 0);
  for (const occurrence of value.occurrences) {
    requireData(occurrenceData(occurrence, true, manifest.public_facts_only) && occurrence.item_id === id && typeof occurrence.body_text === 'string'
      && object(occurrence.locator) && Array.isArray(occurrence.table_cells) && Array.isArray(occurrence.college_mentions)
      && Array.isArray(occurrence.images)
      && (occurrence.original_dates === undefined || (Array.isArray(occurrence.original_dates) && occurrence.original_dates.every(publicDateData)))
      && (occurrence.primary_images === undefined || Array.isArray(occurrence.primary_images)));
    requireData(occurrence.date_review === null || (object(occurrence.date_review)
      && typeof occurrence.date_review.status === 'string' && typeof occurrence.date_review.reason === 'string'
      && (occurrence.date_review.rules_version === undefined || typeof occurrence.date_review.rules_version === 'string')
      && (occurrence.date_review.claims === undefined || (Array.isArray(occurrence.date_review.claims) && occurrence.date_review.claims.every(publicDateData)))
      && sourceReviewData(occurrence.date_review.source_review)));
    for (const image of occurrence.images) {
      requireData(object(image) && typeof image.path === 'string' && typeof image.mime === 'string'
        && (image.source_url === undefined || (typeof image.source_url === 'string' && publicHttpUrl(image.source_url))));
      staticUrl(image.path, manifest.data_version);
    }
    for (const image of occurrence.primary_images ?? []) {
      requireData(primaryImageData(image));
      staticUrl((image as JsonObject).path as string, manifest.data_version);
    }
    for (const date of occurrence.dates) requireData(typeof date.raw_text === 'string' && typeof date.evidence === 'string'
      && (!Object.hasOwn(date, 'literal_source') || ['primary', 'bulletin', 'manual'].includes(String(date.literal_source))));
    requireData(occurrence.table_cells.every((cell: unknown) => object(cell) && typeof cell.header === 'string' && typeof cell.text === 'string'));
    requireData(occurrence.college_mentions.every((college: unknown) => object(college) && typeof college.name === 'string'
      && ['mentioned', 'audience'].includes(String(college.relation)) && typeof college.evidence === 'string'));
  }
  requireData(value.occurrences.some((occurrence: PublicOccurrence) => occurrence.id === value.latest_occurrence_id));
  return value as unknown as PublicItemDetail;
}

export interface StaticDataset {
  manifest: Manifest;
  catalog: Catalog;
  issues: PublicIssue[];
  searchIndexUrl: string;
  assetUrl(path: string): string;
  canonicalItemId(id: string): string;
  loadItem(id: string, signal?: AbortSignal): Promise<PublicItemDetail>;
}

export async function loadDataset(signal?: AbortSignal): Promise<StaticDataset> {
  const manifest = manifestData(await fetchJson(new URL('manifest.json', dataRoot).href, signal, true));
  const [rawCatalog, rawIssues] = await Promise.all([
    fetchJson(staticUrl(manifest.files.catalog, manifest.data_version), signal),
    fetchJson(staticUrl(manifest.files.issues, manifest.data_version), signal),
  ]);
  const catalog = catalogData(rawCatalog, manifest);
  const issues = issuesData(rawIssues, manifest);
  const documentsByIssue = new Map(issues.map((issue) => [issue.id, new Set(issue.documents.map((document) => document.id))]));
  requireData(catalog.occurrences.every((occurrence) => {
    if (occurrence.kind === 'standalone') return occurrence.issue_id === null && occurrence.document_id === null;
    if (typeof occurrence.issue_id !== 'string' || typeof occurrence.document_id !== 'string') return false;
    return documentsByIssue.get(occurrence.issue_id)?.has(occurrence.document_id) === true;
  }));
  const items = new Map(catalog.items.map((item) => [item.id, item]));
  for (const item of catalog.items) for (const alias of item.aliases) items.set(alias, item);
  const shards = new Map<string, JsonObject>();
  return {
    manifest, catalog, issues,
    searchIndexUrl: staticUrl(manifest.files.search_index, manifest.data_version),
    assetUrl: (path) => staticUrl(path, manifest.data_version),
    canonicalItemId: (id) => items.get(id)?.id ?? id,
    async loadItem(id, itemSignal) {
      const item = items.get(id);
      if (!item) throw new Error('未找到这条消息，可能不在当前静态数据版本中。');
      let shard = shards.get(item.detail_path);
      if (!shard) {
        const raw = await fetchJson(staticUrl(item.detail_path, manifest.data_version), itemSignal);
        requireData(object(raw));
        shard = raw;
        if (shards.size >= 8) shards.delete(shards.keys().next().value!);
        shards.set(item.detail_path, shard);
      }
      return detailData(shard[item.id], item.id, manifest);
    },
  };
}
