"""Natural-language questions over the private trade log.

1. The attested model turns the question into a filter (tool call). It sees the
   question and the schema — never a single trade.
2. stats.query() computes the answer inside our process from the vault.
3. The attested model phrases the aggregate for the caller.

Both model calls come back with TEE receipts.
"""
import json

from . import nearai, stats

TOOL = {
    "type": "function",
    "function": {
        "name": "query_trades",
        "description": "Aggregate stats over the strategy's round trips, filtered by entry conditions.",
        "parameters": {
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["all", "long", "short"]},
                "r_min": {"type": "number", "description": "min R at entry; R is in -1..+1"},
                "r_max": {"type": "number", "description": "max R at entry; R is in -1..+1"},
                "abs_r": {"type": "boolean", "description": "apply r_min/r_max to |R| instead of R"},
                "rmode": {"type": "string", "enum": ["LONG", "SHORT", "HOLD"]},
                "since_days": {"type": "number"},
                "fee_bp": {"type": "number", "description": "round-trip fee in basis points to subtract"},
            },
        },
    },
}

SYSTEM = ("You answer questions about a private trading strategy's record using only the query_trades "
          "tool. R is a regime oscillator in -1..+1. Never invent numbers. "
          "A 'refused' result means the slice is too small to disclose — say so; it is not evidence either way.")


ALLOWED = set(TOOL["function"]["parameters"]["properties"])


def ask(question: str, rows, with_receipts: bool = True, max_rounds: int = 4) -> dict:
    """Let the model run as many queries as the question needs, then answer."""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    calls_made, results = [], []
    for rnd in range(max_rounds + 1):
        last = rnd == max_rounds
        call = nearai.chat(msgs, tools=[TOOL], tool_choice="none" if last else ("required" if rnd == 0 else "auto"))
        calls_made.append(call)
        msg = call["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        msgs.append(msg)
        for tc in tool_calls:
            args = {k: v for k, v in json.loads(tc["function"]["arguments"] or "{}").items() if k in ALLOWED}
            result = stats.query(rows, **args)
            results.append(result)
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})
    out = dict(question=question, answer=(msg.get("content") or "").strip(), stats=results)
    if with_receipts:
        out["receipts"] = [nearai.receipt(c) for c in calls_made]
    return out
