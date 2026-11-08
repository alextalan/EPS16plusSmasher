"""Protocol tests against a simulated EPS-16+.

The FakeEPS reproduces behavior observed in a real EPS-16+ session log:
  - 3-byte status replies (01 00 <code>)
  - a short header echo after the ACK on Get Params (7 bytes) and
    Get Data (15 bytes), sent before the host acknowledges
  - unprompted 0x11 parameter packets (group 0x30)
  - WAIT (01 00 01) before the real status
It is a model of observed traffic, not of the EPS firmware; passing here
means the protocol code handles those shapes, not that hardware behaves
identically in every case.

Run:  python3 tests/test_protocol.py
"""
import importlib.util
import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "EPS16plusSmasher", os.path.join(HERE, "..", "EPS16plusSmasher.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# speed up timing for tests
m.SEND_DELAY = 0.01
m.READ_TIMEOUT = 0.6
m.WAIT_TIMEOUT = 1.5
m.wire_seconds = lambda n: 0.0

ACK = [0x01, 0x00, 0x00]
STRAY = [0x11, 0, 0, 0, 0, 0, 1, 0x30, 0, 0, 0, 0, 1]   # seen in July log


class FakeEPS:
    def __init__(self, eps, occupied=()):
        self.eps = eps
        self.occupied = set(occupied)
        self.samples = {}           # (inst, layer, ws_bytes) -> list
        self.pending = None         # packet to send after host ACK
        self.await_payload = None   # (key, start) after 0x0F header
        self.params_set = []
        self.attempts = []          # instrument numbers tried for 0x15
        self.stray_before_ack = False
        self.wait_before_ack = False
        self.split_echo = False     # deliver echo in a separate, later packet

    # deliver a body to the host after a delay, on another thread
    def reply(self, body, delay=0.02):
        def go():
            time.sleep(delay)
            self.eps._on_midi_message(types.SimpleNamespace(
                type="sysex", data=tuple([0x0F, 0x03, 0x00] + list(body))))
        threading.Thread(target=go, daemon=True).start()

    def status(self, code=0x00, delay=0.02):
        if self.stray_before_ack:
            self.reply(STRAY, delay)
            delay += 0.02
        if self.wait_before_ack:
            self.reply([0x01, 0x00, 0x01], delay)
            delay += 0.2
        self.reply([0x01, 0x00, code], delay)

    def send(self, msg):     # acts as the out_port
        body = list(msg.data)[3:]
        if self.await_payload is not None and body[:3] != ACK:
            key, start = self.await_payload
            self.await_payload = None
            words = m.EPS16.convert_from_16_bit_midi(body)
            data = [m.EPS16.convert_to_signed_int(w) for w in words]
            buf = self.samples.setdefault(key, [])
            if len(buf) < start + len(data):
                buf.extend([0] * (start + len(data) - len(buf)))
            buf[start:start + len(data)] = data
            self.status()
            return
        if body == ACK:
            if self.pending is not None:
                pkt, self.pending = self.pending, None
                self.reply(pkt, 0.03)
            return
        cmd, inst, layer = body[0], body[2], body[4]
        ws = tuple(body[5:7])
        key = (inst, layer, ws)
        if cmd == 0x15:
            self.attempts.append(inst)
            if inst in self.occupied:
                self.status(0x0D)          # Instrument in Use
            else:
                self.occupied.add(inst)
                self.status()
        elif cmd in (0x16, 0x19, 0x1C):
            self.status()
        elif cmd == 0x1E:
            self.samples[key] = self.samples.get(key, [])[:1]
            self.status()
        elif cmd == 0x11:
            self.params_set.append((inst, body[7], body[8], body[9:]))
        elif cmd == 0x05:
            n = len(self.samples.get(key, []))
            words = [0] * 139
            v = n << 9
            words[119] = (v >> 16) & 0xFFFF
            words[121] = v & 0xFFFF
            self.status()
            self.reply([0x0E] + [0] * 6, 0.05)            # header echo
            self.pending = m.EPS16.convert_to_16_bit_midi(words)
        elif cmd == 0x06:
            start = self._unpack(body[7:11])
            end = self._unpack(body[11:15])
            data = self.samples.get(key, [])[start:end]
            self.status()
            echo = [0x0F] + [0] * 14
            self.reply(echo, 0.6 if self.split_echo else 0.05)
            self.pending = m.EPS16.convert_to_16_bit_midi(data)
        elif cmd == 0x0F:
            start = self._unpack(body[7:11])
            self.await_payload = (key, start)
            self.status()

    def close(self):
        pass

    @staticmethod
    def _unpack(groups):
        v = 0
        for g in groups:
            v = (v << 6) | g
        return v


def make(occupied=()):
    logs = []
    eps = m.EPS16(lambda s: logs.append(("info", s)),
                  lambda s: logs.append(("error", s)),
                  lambda s: logs.append(("ok", s)),
                  diag_cb=lambda s: logs.append(("diag", s)))
    fake = FakeEPS(eps, occupied)
    eps.out_port = fake
    eps.in_port = object()
    return eps, fake, logs


results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f"  [{detail}]" if detail and not cond else ""))


