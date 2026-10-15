/**
 * robots.txt, read the way well-behaved crawlers read it. PURE.
 *
 * The group for our user-agent token wins if there is one, else the "*" group.
 * Within a group the LONGEST matching Allow/Disallow rule wins, and Allow wins
 * a tie (RFC 9309). "*" and a trailing "$" are honoured in paths. An empty
 * Disallow allows everything. Crawl-delay is read and respected as a minimum
 * gap between our requests to that host.
 */

export type RobotsRules = { rules: { allow: boolean; path: string }[]; crawlDelaySeconds: number | null };

export function parseRobots(text: string, agentToken: string): RobotsRules {
  const token = agentToken.toLowerCase();
  type Group = { agents: string[]; rules: { allow: boolean; path: string }[]; delay: number | null };
  const groups: Group[] = [];
  let current: Group | null = null;
  let lastWasAgent = false;
  for (const rawLine of String(text ?? '').split(/\r?\n/)) {
    const line = rawLine.replace(/#.*$/, '').trim();
    if (!line) continue;
    const idx = line.indexOf(':');
    if (idx < 0) continue;
    const key = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    if (key === 'user-agent') {
      if (!current || !lastWasAgent) {
        current = { agents: [], rules: [], delay: null };
        groups.push(current);
      }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
      continue;
    }
    lastWasAgent = false;
    if (!current) continue;
    if (key === 'allow' || key === 'disallow') {
      if (key === 'disallow' && value === '') continue;
      current.rules.push({ allow: key === 'allow', path: value });
    } else if (key === 'crawl-delay') {
      const n = Number(value);
      if (Number.isFinite(n) && n >= 0) current.delay = n;
    }
  }
  const mine = groups.filter((g) => g.agents.some((a) => a !== '*' && token.includes(a)));
  const chosen = mine.length ? mine : groups.filter((g) => g.agents.includes('*'));
  return {
    rules: chosen.flatMap((g) => g.rules),
    crawlDelaySeconds: chosen.map((g) => g.delay).find((d) => d !== null) ?? null,
  };
}

function ruleMatches(rulePath: string, path: string): boolean {
  if (!rulePath) return false;
  const anchored = rulePath.endsWith('$');
  const body = anchored ? rulePath.slice(0, -1) : rulePath;
  const re = new RegExp('^' + body.split('*').map((p) => p.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + (anchored ? '$' : ''));
  return re.test(path);
}

/** May we fetch this path (+ query) under these rules? */
export function robotsAllows(rules: RobotsRules, pathWithQuery: string): boolean {
  let best: { allow: boolean; len: number } | null = null;
  for (const r of rules.rules) {
    if (!ruleMatches(r.path, pathWithQuery)) continue;
    const len = r.path.length;
    if (!best || len > best.len || (len === best.len && r.allow)) best = { allow: r.allow, len };
  }
  return best ? best.allow : true;
}
