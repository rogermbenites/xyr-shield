// Thin NEAR client for the ShieldLog contract. Prints one JSON line on stdout.
//
//   node near.mjs commit <sha256hex>     (signs as NEAR_ACCOUNT_ID)
//   node near.mjs get <sha256hex>        (view, no key needed)
//   node near.mjs count                  (view)
//   node near.mjs deploy <path.wasm>     (deploys to NEAR_ACCOUNT_ID and inits owner=itself)
//
// env: NEAR_NETWORK (testnet), NEAR_ACCOUNT_ID, NEAR_PRIVATE_KEY (ed25519:...),
//      SHIELD_CONTRACT (defaults to NEAR_ACCOUNT_ID)
import fs from "fs";
import { connect, keyStores, KeyPair } from "near-api-js";

const network = process.env.NEAR_NETWORK || "testnet";
const accountId = process.env.NEAR_ACCOUNT_ID;
const contractId = process.env.SHIELD_CONTRACT || accountId;
const NODES = { testnet: "https://test.rpc.fastnear.com", mainnet: "https://free.rpc.fastnear.com" };

async function near() {
  const keyStore = new keyStores.InMemoryKeyStore();
  if (accountId && process.env.NEAR_PRIVATE_KEY) {
    await keyStore.setKey(network, accountId, KeyPair.fromString(process.env.NEAR_PRIVATE_KEY));
  }
  return connect({ networkId: network, nodeUrl: process.env.NEAR_RPC || NODES[network], keyStore });
}

const nsToIso = (ns) => new Date(Number(BigInt(ns) / 1_000_000n)).toISOString();

async function view(method, args = {}) {
  const conn = await near();
  const acct = await conn.account(contractId);
  return acct.viewFunction({ contractId, methodName: method, args });
}

async function main() {
  const [cmd, arg] = process.argv.slice(2);
  if (!contractId) throw new Error("set NEAR_ACCOUNT_ID or SHIELD_CONTRACT");

  if (cmd === "get") {
    const rec = await view("get_commit", { hash: arg });
    return rec && { n: rec.n, ts: nsToIso(rec.ts_ns), block: rec.block };
  }
  if (cmd === "count") return { count: await view("get_count") };

  const conn = await near();
  const acct = await conn.account(accountId);

  if (cmd === "commit") {
    const res = await acct.functionCall({
      contractId, methodName: "commit", args: { hash: arg }, gas: 30_000_000_000_000n,
    });
    const b64 = res.status.SuccessValue;
    const rec = JSON.parse(Buffer.from(b64, "base64").toString());
    return { n: rec.n, ts: nsToIso(rec.ts_ns), ref: res.transaction.hash, block: rec.block };
  }
  if (cmd === "deploy") {
    await acct.deployContract(fs.readFileSync(arg));
    await acct.functionCall({ contractId: accountId, methodName: "init", args: { owner: accountId } });
    return { deployed: accountId, network };
  }
  throw new Error(`unknown command: ${cmd}`);
}

main()
  .then((out) => console.log(JSON.stringify(out ?? null)))
  .catch((e) => { console.error(e.message || e); process.exit(1); });
