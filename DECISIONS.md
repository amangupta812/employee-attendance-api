# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
On `employees`: unique `emp_code` (guarantees unique employee code); compound `(department, joined_on)` and `(joined_on)` (backs department summary and trend without COLLSCAN). On `attendance_logs`: unique `(emp_code, date)` (enforces at most one daily record and eliminates punch-in race conditions); `(date: -1, emp_code: 1)` (backs attendance list pagination and sorting); `(emp_code, punch_in: -1)` (backs punch-out lookup across midnight); `(date, late_minutes)` (backs late leaderboard).
*Rejected:* An index on embedded `history` fields, as audit entries are append-only and never queried or filtered independently.

2. **Punch-in race.**
Both requests independently validate the employee and compute the attendance date. Both attempt `insert_one`. Because `(emp_code, date)` has a database-level unique index, MongoDB atomically commits one insert (returning 201) and rejects the other with a `DuplicateKeyError`. Our code catches this exception and returns 409 Conflict. No check-then-insert race window exists.

3. **Ties.**
It returns both employees. Under standard competition ranking (`$rank` in `$setWindowFields`), tied employees share the same rank and subsequent ranks are skipped (e.g. 1, 2, 2, 4). Per contract, `limit` filters *after* ranking (`rank <= limit`), returning all tied rows at the cutoff, ordered secondarily by `emp_code`.

4. **Headcount.**
The pipeline originates on `employees` filtering `joined_on <= end_of_month`, and groups by `department` with `headcount: {"$sum": 1}`. Logs are joined via `$lookup`. Employees with zero logs yield an empty logs array, contributing 0 to present days, but are fully preserved in department headcount.

5. **One thing you would change for 100x data.**
I would replace on-the-fly aggregations with incremental event-driven rollups (using MongoDB Change Streams to update pre-aggregated summary collections on punch/regularization events), and shard `attendance_logs` on `(emp_code, date)` to distribute write throughput across multiple shards.