def errors(logs):
    return [s for k, s in logs if k == "error"]


# 1. simple command with stale + stray packets around the ACK
eps, fake, logs = make()
with eps._msg_lock:
    eps._messages.append(STRAY)                # stale, must be drained
fake.stray_before_ack = True
check("create_instrument survives stale and stray packets",
      eps.create_instrument() and not errors(logs), errors(logs))
check("stale packet was logged as discarded",
      any("Discarded stale" in s for k, s in logs if k == "diag"))

# 2. WAIT before ACK
eps, fake, logs = make()
fake.wait_before_ack = True
check("WAIT then ACK counts as success", eps.create_layer())

# 3. error status reported with its meaning
eps, fake, logs = make(occupied={0})
check("occupied slot fails cleanly", not eps.create_instrument())
check("error names the EPS reason",
      any("Instrument in Use" in s for s in errors(logs)), errors(logs))

# 4. full upload -> download round trip, with stray packets everywhere
eps, fake, logs = make()
src = [((i * 37) % 65536) - 32768 for i in range(12345)]
fake.stray_before_ack = True
ok = eps.upload_wav_to_eps(src)
got = eps.get_wavesample_data_chunked(5000, lambda d, f: None)
check("upload/download round trip with strays",
      ok and got == src, f"ok={ok} len={len(got)}")
check("verify report says EXACT",
      m.compare_waves(src, got)[-1] == "RESULT: EXACT ROUND TRIP")
check("param dump logged with end offset",
      any("end offset 12345" in s for k, s in logs if k == "diag"))

# 5. header echo arriving late, as a separate delivery
eps, fake, logs = make()
fake.split_echo = True
src = list(range(-3000, 3000))
eps.upload_wav_to_eps(src)
got = eps.get_wavesample_data_chunked(5000, lambda d, f: None)
check("late header echo is not mistaken for sample data", got == src,
      f"len={len(got)}")

# 6. instrument search: correct order, reaches slot 7
eps, fake, logs = make(occupied={0, 1})
slot = eps._create_in_free_slot()
check("search skips occupied slots without repeats",
      slot == 2 and fake.attempts == [0, 1, 2], fake.attempts)
eps, fake, logs = make(occupied=set(range(7)))
check("search can use slot 7 (eps.js never could)",
      eps._create_in_free_slot() == 7, fake.attempts)

# 7. multi-instrument batch: one attempt per file after the first search
eps, fake, logs = make(occupied={0})
waves = [[i] * 300 for i in range(3)]
placed = eps.upload_to_different_instruments(waves, lambda p: None)
check("batch lands in consecutive free slots",
      [s for _, s in placed] == [1, 2, 3], placed)
check("no wasted create attempts", fake.attempts == [0, 1, 2, 3], fake.attempts)

# 8. transwave with a stray packet after setup still succeeds
eps, fake, logs = make()
frames = [[v] * 256 for v in range(16)]
fake.stray_before_ack = True
slot = eps.upload_as_transwave(frames, lambda p: None)
fake.reply(STRAY, 0.05)
check("transwave returns its slot", slot == 0, errors(logs))
loop_end = [p for p in fake.params_set if p[1] == 0x20 and p[2] == 0x18]
check("transwave sets loop end to one frame last",
      loop_end and fake._unpack(loop_end[-1][3]) == 256, loop_end)

# 9. compare_waves report shapes
r = m.compare_waves([1, 2, 3, 4], [1, 2, 3, 4, 0, 0, 7])
check("compare: extra tail reported", "MATCH (" in r[-1] and any("extra 3" in x for x in r), r)
ref = [((i * 7919) % 2000) - 1000 for i in range(3000)]
r = m.compare_waves(ref, [0] * 5 + ref)
check("compare: offset detected", any("+5" in x for x in r), r)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
