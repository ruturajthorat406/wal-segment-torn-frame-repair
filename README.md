# WAL Segment Torn-Frame & Split-Brain Epoch Reconciler

A Linux systems & storage-engine debugging benchmark task in [Harbor / Terminal-Bench](https://github.com/laude-institute/harbor) format.

## Problem Overview

After an ungraceful node power-off during primary-to-replica failover, the storage engine's Write-Ahead Log (WAL) compactor (`/app/wal_reconcile.py`) fails to recover the committed state from `/app/wal/*.wal`.

Three interacting systems bugs cause the failure:
1. **Torn trailing frames & CRC mismatches**: A partial write at the tail of an older segment raises an unhandled `struct.error` instead of halting segment scan at the first torn/corrupted frame boundary.
2. **Split-brain uncommitted epoch frames**: Frames written in an older leader epoch (`epoch=1`, `seq=105`) after that epoch's last commit barrier (`flags & 0x04`) are erroneously retained when a newer leader epoch (`epoch=2`) takes over from `seq=105`.
3. **Compressed frame payload ordering**: Compressed frames (`flags & 0x01`) verify CRC32 over the on-disk compressed payload bytes (`header[4:21] + key + raw_val`), not the post-`zlib.decompress` buffer.

## Repository Structure

```text
wal-segment-torn-frame-repair/
├── README.md
├── task.toml
├── instruction.md
├── environment/
│   ├── Dockerfile
│   └── app/
│       ├── wal_reconcile.py      # Buggy CLI utility in the container image
│       └── wal/                  # Binary WAL segments (000001.wal .. 000003.wal)
├── solution/
│   └── solve.sh                  # Reference oracle patch & execution script
└── tests/
    ├── test.sh                   # Harbor verifier entrypoint
    └── test_outputs.py           # Pytest suite (validates snapshot + dynamic WAL streams)
```

## Quick Verification (Local or Container)

```bash
TMP=$(mktemp -d)
cp -r environment/app "$TMP/app"
APP_ROOT="$TMP/app" bash solution/solve.sh
APP_ROOT="$TMP/app" pytest -v tests/test_outputs.py
rm -rf "$TMP"
```
