import type { Catalog, CatalogItem, Manifest, PublicDate, PublicIssue, PublicItemDetail, PublicOccurrence } from './types';
import { manualPublicUrl } from './filter';
import { manualReviewStampData, validDay, withinTextLimit } from './review-validation.mjs';
import { decodePartition, decodeItemDetail } from './public-format.mjs';
import type { WireMessage, WirePartition, WireItem, WireExtras } from './public-format.mjs';

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
  let value: unknown;
  try { value = await response.json(); }
  catch (error) { if (signal?.aborted) throw error; throw new Error('静态数据不是有效的 JSON，请检查发布文件后重试。'); }
  signal?.throwIfAborted();
  return value;
}

function manifestData(value: unknown): Manifest {
  const compactFacts = object(value) && value.public_facts_only === true;
  requireData(object(value) && ((value.schema_version === 2 && value.generator === 'webnews-static-v2')
    || (value.schema_version === 3 && value.generator === 'nannanews-reader-v3'))
    && typeof value.data_version === 'string' && VERSION.test(value.data_version)
    && (value.schema_version === 3 ? value.generated_at === undefined || typeof value.generated_at === 'string'
      : compactFacts ? value.generated_at === undefined : typeof value.generated_at === 'string')
    && object(value.files) && object(value.counts));
  requireData(value.public_facts_only === undefined || typeof value.public_facts_only === 'boolean');
  requireData(value.content_rules_version === undefined || typeof value.content_rules_version === 'string');
  const paths: string[] = [];
  const names = value.schema_version === 3 ? ['issues', 'item_index', 'search_index'] : ['catalog', 'issues', 'search_documents', 'search_index'];
  for (const name of names) {
    requireData(typeof value.files[name] === 'string');
    staticUrl(value.files[name], value.data_version);
    paths.push(value.files[name]);
  }
  if (value.schema_version === 3) {
    requireData(object(value.files.partitions));
    for (const [id, path] of Object.entries(value.files.partitions)) {
      requireData((id === 'standalone' || ISSUE_ID.test(id)) && typeof path === 'string');
      staticUrl(path, value.data_version);
      paths.push(path);
    }
    requireData(new Set(paths).size === paths.length);
  } else {
    const hasIssueCatalogs = value.files.issue_catalogs !== undefined;
    requireData(hasIssueCatalogs === (value.files.item_index !== undefined));
    if (hasIssueCatalogs) {
      requireData(object(value.files.issue_catalogs) && typeof value.files.item_index === 'string');
      staticUrl(value.files.item_index, value.data_version);
      for (const [id, path] of Object.entries(value.files.issue_catalogs)) {
        requireData(ISSUE_ID.test(id) && typeof path === 'string');
        staticUrl(path, value.data_version);
      }
    }
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

function itemData(value: unknown, manifest: Manifest, complete: boolean): CatalogItem[] {
  requireData(Array.isArray(value) && (!complete || value.length === manifest.counts.items));
  const ids = new Set<string>();
  for (const item of value) {
    requireData(object(item) && typeof item.id === 'string' && ITEM_ID.test(item.id)
      && typeof item.latest_occurrence_id === 'string' && OCC_ID.test(item.latest_occurrence_id)
      && Number.isSafeInteger(item.occurrence_count) && Number(item.occurrence_count) > 0
      && (manifest.schema_version === 3 ? item.detail_path === undefined || typeof item.detail_path === 'string' : typeof item.detail_path === 'string')
      && stringArray(item.aliases) && !ids.has(item.id));
    if (typeof item.detail_path === 'string') staticUrl(item.detail_path, manifest.data_version);
    ids.add(item.id);
  }
  const aliases = new Set<string>();
  for (const item of value) for (const alias of item.aliases) {
    requireData(ITEM_ID.test(alias) && !ids.has(alias) && !aliases.has(alias));
    aliases.add(alias);
  }
  return value as CatalogItem[];
}

function catalogData(value: unknown, manifest: Manifest, issues: PublicIssue[], issueId?: string): Catalog {
  requireData(object(value) && Array.isArray(value.occurrences));
  const items = itemData(value.items, manifest, !issueId);
  if (!issueId) requireData(value.occurrences.length === manifest.counts.occurrences);
  const byItem = new Map(items.map((item) => [item.id, item]));
  const seen = new Set<string>();
  const counts = new Map<string, number>();
  const latest = new Set<string>();
  const documents = new Map(issues.map((issue) => [issue.id, new Set(issue.documents.map((document) => document.id))]));
  for (const occurrence of value.occurrences) {
    requireData(occurrenceData(occurrence, false, manifest.public_facts_only) && byItem.has(occurrence.item_id)
      && !seen.has(occurrence.id) && (!issueId || occurrence.issue_id === issueId));
    requireData(occurrence.kind === 'standalone' || documents.get(occurrence.issue_id)?.has(occurrence.document_id) === true);
    seen.add(occurrence.id);
    counts.set(occurrence.item_id, (counts.get(occurrence.item_id) || 0) + 1);
    if (byItem.get(occurrence.item_id)?.latest_occurrence_id === occurrence.id) latest.add(occurrence.item_id);
  }
  for (const item of items) {
    const count = counts.get(item.id) || 0;
    requireData(count > 0 && count <= item.occurrence_count && (issueId || count === item.occurrence_count && latest.has(item.id)));
    if (issueId && seen.has(item.latest_occurrence_id)) requireData(latest.has(item.id));
  }
  if (issueId) requireData(value.occurrences.length === issues.find((issue) => issue.id === issueId)?.occurrence_count);
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
  issues: PublicIssue[];
  searchIndexUrl: string;
  assetUrl(path: string): string;
  canonicalItemId(id: string): string;
  loadCatalog(signal?: AbortSignal): Promise<Catalog>;
  loadIssue(issueId: string, signal?: AbortSignal): Promise<Catalog>;
  loadItem(id: string, signal?: AbortSignal): Promise<PublicItemDetail>;
}

export async function loadDataset(signal?: AbortSignal): Promise<StaticDataset> {
  const manifest = manifestData(await fetchJson(new URL('manifest.json', dataRoot).href, signal, true));
  const issues = issuesData(await fetchJson(staticUrl(manifest.files.issues, manifest.data_version), signal), manifest);
  if (manifest.schema_version === 3) return loadPartitionedDataset(manifest, issues);
  const catalogPath = manifest.files.catalog;
  if (manifest.files.issue_catalogs) {
    const paths = manifest.files.issue_catalogs;
    requireData(Object.keys(paths).length === issues.length && issues.every((issue) => Object.hasOwn(paths, issue.id)));
  }
  let catalog: Catalog | undefined;
  let index: CatalogItem[] | undefined;
  const issueCache = new Map<string, Catalog>();
  const shards = new Map<string, JsonObject>();
  const known = new Map<string, CatalogItem>();
  function remember(items: CatalogItem[]) {
    for (const item of items) {
      for (const id of [item.id, ...item.aliases]) {
        const previous = known.get(id);
        requireData(!previous || (previous.id === item.id && previous.latest_occurrence_id === item.latest_occurrence_id
          && previous.occurrence_count === item.occurrence_count && previous.detail_path === item.detail_path
          && previous.aliases.length === item.aliases.length && previous.aliases.every((alias, i) => alias === item.aliases[i])));
      }
    }
    for (const item of items) for (const id of [item.id, ...item.aliases]) known.set(id, item);
  }
  async function loadCatalog(catalogSignal?: AbortSignal): Promise<Catalog> {
    if (catalog) return catalog;
    const value = catalogData(await fetchJson(staticUrl(catalogPath, manifest.data_version), catalogSignal), manifest, issues);
    remember(value.items);
    catalog = value;
    return value;
  }
  function validatedDetail(shard: JsonObject, item: CatalogItem): PublicItemDetail {
    const detail = detailData(shard[item.id], item.id, manifest);
    requireData(detail.latest_occurrence_id === item.latest_occurrence_id && detail.occurrences.length === item.occurrence_count);
    const seen = new Set<string>();
    for (const occurrence of detail.occurrences) {
      requireData(!seen.has(occurrence.id));
      seen.add(occurrence.id);
      requireData(occurrence.kind === 'standalone' || issues.some((issue) => issue.id === occurrence.issue_id
        && issue.documents.some((document) => document.id === occurrence.document_id)));
    }
    return detail;
  }
  return {
    manifest, issues, loadCatalog,
    searchIndexUrl: staticUrl(manifest.files.search_index, manifest.data_version),
    assetUrl: (path) => staticUrl(path, manifest.data_version),
    canonicalItemId: (id) => known.get(id)?.id ?? id,
    async loadIssue(issueId, issueSignal) {
      requireData(issues.some((issue) => issue.id === issueId), '未找到这期刊物，可能不在当前数据版本中。');
      const cached = issueCache.get(issueId);
      if (cached) return cached;
      let value: Catalog;
      if (manifest.files.issue_catalogs) {
        value = catalogData(await fetchJson(staticUrl(manifest.files.issue_catalogs[issueId], manifest.data_version), issueSignal), manifest, issues, issueId);
      } else {
        const global = await loadCatalog(issueSignal);
        const occurrences = global.occurrences.filter((occurrence) => occurrence.issue_id === issueId);
        const ids = new Set(occurrences.map((occurrence) => occurrence.item_id));
        value = { items: global.items.filter((item) => ids.has(item.id)), occurrences };
      }
      remember(value.items);
      if (issueCache.size >= 8) issueCache.delete(issueCache.keys().next().value!);
      issueCache.set(issueId, value);
      known.clear();
      if (catalog) remember(catalog.items);
      if (index) remember(index);
      for (const cachedIssue of issueCache.values()) remember(cachedIssue.items);
      return value;
    },
    async loadItem(id, itemSignal) {
      if (!index && manifest.files.item_index) {
        const value = itemData(await fetchJson(staticUrl(manifest.files.item_index, manifest.data_version), itemSignal), manifest, true);
        remember(value);
        index = value;
      } else if (!manifest.files.item_index) await loadCatalog(itemSignal);
      const item = known.get(id);
      if (!item) throw new Error('未找到这条消息，可能不在当前静态数据版本中。');
      requireData(typeof item.detail_path === 'string');
      let shard = shards.get(item.detail_path);
      if (!shard) {
        const raw = await fetchJson(staticUrl(item.detail_path, manifest.data_version), itemSignal);
        requireData(object(raw));
        shard = raw;
        const detail = validatedDetail(shard, item);
        if (shards.size >= 8) shards.delete(shards.keys().next().value!);
        shards.set(item.detail_path, shard);
        return detail;
      }
      return validatedDetail(shard, item);
    },
  };
}

function loadPartitionedDataset(manifest: Extract<Manifest, { schema_version: 3 }>, issues: PublicIssue[]): StaticDataset {
  const paths = manifest.files.partitions;
  requireData(Object.keys(paths).length === issues.length + (Object.hasOwn(paths, 'standalone') ? 1 : 0)
    && issues.every((issue) => Object.hasOwn(paths, issue.id)));
  const known = new Map<string, CatalogItem>();
  let directory: WireItem[] | undefined;
  let catalog: Catalog | undefined;
  const recentItems = new Map<string, CatalogItem[]>();
  const cache = new Map<string, unknown>();
  const pending = new Map<string, { promise: Promise<unknown>; controller: AbortController; users: number }>();
  let active = 0;
  const queue: Array<() => void> = [];
  async function limitedFetch(path: string, signal: AbortSignal): Promise<unknown> {
    const gate = Promise.withResolvers<void>();
    const start = () => {
      signal.removeEventListener('abort', cancel);
      if (signal.aborted) { gate.reject(signal.reason); return; }
      active++;
      gate.resolve();
    };
    const cancel = () => {
      const index = queue.indexOf(start);
      if (index >= 0) queue.splice(index, 1);
      gate.reject(signal.reason);
    };
    if (signal.aborted) cancel();
    else if (active < 6) start();
    else { queue.push(start); signal.addEventListener('abort', cancel, { once: true }); }
    await gate.promise;
    try { return await fetchJson(staticUrl(path, manifest.data_version), signal); }
    finally { active--; queue.shift()?.(); }
  }
  async function get(path: string, signal?: AbortSignal): Promise<unknown> {
    signal?.throwIfAborted();
    if (cache.has(path)) return cache.get(path);
    let request = pending.get(path);
    if (!request) {
      const controller = new AbortController();
      const entry = { controller, users: 0, promise: Promise.resolve(undefined as unknown) };
      entry.promise = limitedFetch(path, controller.signal).then((value) => {
        if (!controller.signal.aborted) {
          if (cache.size >= 8) cache.delete(cache.keys().next().value!);
          cache.set(path, value);
        }
        return value;
      }).finally(() => { if (pending.get(path) === entry) pending.delete(path); });
      pending.set(path, entry);
      request = entry;
    }
    const shared = request;
    shared.users++;
    const result = Promise.withResolvers<unknown>();
    let finished = false;
    const finish = () => {
      if (finished) return false;
      finished = true;
      signal?.removeEventListener('abort', abort);
      if (--shared.users === 0 && pending.get(path) === shared) {
        pending.delete(path);
        shared.controller.abort();
      }
      return true;
    };
    const abort = () => { if (finish()) result.reject(signal?.reason); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    shared.promise.then((value) => { if (finish()) result.resolve(value); }, (error) => { if (finish()) result.reject(error); });
    return result.promise;
  }
  function remember(items: CatalogItem[]) {
    for (const item of items) for (const id of [item.id, ...item.aliases]) {
      const previous = known.get(id);
      requireData(!previous || (previous.id === item.id && previous.latest_occurrence_id === item.latest_occurrence_id
        && previous.occurrence_count === item.occurrence_count && previous.aliases.length === item.aliases.length
        && previous.aliases.every((alias, index) => alias === item.aliases[index])));
    }
    for (const item of items) for (const id of [item.id, ...item.aliases]) known.set(id, item);
  }
  async function partition(key: string, signal?: AbortSignal): Promise<WirePartition> {
    requireData(Object.hasOwn(paths, key));
    const raw = await get(paths[key], signal);
    try {
      requireData(object(raw) && Array.isArray(raw.messages) && object(raw.owners) && Array.isArray(raw.occurrences));
      requireData(key === 'standalone' ? raw.occurrences.length > 0
        : raw.occurrences.length === issues.find((issue) => issue.id === key)?.occurrence_count);
      const rowOwners = new Map<unknown, unknown>();
      for (const row of raw.occurrences) if (object(row)) rowOwners.set(row.id, row.item_id);
      const messages = new Set<string>();
      for (const message of raw.messages) {
        requireData(object(message) && typeof message.id === 'string' && ITEM_ID.test(message.id)
          && !messages.has(message.id) && stringArray(message.aliases)
          && typeof message.latest_occurrence_id === 'string' && OCC_ID.test(message.latest_occurrence_id)
          && Number.isSafeInteger(message.occurrence_count) && Number(message.occurrence_count) > 0 && object(message.fields));
        messages.add(message.id);
        requireData(rowOwners.get(message.latest_occurrence_id) === message.id);
      }
      const referenced = new Set<string>();
      for (const row of raw.occurrences) {
        requireData(object(row) && typeof row.item_id === 'string' && ITEM_ID.test(row.item_id)
          && Number.isSafeInteger(row.order) && Number(row.order) >= 0 && Number(row.order) < manifest.counts.occurrences);
        referenced.add(row.item_id);
      }
      requireData(Object.keys(raw.owners).length === referenced.size);
      for (const id of referenced) requireData(typeof raw.owners[id] === 'string' && Object.hasOwn(paths, raw.owners[id] as string));
      for (const id of messages) requireData(raw.owners[id] === key);
      return raw as unknown as WirePartition;
    } catch (error) { cache.delete(paths[key]); throw error; }
  }
  function hydrate(part: WirePartition, key: string, owners: Map<string, WireMessage>): Catalog['occurrences'] {
    const rows = decodePartition(part, key, owners);
    requireData(rows.length === part.occurrences.length);
    for (const row of rows) requireData(occurrenceData(row, false, manifest.public_facts_only)
      && (key === 'standalone' ? row.issue_id === null : row.issue_id === key)
      && (row.kind === 'standalone' || issues.some((issue) => issue.id === row.issue_id
        && issue.documents.some((document) => document.id === row.document_id))));
    return rows;
  }
  async function decode(key: string, signal?: AbortSignal): Promise<{ rows: Catalog['occurrences']; items: CatalogItem[]; orders: number[]; owners: Record<string, string> }> {
    const part = await partition(key, signal);
    const ownerKeys = [...new Set(Object.values(part.owners))];
    const ownerParts = await Promise.all(ownerKeys.map((owner) => owner === key ? Promise.resolve(part) : partition(owner, signal)));
    const owners = new Map<string, WireMessage>();
    for (let i = 0; i < ownerParts.length; i++) for (const message of ownerParts[i].messages) {
      if (part.owners[message.id] === ownerKeys[i]) {
        requireData(!owners.has(message.id));
        owners.set(message.id, message);
      }
    }
    for (const id of Object.keys(part.owners)) requireData(owners.has(id));
    let rows: Catalog['occurrences'];
    try {
      rows = hydrate(part, key, owners);
    } catch (error) {
      cache.delete(paths[key]);
      for (const owner of ownerKeys) cache.delete(paths[owner]);
      throw error;
    }
    const items = [...owners.values()].map(({ id, aliases, latest_occurrence_id, occurrence_count }) => ({ id, aliases, latest_occurrence_id, occurrence_count }));
    return { rows, items, orders: part.occurrences.map((row) => row.order), owners: part.owners };
  }
  async function loadIndex(signal?: AbortSignal): Promise<WireItem[]> {
    if (directory) return directory;
    const raw = await get(manifest.files.item_index, signal);
    try {
      const items = itemData(raw, manifest, true);
      const allOccurrences = new Set<string>();
      let count = 0;
      for (const item of items) {
        const entry = item as WireItem;
        requireData(typeof entry.owner === 'string' && Object.hasOwn(paths, entry.owner)
          && Array.isArray(entry.occurrences) && entry.occurrences.length === item.occurrence_count);
        if (entry.detail_path) requireData(!Object.values(paths).includes(entry.detail_path)
          && ![manifest.files.issues, manifest.files.item_index, manifest.files.search_index].includes(entry.detail_path));
        let latest = false;
        for (const ref of entry.occurrences) {
          requireData(object(ref) && typeof ref.id === 'string' && OCC_ID.test(ref.id) && !allOccurrences.has(ref.id)
            && typeof ref.part === 'string' && Object.hasOwn(paths, ref.part));
          allOccurrences.add(ref.id);
          if (ref.id === item.latest_occurrence_id) { requireData(ref.part === entry.owner); latest = true; }
          count++;
        }
        requireData(latest);
      }
      requireData(count === manifest.counts.occurrences);
      signal?.throwIfAborted();
      remember(items);
      directory = raw as WireItem[];
      return directory;
    } catch (error) { cache.delete(manifest.files.item_index); throw error; }
  }
  return {
    manifest, issues,
    searchIndexUrl: staticUrl(manifest.files.search_index, manifest.data_version),
    assetUrl: (path) => staticUrl(path, manifest.data_version),
    canonicalItemId: (id) => known.get(id)?.id ?? id,
    async loadIssue(id, signal) {
      requireData(issues.some((issue) => issue.id === id), '未找到这期刊物，可能不在当前数据版本中。');
      const decoded = await decode(id, signal);
      const value = catalogData({ items: decoded.items, occurrences: decoded.rows }, manifest, issues, id);
      requireData(new Set(decoded.orders).size === decoded.orders.length);
      signal?.throwIfAborted();
      remember(value.items);
      recentItems.delete(id);
      recentItems.set(id, value.items);
      if (recentItems.size > 8) {
        recentItems.delete(recentItems.keys().next().value!);
        known.clear();
        if (catalog) remember(catalog.items);
        if (directory) remember(directory);
        for (const items of recentItems.values()) remember(items);
      }
      return value;
    },
    async loadCatalog(signal) {
      signal?.throwIfAborted();
      if (catalog) return catalog;
      const keys = Object.keys(paths);
      const parts = new Map(await Promise.all(keys.map(async (key) => [key, await partition(key, signal)] as const)));
      try {
        const entities = new Map<string, WireMessage>();
        const owners = new Map<string, string>();
        for (const [key, part] of parts) for (const message of part.messages) {
          requireData(!entities.has(message.id));
          entities.set(message.id, message);
          owners.set(message.id, key);
        }
        const items: CatalogItem[] = [...entities.values()].map(({ id, aliases, latest_occurrence_id, occurrence_count }) =>
          ({ id, aliases, latest_occurrence_id, occurrence_count }));
        const ordered: Array<{ order: number; row: Catalog['occurrences'][number] }> = [];
        for (const [key, part] of parts) {
          for (const [id, owner] of Object.entries(part.owners)) requireData(owners.get(id) === owner);
          const rows = hydrate(part, key, entities);
          rows.forEach((row, index) => ordered.push({ row, order: part.occurrences[index].order }));
        }
        ordered.sort((a, b) => a.order - b.order);
        requireData(ordered.length === manifest.counts.occurrences && ordered.every((entry, index) => entry.order === index));
        const value = catalogData({ items, occurrences: ordered.map((entry) => entry.row) }, manifest, issues);
        signal?.throwIfAborted();
        remember(value.items);
        catalog = value;
        return value;
      } catch (error) {
        for (const key of keys) cache.delete(paths[key]);
        throw error;
      }
    },
    async loadItem(id, signal) {
      const entries = await loadIndex(signal);
      const canonical = known.get(id)?.id;
      const entry = entries.find((item) => item.id === canonical);
      if (!entry) throw new Error('未找到这条消息，可能不在当前静态数据版本中。');
      const keys = [...new Set(entry.occurrences.map((ref) => ref.part))];
      const parts = await Promise.all(keys.map((key) => partition(key, signal)));
      const owner = parts[keys.indexOf(entry.owner)]?.messages.find((message) => message.id === entry.id);
      if (!owner) {
        for (const key of keys) cache.delete(paths[key]);
        throw new Error('静态数据格式不兼容，请重新生成数据。');
      }
      const canonicalOwners = new Map([[entry.id, owner]]);
      const rows = new Map<string, Catalog['occurrences'][number]>();
      const refs = new Map(entry.occurrences.map((ref) => [ref.id, ref.part]));
      const orders = new Set<number>();
      try {
        for (let i = 0; i < parts.length; i++) {
          const part = parts[i];
          requireData(part.owners[entry.id] === entry.owner);
          const occurrences = part.occurrences.filter((row) => row.item_id === entry.id);
          for (const row of occurrences) { requireData(!orders.has(row.order)); orders.add(row.order); }
          const selected: WirePartition = { messages: keys[i] === entry.owner ? [owner] : [],
            owners: { [entry.id]: entry.owner }, occurrences };
          for (const row of hydrate(selected, keys[i], canonicalOwners)) {
            requireData(!rows.has(row.id) && refs.get(row.id) === keys[i]);
            rows.set(row.id, row);
          }
        }
        requireData(rows.size === entry.occurrence_count);
        signal?.throwIfAborted();
        remember([{ id: owner.id, aliases: owner.aliases, latest_occurrence_id: owner.latest_occurrence_id, occurrence_count: owner.occurrence_count }]);
      } catch (error) {
        for (const key of keys) cache.delete(paths[key]);
        throw error;
      }
      const records = entry.occurrences.map((ref) => { const row = rows.get(ref.id); requireData(row); return row; });
      let extras: WireExtras | undefined;
      let detail: PublicItemDetail;
      try {
        if (entry.detail_path) {
          const shard = await get(entry.detail_path, signal);
          requireData(object(shard) && object(shard[entry.id]));
          extras = shard[entry.id] as unknown as WireExtras;
        }
        detail = detailData(decodeItemDetail(entry, records, extras), entry.id, manifest);
      } catch (error) {
        if (entry.detail_path) cache.delete(entry.detail_path);
        throw error;
      }
      requireData(detail.latest_occurrence_id === entry.latest_occurrence_id && detail.occurrences.length === entry.occurrence_count);
      for (const row of detail.occurrences) requireData(row.kind === 'standalone' || issues.some((issue) => issue.id === row.issue_id
        && issue.documents.some((document) => document.id === row.document_id)));
      signal?.throwIfAborted();
      return detail;
    },
  };
}
