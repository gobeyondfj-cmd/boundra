# @boundra/x402

TypeScript client for Boundra's hosted payment decision API. The x402 hook is
**fail-closed**: only an explicit `ALLOW` permits payment creation.

```ts
import { BoundraClient, createX402Hook } from "@boundra/x402";

const boundra = new BoundraClient({ owner: OWNER_WALLET, agentId: "data-bot-v1" });
x402Client.onBeforePaymentCreation(createX402Hook(boundra, {
  purpose: "buy weather data",
  expectedAmount: 0.01,
  expectedRecipient: EXPECTED_PAYEE,
  purchaseId: PURCHASE_ID, // random caller-owned ID for this logical purchase
}));
```

Generate `purchaseId` before concurrent fan-out. Only the first reservation for
an `(owner, purchase_id)` receives ALLOW; same-ID duplicates abort at the
`idempotency` layer, and conflicting intent reuse is reported explicitly. Use a
fresh ID for every separate purchase, even when the quote is unchanged. For
direct API calls, pass `client.evaluate(intent, { purchaseId: PURCHASE_ID })`.

After a real payment succeeds, call `settle(evaluationId, reservationToken, txHash)`.
If it fails or is abandoned, call `release(...)`. Keep the reservation token in
the agent's private runtime state; it is a capability, not a public credential.
