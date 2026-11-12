# LG Sports Deck — fictional account and permissions prototype

**INTERNAL DEVELOPMENT ONLY. NOT A COMPLETED ONLINE TEST SITE, CLIENT INSTALLER OR PRODUCTION SECURITY SYSTEM.** Use fictional `.invalid` accounts and invented test passwords only. Do not enter real customer data, actual passwords, payment keys, camera credentials or client data. This is a separate prototype: it does **not** secure or replace the openly switchable Demo Superuser in `../integrated/` or `../staging/`.

## What is built in this isolated prototype

- Fictional player registration and sign-in; fictional Admin/Manager creation by Superuser; offline one-time Superuser bootstrap with **no default credentials and no bootstrap HTTP endpoint**. Registration does **not** verify an email and sends no message. Staff password assignment in this fixture is not an approved real onboarding method.
- Argon2id salted password hashes; random, opaque 12-hour test sessions stored only as token hashes; server-side revocation on sign-out, account disable or password change; new login replaces the previous browser session. The test HTTP implementation uses `Secure`, `HttpOnly`, `SameSite=Strict` host-prefixed session cookies when HTTPS mode is enabled. The CSRF value has a separate, non-HttpOnly cookie and is verified against its server-side hash and header; state-changing requests also require an explicitly configured exact Origin. Plain HTTP/insecure cookies are explicitly allowed **only for loopback developer tests**.
- A small, SQLite-backed, five-failure/five-minute per-account and peer test throttle, plus a minimal audit of selected events without passwords, session tokens or actual email bodies. These are test mechanisms, not production-scaled abuse prevention, a tamper-proof audit trail or a privacy policy. No client-supplied forwarded IP is trusted.
- Default-deny individual Admin and Manager grants, changed only by the Superuser and checked by the server against the same `core` SQLite database as the queue. The exposed actions are `bookings.view`, `session.view`, `queue.view`, `queue.manage`, `score.enter`, `rankings.view` and `settings.rates.edit`. Rate editing changes only the fictional Open Play rate. Revoked permissions are denied on subsequent requests; cancelling requests already in flight is not guaranteed. Extra features, pages and settings are not yet exposed or grantable. There is deliberately **no Admin/Manager replay grant**, even if they have every listed grant.
- Each player can record only their **own fictional recording-notice acknowledgement**. A simulated Open Play group checkout requires every selected fictional player to have an account and to have acknowledged the test notice; its participants and capacity are written in the same SQLite transaction. This is an engineering test, **not legal consent or a final companion-acceptance design**. No GCash provider, real charge, booking confirmation email or actual customer reservation exists.
- Authenticated test API connects that same simulated Open Play booking to venue-token check-in, provisional queue, match, score confirmation, ranking and synthetic replay authorization. A player cannot choose another account's identity via an HTTP header. Unsent replay/result outbox rows are fictional. A rental **match fixture** can be used by tests, but there is no authenticated rental booking/participant-association workflow in this prototype.
- Minimal internal English UI covers fictional registration, sign-in, account/password change, test notice and Open Play checkout, plus Superuser-only test staff/grant configuration and limited sample operations. Queue, scores, replay and several back-office operations are verified through test APIs, not available as a complete account-backed end-user interface.

## Internal tests and results

With `FFMPEG_TEST_BIN` explicitly set, the account suite exercises one-time bootstrap, fictional-only addresses, password/session storage, sign-in, cookie flags, cross-origin/CSRF rejection, role spoofing, concurrent failed-login throttling, expiry, password change, logout, disabled users, Superuser-only precise grant/revoke, simulated group booking atomicity and one remaining place, participant-only notice attribution, queue/score confirmations and pending rankings. It also checks authenticated synthetic replays for participants and Superuser but **not** Admin/Manager or outsiders, a rental fixture, and same-court rollover while the other synthetic court remains recording.

