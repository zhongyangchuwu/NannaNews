import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Link, Navigate, useLocation } from 'react-router';
import { Archive, ArrowRight, Bookmark, CalendarClock, Filter, Newspaper, Search, X } from 'lucide-react';
import { useReader } from './reader-state';
import type { Filters, ReaderState, ReaderView } from './types';
import { FilterPanel } from './components/FilterPanel';
import { IssueArchive } from './components/IssueArchive';
import { ItemDetail } from './components/ItemDetail';
import { OccurrenceResults, Pagination, ResultSummary } from './components/OccurrenceList';
import { Button } from './components/button';
import { Badge } from './components/badge';
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from './components/sheet';
import { DocumentEvidence } from './components/DocumentEvidence';

type ModeLink = { mode: ReaderView; label: string; path: string; icon: typeof Newspaper };
const modeLinks: ModeLink[] = [
  { mode: 'all', label: '全部消息', path: '/', icon: Newspaper },
  { mode: 'upcoming', label: '近期事项', path: '/upcoming', icon: CalendarClock },
  { mode: 'unknown', label: '日期未明', path: '/unknown', icon: Filter },
  { mode: 'favorites', label: '我的收藏', path: '/favorites', icon: Bookmark },
];

const viewHeadings: Record<string, { eyebrow: string; title: string; description: string }> = {
  all: { eyebrow: '馆藏总目 · COLLECTION', title: '全部消息', description: '已核验的重复刊载归为一条消息；历史总目并非当前未结束的活动数。' },
  upcoming: { eyebrow: '日期索引 · CALENDAR', title: '近期事项', description: '按有依据的原文／上下文日期判断；推定依据在详情中列明。' },
  unknown: { eyebrow: '待考日期 · UNDATED', title: '日期未明', description: '无法确定活动／截止日期的记录，附缺失、长期安排或复核说明。' },
  favorites: { eyebrow: '个人书签 · BOOKMARKS', title: '我的收藏', description: '留在本设备上的阅读书签，不上传或跨设备同步。' },
  issue: { eyebrow: '期刊原件 · ORIGINALS', title: '本期消息', description: '按原始期次查阅正文、清单与留存原件。' },
};
const dateTypeLabels: Record<Filters['date_type'], string> = { event: '活动日期', deadline: '截止日期', listed: '列明日期', source: '来源日期' };

