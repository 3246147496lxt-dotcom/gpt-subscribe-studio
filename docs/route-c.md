# Route C: browser decode + order proxy

Default remains Route A. No behavior changes until both flags are set.

```env
GSS_ROUTE_PROFILE=C
GSS_BROWSER_DECODE=1
GSS_BROWSER_HEADLESS=0
GSS_BROWSER_DECODE_URL=https://chatgpt.com/
GSS_BROWSER_TIMEOUT_MS=30000
GSS_BROWSER_DECODE_AUTH=0
GSS_BROWSER_DECODE_FALLBACK=A
```

Install the optional browser runtime once:

```bash
uv sync --extra browser
uv run playwright install chromium
```

Flow:

```text
Playwright Chromium (direct) -> BrowserContext
  -> ChatGPTClient.apply_browser_context
  -> create_checkout via pinned PH proxy
  -> PlusHarProtocol confirm via pinned PH proxy
```

Anonymous mode never receives the account session cookies. It only collects
the browser's native UA / client hints / public risk cookies.

Failure policy:

- `browser_decode_unavailable` by default;
- `GSS_BROWSER_DECODE_FALLBACK=A` explicitly falls back to legacy Route A;
- no implicit fallback.

`purchase_attempt_id` is generated once per order and emitted to the internal
request header `X-GSS-Purchase-Attempt`. It is never written into an unverified
OpenAI checkout body field.

Billing profiles:

```json
{
  "version": 2,
  "payer_id": "PAYER_ID",
  "verification_ref": "VERIFICATION_REF",
  "profiles": {
    "PHP": { "name": "...", "email": "...", "line1": "...", "city": "...", "state": "OR", "postal_code": "...", "country": "US" },
    "EGP": { "..." : "..." },
    "USD": { "..." : "..." }
  }
}
```

```env
GSS_BILLING_PROFILES_FILE=/Users/pu/.gss/billing-profiles.json
```

Legacy `GSS_VERIFIED_BILLING_FILE` remains supported as a fallback for every
currency.

For a single fixed US tax-free address (the usual deployment), a plain address
file is enough:

```json
{"name": "...", "email": "...", "line1": "...", "city": "Portland", "state": "OR", "postal_code": "97205", "country": "US"}
```

```env
GSS_FIXED_BILLING_FILE=/Users/you/.gss/fixed-billing.json
```

When set it takes precedence over the two files above. Generate it with
`scripts/setup_fixed_us_billing.py`; see [fixed-us-billing.md](fixed-us-billing.md).
