# Data contract (D1)

Machine-readable JSON Schemas are in `contracts/` and are generated from the pydantic models (`python -c "from claimtriage.contracts import write_schemas; write_schemas()"`).
`GET /v1/contract/fnol` serves the live FNOL schema.

| Boundary | Schema | Producer -> consumer |
|---|---|---|
| FNOL submission | `fnol.v1.schema.json` | caller -> API |
| Accepted | `run_accepted.schema.json` | API -> caller (HTTP 202 + `run_id`) |
| Status / result | `run_status.schema.json`, `run_result.schema.json` | API -> caller |
| Quarantine record | `quarantine_record.schema.json` | API -> reviewer |
| Retrieval | `retrieval_search_request/response`, `retrieval_cross_refs` | agents -> retrieval service |
| Feedback | `feedback.schema.json` | caller -> API |

## What happens to a bad record
Validation has two layers, both in `claimtriage/validation.py`:
1. **Field/type/enum rules** (pydantic): missing required fields, wrong types, unknown claim types, malformed dates, negative amounts.
2. **Cross-field rules**: `xfield.future_loss`, `xfield.outside_policy_period`, `xfield.cost_exceeds_idv`, `xfield.fir_missing`, `xfield.theft_drivable`,
   `xfield.injury_count`, `xfield.tp_damage` and others.

A record failing either layer is **never silently dropped and never sent to an agent**: the API answers `422` with the list of
error codes and stores a quarantine record (original payload, error list, timestamp, correlation id) readable at `GET /v1/quarantine` and
`GET /v1/quarantine/{id}`. Quarantine reasons are counted in `fnol_quarantined_total{reason}`.
A record that is valid but contains instruction-like or abusive text is accepted, **flagged**, and forced to the `investigation` route (rule R1) - see `claimtriage/guardrails.py`.
Free-text fields are sanitised before they reach a prompt and are passed to the model only as quoted data.

## Versioning
`fnol.v1`. A breaking change adds `fnol.v2` and the API accepts both for one release; the result carries the contract version it was produced under.
