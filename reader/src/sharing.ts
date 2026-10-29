const ITEM_ID = /^item-[a-f0-9]{24}$/;
const OCCURRENCE_ID = /^occ-[a-f0-9]{24}$/;
const ISSUE_ID = /^issue-[a-f0-9]{24}$/;

export function itemSharePath(itemId: string, occurrenceId: string): string {
  if (!ITEM_ID.test(itemId) || !OCCURRENCE_ID.test(occurrenceId)) {
    throw new TypeError('A valid item and occurrence ID are required to share a message.');
  }
  return `share/items/${itemId}/${occurrenceId}/`;
}

export function issueSharePath(issueId: string): string {
  if (!ISSUE_ID.test(issueId)) throw new TypeError('A valid issue ID is required to share an issue.');
  return `share/issues/${issueId}/`;
}
