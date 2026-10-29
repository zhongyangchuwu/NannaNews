import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router';
import { Archive, ArrowRight, Bookmark, CalendarDays, CalendarClock, Filter, Newspaper, Search, X } from 'lucide-react';
import { useReader } from './reader-state';
import type { Filters, ReaderState, ReaderView } from './types';
import { FilterPanel } from './components/FilterPanel';
import { IssueArchive } from './components/IssueArchive';
import { ItemDetail } from './components/ItemDetail';
import { OccurrenceResults, Pagination, ResultSummary } from './components/OccurrenceList';
import { Button } from './components/button';
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from './components/sheet';
import { EditionPage } from './components/EditionPage';
import { useReadingPosition } from './reading-position';

type ModeLink = { mode: ReaderView; label: string; path: string; icon: typeof Newspaper };
const modeLinks: ModeLink[] = [
  { mode: 'edition', label: '本期消息', path: '/', icon: Newspaper },
  { mode: 'activities', label: '当前活动', path: '/activities', icon: CalendarClock },
  { mode: 'history', label: '历史消息', path: '/history', icon: Archive },
  { mode: 'favorites', label: '我的收藏', path: '/favorites', icon: Bookmark },
];

const viewHeadings: Record<string, { title: string; description: string }> = {
  history: { title: '历史消息', description: '查找往期消息，也可以按期阅读。' },
  activities: { title: '当前活动', description: '查看今天及以后仍有日程的活动与截止事项。活动日期不等于报名期限，参与方式请以原始通知为准。' },
  favorites: { title: '我的收藏', description: '收藏感兴趣的消息，方便回看。书签只保存在本设备，不上传或跨设备同步。' },
};
const dateTypeLabels: Record<Filters['date_type'], string> = { event: '活动日期', deadline: '截止日期', listed: '列明日期', source: '来源日期' };
 
type SearchScope = 'edition' | 'activities' | 'history' | 'favorites';
function Header({ state }: { state: ReaderState }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [draft, setDraft] = useState(state.filters.q);
  const composing = useRef(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  useEffect(() => setDraft(state.filters.q), [state.filters.q]);
  const latestIssue = state.issues[0];
  const backPath = state.listHref.split('?')[0];
  const issueContext = state.view === 'issue' || (state.view === 'item' && backPath.startsWith('/issues/'));
  const activeMode = issueContext ? state.selectedIssue?.id === latestIssue?.id ? 'edition' : 'history'
    : state.view === 'item' ? backPath === '/activities' ? 'activities'
      : backPath === '/favorites' ? 'favorites' : backPath === '/' ? 'edition' : 'history'
      : state.view === 'issues' ? 'history' : state.view;
  const filtersAvailable = ['history', 'activities', 'favorites'].includes(state.view);
  const activeFilterCount = state.filters.category.length + state.filters.college.length + state.filters.source.length
    + [state.filters.following, state.filters.issue_from, state.filters.issue_to, state.filters.date_from, state.filters.date_to,
      state.filters.date_status !== 'all'].filter(Boolean).length;
  const contextScope: SearchScope = activeMode === 'activities' || activeMode === 'favorites'
    ? activeMode : state.view === 'edition' || issueContext || backPath === '/' ? 'edition' : 'history';
  const [scope, setScope] = useState<SearchScope>(contextScope);
  useEffect(() => setScope(contextScope), [contextScope]);
  const scopeLabels = { edition: issueContext ? '这期消息' : '本期消息', activities: '当前活动', history: '全部历史', favorites: '我的收藏' };
  const searchScope = (value: string) => {
    const editionPath = issueContext ? state.view === 'item' ? backPath : location.pathname : '/';
    const pathname = scope === 'edition' ? editionPath : `/${scope}`;
    const currentPath = state.view === 'item' ? backPath : location.pathname;
    const currentSearch = state.view === 'item' ? state.listHref.split('?')[1] || '' : location.search;
    const query = new URLSearchParams(pathname === currentPath ? currentSearch : '');
    query.delete('page');
    if (value.trim()) query.set('q', value.trim());
    else query.delete('q');
    navigate({ pathname, search: query.size ? `?${query}` : '' });
  };
  const onSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!composing.current) searchScope(draft);
  };
  return (
    <>
      <header className="reader-header">
        <div className="reader-header-inner">
          <Link className="brand-lockup" to="/" aria-label="南哪消息首页">
            <span className="brand-seal" aria-hidden="true"><Newspaper size={25} /></span>
            <span className="brand-wordmark"><strong>南哪消息</strong></span>
          </Link>
          {latestIssue?.issue_date && <div className="header-note"><span className="archive-range">最新一期 · {latestIssue.issue_date}</span></div>}
        </div>
      </header>
      <div className="reader-toolbar-wrap">
        <div className="reader-toolbar">
          <nav className="mode-nav" aria-label="消息导航">
            {modeLinks.map(({ mode, label, path, icon: Icon }) => <Link key={mode} to={path} className={activeMode === mode ? 'mode-link is-active' : 'mode-link'} aria-current={activeMode === mode ? 'page' : undefined}><Icon size={16} aria-hidden="true" /><span>{label}</span>{mode === 'favorites' && state.favorites.size > 0 && <span className="mode-count">{state.favorites.size}</span>}</Link>)}
          </nav>
          <form className="search-form" role="search" onSubmit={onSearch}>
            <Search size={18} aria-hidden="true" />
            <select className="search-scope" aria-label="搜索范围" value={scope} onChange={(event) => setScope(event.target.value as SearchScope)}>
              <option value="edition">{scopeLabels.edition}</option><option value="activities">当前活动</option><option value="history">全部历史</option>
              {contextScope === 'favorites' && <option value="favorites">我的收藏</option>}
            </select>
            <label className="visually-hidden" htmlFor="reader-search">搜索{scopeLabels[scope]}</label>
            <input id="reader-search" type="search" value={draft} placeholder={`搜索${scopeLabels[scope]}…`} onChange={(event) => setDraft(event.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || composing.current)) event.preventDefault(); }} />
            {draft && <button type="button" className="search-clear" aria-label="清除搜索词" onClick={() => { setDraft(''); if (state.filters.q) searchScope(''); }}><X size={15} aria-hidden="true" /></button>}
            <Button type="submit" size="sm">搜索</Button>
          </form>
          {filtersAvailable && <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
            <SheetTrigger asChild><Button type="button" variant="outline" className="mobile-filter-trigger" aria-label={activeFilterCount ? `筛选条件，已启用 ${activeFilterCount} 项` : '筛选条件'}><Filter size={16} aria-hidden="true" /><span>筛选</span>{activeFilterCount > 0 && <span className="mobile-filter-count" aria-hidden="true">{activeFilterCount}</span>}</Button></SheetTrigger>
            <SheetContent side="right" className="filter-sheet" onEscapeKeyDown={(event) => {
              const dropdown = event.target instanceof HTMLElement ? event.target.closest<HTMLDetailsElement>('.filter-dropdown[open]') : null;
              if (!dropdown) return;
              event.preventDefault();
              dropdown.open = false;
              dropdown.querySelector('summary')?.focus();
            }}>
              <SheetTitle className="visually-hidden">消息筛选</SheetTitle>
              <SheetDescription className="visually-hidden">设置类别、学院、来源和日期等筛选条件。</SheetDescription>
              <FilterPanel state={state} onDone={() => setSheetOpen(false)} />
            </SheetContent>
          </Sheet>}
        </div>
      </div>
    </>
  );
}

