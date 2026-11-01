import { ArrowUpRight, Bookmark, BookmarkCheck, CalendarDays, ExternalLink, FileText, FileWarning } from 'lucide-react';
import { Link } from 'react-router';
import type { CatalogOccurrence, PublicIssue, ReaderState } from '../types';
import { Button } from './button';
import { ContentBadge, OccurrenceFacts, OccurrenceReminder } from './OccurrenceList';
import { ShareButton } from './ShareButton';
import { issueSharePath } from '../sharing';
import { sourceUrls } from '../source-links.mjs';

const CATEGORY_ORDER = ['校园动态', '讲座', '院级活动', '社团活动', '招聘升学宣讲'];


function groupRecords(records: CatalogOccurrence[]) {
  const groups = new Map<string, CatalogOccurrence[]>();
  for (const record of records) {
    const category = record.category?.trim() || '校园动态';
    const group = groups.get(category);
    if (group) group.push(record);
    else groups.set(category, [record]);
  }
  return [...groups.entries()].sort(([left], [right]) => {
    const leftIndex = CATEGORY_ORDER.indexOf(left);
    const rightIndex = CATEGORY_ORDER.indexOf(right);
    if (leftIndex >= 0 || rightIndex >= 0) {
      if (leftIndex < 0) return 1;
      if (rightIndex < 0) return -1;
      return leftIndex - rightIndex;
    }
    return left.localeCompare(right, 'zh-CN');
  });
}

function EditionNavigation() {
  return <nav className="edition-actions" aria-label="刊物导航">
    <Button asChild variant="outline" size="sm"><Link to="/activities">当前活动</Link></Button>
    <Button asChild variant="ghost" size="sm"><Link to="/issues">历史期次</Link></Button>
  </nav>;
}

function OriginalDocuments({ issue, state }: { issue: PublicIssue; state: ReaderState }) {
  if (state.manifest?.public_facts_only) return <div className="edition-documents" aria-label="本期原始文件">
    <span className="edition-document-label">精简公开事实版仅含基本信息；本构建不含原刊附件。</span>
  </div>;
  const pdfs = issue.documents.filter((document) => document.format === 'pdf' && document.pdf_path);
  const htmls = issue.documents.filter((document) => document.format === 'html');
  const pdfMissing = issue.documents.some((document) => document.format === 'pdf' && !document.pdf_path);
  if (issue.documents.length === 0) return null;
  return <div className="edition-documents" aria-label="本期原始文件">
    {pdfs.map((document) => <a key={document.id} href={state.assetUrl(document.pdf_path!)} target="_blank" rel="noopener noreferrer">
      <FileText size={15} aria-hidden="true" />原始 PDF<ArrowUpRight size={14} aria-hidden="true" />
    </a>)}
    {htmls.map((document) => <span className="edition-document-label" key={document.id}>
      <FileText size={15} aria-hidden="true" />网页原件（HTML）<small>{document.filename} · {document.issue_date ?? document.claimed_issue_date ?? '期次日期未明'}</small>
    </span>)}
    {pdfMissing && <span className="edition-document-unavailable"><FileWarning size={15} aria-hidden="true" />原始 PDF 文件暂不可用</span>}
  </div>;
}

function StoryCard({ record, state }: { record: CatalogOccurrence; state: ReaderState }) {
  const itemHref = state.itemHref(record.item_id, record.id);
  const favorite = state.favorites.has(record.item_id);
  const corrections = record.source_corrections ?? [];
  const replacedUrls = new Set(corrections.map((correction) => correction.original_url));
  const sourceUrlsRaw = [...record.source_urls, ...corrections.map((correction) => correction.corrected_url)]
    .filter((raw) => !replacedUrls.has(raw));
  const sourceLinks = sourceUrls(sourceUrlsRaw);

  return <article className="edition-story" data-item-id={record.item_id} data-occurrence-id={record.id}>
    <div className="edition-story-heading">
      <h3><Link to={itemHref}>{record.title}</Link></h3>
    </div>
    {record.preview && <p className="edition-story-preview">{record.preview}</p>}
    <OccurrenceFacts occurrence={record} />
    <div className="edition-story-meta">
      <ContentBadge status={record.content_status} />
      <OccurrenceReminder occurrence={record} today={state.today} href={itemHref} />
      {sourceLinks[0] ? <a className="edition-source-link" href={sourceLinks[0]} target="_blank" rel="noopener noreferrer">查看来源<ExternalLink size={13} aria-hidden="true" /><span className="visually-hidden">（新窗口打开）</span></a> : <span className="edition-source-link">原始来源链接未提供</span>}
      {sourceLinks.length > 1 && <Link className="edition-source-link" to={itemHref}>另 {sourceLinks.length - 1} 个来源</Link>}
    </div>
    <div className="edition-story-actions">
      <Button type="button" variant="outline" size="sm" onClick={() => state.toggleFavorite(record.item_id)} aria-label={favorite ? `取消收藏：${record.title}` : `收藏：${record.title}`} aria-pressed={favorite}>
        {favorite ? <BookmarkCheck size={15} aria-hidden="true" /> : <Bookmark size={15} aria-hidden="true" />}{favorite ? '已收藏' : '收藏'}
      </Button>
      <Button asChild variant="ghost" size="sm"><Link to={itemHref}>阅读全文<ArrowUpRight size={14} aria-hidden="true" /></Link></Button>
    </div>
  </article>;
}

