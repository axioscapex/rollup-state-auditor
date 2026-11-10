"""Offline controls for Factory ABI decoding and checkpoint mapping."""

from common import game_at_index, game_height, selector


class FakeL1:
    def __init__(self, results):
        self.results = results

    def contract(self, target, calldata, block_tag):
        return self.results[calldata]


def word(value):
    return value.to_bytes(32, "big")


def test_factory_entry_decodes_type_time_and_address():
    game_address = bytes.fromhex("ab" * 20)
    result = word(5) + word(1234567) + bytes(12) + game_address
    calldata = selector("gameAtIndex(uint256)") + f"{7:064x}"
    entry = game_at_index(FakeL1({calldata: result}), "0x" + "cd" * 20,
                          7, "0x10")
    assert entry == {"game_type": 5, "created_at": 1234567,
                     "address": "0x" + game_address.hex()}


def test_timestamp_checkpoint_maps_to_exact_block():
    sequence = 1006
    config = {"checkpoint_height": {"mode": "timestamp_linear",
                                    "anchor_timestamp": 1000,
                                    "anchor_l2_block": 500,
                                    "seconds_per_block": 2}}
    calldata = selector("l2SequenceNumber()")
    height, observed_sequence = game_height(
        FakeL1({calldata: word(sequence)}), "0x" + "cd" * 20,
        config, "0x10")
    assert (height, observed_sequence) == (503, sequence)
