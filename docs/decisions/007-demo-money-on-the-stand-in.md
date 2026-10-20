# 007 — Demo money runs on the stand-in provider
Date: 2026-10-20 · Supersedes: the build plan's "real test-mode money" freeze goal
The Flutterwave smoke test showed payouts refused ("merchant is not enabled to make transfers") and /transfers/rates failing on TLS, so every demo payment runs on the offline stand-in provider, labelled "test payment, simulated, no real money". Flutterwave (collections only) and Payaza stay in the code, switched off, so a final round turns them on instead of building them. The real Flutterwave test-mode evidence is the smoke-test files.
