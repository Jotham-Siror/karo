# 004 — WhatsApp via the Twilio sandbox; public address karo.linxke.com
Date: 2026-10-20 · Supersedes: none

**WhatsApp:** it runs through the Twilio sandbox with the live Account SID and Auth Token. Twilio's test credentials send nothing, and webhook signatures need the Auth Token.

Meta's WhatsApp Cloud API test number was the rejected alternative. It needs no business verification, but it would have meant rewriting the WhatsApp task, and its access token expires every 24 hours.

**Public address:** https://karo.linxke.com, through a Cloudflare named tunnel from Joseph's laptop. The domain was already on Cloudflare, so the address is fixed and survives restarts, unlike a quick tunnel. The laptop, the tunnel and the server must stay up for webhooks to work.
