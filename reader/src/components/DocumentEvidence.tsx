import type { PublicDocument } from '../types';

const warningLabels: Record<string, string> = {
  claimed_issue_date_mismatch: '归档声称日期与原文页头不同，请以原文证据为准。',
  image_missing: '该文档引用的部分历史图片缺失，未自动补抓。',
  image_not_acquired: '该文档的部分外部图片未收录；没有配图不代表原文没有图片。',
  no_ocr: '海报仅保留原图，未进行 OCR，图片中的事实可能未进入检索。',
  unresolved_dates: '该文档含无法完整确认的日期，未推断缺失年份。',
};

export function DocumentEvidence({ document }: { document: PublicDocument }) {
  const mismatch = document.claimed_issue_date && document.issue_date && document.claimed_issue_date !== document.issue_date;
  const warnings = document.warnings.filter((code) => !mismatch || code !== 'claimed_issue_date_mismatch');
  if (!mismatch && warnings.length === 0) return null;
  return <div className="document-evidence" aria-label="原始文档注意事项">
    {mismatch && <p>归档声称日期：{document.claimed_issue_date}；实际页头日期：{document.issue_date}。两者分别保留。</p>}
    {warnings.map((code) => <p key={code}>{warningLabels[code] || '该文档保留了需核对的原始资料警告。'}</p>)}
  </div>;
}
