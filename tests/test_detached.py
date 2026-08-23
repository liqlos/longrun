import signal
import unittest

from longrun.process import ChildRunner


class DetachedRunTest(unittest.TestCase):
    """`nohup longrun go …` must survive the terminal closing.

    nohup works by setting SIGHUP to SIG_IGN before exec. A program that installs its own SIGHUP handler
    unconditionally re-arms the shutdown nohup was used to prevent — which is how one overnight chain died
    two hours into a round with nothing committed.
    """

    def setUp(self):
        self.saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    def tearDown(self):
        for s, h in self.saved.items():
            signal.signal(s, h)

    def test_sighup_ignored_by_parent_is_left_ignored(self):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        r = ChildRunner()
        r.install_signal_handlers()
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN,
                      "nohup's SIG_IGN was overridden: a detached chain would still die on terminal close")
        r.restore_signal_handlers()

    def test_normal_signals_are_still_handled_when_not_ignored(self):
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        r = ChildRunner()
        r.install_signal_handlers()
        self.assertIsNot(signal.getsignal(signal.SIGHUP), signal.SIG_DFL,
                         "an attached run must still shut down cleanly on SIGHUP")
        self.assertIsNot(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        r.restore_signal_handlers()


if __name__ == "__main__":
    unittest.main()
