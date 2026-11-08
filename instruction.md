# Fix the WAL Crash-Recovery Reconciler (`/app/wal_reconcile.py`)

Following an unclean node reset during leader failover, the WAL recovery utility at `/app/wal_reconcile.py` crashes when replaying the binary segment directory `/app/wal` and produces an incorrect state snapshot.

## Binary WAL Specification

Each `.wal` file in `/app/wal` (sorted lexicographically by filename, e.g., `000001.wal`, `000002.wal`, ...) consists of back-to-back binary frames with a 25-byte big-endian header:

| Offset | Size | Type | Field | Description |
|---|---|---|---|---|
| `0..4` | 4 B | ASCII | `magic` | Must equal `b"WAL1"` |
| `4..5` | 1 B | `uint8` | `flags` | Bitmask: `0x01` = zlib-compressed value, `0x02` = tombstone delete, `0x04` = epoch commit barrier |
| `5..7` | 2 B | `uint16` | `epoch` | Monotonically non-decreasing leader term number |
| `7..15` | 8 B | `uint64` | `seq` | Log sequence number |
| `15..17` | 2 B | `uint16` | `klen` | Key length in bytes |
| `17..21` | 4 B | `uint32` | `vlen` | On-disk value length in bytes |
| `21..25` | 4 B | `uint32` | `crc32` | Unsigned `zlib.crc32` over `header[4:21] + key_bytes + raw_val_bytes` |
| `25..25+klen` | `klen` B | UTF-8 | `key` | Record key (or `"__barrier__"` for commit barriers) |
| `25+klen..25+klen+vlen` | `vlen` B | bytes | `raw_val` | UTF-8 JSON payload (or zlib-compressed UTF-8 JSON if `flags & 0x01`) |

## Recovery Rules

1. **Segment Scan & Torn/Corrupt Tail Handling**:
   - Within each segment file, scan frames sequentially from offset `0`.
   - If a frame has fewer bytes remaining than required by the 25-byte header or `klen + vlen`, has an invalid magic header, or fails unsigned `crc32` verification against `header[4:21] + key + raw_val`, treat that frame as a torn/corrupt crash tail: **stop scanning the remainder of that segment file** (do not accept subsequent bytes in the same segment) and continue to the next segment file.
   - Count every rejected/torn frame across all segments in `corrupted_frames_skipped` (at most one per segment tail).

2. **Commit Barrier & Split-Brain Epoch Rollback**:
   - Frames within an epoch are buffered as **uncommitted** until an epoch commit barrier frame (`flags & 0x04`) with the same `epoch` is encountered.
   - When a valid commit barrier frame (`flags & 0x04`) is encountered at `(epoch, seq)`, all buffered uncommitted frames belonging to that `epoch` with sequence numbers strictly monotonic up to `seq` become **committed**.
   - Any frames that never reach a commit barrier (for example, frames written just before an epoch crash, or trailing frames at the end of the final segment after the last barrier) must **not** be applied to the committed key-value state and must be counted in `uncommitted_frames_dropped`.

3. **State Application**:
   - Apply only **committed** non-barrier frames in commit order:
     - Standard or compressed data frame (`not (flags & 0x02)`): if `flags & 0x01` is set, decompress `raw_val` with `zlib.decompress`, parse the UTF-8 JSON object (`{"bal": <int>, "ccy": <str>}`), and set `state[key] = {"bal": ..., "ccy": ..., "epoch": epoch, "seq": seq}`.
     - Tombstone frame (`flags & 0x02`): remove `key` from `state` if present.

## Deliverables

1. Fix `/app/wal_reconcile.py` so that running:
   ```bash
   python3 /app/wal_reconcile.py --wal-dir /app/wal --out /app/output/ledger_snapshot.json
   ```
   exits `0` and works for `/app/wal` as well as any valid/corrupted WAL directory passed via `--wal-dir` and `--out`.
2. Generate `/app/output/ledger_snapshot.json` with exact keys:
   ```json
   {
     "committed_Watermark": {"epoch": <int>, "seq": <int>},
     "corrupted_frames_skipped": <int>,
     "uncommitted_frames_dropped": <int>,
     "total_balance_usd": <int>,
     "accounts": {
       "<account_key>": {"bal": <int>, "ccy": "USD", "epoch": <int>, "seq": <int>}
     }
   }
   ```
   where `accounts` is sorted by key and `total_balance_usd` is the sum of `bal` across all surviving accounts in `accounts`.
