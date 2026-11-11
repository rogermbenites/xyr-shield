// ShieldLog — append-only commitment log. Stores sha256 hashes only; never trade data.
// Each commit gets a sequence number `n` and the block timestamp, so anyone can check
// (a) a revealed trade existed at that time and (b) how many trades were logged in total.
import { NearBindgen, near, call, view, initialize, LookupMap, assert } from "near-sdk-js";

@NearBindgen({ requireInit: true })
class ShieldLog {
  owner = "";
  count = 0;
  commits = new LookupMap("c");

  @initialize({})
  init({ owner }) {
    this.owner = owner;
  }

  @call({})
  commit({ hash }) {
    assert(near.predecessorAccountId() === this.owner, "only owner can commit");
    assert(/^[0-9a-f]{64}$/.test(hash), "hash must be 64 lowercase hex chars");
    assert(this.commits.get(hash) === null, "duplicate commitment");
    const rec = {
      n: this.count,
      ts_ns: near.blockTimestamp().toString(),
      block: near.blockHeight().toString(),
    };
    this.commits.set(hash, rec);
    this.count += 1;
    near.log(`EVENT_JSON:${JSON.stringify({ standard: "xyr_shield", version: "1.0.0",
      event: "commit", data: { hash, ...rec } })}`);
    return rec;
  }

  @view({})
  get_commit({ hash }) {
    return this.commits.get(hash);
  }

  @view({})
  get_count() {
    return this.count;
  }

  @view({})
  get_owner() {
    return this.owner;
  }
}
