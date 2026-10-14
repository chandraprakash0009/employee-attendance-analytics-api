# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
I created five indexes. `uq_employee_code` ensures employee codes are unique. `department_joined` supports department and joining-date filters. `uq_attendance_employee_date` prevents duplicate attendance records for the same employee and date. `attendance_date_employee` supports date-based attendance queries and sorting. `attendance_date_status_employee` supports filtering attendance by date and status. I considered a separate index on attendance `emp_code`, but rejected it because the unique compound index already starts with `emp_code`, making another index largely redundant.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests first check that the employee exists and then attempt to insert attendance for the calculated date. The unique compound index allows only one record for that employee and date. One request succeeds; the other raises `DuplicateKeyError`, which the code catches and returns as HTTP 409 Conflict.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The leaderboard uses competition ranking: tied employees share a rank, and the next rank is skipped. The limit applies to rank, not row count, so every employee tied at the cutoff is included. Employee code provides consistent ordering.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The summary starts with eligible employees, joins monthly attendance logs, and preserves employees without matching logs. They still contribute to headcount.

5. **One thing you would change** if this had to serve 100x the data.
For 100x the data, I would move leaderboard ranking into MongoDB using `$setWindowFields`, reducing Python-side processing.