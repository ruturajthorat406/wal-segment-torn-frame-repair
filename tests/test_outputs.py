import json
import os
import pathlib
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib

APP_ROOT = pathlib.Path(os.environ.get("APP_ROOT", "/app"))
SNAPSHOT_PATH = APP_ROOT / "output" / "ledger_snapshot.json"
RECONCILE_SCRIPT = APP_ROOT / "wal_reconcile.py"


def _pack_frame(flags: int, epoch: int, seq: int, key: bytes, val: bytes, bad_crc: bool = False) -> bytes:
    hdr_body = struct.pack(">BHQHI", flags, epoch, seq, len(key), len(val))
    crc = zlib.crc32(hdr_body + key + val) & 0xFFFFFFFF
    if bad_crc:
        crc ^= 0xFFFFFFFF
    return b"WAL1" + hdr_body + struct.pack(">I", crc) + key + val


class TestWalReconciliation(unittest.TestCase):
    def test_snapshot_exists_and_schema(self):
        self.assertTrue(SNAPSHOT_PATH.is_file(), f"Missing snapshot file at {SNAPSHOT_PATH}")
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        expected_keys = {
            "committed_Watermark",
            "corrupted_frames_skipped",
            "uncommitted_frames_dropped",
            "total_balance_usd",
            "accounts",
        }
        self.assertEqual(set(data.keys()), expected_keys)

    def test_production_wal_reconciliation_values(self):
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["committed_Watermark"], {"epoch": 2, "seq": 108})
        self.assertEqual(data["corrupted_frames_skipped"], 1)
        self.assertEqual(data["uncommitted_frames_dropped"], 2)
        self.assertEqual(data["total_balance_usd"], 12970)
        self.assertEqual(
            data["accounts"],
            {
                "acct:1001": {"bal": 4750, "ccy": "USD", "epoch": 2, "seq": 105},
                "acct:1002": {"bal": 1820, "ccy": "USD", "epoch": 1, "seq": 102},
                "acct:1004": {"bal": 6400, "ccy": "USD", "epoch": 2, "seq": 107},
            },
        )

    def test_cli_handles_dynamic_torn_and_split_brain_streams(self):
        tmp_dir = pathlib.Path(tempfile.mkdtemp())
        try:
            wal_dir = tmp_dir / "wal_dynamic"
            wal_dir.mkdir()
            out_file = tmp_dir / "dynamic_out.json"

            # Segment 1: committed epoch 4, plus torn trailing bytes
            seg1 = (
                _pack_frame(0x01, 4, 1, b"acct:a", zlib.compress(b'{"bal":300,"ccy":"USD"}'))
                + _pack_frame(0x04, 4, 2, b"__barrier__", b"commit")
                + _pack_frame(0x00, 4, 3, b"acct:b", b'{"bal":999,"ccy":"USD"}')[:-4]
            )
            # Segment 2: epoch 4 uncommitted frame followed by epoch 5 takeover + commit
            seg2 = (
                _pack_frame(0x00, 4, 4, b"acct:a", b'{"bal":8888,"ccy":"USD"}')
                + _pack_frame(0x00, 5, 3, b"acct:c", b'{"bal":700,"ccy":"USD"}')
                + _pack_frame(0x04, 5, 4, b"__barrier__", b"commit")
            )
            (wal_dir / "000001.wal").write_bytes(seg1)
            (wal_dir / "000002.wal").write_bytes(seg2)

            subprocess.run(
                ["python3", str(RECONCILE_SCRIPT), "--wal-dir", str(wal_dir), "--out", str(out_file)],
                check=True,
            )
            res = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(res["committed_Watermark"], {"epoch": 5, "seq": 4})
            self.assertEqual(res["corrupted_frames_skipped"], 1)
            self.assertEqual(res["uncommitted_frames_dropped"], 1)
            self.assertEqual(res["total_balance_usd"], 1000)
            self.assertEqual(list(res["accounts"].keys()), ["acct:a", "acct:c"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
