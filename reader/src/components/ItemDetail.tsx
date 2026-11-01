import { ArrowLeft, ArrowUpRight, Bookmark, BookmarkCheck, CalendarDays, ChevronDown, CircleHelp, Download, ExternalLink, FileText, ImageOff, MapPin, Paperclip } from 'lucide-react';
import { Link } from 'react-router';
import { readablePdfText } from '../reading-text';
import type { ActivityCitation, ActivityField, ActivityReview, ManualActivity, PublicCollege, PublicDate, PublicDocument, PublicOccurrence, ReaderState } from '../types';
import { Badge } from './badge';
import { Button } from './button';
import { DocumentEvidence } from './DocumentEvidence';
import { ContentBadge, OccurrenceWarnings } from './OccurrenceList';
import { ShareButton } from './ShareButton';
import { itemSharePath } from '../sharing';
import { participationUrl } from '../participation';
import { confirmedActivityDate, hasVerifiedManualReview, manualPublicUrl } from '../filter';
import { originalSourceUrl, sourceUrls } from '../source-links.mjs';
const safeHttpUrl = originalSourceUrl;


function DateEvidence({ dates, emptyText, expandEvidence = false, referenceOnly = false }: { dates: PublicDate[]; emptyText: string; expandEvidence?: boolean; referenceOnly?: boolean }) {
  if (dates.length === 0) return <p className="detail-empty-note">{emptyText}</p>;
  return <div className="date-evidence-list">
    {dates.map((date, index) => {
      const known = (date.resolution === 'explicit' || date.resolution === 'contextual') && date.normalized_date;
      const dateType = date.kind === 'event' ? '活动' : date.kind === 'deadline' ? '截止' : date.kind === 'listed' ? '刊载' : date.kind === 'source' ? '来源' : date.kind === 'registration' ? '报名' : date.kind === 'application' ? '申请／申报' : date.kind;
      return <div className="date-evidence" key={`${date.kind}-${date.raw_text}-${index}`}>
        <span className="date-evidence-type">{dateType}</span>
        <div>
          <strong>{expandEvidence && known ? date.normalized_date : date.raw_text || (known ? date.normalized_date : '原文日期未明')}</strong>
          {date.literal_source === 'primary' && <Badge variant={referenceOnly ? 'muted' : 'accent'}>{referenceOnly ? '历史原文标记（待核对）' : '引句来自原文'}</Badge>}
          {date.literal_source === 'bulletin' && <Badge variant="muted">刊物引句</Badge>}
          {!expandEvidence && known && date.raw_text !== date.normalized_date && <span className="date-normalized">{date.resolution === 'contextual' ? '上下文推定' : '日期规范化'}：{date.normalized_date}</span>}
          {date.resolution === 'contextual' && <Badge variant="warning">上下文推定</Badge>}
          {!known && <Badge variant="warning">{date.resolution === 'year_unknown' ? '年份未明' : date.resolution === 'ambiguous' ? '表述有歧义' : '日期未能确认'}</Badge>}
          {date.evidence && (expandEvidence
            ? <details className="date-claim-evidence"><summary>日期核查依据</summary><p>原始日期引句：{date.raw_text}</p><p>{date.evidence}</p></details>
            : <p>依据说明：{date.evidence}</p>)}
        </div>
      </div>;
    })}
  </div>;
}

const ACTIVITY_DATE_KINDS: Record<string, true> = { event: true, deadline: true, registration: true, application: true };
const ACTIVITY_DATE_LABELS: Record<string, string> = { event: '活动日期', deadline: '截止日期', registration: '报名日期', application: '申请／申报日期' };
const DATE_PRIORITY: Record<string, number> = { deadline: 0, event: 1, registration: 2, application: 3 };

function orderedActivityDates<T extends { kind: string; resolution?: string }>(dates: T[]): T[] {
  return dates.sort((left, right) => (DATE_PRIORITY[left.kind] ?? 4) - (DATE_PRIORITY[right.kind] ?? 4)
    || Number(left.resolution !== 'explicit') - Number(right.resolution !== 'explicit'));
}

interface DateClaimGroups { primary: PublicDate[]; contextualBulletin: PublicDate[]; reference: PublicDate[] }

function groupDateClaims(claims: PublicDate[], occurrence: PublicOccurrence, factsOnly: boolean): DateClaimGroups {
  const groups: DateClaimGroups = { primary: [], contextualBulletin: [], reference: [] };
  const replaced = new Set((occurrence.source_corrections ?? []).map((correction) => correction.original_url));
  const sources = (occurrence.date_review?.source_review.sources ?? []).filter((source) =>
    !replaced.has(source.url) && originalSourceUrl(source.url) && originalSourceUrl(source.resolved_url));
  for (const claim of claims) {
    const supported = factsOnly || Boolean(claim.raw_text && sources.some((source) => source.quotes.some((quote) => quote.includes(claim.raw_text))));
    if (claim.literal_source === 'primary' && ACTIVITY_DATE_KINDS[claim.kind] && supported) groups.primary.push(claim);
    else if (claim.literal_source === 'bulletin' && claim.resolution === 'contextual' && ACTIVITY_DATE_KINDS[claim.kind]) groups.contextualBulletin.push(claim);
    else groups.reference.push(claim);
  }
  return groups;
}

function currentActivity(occurrence: PublicOccurrence, state: ReaderState): ActivityReview | null {
  if (occurrence.content_status !== 'verified') return null;
  const review = occurrence.date_review;
  const version = state.manifest?.content_rules_version;
  if (!review || !version || review.rules_version !== version || review.source_review.status !== 'source_verified') return null;
  const replaced = new Set((occurrence.source_corrections ?? []).map((correction) => correction.original_url));
  if (review.source_review.sources.some((source) => replaced.has(source.url))) return null;
  const activity = review.source_review.activity;
  const sources = review.source_review.sources;
  const blockedUrls = new Set(sources.filter((source) => !originalSourceUrl(source.url) || !originalSourceUrl(source.resolved_url)).map((source) => source.url));
  if (sources.length > 0 && sources.every((source) => blockedUrls.has(source.url))) return null;
  if (activity) {
    const citations = [activity.title, activity.summary, activity.content, activity.time, activity.venue, activity.organizer, activity.audience, activity.participation]
      .flatMap((field) => field.evidence).concat(activity.colleges.map((college) => college.evidence));
    if (citations.some((citation) => !originalSourceUrl(citation.url) || blockedUrls.has(citation.url))) return null;
  }
  return activity ?? null;
}

