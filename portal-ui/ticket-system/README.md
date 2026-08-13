# Ticket System module

Self-contained Ticket Management UI for the Ming Hwee OS portal. Copy this
folder into the portal's source tree and import one component.

```jsx
import TicketSystemModule from './ticket-system';

<TicketSystemModule supabaseClient={supabase} />
```

`supabaseClient` is the portal's already-initialised `@supabase/supabase-js` v2
client. The module creates none of its own, holds no global state, and adds no
dependencies beyond `react`, `recharts` and `@supabase/supabase-js`. Styling is
Tailwind utility classes only — no CSS files, no inline style objects.

## What it touches

| Table | Access |
| --- | --- |
| `cb_tickets` | read; writes **only** `status`, `priority` and `assigned_agent_id` (plus `updated_at` when the column exists) |
| `wp_chat_conversations` | read |
| `wp_chat_messages` | read |
| `cb_handovers` | read |
| `profiles` | read (`id`, name column, `archetype_key`, `status`) |

Nothing else is queried. No tables, views or functions are created.

## Schema notes

These were verified against the live database and differ from the original
spec. They are the reason several things are written the way they are.

- **`service_type` is `text[]`** on `cb_tickets`, holding snake_case values —
  `new_hiring`, `direct_hiring`, `replacement`, `transfer`, `renewal`,
  `home_leave`, `passport_renewal`, `dispute_salary`, `dispute_assault`,
  `fee_enquiry`, `salary_enquiry`. Filtering uses `.contains()`. Title-case
  values match nothing.
- **`profiles` has `display_name`, not `full_name`.** `utils/profiles.js`
  negotiates the column once per client and caches it, so the module works
  either way. Selecting both at once fails the whole query with `42703`.
- **`profiles` is platform-wide, not a staff table.** Of 37 rows, only 25 are
  assignable people; the rest are employers, suppliers, partners, transporters
  and runners, and several names appear twice as an `active`/`suspended` pair.
  The assignment dropdown filters to `ASSIGNABLE_ARCHETYPES`
  (`sales`, `admin`, `manager`, `super_admin`) with `status = 'active'` — see
  `constants.js` to widen it. Every ticket and handover in the data today is
  assigned to a `sales / active` profile.
- **`direction` is `'inbound'`/`'outbound'`**, not `'in'`/`'out'`.
- **`conversation_id` is an integer**, not a uuid.
- **`captured_info` is nested**: `{contact, details, notes}` plus top-level
  `topic_key`, `enquiry_type`, `also_topics[]`, `follow_ups[]`. A flat
  key/value renderer prints `[object Object]`.
- **Ticket references are `CB-YYYY-NNNN`.**
- **`priority` is `high` / `medium` / `low`.** It was `urgent`/`normal`;
  `portal-ui/migrations/001_priority_three_levels.sql` records the change, all
  three phases of which have been applied — `cb_tkt_priority_check` now permits
  the three values and nothing else. The chatbot's `priority_for()`
  (`app/services/ticket.py`) sets it once at insert — `high` for
  `dispute_assault`, `medium` for everything else — and never revisits it. The
  drawer's Priority select is how a ticket is re-triaged afterwards, and
  nothing in the bot overwrites that.
- `cb_tickets` also carries `resolved_at`, `resolved_by_id` and
  `resolution_note`. They are displayed read-only when present; nothing here
  writes them. Wiring the resolution note to the Save action is the obvious
  next step if the portal wants it.

## The bot-context behaviour

This is the part that is not obvious from the schema and matters operationally.

The chatbot re-reads its open tickets every turn. While a ticket sits in `open`
or `in_progress`, the bot **acknowledges** messages on that ticket's topic
instead of answering them — the client is waiting on a human. Moving the ticket
to `resolved` or `closed` hands the topic back to the bot.

`BotContextBanner` says so in the drawer, and the table marks live tickets
"Bot paused". The topics it lists come from `captured_info.topic_key` and
`captured_info.also_topics`, never from the `service_type` column: a topic the
column's check constraint cannot hold (a job seeker, an attachment, a question
the bot could not answer) is filed under a substitute, and only `captured_info`
carries what the client actually asked about. `enquiry_type` is surfaced
explicitly when that substitution happened.

`FollowUpsList` shows `captured_info.follow_ups` — what the client said about
the topic *after* the ticket was raised, which nobody has replied to.

## Structure

```
ticket-system/
  index.jsx                     the single public export
  hooks/
    useTickets.js               list: filters, sorting, pagination
    useTicketDetail.js          one ticket + conversation + messages + handovers
    useAgents.js                profiles for the assignment dropdown
    useDashboardStats.js        counts for the KPI cards and charts
  components/
    TicketDashboard.jsx         5 KPI cards + donut + bar
    TicketFilters.jsx
    TicketTable.jsx
    TicketDetailDrawer.jsx
    ConversationThread.jsx
    HandoverSection.jsx
    CapturedInfoPanel.jsx
    BotContextBanner.jsx        why the bot is quiet on this topic
    FollowUpsList.jsx           messages received while the ticket was held
    StatusBadge.jsx  PriorityBadge.jsx  ServiceTypePill.jsx
    EmptyState.jsx  ErrorState.jsx  LoadingSkeleton.jsx  Toast.jsx
  utils/
    constants.js  formatters.js  profiles.js
```

`TicketTable` is presentational — list state lives in `index.jsx` so the drawer
can refetch it after a save and the dashboard can refresh alongside it.

## Design

- Timestamps render in Singapore time via `Intl.DateTimeFormat`; the database
  stores UTC.
- Brand colours: MH Blue `#0D7AD2`, Dark Blue `#003E60`, Red `#DF0000`.
- Font is inherited (the portal loads Lato globally).
- The donut palette is capped at seven hues plus a grey "Other". It was checked
  for colourblind separation against both a light and a dark surface — worst
  adjacent pair ΔE 9.4 (deuteranopia), normal-vision floor 31.1, every slot
  above the 3:1 contrast floor. Reordering `SERVICE_CHART_COLORS` or adding an
  eighth hue invalidates that.
- Chart mount animation is off: a `ResponsiveContainer` that settles its width
  mid-animation can leave marks at zero, painting axes over an empty plot.

## Failure behaviour

Every query handles its error. Label lookups (customer, agent name) degrade to
a dash rather than hiding the tickets they label; the ticket list and the
detail body surface a retryable `ErrorState`. The save sends `updated_at`
optimistically and retries without it on the one error that means "no such
column", so a schema without it cannot reject the status change.
