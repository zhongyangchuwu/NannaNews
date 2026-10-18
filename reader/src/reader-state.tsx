import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { loadDataset } from './data';
import type { StaticDataset } from './data';
import { beijingToday, parseFilters, queryCatalog, serializeFilters, sourceTypesForHosts, SOURCE_TYPES } from './filter';
import { SearchClient } from './search';
import type { Filters, PublicItemDetail, ReaderMode, ReaderState, ReaderView } from './types';

const ReaderContext = createContext<ReaderState | null>(null);
const PREF_KEY = 'nanna-reader.preferences.v1';
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const ISSUE_ROUTE = /^\/issues\/issue-[a-f0-9]{24}$/;
const MODES: Record<string, ReaderMode> = { '/': 'all', '/upcoming': 'upcoming', '/unknown': 'unknown', '/favorites': 'favorites' };
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
  const [detail, setDetail] = useState<{ key: string; value: PublicItemDetail | null; loading: boolean; error: string | null }>({ key: '', value: null, loading: false, error: null });
  const [search, setSearch] = useState<{ q: string; hits: string[] | null; loading: boolean; error: string | null }>({ q: '', hits: null, loading: false, error: null });
  const searchClient = useRef<SearchClient | null>(null);
  const searchGeneration = useRef(0);
  const [today, setToday] = useState(beijingToday);
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
  const activeIssueId = issueId || (itemId && back ? /^\/issues\/(issue-[a-f0-9]{24})$/.exec(back.split('?')[0])?.[1] : undefined);
  const backMode = back ? MODES[back.split('?')[0]] : undefined;
  const mode: ReaderMode = MODES[path] || backMode || 'all';
  const view: ReaderView = itemId ? 'item' : issueId ? 'issue' : path === '/issues' ? 'issues' : MODES[path] || 'all';
  const routeError = !(path in MODES) && path !== '/issues' && !itemId && !issueId ? '页面不存在，请返回全部消息。' : null;

  useEffect(() => setActionError(null), [location.key]);

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
    const timer = window.setInterval(() => setToday(beijingToday()), 60_000);
    const update = () => setToday(beijingToday());
    window.addEventListener('focus', update);
    return () => { window.clearInterval(timer); window.removeEventListener('focus', update); };
  }, []);

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
    if (!dataset || !q) {
      setSearch({ q, hits: null, loading: false, error: null });
      return;
    }
    setSearch({ q, hits: null, loading: true, error: null });
    try {
      searchClient.current ??= new SearchClient(dataset.searchIndexUrl);
      searchClient.current.search(q).then((hits) => {
        if (searchGeneration.current === generation) setSearch({ q, hits, loading: false, error: null });
      }).catch((error: unknown) => {
        if (searchGeneration.current !== generation || (typeof error === 'object' && error !== null && 'code' in error && error.code === 'SUPERSEDED')) return;
        setSearch({ q, hits: null, loading: false, error: error instanceof Error ? error.message : '浏览器检索失败，请重试。' });
      });
    } catch (error) {
      setSearch({ q, hits: null, loading: false, error: error instanceof Error ? error.message : '浏览器不支持本地检索。' });
    }
    return () => { searchGeneration.current++; };
  }, [dataset, filters.q]);

  useEffect(() => {
    if (!dataset || !itemId) { setDetail({ key: '', value: null, loading: false, error: null }); return; }
    const controller = new AbortController();
    let active = true;
    setDetail({ key: itemId, value: null, loading: true, error: null });
    dataset.loadItem(itemId, controller.signal).then((value) => {
      if (active) setDetail({ key: itemId, value, loading: false, error: null });
    }).catch((error: unknown) => {
      if (active) setDetail({ key: itemId, value: null, loading: false, error: error instanceof Error ? error.message : '消息详情加载失败。' });
    });
    return () => { active = false; controller.abort(); };
  }, [dataset, itemId]);

  const favorites = useMemo(() => new Set([...preferences.favorites].map((id) => dataset?.canonicalItemId(id) ?? id)),
    [dataset, preferences.favorites]);

  const result = useMemo(() => queryCatalog(dataset?.catalog || emptyCatalog, filters, {
    mode, issueId: activeIssueId || undefined, favorites, followedColleges: preferences.followedColleges,
    asOf: today, searchHits: filters.q.trim() ? search.q === filters.q.trim() && !search.loading && !search.error ? search.hits || [] : [] : null,
  }), [dataset, filters, mode, activeIssueId, favorites, preferences.followedColleges, today, search]);

  const options = useMemo(() => {
    const categories = new Set<string>();
    const colleges = new Set<string>();
    const sources = new Set<string>();
    for (const occurrence of dataset?.catalog.occurrences || []) {
      if (occurrence.category) categories.add(occurrence.category);
      for (const college of occurrence.colleges) colleges.add(college);
      for (const source of sourceTypesForHosts(occurrence.source_hosts)) sources.add(source);
    }
    return { categories: [...categories].sort(), colleges: [...colleges].sort((a, b) => a.localeCompare(b, 'zh-CN')),
      sources: SOURCE_TYPES.filter((source) => sources.has(source)) };
  }, [dataset]);
  const selectedItem = detail.key === itemId ? detail.value : null;
  const selectedOccurrence = selectedItem?.occurrences.find((occurrence) => occurrence.id === params.get('occ'))
    || selectedItem?.occurrences.find((occurrence) => occurrence.id === selectedItem.latest_occurrence_id) || null;
  const selectedIssue = dataset?.issues.find((issue) => issue.id === (selectedOccurrence?.issue_id || issueId)) || null;
  const missingIssue = dataset && issueId && !selectedIssue ? '未找到这期刊物，可能不在当前数据版本中。' : null;
  const error = actionError || parsed.error || routeError || loadError || (search.q === filters.q.trim() ? search.error : null) || missingIssue;
  const queryString = serializeFilters(filters).toString();
  const currentList = `${itemId ? '/' : path}${queryString ? `?${queryString}` : ''}`;
  const listHref = itemId ? back || `/${queryString ? `?${queryString}` : ''}` : issueId ? `/issues${queryString ? `?${queryString}` : ''}` : currentList;

  function updatePreferences(kind: 'favorites' | 'followedColleges', value: string) {
    if (kind === 'favorites' ? !ITEM_ID.test(value) : !options.colleges.includes(value)) return;
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
    status: loading ? 'loading' : error ? 'error' : 'ready', error,
    manifest: dataset?.manifest || null, issues: dataset?.issues || [], view, mode, filters,
    records: result.records, total: result.total, page: result.page, pages: result.pages, perPage: result.per_page,
    searching: Boolean(filters.q.trim()) && (search.loading || search.q !== filters.q.trim()), today,
    selectedIssue, selectedItem, selectedOccurrence,
    detailStatus: !itemId ? 'idle' : detail.key !== itemId || detail.loading ? 'loading' : detail.error ? 'error' : selectedItem ? 'ready' : 'loading',
    detailError: detail.key === itemId ? detail.error : null,
    favorites, followedColleges: preferences.followedColleges, preferenceError: preferences.error,
    options,
    setFilters: updateFilters,
    resetFilters() { navigate({ pathname: routeError ? '/' : location.pathname, search: itemId && back ? `?_from=${encodeURIComponent(back)}` : '' }); },
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
      setLoadAttempt((attempt) => attempt + 1);
    },
    itemHref(id) {
      const query = serializeFilters(filters);
      query.set('_from', itemId ? back || '/' : currentList);
      const occurrenceId = result.records.find((record) => record.item_id === id)?.id;
      if (occurrenceId) query.set('occ', occurrenceId);
      return `/items/${encodeURIComponent(id)}?${query}`;
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
