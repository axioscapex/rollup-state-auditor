"""Generic OP Stack checkpoint primitives; no chain-specific defaults."""

import json
import os
import time
from pathlib import Path

import requests
import rlp
from eth_utils import keccak


class AuditError(Exception):
    pass


class Mismatch(AuditError):
    pass


def require(condition, message):
    if not condition:
        raise Mismatch(message)


def raw_hex(value):
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise AuditError(f"invalid hex value: {value!r}")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise AuditError(f"invalid hex value: {value!r}") from exc


def address(value):
    result = raw_hex(value)
    if len(result) != 20:
        raise AuditError("address must be 20 bytes")
    return result


def selector(signature):
    return "0x" + keccak(text=signature)[:4].hex()


def load_config(path):
    path = Path(path).expanduser().resolve()
    config = json.loads(path.read_text())
    for key in ("l1_rpc", "reference_l2_rpc", "local_l2_rpc", "l1_factory",
                "message_passer", "chain_id", "checkpoint_height"):
        if key not in config or config[key] is None:
            raise AuditError(f"configure {key} in {path.name}")
    for key in ("l1_rpc", "reference_l2_rpc", "local_l2_rpc"):
        if "YOUR_" in config[key]:
            raise AuditError(f"replace placeholder {key}")
    if not isinstance(config["chain_id"], int) or config["chain_id"] <= 0:
        raise AuditError("chain_id must be a positive integer")
    if not isinstance(config.get("expected_game_type"), int) or config["expected_game_type"] < 0:
        raise AuditError("set expected_game_type to the deployed game type")
    if address(config["l1_factory"]) == bytes(20):
        raise AuditError("replace placeholder l1_factory")
    address(config["message_passer"])
    mode = config["checkpoint_height"].get("mode")
    if mode not in ("timestamp_linear", "block_number_method"):
        raise AuditError("checkpoint_height.mode must be timestamp_linear or block_number_method")
    if mode == "timestamp_linear":
        mapping = config["checkpoint_height"]
        for key in ("anchor_timestamp", "anchor_l2_block", "seconds_per_block"):
            if not isinstance(mapping.get(key), int):
                raise AuditError(f"configure checkpoint_height.{key}")
        if mapping["seconds_per_block"] <= 0:
            raise AuditError("seconds_per_block must be positive")
    if config.get("game_claim_method", "rootClaimByChainId") not in ("rootClaimByChainId", "rootClaim"):
        raise AuditError("unsupported game_claim_method")
    if len(raw_hex(config.get("output_root_version", "0x" + "00" * 32))) != 32:
        raise AuditError("output_root_version must be 32 bytes")
    data_dir = (path.parent / config.get("data_dir", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    config["data_dir"] = data_dir
    config["config_path"] = path
    return config


class RPC:
    def __init__(self, url, delay=0.0):
        self.url = url
        self.delay = delay
        self.session = requests.Session()

    def call(self, method, params, timeout=25):
        try:
            response = self.session.post(self.url, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
            }, timeout=timeout)
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AuditError(f"{method} transport failure: {exc}") from exc
        if "error" in result:
            raise AuditError(f"{method} RPC failure: {result['error']}")
        if "result" not in result:
            raise AuditError(f"{method} response missing result")
        if self.delay:
            time.sleep(self.delay)
        return result["result"]

    def contract(self, target, calldata, block_tag):
        return raw_hex(self.call("eth_call", [{"to": target, "data": calldata}, block_tag]))


def fixed_l1_tag(l1, configured_tag):
    block = l1.call("eth_getBlockByNumber", [configured_tag, False])
    if not block or "number" not in block or "hash" not in block:
        raise AuditError("cannot pin L1 block")
    return block["number"], block["hash"]


def decode_uint(data):
    if len(data) != 32:
        raise AuditError(f"expected 32-byte integer, got {len(data)} bytes")
    return int.from_bytes(data, "big")


def game_at_index(l1, factory, index, block_tag):
    calldata = selector("gameAtIndex(uint256)") + f"{index:064x}"
    result = l1.contract(factory, calldata, block_tag)
    if len(result) != 96:
        raise AuditError(f"gameAtIndex({index}) returned {len(result)} bytes")
    return {
        "game_type": int.from_bytes(result[:32], "big"),
        "created_at": int.from_bytes(result[32:64], "big"),
        "address": "0x" + result[76:96].hex(),
    }


def game_claim(l1, game_address, config, block_tag):
    method = config.get("game_claim_method", "rootClaimByChainId")
    if method == "rootClaimByChainId":
        calldata = selector("rootClaimByChainId(uint256)") + f"{config['chain_id']:064x}"
    else:
        calldata = selector("rootClaim()")
    result = l1.contract(game_address, calldata, block_tag)
    if len(result) != 32:
        raise AuditError("game claim must be 32 bytes")
    return "0x" + result.hex()


def game_height(l1, game_address, config, block_tag):
    mapping = config["checkpoint_height"]
    if mapping["mode"] == "block_number_method":
        sequence = decode_uint(l1.contract(game_address, selector("l2BlockNumber()"), block_tag))
        return sequence, sequence
    sequence = decode_uint(l1.contract(game_address, selector("l2SequenceNumber()"), block_tag))
    delta = sequence - mapping["anchor_timestamp"]
    step = mapping["seconds_per_block"]
    require(delta >= 0 and delta % step == 0, "game timestamp does not map to an L2 height")
    return mapping["anchor_l2_block"] + delta // step, sequence


def load_games(config):
    path = config["data_dir"] / "games.json"
    if not path.exists():
        return []
    games = json.loads(path.read_text())
    if not isinstance(games, list) or any(row.get("index") != i for i, row in enumerate(games)):
        raise AuditError("games.json index sequence is invalid")
    return games


def save_games(config, games):
    path = config["data_dir"] / "games.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(games, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def nibbles(data):
    return [nibble for byte in data for nibble in (byte >> 4, byte & 15)]


def compact_path(encoded):
    digits = nibbles(encoded)
    if not digits or digits[0] not in (0, 1, 2, 3):
        raise Mismatch("invalid hex-prefix trie path")
    odd = digits[0] & 1
    leaf = bool(digits[0] & 2)
    if odd:
        return digits[1:], leaf
    require(len(digits) > 1 and digits[1] == 0, "invalid even trie path")
    return digits[2:], leaf


def account_storage_root(state_root_hex, account_address, account_proof, rpc_result=None):
    """Authenticate EIP-1186 accountProof under a locally derived state root."""
    reference = raw_hex(state_root_hex)
    require(len(reference) == 32, "state root must be 32 bytes")
    key = nibbles(keccak(address(account_address)))
    require(bool(account_proof), "empty account proof")
    value = None
    for index, encoded in enumerate(account_proof):
        raw = raw_hex(encoded)
        require((keccak(raw) if len(reference) == 32 else raw) == reference,
                f"proof node {index} has wrong reference")
        try:
            node = rlp.decode(raw)
        except Exception as exc:
            raise Mismatch(f"proof node {index} is invalid RLP") from exc
        require(isinstance(node, list) and all(isinstance(item, bytes) for item in node),
                f"proof node {index} is not an RLP list of bytes")
        if len(node) == 17:
            if key:
                reference = node[key.pop(0)]
                require(bool(reference), "account path ends in empty branch")
            else:
                value = node[16]
                require(index == len(account_proof) - 1, "trailing account proof nodes")
        elif len(node) == 2:
            segment, leaf = compact_path(node[0])
            require(key[:len(segment)] == segment, "account proof path mismatch")
            key = key[len(segment):]
            if leaf:
                require(not key and index == len(account_proof) - 1, "invalid account proof leaf")
                value = node[1]
            else:
                reference = node[1]
                require(bool(reference), "empty extension reference")
        else:
            raise Mismatch(f"proof node {index} has wrong arity")
    require(value is not None, "account proof has no value")
    try:
        nonce, balance, storage_root, code_hash = rlp.decode(value)
    except Exception as exc:
        raise Mismatch("account value is invalid RLP") from exc
    require(not key and len(storage_root) == 32 and len(code_hash) == 32,
            "invalid terminal account")
    if rpc_result is not None:
        require(storage_root == raw_hex(rpc_result["storageHash"]), "storageHash mismatch")
        require(code_hash == raw_hex(rpc_result["codeHash"]), "codeHash mismatch")
        require(int.from_bytes(nonce, "big") == int(rpc_result["nonce"], 16), "nonce mismatch")
        require(int.from_bytes(balance, "big") == int(rpc_result["balance"], 16), "balance mismatch")
    return storage_root


def output_root(version_hex, state_root_hex, storage_root, block_hash_hex):
    version = raw_hex(version_hex)
    state_root = raw_hex(state_root_hex)
    block_hash = raw_hex(block_hash_hex)
    require(all(len(item) == 32 for item in (version, state_root, storage_root, block_hash)),
            "output root fields must each be 32 bytes")
    return "0x" + keccak(version + state_root + storage_root + block_hash).hex()
