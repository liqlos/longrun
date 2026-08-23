import tempfile
import time
import unittest
from pathlib import Path

from longrun.adapters import load_adapter

CONTRACT = {"criteria": [
    {"id": "C1-look", "kind": "visual", "evidence_requirements": ["screenshot", "capture_manifest"]},
    {"id": "C2-doc", "kind": "docs", "evidence_requirements": ["doc"]},
]}


class HarvestTest(unittest.TestCase):
    """Four builder sessions one night were killed — by the timeout or the loop guard — *after* running the
    full verification. The captures were on disk and the ledger was empty, so the round scored as worthless and
    bought a repair: $26.80 and 112 minutes. The controller can hash those artifacts itself, so whether they
    count should not depend on the session surviving long enough to file them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "captures").mkdir()
        self.a = load_adapter("vr_visual", {"capture_dir": "captures"})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, body: bytes):
        p = self.ws / "captures" / name
        p.write_bytes(body)
        return p

    def test_it_picks_up_an_unfiled_capture(self):
        t0 = time.time() - 5
        self.write("establishing_01.png", b"\x89PNG one")
        recs = self.a.post_round(self.ws, t0, CONTRACT, set())
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "capture_manifest")
        self.assertTrue(recs[0]["data"]["harvested"])
        self.assertEqual(len(recs[0]["artifacts"]), 1)

    def test_it_binds_only_to_criteria_that_want_frames(self):
        self.write("establishing_01.png", b"\x89PNG one")
        recs = self.a.post_round(self.ws, time.time() - 5, CONTRACT, set())
        self.assertEqual(recs[0]["criterion_ids"], ["C1-look"])

    def test_a_byte_identical_re_save_is_not_harvested(self):
        import hashlib
        body = b"\x89PNG same"
        self.write("establishing_01.png", body)
        known = {hashlib.sha256(body).hexdigest()}
        self.assertEqual(self.a.post_round(self.ws, time.time() - 5, CONTRACT, known), [])

    def test_captures_older_than_the_round_are_ignored(self):
        p = self.write("establishing_old.png", b"\x89PNG old")
        import os
        os.utime(p, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(self.a.post_round(self.ws, time.time() - 60, CONTRACT, set()), [])

    def test_nothing_to_harvest_yields_nothing(self):
        self.assertEqual(self.a.post_round(self.ws, time.time() - 5, CONTRACT, set()), [])

    def test_a_contract_with_no_visual_criteria_is_left_alone(self):
        self.write("establishing_01.png", b"\x89PNG one")
        docs_only = {"criteria": [{"id": "C1-doc", "kind": "docs", "evidence_requirements": ["doc"]}]}
        self.assertEqual(self.a.post_round(self.ws, time.time() - 5, docs_only, set()), [])

    def test_other_adapters_harvest_nothing(self):
        self.assertEqual(load_adapter("software", None).post_round(self.ws, 0.0, CONTRACT, set()), [])


if __name__ == "__main__":
    unittest.main()
