import { ArrowLeft, ArrowUpRight, Bookmark, BookmarkCheck, CalendarDays, ChevronDown, CircleHelp, Download, ExternalLink, FileText, ImageOff, MapPin, Paperclip } from 'lucide-react';
import { Link } from 'react-router';
import { readablePdfText } from '../reading-text';
import type { ActivityCitation, ActivityField, ActivityReview, PublicCollege, PublicDate, PublicDocument, PublicOccurrence, ReaderState } from '../types';
import { Badge } from './badge';
import { Button } from './button';
import { DocumentEvidence } from './DocumentEvidence';

function safeHttpUrl(raw: string) {
  try {
    const url = new URL(raw);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function DateEvidence({ dates, emptyText, expandEvidence = false }: { dates: PublicDate[]; emptyText: string; expandEvidence?: boolean }) {
  if (dates.length === 0) return <p className="detail-empty-note">{emptyText}</p>;
  return <div className="date-evidence-list">
    {dates.map((date, index) => {
      const known = (date.resolution === 'explicit' || date.resolution === 'contextual') && date.normalized_date;
      const dateType = date.kind === 'event' ? '活动' : date.kind === 'deadline' ? '截止' : date.kind === 'listed' ? '刊载' : date.kind === 'source' ? '来源' : date.kind === 'registration' ? '报名' : date.kind === 'application' ? '申请／申报' : date.kind;
      return <div className="date-evidence" key={`${date.kind}-${date.raw_text}-${index}`}>
        <span className="date-evidence-type">{dateType}</span>
        <div>
          <strong>{expandEvidence && known ? date.normalized_date : date.raw_text || (known ? date.normalized_date : '原文日期未明')}</strong>
          {date.literal_source === 'primary' && <Badge variant="accent">引句来自原文</Badge>}
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
interface DateClaimGroups { primary: PublicDate[]; contextualBulletin: PublicDate[]; reference: PublicDate[] }

function groupDateClaims(claims: PublicDate[]): DateClaimGroups {
  const groups: DateClaimGroups = { primary: [], contextualBulletin: [], reference: [] };
  for (const claim of claims) {
    if (claim.literal_source === 'primary' && ACTIVITY_DATE_KINDS[claim.kind]) groups.primary.push(claim);
    else if (claim.literal_source === 'bulletin' && claim.resolution === 'contextual' && ACTIVITY_DATE_KINDS[claim.kind]) groups.contextualBulletin.push(claim);
    else groups.reference.push(claim);
  }
  return groups;
}

function currentActivity(occurrence: PublicOccurrence, state: ReaderState): ActivityReview | null {
  const review = occurrence.date_review;
  const version = state.manifest?.content_rules_version;
  if (!review || !version || review.rules_version !== version || review.source_review.status !== 'source_verified') return null;
  const replaced = new Set((occurrence.source_corrections ?? []).map((correction) => correction.original_url));
  if (review.source_review.sources.some((source) => replaced.has(source.url))) return null;
  return review.source_review.activity ?? null;
}

function ActivityCitationEvidence({ citation }: { citation: ActivityCitation }) {
  const href = safeHttpUrl(citation.url);
  return <div className="activity-citation">
    <blockquote>{citation.text}</blockquote>
    {href && <a href={href} target="_blank" rel="noopener noreferrer">{citation.url}<ExternalLink size={14} aria-hidden="true" /></a>}
  </div>;
}

function ActivityFieldEvidence({ field, label }: { field: ActivityField; label: string }) {
  if (field.evidence.length === 0) return null;
  return <details className="activity-field-evidence">
    <summary>{label}来源依据（{field.evidence.length} 条）</summary>
    {field.evidence.map((citation, index) => <ActivityCitationEvidence key={`${citation.url}-${citation.image_index}-${index}`} citation={citation} />)}
  </details>;
}

function ActivityTextField({ field, label }: { field: ActivityField; label: string }) {
  return <section className="activity-copy">
    <h3>{label}</h3>
    <p>{field.value ?? '尚未从已核验来源确认此项。'}</p>
    <ActivityFieldEvidence field={field} label={label} />
  </section>;
}

const ACTIVITY_FACT_FIELDS = [
  ['time', '活动时间'], ['venue', '地点'], ['organizer', '组织方'], ['audience', '面向对象'], ['participation', '参与方式'],
] as const;

function ActivityFacts({ activity }: { activity: ActivityReview }) {
  return <dl className="activity-facts">
    {ACTIVITY_FACT_FIELDS.map(([key, label]) => {
      const field = activity[key];
      return <div className="activity-fact" key={key}>
        <dt>{label}</dt>
        <dd>{field.value ?? <span className="activity-unknown">尚未确认</span>}</dd>
        <ActivityFieldEvidence field={field} label={label} />
      </div>;
    })}
  </dl>;
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
  const links = [...new Set([...occurrence.source_urls, ...(review?.sources.map((source) => source.url) ?? []), ...corrections.map((correction) => correction.corrected_url)])]
    .filter((url) => !replaced.has(url)).map((raw) => ({ raw, href: safeHttpUrl(raw) })).filter((entry) => entry.href);
  return <>
    {corrections.length > 0 && <div className="document-evidence source-corrections" aria-label="来源链接纠正记录">
      <Badge variant="accent">已人工校正来源</Badge>
      {corrections.map((correction) => {
        const correctedHref = safeHttpUrl(correction.corrected_url);
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
      {staleReview ? <p>来源链接已人工校正；旧来源核验不代表新链接、日期或活动内容已核实。</p>
        : legacyReview ? <Badge variant="muted">历史留存来源</Badge>
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
    })}</ul> : <p className="detail-empty-note">本条记录未附可访问的来源链接。</p>}
  </>;
}

function sourceDocument(occurrence: PublicOccurrence, state: ReaderState): PublicDocument | null {
  const issue = state.issues.find((entry) => entry.id === occurrence.issue_id);
  return issue?.documents.find((document) => document.id === occurrence.document_id) ?? null;
}

function IssuePdfs({ occurrence, state }: { occurrence: PublicOccurrence; state: ReaderState }) {
  const original = sourceDocument(occurrence, state);
  const documents = original?.format === 'pdf' ? [original] : [];
  const available = documents.filter((document) => document.pdf_path);
  return <section className="reference-block original-pdf-reference">
    <div className="section-heading"><span className="section-icon"><Paperclip size={17} aria-hidden="true" /></span><div><h3>本条所在期次原件</h3><p>仅显示与这条消息来源文档匹配的历史原件。</p></div></div>
    {available.length ? <div className="pdf-downloads">{available.map((document) => <a className="pdf-download" key={document.id} href={state.assetUrl(document.pdf_path!)} target="_blank" rel="noopener noreferrer"><Download size={16} aria-hidden="true" /><span><strong>打开历史原始 PDF</strong><small>{document.filename} · {document.issue_date ?? document.claimed_issue_date ?? '期次日期未明'}</small></span><ArrowUpRight size={16} aria-hidden="true" /></a>)}</div> : <p className="pdf-empty"><FileText size={16} aria-hidden="true" />{documents.length ? '该期原始 PDF 当前不可用。' : '该来源期次未提供原始 PDF。'}</p>}
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
  if (!review) message = '尚无当前规则下的来源核验记录；原刊文字仅作为参考。';
  else if (staleReview) message = '来源已人工校正；旧来源核验不代表新链接、日期或活动内容已核实。';
  else if (!currentRules) message = '此为历史核查记录，不构成当前规则下的活动内容确认；旧日期仅在下方作为参考。';
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
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div><h2>AI 标准化活动信息</h2><p>信息依据已读取的原始来源整理；每项可展开查看证据。</p></div></div>
    <div className="activity-primary-copy">
      <ActivityTextField field={activity.summary} label="活动摘要" />
      <details className="activity-more-content"><summary>补充内容与依据</summary><ActivityTextField field={activity.content} label="活动内容" /></details>
    </div>
    <ActivityDates primaryDates={dateClaims.primary} contextualDates={dateClaims.contextualBulletin} />
    <ActivityFacts activity={activity} />
    <ActivityColleges colleges={activity.colleges} state={state} />
    <PrimaryActivityImages images={activity.image_indices.length > 0 ? occurrence.primary_images : undefined} state={state} />
  </section>;
}

function BulletinReference({ occurrence, state, referenceClaims }: { occurrence: PublicOccurrence; state: ReaderState; referenceClaims: PublicDate[] }) {
  const document = sourceDocument(occurrence, state);
  const isPdf = document?.format === 'pdf';
  const corrections = occurrence.source_corrections ?? [];
  const printedUrls = [...new Set([...occurrence.source_urls, ...corrections.map((correction) => correction.original_url)])];
  const referenceDates = occurrence.original_dates ?? occurrence.dates;
  const readableText = isPdf ? readablePdfText(occurrence.body_text, printedUrls) : occurrence.body_text;
  return <section className="detail-section reference-section">
    <div className="section-heading"><span className="section-icon"><FileText size={17} aria-hidden="true" /></span><div><h2>原刊与存档参考</h2><p>以下保留原刊资料、来源链接和档案内容；这些内容不替代已核验的活动字段。</p></div></div>
    <dl className="archive-metadata">
      <div><dt>原刊标题</dt><dd>{occurrence.title}</dd></div>
      <div><dt>消息类别</dt><dd>{occurrence.category ?? '未标注'}</dd></div>
      <div><dt>刊载类型与日期</dt><dd>{occurrence.kind === 'summary' ? '摘要' : occurrence.kind === 'detail' ? '详情' : '通知'} · {occurrence.issue_date ?? '期次日期未明'}</dd></div>
      {(occurrence.locator.page !== undefined || occurrence.locator.anchor) && <div><dt>原文定位</dt><dd>{occurrence.locator.page !== undefined ? `第 ${occurrence.locator.page} 页` : ''}{occurrence.locator.anchor ? ` · ${occurrence.locator.anchor}` : ''}</dd></div>}
    </dl>
    <section className="reference-block source-reference">
      <h3>来源链接与人工纠正记录</h3>
      <SourceLinks occurrence={occurrence} state={state} />
    </section>
    <section className="reference-block archived-excerpt">
      <h3>原刊文字摘录</h3>
      <p className="reference-note">{isPdf ? 'PDF 可读性排版仅调整部分断行；展开可查看保留原始换行的存档文本。' : 'HTML 文字按存档中的原始段落排版呈现。'}</p>
      {occurrence.body_text ? <div className="original-text">{readableText}</div> : <p className="detail-empty-note">这条记录没有可展示的正文文本。</p>}
      {isPdf && occurrence.body_text && <details className="pdf-original-layout">
        <summary>查看 PDF 原始版式与换行</summary>
        <div className="original-text">{occurrence.body_text}</div>
      </details>}
    </section>
    {referenceDates.length > 0 && <section className="reference-block">
      <h3>{occurrence.original_dates ? '原刊识别日期（参考）' : '历史日期记录（参考）'}</h3>
      <DateEvidence dates={referenceDates} emptyText="原刊中没有留存可识别的日期。" />
    </section>}
    {referenceClaims.length > 0 && <section className="reference-block">
      <h3>AI 日期判断（仅参考，不代表原始来源已核实）</h3>
      <DateEvidence dates={referenceClaims} emptyText="没有其他可供参考的日期判断。" expandEvidence />
    </section>}
    {occurrence.college_mentions.length > 0 && <section className="reference-block">
      <h3>原刊学院提及（参考）</h3>
      <CollegeMentions occurrence={occurrence} state={state} readOnly={Boolean(currentActivity(occurrence, state))} />
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
    <IssuePdfs occurrence={occurrence} state={state} />
  </section>;
}

function DetailBody({ occurrence, state, activity, dateClaims }: { occurrence: PublicOccurrence; state: ReaderState; activity: ActivityReview | null; dateClaims: DateClaimGroups }) {
  return <>
    <ContentConfirmation occurrence={occurrence} state={state} activity={activity} />
    {activity && <ActivityReviewPanel occurrence={occurrence} state={state} activity={activity} dateClaims={dateClaims} />}
    <BulletinReference occurrence={occurrence} state={state} referenceClaims={dateClaims.reference} />
  </>;
}

export function ItemDetail({ state }: { state: ReaderState }) {
  const occurrence = state.selectedOccurrence;
  const item = state.selectedItem;
  const favorite = item ? state.favorites.has(item.id) : false;

  if (state.detailStatus === 'loading') return <div className="detail-loading" role="status"><span className="loading-dot" />正在读取这条消息的公开档案…</div>;
  if (state.detailStatus === 'error') return <div className="detail-error" role="alert"><h2>暂时无法读取这条消息</h2><p>{state.detailError || '档案文件读取失败。'}</p><Button type="button" onClick={state.retry}>重试读取</Button></div>;
  if (!item || !occurrence) return <div className="missing-item" role="status"><div className="empty-state-mark"><CircleHelp size={22} aria-hidden="true" /></div><h2>没有找到这条消息</h2><p>此条目可能不在当前静态档案版本中。</p><Button asChild variant="outline"><Link to={state.listHref}><ArrowLeft size={15} aria-hidden="true" />返回消息列表</Link></Button></div>;
  const activity = currentActivity(occurrence, state);
  const dateClaims: DateClaimGroups = activity
    ? groupDateClaims(occurrence.date_review?.claims ?? [])
    : { primary: [], contextualBulletin: [], reference: occurrence.date_review?.claims ?? [] };

  return (
    <article className="item-detail">
      <div className="detail-top-actions">
        <Button asChild variant="ghost" size="sm" className="detail-back"><Link to={state.listHref}><ArrowLeft size={16} aria-hidden="true" />返回列表</Link></Button>
        <Button type="button" variant="outline" size="sm" onClick={() => state.toggleFavorite(item.id)} aria-pressed={favorite}>
          {favorite ? <BookmarkCheck size={16} aria-hidden="true" /> : <Bookmark size={16} aria-hidden="true" />}{favorite ? '已收藏' : '收藏消息'}
        </Button>
      </div>
      <header className="detail-header">
        <div className="detail-badges">{occurrence.category && <Badge variant="accent">{occurrence.category}</Badge>}<Badge variant="muted">{occurrence.kind === 'summary' ? '摘要' : occurrence.kind === 'detail' ? '详情' : '通知'}</Badge><span className="detail-issue-date">{occurrence.issue_date ?? '消息日期未明'}</span><Badge variant={activity ? 'accent' : 'warning'}>{activity ? 'AI 已对照原文' : '活动内容未确认'}</Badge>{(occurrence.source_corrections?.length ?? 0) > 0 && <Badge variant="muted">人工校正来源</Badge>}</div>
        <h1>{activity ? activity.title.value ?? '活动标题未确认' : occurrence.title}</h1>
        <p className="detail-title-source">{activity ? '标题由 AI 依据已读取的原文整理。' : '原刊档案标题；不代表活动内容已核验。'}</p>
        {activity && <ActivityFieldEvidence field={activity.title} label="活动标题" />}
      </header>
      <OccurrenceSwitcher occurrence={occurrence} state={state} />
      <div className="detail-sections"><DetailBody occurrence={occurrence} state={state} activity={activity} dateClaims={dateClaims} /></div>
    </article>
  );
}