function ActivityCitationEvidence({ citation, blockedUrls }: { citation: ActivityCitation; blockedUrls?: Set<string> }) {
  const href = blockedUrls?.has(citation.url) ? null : safeHttpUrl(citation.url);
  return <div className="activity-citation">
    <blockquote>{citation.text}</blockquote>
    {href && <a href={href} target="_blank" rel="noopener noreferrer">{citation.url}<ExternalLink size={14} aria-hidden="true" /></a>}
  </div>;
}

function ActivityFieldEvidence({ field, label, blockedUrls }: { field: ActivityField; label: string; blockedUrls?: Set<string> }) {
  if (field.evidence.length === 0) return null;
  return <details className="activity-field-evidence">
    <summary>{label}来源依据（{field.evidence.length} 条）</summary>
    {field.evidence.map((citation, index) => <ActivityCitationEvidence key={`${citation.url}-${citation.image_index}-${index}`} citation={citation} blockedUrls={blockedUrls} />)}
  </details>;
}

function ActivityTextField({ field, label }: { field: ActivityField; label: string }) {
  return <section className="activity-copy">
    <h3>{label}</h3>
    <p>{field.value ?? '尚未从已核验来源确认此项。'}</p>
  </section>;
}

const ACTIVITY_FACT_FIELDS = [
  ['time', '活动时间'], ['venue', '地点'], ['organizer', '组织方'], ['audience', '面向对象'], ['participation', '参与方式'],
] as const;

function ActivityFacts({ activity, category, dates }: { activity: ActivityReview; category: string | null; dates: PublicDate[] }) {
  const schedule = orderedActivityDates(dates.filter((date) => date.literal_source === 'primary'
    && ACTIVITY_DATE_KINDS[date.kind] && date.normalized_date
    && (date.resolution === 'explicit' || date.resolution === 'contextual')));
  const fields = category === '招聘升学宣讲'
    ? [ACTIVITY_FACT_FIELDS[0], ACTIVITY_FACT_FIELDS[3], ACTIVITY_FACT_FIELDS[4], ACTIVITY_FACT_FIELDS[1], ACTIVITY_FACT_FIELDS[2]]
    : ACTIVITY_FACT_FIELDS;
  return <dl className="activity-facts">
    {schedule.map((date, index) => <div className="activity-fact" key={`${date.kind}/${date.normalized_date}/${date.resolution}/${index}`}>
      <dt>{ACTIVITY_DATE_LABELS[date.kind]}</dt><dd>{date.normalized_date}{date.resolution === 'contextual' && '（上下文推定）'}</dd>
    </div>)}
    {fields.map(([key, label]) => <div className="activity-fact" key={key}>
      <dt>{label}</dt>
      <dd>{activity[key].value ?? <span className="activity-unknown">尚未确认</span>}</dd>
    </div>)}
  </dl>;
}

function ManualConfirmation({ occurrence }: { occurrence: PublicOccurrence }) {
  const review = occurrence.manual_review;
  if (!review) return null;
  const verified = hasVerifiedManualReview(occurrence);
  return <aside className={`content-confirmation${verified ? ' is-confirmed' : ''}`} role="status">
    <div className="content-confirmation-heading"><Badge variant={verified ? 'accent' : 'warning'}>{verified ? '人工核实' : '人工记录·待核对'}</Badge>
      <time dateTime={review.reviewed_at}>{review.reviewed_at}</time>
    </div>
    <p>{review.reason}</p>
    {!verified && <p>以下是尚待核对的人工修改，不作为已确认活动或参与资格；不会进入当前活动日程。</p>}
  </aside>;
}

function PublicFactsNotice() {
  return <aside className="content-confirmation" role="status">
    <p>状态标签沿用已发布的公开事实；本精简版未纳入来源核验材料，不对未导出的证据作判断。</p>
  </aside>;
}

function ManualFacts({ activity, verified }: { activity: ManualActivity; verified: boolean }) {
  return <section className="manual-facts">
    {!verified && <p className="reference-note">人工填写的日程与信息（待核对）</p>}
    <dl className="activity-facts">
      {orderedActivityDates([...activity.dates]).map((date) => <div className="activity-fact" key={`${date.kind}/${date.date}`}><dt>{ACTIVITY_DATE_LABELS[date.kind]}</dt><dd>{date.date}{!verified && '（待核对）'}</dd></div>)}
      {ACTIVITY_FACT_FIELDS.map(([key, label]) => <div className="activity-fact" key={key}><dt>{label}</dt><dd>{activity[key] ?? <span className="activity-unknown">尚未确认</span>}</dd></div>)}
    </dl>
  </section>;
}

function ManualActivityPanel({ occurrence }: { occurrence: PublicOccurrence }) {
  const review = occurrence.manual_review;
  const activity = review?.activity;
  if (!review || !activity) return null;
  const verified = hasVerifiedManualReview(occurrence);
  const links = sourceUrls(activity.source_urls).filter((url) => manualPublicUrl(url));
  return <section className="detail-section manual-activity">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div><h2>{verified ? '人工核实内容' : '人工填写内容（待核对）'}</h2><p>{occurrence.kind === 'standalone' ? '此活动由人工登记并明确发布，不属于原刊期次，也不新增来源证据。' : '人工修改独立于下方留存的 AI 与原刊记录，不新增 AI 来源证明。'}</p></div></div>
    <div className="original-text">{activity.content}</div>
    <section className="reference-block manual-source-links"><h3>{verified ? '人工确认的来源链接' : '人工填写的来源链接（待核对）'}</h3>
      {links.length ? <ul className="source-link-list">{links.map((href) => <li key={href}><a href={href} target="_blank" rel="noopener noreferrer"><span>{href}</span><ExternalLink size={14} aria-hidden="true" /></a></li>)}</ul> : <p className="detail-empty-note">原始来源链接未提供。</p>}
    </section>
  </section>;
}

