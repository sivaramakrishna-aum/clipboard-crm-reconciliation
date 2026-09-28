# clipboard-crm-reconciliation

A pipeline for the Clipboard Sales Operations Analyst assessment. It scrapes every Bellhaven Senior Living community from the assessment website, reconciles them against the Bellhaven CRM Sandbox API, and queues proposed fixes, each with evidence, in a local Streamlit review app. **The CRM is written only for proposals a named reviewer approved, after a second explicit confirmation, with writes enabled locally. Every write is verified by reading the account back.**

## Sources (inspected 2026-09-27)

| | |
|---|---|
| Website | https://analyst-assessment-production.up.railway.app |
| API base | https://analyst-assessment-production.up.railway.app/api/v1 |
| API docs | https://analyst-assessment-production.up.railway.app/api/docs (spec: `/api/openapi.json`) |

**API facts from the docs and live responses**
* **Auth:** `Authorization: Bearer <token>`. The unauthenticated 401 body says: *"Provide your candidate token as: Authorization: Bearer <token>"*.
* **`GET /accounts`** filters: `q, city, state, zip, street, parent_id`, plus `page` (default 1) and `page_size` (default 50).
* **`POST /accounts`** returns 201. There's also `GET /accounts/{id}`, `PATCH /accounts/{id}` and `GET /me`, and contacts endpoints that this project doesn't need.
* **No DELETE endpoint** exists, and the client has no delete method.
* **Not documented:** the spec gives no request or response bodies. See "Schema handling" below.

**Website facts**
* `/communities` is paginated (3 pages, "34 communities listed").
* Detail pages hold the address (`street<br>City, ST ZIP`) and care offerings (`span.badge`).
* The home page says "35 communities" and links to **Bellhaven Meadows of Findlay**, which is *not* in the directory. The scraper collects links from the home and about pages as well as every directory page, and marks off-directory communities in the evidence.

## Architecture

```
scraper/bellhaven.py      site ─► 35 Location records (name, street, city, state, ZIP, care offerings, source URL)
review_app/crm_client.py  Bearer auth, pagination, FIELD_MAP, schema gate, guarded create/update,
                          read-back verification, idempotent apply_proposal(), `inspect` / `verify` CLIs
matcher/reconcile.py      matching + classification ─► Proposals with evidence; DecisionStore; pipeline CLI
matcher/billing.py        pure rules: CHOW vs re-parent vs manual review; duplicate survivor selection
review_app/app.py         Approve / Reject (named reviewer), then confirm + Apply for approved items
decisions/ledger.jsonl    append-only decisions + applied operations (committed to a private repo)
.github/workflows/daily.yml  daily read-only run; writes are impossible there
```

## Setup (Windows PowerShell)

```powershell
cd "D:\Main Resumes\SDE\old\clipboard-crm-reconciliation"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env        # paste your token into CRM_API_TOKEN; keep CRM_WRITE_ENABLED=false
```

## Run

```powershell
python -m pytest -q                                   # unit tests
python -m review_app.crm_client inspect               # READ ONLY: live schema check (values redacted)
python -m scraper.bellhaven                           # READ ONLY: writes data\locations.csv
python -m matcher.reconcile --dry-run                 # READ ONLY: scrape + CRM + proposals
streamlit run review_app\app.py                       # review
python -m review_app.crm_client verify                # READ ONLY: re-check every applied write
```

To apply approved changes: set `CRM_WRITE_ENABLED=true` in `.env`, restart the app, then tick the confirmation box and click **Apply** on each approved item. Set it back to `false` afterwards.

## Schema handling (because the spec has no body schemas)

`FIELD_MAP` in `review_app/crm_client.py` is the one place that names CRM fields:
* The hierarchy and billing fields (`status`, `lifetime_revenue`, `outstanding_ar`, `chow_current_account`, `duplicate_of_account`) come from the assessment instructions.
* `parent_id`, `street`, `city`, `state` and `zip` mirror the documented list filters.

Three safeguards cover the gap:
1. `crm_client inspect` prints the real response envelope, record keys and value types (no values), and checks every mapped field exists.
2. The client **refuses any write** until that same check passes against live records.
3. Every create or update is **read back and compared**. A mismatch raises and the operation is not marked as applied.

The list response may be a bare list or an object with exactly one list of records. Anything else stops with an error rather than a guess.

## Matching strategy

