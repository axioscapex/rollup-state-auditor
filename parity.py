"""Compare a locally derived checkpoint over configured L2 transports."""

import argparse
import json
import sys
from datetime import datetime, timezone

import websocket

from common import AuditError, RPC, load_config, load_games, require


FIELDS = (
    "number", "hash", "parentHash", "stateRoot", "transactionsRoot",
    "receiptsRoot", "logsBloom", "timestamp", "gasUsed", "baseFeePerGas",
    "transactions",
)


def ws_call(ws, method, params, identifier):
    ws.send(json.dumps({"jsonrpc": "2.0", "id": identifier,
                        "method": method, "params": params}))
    while True:
        payload = json.loads(ws.recv())
        if payload.get("id") != identifier:
            continue
        if "error" in payload:
            raise AuditError(f"WSS {method}: {payload['error']}")
        return payload["result"]


def audit(config, index):
    games = load_games(config)
    if not 0 <= index < len(games):
        raise AuditError("game index missing from schedule")
    height = games[index]["l2_block"]
    tag = hex(height)
    urls = {"local": config["local_l2_rpc"],
            "reference_https": config["reference_l2_rpc"]}
    if config.get("explorer_rpc"):
        urls["explorer_rpc"] = config["explorer_rpc"]
    clients = {name: RPC(url) for name, url in urls.items()}
    blocks = {name: client.call("eth_getBlockByNumber", [tag, False])
              for name, client in clients.items()}
    ws = None
    if config.get("reference_l2_wss"):
        ws = websocket.create_connection(config["reference_l2_wss"], timeout=25)
        blocks["reference_wss"] = ws_call(ws, "eth_getBlockByNumber", [tag, False], 1)
    try:
        require(all(isinstance(block, dict) for block in blocks.values()),
                "checkpoint block unavailable over a configured transport")
        local = blocks["local"]
        block_diffs = {
            name: [field for field in FIELDS if block.get(field) != local.get(field)]
            for name, block in blocks.items() if name != "local"
        }
        txs = local["transactions"]
        receipts = {name: [] for name in blocks}
        for position, tx in enumerate(txs):
            for name, client in clients.items():
                receipts[name].append(client.call("eth_getTransactionReceipt", [tx]))
            if ws:
                receipts["reference_wss"].append(
                    ws_call(ws, "eth_getTransactionReceipt", [tx], 10 + position))
    finally:
        if ws:
            ws.close()
    exact = {}
    common_diffs = {}
    missing_keys = {}
    for name in receipts:
        if name == "local":
            continue
        require(all(a is not None and b is not None for a, b in
                    zip(receipts["local"], receipts[name])),
                f"missing receipt over {name}")
        exact[name] = [a == b for a, b in zip(receipts["local"], receipts[name])]
        common_diffs[name] = [
            {"tx": txs[i], "key": key, "local": a[key], "other": b[key]}
            for i, (a, b) in enumerate(zip(receipts["local"], receipts[name]))
            for key in a.keys() & b.keys() if a[key] != b[key]
        ]
        missing_keys[name] = sorted(set().union(*[
            a.keys() - b.keys() for a, b in zip(receipts["local"], receipts[name])
        ])) if txs else []
    require(not any(block_diffs.values()), f"block parity mismatch: {block_diffs}")
    require(not any(common_diffs.values()), f"receipt parity mismatch: {common_diffs}")
    for name in ("reference_https", "reference_wss"):
        if name in exact:
            require(all(exact[name]), f"receipt metadata differs over {name}")
    return {"game": index, "l2_block": height, "block_hash": local["hash"],
            "transaction_count": len(txs), "block_diffs": block_diffs,
            "receipt_exact": exact, "receipt_missing_keys": missing_keys,
            "matched": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("index", type=int)
    args = parser.parse_args()
    config = load_config(args.config)
    row = {"time_utc": datetime.now(timezone.utc).isoformat(), "game": args.index}
    code = 0
    try:
        row.update(audit(config, args.index))
    except (AuditError, websocket.WebSocketException) as exc:
        row.update({"matched": False, "error": str(exc)})
        code = 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config["data_dir"] / f"parity-{args.index}-{stamp}.json"
    path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"audit": str(path), **row}, sort_keys=True))
    sys.exit(code)
