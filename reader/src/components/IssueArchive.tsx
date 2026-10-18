import { Archive, CalendarDays, FileText, FileWarning, Hash } from 'lucide-react';
import { Link } from 'react-router';
import type { PublicIssue, ReaderState } from '../types';
import { Badge } from './badge';
import { DocumentEvidence } from './DocumentEvidence';


function IssueDocuments({ issue, state }: { issue: PublicIssue; state: ReaderState }) {
  const pdfs = issue.documents.filter((document) => document.format === 'pdf' && document.pdf_path);
  const pdfMissing = issue.documents.some((document) => document.format === 'pdf' && !document.pdf_path);
  return (
    <div className="issue-documents" aria-label="本期原始文件">
      {issue.documents.map((document) => (
        <span className="document-label" key={document.id}><FileText size={14} aria-hidden="true" />{document.format === 'pdf' ? '原始 PDF' : '网页版本'}</span>
      ))}
      {pdfs.map((document) => <a className="pdf-link" key={document.id} href={state.assetUrl(document.pdf_path!)} target="_blank" rel="noopener noreferrer">查看原始 PDF</a>)}
      {pdfMissing && <span className="pdf-unavailable"><FileWarning size={14} aria-hidden="true" />原始 PDF 文件暂不可用</span>}
      {issue.documents.length > 0 && !issue.documents.some((document) => document.format === 'pdf') && <span className="pdf-unavailable"><FileWarning size={14} aria-hidden="true" />未提供原始 PDF</span>}
      {issue.documents.length === 0 && <span className="pdf-unavailable">未提供原始文件</span>}
    </div>
  );
}

export function IssueArchive({ state }: { state: ReaderState }) {
  const grouped = new Map<string, PublicIssue[]>();
  for (const issue of state.issues) {
    const key = issue.issue_date?.slice(0, 7) ?? '日期未明';
    const group = grouped.get(key);
    if (group) group.push(issue);
    else grouped.set(key, [issue]);
  }
  const groups = [...grouped.entries()].sort(([left], [right]) => {
    if (left === '日期未明') return 1;
    if (right === '日期未明') return -1;
    return right.localeCompare(left);
  });

  if (groups.length === 0) {
    return <div className="empty-state"><div className="empty-state-mark"><Archive size={22} aria-hidden="true" /></div><h2>尚无可浏览的历史期次</h2><p>当前静态档案没有可展示的期刊目录。</p></div>;
  }
  return (
    <div className="issue-archive">
      {groups.map(([month, issues]) => (
        <section className="issue-month" key={month} aria-labelledby={`issue-month-${month}`}>
          <div className="month-heading"><span className="month-rule" /><h2 id={`issue-month-${month}`}>{month === '日期未明' ? month : `${month.slice(0, 4)} 年 ${Number(month.slice(5))} 月`}</h2><Badge variant="muted">{issues.length} 期</Badge></div>
          <div className="issue-grid">
            {issues.map((issue) => (
              <article className="issue-card" key={issue.id}>
                <div className="issue-card-kicker"><CalendarDays size={15} aria-hidden="true" />{issue.issue_date ?? '期次日期未明'}{issue.issue_number !== null && <span className="issue-number"><Hash size={13} aria-hidden="true" />{issue.issue_number}</span>}</div>
                <h3><Link to={state.issueHref(issue.id)}>{issue.title}</Link></h3>
                <p className="issue-count">收录 {issue.occurrence_count.toLocaleString('zh-CN')} 条刊载记录</p>
                <IssueDocuments issue={issue} state={state} />
                {issue.documents.map((document) => <DocumentEvidence key={document.id} document={document} />)}
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