1. **Normalize** names (case, punctuation, filler words), streets (Street→st, Avenue→ave, North→n, …) and ZIPs (first 5 digits).
2. **Primary match:** same ZIP and street similarity ≥ 0.90.
3. **Fallback:** same city and state with name similarity ≥ 0.95.
4. Accounts already carrying `duplicate_of_account` or `chow_current_account` are retired and excluded, so resolved items stay resolved.
5. The parent is the single top-level account named "Bellhaven Senior Living", or `BELLHAVEN_PARENT_ACCOUNT_ID` if set. If it's ambiguous, the run stops.

| Classification | Trigger | Proposal |
|---|---|---|
| Confident match | One candidate, name OK, correct parent | none |
| Name mismatch | Name similarity < 0.90 | PATCH `name` |
| Parent mismatch | Parent ≠ Bellhaven parent | billing/CHOW rule |
| Missing in CRM | No candidate | POST a new account under the parent |
| Duplicate | ≥ 2 candidates for one location | mark the losing accounts |
| No longer listed | Active account under the parent that no location matched | flag only, no write |

## Billing / CHOW rule (exact)

* `lifetime_revenue > 0` **and** `outstanding_ar > 0` → **keep the old account unchanged**, create a new account under the correct parent, then set **only** `chow_current_account` on the old account to the new ID.
* Otherwise → **re-parent** the existing account (PATCH its parent only).
* Missing or ambiguous values (blank, `N/A`, text, NaN, negative) → **manual review**, with no operations.
* **Duplicates:** the losing account gets `duplicate_of_account = <survivor id>` and `status = Inactive`. The survivor is chosen in this order: not already retired, then under the correct parent, then highest lifetime revenue, then lowest ID.
* **Never delete.** There is no delete operation type, client method or API call.

## Approval, idempotency and interrupted requests

* A proposal's key is a hash of its classification, account IDs, location and target values. The same situation always gets the same key.
* Decisions need a reviewer name, can be recorded only once, and are written to SQLite and to `decisions/ledger.jsonl`.
* `apply_proposal` refuses anything not approved and skips operations already recorded as applied.
* Before any create, it looks for an existing account under the target parent with the same name, street and ZIP. For CHOW, it first checks whether the old account is already linked. So a POST that succeeded but whose response was lost, or a rerun on a fresh machine, adopts the existing account instead of creating a second one.
* Updates are skipped when the account already has the target values.
* GETs are retried. Writes are never retried automatically; you retry by clicking Apply again, which is safe for the reasons above.

## Persistence architecture (why the scheduled run cannot lose decisions)

* **Local SQLite** (`data/decisions.db`, git-ignored) is the review app's working store. It's backed up automatically before each pipeline run to `data/backups/` (last 14 copies).
* **Ledger** (`decisions/ledger.jsonl`, committed): an append-only log of decisions and applied operations. It contains only proposal keys, the reviewer name, timestamps and CRM IDs: no token, financials or addresses. On startup it's replayed into SQLite, so decisions survive a lost database or a new machine.
* **GitHub Actions** has no database of its own. It runs with an in-memory database, reads the committed ledger so decided proposals are not re-reported, and uploads the pending proposals as a private artifact that is kept for 7 days. It has `contents: read` only, never commits, and cannot write to the CRM.

There is no public or hosted database anywhere. Keep the repository **private**, and commit `decisions/ledger.jsonl` after each review session.

## Scheduling

`.github/workflows/daily.yml` runs at 13:00 UTC and can also be started with `workflow_dispatch`. It runs the tests, then the read-only reconciliation, and uploads the proposals artifact. It needs one repository secret, `CRM_API_TOKEN`, and optionally a repository variable, `BELLHAVEN_PARENT_ACCOUNT_ID`.

## AI usage disclosure

This project was built with help from an AI assistant (Claude). The assistant inspected the public site and API docs, wrote the code and tests, and ran the tests in its own sandbox. I reviewed the design and I own the final code. No endpoint in this project is taken from anywhere other than the official docs. Where the docs are silent (body schemas), the code checks against live data instead of assuming.

## Future improvements

* Fuzzy matching with `rapidfuzz`, plus address standardization.
* Bulk approve for high-confidence proposals, and a CSV audit export.
* Alerts when the daily run finds new proposals.
* Contract tests from a recorded, redacted API response once the schema is confirmed.
