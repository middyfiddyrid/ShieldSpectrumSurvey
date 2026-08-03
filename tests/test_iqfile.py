"""Unit tests for the SigMF IQ record/replay path (survey/iqfile.py + the CLIs).

WHAT THESE TESTS ARE FOR. The whole point of the record/replay harness is that
the operator can trust a replay to be the SAME signal the radio handed up, so
that when a decode fails he knows the failure is in the decoder and not in the
plumbing. Every test here is aimed at that one claim:

  * complex64 round-trips EXACTLY — not "close", not "correlates well". If the
    container quantised, an off-air bit-order experiment would be run against
    slightly different samples every time and the results would be noise.
  * the metadata that makes a recording reproducible (centre frequency, sample
    rate, time, device, gain, note) survives the trip;
  * chunked reads and a bounded read return the identical array, so streaming a
    1 GB capture is not a different experiment from loading a small one;
  * a truncated or byte-misaligned file is REPORTED, never zero-padded — silent
    padding would look exactly like a dead band, which is the single most
    expensive way to waste a field trip;
  * the sidecar satisfies SigMF's required fields, so the same file opens in
    inspectrum or GNU Radio when this decoder needs a second opinion;
  * and the end-to-end one that matters most: a synthetic P25 call built with
    p25's encoder and c4fm.encode_c4fm, written to disk, read back and pushed
    through the replay harness, recovers the same frames — NAC, DUIDs, RS passes
    and 100 % FEC-clean IMBE voice frames — as decoding the in-memory array
    directly. That proves the harness does not corrupt what passes through it.

All headless — no hardware, no radio.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from survey import ambe, c4fm, dmr, p25                             # noqa: E402
from survey.iqfile import (CLIP_LEVEL, DATA_EXT, DEFAULT_DATATYPE,   # noqa: E402
                           IQFileError, IQMeta, META_EXT, SIGMF_VERSION,
                           SigMFReader, SigMFWriter, paths_for,
                           read_sigmf, write_sigmf)
import iq_replay                                                     # noqa: E402

FS = 48000.0
NAC, TGID, SRC = 0x293, 4021, 1234567


def _iq(n=4096, seed=0, peak=0.5):
    """Random IQ scaled so the largest |I| or |Q| sits at `peak` of full scale.

    Bounded deliberately: an unbounded Gaussian would stray past the converter
    rails and trip the writer's clipping counter, which would make the clipping
    tests below meaningless. `peak=0.5` is also what a well-set-up capture
    actually looks like — comfortably clear of both the rails and the floor."""
    rng = np.random.default_rng(seed)
    a = (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    scale = peak / max(float(np.max(np.abs(a.real))), float(np.max(np.abs(a.imag))))
    return (a * scale).astype(np.complex64)


def _meta(**kw):
    base = dict(center_hz=851_012_500.0, sample_rate=FS, source="b220",
                hardware="B220 Mini (uhd) type=b200", gain_db=42.0,
                gain_mode="manual", note="bench capture, rooftop whip")
    base.update(kw)
    return IQMeta(**base)


def _p25_voice_frames(rng, pitch0=90):
    """Nine IMBE frames, properly FEC-coded by ambe.pack_imbe_frame().

    Real coded frames, not filler: that is what lets the end-to-end test assert
    100 % IMBE FEC-clean and therefore prove that the voice bits survive the
    write/read round trip bit for bit. A plausible, slowly-moving pitch index
    also exercises the field-order plausibility check."""
    out = []
    for i in range(p25.VOICE_FRAMES_PER_LDU):
        info = np.zeros(ambe.IMBE_INFO_BITS, dtype=np.uint8)
        info[:ambe.IMBE_PITCH_BITS] = [int(b) for b in format(pitch0 + i, "08b")]
        info[ambe.IMBE_PITCH_BITS:] = rng.integers(0, 2, ambe.IMBE_INFO_BITS - 8)
        out.append(list(ambe.pack_imbe_frame(info)))
    return out


def _p25_call_dibits(ldu_pairs=3, seed=7):
    rng = np.random.default_rng(seed)
    lc = p25.make_link_control(tgid=TGID, source_id=SRC)
    es = p25.make_encryption_sync()
    parts = [p25.encode_frame_dibits(p25.DUID_HDU, NAC,
                                     hdu_bits=p25.make_hdu_header(tgid=TGID))]
    for k in range(ldu_pairs):
        parts.append(p25.encode_frame_dibits(
            p25.DUID_LDU1, NAC, lc_bits=lc,
            voice=_p25_voice_frames(rng, 90 + k)))
        parts.append(p25.encode_frame_dibits(
            p25.DUID_LDU2, NAC, es_bits=es,
            voice=_p25_voice_frames(rng, 93 + k)))
    parts.append(p25.encode_frame_dibits(p25.DUID_TDULC, NAC, lc_bits=lc))
    return np.concatenate(parts).astype(np.uint8)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.base = os.path.join(self.dir, "cap")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ------------------------------------------------------------------ round trip
class TestRoundTrip(_Tmp):
    def test_complex64_round_trips_exactly(self):
        """Bit-for-bit, not approximately. A container that quantises would make
        every replay a slightly different experiment."""
        iq = _iq(10_000)
        write_sigmf(self.base, iq, _meta())
        back, _ = read_sigmf(self.base)
        self.assertEqual(back.dtype, np.complex64)
        self.assertEqual(back.size, iq.size)
        self.assertTrue(np.array_equal(back, iq))
        # and byte-identical, which array_equal would not catch for -0.0/NaN
        self.assertEqual(back.tobytes(), iq.tobytes())

    def test_writes_both_halves_of_the_pair(self):
        write_sigmf(self.base, _iq(64), _meta())
        self.assertTrue(os.path.exists(self.base + DATA_EXT))
        self.assertTrue(os.path.exists(self.base + META_EXT))

    def test_paths_for_accepts_either_half_or_the_base(self):
        d, m = paths_for(self.base)
        for candidate in (self.base, d, m):
            self.assertEqual(paths_for(candidate), (d, m))

    def test_streamed_append_matches_one_shot(self):
        """Recording in blocks (what the radio actually does) must produce the
        same file as writing the array in one go."""
        iq = _iq(5000, seed=3)
        with SigMFWriter(self.base, _meta()) as w:
            for i in range(0, iq.size, 501):
                w.append(iq[i:i + 501])
            w.close()
        back, _ = read_sigmf(self.base)
        self.assertTrue(np.array_equal(back, iq))

    def test_ci16_is_lossy_and_says_so(self):
        """ci16_le halves the file and is NOT exact. The test pins the size
        saving and the quantisation bound so nobody reaches for it expecting
        bit-exact replay."""
        iq = _iq(4096, seed=5)
        write_sigmf(self.base, iq, _meta(datatype="ci16_le"))
        back, meta = read_sigmf(self.base)
        self.assertEqual(meta.datatype, "ci16_le")
        self.assertEqual(os.path.getsize(self.base + DATA_EXT), iq.size * 4)
        self.assertFalse(np.array_equal(back, iq))
        # one int16 LSB either way, and nothing worse
        self.assertLess(float(np.max(np.abs(back.real - iq.real))), 2.0 / 32767.0)
        self.assertLess(float(np.max(np.abs(back.imag - iq.imag))), 2.0 / 32767.0)


# ------------------------------------------------------------------ metadata
class TestMetadata(_Tmp):
    def test_reproducibility_fields_survive(self):
        m = _meta(note="P25 control channel, 3rd floor, LOS to the tower")
        write_sigmf(self.base, _iq(256), m)
        _, back = read_sigmf(self.base)
        self.assertEqual(back.center_hz, 851_012_500.0)
        self.assertEqual(back.sample_rate, FS)
        self.assertEqual(back.datatype, DEFAULT_DATATYPE)
        self.assertEqual(back.source, "b220")
        self.assertEqual(back.hardware, "B220 Mini (uhd) type=b200")
        self.assertEqual(back.gain_db, 42.0)
        self.assertEqual(back.gain_mode, "manual")
        self.assertEqual(back.note, "P25 control channel, 3rd floor, LOS to the tower")
        self.assertTrue(back.utc_start.endswith("Z"))
        self.assertEqual(back.sample_count, 256)
        self.assertEqual(len(back.sha512), 128)

    def test_sample_rate_and_frequency_are_exact_floats(self):
        """Rounding either of these silently mis-scales every downstream symbol
        rate and every frequency offset."""
        m = _meta(center_hz=162_550_000.5, sample_rate=2_400_000.0)
        write_sigmf(self.base, _iq(256), m)
        _, back = read_sigmf(self.base)
        self.assertEqual(back.center_hz, 162_550_000.5)
        self.assertEqual(back.sample_rate, 2_400_000.0)

    def test_sidecar_has_the_sigmf_required_fields(self):
        """SigMF v1.0.0 hard requirements: core:datatype and core:version in
        `global`, and at least one capture segment with core:sample_start. We
        additionally REQUIRE core:sample_rate, because a replay cannot recover
        symbol timing without it."""
        write_sigmf(self.base, _iq(256), _meta())
        with open(self.base + META_EXT, "r", encoding="utf-8") as f:
            obj = json.load(f)
        self.assertIn("global", obj)
        self.assertIn("captures", obj)
        self.assertIn("annotations", obj)
        g = obj["global"]
        self.assertEqual(g["core:datatype"], "cf32_le")
        self.assertEqual(g["core:version"], SIGMF_VERSION)
        self.assertEqual(len(g["core:version"].split(".")), 3)
        self.assertIsInstance(g["core:sample_rate"], float)
        self.assertEqual(g["core:num_channels"], 1)
        cap = obj["captures"][0]
        self.assertEqual(cap["core:sample_start"], 0)
        self.assertIn("core:frequency", cap)
        # ISO-8601 UTC with the explicit Z the spec asks for
        self.assertRegex(cap["core:datetime"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        # non-core fields are namespaced and the namespace is declared
        self.assertIn({"name": "shield", "version": "1.0.0", "optional": True},
                      g["core:extensions"])
        for k in g:
            self.assertTrue(k.startswith("core:") or k.startswith("shield:"),
                            f"un-namespaced metadata key {k!r} would violate the spec")

    def test_missing_required_field_is_refused(self):
        write_sigmf(self.base, _iq(256), _meta())
        with open(self.base + META_EXT, "r", encoding="utf-8") as f:
            obj = json.load(f)
        del obj["global"]["core:datatype"]
        with open(self.base + META_EXT, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        with self.assertRaises(IQFileError) as cm:
            SigMFReader(self.base)
        self.assertIn("core:datatype", str(cm.exception))

    def test_missing_sample_rate_is_refused(self):
        write_sigmf(self.base, _iq(256), _meta())
        with open(self.base + META_EXT, "r", encoding="utf-8") as f:
            obj = json.load(f)
        del obj["global"]["core:sample_rate"]
        with open(self.base + META_EXT, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        with self.assertRaises(IQFileError):
            SigMFReader(self.base)

    def test_sidecar_exists_before_the_capture_finishes(self):
        """A crash mid-capture must still leave a usable sidecar, or the partial
        data file is an anonymous pile of bytes."""
        w = SigMFWriter(self.base, _meta())
        try:
            w.append(_iq(128))
            self.assertTrue(os.path.exists(self.base + META_EXT))
            with open(self.base + META_EXT, "r", encoding="utf-8") as f:
                obj = json.load(f)
            self.assertEqual(obj["captures"][0]["core:frequency"], 851_012_500.0)
            self.assertNotIn("shield:sample_count", obj["global"])
        finally:
            w.close()


# ------------------------------------------------------------------ chunking
class TestChunkedReads(_Tmp):
    def test_blocks_reassemble_to_the_bounded_read(self):
        iq = _iq(20_011, seed=11)
        write_sigmf(self.base, iq, _meta())
        with SigMFReader(self.base) as r:
            bounded = r.read(20_011, 0)
            for block in (1, 7, 1024, 20_011, 999_999):
                chunks = list(r.blocks(block=block))
                joined = np.concatenate(chunks) if chunks else np.zeros(0, np.complex64)
                self.assertTrue(np.array_equal(joined, bounded),
                                f"block size {block} did not reassemble")
                self.assertTrue(all(c.size <= block for c in chunks))
        self.assertTrue(np.array_equal(bounded, iq))

    def test_offset_and_count_select_the_right_window(self):
        iq = _iq(5000, seed=13)
        write_sigmf(self.base, iq, _meta())
        with SigMFReader(self.base) as r:
            self.assertTrue(np.array_equal(r.read(100, 250), iq[250:350]))
            joined = np.concatenate(list(r.blocks(block=64, start=250, count=100)))
            self.assertTrue(np.array_equal(joined, iq[250:350]))

    def test_reads_past_the_end_are_short_not_padded(self):
        iq = _iq(1000, seed=17)
        write_sigmf(self.base, iq, _meta())
        with SigMFReader(self.base) as r:
            got = r.read(5000, 900)
            self.assertEqual(got.size, 100)
            self.assertTrue(np.array_equal(got, iq[900:]))
            self.assertEqual(r.read(10, 1000).size, 0)


# ------------------------------------------------------------------ corruption
class TestCorruptionIsReported(_Tmp):
    def _truncate(self, keep_bytes):
        with open(self.base + DATA_EXT, "r+b") as f:
            f.truncate(keep_bytes)

    def test_truncated_file_raises_by_default(self):
        iq = _iq(4000, seed=19)
        write_sigmf(self.base, iq, _meta())
        self._truncate(1000 * 8)
        with self.assertRaises(IQFileError) as cm:
            SigMFReader(self.base)
        msg = str(cm.exception)
        self.assertIn("TRUNCATED", msg)
        self.assertIn("4000", msg)
        self.assertIn("1000", msg)

    def test_truncated_file_is_not_zero_padded(self):
        """The failure mode this guards against: a short capture silently read as
        zeros looks exactly like a dead band, and you cannot go back for it."""
        iq = _iq(4000, seed=23)
        write_sigmf(self.base, iq, _meta())
        self._truncate(1000 * 8)
        r = SigMFReader(self.base, strict=False)
        try:
            self.assertTrue(r.truncated)
            self.assertEqual(r.sample_count, 1000)
            self.assertEqual(r.declared_sample_count, 4000)
            got = r.read(4000, 0)
            self.assertEqual(got.size, 1000)               # short, NOT padded
            self.assertTrue(np.array_equal(got, iq[:1000]))
            self.assertFalse(np.any(got[-10:] == 0))
            self.assertTrue(any("TRUNCATED" in p for p in r.problems))
        finally:
            r.close()

    def test_byte_misaligned_file_is_reported(self):
        write_sigmf(self.base, _iq(500), _meta())
        with open(self.base + DATA_EXT, "ab") as f:
            f.write(b"\x01\x02\x03")
        with self.assertRaises(IQFileError) as cm:
            SigMFReader(self.base)
        self.assertIn("whole number", str(cm.exception))
        r = SigMFReader(self.base, strict=False)
        try:
            self.assertEqual(r.trailing_bytes, 3)
            self.assertEqual(r.sample_count, 500)
        finally:
            r.close()

    def test_unclosed_recording_is_flagged_not_trusted_silently(self):
        w = SigMFWriter(self.base, _meta())
        w.append(_iq(256))
        w._f.close()                    # simulate the process dying mid-capture
        w._closed = True
        with self.assertRaises(IQFileError) as cm:
            SigMFReader(self.base)
        self.assertIn("never closed cleanly", str(cm.exception))

    def test_missing_sidecar_or_data_is_refused(self):
        write_sigmf(self.base, _iq(256), _meta())
        os.remove(self.base + META_EXT)
        with self.assertRaises(IQFileError):
            SigMFReader(self.base)
        write_sigmf(self.base, _iq(256), _meta())
        os.remove(self.base + DATA_EXT)
        with self.assertRaises(IQFileError):
            SigMFReader(self.base)

    def test_sha512_detects_a_flipped_bit(self):
        write_sigmf(self.base, _iq(1000), _meta())
        with SigMFReader(self.base) as r:
            ok, _ = r.verify_sha512()
            self.assertTrue(ok)
        with open(self.base + DATA_EXT, "r+b") as f:
            f.seek(64)
            b = f.read(1)
            f.seek(64)
            f.write(bytes([b[0] ^ 0x01]))
        with SigMFReader(self.base) as r:
            ok, _ = r.verify_sha512()
            self.assertFalse(ok)


# ------------------------------------------------------------------ warnings
class TestCaptureWarnings(_Tmp):
    def test_clipping_is_counted_and_warned(self):
        """A clipped capture is the classic wasted field trip. It must be visible
        at record time, in the summary AND in the sidecar."""
        iq = _iq(1000, seed=29)
        iq[:50] = (1.5 + 1.5j)                 # pinned well past the rails
        s = write_sigmf(self.base, iq, _meta())
        self.assertEqual(s["clipped_samples"], 50)
        self.assertAlmostEqual(s["clipped_fraction"], 0.05)
        self.assertTrue(any("CLIPPING" in w for w in s["warnings"]))
        with SigMFReader(self.base) as r:
            self.assertEqual(r.meta.extra["shield:clipped_samples"], 50)
            self.assertEqual(r.meta.extra["shield:clip_level"], CLIP_LEVEL)

    def test_a_buried_capture_is_warned_about(self):
        s = write_sigmf(self.base, (_iq(1000) * 1e-4).astype(np.complex64), _meta())
        self.assertTrue(any("WEAK" in w for w in s["warnings"]))

    def test_an_empty_capture_is_warned_about(self):
        with SigMFWriter(self.base, _meta()) as w:
            s = w.close()
        self.assertEqual(s["samples"], 0)
        self.assertTrue(any("NO SAMPLES" in w for w in s["warnings"]))

    def test_a_healthy_capture_warns_about_nothing(self):
        s = write_sigmf(self.base, _iq(2000, seed=31), _meta())
        self.assertEqual(s["warnings"], [])


# ------------------------------------------------------------------ end to end
class TestP25EndToEnd(_Tmp):
    """THE test. A synthetic P25 call, through the file, out the other side.

    If the harness altered the samples in any way this fails, and it fails
    loudly — the FEC ladder collapses rather than degrading gracefully. That is
    exactly the property wanted: when a real off-air capture fails to decode, the
    operator needs to be able to rule the container out in one command.
    """

    def setUp(self):
        super().setUp()
        self.dibits = _p25_call_dibits(ldu_pairs=3)
        self.iq = c4fm.encode_c4fm(self.dibits, FS, noise=0.005, seed=3)
        write_sigmf(self.base, self.iq, _meta(sample_rate=FS))

    def _replay(self, **kw):
        iq, info = iq_replay.load_slice(self.base, 0.0, None, strict=True)
        sym = iq_replay.c4fm_trial(iq, float(info["sample_rate"]),
                                   invert=kw.get("invert", False),
                                   symbol_rate=c4fm.SYMBOL_RATE)
        lad = iq_replay.p25_ladder(np.asarray(sym["dibits"], dtype=np.uint8))
        voice = iq_replay.p25_voice_fec(lad["frames"])
        return iq, sym, lad, voice

    def test_samples_survive_the_file(self):
        back, meta = read_sigmf(self.base)
        self.assertTrue(np.array_equal(back, self.iq))
        self.assertEqual(meta.sample_rate, FS)

    def test_frames_are_recovered_end_to_end(self):
        _iqr, sym, lad, voice = self._replay()
        self.assertGreaterEqual(sym["rails_used"], 4)
        self.assertGreater(sym["quality"], 0.8)
        # sync -> NID BCH -> RS: every rung, with denominators
        self.assertGreaterEqual(lad["sync_locks"], 6)
        self.assertEqual(lad["nid_fail"], 0)
        self.assertGreaterEqual(lad["nid_pass"], 6)
        self.assertGreaterEqual(lad["rs_pass"], 6)
        self.assertEqual(set(lad["nacs"]), {f"0x{NAC:03X}"})
        self.assertIn("LDU1", lad["duids"])
        self.assertIn("LDU2", lad["duids"])
        # and the vocoder rung: real IMBE-coded frames must come back FEC-clean
        self.assertGreaterEqual(voice["total"], 9 * 6)
        self.assertEqual(voice["clean"], voice["total"])
        self.assertEqual(max(voice["errors"]), 0)

    def test_replay_matches_decoding_the_array_directly(self):
        """The harness must be transparent: identical verdict from the file and
        from the in-memory array."""
        from_file = p25.decode_p25(read_sigmf(self.base)[0], FS)
        from_mem = p25.decode_p25(self.iq, FS)
        self.assertEqual(from_file["note"], from_mem["note"])
        self.assertEqual(from_file["stats"]["frames"], from_mem["stats"]["frames"])
        self.assertEqual(from_file["stats"]["nacs"], from_mem["stats"]["nacs"])
        self.assertEqual([c["tgid"] for c in from_file["calls"]],
                         [c["tgid"] for c in from_mem["calls"]])

    def test_call_metadata_is_exact(self):
        res = p25.decode_p25(read_sigmf(self.base)[0], FS)
        self.assertTrue(res["calls"])
        call = res["calls"][0]
        self.assertEqual(call["nac"], NAC)
        self.assertEqual(call["tgid"], TGID)
        self.assertEqual(call["source_id"], SRC)
        self.assertIs(call["encrypted"], False)

    def test_field_order_evidence_reads_as_pitch_on_a_known_good_stream(self):
        """The pitch-plausibility check is only useful if it can tell a real
        pitch field from noise. Here the pitch really is where we say it is, so
        it must read that way — this is the positive control for the diagnostic
        the operator will point at real traffic."""
        _iqr, _sym, _lad, voice = self._replay()
        ev = iq_replay.pitch_plausibility(voice["reports"])
        self.assertGreater(ev["n"], 0)
        self.assertEqual(ev["reserved"], 0)
        self.assertLess(ev["mean_step"], 10.0)          # random bits give ~85
        self.assertLess(ev["max"], ambe.IMBE_PITCH_RESERVED)

    def test_a_chunked_replay_sees_the_same_signal(self):
        """Streaming a big capture must not change the answer versus a bounded
        read — that is what makes a 1 GB file usable at all."""
        with SigMFReader(self.base) as r:
            joined = np.concatenate(list(r.blocks(block=4096)))
        self.assertTrue(np.array_equal(joined, self.iq))
        self.assertEqual(p25.decode_p25(joined, FS)["stats"]["frames"],
                         p25.decode_p25(self.iq, FS)["stats"]["frames"])

    def test_cli_runs_and_prints_the_ladder(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = iq_replay.main(["--file", self.base, "--decoder", "p25",
                                 "--seconds", "0"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        for expect in ("P25 PHASE 1 FRAME LADDER", "NID BCH(63,16)",
                       "Reed-Solomon", "voice frames FEC-CLEAN",
                       f"0x{NAC:03X}", "FIELD-ORDER EVIDENCE"):
            self.assertIn(expect, out)

    def test_cli_sweep_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = iq_replay.main(["--file", self.base, "--decoder", "p25",
                                 "--seconds", "0", "--invert-polarity", "both",
                                 "--symbol-rate-search", "4790:4810:3"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("PARAMETER SWEEP", out)
        self.assertIn("BEST:", out)

    def test_cli_refuses_a_truncated_file_by_default(self):
        with open(self.base + DATA_EXT, "r+b") as f:
            f.truncate(800)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = iq_replay.main(["--file", self.base, "--decoder", "p25"])
        self.assertEqual(rc, 2)
        self.assertIn("TRUNCATED", buf.getvalue())


class TestDMREndToEnd(_Tmp):
    """DMR through the same pipe, plus a check on the harness's own AMBE
    extraction convention.

    dmr.py deliberately locates the AMBE frames without decoding them, so
    iq_replay.dmr_voice_bits() is the replay harness's own reading of the burst
    layout. Feeding it bursts whose voice slots hold REAL AMBE-coded frames is
    the only way to tell whether that reading is right — and it has to be right,
    or a genuine off-air vocoder failure would be blamed on the wrong layer."""

    def _lc(self):
        return dmr.pack_full_lc(dmr.FLCO_GROUP_VOICE, 0, 0, TGID, 2621102) + [0] * 24

    def _ambe_voice_burst(self, sync_name=None, emb=None, seed=0):
        """A voice burst whose 216 voice bits are three real AMBE-coded frames."""
        rng = np.random.default_rng(seed)
        frames = []
        for _ in range(3):
            info = rng.integers(0, 2, ambe.AMBE_INFO_BITS).astype(np.uint8)
            frames.append(ambe.pack_ambe_frame(info))
        v = [int(b) for b in np.concatenate(frames)]
        self.assertEqual(len(v), 216)
        bits = v[:108]
        if sync_name is not None:
            bits += dmr._int_to_bits(dmr.SYNC_PATTERNS[sync_name], dmr.SYNC_BITS)
        else:
            cw = dmr.emb_encode(dmr.pack_emb(*emb))
            bits += dmr._int_to_bits(cw >> 8, 8)
            bits += [0] * 32
            bits += dmr._int_to_bits(cw & 0xFF, 8)
        bits += v[108:]
        return bits

    def test_dmr_survives_the_file_and_ambe_extraction_is_consistent(self):
        bursts = [dmr.build_data_burst("bs_data", 1, dmr.DT_VOICE_LC_HEADER,
                                       self._lc()),
                  self._ambe_voice_burst("bs_voice", seed=1),
                  self._ambe_voice_burst(emb=(1, 0, 0), seed=2),
                  dmr.build_data_burst("bs_data", 1, dmr.DT_TERMINATOR_LC,
                                       self._lc())]
        iq = dmr.encode_dmr(bursts, FS)
        write_sigmf(self.base, iq, _meta(center_hz=451_500_000.0, sample_rate=FS))
        back, meta = read_sigmf(self.base)
        self.assertTrue(np.array_equal(back, iq))

        sym = c4fm.demod_c4fm(back, meta.sample_rate)
        out = dmr.decode_dmr(back, meta.sample_rate, symbols=sym)
        self.assertGreaterEqual(out["stats"]["bptc_ok"], 2)
        self.assertEqual(out["stats"]["colour_codes"], [1])
        calls = [m for m in out["messages"] if m["type"] == "voice_call"]
        self.assertTrue(calls)
        self.assertEqual(calls[0]["talkgroup"], TGID)

        voice = iq_replay.dmr_voice_fec(
            np.asarray(sym["dibits"], dtype=np.uint8), out["bursts"],
            bool(out["stats"]["inverted"]))
        self.assertGreaterEqual(voice["total"], 6)
        # every extracted frame must FEC-clean: the bits went in AMBE-coded and
        # the harness's burst offsets are the only thing that could break that
        self.assertEqual(voice["clean"], voice["total"])

    def test_dmr_voice_bits_inverts_the_burst_layout(self):
        """Direct check of the offsets, independent of any radio maths."""
        burst = self._ambe_voice_burst("bs_voice", seed=5)
        frames = iq_replay.dmr_voice_bits(burst)
        self.assertEqual(len(frames), 3)
        for f in frames:
            self.assertEqual(len(f), ambe.AMBE_FRAME_BITS)
            self.assertTrue(ambe.unpack_ambe_frame(f)["fec_ok"])
        # a burst of the wrong length yields nothing rather than garbage
        self.assertEqual(iq_replay.dmr_voice_bits(burst[:100]), [])


# ------------------------------------------------------------------ replay CLI
class TestReplayHelpers(_Tmp):
    def test_symbol_rate_grid_forms(self):
        self.assertEqual(iq_replay.symbol_rate_grid(""), [])
        auto = iq_replay.symbol_rate_grid("auto")
        self.assertEqual(len(auto), 9)
        self.assertAlmostEqual(auto[4], c4fm.SYMBOL_RATE)
        self.assertEqual(iq_replay.symbol_rate_grid("4700:4900:3"),
                         [4700.0, 4800.0, 4900.0])
        self.assertEqual(iq_replay.symbol_rate_grid("4750,4800"), [4750.0, 4800.0])

    def test_noise_capture_reports_nothing_rather_than_frames(self):
        """The honesty case: a file of noise must not produce a P25 ladder full
        of numbers. Sync may occasionally lock; the FEC must not follow."""
        write_sigmf(self.base, _iq(40_000, seed=101), _meta())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = iq_replay.main(["--file", self.base, "--decoder", "p25",
                                 "--seconds", "0"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("P25 PHASE 1 FRAME LADDER", out)
        iq, info = iq_replay.load_slice(self.base, 0.0, None, strict=True)
        sym = iq_replay.c4fm_trial(iq, float(info["sample_rate"]), False,
                                   c4fm.SYMBOL_RATE)
        lad = iq_replay.p25_ladder(np.asarray(sym["dibits"], dtype=np.uint8))
        self.assertEqual(lad["rs_pass"], 0)
        self.assertEqual(p25.decode_p25(iq, FS)["calls"], [])

    def test_auto_path_runs_on_a_p25_file(self):
        dibits = _p25_call_dibits(ldu_pairs=2)
        write_sigmf(self.base, c4fm.encode_c4fm(dibits, FS, noise=0.005, seed=1),
                    _meta())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = iq_replay.main(["--file", self.base, "--decoder", "auto",
                                 "--seconds", "0"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("SIGNAL FEATURES", out)
        self.assertIn("VERDICT", out)
        # the router has no P25 entry, so the probe is what finds the frames
        self.assertIn("C4FM FRAME PROBE", out)
        self.assertIn("P25 PHASE 1 FRAME LADDER", out)


# ------------------------------------------------------------------ flask route
class _StubSDR:
    """Finite stream_iq so the synchronous route completes without a radio."""
    is_swept_sdr = True

    def __init__(self):
        import types
        self.info = types.SimpleNamespace(
            model="StubSDR", driver="stub", port="SIM",
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0,
                                           side="right")],
            simulated=True, warnings=[])

    def is_alive(self):
        return True

    def capture_iq(self, center_hz, fs, n):
        return _iq(int(n), seed=int(center_hz) % 1000)

    def stream_iq(self, center_hz, fs, n_block, stop):
        while not stop.is_set():
            yield _iq(int(n_block), seed=41)


class TestSigmfRoute(unittest.TestCase):
    """The optional UI button. It is the small sibling of the CLI, so the test
    only pins that it writes a real SigMF pair, records the row, and surfaces
    the capture warnings instead of swallowing them."""

    def setUp(self):
        from survey.config import Config
        from survey.db import Database
        from survey.server import create_app
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.cfg.data["pluto"]["stream_sample_rate"] = 48000
        self.cfg.data["pluto"]["listen_block_seconds"] = 0.05
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.app = create_app(self.db, self.cfg)
        self.st = self.app.config["APP_STATE"]
        self.st.controller.device = _StubSDR()
        self.st.controller.active_source = "pluto"
        self.c = self.app.test_client()
        self.rec_dir = self.cfg.abs_path("recordings")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_route_writes_a_readable_sigmf_pair(self):
        r = self.c.post("/api/record/iq_sigmf",
                        json={"center_mhz": 851.0125, "seconds": 0.25,
                              "note": "bench button"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["samples"], 12000)          # 0.25 s at 48 kS/s
        data_path = os.path.join(self.rec_dir, body["file_path"])
        with SigMFReader(data_path) as reader:
            self.assertEqual(reader.sample_rate, 48000.0)
            self.assertAlmostEqual(reader.center_hz, 851_012_500.0)
            self.assertEqual(reader.meta.note, "bench button")
            self.assertEqual(reader.sample_count, 12000)
        recs = self.c.get("/api/recordings").get_json()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "iq")

    def test_route_caps_the_length(self):
        r = self.c.post("/api/record/iq_sigmf",
                        json={"center_mhz": 433.0, "seconds": 9999,
                              "sample_rate": 8000})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["samples"], int(8000 * 30))

    def test_route_needs_a_session_and_a_frequency(self):
        from survey.config import Config
        from survey.db import Database
        from survey.server import create_app
        d = tempfile.mkdtemp()
        try:
            c = create_app(Database(os.path.join(d, "e.db")),
                           Config(os.path.join(d, "config.yaml"))).test_client()
            self.assertEqual(c.post("/api/record/iq_sigmf",
                                    json={"center_mhz": 433.0}).status_code, 400)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(self.c.post("/api/record/iq_sigmf", json={}).status_code,
                         400)


if __name__ == "__main__":
    unittest.main()
