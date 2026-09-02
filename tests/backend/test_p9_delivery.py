"""Delivery encoding: what the browser downloads instead of raw PCM.

Masters and variants are rendered as 16-bit PCM and kept that way on disk,
because a variant is time-stretched from the master and a lossy source would
compound its own artefacts through every render. The copy that crosses the
network is compressed — roughly a fifth of the bytes, and audio download is
what dominates time-to-first-sound.

The property these tests exist for is ALIGNMENT, not size. Every perceptual
codec has a priming delay: the decoder is handed more samples than the encoder
was given, and the container carries the count to discard. If that count is
lost or ignored, the audio comes back shifted by a fixed amount — around 2100
samples for AAC, 312 for Opus. Nothing would sound broken in isolation, and a
single track would play normally. But the beat grid in `analysis_json` was
measured against the PCM, so every beat would sit that far from where the
mixer believes it is, and every transition would land late by the same amount.
It would read as a bug in the beat matcher, which is the expensive place to go
looking for it.
"""
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest import mock
from pathlib import Path

import numpy as np

from backend import audio_io, config, storage

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _signal(sr, seconds=6.0):
    """Broadband, non-repeating, deterministic — so cross-correlation has an
    unambiguous peak. A pure tone would correlate almost as well one period
    off, which is exactly the ambiguity this test must not have."""
    rng = np.random.default_rng(20260902)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    tone = 0.35 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 587 * t)
    noise = 0.05 * rng.standard_normal(n)
    # An amplitude envelope with beat-like transients to correlate against.
    env = 1.0 - 0.75 * ((t * 2) % 1.0)
    return np.clip((tone + noise) * env, -1.0, 1.0)


def _decode_to_samples(path, sr):
    with tempfile.TemporaryDirectory() as td:
        out = f"{td}/out.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-ac", "1", "-ar", str(sr), "-sample_fmt", "s16", out],
            check=True, capture_output=True)
        with wave.open(out, "rb") as w:
            return audio_io.pcm16_to_float(w.readframes(w.getnframes()))


def _best_offset(a, b, radius=4000):
    """Sample shift of `b` against `a` with the highest correlation."""
    a = np.ascontiguousarray(a, dtype=np.float64)
    b = np.ascontiguousarray(b, dtype=np.float64)
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        raise AssertionError("decoded audio contains non-finite samples")

    pad = radius + 500
    n = min(len(a), len(b)) - pad
    # Normalise once rather than dividing inside the loop: the dot of two unit
    # vectors is the correlation directly, and it keeps the running sum near 1
    # instead of near the sample count.
    x = a[pad:n] - a[pad:n].mean()
    x = x / (np.linalg.norm(x) or 1.0)

    best, offset = -2.0, 0
    # The BLAS behind `@` (Accelerate on Apple Silicon) raises FP flags from the
    # padding lanes of its vectorised inner loop, so a perfectly good dot
    # product reports divide-by-zero and overflow. Both inputs were checked
    # finite above and every result is asserted on, so the flags are noise —
    # suppressed here rather than globally, to keep the scope to this one loop.
    with np.errstate(all="ignore"):
        for off in range(-radius, radius + 1, 1):
            y = b[pad + off:n + off]
            if len(y) != len(x):
                continue
            y = y - y.mean()
            ny = np.linalg.norm(y)
            if ny <= 1e-12:             # a silent window correlates with nothing
                continue
            c = float(x @ (y / ny))
            if c > best:
                best, offset = c, off
    return offset, best


class TestKeys(unittest.TestCase):
    def test_served_keys_carry_the_delivery_extension(self):
        ext = config.delivery_ext()
        self.assertTrue(storage.master_key("1001").endswith(f".{ext}"))
        self.assertTrue(storage.variant_key("1001", 120).endswith(f".{ext}"))

    def test_the_pcm_master_keeps_its_own_key(self):
        """The two must not collide: one is rendered from, one is served, and
        a single key would mean the encoded copy overwrote the source every
        render — after which no variant could be produced again."""
        self.assertEqual(storage.master_source_key("1001"), "audio/1001.wav")
        self.assertNotEqual(storage.master_source_key("1001"),
                            storage.master_key("1001"))

    def test_an_unknown_codec_is_refused_by_name(self):
        with mock.patch.object(config, "AUDIO_DELIVERY_CODEC", "flac"):
            with self.assertRaises(ValueError) as cm:
                config.delivery_format()
        self.assertIn("flac", str(cm.exception))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to encode delivery audio")
class TestEncoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sr = config.SAMPLE_RATE
        cls.samples = _signal(cls.sr)
        cls.tmp = Path(tempfile.mkdtemp(prefix="delivery-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_codec_decodes_sample_aligned(self):
        """The whole point. A codec that shifted the audio by its priming
        delay would move every beat off the grid the analysis recorded."""
        for codec in sorted(config.DELIVERY_FORMATS):
            with self.subTest(codec=codec):
                ext = config.DELIVERY_FORMATS[codec][0]
                dst = self.tmp / f"aligned.{codec}.{ext}"
                audio_io.encode_delivery(self.samples, self.sr, dst, codec=codec)
                back = _decode_to_samples(dst, self.sr)
                offset, corr = _best_offset(self.samples, back)
                self.assertEqual(offset, 0,
                                 f"{codec} shifted the audio by {offset} samples")
                self.assertGreater(corr, 0.95, f"{codec} correlation {corr:.4f}")

    def test_length_is_preserved(self):
        """A codec pads to its frame size. If that padding reached the caller
        the variant would be longer than the duration_s stored beside it, and
        the mix would leave a gap at every transition."""
        for codec in sorted(config.DELIVERY_FORMATS):
            with self.subTest(codec=codec):
                ext = config.DELIVERY_FORMATS[codec][0]
                dst = self.tmp / f"len.{codec}.{ext}"
                audio_io.encode_delivery(self.samples, self.sr, dst, codec=codec)
                back = _decode_to_samples(dst, self.sr)
                drift = abs(len(back) - len(self.samples)) / self.sr
                self.assertLess(drift, 0.01,
                                f"{codec} changed the duration by {drift:.4f}s")

    def test_compression_actually_compresses(self):
        """Guards the reason for doing this at all. If a change ever made the
        encoded copy no smaller than the PCM, every cost here would be paid
        for nothing."""
        wav = self.tmp / "ref.wav"
        audio_io.save_wav(wav, self.samples, self.sr)
        dst = self.tmp / f"small.{config.delivery_ext()}"
        audio_io.encode_delivery(self.samples, self.sr, dst)
        ratio = wav.stat().st_size / dst.stat().st_size
        self.assertGreater(ratio, 2.0,
                           f"delivery encoding is only {ratio:.1f}x smaller")

    def test_wav_codec_is_a_passthrough(self):
        """The escape hatch has to produce something the stdlib can still read,
        because it exists for the case where a codec is under suspicion."""
        dst = self.tmp / "passthrough.wav"
        mime = audio_io.encode_delivery(self.samples, self.sr, dst, codec="wav")
        self.assertEqual(mime, "audio/wav")
        back, sr = audio_io.load_wav(dst)
        self.assertEqual(sr, self.sr)
        np.testing.assert_allclose(back, self.samples, atol=1e-4)

    def test_a_failed_encode_names_the_codec(self):
        with self.assertRaises(RuntimeError) as cm:
            audio_io.encode_delivery(self.samples, self.sr,
                                     self.tmp / "x.m4a", bitrate="nonsense")
        self.assertIn("m4a", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
