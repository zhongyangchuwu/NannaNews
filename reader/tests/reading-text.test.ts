import { describe, expect, it } from 'vitest';
import { readablePdfText } from '../src/reading-text';

describe('PDF reading projection', () => {
  it('joins wrapped CJK words while retaining headings, bullets, and paragraph breaks', () => {
    const original = '活动安排：\n南青报告\n厅\n\n• 报名方式\n凭转发记录领\n取）\n\n下一个段落';

    expect(readablePdfText(original)).toBe('活动安排：\n南青报告厅\n\n• 报名方式\n凭转发记录领取）\n\n下一个段落');
    expect(original).toContain('南青报告\n厅');
    expect(original).toContain('凭转发记录领\n取）');
  });

  it('keeps Latin word spacing and blank-line paragraph boundaries', () => {
    const original = 'Join\nthe Research\nTeam\n\n第二段';

    expect(readablePdfText(original)).toBe('Join the Research Team\n\n第二段');
  });

  it('keeps separate populated fields while repairing their wrapped continuations', () => {
    const original = '演出时间：10 月 17 日 19:00\n演出地点：仙林校区南青报告\n厅\n领票时间：10 月 15 日、16 日（凭转发记录领\n取）\n详见：https://mp.weixin.qq.\ncom/s/example';
    expect(readablePdfText(original, ['https://mp.weixin.qq.com/s/example'])).toBe(
      '演出时间：10 月 17 日 19:00\n演出地点：仙林校区南青报告厅\n领票时间：10 月 15 日、16 日（凭转发记录领取）\n详见：https://mp.weixin.qq.com/s/example');
  });

  it('retains sentence-ending paragraph boundaries', () => {
    expect(readablePdfText('第一段结束。\n第二段的文字\n续行')).toBe('第一段结束。\n第二段的文字续行');
  });

  it('joins a wrapped printed URL only when it is supplied as an original source URL', () => {
    const originalUrl = 'https://mp.weixin.qq.com/s/n4_yO0-kRyi5YhFvA1zqkw';
    const wrapped = `${originalUrl.slice(0, 31)}\n${originalUrl.slice(31)}`;
    const rawText = `原刊链接：\n${wrapped}`;

    expect(readablePdfText(rawText, [originalUrl])).toBe(`原刊链接：\n${originalUrl}`);
    expect(readablePdfText(rawText, ['https://example.edu/corrected'])).toBe(rawText);
  });
});
