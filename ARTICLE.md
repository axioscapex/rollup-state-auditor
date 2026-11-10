# Attacking the Rollup: How to Build a Custom State-Derivation Auditor

*Most people audit smart contracts. Very few audit the sequencer. Here is how
to build a local replay engine to catch a proposer lying about the state of L2.*

A rollup can have carefully audited bridge contracts and still publish an
incorrect state commitment. To test that boundary, you need an answer to one
question: **given the data actually posted to L1, what L2 state should an
independent node derive?**

There are two roles to keep separate. A sequencer orders L2 transactions and
produces blocks. A proposer posts an output claim on L1. They may be the same
operator, but a wrong claim does not by itself establish who caused it or why.
The auditor below checks the claim against independently executed L1-derived
state; it does not infer intent.

The core trust path is:

```text
Ethereum L1 blocks + beacon blob data
                 |
          isolated rollup node
                 |
       local execution engine
                 |
   local block, state root, storage root
                 |
      recomputed L2 output root ---- L1 dispute-game claim
                 |
       public L2 RPC comparison
          (corroboration only)
```

The public L2 RPC belongs on the right-hand side of that diagram. If it
supplies the block that your “independent” auditor executes, two matching RPC
responses establish little. The [OP Stack derivation specification](https://specs.optimism.io/protocol/derivation.html)
defines the real input path: authenticated batcher transactions, L1 deposits,
L1 attributes, and the previous L2 state are transformed into payloads for an
execution engine.

## Build the auditor in ten steps

1. **Pin the deployment.** Record the L1 Factory, game implementation, L2
   genesis or migration state, rollup configuration, batch inbox, authorized
   batcher, chain ID, MessagePasser, fork activation times, and client image
   digests. Check current proxy implementations and configuration against the
   source you intend to audit. A working node on the wrong fork schedule can
   produce a convincing but irrelevant mismatch.

2. **Choose an L1 trust anchor.** Use Ethereum execution data and the beacon
   data needed for blob batches. Pin a specific L1 block hash for every audit
   record; wait for the desired L1 finality before calling a result final.
   Validate batch sender and inbox at the L1 block where the data appears.

3. **Verify blob inputs where applicable.** For EIP-4844 batches, retrieve the
   matching sidecar, bind the blob to its KZG commitment and versioned hash,
   and then let the rollup node decode the actual channel data. The
   [Ecotone derivation rules](https://specs.optimism.io/protocol/ecotone/derivation.html)
   require blob retrieval for type-3 batcher transactions;
   [EIP-4844](https://eips.ethereum.org/EIPS/eip-4844) defines the commitment
   and versioned-hash relationship. A commitment match is an input check, not
   a substitute for L2 execution.

4. **Isolate the replay.** Start an op-node and an execution engine with their
   own data directory, a local Engine API connection, and loopback-only RPC.
   Disable L2 P2P and any sequencer feed. In the case study that inspired this
   guide, the rollup node used consensus-layer sync and P2P was disabled; the
   execution engine also had peer discovery and inbound/outbound peers off.
   Its L2 blocks therefore came from L1 derivation rather than the public L2
   node being checked.

5. **Start from the right base state.** A rollup may have a migration block
   rather than block zero as its starting point. Validate the imported genesis
   or migration hash and state root before moving forward. Keep the L1 receipt
   source and the rollup config pinned. A wrong base state invalidates every
   later comparison.

6. **Build a finite checkpoint schedule from L1.** Query
   `gameCount()` and `gameAtIndex()` on the deployed Factory. For each selected
   game, record its type, address, timestamp or L2 height, and chain-specific
   claim. Do not assume the Factory's overall `rootClaim()` equals a single
   chain's output: super-root games expose
   `rootClaimByChainId(chainId)`, as the
   [game implementation](https://github.com/ethereum-optimism/optimism/blob/develop/packages/contracts-bedrock/src/dispute/SuperPermissionedDisputeGame.sol)
   illustrates. Set a finite highest index for the run; otherwise a Factory
   that posts new games regularly creates an endless replay target.

7. **Wait for the derived safe head.** At each checkpoint, require the local
   rollup node's safe L2 head to reach the target. Compare the local block's
   number, hash, parent hash, state root, transaction root, receipt root,
   timestamp, and transactions with the public view. The public comparison is
   useful diagnostic evidence; the local derivation is the independent part.

8. **Authenticate the MessagePasser account proof.** Request an
   [`eth_getProof` account proof](https://eips.ethereum.org/EIPS/eip-1186) for
   the L2 MessagePasser at that exact block. A remote RPC may supply the proof
   bytes, but verify each RLP trie node and hex-prefix path against the
   **locally executed** state root. Only then extract the account's storage
   root. Reading the RPC's `storageHash` field without verifying its proof
   would put the untrusted public node back in the trust path.

9. **Recompute and compare the output.** For version-zero OP Stack outputs,
   the [specified commitment](https://specs.optimism.io/protocol/proposals.html)
   is:

   ```text
   keccak256(
       bytes32(0) || local_state_root ||
       proven_message_passer_storage_root || local_block_hash
   )
   ```

   Compare that value with the retained schedule and re-read the per-chain
   claim directly from the L1 game at a pinned L1 block. The sanitized
   [checkpoint verifier](verify_checkpoint.py) implements this boundary;
   [the proof code](common.py) authenticates the account path. A chain using a
   different output version needs its own verified formula.

10. **Treat mismatches as evidence to investigate, not instant findings.**
    Preserve the L1 block hash, config and image digests, local and public L2
    headers, proof nodes, computed root, L1 claim, and first divergent height.
    Recheck the fork schedule, batch data, L1 reorg status, and a second L1
    source. Stop the replay on a real checkpoint mismatch. A local auditor
    does not generate or submit an on-chain fault proof; that is a separate
    protocol step governed by the
    [fault-proof system](https://specs.optimism.io/fault-proof/).

## What happened in the case study

I used this design against a live OP Stack deployment during a bounded
security review. The isolated node replayed from the published migration
state. Twenty-six registered checkpoints, indices 0 through 25, matched the
locally executed block and state roots, an authenticated MessagePasser account
proof, and a fresh direct L1 per-chain claim. At one late checkpoint the
account proof had six trie nodes. A separate parity pass compared that block
and its receipt over public HTTPS, WSS, and an explorer RPC view.

For four newer games, I checked registration and L1 input fidelity. Eight
historical blob commitments also matched their versioned hashes. I did
**not** execute those four later outputs locally, so I did not call them
verified. No incorrect proposer claim was found in the executed checkpoints.

The mundane engineering mattered. The host had limited free disk space, so a
one-second guard stopped the local containers below a defined floor. A
supervisor restarted only after sustained recovery from a clean disk stop;
it refused to restart after a mismatch or manual interruption. The retained
logs made each stop and repeated checkpoint visible. Eventually, serially
checking new games on an unchanged code path had diminishing value, and the
run ended with the four-output gap recorded explicitly.

This is the useful standard for a state-derivation auditor: **make L1 data the
input, execute it yourself, authenticate any proof against your own state,
and compare the result with the claim on L1.** A match strengthens confidence
in that checkpoint. A mismatch gives you a precise place to investigate.