function Header({ state }: { state: ReaderState }) {
  const location = useLocation();
  const [draft, setDraft] = useState(state.filters.q);
  const composing = useRef(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  useEffect(() => setDraft(state.filters.q), [state.filters.q]);
  const activeMode = state.view === 'issues' || state.view === 'issue' ? null : state.mode;
  const filtersAvailable = location.pathname !== '/issues';
  const archiveRange = useMemo(() => {
    let first = '';
    let last = '';
    for (const issue of state.issues) {
      if (!issue.issue_date) continue;
      if (!first || issue.issue_date < first) first = issue.issue_date;
      if (!last || issue.issue_date > last) last = issue.issue_date;
    }
    return first ? `${first.replaceAll('-', '.')} — ${last.replaceAll('-', '.')}` : '期刊日期未明';
  }, [state.issues]);
  const onSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (composing.current) return;
    state.setFilters({ q: draft, page: 1 });
  };
  return (
    <>
      <header className="reader-header">
        <div className="reader-header-inner">
          <Link className="brand-lockup" to="/" aria-label="南哪消息档案首页">
            <span className="brand-seal" aria-hidden="true"><span>南</span><span>哪</span></span>
            <span className="brand-wordmark"><strong>南哪·消息档案</strong><small>校园消息历史资料库 / NANNA NEWS ARCHIVE</small></span>
          </Link>
          <div className="header-note"><span className="header-note-label">非官方历史资料 · 以原刊为据</span><span className="archive-range">原刊日期 {archiveRange}</span></div>
          <Link className="archive-shortcut" to={{ pathname: '/issues', search: location.search }} aria-current={state.view === 'issues' || state.view === 'issue' ? 'page' : undefined}><Archive size={16} aria-hidden="true" /><span>期刊目录</span><ArrowRight size={14} aria-hidden="true" /></Link>
        </div>
      </header>
      <div className="reader-toolbar-wrap">
        <div className="reader-toolbar">
          <nav className="mode-nav" aria-label="档案浏览">
            {modeLinks.map(({ mode, label, path, icon: Icon }) => <Link key={mode} to={{ pathname: path, search: location.search }} className={activeMode === mode ? 'mode-link is-active' : 'mode-link'} aria-current={activeMode === mode ? 'page' : undefined}><Icon size={16} aria-hidden="true" /><span>{label}</span>{mode === 'favorites' && state.favorites.size > 0 && <span className="mode-count">{state.favorites.size}</span>}</Link>)}
          </nav>
          {filtersAvailable && <>
            <form className="search-form" role="search" onSubmit={onSearch}>
              <Search size={18} aria-hidden="true" />
              <label className="visually-hidden" htmlFor="reader-search">搜索消息标题与原文</label>
              <input id="reader-search" type="search" value={draft} placeholder="搜索标题、正文、学院…" onChange={(event) => setDraft(event.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || composing.current)) event.preventDefault(); }} />
              {draft && <button type="button" className="search-clear" aria-label="清除搜索词" onClick={() => { setDraft(''); state.setFilters({ q: '', page: 1 }); }}><X size={15} aria-hidden="true" /></button>}
              <Button type="submit" size="sm">搜索</Button>
            </form>
            <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
              <SheetTrigger asChild><Button type="button" variant="outline" className="mobile-filter-trigger"><Filter size={16} aria-hidden="true" />筛选</Button></SheetTrigger>
              <SheetContent side="right" className="filter-sheet" onEscapeKeyDown={(event) => {
                const dropdown = event.target instanceof HTMLElement
                  ? event.target.closest<HTMLDetailsElement>('.filter-dropdown[open]') : null;
                if (!dropdown) return;
                event.preventDefault();
                dropdown.open = false;
                dropdown.querySelector('summary')?.focus();
              }}>
                <SheetTitle className="visually-hidden">消息筛选</SheetTitle>
                <SheetDescription className="visually-hidden">设置类别、学院、来源和日期等筛选条件。</SheetDescription>
                <FilterPanel state={state} onDone={() => setSheetOpen(false)} />
              </SheetContent>
            </Sheet>
          </>}
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
  return <main className="state-page"><div className="state-card" role="alert"><div className="state-mark state-mark--error">!</div><span className="eyebrow">档案暂不可用</span><h1>读取静态消息档案失败</h1><p>{state.error || '档案数据暂时无法读取，请稍后重试。'}</p><Button type="button" onClick={state.retry}>重试</Button></div></main>;
}

function ReaderLoading() {
  return <main className="state-page"><div className="loading-card" role="status"><span className="loading-dot" /><p>正在读取公开消息档案…</p></div></main>;
}

function IssuePage({ state }: { state: ReaderState }) {
  const issue = state.selectedIssue;
  const location = useLocation();
  const archiveHref = { pathname: '/issues', search: location.search };
  if (!issue) return <div className="state-page"><div className="state-card"><div className="state-mark"><Archive size={20} aria-hidden="true" /></div><span className="eyebrow">历史期次</span><h1>没有找到这个期次</h1><p>该期次可能不在当前静态档案版本中。</p><Button asChild variant="outline"><Link to={archiveHref}>返回历史期次</Link></Button></div></div>;
  return (
    <div className="issue-page">
      <div className="page-heading issue-page-heading"><div><Link className="text-back-link" to={archiveHref}><ArrowRight size={14} aria-hidden="true" />全部历史期次</Link><span className="eyebrow">期次原始记录</span><h1>{issue.title}</h1><p>{issue.issue_date ?? '期次日期未明'}{issue.issue_number !== null ? ` · 第 ${issue.issue_number} 期` : ''}</p></div><Badge variant="muted">{issue.occurrence_count} 条刊载记录</Badge></div>
      <ActiveFilters state={state} />
      <div className="issue-reading-layout">
        <aside className="desktop-filters" aria-label="筛选本期消息"><FilterPanel state={state} /></aside>
        <section className="issue-page-content" aria-label="本期消息列表">
          <IssueDocumentsInline issue={issue} state={state} />
          <ResultSummary state={state} />
          <OccurrenceResults state={state} emptyTitle="本期没有符合条件的消息" emptyDescription="可以清除部分筛选条件，或返回其他期次。" />
          <Pagination state={state} />
        </section>
      </div>
    </div>
  );
}

