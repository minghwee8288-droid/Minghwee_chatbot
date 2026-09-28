import { Pill, Shell } from '@/components/Shell';
import { requireViewer } from '@/lib/auth';
import { rules } from '@/lib/queries';

const EXPLAIN: Record<string, string> = {
  price_policy: 'Whether the bot may quote this service’s fee (stated) or must defer to an agent (withheld).',
  price_nationality: 'The nationalities we hold a fee for, on a service priced per nationality.',
  salary_floor: 'Minimum monthly salary (SGD) a helper of this nationality can be placed at.',
  contact: 'Contact details the agency publishes.',
};

function show(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export default async function RulesPage() {
  const viewer = await requireViewer();
  const list = await rules();
  const groups = [...new Set(list.map((r) => r.rule_type))];
  return (
    <Shell viewer={viewer} active="/rules">
      <h1 className="mb-1 text-xl font-semibold">Rules</h1>
      <p className="mb-4 text-sm text-slate-500">
        The pricing and contact rules the bot reads (cb_kb_rules). {list.length} rules. Locked rules
        are developer-only and could never be changed from here.
      </p>
      <div className="space-y-6">
        {groups.map((g) => (
          <section key={g}>
            <h2 className="font-medium">{g}</h2>
            <p className="mb-2 text-xs text-slate-500">{EXPLAIN[g] ?? ''}</p>
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
                  <tr>
                    <th className="px-3 py-2">Service</th>
                    <th className="px-3 py-2">Nationality</th>
                    <th className="px-3 py-2">Value</th>
                    <th className="px-3 py-2">Locked</th>
                    <th className="px-3 py-2">Last changed</th>
                  </tr>
                </thead>
                <tbody>
                  {list
                    .filter((r) => r.rule_type === g)
                    .map((r) => (
                      <tr key={r.id} className={`border-t border-slate-100 ${r.locked ? 'bg-slate-50' : ''}`}>
                        <td className="px-3 py-2">{r.service_type}</td>
                        <td className="px-3 py-2">{r.nationality}</td>
                        <td className="px-3 py-2 font-mono text-xs">{show(r.value)}</td>
                        <td className="px-3 py-2">
                          {r.locked ? <Pill tone="red">🔒 locked</Pill> : <span className="text-slate-400">—</span>}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-xs text-slate-500">
                          {r.updated_at.slice(0, 16)} · {r.updated_by}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      </div>
    </Shell>
  );
}
