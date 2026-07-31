"""Static integration checks for the Sentry direct spectrum probe."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestSpectrumProbeUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "survey" / "web" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "survey" / "web" / "app.js").read_text(encoding="utf-8")

    def test_probe_is_mounted_on_spectrum_and_waterfall(self):
        for element_id in (
            "sp-layer-chart",
            "sp-layer-waterfall",
            "spectrum-probe-card",
            "sp-open-monitor",
            "sp-unpin",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("SENTRY <span>RF INTELLIGENCE</span> // SWEEP CONSOLE", self.html)

    def test_pin_starts_passive_metadata_analysis(self):
        self.assertIn("metadata_only: true", self.js)
        self.assertIn("initSpectrumProbe();", self.js)
        pin_body = self.js.split("function spectrumProbePin(target)", 1)[1].split(
            "function spectrumProbeRelease", 1
        )[0]
        self.assertIn("spectrumProbeAnalyzePinned();", pin_body)
        self.assertIn("spectrumProbeAccumulate(target);", pin_body)
        self.assertNotIn("listenAt(", pin_body)

    def test_receiver_requires_the_explicit_action(self):
        monitor_body = self.js.split('const monitor = $("sp-open-monitor")', 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("listenAt(", monitor_body)


if __name__ == "__main__":
    unittest.main()