function IssueDocumentsInline({ issue, state }: { issue: NonNullable<ReaderState['selectedIssue']>; state: ReaderState }) {
  const pdfs = issue.documents.filter((doc) => doc.format === 'pdf' && doc.pdf_path);
  return <div className="issue-inline-documents">{issue.documents.map((document) => <span className="document-label" key={document.id}>{document.format === 'pdf' ? '原始 PDF' : '网页文档'} · {document.filename}</span>)}{pdfs.map((document) => <a className="pdf-link" key={document.id} href={state.assetUrl(document.pdf_path!)} target="_blank" rel="noopener noreferrer">查看本期原始 PDF</a>)}{!pdfs.length && <span className="pdf-unavailable">本期没有可用的原始 PDF。</span>}{issue.documents.map((document) => <DocumentEvidence key={document.id} document={document} />)}</div>;
}

function ReaderContent({ state }: { state: ReaderState }) {
  const location = useLocation();
  const isItem = /^\/items\/[^/]+$/.test(location.pathname);
  const isIssue = /^\/issues\/[^/]+$/.test(location.pathname);
  const isArchive = location.pathname === '/issues';
  const view = isIssue ? 'issue' : state.mode;
  const heading = viewHeadings[view] ?? viewHeadings.all;
  const desktopDetail = isItem;
  return (
    <main className={`reader-main${desktopDetail ? ' reader-main--detail-open' : ''}`}>
      {isArchive ? <>
        <div className="page-heading"><div><span className="eyebrow">期刊编目 · PERIODICALS</span><h1>期刊目录</h1><p>依年月编排，逐期查阅消息与留存原件。</p></div><Badge variant="outline">共 {state.issues.length} 期</Badge></div>
        <IssueArchive state={state} />
      </> : isIssue ? <IssuePage state={state} /> : (
        <>
          {!isItem && <div className="page-heading"><div><span className="eyebrow">{heading.eyebrow}</span><h1>{heading.title}</h1><p>{heading.description}</p></div><div className="heading-aside"><span className="archive-edition">公开资料 · 数字存档</span>{state.manifest && <span className="data-version">存档版本 {state.manifest.data_version.slice(0, 8)}</span>}</div></div>}
          <ActiveFilters state={state} />
          <div className={desktopDetail ? 'reading-layout reading-layout--detail' : 'reading-layout'}>
            <aside className="desktop-filters" aria-label="筛选消息"><FilterPanel state={state} /></aside>
            <section className="results-column" aria-label="消息列表">
              <div className="list-toolbar"><ResultSummary state={state} />{isItem && <span className="detail-current-label">正在阅读档案</span>}</div>
              <OccurrenceResults state={state} />
              <Pagination state={state} />
            </section>
            {desktopDetail && <aside className="detail-column" aria-label="消息详情"><ItemDetail state={state} /></aside>}
          </div>
        </>
      )}
    </main>
  );
}

export default function ReaderApp() {
  const state = useReader();
  const location = useLocation();
  const knownPath = location.pathname === '/' || location.pathname === '/upcoming' || location.pathname === '/unknown' || location.pathname === '/favorites' || location.pathname === '/issues' || /^\/(?:issues|items)\/[^/]+$/.test(location.pathname);
  if (state.status === 'loading') return <div className="reader-app"><ReaderLoading /></div>;
  if (state.status === 'error') return <div className="reader-app"><ReaderError state={state} /></div>;
  if (!knownPath) return <Navigate to={state.listHref} replace />;
  return (
    <div className="reader-app">
      <Header state={state} />
      {state.preferenceError && <div className="preference-warning" role="status"><Bookmark size={16} aria-hidden="true" /><p><strong>本地偏好暂未保存</strong><span>{state.preferenceError}</span></p></div>}
      <ReaderContent state={state} />
      <footer className="reader-footer"><div><span className="footer-seal">南</span><span>南哪·消息档案</span></div><p>历史资料整理展示 · 非官方档案 · 以来源原文为准</p><a href="#reader-top" onClick={(event) => { event.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }); }}>回到顶部 ↑</a></footer>
    </div>
  );
}
