import { Bookmark, BookmarkCheck, CalendarDays, ChevronLeft, ChevronRight, CircleHelp } from 'lucide-react';
import { Link } from 'react-router';
import type { CatalogDate, CatalogOccurrence, ContentStatus, ReaderState } from '../types';
import { Badge } from './badge';
import { Button } from './button';
import { confirmedActivityDate, currentActivityDate, hasVerifiedManualReview } from '../filter';

const CONTENT_LABELS: Record<ContentStatus, string> = {
  verified: '已对照原文', human_verified: '人工核实', conflict: '原文与刊物有冲突', unconfirmed: '内容待核实',
  unreviewed: '原刊摘录', legacy: '历史记录·参考原刊', source_corrected: '来源已校正·待核验', needs_review: '日期／来源待核对',
};
const DATE_LABELS: Record<string, string> = {
  event: '活动', deadline: '截止', registration: '报名', application: '申请／申报',
};
const DATE_PRIORITY: Record<string, number> = { deadline: 0, event: 1, registration: 2, application: 3 };

function orderedActivityDates<T extends { kind: string; resolution?: string }>(dates: T[]): T[] {
  return dates.sort((left, right) => (DATE_PRIORITY[left.kind] ?? 4) - (DATE_PRIORITY[right.kind] ?? 4)
    || Number(left.resolution !== 'explicit') - Number(right.resolution !== 'explicit'));
}
export function ContentBadge({ status }: { status?: ContentStatus }) {
  const variant = status === 'verified' || status === 'human_verified' ? 'accent'
    : status && !['unreviewed', 'legacy'].includes(status) ? 'warning' : 'muted';
  return <Badge variant={variant}>{status ? CONTENT_LABELS[status] : '原刊摘录'}</Badge>;
}

function occurrenceNotices(occurrence: CatalogOccurrence, today: string, dateClaims: CatalogDate[]) {
  const dates = occurrence.content_status === 'verified' || hasVerifiedManualReview(occurrence)
    ? dateClaims.filter((date) => ['event', 'deadline', 'registration', 'application'].includes(date.kind)) : [];
  const passedDeadline = dates.some((date) => confirmedActivityDate(occurrence, date)
    && date.kind === 'deadline' && date.normalized_date! < today);
  const passedParticipationNode = dates.some((date) => confirmedActivityDate(occurrence, date)
    && ['registration', 'application'].includes(date.kind) && date.normalized_date! < today);
  return {
    warningStatus: Boolean(occurrence.content_status && !['verified', 'human_verified', 'unreviewed', 'legacy'].includes(occurrence.content_status)),
    reasons: [
      passedDeadline ? '部分已列明的截止日期已过；后续活动或提交日程不代表仍可报名。'
        : passedParticipationNode ? '已有报名／申请节点早于今天；节点不一定是截止日，是否仍可参与请核对原始通知。' : null,
      dates.some((date) => date.resolution === 'contextual') ? '含上下文推定日期，请核对原始通知。' : null,
      dates.some((date) => date.resolution !== 'explicit' && date.resolution !== 'contextual') ? '仍有未确定日期，请核对原始通知。' : null,
    ].filter((reason): reason is string => reason !== null),
  };
}

export function OccurrenceReminder({ occurrence, today, href }: { occurrence: CatalogOccurrence; today: string; href: string }) {
  const notices = occurrenceNotices(occurrence, today, occurrence.dates);
  if (!notices.warningStatus && notices.reasons.length === 0) return null;
  return <Link className="reader-badge reader-badge--warning occurrence-reminder" to={href} aria-label="查看阅读提醒及核对说明">阅读提醒<ChevronRight size={12} aria-hidden="true" /></Link>;
}

export function OccurrenceWarnings({ occurrence, today, dateClaims = occurrence.dates }: { occurrence: CatalogOccurrence; today: string; dateClaims?: CatalogDate[] }) {
  const notices = occurrenceNotices(occurrence, today,
    dateClaims === occurrence.dates ? dateClaims : [...occurrence.dates, ...dateClaims]);
  if (!notices.warningStatus && notices.reasons.length === 0) return null;
  return <div className="occurrence-warnings" aria-label="阅读提醒">
    {notices.warningStatus && <ContentBadge status={occurrence.content_status} />}
    {notices.reasons.map((reason) => <span key={reason}>{reason}</span>)}
  </div>;
}