function ActiveFilters({ state }: { state: ReaderState }) {
  const chips: { key: string; label: string; remove: () => void }[] = [];
  const { filters } = state;
  if (filters.q) chips.push({ key: 'q', label: `搜索：${filters.q}`, remove: () => state.setFilters({ q: '', page: 1 }) });
  for (const value of filters.category) chips.push({ key: `category-${value}`, label: value, remove: () => state.setFilters({ category: filters.category.filter((item) => item !== value), page: 1 }) });
  for (const value of filters.college) chips.push({ key: `college-${value}`, label: value, remove: () => state.setFilters({ college: filters.college.filter((item) => item !== value), page: 1 }) });
  for (const value of filters.source) chips.push({ key: `source-${value}`, label: value, remove: () => state.setFilters({ source: filters.source.filter((item) => item !== value), page: 1 }) });
  if (filters.issue_from) chips.push({ key: 'issue-from', label: `消息日期 ≥ ${filters.issue_from}`, remove: () => state.setFilters({ issue_from: '', page: 1 }) });
  if (filters.issue_to) chips.push({ key: 'issue-to', label: `消息日期 ≤ ${filters.issue_to}`, remove: () => state.setFilters({ issue_to: '', page: 1 }) });
  if (filters.date_from) chips.push({ key: 'date-from', label: `${dateTypeLabels[filters.date_type]} ≥ ${filters.date_from}`, remove: () => state.setFilters({ date_from: '', page: 1 }) });
  if (filters.date_to) chips.push({ key: 'date-to', label: `${dateTypeLabels[filters.date_type]} ≤ ${filters.date_to}`, remove: () => state.setFilters({ date_to: '', page: 1 }) });
  if (filters.date_status !== 'all') chips.push({ key: 'date-status', label: filters.date_status === 'known' ? '可确定日期' : '日期未明', remove: () => state.setFilters({ date_status: 'all', page: 1 }) });
  if (filters.following) chips.push({ key: 'following', label: '关注学院', remove: () => state.setFilters({ following: false, page: 1 }) });
  if (!chips.length) return null;
  return <div className="active-filters" aria-label="已启用的筛选条件"><span className="active-filter-label">当前筛选</span>{chips.map((chip) => <button type="button" className="filter-chip" key={chip.key} onClick={chip.remove}>{chip.label}<X size={13} aria-hidden="true" /><span className="visually-hidden">移除筛选</span></button>)}<button type="button" className="clear-filters" onClick={() => state.resetFilters()}>全部清除</button></div>;
}

