# LG Sports Deck — Internal Domain and Synthetic Recording Tests

**Internal test code, not an integrated site or production system. Do not use for real users, personal information, camera streams, customer payments, customer email, or venue installation.**

## What's here

- `domain.py`: an isolated SQLite prototype for fictional `.invalid` accounts, Superuser-only staff permission grants, simulated paid registration, a test venue token, ordered check-in, provisional queue matching, match results, two-team score confirmation, Open Play-only individual win/loss aggregates and test-only replay/result outbox records.
- `recording.py`: a **single-process test coordinator** for one test recorder per court. It serializes duplicate starts, attempts to finalize a previous match before starting the next match on the same court, leaves the other court alone, reports synthetic failures rather than marking a bad recording ready, and restricts path lookup to participant/Superuser test identities. `SyntheticFFmpegBackend` records a generated test pattern or an explicitly supplied labelled stock sample—not a camera stream—and checks that the generated clip decodes. The test coordinator is not designed for an actual IP camera or deployment.
- `staging_bridge.py`: an **internal-only adapter** linking one fictional staging sandbox's *simulated* Open Play booking to the separate domain prototype. The separate `integrated/` prototype calls it through fictional test-only HTTP routes; it is not real account linkage, a payment webhook, event bus, or financial reconciliation. Staging and domain databases are separate; a production integration must handle reliable atomic events, provider status, retries, no-shows/cancellations, ordering and recovery.
- `retention.py`: opt-in **synthetic-file-only** seven-day deletion primitives. Tests explicitly provide a fictional anchor. The user has chosen video-ready time for the real policy, but these core primitives themselves still require an explicit fictional anchor and install no timer; the separate unified opt-in lab adds a sample-only loop. There is no deletion of cloud/NVR/backup copies. Its deletion controls are not production-hardened.
- `tests/`: independent automated checks, with temporary test databases and synthetic files. The end-to-end check joins a simulated booking to the check-in/queue/test-video/outbox/result path. It does not test a browser workflow across these components or use a real venue.

Queue selection and how pending-result players are grouped are **provisional**, not an approved final workflow. A test-only staff override can choose four waiting players and their teams; full queue reordering and automatic partner-persistence rules are not implemented. Rankings are aggregates only; final ranking sorting/tie-breaks and team-rating rules are undecided. A test venue token is not a deployable QR system. Paths and the `.invalid` outbox are not real private links or delivered messages.

## Reproducing internal checks

Python 3.10+ is required. In an isolated developer virtual environment, starting from the workspace directory that contains `lg_sports_deck`:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r lg_sports_deck/core/requirements-test.txt
export PYTHONPATH="$PWD"
export FFMPEG_TEST_BIN="$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
python -m pytest -q lg_sports_deck/integrated/tests lg_sports_deck/core/tests lg_sports_deck/staging/tests
```

On PowerShell, activate `.venv\Scripts\Activate.ps1`, set `$env:PYTHONPATH = (Get-Location).Path`, and set `$env:FFMPEG_TEST_BIN = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"` before running the same pytest command. FFmpeg is obtained through the development package `imageio-ffmpeg` and is used only to generate synthetic imagery. If the FFmpeg executable is unavailable, the two synthetic-video integration checks **skip**; a skipped test is not evidence that recording works. Staging tests may report an AnyIO deprecation warning.

Last internal run in the development environment: **29 tests passed** (15 domain/recording/retention, 2 cross-component integration, 4 internal portal HTTP, 8 staging). FFmpeg 7.0.2 generated and decoded sample test clips. These results do not validate real payment, email, camera hardware, privacy compliance, public hosting or production security.

## Not yet built or validated

- Real login/session security, companion account linking and individual consent, independent role authorization throughout an application, or remote access.
- Real GCash merchant/provider, payment expiry, webhooks, settlement, failed/duplicate payment handling and reconciliation.
- Real QR scanning, full queue administration/reordering, automatic partner persistence, dispute/correction workflows, team ratings, or production-ready queue UI.
- Real SMTP/Gmail sending, delivery retries and monitoring, secure expiring replay links and video playback/streaming.
- RTSP cameras/NVR, site network tests, robust recording process supervision, durable recovery, multi-process and multi-machine coordination, telemetry, encrypted storage and backup/deletion verification.
- A durable real-media retention scheduler and backup deletion for the chosen video-ready-plus-seven-days policy, Philippines privacy and consumer requirements, BIR/POS compliance, quotes or launch infrastructure.

Before any actual installation, security and legal review, selected equipment/provider, costs, configuration, full end-to-end tests and user approval are still needed. Never put secrets or real customer data in these test modules. Do not ask the user to test an incomplete milestone.
