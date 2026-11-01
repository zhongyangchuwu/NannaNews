import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { loadDataset } from './data';
import type { StaticDataset } from './data';
import { beijingToday, originalHosts, parseFilters, queryCatalog, serializeFilters, sourceTypesForHosts, SOURCE_TYPES } from './filter';
import { SearchClient } from './search';
import type { Catalog, SelectionCatalog, HistoryIndex, Filters, PublicIssue, PublicItemDetail, ReaderMode, ReaderState, ReaderView } from './types';

const ReaderContext = createContext<ReaderState | null>(null);
const PREF_KEY = 'nanna-reader.preferences.v1';
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const ISSUE_ROUTE = /^\/issues\/issue-[a-f0-9]{24}$/;
const MODES: Record<string, ReaderMode> = { '/': 'all', '/activities': 'current', '/history': 'all', '/favorites': 'favorites' };
const VIEWS: Record<string, ReaderView> = { '/': 'edition', '/activities': 'activities', '/history': 'history', '/favorites': 'favorites', '/issues': 'issues' };
const emptyCatalog = { items: [], occurrences: [] };

function readPreferences() {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    if (typeof raw !== 'object' || raw === null) throw new Error('invalid');
    const value = raw as Record<string, unknown>;
    const favorites = Array.isArray(value.favorites)
      ? value.favorites.filter((id): id is string => typeof id === 'string' && ITEM_ID.test(id)).slice(0, 10000) : [];
    const colleges = Array.isArray(value.followedColleges)
      ? value.followedColleges.filter((name): name is string => typeof name === 'string' && name.length > 0 && name.length <= 100).slice(0, 1000) : [];
    return { favorites: new Set(favorites), followedColleges: new Set(colleges), error: null as string | null };
  } catch {
    return { favorites: new Set<string>(), followedColleges: new Set<string>(), error: '本地偏好不可读取；本次收藏和关注仅在当前页面有效。' };
  }
}

function returnPath(value: string | null): string | null {
  if (!value || !value.startsWith('/') || value.startsWith('//')) return null;
  try {
    const url = new URL(value, 'https://reader.invalid');
    if (!(url.pathname in MODES) && url.pathname !== '/issues' && !ISSUE_ROUTE.test(url.pathname)) return null;
    const query = serializeFilters(parseFilters(url.searchParams)).toString();
    return url.pathname + (query ? `?${query}` : '');
  } catch { return null; }
}

