/**
 * The one place a stored code becomes the words a person reads. Display only:
 * filters, links and queries always carry the raw code, never these labels.
 */

export type LabelKind = 'service' | 'audience' | 'nationality' | 'entryType' | 'maintainedBy';

const SERVICES: Record<string, string> = {
  new_hiring: 'New hiring',
  direct_hiring: 'Direct hiring',
  replacement: 'Replacement',
  transfer: 'Transfer',
  transfer_employer: 'Transfer (employer)',
  renewal: 'Work permit renewal',
  passport_renewal: 'Passport renewal',
  home_leave: 'Home leave',
  insurance: 'Insurance',
  general: 'General',
  fee_enquiry: 'Fee enquiry',
  salary_enquiry: 'Salary enquiry',
  dispute_salary: 'Salary dispute',
  dispute_assault: 'Assault report',
  candidate_new_hiring: 'Helper registration',
  candidate_registration: 'Helper registration',
};

const NAMES: Record<Exclude<LabelKind, 'service'>, Record<string, string>> = {
  audience: { candidate: 'Helper', employer: 'Employer', all: 'Everyone' },
  nationality: { PH: 'Philippines', ID: 'Indonesia', MM: 'Myanmar', all: 'All nationalities' },
  entryType: { qa_pair: 'Question and answer', document_chunk: 'Document passage', table_unit: 'Table' },
  maintainedBy: { loader: 'Imported by the loader', ui: 'KB Admin', '(none)': 'Not set' },
};

/** "passport_renewal" -> "Passport renewal"; an unmapped service reads as words. */
function serviceLabel(code: string): string {
  if (SERVICES[code]) return SERVICES[code];
  const words = code.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : code;
}

/** The readable name for a code. Anything unmapped (other than a service) shows the code itself. */
export function label(kind: LabelKind, code: string | null | undefined): string {
  if (code === null || code === undefined || code === '') return '—';
  if (kind === 'service') return serviceLabel(code);
  return NAMES[kind][code] ?? code;
}
