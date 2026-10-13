# DECISIONS.md

1. **Indexes.**
   On `employees`, I created a unique index on `emp_code` (guaranteeing identity uniqueness and speeding lookups) and compound indexes on `(department, emp_code)` and `joined_on` to accelerate filtering. On `attendance_logs`, I created a unique compound index on `(emp_code, date)` to enforce single daily presence and address records; `(date: -1, emp_code: 1)` to provide index-backed sorting for paginated listings; and `(emp_code: 1, punch_in: -1)` for punch-out lookups. I considered and rejected a standalone index on `status` because its low cardinality offers poor index selectivity.

2. **Punch-in race.**
   Both requests compute the same attendance date and attempt `insert_one`. The database's unique compound index on `(emp_code, date)` is the serialization barrier: MongoDB accepts the first write (yielding 201 Created) and rejects the second with a `DuplicateKeyError`. Our application catches this exception and returns 409 Conflict, completely eliminating check-then-insert race windows.

3. **Ties.**
   Using MongoDB's `$setWindowFields` with the `$rank` function, employees with identical `total_late_minutes` share the same rank, and subsequent ranks are skipped (e.g., 1, 2, 2, 4). Because `limit` filters on `rank <= limit`, all tied employees at that cutoff rank are included. The response may return more rows than `limit`, as required by the specification.

4. **Headcount.**
   The aggregation begins on `employees` (filtering `joined_on <= month_end`), not `attendance_logs`. It groups employees by department to establish baseline headcount before performing a left outer `$lookup` into `attendance_logs`. Unwinding with `preserveNullAndEmptyArrays: true` ensures employees without attendance records are fully counted in headcount.

5. **One thing you would change for 100x data.**
   At 100x scale (~10M documents), I would maintain daily and monthly pre-aggregated rollup collections updated atomically on punch-out and regularizations, replacing expensive multi-stage `$lookup` and `$group` pipeline scans with indexed point reads.