function scrollToSection(id: string) {
  const section = document.getElementById(id);
  const heading = document.getElementById(`${id}-heading`);
  if (!section || !heading) return;
  section.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  heading.focus({ preventScroll: true });
}

export function EditionPage({ state }: { state: ReaderState }) {
  const issue = state.editionIssue;
  const groups = groupRecords(state.editionRecords);
  const query = state.filters.q.trim();

  return <div className="edition-page">
    <header className="edition-hero">
      <h1>{issue?.title || '南哪消息'}</h1>
      {issue && <p className="edition-date">
        <CalendarDays size={16} aria-hidden="true" />{issue.issue_date || '期次日期未明'}
        {issue.issue_number !== null && <span>第 {issue.issue_number} 期</span>}
      </p>}
      {query && <div className="edition-search-status" role="status">
        <span>{state.searching ? '正在检索本期' : '本期搜索'}「{query}」{!state.searching && ` · ${state.editionRecords.length} 条匹配`}</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => state.setFilters({ q: '', page: 1 })}>清除搜索</Button>
      </div>}
      {issue && <div className="edition-meta">
        <div className="edition-stats" aria-label="本期收录统计">
          <span><strong>{state.editionRecords.length.toLocaleString('zh-CN')}</strong> 条消息</span>
          <span><strong>{groups.length.toLocaleString('zh-CN')}</strong> 个栏目</span>
        </div>
        <ShareButton key={issue.id} title={issue.title} text={`${issue.issue_date ?? ''} · ${issue.occurrence_count} 条校园消息`} path={issueSharePath(issue.id)} label="分享本期" />
        <OriginalDocuments state={state} issue={issue} />
      </div>}
    </header>

    {!issue ? <section className="edition-empty" aria-live="polite">
      <h2>{state.status === 'loading' ? '正在读取本期内容' : '暂时没有可展示的本期'}</h2>
      <p>{state.status === 'error' ? state.error || '当前静态档案暂不可读取。' : '本期刊物尚未载入；你仍可浏览当前活动或历史期次。'}</p>
      <EditionNavigation />
    </section> : state.editionRecords.length === 0 ? <section className="edition-empty" aria-live="polite">
      <h2>{state.searching ? '正在检索本期内容' : query ? '本期没有匹配的消息' : '本期暂无可展示的消息'}</h2>
      <p>{state.searching ? '检索完成后会在此显示本期匹配消息。' : query ? '可以换一个关键词，或清除搜索，继续按栏目阅读本期。' : '本期目录已载入，但当前没有可浏览的刊载记录。'}</p>
      {query ? <Button type="button" variant="outline" onClick={() => state.setFilters({ q: '', page: 1 })}>清除搜索，阅读本期</Button> : <EditionNavigation />}
    </section> : <div className="edition-layout">
      <nav className="edition-toc" aria-label="本期栏目目录">
        <p>本期栏目</p>
        {groups.map(([category, records], index) => {
          const sectionId = `edition-section-${index}`;
          return <Button key={category} type="button" variant="ghost" size="sm" onClick={() => scrollToSection(sectionId)} aria-controls={sectionId}>
            <span>{category}</span><span className="edition-toc-count">{records.length.toLocaleString('zh-CN')}</span>
          </Button>;
        })}
      </nav>
      <div className="edition-sections">
        {groups.map(([category, records], index) => {
          const sectionId = `edition-section-${index}`;
          return <section className="edition-section" id={sectionId} key={category} aria-labelledby={`${sectionId}-heading`}>
            <header className="edition-section-heading">
              <h2 id={`${sectionId}-heading`} tabIndex={-1}>{category}</h2>
              <span>{records.length.toLocaleString('zh-CN')} 条</span>
            </header>
            <div className="edition-stories">
              {records.map((record) => <StoryCard key={record.id} record={record} state={state} />)}
            </div>
          </section>;
        })}
      </div>
    </div>}
  </div>;
}
