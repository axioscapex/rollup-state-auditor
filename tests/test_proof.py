"""Offline controls for the security-critical trie proof boundary."""

import pytest
import rlp
from eth_utils import keccak

from common import Mismatch, account_storage_root, output_root


def synthetic_account():
    account_address = "0x" + "11" * 20
    storage_root = bytes.fromhex("22" * 32)
    code_hash = bytes.fromhex("33" * 32)
    account = rlp.encode([b"\x01", b"\x02", storage_root, code_hash])
    # A single leaf is enough to test reference hashing, the hex-prefix path,
    # terminal account RLP, and storage-root extraction.
    path = b"\x20" + keccak(bytes.fromhex(account_address[2:]))
    leaf = rlp.encode([path, account])
    result = {"address": account_address, "accountProof": ["0x" + leaf.hex()],
              "storageHash": "0x" + storage_root.hex(),
              "codeHash": "0x" + code_hash.hex(),
              "nonce": "0x1", "balance": "0x2"}
    return "0x" + keccak(leaf).hex(), result, storage_root


def test_authenticates_account_against_state_root():
    state_root, proof, expected = synthetic_account()
    assert account_storage_root(state_root, proof["address"],
                                proof["accountProof"], proof) == expected


def test_rejects_tampered_proof_node():
    state_root, proof, _ = synthetic_account()
    raw = bytearray(bytes.fromhex(proof["accountProof"][0][2:]))
    raw[-1] ^= 1
    proof["accountProof"] = ["0x" + raw.hex()]
    with pytest.raises(Mismatch, match="wrong reference"):
        account_storage_root(state_root, proof["address"], proof["accountProof"], proof)


def test_rejects_proof_for_another_address():
    state_root, proof, _ = synthetic_account()
    with pytest.raises(Mismatch, match="path mismatch"):
        account_storage_root(state_root, "0x" + "44" * 20,
                             proof["accountProof"], proof)


def test_output_root_commits_to_all_four_fields():
    state_root, proof, storage_root = synthetic_account()
    version = "0x" + "00" * 32
    block_hash = "0x" + "55" * 32
    actual = output_root(version, state_root, storage_root, block_hash)
    expected = "0x" + keccak(bytes(32) + bytes.fromhex(state_root[2:]) +
                            storage_root + bytes.fromhex(block_hash[2:])).hex()
    assert actual == expected
    assert output_root(version, state_root, storage_root,
                       "0x" + "66" * 32) != actual
