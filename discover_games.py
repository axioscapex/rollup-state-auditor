"""Append L1-registered output checkpoints; public L2 data is a prefilter only."""

import argparse
import json

from common import (AuditError, RPC, account_storage_root, fixed_l1_tag,
                    game_at_index, game_claim, game_height, load_config,
                    load_games, output_root, require, save_games, selector,
                    decode_uint)


def discover(config, max_new):
    l1 = RPC(config["l1_rpc"], delay=0.15)
    reference = RPC(config["reference_l2_rpc"], delay=0.1)
    l1_tag, l1_hash = fixed_l1_tag(l1, config.get("l1_head_tag", "finalized"))
    count = decode_uint(l1.contract(config["l1_factory"], selector("gameCount()"), l1_tag))
    games = load_games(config)
    require(len(games) <= count <= len(games) + max_new,
            "Factory count regressed or exceeds bounded discovery limit")

    if games:
        previous = games[-1]
        current = game_at_index(l1, config["l1_factory"], previous["index"], l1_tag)
        require(current["address"].lower() == previous["game_address"].lower(),
                "retained schedule disagrees with Factory")
        require(game_claim(l1, current["address"], config, l1_tag).lower() ==
                previous["chain_output_root"].lower(),
                "retained output claim changed")

    for index in range(len(games), count):
        entry = game_at_index(l1, config["l1_factory"], index, l1_tag)
        expected_type = config.get("expected_game_type")
        if expected_type is not None:
            require(entry["game_type"] == expected_type, f"game {index} has unexpected type")
        require(entry["created_at"] > 0, f"game {index} has no creation timestamp")
        require(int(entry["address"], 16) > 0, f"game {index} has zero address")
        height, sequence = game_height(l1, entry["address"], config, l1_tag)
        require(height >= 0 and (not games or height > games[-1]["l2_block"]),
                f"game {index} height is not monotonic")
        claim = game_claim(l1, entry["address"], config, l1_tag)

        # This only checks that the published L2 view is self-consistent with L1.
        # An independent local replay is required by verify_checkpoint.py.
        block = reference.call("eth_getBlockByNumber", [hex(height), False])
        require(isinstance(block, dict) and int(block["number"], 16) == height,
                f"reference L2 block {height} unavailable")
        if config["checkpoint_height"]["mode"] == "timestamp_linear":
            require(int(block["timestamp"], 16) == sequence,
                    f"game {index} timestamp differs from mapped L2 block")
        proof = reference.call("eth_getProof", [config["message_passer"], [], hex(height)])
        require(proof["address"].lower() == config["message_passer"].lower(),
                "proof address mismatch")
        storage_root = account_storage_root(block["stateRoot"],
                                            config["message_passer"],
                                            proof["accountProof"], proof)
        published_output = output_root(config["output_root_version"],
                                       block["stateRoot"], storage_root, block["hash"])
        require(published_output.lower() == claim.lower(),
                f"published L2 output differs from L1 game {index}")
        games.append({
            "index": index, "game_type": entry["game_type"],
            "game_address": entry["address"], "created_at": entry["created_at"],
            "l2_block": height, "sequence": sequence,
            "chain_output_root": claim,
            "discovered_l1_block": int(l1_tag, 16),
            "discovered_l1_hash": l1_hash,
            "prefilter": "published L2 block/proof only; no independent execution",
        })
    save_games(config, games)
    return {"factory_count": count, "retained_games": len(games),
            "l1_block": int(l1_tag, 16), "l1_hash": l1_hash}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--max-new", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.max_new <= 100:
        parser.error("--max-new must be 1..100")
    try:
        print(json.dumps(discover(load_config(args.config), args.max_new)))
    except AuditError as exc:
        parser.exit(2, f"discovery stopped: {exc}\n")
