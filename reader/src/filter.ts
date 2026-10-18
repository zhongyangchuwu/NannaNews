import type { Catalog, CatalogOccurrence, Filters, Freshness, QueryOptions, QueryResult } from './types';

export const SOURCE_TYPES = ['官网', '微信公众号', '其他'] as const;
export type SourceType = (typeof SOURCE_TYPES)[number];
const INSTITUTIONAL_DOMAINS = ['nju.edu.cn', 'pku.edu.cn', 'neea.edu.cn', 'csc.edu.cn'];

function isDomainOrSubdomain(host: string, domain: string): boolean {
  return host === domain || host.endsWith(`.${domain}`);
}

export function sourceTypeForHost(host: string): SourceType {
  const hostname = host.toLowerCase().replace(/\.$/, '');
  if (isDomainOrSubdomain(hostname, 'mp.weixin.qq.com')) return '微信公众号';
  // Recognized institutional roots; unknown sites retain their original links under 其他.
  if (INSTITUTIONAL_DOMAINS.some((domain) => isDomainOrSubdomain(hostname, domain))
    || isDomainOrSubdomain(hostname, 'edu') || isDomainOrSubdomain(hostname, 'gov.cn')) return '官网';
  return '其他';
}

export function sourceTypesForHosts(hosts: readonly string[]): SourceType[] {
  let official = false;
  let wechat = false;
  for (const host of hosts) {
    const type = sourceTypeForHost(host);
    if (type === '官网') official = true;
    if (type === '微信公众号') wechat = true;
  }
  // Archive and registration links do not make a known source also count as 其他.
  const types: SourceType[] = [];
  if (official) types.push('官网');
  if (wechat) types.push('微信公众号');
  if (types.length === 0) types.push('其他');
  return types;
}

export class FilterError extends Error {
  readonly code = 'INVALID_FILTER';
  constructor(message: string) { super(message); this.name = 'FilterError'; }
}

export function defaultFilters(): Filters {
  return { q: '', category: [], college: [], source: [], issue_from: '', issue_to: '',
    date_type: 'event', date_from: '', date_to: '', date_status: 'all', following: false, page: 1 };
}

function validDay(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]!;
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

export function beijingToday(now: Date = new Date()): string {
  if (!Number.isFinite(now.getTime())) throw new FilterError('当前时间无效。');
  return new Date(now.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

export function getFreshness(expiry: string | null, asOf: string): Freshness {
  if (!validDay(asOf)) throw new FilterError('当前日期必须是有效的 YYYY-MM-DD 日期。');
  if (expiry === null || !validDay(expiry)) return 'unknown';
  return expiry < asOf ? 'expired' : 'not_expired';
}


function resolvedDates(occurrence: CatalogOccurrence, kind: string): string[] {
  return occurrence.dates.filter((date) => date.kind === kind && (date.resolution === 'explicit' || date.resolution === 'contextual')
    && date.normalized_date !== null && validDay(date.normalized_date)).map((date) => date.normalized_date!);
}

export function queryCatalog(catalog: Catalog, filters: Filters, options: QueryOptions): QueryResult {
  validateFilters(filters);
  if (!validDay(options.asOf)) throw new FilterError('当前日期必须是有效的 YYYY-MM-DD 日期。');
  const hits = filters.q.trim() ? new Set(options.searchHits ?? []) : null;
  const categories = new Set(filters.category);
  const colleges = new Set(filters.college);
  const sources = new Set(filters.source);
  const seenItems = new Set<string>();
  const matching: CatalogOccurrence[] = [];
  for (const occurrence of catalog.occurrences) {
    if (options.issueId && occurrence.issue_id !== options.issueId) continue;
    if (hits && !hits.has(occurrence.id)) continue;
    if (categories.size && (occurrence.category === null || !categories.has(occurrence.category))) continue;
    if (colleges.size && !occurrence.colleges.some((college) => colleges.has(college))) continue;
    if (sources.size && !sourceTypesForHosts(occurrence.source_hosts).some((source) => sources.has(source))) continue;
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
    if (options.mode === 'upcoming' || options.mode === 'unknown') {
      const freshness = getFreshness(occurrence.expiry_date, options.asOf);
      if (options.mode === 'upcoming' && freshness !== 'not_expired') continue;
      if (options.mode === 'unknown' && freshness !== 'unknown') continue;
    }
    // The exported order is authoritative (including document/detail tie-breaks).
    if (seenItems.has(occurrence.item_id)) continue;
    seenItems.add(occurrence.item_id);
    matching.push(occurrence);
  }
  const total = matching.length;
  const pages = Math.max(1, Math.ceil(total / 20));
  const page = Math.min(filters.page, pages);
  return { records: matching.slice((page - 1) * 20, page * 20), total, page, pages, per_page: 20 };
}
