import { useEffect, useId, useRef, useState } from 'react';
import { CalendarDays, RotateCcw } from 'lucide-react';
import type { Filters, ReaderState } from '../types';
import { Button } from './button';

function MultiSelectDropdown({
  label,
  values,
  selected,
  onChange,
}: {
  label: string;
  values: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const dropdownRef = useRef<HTMLDetailsElement>(null);
  const summaryRef = useRef<HTMLElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  useEffect(() => {
    if (!isOpen) return;

    const closeOnOutsideClick = (event: MouseEvent) => {
      const dropdown = dropdownRef.current;
      if (dropdown && !dropdown.contains(event.target as Node)) {
        dropdown.open = false;
        setIsOpen(false);
      }
    };

    // Close after activation so collapsing a preceding group cannot move the clicked control.
    document.addEventListener('click', closeOnOutsideClick);
    return () => document.removeEventListener('click', closeOnOutsideClick);
  }, [isOpen]);

  if (values.length === 0) return null;
  const selectionSummary = selected.length === 0 ? '全部' : selected.join('、');

  return (
    <fieldset className="filter-group filter-select-group">
      <legend>{label}</legend>
      <details
        className="filter-dropdown"
        ref={dropdownRef}
        onToggle={(event) => setIsOpen(event.currentTarget.open)}
        onKeyDown={(event) => {
          if (event.key !== 'Escape' || !dropdownRef.current?.open) return;
          event.preventDefault();
          event.stopPropagation();
          dropdownRef.current.open = false;
          setIsOpen(false);
          summaryRef.current?.focus();
        }}
      >
        <summary
          ref={summaryRef}
          aria-label={`${label}，${selected.length === 0 ? '全部' : `已选 ${selected.length} 项`}`}
          title={selectionSummary}
        >
          <span className="filter-dropdown-summary">{selectionSummary}</span>
          {selected.length > 0 && <span className="filter-dropdown-count" aria-hidden="true">{selected.length}</span>}
        </summary>
        <div className="filter-options" role="group" aria-label={`${label}选项`}>
          {values.map((value) => {
            const checked = selected.includes(value);
            return (
              <label className="filter-option" key={value}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(event) => onChange(event.target.checked ? [...selected, value] : selected.filter((item) => item !== value))}
                />
                <span>{value}</span>
              </label>
            );
          })}
        </div>
      </details>
    </fieldset>
  );
}

interface FilterPanelProps {
  state: ReaderState;
  onDone?: () => void;
}

export function FilterPanel({ state, onDone }: FilterPanelProps) {
  const { filters } = state;
  const fieldId = useId();
  const patch = (values: Partial<Filters>) => state.setFilters({ ...values, page: 1 });

  return (
    <div className="filter-panel">
      <div className="filter-panel-heading">
        <div>
          <span className="eyebrow">分类检索 · INDEX</span>
          <h2>筛选条件</h2>
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={() => state.resetFilters()}>
          <RotateCcw size={15} aria-hidden="true" /> 重置
        </Button>
      </div>

      <label className="filter-toggle">
        <input type="checkbox" checked={filters.following} onChange={(event) => patch({ following: event.target.checked })} />
        <span><strong>只看关注学院</strong><small>按已关注学院筛选提及内容</small></span>
      </label>

      <MultiSelectDropdown label="信息类别" values={state.options.categories} selected={filters.category} onChange={(category) => patch({ category })} />
      <MultiSelectDropdown label="学院" values={state.options.colleges} selected={filters.college} onChange={(college) => patch({ college })} />
      <MultiSelectDropdown label="来源" values={state.options.sources} selected={filters.source} onChange={(source) => patch({ source })} />

      <fieldset className="filter-group">
        <legend>日期与时间</legend>
        <label className="field-label" htmlFor={`${fieldId}-date-type`}>按日期类型</label>
        <select id={`${fieldId}-date-type`} value={filters.date_type} onChange={(event) => patch({ date_type: event.target.value as Filters['date_type'] })}>
          <option value="event">活动日期</option>
          <option value="deadline">截止日期</option>
          <option value="listed">列明日期</option>
          <option value="source">来源日期</option>
        </select>
        <div className="date-range">
          <label>
            <span>从</span>
            <span className="date-input-wrap"><CalendarDays size={15} aria-hidden="true" /><input type="date" value={filters.date_from} onChange={(event) => patch({ date_from: event.target.value })} /></span>
          </label>
          <label>
            <span>至</span>
            <span className="date-input-wrap"><CalendarDays size={15} aria-hidden="true" /><input type="date" value={filters.date_to} onChange={(event) => patch({ date_to: event.target.value })} /></span>
          </label>
        </div>
        <div className="issue-range">
          <label htmlFor={`${fieldId}-issue-from`}>消息日期从</label>
          <input id={`${fieldId}-issue-from`} type="date" value={filters.issue_from} onChange={(event) => patch({ issue_from: event.target.value })} />
          <label htmlFor={`${fieldId}-issue-to`}>至</label>
          <input id={`${fieldId}-issue-to`} type="date" value={filters.issue_to} onChange={(event) => patch({ issue_to: event.target.value })} />
        </div>
        <div className="knownness-options" role="group" aria-label="日期能否确定">
          {([['all', '不限'], ['known', '可确定日期'], ['unknown', '日期未明']] as const).map(([value, label]) => (
            <button key={value} type="button" className={filters.date_status === value ? 'knownness-option is-active' : 'knownness-option'} onClick={() => patch({ date_status: value })} aria-pressed={filters.date_status === value}>{label}</button>
          ))}
        </div>
      </fieldset>

      <div className="filter-panel-footer">
        <span className="filter-note">筛选结果会保留在地址栏中</span>
        {onDone && <Button type="button" onClick={onDone}>查看结果</Button>}
      </div>
    </div>
  );
}
