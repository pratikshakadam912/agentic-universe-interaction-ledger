# Submission

## Candidate

- **Name:** Pratiksha Kadam
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 5 hours

## How to run

Run these commands from the repository root after cloning the project. Python must be installed.

```bash
# Install the test dependency
python -m pip install pytest

# Generate the sample input data (if it is not already present)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# Ingest the two drops in order
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# Generate the five reports
python -m solution.cli report --out reports/

# Run the automated tests
python -m pytest -q
```

The SQLite database is created at `data/interaction_ledger.db`. Keep this database between runs when performing incremental ingestion. To start with a fresh database, remove that database file and then run the ingestion commands again.

## Design

The pipeline has three main stages: ingestion, persistence/linking, and reporting.

1. **Lead ingestion:** CSV files are processed using the relevant workflow JSON schema. Headers and supported values are normalized, required fields are validated, and rejected rows are recorded with a reason. Valid leads are identified by `(workflow_id, SHA-256(normalized_phone))`. Repeated identities are merged; the first accepted row determines the lead's original `batch_id`.
2. **Event ingestion and linking:** JSONL events are stored in an event ledger using `event_id` as the unique key. Events are linked to leads through workflow and phone identity. Events that cannot be linked are placed in quarantine. Link resolution runs again after leads are ingested, allowing previously quarantined events to link when a later drop supplies the lead. Disposition codes are checked against `dispositions.csv`.
3. **Reporting:** Reports are built from persisted leads and linked events. Event timestamps are converted to IST for date-based reporting.

### Database schema

| Table                   | Purpose                                                                                                              |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `leads`                 | One record per workflow and hashed phone identity; stores the original batch, masked phone, and normalized lead data |
| `events`                | Event ledger with globally unique `event_id`, event type, workflow, call ID, timestamp, payload, and source file     |
| `event_lead_links`      | Links a stored event to a lead identity without copying the full phone number                                        |
| `event_quarantine`      | Tracks stored events that could not be linked, including the reason                                                  |
| `processed_lead_files`  | Records lead files already ingested, supporting rerun skipping                                                       |
| `processed_event_files` | Records event files already ingested, supporting rerun skipping                                                      |
| `lead_file_stats`       | Persists lead ingestion totals, rejections, duplicate merges, and warnings                                           |
| `lead_rejections`       | Stores rejected lead row metadata and reason, without storing rejected row PII                                       |

SQLite was chosen because the assignment permits SQLite or PostgreSQL and the pipeline can run locally without a separate database service. The lead primary key prevents duplicate lead identities within a workflow. The event primary key prevents duplicate event IDs. Separate link and quarantine tables keep event storage distinct from lead resolution, which is useful when events arrive before their leads.

PII handling: the normalized phone is hashed for matching and represented as a masked value in the lead record. Schema fields marked as PII are not stored as plaintext in the lead JSON data; for example, `customer_name` is stored as a hash.

## Decisions & assumptions

| Decision                                                                                                  | Why                                                                                                             | Alternative considered                                           |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Use SQLite                                                                                                | It satisfies the assignment's database requirement and keeps local setup simple                                 | PostgreSQL, which would require a separate service/configuration |
| Identify a lead by workflow plus SHA-256 of normalized phone                                              | Phone is the matching identity specified by the assignment; workflow keeps identities separate across campaigns | Store the full phone as a database key                           |
| Preserve the first accepted lead's batch ID after duplicate merges                                        | Keeps the lead attributed to its original batch while later rows enrich its data                                | Move the lead to the most recent batch                           |
| Merge only non-null values from later duplicate rows                                                      | Prevents missing later values from erasing previously captured information                                      | Replace the entire stored lead record                            |
| Quarantine unresolved events and retry linking after lead ingestion                                       | Supports events arriving before their corresponding lead                                                        | Permanently discard unresolved events                            |
| Treat the latest override as effective when any valid override exists; otherwise use the latest valid set | Implements the specified override precedence                                                                    | Always use the latest disposition event regardless of event type |
| Interpret timestamps without an offset as IST and report dates in IST                                     | Provides a consistent timezone for reporting                                                                    | Treat naive timestamps as UTC                                    |
| Count all remaining quarantine rows as orphan events in the quality report                                | The quarantine table contains unresolved events with reasons such as `lead_not_found` and `missing_phone`       | Count only rows whose reason is literally `orphan`               |