export function OccurrenceFacts({ occurrence }: { occurrence: CatalogOccurrence }) {
  if (occurrence.content_status !== 'verified' && !hasVerifiedManualReview(occurrence)) return null;
  const facts = occurrence.activity_facts;
  const dates = orderedActivityDates(occurrence.dates.filter((date) => confirmedActivityDate(occurrence, date)));
  const fields = occurrence.category === '招聘升学宣讲'
    ? [['audience', '面向'], ['participation', '参与'], ['venue', '地点']] as const
    : [['venue', '地点'], ['audience', '面向'], ['participation', '参与']] as const;
  if (!dates.length && !facts?.time && !fields.some(([key]) => facts?.[key])) return null;
  return <dl className="card-facts">
    {dates.map((date, index) => <div key={`${date.kind}-${date.normalized_date}-${date.resolution}-${index}`}>
      <dt>{DATE_LABELS[date.kind]}</dt><dd>{date.normalized_date}{date.resolution === 'contextual' ? '（上下文推定）' : ''}</dd>
    </div>)}
    {facts?.time && <div><dt>时间</dt><dd>{facts.time}</dd></div>}
    {fields.map(([key, label]) => facts?.[key] ? <div key={key}><dt>{label}</dt><dd>{facts[key]}</dd></div> : null)}
  </dl>;
}

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
  return (
    <article data-item-id={occurrence.item_id} data-occurrence-id={occurrence.id} className={state.selectedOccurrence?.id === occurrence.id ? 'occurrence-card is-current' : 'occurrence-card'}>
      <div className="occurrence-card-topline">
        {occurrence.category && <Badge variant="muted">{occurrence.category}</Badge>}
        <button type="button" className="icon-action favorite-action" onClick={() => state.toggleFavorite(occurrence.item_id)} aria-label={isFavorite ? `取消收藏：${occurrence.title}` : `收藏：${occurrence.title}`} aria-pressed={isFavorite}>
          {isFavorite ? <BookmarkCheck size={17} aria-hidden="true" /> : <Bookmark size={17} aria-hidden="true" />}
        </button>
      </div>
      <h3><Link className="occurrence-title-link" to={state.itemHref(occurrence.item_id, occurrence.id)} aria-current={state.selectedOccurrence?.id === occurrence.id ? 'page' : undefined}>{occurrence.title}</Link></h3>
      {occurrence.preview && <p className="occurrence-preview">{occurrence.preview}</p>}
      <OccurrenceFacts occurrence={occurrence} />
      <div className="occurrence-meta">
        {occurrence.kind === 'standalone' && <span className="occurrence-date"><CalendarDays size={13} aria-hidden="true" />独立收录</span>}
        <ContentBadge status={occurrence.content_status} />
        <OccurrenceReminder occurrence={occurrence} today={state.today} href={state.itemHref(occurrence.item_id, occurrence.id)} />
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
        {hasFavorites ? <Button asChild variant="outline"><Link to="/history">浏览历史消息</Link></Button> : <Button type="button" variant="outline" onClick={() => state.resetFilters()}>清除搜索与筛选</Button>}
      </div>
    );
  }
  if (state.mode !== 'current') return <div className="occurrence-list">{state.records.map((record) => <OccurrenceCard key={record.id} occurrence={record} state={state} />)}</div>;
  const weekEnd = new Date(`${state.today}T00:00:00Z`);
  weekEnd.setUTCDate(weekEnd.getUTCDate() + (7 - weekEnd.getUTCDay()) % 7);
  const lastDay = weekEnd.toISOString().slice(0, 10);
  const groups: { title: string; description: string; records: CatalogOccurrence[] }[] = [
    { title: '今天', description: state.today, records: [] },
    { title: '本周', description: `今天之后至本周日 ${lastDay}`, records: [] },
    { title: '稍后', description: '本周之后的已列明日程', records: [] },
  ];
  for (const record of state.records) {
    const day = currentActivityDate(record, state.today)?.normalized_date;
    const index = day === state.today ? 0 : day && day <= lastDay ? 1 : 2;
    groups[index].records.push(record);
  }
  return <div className="activity-groups">
    <p className="activity-group-note">按本页结果最近一项已核实日程分组；日期按天判断，不代表此刻仍在进行或开放报名。</p>
    {groups.filter((group) => group.records.length > 0).map((group) => <section className="activity-group" key={group.title} aria-label={group.title}>
      <header className="activity-group-heading"><h2>{group.title}</h2><span>{group.description} · 本页 {group.records.length} 条</span></header>
      <div className="occurrence-list">{group.records.map((record) => <OccurrenceCard key={record.id} occurrence={record} state={state} />)}</div>
    </section>)}
  </div>;
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
