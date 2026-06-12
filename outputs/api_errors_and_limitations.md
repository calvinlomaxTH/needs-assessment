# API Errors and Data Limitations

## General limitations

- HUD PIT now always emits one annual row for each requested HUD year for total, sheltered, and unsheltered homelessness.
- When HUD CoC parsing fails or no CoC codes are supplied, the script uses state-level fallback rows when possible.
- Medicaid coverage is computed from ACS C27007 Medicaid/means-tested public coverage, not from public coverage.
- HRSA, SAMHSA, and NCES are now automated best-effort pulls. Review parsed rows before publication.
- Internal client, utilization, staffing, and qualitative data cannot be pulled from government APIs.

## Run notes

- SAMHSA exact county search requires FindTreatment county ID or coordinates; using state search with county-name filtering.

## Captured errors

No API errors captured.