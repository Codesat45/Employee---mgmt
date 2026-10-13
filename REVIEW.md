# Review of the starter implementation

The findings below are the contract and correctness defects addressed during review. The examples describe checks that
expose the behavior; validation should also cover the larger hidden dataset.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `_serialize_employee`, `_to_epoch_ms` | A BSON datetime can be treated as an integer epoch value, raising `TypeError` when listing seeded employees. | Call `GET /employees` against the supplied BSON sample. | Convert datetimes through the UTC datetime-to-epoch helper; accept integer epochs separately. |
| 2 | `_serialize_attendance` | Seeded records may omit `half_day`, `history`, or other optional stored values even though the response schema requires them. | List a legacy document without `half_day` or `history`. | Supply API defaults (`false`, empty history, null punches/work hours, zero minute totals) while serializing. |
| 3 | `_truncate_epoch_ms` | Float-based truncation can lose precision and is unnecessary for integer epoch milliseconds. | Test an instant with milliseconds just before a whole-second boundary. | Truncate with integer floor division before storage and calculations. |
| 4 | `punch_out` | Selecting an arbitrary open punch can close a punch that occurred after the requested punch-out instant; a closed latest eligible record must conflict. | Punch out before a future punch-in, or repeat a punch-out for a just-closed record. | Select the latest record with `punch_in <= punched_at`, return 404/409 appropriately, and close it with one conditional atomic update. |
| 5 | `punch_out` | Updating derived fields and `half_day` in separate writes exposes an inconsistent intermediate record and weakens race behavior. | Read the record while punch-out completes, or send two simultaneous punch-outs. | Compute all derived values first and write them together using `find_one_and_update`; the conditional update lets one caller win. |
| 6 | `regularize_attendance` | Optimistic concurrency based on `history: []` does not match legacy records where `history` is absent. | Regularize a seeded legacy record without a `history` field. | Match missing history with `$exists: false`; otherwise compare the existing history array so concurrent updates conflict. |
| 7 | `list_attendance`, `department_trend` | Date query values were compared as raw strings without validating the required canonical calendar-date format. | Submit an invalid date such as `2026-02-30` or a non-ISO form. | Validate `YYYY-MM-DD` calendar dates and return 422 for invalid or reversed ranges. |
| 8 | `employee_monthly` | Reading all matching logs and computing totals in Python bypasses the required database aggregation and needlessly transfers records. | Inspect implementation or run against a large employee history. | Aggregate from the indexed employee code and use an indexed attendance lookup to compute monthly totals and join-date-adjusted working days in MongoDB. |
| 9 | `department_summary` | Per-employee log queries create an N+1 pattern, and using the first day of the next month as an inclusive join cutoff counts future joiners. | Include a joiner on the first of the following month and employees with no logs. | Aggregate from employees, use a cross-collection indexed lookup, and match `joined_on < next_month_start`; preserve employees with no logs in the grouping. |
| 10 | `late_leaderboard` | Fetching logs and ranking them in Python is unbounded and does not provide the required MongoDB window-function implementation. | Compare the route against the 100k-record requirement and tie cases. | Group late minutes, join employee details, use `$rank` for competition ranking, then apply the rank cutoff so cutoff ties remain included. |
| 11 | `department_trend` | Building dates and querying each day in Python violates the database-side gap-filling requirement and can miss zero-headcount days for a known department. | Request a range containing weekends, no-log days, and employees who join later in the range. | Generate dates in an aggregation, calculate active headcount and daily totals in MongoDB, and compute the seven-row moving average with `$setWindowFields`. |
| 12 | `/admin/explain/{endpoint}` | Explanations were not consistently generated from the real endpoint pipelines and filters; some represented only a partial query. | Compare explain stages/filters with the endpoint request or search for a required IXSCAN. | Reuse the real query/pipeline builders and request `executionStats` explain with the same filters, sort, pagination, and ranking cutoff. |
| 13 | Trend and explain query parameters | The contract names the trend range parameters `from` and `to`; Python parameter names alone exposed `from_date` and `to_date`. | Call the documented `?from=...&to=...` form. | Bind the Python parameters to the contract names with FastAPI query aliases. |

## Checked and retained

- The unique compound attendance index on `(emp_code, date)` is the correct duplicate-punch guard; the insert catches its
  duplicate-key error rather than relying on a race-prone pre-check.
- The competition-rank cutoff must retain every row whose rank is within `limit`, including ties. A response can
  therefore contain more than `limit` rows.
- `work_hours` uses half-up rounding, and the half-day decision is made from the rounded value, not the unrounded
  duration.
