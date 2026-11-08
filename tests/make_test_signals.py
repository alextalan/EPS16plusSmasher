"""Generate the standard hardware-test WAVs for ClawdOut.

All files: 16-bit signed PCM, mono, 44.1 kHz header. Fully deterministic:
running this again produces byte-identical files (SHA-256 in MANIFEST.txt).

Design goals:
  - unique at every position (seeded noise), so Verify can detect offsets
  - full-scale and +/-1 marker samples at both ends (sign / top-nibble edges)
  - lengths that are not multiples of the 5000-sample transfer chunk
  - nonzero known first/last samples, so truncation or padding is visible

Run:  python3 tests/make_test_signals.py [output_dir]
"""
import hashlib
import math
import os
import struct
import sys
import wave

RATE = 44100
MARK_HEAD = [32767, -32768, 1, -1, 0x5A5A, -0x5A5A]
MARK_TAIL = [0x1234, -0x1234, -1, 1, -32768, 32767]


def lcg_noise(n, seed, amp):
    """Deterministic noise in [-amp, amp] (32-bit LCG, Numerical Recipes)."""
    x = seed & 0xFFFFFFFF
    out = []
    for _ in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        out.append(int(((x >> 16) / 65535.0 * 2 - 1) * amp))
    return out


def clip(v):
    return max(-32768, min(32767, int(round(v))))


def with_markers(body):
    body = list(body)
    body[:len(MARK_HEAD)] = MARK_HEAD
    body[-len(MARK_TAIL):] = MARK_TAIL
    return body


def tone_plus_noise(n, period, seed):
    """Audible tone at -9 dBFS plus -30 dBFS unique noise, then markers."""
    noise = lcg_noise(n, seed, 1000)
    body = [clip(11600 * math.sin(2 * math.pi * i / period) + noise[i])
            for i in range(n)]
    return with_markers(body)


def harmonic_cycle(period, n_harm, amp=16000):
    """One cosine-phase cycle with n_harm harmonics (saw-like, 1/k)."""
    norm = sum(1.0 / k for k in range(1, n_harm + 1))
    return [clip(amp * sum(math.cos(2 * math.pi * k * i / period) / k
                           for k in range(1, n_harm + 1)) / norm)
            for i in range(period)]


def loop_wave(shape, period=200, cycles=22, amp=14000):
    """Seamless loop: exact integer number of cycles, same pitch."""
    out = []
    for i in range(period * cycles):
        ph = (i % period) / period
        if shape == "sine":
            v = math.sin(2 * math.pi * ph)
        elif shape == "square":
            v = 1.0 if ph < 0.5 else -1.0
        elif shape == "saw":
            v = 2 * ph - 1
        out.append(clip(amp * v))
    return out


def write(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "test_signals"
    os.makedirs(out, exist_ok=True)
    files = {}

    # T1: core round trip. 12,345 samples -> chunks 5000/5000/2345.
    files["T1_core_12345.wav"] = tone_plus_noise(12345, 100, seed=1)

    # T2: Smasher source. 16 frames x 256 samples, each frame exactly one
    # cosine-phase cycle with 1..16 harmonics: mod wheel = brightness sweep.
    t2 = []
    for f in range(16):
        t2 += harmonic_cycle(256, f + 1)
    files["T2_transwave_src_16frames.wav"] = t2

    # T3: one-per-instrument batch. Distinct lengths and pitches.
    files["T3a_4321.wav"] = tone_plus_noise(4321, 200, seed=2)
    files["T3b_7777.wav"] = tone_plus_noise(7777, 150, seed=3)
    files["T3c_10007.wav"] = tone_plus_noise(10007, 100, seed=4)

    # T4: soundscape. Same pitch, seamless loops, very different timbres,
    # so an LFO crossfade is heard as a timbre morph.
    files["T4a_sine_loop.wav"] = loop_wave("sine")
    files["T4b_square_loop.wav"] = loop_wave("square")
    files["T4c_saw_loop.wav"] = loop_wave("saw")

    lines = ["ClawdOut hardware test signals (16-bit mono PCM, 44.1 kHz header)",
             ""]
    for name, s in files.items():
        digest = write(os.path.join(out, name), s)
        lines.append(f"{name:32s} {len(s):6d} samples  first {s[0]:6d}  "
                     f"last {s[-1]:6d}  sha256 {digest[:16]}")
    with open(os.path.join(out, "MANIFEST.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
