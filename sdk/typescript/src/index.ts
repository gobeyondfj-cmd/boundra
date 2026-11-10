export type Decision = "ALLOW" | "ASK" | "DENY" | "INDETERMINATE";

export interface PaymentIntent {
  amount: number;
  recipient: string;
  purpose: string;
  expected_amount?: number;
  expected_recipient?: string;
  resource?: string;
  method?: string;
  network?: string;
  asset?: string;
  source_content?: string;
}

export interface Verdict {
  decision: Decision;
  reasons?: string[];
  layer?: string;
  challenge?: Record<string, unknown>;
  evaluation_id?: string;
  reservation_token?: string;
  reservation_expires_at?: number;
  [key: string]: unknown;
}

export interface BoundraOptions {
  baseUrl?: string;
  owner?: string;
  agentId?: string;
  fetch?: typeof fetch;
}

export class BoundraClient {
  readonly baseUrl: string;
  readonly owner?: string;
  readonly agentId?: string;
  private readonly request: typeof fetch;

  constructor(options: BoundraOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "https://api.boundra.io").replace(/\/$/, "");
    this.owner = options.owner;
    this.agentId = options.agentId;
    this.request = options.fetch ?? fetch;
  }

  private async post<T>(path: string, body: unknown, semantic = false): Promise<T> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (semantic && this.agentId) headers["X-Boundra-Agent"] = this.agentId;
    const response = await this.request(`${this.baseUrl}${path}`, {
      method: "POST", headers, body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Boundra HTTP ${response.status}`);
    return await response.json() as T;
  }

  async evaluate(intent: PaymentIntent, options: { semantic?: boolean; approvalToken?: string; mandate?: unknown; purchaseId?: string } = {}): Promise<Verdict> {
    const result = await this.post<Verdict>(options.semantic ? "/evaluate/semantic" : "/evaluate/basic", {
      intent,
      ...(this.owner ? { owner: this.owner } : {}),
      ...(this.agentId ? { agent_id: this.agentId } : {}),
      ...(options.approvalToken ? { approval_token: options.approvalToken } : {}),
      ...(options.mandate ? { mandate: options.mandate } : {}),
      ...(options.purchaseId !== undefined ? { purchase_id: options.purchaseId } : {}),
    }, options.semantic);
    if (!result || !["ALLOW", "ASK", "DENY", "INDETERMINATE"].includes(result.decision)) {
      throw new Error("Boundra returned an invalid verdict");
    }
    return result;
  }

  settle(evaluationId: string, reservationToken: string, settlementRef: string): Promise<Record<string, unknown>> {
    return this.post(`/evaluations/${encodeURIComponent(evaluationId)}/settle`, {
      reservation_token: reservationToken, settlement_ref: settlementRef,
    });
  }

  release(evaluationId: string, reservationToken: string): Promise<Record<string, unknown>> {
    return this.post(`/evaluations/${encodeURIComponent(evaluationId)}/release`, {
      reservation_token: reservationToken,
    });
  }
}

interface X402Requirements {
  amount: string;
  payTo: string;
  network?: string;
  asset?: string;
}

interface X402Context {
  selectedRequirements: X402Requirements;
  paymentRequired?: {
    resource?: { url?: string; description?: string; method?: string };
  };
}

export interface X402HookOptions {
  purpose: string;
  expectedAmount?: number;
  expectedRecipient?: string;
  semantic?: boolean;
  purchaseId?: string;
  approve?: (verdict: Verdict) => Promise<string | undefined> | string | undefined;
}

/** Return void only after ALLOW; every error or unresolved ASK aborts signing. */
export function createX402Hook(client: BoundraClient, options: X402HookOptions) {
  return async (context: X402Context): Promise<void | { abort: true; reason: string }> => {
    try {
      const req = context.selectedRequirements;
      const intent: PaymentIntent = {
        amount: Number(req.amount) / 1_000_000,
        recipient: req.payTo,
        purpose: options.purpose,
        expected_amount: options.expectedAmount,
        expected_recipient: options.expectedRecipient,
        resource: context.paymentRequired?.resource?.url,
        method: context.paymentRequired?.resource?.method,
        network: req.network,
        asset: req.asset,
        source_content: context.paymentRequired?.resource?.description ?? "",
      };
      let verdict = await client.evaluate(intent, {
        semantic: options.semantic, purchaseId: options.purchaseId,
      });
      if (verdict.decision === "ASK" && options.approve) {
        const token = await options.approve(verdict);
        if (token) verdict = await client.evaluate(intent, {
          semantic: options.semantic, approvalToken: token, purchaseId: options.purchaseId,
        });
      }
      if (verdict.decision === "ALLOW") return;
      return { abort: true, reason: `Boundra ${verdict.decision}: ${(verdict.reasons ?? []).join("; ")}` };
    } catch (error) {
      return { abort: true, reason: `Boundra pre-sign check failed closed: ${String(error)}` };
    }
  };
}