Run from the workspace root in an isolated test environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r lg_sports_deck/accounts/requirements-test.txt
export PYTHONPATH="$PWD"
export FFMPEG_TEST_BIN="$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
python -m pytest -q lg_sports_deck/accounts/tests lg_sports_deck/integrated/tests lg_sports_deck/core/tests lg_sports_deck/staging/tests
```

The latest combined run passed **43 tests**: 14 account tests (6 service and 8 HTTP) plus the previous 29 integrated/core/staging tests, with one AnyIO deprecation warning. The test dependencies are development dependencies, **not a reviewed production lockfile**. If FFmpeg is not explicitly available, synthetic-video tests skip; a skipped test does not verify recording. All tests use temporary fictional databases and generated patterns, not hardware. In internal browser QA, a fictional player signed up, signed in, acknowledged the test notice and simulated a ₱200 checkout; a fictional Superuser created an Admin and granted two actions, and the Admin viewed the sample booking. A 390×844 mobile account view was inspected. A browser-only checkbox-selection bug was found and fixed before the successful checkout; the later console had no errors or warnings. No user acceptance test was requested.

## Developer-only local setup (not client installation)

Use a new, disposable, isolated DB. Never point these commands at a real or existing venue database. After installing the test dependencies as above, from the workspace root:

```sh
mkdir -p lg_sports_deck/accounts/data/media
export PYTHONPATH="$PWD"
export LG_ACCOUNTS_TEST_DB="$PWD/lg_sports_deck/accounts/data/accounts-test.sqlite3"
export LG_ACCOUNTS_TEST_MEDIA="$PWD/lg_sports_deck/accounts/data/media"
python -m lg_sports_deck.accounts.bootstrap --db "$LG_ACCOUNTS_TEST_DB" --email owner@example.invalid --session-label "Fictional session"
# The CLI privately prompts for an invented TEST password and a fictional venue token.
export LG_ACCOUNTS_TEST_ORIGINS="http://127.0.0.1:8015"
export LG_ACCOUNTS_INSECURE_LOOPBACK=1
# Optional: enable a generated test pattern, never CCTV footage.
export LG_ACCOUNTS_SYNTHETIC_FFMPEG="$FFMPEG_TEST_BIN"
python -m uvicorn lg_sports_deck.accounts.devserver:app --host 127.0.0.1 --port 8015
```

Open `http://127.0.0.1:8015` on that same local computer only. This example intentionally uses **insecure loopback-only test cookies**; do not expose it to a network or put real data in it. The bootstrap is offline-only, refuses a second Superuser, and does not reset or migrate an existing DB. If HTTPS is used for an isolated development environment, omit `LG_ACCOUNTS_INSECURE_LOOPBACK`, explicitly configure the exact HTTPS Origin and provide TLS separately; this is still not approval for production. The application uses one recorder process/worker and does not have production-safe coordination or crash recovery.

## Remaining work and decisions

The earlier `integrated/` demo and this account prototype are **not yet a single authenticated complete portal**. Full rental/paddle/waitlist/no-show flows, paid checkout and receipts, QR scanning, complete queue administration/UI and all feature-by-feature permission checks still need to be integrated and tested.

Before any real accounts or online rollout: real address verification and safe enrollment, password recovery, MFA and staff onboarding; production anti-automation and distributed rate limits, secure TLS/host/proxy configuration, security headers and session lifecycle review, dependency and vulnerability review, independent penetration testing, operational audit/log retention and backups, accessible UX, privacy/legal review, and recovery procedures. The current signup endpoint can be abused and test throttling can lock users out; neither is suitable for public use. Concurrent in-flight revocation needs further design.

No GCash provider, Gmail/SMTP delivery, real QR scanner, RTSP/CCTV camera, real recordings or secure production media infrastructure is connected. Synthetic clip retention remains opt-in: **no automatic seven-day clock/deletion worker is enabled**, and production deletion across NVR, cloud and backups is unimplemented. The starting event, companion acceptance, rental replay email recipients and Philippine privacy/consumer/BIR requirements remain unresolved. Provider, hosting, camera, storage and network costs are not selected or quoted; no paid service or equipment has been purchased.
