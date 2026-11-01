import type { CatalogDate, SelectionCatalog, SelectionOccurrence, Filters, QueryOptions, QueryResult } from './types';
import { manualReviewStampData, validDay } from './review-validation.mjs';
import { sourceUrls } from './source-links.mjs';

export { SOURCE_TYPES, sourceTypeForHost, sourceTypesForHosts } from './source-types.mjs';
export type { SourceType } from './source-types.mjs';
import { sourceTypesForHosts } from './source-types.mjs';

export function originalHosts(occurrence: SelectionOccurrence): string[] {
  if (!('source_urls' in occurrence) || !Array.isArray(occurrence.source_urls)) return occurrence.source_hosts;
  const corrections = 'source_corrections' in occurrence && Array.isArray(occurrence.source_corrections) ? occurrence.source_corrections : [];
  const replaced = new Set(corrections.map((correction) => correction.original_url));
  const urls = [...occurrence.source_urls, ...corrections.map((correction) => correction.corrected_url)].filter((url) => !replaced.has(url));
  return [...new Set(sourceUrls(urls).map((url: string) => new URL(url).hostname.toLowerCase().replace(/\.$/, '')))];
}


export class FilterError extends Error {
  readonly code = 'INVALID_FILTER';
  constructor(message: string) { super(message); this.name = 'FilterError'; }
}

export function defaultFilters(): Filters {
  return { q: '', category: [], college: [], source: [], issue_from: '', issue_to: '',
    date_type: 'event', date_from: '', date_to: '', date_status: 'all', following: false, page: 1 };
}


export function hasVerifiedManualReview(occurrence: SelectionOccurrence): boolean {
  const review = occurrence.manual_review;
  return occurrence.content_status === 'human_verified' && manualReviewStampData(review,
    Boolean(review && Object.hasOwn(review, 'activity'))) && review.status === 'verified';
}

export function confirmedActivityDate(occurrence: SelectionOccurrence, date: CatalogDate): boolean {
  if (!['event', 'deadline', 'registration', 'application'].includes(date.kind)
    || !date.normalized_date || !validDay(date.normalized_date)) return false;
  if (occurrence.content_status === 'verified' && !occurrence.manual_review) {
    return date.literal_source === 'primary' && (date.resolution === 'explicit' || date.resolution === 'contextual');
  }
  return hasVerifiedManualReview(occurrence) && date.literal_source === 'manual' && date.resolution === 'explicit';
}

