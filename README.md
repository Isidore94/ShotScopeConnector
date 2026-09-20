# ShotScopeConnector

Standalone personal Shot Scope dashboard importer for a Windows mini PC. Fetches available round/hole/shot data, stores it in its **own** SQLite database, and writes CSV/JSON into a separate local Google Drive for desktop folder.

**Status:** implemented and offline-tested. Live Shot Scope authentication, Apple Watch response compatibility, Windows Credential Manager and Windows scheduling still need validation on the target PC. This is an **unofficial dashboard integration**, not an approved public Shot Scope API.

## Independent of Square

- Package: `shotscope_connector`; command: `shotscope`.
- Database: `shotscope.sqlite3` in dedicated local storage.
- Configuration: its own `.env` and `SHOTSCOPE_*` settings.
- Credentials: its own Windows Credential Manager entry.
- No new listening port, phone Shortcut, Bluetooth or Google API setup.
- Does not open `square.sqlite3`, read Square's generic settings or modify its code.
- Publish into `Golf Performance/ShotScope`, not `Golf Performance/Square`.

The intended flow is: upload the round through Shot Scope, run a scheduled outbound sync, then let Drive for desktop synchronize the generated files. Routine imports need no CSV export.

## Windows setup

Use a separate project folder. From it in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\shotscope.exe init
notepad .env
```

`init` creates a new config and refuses to overwrite an existing file. Set:

```dotenv
SHOTSCOPE_EMAIL=your-account-email
SHOTSCOPE_OUTPUT_DIR="G:/My Drive/Golf Performance/ShotScope"
SHOTSCOPE_DISTANCE_UNIT=unknown
```

The path is an **example**. Select an already-existing folder in your actual Drive for desktop location. Do not create a lookalike drive path. Leave `SHOTSCOPE_DATA_DIR` at the generated default or use another ordinary local directory outside all cloud-sync folders. It must not overlap the output directory in either direction.

Never put a password in `.env`. Verify the login locally:

```powershell
.\.venv\Scripts\shotscope.exe login
.\.venv\Scripts\shotscope.exe probe
```

`login` prompts without echo, performs normal HTTPS form login and only then stores the password using the explicit Windows Credential Manager backend. It saves no session cookies to disk. Authentication errors, changed login forms, 2FA/CAPTCHA or external redirects stop the operation; no bypass is attempted.

`probe` fetches one round, archives its returned JSON locally and reports counts of shots, GPS endpoints, club labels and pins. It does not import or publish that round. `ok` only means at least one shot has complete coordinate fields, not that every shot is accurate or the integration is fully validated. Compare the counts and distances with your actual Apple Watch round.

No uploaded rounds means there is nothing to validate yet. It does not mean the importer has proved shot-level support.

## Distance units

The reference implementation treats provider distances as meters, but this project deliberately leaves the setting `unknown` until you verify your responses. The app's display-unit preference is not proof of the units returned by the dashboard endpoint.

Until verified, `distance_source` and `remaining_source` are retained but `distance_m` and `remaining_m` remain empty. `gps_displacement_m` is a separate calculation from endpoints, not carry or a unit-validation shortcut. Once verified, set `m` or `yd` and re-fetch all returned rounds to update normalization.

## Import and publish

```powershell
.\.venv\Scripts\shotscope.exe sync
.\.venv\Scripts\shotscope.exe status
# Explicitly re-fetch every round returned by the account listing:
.\.venv\Scripts\shotscope.exe sync --all
# Re-fetch an older corrected round (replace 123 with its actual ID):
.\.venv\Scripts\shotscope.exe sync --round-id 123
# Retry publishing locally without contacting Shot Scope:
.\.venv\Scripts\shotscope.exe publish
```

A normal sync fetches new rounds plus the latest 20 returned rounds to capture edits. It preserves source revisions and replaces current hole/shot rows transactionally. Repeated identical imports do not duplicate shots. Changes in verified unit settings also trigger a new normalized revision when a round is re-fetched.

Unknown schema data is archived and flagged while previous valid records are preserved. Missing rounds are flagged, not deleted: the list might have become filtered or incomplete. There is no verified pagination contract, so `--all` means every **returned** round, not a guarantee of the provider's complete history. Unknown hole putt counts, exact penalties and strokes gained are not invented.

A failed Drive write remains pending. Sync retries pending publication before its network work; `publish` can also retry independently. Jobs are protected by an operating-system file lock. The data directory must be writable by the Windows account running the job.

## Output

```text
ShotScope/
  README.md
  manifest.json
  rounds.csv
  holes.csv
  shots.csv
  quality_warnings.csv
  rounds/<round_id>.json
  raw/<round_id>/<source_hash>.json
```

Read the manifest, its sync scope, failed/missing round lists and file hashes before comparing CSVs. Each local file is replaced atomically and the manifest is written last. Cloud sync order can differ, so hashes identify a mixed generation. Local write completion is **not** proof that the files reached Drive on the web.

Raw JSON preserves returned field values in a canonical reserialization; it is not byte-for-byte HTTP traffic. Authentication responses are not archived. Local source revisions are retained, while the cloud output only guarantees publication of current raw revisions (previously published versions may remain). This is not a complete database backup system.

## Scheduling after a successful live test

Use Windows Task Scheduler under the **same Windows user** that saved the credential and runs Google Drive for desktop. A different user will not automatically have those credentials or the same Drive mount.

Create a task with:
- Program: the full path to this repo's `.venv\Scripts\shotscope.exe`.
- Arguments: `--env "C:\actual\path\ShotScopeConnector\.env" sync`.
- Start in: this repo's actual directory.
- Trigger: at logon and repeat every 2 hours using Task Scheduler's repetition settings.
- Run only while that user is logged on for the initial Drive-compatible deployment.
- Do not start a second instance while a prior instance is running.

The CLI is a scheduled job, not a web server or background daemon. Normal results go to standard output and operational state is saved locally. It does not send notifications or email. Exit 0 means success; exit 2 means an operational failure or pending publication that needs attention. Authentication/rate blocks stop the run instead of retrying indefinitely. Test a reboot/sign-in and another real sync before assuming unattended operation.

## Credentials and privacy

```powershell
.\.venv\Scripts\shotscope.exe logout
```

This removes the saved password but retains local golf data. Nothing from your account, `.env`, GPS archive, SQLite files or logs belongs in Git. The account fingerprint in the local database prevents accidentally mixing accounts in the same archive; it is not a substitute for disk encryption.

No repository visibility settings are changed by this package. Use your repository's privacy controls separately. `.gitignore` helps prevent mistakes but is not an access-control boundary.

## Tests and limitations

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q shotscope_connector
```

Tests use synthetic records and fake network responses. The recorded test result in `docs/TEST_RESULTS.txt` describes the environment actually used. It is not proof of current provider support or Windows deployment.

The first real acceptance check is: account login, one actual uploaded Watch round, verified GPS/club/pin completeness and units, an import, then confirmation that the normalized files appear on Drive on the web.

See `THIRD_PARTY_NOTICES.txt` for protocol attribution and the reference project's license. This repository does not bundle a strokes-gained benchmark or provide automated swing diagnoses.
