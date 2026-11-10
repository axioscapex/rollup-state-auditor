"""Compare independent local execution with L1's per-chain output claim."""

import argparse
import json
import sys
from datetime import datetime, timezone

from common import (AuditError, RPC, account_storage_root, fixed_l1_tag,
                    game_at_index, game_claim, game_height, load_config, load_games,
                    output_root, require)


BLOCK_FIELDS = (
    "number", "hash", "parentHash", "stateRoot", "transactionsRoot",
    "receiptsRoot", "timestamp", "gasUsed", "transactions",
)


def verify(config, game):
    height = game["l2_block"]
    local = RPC(config["local_l2_rpc"])
    reference = RPC(config["reference_l2_rpc"])
    l1 = RPC(config["l1_rpc"])
    local_block = local.call("eth_getBlockByNumber", [hex(height), False])
    remote_block = reference.call("eth_getBlockByNumber", [hex(height), False])
    require(isinstance(local_block, dict) and isinstance(remote_block, dict),
            f"L2 block {height} unavailable")
    for field in BLOCK_FIELDS:
        require(local_block.get(field) == remote_block.get(field),
                f"L2 block {height} differs on {field}")
    require(int(local_block["number"], 16) == height, "wrong L2 block number")
    if config["checkpoint_height"]["mode"] == "timestamp_linear":
        require(int(local_block["timestamp"], 16) == game["sequence"],
                "L2 timestamp differs from game sequence")

    # The reference RPC supplies proof bytes, but its storageHash is not trusted.
    # The proof is authenticated against the locally executed state root.
    proof = reference.call("eth_getProof", [config["message_passer"], [], hex(height)])
    require(proof["address"].lower() == config["message_passer"].lower(),
            "MessagePasser proof address mismatch")
    storage_root = account_storage_root(local_block["stateRoot"],
                                        config["message_passer"],
                                        proof["accountProof"], proof)
    computed = output_root(config["output_root_version"], local_block["stateRoot"],
                           storage_root, local_block["hash"])
    require(computed.lower() == game["chain_output_root"].lower(),
            "derived output differs from retained schedule")

    l1_tag, l1_hash = fixed_l1_tag(l1, config.get("l1_head_tag", "finalized"))
    registered = game_at_index(l1, config["l1_factory"], game["index"], l1_tag)
    require(registered["address"].lower() == game["game_address"].lower(),
            "Factory game address differs from retained schedule")
    require(registered["game_type"] == game["game_type"],
            "Factory game type differs from retained schedule")
    require(registered["game_type"] == config["expected_game_type"],
            "Factory game type differs from pinned configuration")
    live_height, live_sequence = game_height(l1, registered["address"], config, l1_tag)
    require((live_height, live_sequence) == (height, game["sequence"]),
            "game checkpoint height differs from retained schedule")
    live_claim = game_claim(l1, registered["address"], config, l1_tag)
    require(computed.lower() == live_claim.lower(),
            "locally derived output differs from direct L1 claim")
    return {
        "game": game["index"], "l2_block": height,
        "block_hash": local_block["hash"],
        "state_root": local_block["stateRoot"],
        "message_passer_storage_root": "0x" + storage_root.hex(),
        "account_proof_nodes": len(proof["accountProof"]),
        "derived_output_root": computed,
        "l1_claim": live_claim,
        "l1_block": int(l1_tag, 16), "l1_block_hash": l1_hash,
        "matched": True,
    }


def verify_index(config, index):
    games = load_games(config)
    if not 0 <= index < len(games):
        raise AuditError(f"game {index} missing from schedule; run discover_games.py")
    return verify(config, games[index])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("index", type=int)
    args = parser.parse_args()
    config = load_config(args.config)
    record = {"time_utc": datetime.now(timezone.utc).isoformat(),
              "game": args.index}
    status = 0
    try:
        record.update(verify_index(config, args.index))
    except AuditError as exc:
        record.update({"matched": False, "error": str(exc)})
        status = 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config["data_dir"] / f"checkpoint-{args.index}-{stamp}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"audit": str(path), **record}, sort_keys=True))
    sys.exit(status)
