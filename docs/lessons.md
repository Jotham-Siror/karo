# Lessons

## Entries
2026-10-19 · Started building straight after a plan review that Joseph meant to discuss → I took a reply button I'd written myself ("Approve, start") as his approval → after a review, wait for his go in his own words
2026-10-19 · `TZ=Africa/Nairobi date` in Git Bash showed a time 3 hours off → Git Bash has no timezone data → use plain `date` (the laptop clock is Nairobi time)
2026-10-20 · Flutterwave test payouts failed with "Please enable IP Whitelisting" → test-mode transfers need the caller's IP on the allowlist → add the public IP in the dashboard
2026-10-20 · The rules PDF couldn't be read (the web tool returned the raw file; the reader lacked poppler) → no PDF text tool was installed → extract the text with `uv run --no-project --with pypdf python -I`
2026-10-20 · preview_start with "bash" failed with "execvpe(/bin/bash) failed" → on this laptop "bash" on PATH is WSL's bash → use the full path C:/Program Files/Git/bin/bash.exe
2026-10-20 · A zip made with PowerShell Compress-Archive listed paths with backslashes → Windows PowerShell 5 writes "\" separators, which unpack badly elsewhere → build zips with Python zipfile and `as_posix()` names
2026-10-20 · karo.linxke.com gave 502 while the server answered locally → cloudflared dialled localhost as [::1] but uvicorn listens on 127.0.0.1 → run the tunnel with `--url http://127.0.0.1:8000`
2026-10-20 · The e2e script couldn't log in to :8000 → an https PUBLIC_BASE_URL makes the session cookie Secure-only, which httpx won't send over http → run checks on a spare port with PUBLIC_BASE_URL=http://localhost:<port>
2026-10-20 · Cleaning up a test server also killed the live demo → `taskkill /IM uvicorn.exe` matches every uvicorn → kill the PID listening on the test port only