function ReaderError({ state }: { state: ReaderState }) {
  return <main className="state-page"><div className="state-card" role="alert"><div className="state-mark state-mark--error">!</div><h1>消息加载失败</h1><p>{state.error || '公开消息数据暂时无法读取，请稍后重试。'}</p><Button type="button" onClick={state.retry}>重试</Button></div></main>;
}

function ReaderLoading() {
  return <main className="state-page"><div className="loading-card" role="status"><span className="loading-dot" /><p>正在载入南哪消息…</p></div></main>;
}

function ReaderContent({ state }: { state: ReaderState }) {
  if (state.view === 'edition' || state.view === 'issue') {
    return <main className="reader-main"><EditionPage state={state} /></main>;
  }
  if (state.view === 'item') {
    return <main className="reader-main detail-page">
      <div className="detail-column"><ItemDetail state={state} /></div>
    </main>;
  }
  if (state.view === 'issues') {
    return <main className="reader-main">
      <div className="page-heading"><div><h1>往期刊物</h1><p>按年份和月份浏览往期刊物。</p></div><Link className="publication-archive-link" to="/history">按消息检索<ArrowRight size={15} aria-hidden="true" /></Link></div>
      <IssueArchive state={state} />
    </main>;
  }
  const heading = viewHeadings[state.view] ?? viewHeadings.history;
  const activities = state.view === 'activities';
  return <main className="reader-main">
    <div className="page-heading"><div><h1>{heading.title}</h1><p>{heading.description}</p></div>{activities ? <span className="archive-range"><CalendarDays size={16} aria-hidden="true" />按北京时间 {state.today} 整理</span> : state.view === 'history' && <Link className="publication-archive-link" to="/issues">浏览往期刊物<ArrowRight size={15} aria-hidden="true" /></Link>}</div>
    {activities && <aside className="activities-notice"><CalendarClock size={18} aria-hidden="true" /><p>依据已发布消息中的日期整理，仅列入已对照原文或经人工核实、并有后续日程的记录。最新收录至 {state.issues[0]?.issue_date ?? '日期未明'}；当天活动可能已经结束，报名是否开放请查看原始通知。</p></aside>}
    <ActiveFilters state={state} />
    <div className="reading-layout">
      <aside className="desktop-filters" aria-label="筛选消息"><FilterPanel state={state} /></aside>
      <section className="results-column" aria-label={activities ? '当前活动列表' : '消息列表'}>
        <div className="list-toolbar"><ResultSummary state={state} /></div>
        <OccurrenceResults state={state} emptyTitle={activities ? '暂未找到有后续日程的活动' : undefined} emptyDescription={activities ? '可清除筛选条件，或阅读本期与历史消息。日期未明和待核对内容不会自动判为正在进行。' : undefined} />
        {activities && state.total === 0 && <Button asChild variant="outline"><Link to="/">阅读本期消息</Link></Button>}
        <Pagination state={state} />
      </section>
    </div>
  </main>;
}

export default function ReaderApp() {
  const state = useReader();
  const location = useLocation();
  useReadingPosition(state);
  const knownPath = ['/', '/activities', '/history', '/favorites', '/issues'].includes(location.pathname) || /^\/(?:issues|items)\/[^/]+$/.test(location.pathname);
  if (state.status === 'loading') return <div className="reader-app"><ReaderLoading /></div>;
  if (!knownPath) return <Navigate to="/" replace />;
  if (state.status === 'error') return <div className="reader-app"><ReaderError state={state} /></div>;
  return (
    <div className="reader-app" id="reader-top">
      <Header state={state} />
      {state.preferenceError && <div className="preference-warning" role="status"><Bookmark size={16} aria-hidden="true" /><p><strong>本地偏好暂未保存</strong><span>{state.preferenceError}</span></p></div>}
      <ReaderContent state={state} />
      <footer className="reader-footer"><div><span className="footer-seal"><Newspaper size={16} aria-hidden="true" /></span><span>南哪消息</span></div><p>校园消息网页刊 · 非官方整理 · 重要信息请以原始发布为准</p><button type="button" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>回到顶部 ↑</button></footer>
    </div>
  );
}