## Failure modes considered

- **Invalid lead values:** Required-field and phone validation failures are rejected with a reason. Invalid optional values are handled as warnings/null values by the lead validation flow.
- **Blank lead rows:** Fully blank rows are skipped.
- **Malformed event JSON:** Malformed non-empty event lines are counted and skipped.
- **Duplicate event IDs:** Duplicate events are dropped using the event ID uniqueness check and included in ingestion statistics.
- **Duplicate leads:** Valid rows with an existing workflow/phone identity are merged rather than inserted as another lead.
- **Events arriving before leads:** Unlinked events are quarantined and link resolution is retried after lead ingestion.
- **Unknown disposition codes:** They are counted in the quality report and are not used as effective dispositions.
- **Repeated ingestion:** Processed-file records allow already ingested files to be skipped on reruns.

**Known limitations / not verified:**

- The test run does not include a forced process termination (`kill -9`) during ingestion or a recovery test after such a termination.
- No large-scale performance or concurrent-ingestion test was run.
- The handling of disk-full conditions, database corruption, and simultaneous writers was not tested.
- The local clean-clone setup sequence is documented, but a separate clean-clone environment was not used for this verification.

## Testing

### Automated tests

Command:

```bash
python -m pytest -q
```

Result from the completed local run:

```text
22 passed in 0.07s
```

The tests cover:

- Valid and invalid Indian phone normalization.
- Supported date formats and invalid calendar dates.
- Latest override precedence over disposition sets.
- Latest disposition set when no override exists.
- Lead bucket classification for no calls, terminal disposition, all calls telephony-failed, and mixed call outcomes.

### Pipeline checks performed

- Ingested `data/drop_1` and `data/drop_2`.
- Re-ran both ingest commands and confirmed already processed lead files were skipped.
- Generated all five report files successfully.
- Inspected report headers and sample rows.
- Inspected the data-quality output and database counts to investigate and correct the orphan-event count.

Not tested: forced crash recovery, concurrent ingestion, high-volume performance, and a fresh-clone run in a separate environment.

- Verified database-level event immutability by attempting an UPDATE and confirming that the append-only trigger blocked it.

## Results

For the tested generated dataset:

- Lead rows: **2,772**
- Rejected lead rows: **108**
- Duplicate lead rows merged: **772**
- Field warnings: **122**
- Event lines read: **8,745**
- Malformed events: **8**
- Duplicate event IDs dropped: **237**
- Remaining quarantined/orphan events: **462**
- Unknown disposition events: **18**

The database contained **1,892 unique leads** and **8,500 stored events**. Of the stored events, **8,038 were linked** and **462 remained quarantined** at report time.

All five reports were generated:

- `batch_summary.csv`
- `daily_overview.csv`
- `lead_report.csv`
- `rejections.csv`
- `data_quality.json`

The automated test run took **0.07 seconds**. End-to-end ingestion and report-generation timings were not recorded, so no pipeline runtime is claimed.

One notable data finding was that the quarantine table used reasons `lead_not_found` and `missing_phone`, rather than the literal reason `orphan`. The quality report initially counted only the latter and showed zero. The report query was corrected to count all rows remaining in quarantine, producing the final count of **462**.

## If I had another day

1. Add integration tests for the complete two-drop workflow, report schemas, idempotency, and orphan relinking.
2. Add a forced-crash/restart test and verify that a partially processed file can be recovered safely.
3. Expand database integration tests to cover event immutability, event deduplication, and quarantine behavior.
4. Add tests for unknown dispositions, malformed event lines, and phone-less override linking.
5. Benchmark larger input volumes and optimize database access if needed.
6. Test the documented setup from a completely clean clone.
