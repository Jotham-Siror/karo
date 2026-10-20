# 006 — Payaza goes to main after the email; the submission-v2 tag stays frozen
Date: 2026-10-20 · Supersedes: none

The build session added a switched-off Payaza adapter (77cbb9b, 65 tests) at about 15:30, after the email text was final. Joseph chose to send the email at once and only update GitHub afterwards. `main` was fast-forwarded to d042f44, with one honest doc line and the new count (1,280) next to the submitted 1,215. The `submission-v2` tag and release were not moved, so everything the email says stays true. The rejected options were holding the email for a rebuilt kit, and moving the tag with a force push.
