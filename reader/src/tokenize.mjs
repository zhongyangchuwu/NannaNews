// Shared by the offline builder and browser worker: never use locale-sensitive segmentation.
export function normalizeText(text) {
  return text.normalize('NFC').toLowerCase().replace(/ß/g, 'ss').replace(/ς/g, 'σ').normalize('NFC');
}

export function tokenize(text) {
  const tokens = [];
  const runs = normalizeText(text).match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]+|(?:(?![\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}])[\p{L}\p{M}\p{N}])+|[%_]+/gu) ?? [];
  for (const run of runs) {
    if (/^[%_]+$/u.test(run)) {
      const characters = [...run];
      tokens.push(...characters);
      for (let i = 1; i < characters.length; i += 1) tokens.push(characters[i - 1] + characters[i]);
    } else if (/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]+$/u.test(run)) {
      const characters = [...run];
      if (characters.length === 1) tokens.push(run);
      for (let i = 1; i < characters.length; i += 1) tokens.push(characters[i - 1] + characters[i]);
    } else {
      tokens.push(run);
    }
  }
  return tokens;
}

// MiniSearch.loadJSON must receive these same options; functions are not serialized.
export const searchOptions = {
  fields: ['title', 'text'],
  idField: 'id',
  storeFields: [],
  tokenize,
  processTerm: (term) => term, // tokenize already normalizes the complete input once.
  searchOptions: { combineWith: 'AND', boost: { title: 3 }, prefix: false, fuzzy: false },
};
