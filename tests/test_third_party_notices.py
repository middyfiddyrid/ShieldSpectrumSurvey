"""The mbelib ISC attribution is a LICENCE CONDITION, not a courtesy.

survey/imbe_tables.py ships mbelib's IMBE quantizer tables. ISC permits that in
a closed-source commercial product on one condition: the copyright and
permission notice appears in all copies. A comment inside a source file the
customer never sees does not discharge it — the notice has to travel with the
product.

These tests exist so the obligation cannot be quietly lost to a refactor, a
file move, or someone tidying up a "redundant" markdown file.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from survey.ambe import capability                              # noqa: E402

NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"


class ThirdPartyNoticeTest(unittest.TestCase):
    def test_notices_file_exists(self):
        self.assertTrue(NOTICES.exists(),
                        "THIRD_PARTY_NOTICES.md is missing — shipping the mbelib "
                        "tables without it breaches the ISC licence condition")

    def test_isc_notice_is_present_verbatim(self):
        """ISC requires the copyright line AND the permission paragraph."""
        text = NOTICES.read_text(encoding="utf-8")
        self.assertIn("Copyright (C) 2010 mbelib Author", text)
        self.assertIn("Permission to use, copy, modify, and/or distribute", text)
        self.assertIn("appear in all copies", text)

    def test_the_gpl_forks_stay_documented_as_unusable(self):
        """mbelib-neo is the actively-maintained fork and is GPL-2.0. If this
        warning is ever dropped, someone updates the tables from it and
        relicenses the whole product by accident."""
        text = NOTICES.read_text(encoding="utf-8")
        for trap in ("mbelib-neo", "imbe_vocoder", "jmbe"):
            self.assertIn(trap, text, f"{trap} licence trap no longer documented")
        self.assertIn("GPL", text)

    def test_capability_does_not_claim_an_undischarged_obligation(self):
        """capability() is what the UI and report layer read. It must not still
        be telling operators the attribution is outstanding once it is done."""
        attr = capability()["spectral_amplitudes"]["attribution_required"]
        self.assertIn("THIRD_PARTY_NOTICES.md", attr)
        self.assertNotIn("NOT yet discharged", attr)

    def test_tables_module_still_names_its_source(self):
        """The in-source provenance has to survive too — it is what tells the
        next engineer WHICH mbelib the values may come from."""
        src = (ROOT / "survey" / "imbe_tables.py").read_text(encoding="utf-8")
        self.assertIn("szechyjs/mbelib", src)
        self.assertIn("ISC", src)


if __name__ == "__main__":
    unittest.main()
