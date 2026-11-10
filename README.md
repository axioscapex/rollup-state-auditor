# Generic rollup state auditor

This is a sanitized, read-only checkpoint auditor for an OP Stack rollup. It
orchestrates a **separately configured** local rollup node and execution engine;
these Python scripts do not implement L1 derivation or the EVM themselves.
They never send an L1 or L2 transaction. The example configuration has no live
target, and the monitor refuses to run without named local Docker containers
that its disk guard can stop.

See [ARTICLE.md](ARTICLE.md) for the method and its evidence limits.

## Configure

1. Copy `config.example.json` to `config.json` and replace every placeholder.
   Keep `config.json` local because RPC URLs may contain credentials.
2. Pin and independently verify the target's L1 factory, chain ID, game type,
   MessagePasser address, L2 genesis or migration state, rollup configuration,
   activation schedule, and deployed client versions. These vary by rollup.
3. Set `game_claim_method` to `rootClaimByChainId` for a super-root game or
   `rootClaim` for a classic single-chain game. Set `checkpoint_height.mode` to
   `timestamp_linear` only after validating the anchor timestamp, anchor L2
   block, and block time; otherwise use `block_number_method` if the game
   exposes `l2BlockNumber()`.
4. Run an isolated local derivation stack. The rollup node should read L1 and
   beacon data, drive the local execution engine in consensus-layer mode, and
   accept no L2 block feed or P2P peers. Bind its read-only RPC and the local
   execution RPC to loopback. Configure `docker_containers` with only those
   local replay containers if using `monitor.py` or `supervise.py`.

## Install and run

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q

python discover_games.py --config config.json --max-new 12
python verify_checkpoint.py --config config.json 0
python parity.py --config config.json 0

# Optional: start the named, stopped local Docker containers and supervise a
# finite range of checkpoints. The 5 GiB disk guard stops them on low space.
python supervise.py --config config.json --start-index 0 --through-index 5
```

`discover_games.py` checks new L1 Factory entries against the **published** L2
view before appending them to `data/games.json`. That is only a schedule
prefilter. `verify_checkpoint.py` is the independent check: it compares a
locally derived block, authenticates an EIP-1186 account proof under the
**local** state root, recomputes the per-chain output root, and re-reads the
claim directly from L1. `parity.py` optionally compares HTTPS, WSS, and a
second JSON-RPC view; it does not replace independent derivation.

The monitor uses the op-node **safe** L2 head, stops on a mismatch, and stops
its configured local containers on a disk-floor event or interruption. The
supervisor can restart only after a logged disk-floor stop and sustained free
space. It refuses to restart after a mismatch, RPC failure, or manual stop.

## Limitations

- The checkpoint formula implemented here is version 0 unless a different
  32-byte `output_root_version` is explicitly configured and verified. A
  different rollup output format needs corresponding code, not just a value
  change.
- The generic discovery modes support a validated linear timestamp mapping or
  a game that exposes `l2BlockNumber()`. They do not infer arbitrary
  deployment-specific game layouts or hardfork schedules.
- `eth_getProof` must be available at the historical checkpoint on the
  reference RPC. The proof bytes are untrusted until authenticated against the
  local state root. If the endpoint cannot serve historical proofs, mark that
  checkpoint unavailable or capture a local proof while it is the current
  head; do not substitute an unverified `storageHash`.
- A matching checkpoint does not prove every block, withdrawal, or future
  output safe. An incorrect claim requires independent reproduction and the
  applicable dispute mechanism; these scripts do not submit a challenge.
