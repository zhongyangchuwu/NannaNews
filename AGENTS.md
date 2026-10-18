# Repository guidance

## Product and package boundary

- The installable Python product lives in `src/nannanews/`; the public historical reader is the separate `reader/` project.
- Use the `nannanews` CLI and the single `pyproject.toml` package definition. Keep templates, static assets and catalog data in package resources. Do not add root-level compatibility modules or duplicate dependency metadata.
- `README.md`, `docs/product.md`, `docs/operations.md` and `WORKFLOW.md` are the canonical user and operator docs. Update them when product behavior, lifecycle or deployment contracts change; keep the root README concise.

## Runtime and data safety

- Importing/configuring the application and showing CLI help must not create runtime directories, keys or databases. `nannanews migrate` is the explicit versioned migration path; `init-admin` explicitly initializes as needed. Serving and reads require a current schema and must not silently migrate or backfill.
- Keep each operator's database, uploaded files, source evidence, session key, paid-attempt prevention history and backups outside Git, package builds, container layers and public static output.
- Drain every writer before backup, restore or migration. Preserve the paid-attempt ledger and its source receipts; never erase it to retry a paid request or perform an in-place schema downgrade.
- CI must not receive model credentials, run collection or control production services. Static Pages accepts only a reviewed public whitelist export and is not a Python backend.
- Do not reintroduce email/SMTP delivery or external lecture synchronization. Keep collection opt-in and separate from approval and publication.

## Maintenance and licensing

- Install runtime dependencies with `python -m pip install .` and test dependencies with `python -m pip install '.[test]'`. Use focused behavioral checks during development and CI for integrated verification; never use production data or paid model calls for tests.
- Preserve attribution to [nik-nul/WebNews](https://github.com/nik-nul/WebNews) and the complete GNU GPL v3 text in `LICENSE`. Do not copy credentials, private workbooks, contacts, production data or another operator's services into this repository.
