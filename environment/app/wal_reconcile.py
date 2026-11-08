#!/usr/bin/env python3
"""WAL Crash-Recovery Reconciler CLI (contains production bugs to be diagnosed and fixed)."""
import argparse
import json
import pathlib
import struct
import zlib

HEADER_FMT = ">4sBHQHII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def reconcile_wal(wal_dir: pathlib.Path) -> dict:
    accounts = {}
    corrupted_frames_skipped = 0
    uncommitted_frames_dropped = 0
    watermark = {"epoch": 0, "seq": 0}

    for seg_path in sorted(wal_dir.glob("*.wal")):
        data = seg_path.read_bytes()
        offset = 0
        while offset < len(data):
            # BUG 1: Does not guard against truncated header/payload at tail; raises struct.error
            magic, flags, epoch, seq, klen, vlen, crc = struct.unpack_from(HEADER_FMT, data, offset)
            if magic != b"WAL1":
                corrupted_frames_skipped += 1
                break
            key_start = offset + HEADER_SIZE
            val_start = key_start + klen
            frame_end = val_start + vlen

            key_bytes = data[key_start:val_start]
            raw_val = data[val_start:frame_end]

            # BUG 2: Decompresses BEFORE verifying CRC32, breaking CRC check on compressed frames
            if flags & 0x01:
                val_bytes = zlib.decompress(raw_val)
            else:
                val_bytes = raw_val

            calc_crc = zlib.crc32(data[offset + 4 : offset + 21] + key_bytes + val_bytes) & 0xFFFFFFFF
            if calc_crc != crc:
                corrupted_frames_skipped += 1
                offset = frame_end
                continue

            key = key_bytes.decode("utf-8")
            # BUG 3: Applies frames immediately without waiting for an epoch commit barrier (0x04)
            if flags & 0x04:
                watermark = {"epoch": epoch, "seq": seq}
            elif flags & 0x02:
                accounts.pop(key, None)
            else:
                payload = json.loads(val_bytes.decode("utf-8"))
                accounts[key] = {
                    "bal": int(payload["bal"]),
                    "ccy": payload["ccy"],
                    "epoch": epoch,
                    "seq": seq,
                }
            offset = frame_end

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