export function manualPublicUrl(raw: string): string | null {
  if (raw !== raw.trim() || /[\x00-\x20\x7f\\]/.test(raw)) return null;
  try {
    const url = new URL(raw);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    if (!host.includes('.') && !host.startsWith('[')
      || ['localhost', 'local', 'internal'].some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) return null;
    if (host.startsWith('[')) {
      // Public unicast only; mapped IPv4, local, multicast and documentation ranges stay out.
      const ip = host.slice(1, -1);
      if (!/^[23][a-f0-9]{3}:/.test(ip) || /^2001:(?:[01]?[a-f0-9]{1,2}|db8):/.test(ip)
        || /^3fff:/.test(ip)) return null;
    } else if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      const [a, b, c, d] = host.split('.').map(Number);
      if (a === 0 || a === 10 || a === 127 || a >= 224 || a === 100 && b >= 64 && b <= 127
        || a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31
        || a === 192 && (b === 168 || b === 0 && (c === 2 || c === 0 && d !== 9 && d !== 10))
        || a === 198 && (b === 18 || b === 19 || b === 51 && c === 100)
        || a === 203 && b === 0 && c === 113) return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

function requireDay(value: string, label: string): void {
  if (value && !validDay(value)) throw new FilterError(`${label}必须是有效的 YYYY-MM-DD 日期。`);
}

function validateFilters(filters: Filters): void {
  for (const key of ['issue_from', 'issue_to', 'date_from', 'date_to'] as const) requireDay(filters[key], '筛选日期');
  if ((filters.issue_from && filters.issue_to && filters.issue_from > filters.issue_to)
    || (filters.date_from && filters.date_to && filters.date_from > filters.date_to)) {
    throw new FilterError('开始日期不能晚于结束日期。');
  }
  if (!['event', 'deadline', 'listed', 'source'].includes(filters.date_type)
    || !['all', 'known', 'unknown'].includes(filters.date_status)) throw new FilterError('日期筛选类型无效。');
  if (!Number.isSafeInteger(filters.page) || filters.page < 1) throw new FilterError('页码必须是正整数。');
}

export function parseFilters(params: URLSearchParams): Filters {
  const result = defaultFilters();
  const scalar = (key: string, fallback = ''): string => {
    if (params.getAll(key).length > 1) throw new FilterError('单值筛选参数不能重复。');
    return params.get(key) ?? fallback;
  };
  result.q = scalar('q');
  for (const key of ['category', 'college', 'source'] as const) {
    result[key] = [...new Set(params.getAll(key).map((value) => value.trim()).filter(Boolean))];
  }
  for (const key of ['issue_from', 'issue_to', 'date_from', 'date_to'] as const) result[key] = scalar(key);
  result.date_type = scalar('date_type', 'event') as Filters['date_type'];
  result.date_status = scalar('date_status', 'all') as Filters['date_status'];
  const following = scalar('following', '0');
  if (!['0', '1', 'false', 'true'].includes(following)) throw new FilterError('关注筛选参数无效。');
  result.following = following === '1' || following === 'true';
  const page = scalar('page', '1');
  if (!/^\d+$/.test(page)) throw new FilterError('页码必须是正整数。');
  result.page = Number(page);
  validateFilters(result);
  return result;
}

export function serializeFilters(filters: Filters): URLSearchParams {
  validateFilters(filters);
  const params = new URLSearchParams();
  if (filters.q) params.set('q', filters.q);
  for (const key of ['category', 'college', 'source'] as const) {
    for (const value of [...new Set(filters[key].map((entry) => entry.trim()).filter(Boolean))]) params.append(key, value);
  }
  for (const key of ['issue_from', 'issue_to', 'date_from', 'date_to'] as const) if (filters[key]) params.set(key, filters[key]);
  if (filters.date_type !== 'event') params.set('date_type', filters.date_type);
  if (filters.date_status !== 'all') params.set('date_status', filters.date_status);
  if (filters.following) params.set('following', '1');
  if (filters.page !== 1) params.set('page', String(filters.page));
  return params;
}

function resolvedDates(occurrence: SelectionOccurrence, kind: string): string[] {
  return occurrence.dates.filter((date) => date.kind === kind && (date.resolution === 'explicit' || date.resolution === 'contextual')
    && date.normalized_date !== null && validDay(date.normalized_date)
    && (!occurrence.manual_review && occurrence.content_status !== 'human_verified' && date.literal_source !== 'manual'
      || confirmedActivityDate(occurrence, date)))
    .map((date) => date.normalized_date!);
}

export function beijingToday(now = new Date()): string {
  return new Date(now.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

export function currentActivityDate(occurrence: SelectionOccurrence, asOf: string): CatalogDate | null {
  if (!validDay(asOf)) return null;
  let upcoming: CatalogDate | null = null;
  for (const date of occurrence.dates) {
    if (!confirmedActivityDate(occurrence, date) || date.normalized_date! < asOf) continue;
    if (!upcoming || date.normalized_date! < upcoming.normalized_date!) upcoming = date;
  }
  return upcoming;
}

export function queryCatalog<T extends SelectionOccurrence>(catalog: SelectionCatalog & { occurrences: T[] }, filters: Filters, options: QueryOptions): QueryResult<T> {
  validateFilters(filters);
  let occurrences = catalog.occurrences;
  if (options.mode === 'current') {
    if (!options.asOf || !validDay(options.asOf)) throw new FilterError('当前活动需要有效的 YYYY-MM-DD 参考日期。');
    const upcoming: { occurrence: T; day: string }[] = [];
    for (const occurrence of occurrences) {
      const date = currentActivityDate(occurrence, options.asOf);
      if (date) upcoming.push({ occurrence, day: date.normalized_date! });
    }
    // Stable sorting preserves the exported preference order for equal milestones.
    upcoming.sort((a, b) => a.day < b.day ? -1 : a.day > b.day ? 1 : 0);
    occurrences = upcoming.map(({ occurrence }) => occurrence);
  }
  const hits = filters.q.trim() ? new Set(options.searchHits ?? []) : null;
  const categories = new Set(filters.category);
  const colleges = new Set(filters.college);
  const sources = new Set(filters.source);
  const seenItems = new Set<string>();
  const matching: T[] = [];
  for (const occurrence of occurrences) {
    if (options.issueId && occurrence.issue_id !== options.issueId) continue;
    if (hits && !hits.has(occurrence.id)) continue;
    if (categories.size && (occurrence.category === null || !categories.has(occurrence.category))) continue;
    if (colleges.size && !occurrence.colleges.some((college) => colleges.has(college))) continue;
    if (sources.size && !sourceTypesForHosts(originalHosts(occurrence)).some((source) => sources.has(source))) continue;
    if (filters.following && !occurrence.colleges.some((college) => options.followedColleges.has(college))) continue;
    if (options.mode === 'favorites' && !options.favorites.has(occurrence.item_id)) continue;
    if ((filters.issue_from || filters.issue_to) && (occurrence.issue_date === null || !validDay(occurrence.issue_date)
      || (filters.issue_from && occurrence.issue_date < filters.issue_from)
      || (filters.issue_to && occurrence.issue_date > filters.issue_to))) continue;
    const dates = resolvedDates(occurrence, filters.date_type);
    if (filters.date_status === 'known' && dates.length === 0) continue;
    if (filters.date_status === 'unknown' && dates.length > 0) continue;
    if ((filters.date_from || filters.date_to) && !dates.some((day) => (!filters.date_from || day >= filters.date_from)
      && (!filters.date_to || day <= filters.date_to))) continue;
    // Eligibility and date ordering precede canonical-item collapse and pagination.
    if (seenItems.has(occurrence.item_id)) continue;
    seenItems.add(occurrence.item_id);
    matching.push(occurrence);
  }
  const total = matching.length;
  const pages = Math.max(1, Math.ceil(total / 20));
  const page = Math.min(filters.page, pages);
  return { records: matching.slice((page - 1) * 20, page * 20), matchingRecords: matching,
    total, page, pages, per_page: 20 };
}
