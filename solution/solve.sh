#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"

cat << 'EOF' > "${APP_ROOT}/wal_reconcile.py"
#!/usr/bin/env python3
"""Fixed WAL Crash-Recovery Reconciler CLI."""
import argparse
import json
import pathlib
import struct
import zlib

HEADER_FMT = ">4sBHQHII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def reconcile_wal(wal_dir: pathlib.Path) -> dict:
    accounts: dict[str, dict] = {}
    corrupted_frames_skipped = 0
    uncommitted_frames_dropped = 0
    watermark = {"epoch": 0, "seq": 0}

    # Buffer uncommitted frames per epoch: list of (epoch, seq, flags, key, val_bytes)
    pending_epoch: int | None = None
    pending_frames: list[tuple[int, int, int, str, bytes]] = []

    for seg_path in sorted(wal_dir.glob("*.wal")):
        data = seg_path.read_bytes()
        offset = 0
        while offset < len(data):
            if len(data) - offset < HEADER_SIZE:
                corrupted_frames_skipped += 1
                break

            magic, flags, epoch, seq, klen, vlen, crc = struct.unpack_from(HEADER_FMT, data, offset)
            if magic != b"WAL1":
                corrupted_frames_skipped += 1
                break

            key_start = offset + HEADER_SIZE
            val_start = key_start + klen
            frame_end = val_start + vlen
            if frame_end > len(data):
                corrupted_frames_skipped += 1
                break

            key_bytes = data[key_start:val_start]
            raw_val = data[val_start:frame_end]

            calc_crc = zlib.crc32(data[offset + 4 : offset + 21] + key_bytes + raw_val) & 0xFFFFFFFF
            if calc_crc != crc:
                corrupted_frames_skipped += 1
                break

            try:
                key = key_bytes.decode("utf-8")
                val_bytes = zlib.decompress(raw_val) if (flags & 0x01) else raw_val
            except Exception:
                corrupted_frames_skipped += 1
                break

            # If epoch changes before a barrier, any uncommitted frames from prior epoch are dropped
            if pending_epoch is not None and epoch != pending_epoch:
                uncommitted_frames_dropped += len(pending_frames)
                pending_frames.clear()
            pending_epoch = epoch

            if flags & 0x04:
                # Commit all buffered frames in this epoch up to barrier seq
                for f_epoch, f_seq, f_flags, f_key, f_val in pending_frames:
                    if f_flags & 0x02:
                        accounts.pop(f_key, None)
                    else:
                        payload = json.loads(f_val.decode("utf-8"))
                        accounts[f_key] = {
                            "bal": int(payload["bal"]),
                            "ccy": payload["ccy"],
                            "epoch": f_epoch,
                            "seq": f_seq,
                        }
                pending_frames.clear()
                watermark = {"epoch": epoch, "seq": seq}
            else:
                pending_frames.append((epoch, seq, flags, key, val_bytes))

            offset = frame_end

    # Any trailing uncommitted frames after the final segment are dropped
    if pending_frames:
        uncommitted_frames_dropped += len(pending_frames)
        pending_frames.clear()

    total_bal = sum(v["bal"] for v in accounts.values())
    return {
        "committed_Watermark": watermark,
        "corrupted_frames_skipped": corrupted_frames_skipped,
        "uncommitted_frames_dropped": uncommitted_frames_dropped,
        "total_balance_usd": total_bal,
        "accounts": dict(sorted(accounts.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile binary WAL segments.")
    parser.add_argument("--wal-dir", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()

    snapshot = reconcile_wal(args.wal_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
EOF

chmod +x "${APP_ROOT}/wal_reconcile.py"
python3 "${APP_ROOT}/wal_reconcile.py" \
  --wal-dir "${APP_ROOT}/wal" \
  --out "${APP_ROOT}/output/ledger_snapshot.json"