function PublicFactsPanel({ occurrence }: { occurrence: PublicOccurrence }) {
  const labels = { time: '活动时间', venue: '地点', audience: '面向对象', participation: '参与方式' } as const;
  const dates = orderedActivityDates(occurrence.dates.filter((date) => ACTIVITY_DATE_KINDS[date.kind]
    && confirmedActivityDate(occurrence, date)));
  const links = sourceUrls(occurrence.source_urls).filter((url) => manualPublicUrl(url));
  return <section className="detail-section manual-activity">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div>
      <h2>{occurrence.kind === 'standalone' ? '独立收录的活动基本信息' : '活动公开基本信息'}</h2>
      <p>此精简事实版仅包含已发布的基本字段；原刊正文、附件和模型记录未纳入本构建。下列来源链接为已公开链接。</p>
    </div></div>
    <dl className="activity-facts">
      {dates.map((date, index) => <div className="activity-fact" key={`${date.kind}-${date.normalized_date}-${date.resolution}-${index}`}>
        <dt>{ACTIVITY_DATE_LABELS[date.kind]}</dt><dd>{date.normalized_date}{date.resolution === 'contextual' && '（上下文推定）'}</dd>
      </div>)}
      {occurrence.activity_facts && (Object.keys(labels) as (keyof typeof labels)[]).map((key) => <div className="activity-fact" key={key}>
        <dt>{labels[key]}</dt><dd>{occurrence.activity_facts?.[key] ?? <span className="activity-unknown">尚未确认</span>}</dd>
      </div>)}
    </dl>
    <section className="reference-block manual-source-links"><h3>公开来源链接</h3>
      {links.length ? <ul className="source-link-list">{links.map((href) => <li key={href}><a href={href} target="_blank" rel="noopener noreferrer"><span>{href}</span><ExternalLink size={14} aria-hidden="true" /></a></li>)}</ul> : <p className="detail-empty-note">原始来源链接未提供。</p>}
    </section>
  </section>;
}

function StandaloneReference({ occurrence, referenceClaims }: { occurrence: PublicOccurrence; referenceClaims: PublicDate[] }) {
  const referenceDates = occurrence.original_dates ?? occurrence.dates;
  return <section className="detail-section reference-section">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div>
      <h2>独立收录</h2><p>此活动没有对应的原刊期次或原始归档文档；不以登记时间代替刊期。</p>
    </div></div>
    {!occurrence.manual_review?.activity && <p>公开来源链接见上方活动基本信息。</p>}
    {referenceDates.length > 0 && <section className="reference-block">
      <h3>独立收录的日期记录（参考）</h3>
      <DateEvidence dates={referenceDates} emptyText="没有留存可识别的日期记录。" />
    </section>}
    {referenceClaims.length > 0 && <section className="reference-block">
      <h3>AI 日期判断（仅参考，不代表原始来源已核实）</h3>
      <DateEvidence dates={referenceClaims} emptyText="没有其他可供参考的日期判断。" expandEvidence referenceOnly />
    </section>}
  </section>;
}


