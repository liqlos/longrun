import unittest

from longrun.adapters import load_adapter


class RoundGateTest(unittest.TestCase):
    """The round gate is the cheap check that stands between a broken workspace and a paid judgement.

    Measured: a CS0128 introduced in one round surfaced two seconds before the *next* round ended, after 37
    minutes and $5.95. A compile check costs ~23 s on that project; a capture costs ~140 s; an evaluation ~$5.
    """

    def test_vr_visual_ships_a_default_gate(self):
        a = load_adapter("vr_visual", None)
        self.assertTrue(a.round_gate_commands, "vr_visual must gate rounds by default")
        self.assertTrue(all("cmd" in c for c in a.round_gate_commands))

    def test_project_can_replace_the_gate_with_its_real_build(self):
        a = load_adapter("vr_visual", {"round_gate": [{"cmd": "Unity -batchmode -quit -executeMethod X.Compile",
                                                       "timeout_seconds": 300}]})
        self.assertEqual(len(a.round_gate_commands), 1)
        self.assertIn("executeMethod", a.round_gate_commands[0]["cmd"])
        self.assertEqual(a.round_gate_commands[0]["kind"], "check")

    def test_adapters_without_a_gate_are_unaffected(self):
        a = load_adapter("software", None)
        self.assertEqual(a.round_gate_commands, [])

    def test_gate_is_published_in_the_adapter_descriptor(self):
        d = load_adapter("vr_visual", None).to_json()
        self.assertIn("round_gate_commands", d)


if __name__ == "__main__":
    unittest.main()
