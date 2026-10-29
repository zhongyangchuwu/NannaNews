import { describe, expect, it } from 'vitest';
import { participationUrl } from '../src/participation';
import type { ActivityField } from '../src/types';

function field(value: string | null, citations: string[]): ActivityField {
  return { value, evidence: citations.map((text) => ({ url: 'https://example.org/notice', image_index: null, text })) };
}

describe('source-supported participation links', () => {
  it('does not promote a shortened URL merely because it prefixes a cited endpoint', () => {
    expect(participationUrl(field('填写 https://example.org/forms/a', ['请填写 https://example.org/forms/abc']))).toBeNull();
    expect(participationUrl(field('https://example.org/forms/a?group=1', ['https://example.org/forms/a?group=12']))).toBeNull();
  });

  it('uses a complete cited endpoint and skips an earlier unsupported candidate', () => {
    expect(participationUrl(field('https://example.org/wrong 或（https://example.org/form?id=12）。',
      ['请填表（https://example.org/form?id=12），截止日期见通知。']))).toBe('https://example.org/form?id=12');
  });

  it('does not substitute the citation source page for a missing participation URL', () => {
    expect(participationUrl(field('现场参加', ['请直接到场。']))).toBeNull();
    expect(participationUrl(field('https://example.org/notice', ['请直接到场。']))).toBeNull();
  });

  it('does not promote unsupported schemes or unconfirmed values', () => {
    expect(participationUrl(field('javascript:alert(1)', ['javascript:alert(1)']))).toBeNull();
    expect(participationUrl(field(null, ['https://example.org/form']))).toBeNull();
    expect(participationUrl(undefined)).toBeNull();
  });
});