export function ReaderProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [dataset, setDataset] = useState<StaticDataset | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [preferences, setPreferences] = useState(readPreferences);
  const [today, setToday] = useState(beijingToday);
  const [detail, setDetail] = useState<{ key: string; value: PublicItemDetail | null; loading: boolean; error: string | null }>({ key: '', value: null, loading: false, error: null });
  const [scope, setScope] = useState<{ dataset: StaticDataset | null; key: string; catalog: SelectionCatalog | null; history: HistoryIndex | null; error: string | null }>({ dataset: null, key: '', catalog: null, history: null, error: null });
  const [pageData, setPageData] = useState<{ dataset: StaticDataset | null; key: string; records: Catalog['occurrences'] | null; error: string | null }>({ dataset: null, key: '', records: null, error: null });
  const [scopeAttempt, setScopeAttempt] = useState(0);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [metadataRevision, setMetadataRevision] = useState(0);
  const [search, setSearch] = useState<{ key: string; q: string; hits: string[] | null; loading: boolean; error: string | null }>({ key: '', q: '', hits: null, loading: false, error: null });
  const searchClient = useRef<SearchClient | null>(null);
  const searchGeneration = useRef(0);
  const params = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const parsed = useMemo(() => {
    try { return { filters: parseFilters(params), error: null }; }
    catch (error) { return { filters: parseFilters(new URLSearchParams()), error: error instanceof Error ? error.message : '筛选条件无效。' }; }
  }, [params]);
  const filters = parsed.filters;
  const path = location.pathname.replace(/\/$/, '') || '/';
  const itemId = /^\/items\/(item-[a-f0-9]{24})$/.exec(path)?.[1] || null;
  const issueId = /^\/issues\/(issue-[a-f0-9]{24})$/.exec(path)?.[1] || null;
  const back = returnPath(params.get('_from'));
  const backIssueId = itemId && back ? /^\/issues\/(issue-[a-f0-9]{24})$/.exec(back.split('?')[0])?.[1] : undefined;
  const backMode = back ? MODES[back.split('?')[0]] : undefined;
  const mode: ReaderMode = MODES[path] || backMode || 'all';
  const view: ReaderView = itemId ? 'item' : issueId ? 'issue' : VIEWS[path] || 'edition';
  const routeError = !(path in VIEWS) && !itemId && !issueId ? '页面不存在，请返回本期消息。' : null;
  const latestIssue = useMemo(() => {
    let latest: PublicIssue | null = null;
    for (const issue of dataset?.issues || []) {
      if (issue.issue_date && (!latest?.issue_date || issue.issue_date > latest.issue_date)) latest = issue;
    }
    return latest;
  }, [dataset]);
  const editionIssueId = issueId || backIssueId;
  const editionIssue = editionIssueId ? dataset?.issues.find((issue) => issue.id === editionIssueId) || null : latestIssue;
  const activeIssueId = issueId || backIssueId || (view === 'edition' || (itemId && back?.split('?')[0] === '/') ? editionIssue?.id : undefined);
  const plainHistory = view === 'history' && !filters.q.trim() && !filters.category.length && !filters.college.length && !filters.source.length
    && !filters.following && !filters.issue_from && !filters.issue_to && !filters.date_from && !filters.date_to
    && filters.date_status === 'all' && filters.date_type === 'event'
    && dataset?.manifest.schema_version === 3 && Boolean(dataset.manifest.files.history_index);
  const scopeKey = view === 'edition' ? (editionIssue ? `issue:${editionIssue.id}` : '')
    : view === 'issue' ? (editionIssue ? `issue:${editionIssue.id}` : '')
      : plainHistory ? 'history' : view === 'activities' || view === 'history' || view === 'favorites' ? 'global' : '';
  const scopedCatalog = scope.dataset === dataset && scope.key === scopeKey && scopeKey ? scope.catalog : null;
  const scopedHistory = scope.dataset === dataset && scope.key === scopeKey && scopeKey === 'history' ? scope.history : null;
  const globalScope = scopeKey === 'global' || scopeKey === 'history';

  useEffect(() => {
    if (!dataset || !scopeKey) return;
    const controller = new AbortController();
    let active = true;
    setScope({ dataset, key: scopeKey, catalog: null, history: null, error: null });
    const request = scopeKey === 'history' ? dataset.loadHistory(controller.signal).then((history) => ({ catalog: null, history }))
      : (scopeKey === 'global' ? dataset.loadSelection(controller.signal) : dataset.loadIssue(scopeKey.slice(6), controller.signal))
        .then((catalog) => ({ catalog, history: null }));
    request.then(({ catalog, history }) => {
      if (active) { setScope({ dataset, key: scopeKey, catalog, history, error: null }); setMetadataRevision((value) => value + 1); }
    }).catch((error: unknown) => {
      if (active) setScope({ dataset, key: scopeKey, catalog: null, history: null, error: error instanceof Error ? error.message : '静态数据加载失败。' });
    });
    return () => { active = false; controller.abort(); };
  }, [dataset, scopeKey, scopeAttempt]);

  useEffect(() => setActionError(null), [location.key]);

  useEffect(() => {
    let timer: number;
    const update = () => setToday(beijingToday());
    const tick = () => {
      update();
      timer = window.setTimeout(tick, 60_000 - Date.now() % 60_000);
    };
    tick();
    window.addEventListener('focus', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('focus', update);
      document.removeEventListener('visibilitychange', update);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setLoadError(null);
    setDataset(null);
    loadDataset(controller.signal).then((value) => {
      if (active) { setDataset(value); setLoading(false); }
    }).catch((error: unknown) => {
      if (active) { setLoadError(error instanceof Error ? error.message : '静态数据加载失败。'); setLoading(false); }
    });
    return () => { active = false; controller.abort(); };
  }, [loadAttempt]);

  useEffect(() => {
    const update = (event: StorageEvent) => { if (event.key === PREF_KEY || event.key === null) setPreferences(readPreferences()); };
    window.addEventListener('storage', update);
    return () => window.removeEventListener('storage', update);
  }, []);

  useEffect(() => () => {
    searchGeneration.current++;
    searchClient.current?.dispose();
    searchClient.current = null;
  }, [dataset]);

  useEffect(() => {
    const generation = ++searchGeneration.current;
    const q = filters.q.trim();
    if (!dataset || !scopeKey || !q) {
      setSearch({ key: scopeKey, q, hits: null, loading: false, error: null });
      return;
    }
    setSearch({ key: scopeKey, q, hits: null, loading: true, error: null });
    try {
      searchClient.current ??= new SearchClient(dataset.searchIndexUrl);
      searchClient.current.search(q).then((hits) => {
        if (searchGeneration.current === generation) setSearch({ key: scopeKey, q, hits, loading: false, error: null });
      }).catch((error: unknown) => {
        if (searchGeneration.current !== generation || (typeof error === 'object' && error !== null && 'code' in error && error.code === 'SUPERSEDED')) return;
        setSearch({ key: scopeKey, q, hits: null, loading: false, error: error instanceof Error ? error.message : '浏览器检索失败，请重试。' });
      });
    } catch (error) {
      setSearch({ key: scopeKey, q, hits: null, loading: false, error: error instanceof Error ? error.message : '浏览器不支持本地检索。' });
    }
    return () => { searchGeneration.current++; };
  }, [dataset, filters.q, scopeKey]);

  useEffect(() => {
    if (!dataset || !itemId) { setDetail({ key: '', value: null, loading: false, error: null }); return; }
    const controller = new AbortController();
    let active = true;
    setDetail({ key: itemId, value: null, loading: true, error: null });
    dataset.loadItem(itemId, controller.signal).then((value) => {
      if (active) { setDetail({ key: itemId, value, loading: false, error: null }); setMetadataRevision((revision) => revision + 1); }
    }).catch((error: unknown) => {
      if (active) setDetail({ key: itemId, value: null, loading: false, error: error instanceof Error ? error.message : '消息详情加载失败。' });
    });
    return () => { active = false; controller.abort(); };
  }, [dataset, itemId, detailAttempt]);

  const favorites = useMemo(() => new Set([...preferences.favorites].map((id) => dataset?.canonicalItemId(id) ?? id)),
    [dataset, preferences.favorites, metadataRevision]);

  const result = useMemo(() => {
    if (scopeKey === 'history') {
      const matchingRecords = scopedHistory?.occurrences || [];
      const total = matchingRecords.length, pages = Math.max(1, Math.ceil(total / 20)), page = Math.min(filters.page, pages);
      return { records: matchingRecords.slice((page - 1) * 20, page * 20), matchingRecords, total, pages, page, per_page: 20 as const };
    }
    return queryCatalog(scopedCatalog || emptyCatalog, filters, {
      mode, asOf: today, issueId: activeIssueId || undefined, favorites, followedColleges: preferences.followedColleges,
      searchHits: filters.q.trim() ? search.key === scopeKey && search.q === filters.q.trim() && !search.loading && !search.error ? search.hits || [] : [] : null,
    });
  }, [scopedCatalog, scopedHistory, scopeKey, filters, mode, today, activeIssueId, favorites, preferences.followedColleges, search]);
  // Only identity and partition routing affect hydration; filter or preference objects may change without changing this page.
  const pageKey = JSON.stringify([scopeKey, view, mode, result.records.map(({ id, item_id, issue_id }) => [id, item_id, issue_id])]);
  const scopeReady = Boolean(scopedCatalog || scopedHistory);
  const searchReady = !filters.q.trim() || (search.key === scopeKey && search.q === filters.q.trim() && !search.loading && !search.error);
  useEffect(() => {
    if (!dataset || !globalScope || !scopeReady || !searchReady) return;
    if (pageData.dataset === dataset && pageData.key === pageKey && pageData.records) return;
    const controller = new AbortController();
    let active = true;
    const rows = result.records;
    setPageData({ dataset, key: pageKey, records: null, error: null });
    dataset.hydrateSelection(rows, controller.signal).then((records) => {
      if (active) setPageData({ dataset, key: pageKey, records, error: null });
    }).catch((error: unknown) => {
      if (active) setPageData({ dataset, key: pageKey, records: null, error: error instanceof Error ? error.message : '静态数据加载失败。' });
    });
    return () => { active = false; controller.abort(); };
    // pageKey covers every row field consumed by hydrateSelection; array identity is intentionally irrelevant.
  }, [dataset, globalScope, scopeReady, searchReady, pageKey, scopeAttempt]);
  const pageCurrent = scopeReady && searchReady && pageData.dataset === dataset && pageData.key === pageKey;
  const pageError = globalScope && pageCurrent ? pageData.error : null;
  const records = globalScope ? (pageCurrent ? pageData.records || [] : []) : result.records as Catalog['occurrences'];
  const editionRecords = view === 'edition' || view === 'issue' || backIssueId
    || (itemId && back?.split('?')[0] === '/') ? result.matchingRecords as Catalog['occurrences'] : emptyCatalog.occurrences;

  const options = useMemo(() => {
    if (scopedHistory) return scopedHistory.options;
    const categories = new Set<string>();
    const colleges = new Set<string>();
    const sources = new Set<string>();
    for (const occurrence of scopedCatalog?.occurrences || []) {
      if (occurrence.category) categories.add(occurrence.category);
      for (const college of occurrence.colleges) colleges.add(college);
      for (const source of sourceTypesForHosts(originalHosts(occurrence))) sources.add(source);
    }
    return { categories: [...categories].sort(), colleges: [...colleges].sort((a, b) => a.localeCompare(b, 'zh-CN')),
      sources: SOURCE_TYPES.filter((source) => sources.has(source)) };
  }, [scopedCatalog, scopedHistory]);
  const selectedItem = detail.key === itemId ? detail.value : null;
  const selectedOccurrence = selectedItem?.occurrences.find((occurrence) => occurrence.id === params.get('occ'))
    || selectedItem?.occurrences.find((occurrence) => occurrence.id === selectedItem.latest_occurrence_id) || null;
  const selectedIssue = itemId
    ? dataset?.issues.find((issue) => issue.id === selectedOccurrence?.issue_id) || null
    : view === 'edition' || view === 'issue' ? editionIssue : null;
  const missingIssue = dataset && issueId && !selectedIssue ? '未找到这期刊物，可能不在当前数据版本中。' : null;
  const scopeError = scope.dataset === dataset && scope.key === scopeKey && scopeKey ? scope.error : null;
  const error = actionError || parsed.error || routeError || loadError || scopeError || pageError || (scopeKey && search.key === scopeKey && search.q === filters.q.trim() ? search.error : null) || missingIssue;
  const queryString = serializeFilters(filters).toString();
  const detailList = `${selectedIssue ? `/issues/${encodeURIComponent(selectedIssue.id)}` : '/history'}${queryString ? `?${queryString}` : ''}`;
  const currentList = `${path}${queryString ? `?${queryString}` : ''}`;
  const listHref = itemId ? back || detailList : issueId ? `/issues${queryString ? `?${queryString}` : ''}` : currentList;

  function updatePreferences(kind: 'favorites' | 'followedColleges', value: string) {
    if (kind === 'favorites' ? !ITEM_ID.test(value) : !options.colleges.includes(value)
      && !selectedOccurrence?.colleges.includes(value)
      && !selectedOccurrence?.college_mentions.some((college) => college.name === value)
      && !selectedOccurrence?.date_review?.source_review.activity?.colleges.some((college) => college.name === value)) return;
    const target = kind === 'favorites' ? dataset?.canonicalItemId(value) ?? value : value;
    setPreferences((previous) => {
      const values = kind === 'favorites'
        ? new Set([...previous.favorites].map((id) => dataset?.canonicalItemId(id) ?? id))
        : new Set(previous[kind]);
      if (values.has(target)) values.delete(target); else values.add(target);
      const next = { ...previous, [kind]: values };
      try {
        localStorage.setItem(PREF_KEY, JSON.stringify({ favorites: [...next.favorites], followedColleges: [...next.followedColleges] }));
        next.error = null;
      } catch { next.error = '浏览器未允许保存本地偏好；本次收藏和关注仅在当前页面有效，刷新后可能丢失。'; }
      return next;
    });
  }

  function updateFilters(patch: Partial<Filters>) {
    try {
      const query = serializeFilters({ ...filters, ...patch, page: patch.page ?? 1 });
      if (itemId && back) query.set('_from', back);
      if (itemId && params.has('occ')) query.set('occ', params.get('occ')!);
      navigate({ pathname: location.pathname, search: query.size ? `?${query}` : '' });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '筛选条件无效。');
    }
  }

  const state: ReaderState = {
    status: loading || Boolean(dataset && scopeKey && !(scopedCatalog || scopedHistory) && !scopeError) || Boolean(globalScope && (scopedCatalog || scopedHistory) && !pageError && (!pageCurrent || !pageData.records) && !(filters.q.trim() && search.error)) ? 'loading' : error ? 'error' : 'ready', error,
    manifest: dataset?.manifest || null, issues: dataset?.issues || [], view, mode, filters,
    editionIssue, editionRecords, today,
    records, total: result.total, page: result.page, pages: result.pages, perPage: result.per_page,
    searching: Boolean(scopeKey && filters.q.trim()) && (search.key !== scopeKey || search.loading || search.q !== filters.q.trim()),
    selectedIssue, selectedItem, selectedOccurrence,
    detailStatus: !itemId ? 'idle' : detail.key !== itemId || detail.loading ? 'loading' : detail.error ? 'error' : selectedItem ? 'ready' : 'loading',
    detailError: detail.key === itemId ? detail.error : null,
    favorites, followedColleges: preferences.followedColleges, preferenceError: preferences.error,
    options,
    setFilters: updateFilters,
    resetFilters() {
      const query = new URLSearchParams();
      if (itemId && back) query.set('_from', back);
      if (itemId && params.has('occ')) query.set('occ', params.get('occ')!);
      navigate({ pathname: routeError ? '/' : location.pathname, search: query.size ? `?${query}` : '' });
    },
    setPage(page) { updateFilters({ page }); window.scrollTo({ top: 0, behavior: 'instant' }); },
    toggleFavorite(id) { updatePreferences('favorites', id); },
    toggleFollowedCollege(name) { updatePreferences('followedColleges', name); },
    selectOccurrence(id) {
      if (!selectedItem?.occurrences.some((occurrence) => occurrence.id === id)) return;
      const query = new URLSearchParams(params);
      query.set('occ', id);
      navigate({ pathname: location.pathname, search: `?${query}` });
    },
    retry() {
      if (actionError) { setActionError(null); return; }
      if (parsed.error || routeError || missingIssue) { navigate('/'); return; }
      if (scopeError || pageError) { setScopeAttempt((attempt) => attempt + 1); return; }
      if (itemId && detail.error) { setDetailAttempt((attempt) => attempt + 1); return; }
      setLoadAttempt((attempt) => attempt + 1);
    },
    itemHref(id, occurrenceId) {
      const canonicalId = dataset?.canonicalItemId(id) ?? id;
      const query = serializeFilters(filters);
      query.set('_from', itemId ? back || detailList : currentList);
      const records = view === 'edition' || view === 'issue' ? editionRecords : globalScope ? pageData.records || [] : result.records;
      const selected = selectedItem?.id === canonicalId ? selectedOccurrence?.id : undefined;
      const exactOccurrence = occurrenceId || selected || records.find((record) => (dataset?.canonicalItemId(record.item_id) ?? record.item_id) === canonicalId)?.id;
      if (exactOccurrence) query.set('occ', exactOccurrence);
      return `/items/${encodeURIComponent(canonicalId)}?${query}`;
    },
    issueHref(id) { return `/issues/${encodeURIComponent(id)}${queryString ? `?${queryString}` : ''}`; },
    assetUrl(path) {
      if (!dataset) throw new Error('静态数据尚未加载。');
      return dataset.assetUrl(path);
    },
    listHref,
  };
  return <ReaderContext.Provider value={state}>{children}</ReaderContext.Provider>;
}

export function useReader(): ReaderState {
  const value = useContext(ReaderContext);
  if (!value) throw new Error('ReaderProvider is required.');
  return value;
}
