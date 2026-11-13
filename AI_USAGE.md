# AI Usage

## AI tools used
- ChatGPT

## How AI assistance was used
- Helped interpret the assignment requirements and break the work into implementation and verification tasks.
- Reviewed portions of the Python ingestion, database, normalization, and reporting code shared during the conversation.
- Helped diagnose the orphan-event reporting mismatch. The quarantine table contained `lead_not_found` and `missing_phone` reasons, while the original report query counted only the literal reason `orphan`. The query was changed to count all rows remaining in `event_quarantine`.
- Suggested focused tests for phone/date normalization, effective disposition selection, and lead bucket classification.
- Helped prepare the submission documentation and command instructions.

## Verification
The implementation was run locally. The automated test command completed with 22 passing tests. Both data drops were re-run and previously processed lead files were skipped. The report command generated all five required report files.

AI suggestions were reviewed and tested locally rather than assumed to be correct. The developer is responsible for the final code, execution, test results, and submitted work.
