"""NEAR AI Cloud client that keeps the receipts.

Every call goes to a TEE-attested model and returns, alongside the answer, the
proof bundle: request hash, response hash, the TEE's signature over them, and
whether that signature recovers to the address in the model's hardware
attestation.

Raw requests (not the openai SDK) so we hash the exact bytes on the wire.
"""
import hashlib
import json
import os
import secrets
import time

import requests

BASE = "https://cloud-api.near.ai/v1"
DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"   # attested + tools + json_mode, cheap


def _key():
    k = os.environ.get("NEARAI_API_KEY")
    if not k:
        raise RuntimeError("NEARAI_API_KEY not set (get one at cloud.near.ai)")
    return k


def _h():
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
            "accept": "application/json"}


_verifiable = None


def verifiable_models() -> set:
    """Models served inside TEEs. External models (Claude, GPT…) are proxied and NOT private."""
    global _verifiable
    if _verifiable is None:
        models = requests.get(f"{BASE}/model/list", timeout=20).json()["models"]
        _verifiable = {m["modelId"] for m in models if m["metadata"].get("verifiable")}
    return _verifiable


def model() -> str:
    m = os.environ.get("NEARAI_MODEL", DEFAULT_MODEL)
    if m not in verifiable_models():
        raise RuntimeError(f"{m} is not a TEE-attested model — refusing to send private data to it")
    return m


def chat(messages, **extra) -> dict:
    """Returns {message, chat_id, request_hash, response_hash, model}."""
    m = model()
    body = json.dumps(dict(model=m, messages=messages, stream=False, **extra), separators=(",", ":"))
    r = requests.post(f"{BASE}/chat/completions", data=body, headers=_h(), timeout=120)
    r.raise_for_status()
    raw = r.text
    data = json.loads(raw)
    return dict(message=data["choices"][0]["message"], chat_id=data["id"], model=m,
                request_hash=hashlib.sha256(body.encode()).hexdigest(),
                response_hash=hashlib.sha256(raw.encode()).hexdigest())


def _find_addresses(obj) -> set:
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "signing_address" and isinstance(v, str):
                out.add(v.lower())
            else:
                out |= _find_addresses(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _find_addresses(v)
    return out


def attested_addresses(m: str, gateway: bool = False) -> set:
    """signing_address(es) bound to the TEE hardware quote (model nodes, or the gateway)."""
    params = dict(signing_algo="ecdsa", nonce=secrets.token_hex(32))
    if not gateway:
        params["model"] = m
    rep = requests.get(f"{BASE}/attestation/report", params=params, headers=_h(), timeout=60).json()
    if not gateway:
        rep = rep.get("model_attestations", rep)
    return _find_addresses(rep)


def receipt(call: dict, retries: int = 6) -> dict:
    """Fetch the TEE signature for a completed call and check it end to end."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    sig = None
    for _ in range(retries):   # signature lives on the node that served us; retry hits it
        r = requests.get(f"{BASE}/signature/{call['chat_id']}",
                         params=dict(model=call["model"], signing_algo="ecdsa"), headers=_h(), timeout=30)
        if r.ok and "signature" in r.json():
            sig = r.json()
            break
        time.sleep(1)
    if sig is None:
        return dict(verified=False, error="signature not found")

    text = sig["text"]
    kind = sig.get("signature_kind") or ("provider_tee" if text.count(":") >= 2 else "gateway")
    hashes_match = text.endswith(f"{call['request_hash']}:{call['response_hash']}")
    recovered = Account.recover_message(encode_defunct(text=text), signature=sig["signature"]).lower()
    attested = attested_addresses(call["model"], gateway=(kind == "gateway"))
    return dict(
        verified=hashes_match and recovered == sig["signing_address"].lower() and recovered in attested,
        signature_kind=kind, hashes_match=hashes_match, signer=recovered,
        signer_attested=recovered in attested, signed_text=text, signature=sig["signature"],
        chat_id=call["chat_id"], model=call["model"],
    )
