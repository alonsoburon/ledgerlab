# Engine HTTP contract v1

All engines expose JSON over HTTP and implement these routes. UTF-8; money values are integer minor units. Errors have `{"error":{"code":"...","message":"..."}}` shape.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, implementation name, protocol version |
| GET | `/metrics` | Entry count, net ledger amount, transfer count, invalid transfer groups |
| GET | `/accounts` | List accounts and current balances |
| POST | `/accounts` | Create `{ "id": "...", "currency": "USD" }` |
| GET | `/accounts/{id}` | Account and balance |
| POST | `/transfers` | Post a transfer |
| GET | `/ledger?account_id=...&limit=100` | Read immutable entries |

Transfer request:

```json
{
  "idempotency_key": "run-17-op-8",
  "from_account": "alice",
  "to_account": "bob",
  "amount_minor": 250,
  "currency": "USD"
}
```

Successful posting returns `201` with transfer ID, status `posted`, and resulting source/destination balances. Reusing a key with the same request returns the original transfer; reusing it with different content returns `409 idempotency_conflict`. Insufficient funds returns `409 insufficient_funds`; malformed requests return `400`; unknown accounts return `404`. Posting a transfer with identical source and destination is invalid.

Each success creates exactly two entries tied to the transfer ID: source debit `-amount_minor`, destination credit `+amount_minor`. Their sum is zero. Entries are never updated or deleted. See `docs/architecture.md` for invariants and methodology.
