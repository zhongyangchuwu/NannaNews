import { useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { Archive, CalendarDays, FileText, FileWarning, Hash } from 'lucide-react';
import type { PublicIssue, ReaderState } from '../types';
import { Badge } from './badge';
import { Button } from './button';
import { DocumentEvidence } from './DocumentEvidence';

function IssueDocuments({ issue, state }: { issue: PublicIssue; state: ReaderState }) {
  if (state.manifest?.public_facts_only) return <div className="issue-documents" aria-label="本期原始文件">
    <span className="document-label">精简公开事实版仅含基本信息；本构建不含原刊附件。</span>
  </div>;
  const pdfs = issue.documents.filter((document) => document.format === 'pdf' && document.pdf_path);
  const pdfMissing = issue.documents.some((document) => document.format === 'pdf' && !document.pdf_path);
  return (
    <div className="issue-documents" aria-label="本期原始文件">
      {issue.documents.map((document) => (
        <span className="document-label" key={document.id}><FileText size={14} aria-hidden="true" />{document.format === 'pdf' ? '原始 PDF' : '网页原件（HTML）'}</span>
      ))}
      {pdfs.map((document) => <a className="pdf-link" key={document.id} href={state.assetUrl(document.pdf_path!)} target="_blank" rel="noopener noreferrer">查看原始 PDF</a>)}
      {pdfMissing && <span className="pdf-unavailable"><FileWarning size={14} aria-hidden="true" />原始 PDF 文件暂不可用</span>}
      {issue.documents.length === 0 && <span className="pdf-unavailable">未提供原始文件</span>}
    </div>
  );
}

function groupIssues(issues: PublicIssue[]) {
  const years = new Map<string, Map<string, PublicIssue[]>>();
  const sortedIssues = [...issues].sort((left, right) => {
    const dateOrder = (right.issue_date ?? '').localeCompare(left.issue_date ?? '');
    if (dateOrder) return dateOrder;
    if (left.issue_number !== right.issue_number) {
      if (left.issue_number === null) return 1;
      if (right.issue_number === null) return -1;
      return right.issue_number - left.issue_number;
    }
    return left.title.localeCompare(right.title, 'zh-CN');
  });

  for (const issue of sortedIssues) {
    const year = issue.issue_date?.slice(0, 4) || 'undated';
    const month = issue.issue_date?.slice(0, 7) || 'undated';
    let months = years.get(year);
    if (!months) {
      months = new Map();
      years.set(year, months);
    }
    const group = months.get(month);
    if (group) group.push(issue);
    else months.set(month, [issue]);
  }

  return [...years.entries()].sort(([left], [right]) => {
    if (left === 'undated') return 1;
    if (right === 'undated') return -1;
    return right.localeCompare(left);
  });
}

function scrollToYear(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'instant', block: 'start' });
}

export function IssueArchive({ state }: { state: ReaderState }) {
  const years = groupIssues(state.issues);
  const location = useLocation();
  const navigate = useNavigate();
  const yearParam = new URLSearchParams(location.search).get('year');
  const expandedYear = yearParam === 'none' ? null : years.some(([year]) => year === yearParam) ? yearParam : years[0]?.[0];
  const expandYear = (year: string | null) => {
    const query = new URLSearchParams(location.search);
    query.set('year', year ?? 'none');
    navigate({ pathname: '/issues', search: `?${query}` }, { replace: true });
  };
  useEffect(() => {
    if (yearParam && expandedYear) scrollToYear(`issue-year-${expandedYear}`);
    else window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
  }, [yearParam, expandedYear]);

  if (years.length === 0) {
    return <div className="empty-state"><div className="empty-state-mark"><Archive size={22} aria-hidden="true" /></div><h2>尚无可浏览的历史期次</h2><p>当前静态档案没有可展示的期刊目录。</p></div>;
  }

  return (
    <div className="issue-archive">
      <nav className="issue-years" aria-label="按年份浏览历史期次">
        {years.map(([year, months]) => {
          const id = `issue-year-${year}`;
          const count = [...months.values()].reduce((total, issues) => total + issues.length, 0);
          const isExpanded = expandedYear === year;
          return <Button key={year} type="button" size="sm" variant="outline" aria-controls={id} aria-expanded={isExpanded} onClick={() => {
            expandYear(year);
          }}>
            {year === 'undated' ? '日期未明' : `${year} 年`}<span>{count} 期</span>
          </Button>;
        })}
      </nav>
      {years.map(([year, months]) => {
        const id = `issue-year-${year}`;
        const count = [...months.values()].reduce((total, issues) => total + issues.length, 0);
        const isExpanded = expandedYear === year;
        return <details className="issue-year" id={id} key={year} open={isExpanded} onToggle={(event) => {
          const open = event.currentTarget.open;
          if (open !== isExpanded) expandYear(open ? year : null);
        }}>
          <summary className="issue-year-summary">
            <span>{year === 'undated' ? '日期未明' : `${year} 年`}</span><Badge variant="muted">{count} 期</Badge>
          </summary>
          <div className="issue-year-content">
            {[...months.entries()].map(([month, issues], monthIndex) => {
              const monthId = `${id}-month-${monthIndex}`;
              return <section className="issue-month" key={month} aria-labelledby={monthId}>
                <div className="month-heading"><span className="month-rule" /><h2 id={monthId}>{month === 'undated' ? '日期未明' : `${month.slice(0, 4)} 年 ${Number(month.slice(5))} 月`}</h2><Badge variant="muted">{issues.length} 期</Badge></div>
                <div className="issue-grid">
                  {issues.map((issue) => (
                    <article className="issue-card" key={issue.id}>
                      <div className="issue-card-kicker"><CalendarDays size={15} aria-hidden="true" />{issue.issue_date ?? '期次日期未明'}{issue.issue_number !== null && <span className="issue-number"><Hash size={13} aria-hidden="true" />{issue.issue_number}</span>}</div>
                      <h3><Link to={state.issueHref(issue.id)}>{issue.title}</Link></h3>
                      <p className="issue-count">收录 {issue.occurrence_count.toLocaleString('zh-CN')} 条刊载记录</p>
                      <IssueDocuments issue={issue} state={state} />
                      {!state.manifest?.public_facts_only && issue.documents.map((document) => <DocumentEvidence key={document.id} document={document} />)}
                    </article>
                  ))}
                </div>
              </section>;
            })}
          </div>
        </details>;
      })}
    </div>
  );
}
