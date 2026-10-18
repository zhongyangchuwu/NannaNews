import { Bookmark, BookmarkCheck, CalendarDays, ChevronLeft, ChevronRight, CircleHelp, Clock3, MapPin } from 'lucide-react';
import { Link } from 'react-router';
import type { CatalogOccurrence, ReaderState } from '../types';
import { Badge } from './badge';
import { Button } from './button';


export function ResultSummary({ state }: { state: ReaderState }) {
  const start = state.total === 0 ? 0 : (state.page - 1) * state.perPage + 1;
  const end = Math.min(state.page * state.perPage, state.total);
  return (
    <div className="result-summary" aria-live="polite">
      <p><strong>{state.total.toLocaleString('zh-CN')}</strong> 条记录 <span>·</span> {state.total ? `第 ${start}–${end} 条` : '没有匹配记录'}</p>
      {state.searching && <span className="searching-indicator"><span className="loading-dot" /> 正在检索</span>}
    </div>
  );
}

export function OccurrenceCard({ occurrence, state }: { occurrence: CatalogOccurrence; state: ReaderState }) {
  const isFavorite = state.favorites.has(occurrence.item_id);
  const resolvedDates = occurrence.dates.filter((date) => (date.resolution === 'explicit' || date.resolution === 'contextual') && date.normalized_date);
  const dateLabel = resolvedDates[0]?.normalized_date ?? (occurrence.dates.length ? '日期未明' : '未列出日期');
  return (
    <article className={state.selectedOccurrence?.id === occurrence.id ? 'occurrence-card is-current' : 'occurrence-card'}>
      <div className="occurrence-card-topline">
        <span className="occurrence-date"><CalendarDays size={14} aria-hidden="true" />{occurrence.issue_date ?? '消息日期未明'}</span>
        <button type="button" className="icon-action favorite-action" onClick={() => state.toggleFavorite(occurrence.item_id)} aria-label={isFavorite ? `取消收藏：${occurrence.title}` : `收藏：${occurrence.title}`} aria-pressed={isFavorite}>
          {isFavorite ? <BookmarkCheck size={17} aria-hidden="true" /> : <Bookmark size={17} aria-hidden="true" />}
        </button>
      </div>
      <h3><Link className="occurrence-title-link" to={state.itemHref(occurrence.item_id)} aria-current={state.selectedOccurrence?.id === occurrence.id ? 'page' : undefined}>{occurrence.title}</Link></h3>
      {occurrence.preview && <p className="occurrence-preview">{occurrence.preview}</p>}
      <div className="occurrence-meta">
        {occurrence.category && <Badge variant="accent">{occurrence.category}</Badge>}
        <Badge variant="muted">{occurrence.kind === 'summary' ? '摘要' : occurrence.kind === 'detail' ? '详情' : '通知'}</Badge>
        {!!occurrence.source_corrections?.length && <Badge variant="warning">原刊来源链接已校正</Badge>}
        {occurrence.colleges.slice(0, 3).map((college) => <span className="meta-item" key={college}><MapPin size={13} aria-hidden="true" />{college}</span>)}
        {occurrence.colleges.length > 3 && <span className="meta-item">另有 {occurrence.colleges.length - 3} 个学院</span>}
        {dateLabel !== '未列出日期' && <span className="meta-item"><Clock3 size={13} aria-hidden="true" />{dateLabel}</span>}
        {occurrence.dates.some((date) => date.resolution === 'contextual') && <span className="uncertain-meta"><CircleHelp size={13} aria-hidden="true" />含上下文推定日期</span>}
        {occurrence.dates.some((date) => date.resolution !== 'explicit' && date.resolution !== 'contextual') && <span className="uncertain-meta"><CircleHelp size={13} aria-hidden="true" />仍有未确定日期</span>}
      </div>
    </article>
  );
}

export function OccurrenceResults({ state, emptyTitle = '暂时没有符合条件的消息', emptyDescription = '可以清除部分筛选条件，或尝试其他关键词。' }: { state: ReaderState; emptyTitle?: string; emptyDescription?: string }) {
  if (state.records.length === 0) {
    const hasFavorites = state.view === 'favorites';
    return (
      <div className="empty-state">
        <div className="empty-state-mark"><CircleHelp size={22} aria-hidden="true" /></div>
        <h2>{hasFavorites ? '暂无收藏的消息' : emptyTitle}</h2>
        <p>{hasFavorites ? '在消息条目旁点选书签，即可把感兴趣的记录留在这里。' : emptyDescription}</p>
        {hasFavorites ? <Button asChild variant="outline"><Link to="/">浏览全部消息</Link></Button> : <Button type="button" variant="outline" onClick={() => state.resetFilters()}>清除筛选</Button>}
      </div>
    );
  }
  return <div className="occurrence-list">{state.records.map((record) => <OccurrenceCard key={record.id} occurrence={record} state={state} />)}</div>;
}

export function Pagination({ state }: { state: ReaderState }) {
  if (state.pages < 2) return null;
  return (
    <nav className="pagination" aria-label="结果分页">
      <Button type="button" size="sm" variant="outline" onClick={() => state.setPage(state.page - 1)} disabled={state.page <= 1}>
        <ChevronLeft size={16} aria-hidden="true" />上一页
      </Button>
      <span>第 <strong>{state.page}</strong> 页 / 共 {state.pages} 页</span>
      <Button type="button" size="sm" variant="outline" onClick={() => state.setPage(state.page + 1)} disabled={state.page >= state.pages}>
        下一页<ChevronRight size={16} aria-hidden="true" />
      </Button>
    </nav>
  );
}
