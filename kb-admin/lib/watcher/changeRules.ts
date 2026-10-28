/**
 * How a change to an existing entry is asked for (2026-10-16): never a
 * rewritten answer - only the facts to add and the sentences the page
 * contradicts. kb-admin builds the entry from them (lib/watcher/additive.ts).
 * Shared by the first call and the merge call (lib/watcher/overlap.ts).
 */
export const UPDATE_RULES = [
  '- To change an existing entry, set target_entry_id to its id and DO NOT rewrite it. Give only:',
  '  "add": each NEW fact from the page that the entry does not state, as one plain sentence, with "after" (the existing sentence of the entry it belongs after, copied exactly, or "" for the end) and its source_passage;',
  '  "replace": only where the page DIRECTLY CONTRADICTS a sentence of the entry (a different figure, date or rule for the same thing): "old" is that whole sentence copied exactly from the entry, "new" its corrected version, and source_passage the page words that contradict it, including what the figure is for.',
  '  Everything else in the entry stays exactly as it is. Never replace a sentence to reword, shorten or tidy it, and never add a fact the entry already states. If there is nothing to add and nothing contradicted, return no draft for that entry.',
].join('\n');
export const NEW_SHAPE = '{"target_entry_id":null,"question":string,"answer":string,"source_passage":string,"service":string,"audience":string,"nationality":string,"why":string}';
export const UPDATE_SHAPE = '{"target_entry_id":string,"add":[{"sentence":string,"after":string,"source_passage":string}],"replace":[{"old":string,"new":string,"source_passage":string}],"service":string,"audience":string,"nationality":string,"why":string}';
