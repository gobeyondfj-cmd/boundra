import test from "node:test";
import assert from "node:assert/strict";
import { BoundraClient, createX402Hook } from "../dist/index.js";

const context = {
  selectedRequirements: { amount: "3000", payTo: "0xabc", network: "eip155:8453", asset: "USDC" },
  paymentRequired: { resource: { url: "https://merchant.test/data", method: "GET", description: "data" } },
};

test("evaluate sends owner and exact expectations, then settles", async () => {
  const calls = [];
  const mockFetch = async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith("/evaluate/basic")) {
      return new Response(JSON.stringify({ decision: "ALLOW", evaluation_id: "e1" }), { status: 200 });
    }
    return new Response(JSON.stringify({ id: "e1", status: "settled" }), { status: 200 });
  };
  const client = new BoundraClient({ baseUrl: "https://api.test", owner: "0xowner", agentId: "bot", fetch: mockFetch });
  const verdict = await client.evaluate(
    { amount: 0.003, recipient: "0xabc", purpose: "data", expected_amount: 0.003 },
    { purchaseId: "purchase-a" },
  );
  assert.equal(verdict.decision, "ALLOW");
  const state = await client.settle("e1", "private-token", "0xhash");
  assert.equal(state.status, "settled");
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.owner, "0xowner");
  assert.equal(body.intent.expected_amount, 0.003);
  assert.equal(body.purchase_id, "purchase-a");
});

test("ALLOW permits x402 hook, DENY and network failures abort", async () => {
  const allow = new BoundraClient({ fetch: async () => new Response(JSON.stringify({ decision: "ALLOW" }), { status: 200 }) });
  assert.equal(await createX402Hook(allow, { purpose: "data" })(context), undefined);
  const deny = new BoundraClient({ fetch: async () => new Response(JSON.stringify({ decision: "DENY", reasons: ["price changed"] }), { status: 200 }) });
  assert.equal((await createX402Hook(deny, { purpose: "data" })(context)).abort, true);
  const offline = new BoundraClient({ fetch: async () => { throw new Error("offline"); } });
  const aborted = await createX402Hook(offline, { purpose: "data" })(context);
  assert.equal(aborted.abort, true);
  assert.match(aborted.reason, /failed closed/);
});