function manualParticipationUrl(activity: ManualActivity | undefined, blockedUrls: Set<string>): string | null {
  if (!activity?.participation) return null;
  for (const match of activity.participation.matchAll(/https?:\/\/[^\s<>"）)」』，。；、]+/gi)) {
    const href = manualPublicUrl(match[0]);
    if (href && originalSourceUrl(href) && !blockedUrls.has(match[0]) && !blockedUrls.has(href)) return href;
  }
  return null;
}

function primaryParticipationUrl(field: ActivityField | undefined, blockedUrls: Set<string>): string | null {
  if (!field?.value) return null;
  const value = field.value.replace(/https?:\/\/[^\s<>"）)」』，。；、]+/gi,
    (url) => originalSourceUrl(url) && !blockedUrls.has(url) && !blockedUrls.has(originalSourceUrl(url)!) ? url : '');
  return participationUrl({ ...field, value });
}

function SourceLinks({ occurrence, state }: { occurrence: PublicOccurrence; state: ReaderState }) {
  const review = occurrence.date_review?.source_review;
  const corrections = occurrence.source_corrections ?? [];
  const replaced = new Set(corrections.map((correction) => correction.original_url));
  const staleReview = review?.sources.some((source) => replaced.has(source.url));
  const currentRules = Boolean(occurrence.date_review && state.manifest?.content_rules_version
    && occurrence.date_review.rules_version === state.manifest.content_rules_version);
  const legacyReview = occurrence.date_review && !currentRules;
  const labels = {
    source_verified: 'AI 已对照原始来源', source_conflict: '原始来源与刊物有冲突',
    source_unavailable: '原始来源未能读取', source_mismatch: '链接未返回对应原文',
    secondary_only: '尚未完成当前规则下的 AI 原文核验',
  };
  const blockedRedirects = new Set((review?.sources ?? []).filter((source) => !originalSourceUrl(source.resolved_url)).map((source) => source.url));
  const links = [...new Set([...occurrence.source_urls, ...(review?.sources.map((source) => source.url) ?? []), ...corrections.map((correction) => correction.corrected_url)])]
    .filter((url) => !replaced.has(url) && !blockedRedirects.has(url)).map((raw) => ({ raw, href: safeHttpUrl(raw) })).filter((entry) => entry.href);
  return <>
    {corrections.length > 0 && <div className="document-evidence source-corrections" aria-label="来源链接纠正记录">
      <Badge variant="accent">已人工校正来源</Badge>
      {corrections.map((correction) => {
        const correctedHref = blockedRedirects.has(correction.corrected_url) ? null : safeHttpUrl(correction.corrected_url);
        return <details key={`${correction.original_url}-${correction.corrected_url}`}>
          <summary>校正说明与原刊链接</summary>
          <p>{correction.reason}</p>
          <p>原刊链接：<code>{correction.original_url}</code></p>
          <p>已校正到：{correctedHref ? <a href={correctedHref} target="_blank" rel="noopener noreferrer">{correction.corrected_url}</a> : <code>{correction.corrected_url}</code>} · {correction.title} · 读取于 {correction.fetched_at}</p>
          <p>证据快照 <code title={correction.sha256}>{correction.sha256.slice(0, 12)}</code></p>
        </details>;
      })}
    </div>}
    <div className="document-evidence" aria-label="原始来源核验状态">
      {occurrence.content_status === 'needs_review' ? <p>同一活动仍有待核对记录；以下来源快照不代表日期或来源冲突已经解决。</p>
        : staleReview ? <p>来源链接已人工校正；旧来源核验不代表新链接、日期或活动内容已核实。</p>
        : legacyReview ? <Badge variant="muted">历史留存来源</Badge>
        : review?.status === 'source_verified' && (!review.sources.some((source) => originalSourceUrl(source.url) && originalSourceUrl(source.resolved_url)) || Boolean(review.activity && !currentActivity(occurrence, state))) ? <p>留存核验记录不能在此确认原始来源；请查阅保留的核查记录。</p>
        : review ? <><Badge variant={review.status === 'source_verified' ? 'accent' : 'warning'}>{labels[review.status]}</Badge>
        {review.reason !== occurrence.date_review?.reason && <p>{review.reason}</p>}</>
        : <p>{corrections.length ? '尚未完成当前规则下的 AI 原文与日期核查；人工校正链接不代表日期或活动内容已核实。' : '尚未进行 AI 来源核验；以下链接由刊物留存，不能据此视为原文已核实。'}</p>}
    </div>
    {links.length ? <ul className="source-link-list">{links.map(({ raw, href }) => {
      const captured = !staleReview && !legacyReview ? review?.sources.find((source) => source.url === raw) : undefined;
      return <li key={raw}><a href={href!} target="_blank" rel="noopener noreferrer"><span>{raw}</span><ExternalLink size={14} aria-hidden="true" /></a>
        {captured && <div className="document-evidence"><p>{captured.title || '已读取的公开页面'} · {captured.published_date ? `原文发布：${captured.published_date}` : '原文发布时间未提供'}</p>
          <p>读取于 {captured.fetched_at} · 快照 <code title={captured.sha256}>{captured.sha256.slice(0, 12)}</code></p>
          {captured.resolved_url !== raw && safeHttpUrl(captured.resolved_url) && <p><a href={captured.resolved_url} target="_blank" rel="noopener noreferrer">查看实际返回页面<ExternalLink size={14} aria-hidden="true" /></a></p>}
        </div>}
      </li>;
    })}</ul> : <p className="detail-empty-note">原始来源链接未提供。</p>}
  </>;
}

function sourceDocument(occurrence: PublicOccurrence, state: ReaderState): PublicDocument | null {
  const issue = state.issues.find((entry) => entry.id === occurrence.issue_id);
  return issue?.documents.find((document) => document.id === occurrence.document_id) ?? null;
}

function IssueOriginal({ occurrence, state }: { occurrence: PublicOccurrence; state: ReaderState }) {
  const issue = state.issues.find((entry) => entry.id === occurrence.issue_id);
  const original = issue?.documents.find((document) => document.id === occurrence.document_id) ?? null;
  const unavailableMessage = original?.format === 'pdf'
    ? '该期原始 PDF 当前不可用。'
    : !issue
      ? '无法确认这条消息对应的期次原件。'
      : issue.documents.length === 0
        ? '该来源期次未提供原始文件。'
        : '未找到与这条消息匹配的原始文件。';
  return <section className="reference-block original-document-reference">
    <div className="section-heading"><span className="section-icon"><Paperclip size={17} aria-hidden="true" /></span><div><h3>本条所在期次原件</h3><p>仅显示与这条消息来源文档匹配的历史原件。</p></div></div>
    {original?.format === 'pdf' && original.pdf_path
      ? <div className="pdf-downloads"><a className="pdf-download" href={state.assetUrl(original.pdf_path)} target="_blank" rel="noopener noreferrer"><Download size={16} aria-hidden="true" /><span><strong>打开历史原始 PDF</strong><small>{original.filename} · {original.issue_date ?? original.claimed_issue_date ?? '期次日期未明'}</small></span><ArrowUpRight size={16} aria-hidden="true" /></a></div>
      : original?.format === 'html'
        ? <p className="original-document-note"><FileText size={16} aria-hidden="true" />网页原件（HTML） · {original.filename} · {original.issue_date ?? original.claimed_issue_date ?? '期次日期未明'}</p>
        : <p className="original-document-note"><FileText size={16} aria-hidden="true" />{unavailableMessage}</p>}
    {original && <DocumentEvidence document={original} />}
  </section>;
}


function OccurrenceSwitcher({ occurrence, state }: { occurrence: PublicOccurrence; state: ReaderState }) {
  const alternatives = state.selectedItem?.occurrences ?? [];
  if (alternatives.length <= 1) return null;
  return (
    <label className="occurrence-switcher">
      <span>本条消息的刊载与详情记录</span>
      <span className="select-with-icon"><select value={occurrence.id} onChange={(event) => state.selectOccurrence(event.target.value)} aria-label="选择本条消息的期次记录">
        {alternatives.map((entry) => <option value={entry.id} key={entry.id}>{entry.issue_date ?? '期次日期未明'} · {entry.title} · {entry.kind === 'summary' ? '摘要' : entry.kind === 'detail' ? '详情' : '通知'}</option>)}
      </select><ChevronDown size={15} aria-hidden="true" /></span>
    </label>
  );
}

function CollegeMentions({ occurrence, state, readOnly = false }: { occurrence: PublicOccurrence; state: ReaderState; readOnly?: boolean }) {
  if (occurrence.college_mentions.length === 0) return <p className="detail-empty-note">原文中没有明确提及可识别的学院。</p>;
  const colleges = new Map<string, PublicCollege[]>();
  for (const mention of occurrence.college_mentions) {
    const group = colleges.get(mention.name) ?? [];
    if (!group.some((entry) => entry.relation === mention.relation && entry.evidence === mention.evidence)) group.push(mention);
    colleges.set(mention.name, group);
  }
  return <div className="college-mention-list">
    {[...colleges].map(([name, evidence]) => {
      const followed = !readOnly && state.followedColleges.has(name);
      const relations = [...new Set(evidence.map((mention) => mention.relation))];
      return <article className="college-mention" key={name}>
        <div className="college-mention-heading"><MapPin size={15} aria-hidden="true" /><strong>{name}</strong>
          {relations.map((relation) => <Badge key={relation} variant={relation === 'audience' ? 'accent' : 'muted'}>{relation === 'audience' ? '面向对象' : '原文提及'}</Badge>)}
          {!readOnly && <button type="button" className="follow-button" onClick={() => state.toggleFollowedCollege(name)} aria-pressed={followed}>{followed ? '已关注' : '关注学院'}</button>}
        </div>
        {evidence.some((mention) => mention.evidence.length > 0) && <details className="college-evidence">
          <summary>原文依据（{evidence.filter((mention) => mention.evidence.length > 0).length} 条）</summary>
          {evidence.filter((mention) => mention.evidence.length > 0).map((mention, index) => <div className="college-reference-evidence" key={`${mention.relation}-${index}`}>
            <Badge variant={mention.relation === 'audience' ? 'accent' : 'muted'}>{mention.relation === 'audience' ? '面向对象' : '原文提及'}</Badge>
            <p>{mention.evidence}</p>
          </div>)}
        </details>}
      </article>;
    })}
  </div>;
}

function ContentConfirmation({ occurrence, state, activity }: { occurrence: PublicOccurrence; state: ReaderState; activity: ActivityReview | null }) {
  const review = occurrence.date_review;
  const sourceReview = review?.source_review;
  const version = state.manifest?.content_rules_version;
  const currentRules = Boolean(review && version && review.rules_version === version);
  const replaced = new Set((occurrence.source_corrections ?? []).map((correction) => correction.original_url));
  const staleReview = sourceReview?.sources.some((source) => replaced.has(source.url));
  let message: string;
  if (occurrence.content_status === 'needs_review') message = '同一活动的日期或来源仍待核对；当前保留原刊和核查记录作为参考，不作为已确认活动展示。';
  else if (!review) message = '尚无当前规则下的来源核验记录；原刊文字仅作为参考。';
  else if (staleReview) message = '来源已人工校正；旧来源核验不代表新链接、日期或活动内容已核实。';
  else if (!currentRules) message = '此为历史核查记录，不构成当前规则下的活动内容确认；旧日期仅在下方作为参考。';
  else if (sourceReview?.status === 'source_verified' && sourceReview.sources.some((source) => !originalSourceUrl(source.url) || !originalSourceUrl(source.resolved_url)) && !activity) message = '留存核验记录包含刊物归档；原始来源及其活动内容仍需核对。';
  else if (sourceReview?.status === 'source_verified' && activity) message = '以下为 AI 根据已读取的原始来源整理的信息，不等于人工审核结论。';
  else if (sourceReview?.status === 'source_verified') message = '原始来源有核验记录，但未提供当前规则下的完整活动内容；旧日期仅作参考。';
  else if (sourceReview?.status === 'source_conflict') message = '原始来源与刊物存在冲突，活动内容未确认。';
  else if (sourceReview?.status === 'source_unavailable') message = '原始来源未能读取，活动内容未确认。';
  else if (sourceReview?.status === 'source_mismatch') message = '来源链接未返回对应原文，活动内容未确认。';
  else if (sourceReview?.status === 'secondary_only') message = '只有二手来源，活动内容未确认。';
  else message = '活动内容尚未由当前规则下的原始来源确认。';
  const reasons = [...new Set([review?.reason, sourceReview?.reason].filter((reason): reason is string => Boolean(reason)))];
  return <aside className={`content-confirmation${activity ? ' is-confirmed' : ''}`} role="status">
    <div className="content-confirmation-heading">
      <Badge variant={activity ? 'accent' : 'warning'}>{activity ? 'AI 已对照原文' : '活动内容尚未确认'}</Badge>
    </div>
    <p>{message}</p>
    {reasons.length > 0 && <details><summary>查看来源核验说明</summary>{reasons.map((reason, index) => <p key={`${reason}-${index}`}>{reason}</p>)}</details>}
  </aside>;
}

function ActivityDates({ primaryDates, contextualDates }: { primaryDates: PublicDate[]; contextualDates: PublicDate[] }) {
  return <section className="activity-date-section">
    <h3><CalendarDays size={16} aria-hidden="true" />AI 日期判断（原始来源字面对应）</h3>
    <p className="reference-note">“原始来源字面”仅表示相同日期文字出现在已核验的原始来源；活动、截止等类别仍是 AI 判断。</p>
    <DateEvidence dates={primaryDates} emptyText="尚未从已核验的原始来源确认活动日期。" expandEvidence />
    {contextualDates.length > 0 && <div className="activity-contextual-dates">
      <h4>AI 上下文推定（刊物引句）</h4>
      <p>引句取自刊物，规范化日期由 AI 结合上下文推定；展开可查看所用原文或年份依据。</p>
      <DateEvidence dates={contextualDates} emptyText="没有可展示的上下文日期判断。" expandEvidence />
    </div>}
  </section>;
}

function ActivityColleges({ colleges, state }: { colleges: ActivityReview['colleges']; state: ReaderState }) {
  if (colleges.length === 0) return null;
  const groups = new Map<string, ActivityReview['colleges']>();
  for (const college of colleges) {
    const group = groups.get(college.name) ?? [];
    group.push(college);
    groups.set(college.name, group);
  }
  return <section className="activity-colleges">
    <h3>相关学院与面向对象</h3>
    <div className="college-mention-list">
      {[...groups].map(([name, entries]) => {
        const relations = [...new Set(entries.map((entry) => entry.relation))];
        const followed = state.followedColleges.has(name);
        return <article className="college-mention" key={name}>
          <div className="college-mention-heading"><MapPin size={15} aria-hidden="true" /><strong>{name}</strong>
            {relations.map((relation) => <Badge key={relation} variant={relation === 'audience' ? 'accent' : 'muted'}>{relation === 'audience' ? '面向对象' : '关联学院'}</Badge>)}
            <button type="button" className="follow-button" onClick={() => state.toggleFollowedCollege(name)} aria-pressed={followed}>{followed ? '已关注' : '关注学院'}</button>
          </div>
          <details className="college-evidence activity-college-evidence">
            <summary>原始来源依据（{entries.length} 条）</summary>
            {entries.map((entry, index) => <div key={`${entry.relation}-${entry.evidence.url}-${index}`}>
              <Badge variant={entry.relation === 'audience' ? 'accent' : 'muted'}>{entry.relation === 'audience' ? '面向对象' : '关联学院'}</Badge>
              <ActivityCitationEvidence citation={entry.evidence} />
            </div>)}
          </details>
        </article>;
      })}
    </div>
  </section>;
}

function PrimaryActivityImages({ images, state }: { images: PublicOccurrence['primary_images']; state: ReaderState }) {
  if (!images || images.length === 0) return null;
  return <section className="primary-activity-images">
    <h3>原始来源活动图片</h3>
    <div className="image-gallery">{images.map((image) => {
      const sourceHref = image.source_url ? safeHttpUrl(image.source_url) : null;
      return <figure className="primary-activity-image" key={image.id}>
        <a href={state.assetUrl(image.path)} target="_blank" rel="noopener noreferrer" aria-label="打开原文图片安全预览"><img src={state.assetUrl(image.path)} alt={image.caption || '原始活动图片安全预览'} loading="lazy" /></a>
        <figcaption>{image.caption || '原文配图安全预览'} · 本地安全预览
          {sourceHref && <> · <a href={sourceHref} target="_blank" rel="noopener noreferrer">查看公开原图来源</a></>}
        </figcaption>
      </figure>;
    })}</div>
  </section>;
}

function ActivityReviewPanel({ occurrence, state, activity, dateClaims }: { occurrence: PublicOccurrence; state: ReaderState; activity: ActivityReview; dateClaims: DateClaimGroups }) {
  return <section className="detail-section activity-review">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div><h2>消息内容</h2><p>根据已读取的原始发布整理，完整内容如下。</p></div></div>
    {activity.content.value ? <ActivityTextField field={activity.content} label="完整内容" /> : <OriginalExcerpt occurrence={occurrence} state={state} />}
    <PrimaryActivityImages images={activity.image_indices.length > 0 ? occurrence.primary_images : undefined} state={state} />
    <details className="activity-date-evidence"><summary>来源引用与日期核查依据</summary>
      <ActivityFieldEvidence field={activity.title} label="标题" />
      <ActivityFieldEvidence field={activity.summary} label="摘要" />
      <ActivityFieldEvidence field={activity.content} label="完整内容" />
      {ACTIVITY_FACT_FIELDS.map(([key, label]) => <ActivityFieldEvidence key={key} field={activity[key]} label={label} />)}
      <ActivityDates primaryDates={dateClaims.primary} contextualDates={dateClaims.contextualBulletin} />
      <ActivityColleges colleges={activity.colleges} state={state} />
      <ContentConfirmation occurrence={occurrence} state={state} activity={activity} />
    </details>
  </section>;
}

function OriginalExcerpt({ occurrence, state }: { occurrence: PublicOccurrence; state: ReaderState }) {
  const isPdf = sourceDocument(occurrence, state)?.format === 'pdf';
  const printedUrls = [...new Set([...occurrence.source_urls, ...(occurrence.source_corrections ?? []).map((correction) => correction.original_url)])];
  const readableText = isPdf ? readablePdfText(occurrence.body_text, printedUrls) : occurrence.body_text;
  return <section className="detail-section archived-excerpt">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div><h2>原刊文字</h2><p>保留刊载内容，不代表活动事实已核实。</p></div></div>
    {occurrence.body_text ? <div className="original-text">{readableText}</div> : <p className="detail-empty-note">这条记录没有可展示的正文文本。</p>}
    {isPdf && occurrence.body_text && <details className="pdf-original-layout">
      <summary>查看 PDF 存档原始换行</summary>
      <div className="original-text">{occurrence.body_text}</div>
    </details>}
  </section>;
}

function BulletinReference({ occurrence, state, referenceClaims }: { occurrence: PublicOccurrence; state: ReaderState; referenceClaims: PublicDate[] }) {
  const referenceDates = occurrence.original_dates ?? occurrence.dates;
  const issue = occurrence.issue_id ? state.issues.find((entry) => entry.id === occurrence.issue_id) ?? null : null;
  const blockedUrls = new Set((occurrence.date_review?.source_review.sources ?? [])
    .filter((source) => !originalSourceUrl(source.resolved_url)).map((source) => source.url));
  return <section className="detail-section reference-section">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div><h2>原刊与存档参考</h2><p>以下保留原刊资料、来源链接和档案内容；这些内容不替代已核验的活动字段。</p></div></div>
    <dl className="archive-metadata">
      <div><dt>原刊标题</dt><dd>{occurrence.title}</dd></div>
      <div><dt>消息类别</dt><dd>{occurrence.category ?? '未标注'}</dd></div>
      <div><dt>刊载类型与日期</dt><dd>{occurrence.kind === 'summary' ? '摘要' : occurrence.kind === 'detail' ? '详情' : '通知'} · {occurrence.issue_date ?? '期次日期未明'}</dd></div>
      {occurrence.issue_id && <div><dt>所属期次页面</dt><dd>{issue
        ? <Link to={state.issueHref(issue.id)}>{issue.issue_date ?? '期次日期未明'}{issue.issue_number !== null && ` · 第 ${issue.issue_number} 期`} · 打开本期</Link>
        : `期次记录暂不可用（${occurrence.issue_id}）`}</dd></div>}
      {(occurrence.locator.page !== undefined || occurrence.locator.anchor) && <div><dt>原文定位</dt><dd>{occurrence.locator.page !== undefined ? `第 ${occurrence.locator.page} 页` : ''}{occurrence.locator.anchor ? ` · ${occurrence.locator.anchor}` : ''}</dd></div>}
    </dl>
    <section className="reference-block source-reference">
      <h3>来源链接与人工纠正记录</h3>
      <SourceLinks occurrence={occurrence} state={state} />
    </section>
    {occurrence.manual_review && occurrence.date_review && <section className="reference-block manual-ai-reference">
      <h3>人工修改前的 AI 核查记录（仅参考）</h3>
      <p className="reference-note">保留原始核查理由和引文；不代表 AI 已核验本次人工修改。</p>
      <p>{occurrence.date_review.reason}</p>
      {occurrence.date_review.source_review.reason !== occurrence.date_review.reason && <p>{occurrence.date_review.source_review.reason}</p>}
      {occurrence.date_review.source_review.activity && <>
        <ActivityTextField field={occurrence.date_review.source_review.activity.title} label="AI 留存标题" />
        <ActivityTextField field={occurrence.date_review.source_review.activity.summary} label="AI 留存摘要" />
        <ActivityTextField field={occurrence.date_review.source_review.activity.content} label="AI 留存正文" />
        {(['title', 'summary', 'content', ...ACTIVITY_FACT_FIELDS.map(([key]) => key)] as const).map((key) => <ActivityFieldEvidence key={key} field={occurrence.date_review!.source_review.activity![key]} label={key} blockedUrls={blockedUrls} />)}
      </>}
    </section>}
    {occurrence.manual_review && <PrimaryActivityImages images={occurrence.primary_images} state={state} />}
    {referenceDates.length > 0 && <section className="reference-block">
      <h3>{occurrence.original_dates ? '原刊识别日期（参考）' : '历史日期记录（参考）'}</h3>
      <DateEvidence dates={referenceDates} emptyText="原刊中没有留存可识别的日期。" />
    </section>}
    {referenceClaims.length > 0 && <section className="reference-block">
      <h3>AI 日期判断（仅参考，不代表原始来源已核实）</h3>
      <DateEvidence dates={referenceClaims} emptyText="没有其他可供参考的日期判断。" expandEvidence referenceOnly />
    </section>}
    {occurrence.college_mentions.length > 0 && <section className="reference-block">
      <h3>原刊学院提及（参考）</h3>
      <CollegeMentions occurrence={occurrence} state={state} readOnly={Boolean(occurrence.manual_review || currentActivity(occurrence, state))} />
    </section>}
    {occurrence.table_cells.length > 0 && <section className="reference-block">
      <h3>原刊表格文字（参考）</h3>
      <div className="table-scroll"><table className="summary-table"><thead><tr><th scope="col">项目</th><th scope="col">原文内容</th></tr></thead><tbody>{occurrence.table_cells.map((cell, index) => <tr key={`${cell.header}-${index}`}><th scope="row">{cell.header || '未标注'}</th><td>{cell.text}</td></tr>)}</tbody></table></div>
    </section>}
    {occurrence.images.length > 0 && <section className="reference-block">
      <h3><ImageOff size={16} aria-hidden="true" />原刊档案配图</h3>
      <div className="image-gallery">{occurrence.images.map((image) => <figure className="archive-image" key={image.id}>
        <a href={state.assetUrl(image.path)} target="_blank" rel="noopener noreferrer" aria-label={`打开原刊档案图片：${image.caption || occurrence.title}`}><img src={state.assetUrl(image.path)} alt={image.caption || `原文配图${image.page !== null ? `，第 ${image.page} 页` : ''}`} loading="lazy" /></a>
        <figcaption>{image.caption || (image.page !== null ? `原文第 ${image.page} 页` : '原文配图')} · 点击查看档案图片</figcaption>
      </figure>)}</div>
    </section>}
    <IssueOriginal occurrence={occurrence} state={state} />
  </section>;
}

function DetailBody({ occurrence, state, activity, dateClaims }: { occurrence: PublicOccurrence; state: ReaderState; activity: ActivityReview | null; dateClaims: DateClaimGroups }) {
  const manual = Boolean(occurrence.manual_review?.activity);
  const hasAlternateRecords = (state.selectedItem?.occurrences.length ?? 0) > 1;
  const referenceLabel = occurrence.kind === 'standalone'
    ? hasAlternateRecords ? '独立收录与其他记录参考' : '独立收录与日期参考'
    : state.manifest?.public_facts_only
      ? hasAlternateRecords ? '刊载记录、其他版本与来源日期参考' : '刊载记录、来源与日期参考'
      : hasAlternateRecords
        ? manual ? '其他刊载记录、修改前的 AI 与来源通知参考' : '其他刊载记录、来源通知与原刊参考'
        : manual ? '修改前的 AI、来源通知与原刊参考' : '来源通知、原刊与核对记录';
  if (state.manifest?.public_facts_only) return <>
    <PublicFactsPanel occurrence={occurrence} />
    <details className="bulletin-reference-disclosure"><summary>{referenceLabel}</summary>
      <OccurrenceSwitcher occurrence={occurrence} state={state} />
      {occurrence.kind === 'standalone'
        ? <StandaloneReference occurrence={occurrence} referenceClaims={dateClaims.reference} />
        : <BulletinReference occurrence={occurrence} state={state} referenceClaims={dateClaims.reference} />}
    </details>
  </>;
  if (occurrence.kind === 'standalone') return <>
    {occurrence.manual_review?.activity ? <ManualActivityPanel occurrence={occurrence} /> : <PublicFactsPanel occurrence={occurrence} />}
    <details className="bulletin-reference-disclosure"><summary>{referenceLabel}</summary>
      <OccurrenceSwitcher occurrence={occurrence} state={state} />
      <StandaloneReference occurrence={occurrence} referenceClaims={dateClaims.reference} />
    </details>
  </>;
  return <>
    {manual ? <ManualActivityPanel occurrence={occurrence} />
      : activity ? <ActivityReviewPanel occurrence={occurrence} state={state} activity={activity} dateClaims={dateClaims} />
        : <OriginalExcerpt occurrence={occurrence} state={state} />}
    <details className="bulletin-reference-disclosure"><summary>{referenceLabel}</summary>
      <OccurrenceSwitcher occurrence={occurrence} state={state} />
      {(manual || activity) && <OriginalExcerpt occurrence={occurrence} state={state} />}
      <BulletinReference occurrence={occurrence} state={state} referenceClaims={dateClaims.reference} />
    </details>
  </>;
}

export function ItemDetail({ state }: { state: ReaderState }) {
  const occurrence = state.selectedOccurrence;
  const item = state.selectedItem;
  const favorite = item ? state.favorites.has(item.id) : false;

  if (state.detailStatus === 'loading') return <div className="detail-loading" role="status"><span className="loading-dot" />正在读取消息内容…</div>;
  if (state.detailStatus === 'error') return <div className="detail-error" role="alert"><h2>暂时无法读取这条消息</h2><p>{state.detailError || '档案文件读取失败。'}</p><Button type="button" onClick={state.retry}>重试读取</Button></div>;
  if (!item || !occurrence) return <div className="missing-item" role="status"><div className="empty-state-mark"><CircleHelp size={22} aria-hidden="true" /></div><h2>没有找到这条消息</h2><p>此条目可能不在当前静态档案版本中。</p><Button asChild variant="outline"><Link to={state.listHref} data-reader-return><ArrowLeft size={15} aria-hidden="true" />返回消息列表</Link></Button></div>;
  const activity = currentActivity(occurrence, state);
  const manualReview = occurrence.manual_review;
  const manual = manualReview?.activity;
  const manuallyReviewed = Boolean(manualReview);
  const humanVerified = hasVerifiedManualReview(occurrence);
  const dateClaims: DateClaimGroups = activity
    ? groupDateClaims(occurrence.date_review?.claims ?? [], occurrence, Boolean(state.manifest?.public_facts_only))
    : { primary: [], contextualBulletin: [], reference: occurrence.date_review?.claims ?? [] };
  const replaced = new Set((occurrence.source_corrections ?? []).map((correction) => correction.original_url));
  const blockedRedirects = new Set((occurrence.date_review?.source_review.sources ?? [])
    .filter((source) => !originalSourceUrl(source.resolved_url))
    .flatMap((source) => [source.url, originalSourceUrl(source.url)].filter((url): url is string => Boolean(url))));
  const sources = manual ? manual.source_urls
    : [activity?.title.evidence[0]?.url, ...(occurrence.source_corrections ?? []).map((correction) => correction.corrected_url), ...occurrence.source_urls, ...(occurrence.date_review?.source_review.sources.map((source) => source.url) ?? [])];
  const sourceHref = sources.filter((url): url is string => Boolean(url) && !replaced.has(url!) && !blockedRedirects.has(url!))
    .map((url) => manual ? manualPublicUrl(url) : url).filter((url): url is string => Boolean(url))
    .map(originalSourceUrl).find(Boolean);
  const participationCandidate = manual ? humanVerified ? manualParticipationUrl(manual, blockedRedirects) : null : primaryParticipationUrl(activity?.participation, blockedRedirects);
  const participationHref = participationCandidate ? originalSourceUrl(participationCandidate) : null;
  const title = manual?.title ?? (activity ? activity.title.value ?? '活动标题未确认' : occurrence.title);
  const summary = manual?.summary ?? activity?.summary.value ?? occurrence.preview;
  const category = manual ? manual.category : occurrence.category;

  return (
    <article className="item-detail" data-item-id={item.id} data-occurrence-id={occurrence.id}>
      <div className="detail-top-actions">
        <Button asChild variant="ghost" size="sm" className="detail-back"><Link to={state.listHref} data-reader-return><ArrowLeft size={16} aria-hidden="true" />返回阅读页</Link></Button>
        <div className="detail-save-share">
          <Button type="button" variant="outline" size="sm" onClick={() => state.toggleFavorite(item.id)} aria-pressed={favorite}>
            {favorite ? <BookmarkCheck size={16} aria-hidden="true" /> : <Bookmark size={16} aria-hidden="true" />}{favorite ? '已收藏' : '收藏消息'}
          </Button>
          <ShareButton key={occurrence.id} title={title} text={summary ?? occurrence.preview} path={itemSharePath(item.id, occurrence.id)} label="分享消息" />
        </div>
      </div>
      <header className="detail-header">
        <h1>{title}</h1>
        <OccurrenceWarnings occurrence={!manual && occurrence.date_review && !state.manifest?.public_facts_only ? { ...occurrence, dates: [] } : occurrence} today={state.today} dateClaims={manual ? occurrence.dates : activity ? [...dateClaims.primary, ...dateClaims.contextualBulletin] : []} />
        {state.manifest?.public_facts_only ? <>
          {manuallyReviewed && <ManualConfirmation occurrence={occurrence} />}
          <PublicFactsNotice />
        </> : manuallyReviewed ? <ManualConfirmation occurrence={occurrence} /> : !activity && <ContentConfirmation occurrence={occurrence} state={state} activity={null} />}
        {summary && <p className="detail-summary">{summary}</p>}
        {manual ? <ManualFacts activity={manual} verified={humanVerified} /> : activity && <ActivityFacts activity={activity} category={occurrence.category} dates={dateClaims.primary} />}
        <div className="detail-primary-actions">
          {participationHref ? <a className="reader-button reader-button--primary reader-button--default" href={participationHref} target="_blank" rel="noopener noreferrer">{manual ? '查看参与方式（人工确认）' : '查看参与方式'}<ExternalLink size={15} aria-hidden="true" /><span className="visually-hidden">（新窗口打开）</span></a>
            : sourceHref ? <a className="detail-source-action" href={sourceHref} target="_blank" rel="noopener noreferrer">{manuallyReviewed ? humanVerified ? '查看来源链接（人工确认）' : '查看来源链接（待核对）' : activity ? '查看原始通知' : '查看来源链接'}<ExternalLink size={15} aria-hidden="true" /></a>
              : <p className="detail-empty-note">原始来源链接未提供；可参考下方留存记录。</p>}
        </div>
        <div className="detail-badges">{category && <Badge variant="muted">{category}</Badge>}{occurrence.kind === 'standalone' && <Badge variant="muted">独立收录</Badge>}{state.manifest?.public_facts_only ? <ContentBadge status={occurrence.content_status} /> : <Badge variant={manuallyReviewed ? humanVerified ? 'accent' : 'warning' : activity ? 'muted' : 'warning'}>{manuallyReviewed ? humanVerified ? '人工核实' : '人工记录·待核对' : activity ? '已对照原文' : '原刊参考'}</Badge>}</div>
      </header>
      <div className="detail-sections"><DetailBody occurrence={occurrence} state={state} activity={activity} dateClaims={dateClaims} /></div>
    </article>
  );
}
