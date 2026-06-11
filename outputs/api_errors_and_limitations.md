# API Errors and Data Limitations

## General limitations

- Each source determines its own most recent available data; years will not necessarily match across sources.
- ACS requests are chunked to prevent missing data from the Census API variable limit.
- ACS estimates are period estimates and include margins of error.
- ACS calculated percentages do not include derived margins of error.
- ACS sum margins of error are approximated using square root of sum of squared MOEs.
- PUBLIC_COVERAGE_RATE is not Medicaid-only coverage.
- MEDICAID_COVERAGE_RATE is left as a placeholder unless a Medicaid-specific source is connected.
- CDC PLACES values are modeled estimates.
- BLS LAUS county series IDs are inferred and should be validated before publication.
- CDC WONDER/NVSS mortality data may suppress small counts and may require manual downloads.
- HRSA, SAMHSA, HUD, and NCES source schemas may require source-specific download handling or crosswalks.
- Internal and qualitative data cannot be filled from government APIs.

## Captured errors

- GET JSON failed: https://api.census.gov/data/2024/acs/acs5//variables.json params={} error=404: <!doctype html><html lang="en"><head><title>HTTP Status 404 ? Not Found</title><style type="text/css">body {font-family:Tahoma,Arial,sans-serif;} h1, h2, h3, b {color:white;background-color:#525D76;} h1 {font-size:22px;} h2 {font-size:16px;} h3 {font-size:14px;} p {font-size:12px;} a {color:black;} .line {height:1px;background-color:#525D76;border:none;}</style></head><body><h1>HTTP Status 404 ? Not Found</h1></body></html>