# Auto-Bet and Financial Execution Safety

Auto-Bet is separate from data collection, analytics, prediction and dashboard development.

The language model must not independently authorize or initiate real-money betting. Execution must be controlled by deterministic application logic and explicit operator authorization.

## Required controls
- Verified event and market identity.
- Eligibility and trigger validation.
- Current odds/price verification.
- Stake-per-bet limit.
- Maximum bets per day.
- Maximum daily exposure.
- Explicit execution authorization.
- Kill switch and fail-closed behaviour.
- Idempotency and duplicate-submission protection.
- Auditable execution and confirmation records.

If any required limit, authorization, identity or verification is missing, execution must remain disabled. Do not infer permission from a UI toggle, a test result, a prior conversation or the presence of credentials.

Do not enable live submission, bypass controls, retry real submissions automatically or change financial limits without explicit operator approval. Testing must use dry-run or isolated test fixtures and must not submit real wagers.
