# Diagnostics

This folder contains ad-hoc operational scripts for manual OData and 1C investigations.

Rules:
- Do not name diagnostic scripts `test_*.py`; automated pytest collection is limited to `tests/`.
- Do not hard-code credentials. Use `ODATA_USERNAME`, `ODATA_PASSWORD`, `ODATA_BASE_URL`, or the runtime UI config.
- Treat scripts here as operator tools, not application code.
