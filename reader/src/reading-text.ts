const CJK_CHARACTER = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/u;
const CJK_PUNCTUATION = /[，。！？；：、…—（）()《》「」『』【】〔〕“”‘’]/u;
const LATIN_WORD_CHARACTER = /[A-Za-z0-9]/;
const BULLET_LINE = /^(?:[-*+•·●▪◦‣⁃]\s+|\d{1,3}[.)、]\s*|[一二三四五六七八九十]+[、.]\s*)/;
const EXPLICIT_HEADING = /^(?:【[^】]+】|第[一二三四五六七八九十0-9]+(?:章|节|部分|条)(?:\s+[^\n]{0,24})?|[^\n]{1,30}[：:])$/;
const FIELD_LABEL_LINE = /^(?!https?:\/\/)[^：:\n]{1,30}[：:]/iu;
const SENTENCE_END = /[。！？；][”’」』）)]?$/u;

export function readablePdfText(bodyText: string, sourceUrls: string[] = []): string {
  let text = bodyText;
  for (const sourceUrl of sourceUrls) {
    if (!/^https?:\/\//i.test(sourceUrl) || sourceUrl.trim() !== sourceUrl) continue;
    const escaped = Array.from(sourceUrl, (character) => character.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
      .join('[ \\t]*(?:\\r?\\n[ \\t]*)?');
    const wrappedUrl = new RegExp(escaped, 'g');
    text = text.replace(wrappedUrl, (match) => /[\r\n]/.test(match) ? sourceUrl : match);
  }

  const lines = text.split(/(\r\n|\n|\r)/);
  if (lines.length < 3) return text;

  let readable = lines[0];
  let previousLine = lines[0];
  for (let index = 1; index < lines.length; index += 2) {
    const separator = lines[index];
    const nextLine = lines[index + 1] ?? '';
    const left = previousLine.replace(/[ \t]+$/, '');
    const right = nextLine.replace(/^[ \t]+/, '');
    const previousCharacter = left.at(-1) ?? '';
    const nextCharacter = right[0] ?? '';
    const previousText = previousLine.trim();
    const nextText = nextLine.trim();
    const blankBoundary = left.length === 0 || right.length === 0;
    const explicitBoundary = BULLET_LINE.test(previousText) || EXPLICIT_HEADING.test(previousText)
      || BULLET_LINE.test(nextText) || EXPLICIT_HEADING.test(nextText)
      || FIELD_LABEL_LINE.test(nextText) || SENTENCE_END.test(previousText);
    const urlBoundary = /https?:\/\/\S*$/i.test(left);
    const cjkWrap = (CJK_CHARACTER.test(previousCharacter) || CJK_PUNCTUATION.test(previousCharacter))
      && (CJK_CHARACTER.test(nextCharacter) || CJK_PUNCTUATION.test(nextCharacter));
    const latinWords = LATIN_WORD_CHARACTER.test(previousCharacter) && LATIN_WORD_CHARACTER.test(nextCharacter);

    if (!blankBoundary && !explicitBoundary && !urlBoundary && cjkWrap) {
      readable = readable.replace(/[ \t]+$/, '') + right;
      previousLine = left + right;
    } else if (!blankBoundary && !explicitBoundary && !urlBoundary && latinWords) {
      readable = readable.replace(/[ \t]+$/, '') + ` ${right}`;
      previousLine = right;
    } else {
      readable += `${separator}${nextLine}`;
      previousLine = nextLine;
    }
  }
  return readable;
}
