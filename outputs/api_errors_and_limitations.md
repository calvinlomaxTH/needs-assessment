# API Errors and Data Limitations

## General limitations

- Each source determines its own most recent available data; years will not necessarily match across sources.
- ACS data requests require a valid Census API key in this environment.
- The Census API key is masked in logs and metadata.
- ACS latest-year detection probes variables.json and avoids future-year retry spam.
- ACS URLs are built without double slashes.
- ACS requests are chunked to prevent missing data from the Census API variable limit.
- ACS MOE variables are optional; unavailable MOE variables are omitted without failing the indicator.
- ACS special values such as -555555555 are converted to blank/null.
- ACS estimates are period estimates and include margins of error when available.
- ACS calculated percentages do not include derived margins of error.
- ACS sum margins of error are approximated using square root of sum of squared MOEs.
- PUBLIC_COVERAGE_RATE is not Medicaid-only coverage.
- MEDICAID_COVERAGE_RATE is left as a placeholder unless a Medicaid-specific source is connected.
- BLS LAUS annual average is preferred. If M13 is not returned, the script calculates a complete-year monthly average; if that is unavailable, it uses the latest monthly value and labels it.
- CDC PLACES values are modeled estimates.
- Internal and qualitative data cannot be filled from government APIs.

## Run notes

No run notes captured.

## Captured errors

No API errors captured.