# Decisions

1. **Indexes.** Unique `employees.emp_code` supports identity lookups; `(department, emp_code)` supports filtered lists;
   department/join-date and join-date indexes support headcount queries. Unique attendance `(emp_code, date)` prevents
   duplicate days and supports employee-month queries; date/employee supports list order and trends; employee/punch-in
   supports punch-out; status/date supports filtered lists; date/late-minutes supports the leaderboard. I rejected
   redundant single-field indexes because they add storage and write cost without covering a new access pattern.

2. **Punch-in race.** Both requests can attempt insert. The unique index lets one succeed (201); MongoDB rejects the
   other as duplicate, which the API maps to 409. No check-then-insert assumption is needed.

3. **Ties.** `$rank` assigns equal totals the same rank and skips the next rank. Filtering by `rank <= limit` returns every
   tie at the cutoff, sorted by employee code.

4. **Headcount.** Department summary starts with employees joined before the next month, then performs a left lookup for
   logs. Preserving an empty lookup counts people with no logs; the join-date cutoff excludes future joiners.

5. **100x data.** I would add incrementally refreshed monthly rollups or a reporting store, while retaining raw logs as
   the source of truth for corrections and audit history.
