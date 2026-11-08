#!/usr/bin/env python3
"""
EPS16plusSmasher -- native macOS sample transfer + transwave tool for the
Ensoniq EPS-16+
=========================================================================

Built on:
  - EnsoniqEPS16Plus by summitt (Null Lexicon)
      https://github.com/summitt/EnsoniqEPS16Plus
      Support: https://patreon.com/null0perat0r
    The MIDI/sysex protocol layer here is a port of its eps.js. Departures
    from eps.js are marked in comments (packet filtering, instrument-slot
    search fix, cable-time-scaled timeouts, download range clamp).
  - Transwave Atom Smasher by handsthatstrike (MIT license)
      https://github.com/handsthatstrike/Transwave-Atom-Smasher
    The Transwave Smasher section is a native Python port of its DSP.

Requirements:
    python.org Python 3.12 (includes Tkinter)
    pip3 install mido python-rtmidi

On the EPS-16+: Edit -> System-MIDI -> SYS-EX = ON
"""

import os
import queue
import struct
import sys
import threading
import time
import wave

# ---------------------------------------------------------------------------
# MIDI backend
# ---------------------------------------------------------------------------
MIDO_IMPORT_ERROR = None
try:
    import mido

    mido.set_backend("mido.backends.rtmidi")
except Exception as exc:  # ImportError or backend load failure
    mido = None
    MIDO_IMPORT_ERROR = exc

CHUNK_SIZE = 5000          # samples per sysex chunk (matches web version)
MAX_SAMPLES = 512900       # max samples per file (matches web version)
SEND_DELAY = 0.7           # post-send sleep, seconds (matches web version)
READ_TIMEOUT = 5.0         # readMessages timeout, seconds (matches web version)
READ_POLL = 0.1            # poll interval while waiting for messages
WAIT_TIMEOUT = 30.0        # how long to honor a WAIT response ("manual says
                           # wait up to 30 seconds" -- comment in eps.js)

RESPONSE_CODES = {
    0x00: "SUCCESS: ACK",
    0x01: "INFO: WAIT",
    0x02: "Error: Insert System Disk",
    0x03: "Error: Invalid Param Number",
    0x04: "Error: Invalid Param Value",
    0x05: "Error: Invalid Instrument",
    0x06: "Error: Invalid Layer",
    0x07: "Error: Layer In Use",
    0x08: "Error: Invalid Wavesample",
    0x09: "Error: Wavesample in Use",
    0x0A: "Error: Invalid Wavesample data range",
    0x0B: "Error: File Not Found",
    0x0C: "Error: Memory Full",
    0x0D: "Error: Instrument in Use",
    0x0E: "Error: No More Layers",
    0x0F: "Error: No More Samples",
    0x10: "Error: reserved",
    0x11: "Error: Wavesample is a copy",
    0x12: "Error: Zone Too Big",
    0x13: "Error: Sequencer Must Be Stopped",
    0x14: "Error: Disk Access in Progress",
    0x15: "Error: Disk Full",
    0x16: "Error: Loop is too long",
    0x17: "Error: NAK",
    0x18: "Error: No Layer To Edit",
    0x19: "Error: No More Pitch Tables",
    0x1A: "Error: Cross Fade length is zero",
    0x1B: "Error: Cross Fade Length is greater than 50%",
    0x1C: "Error: Loop Start is too close to sample start",
    0x1D: "Error: Loop End is too close to sample end",
    0x1E: "Error: Quiet Layer",
}

APP_VERSION = "1.0.0"

HEX_DUMP_LIMIT = 96        # bytes shown for large (payload) packets
FULL_DUMP_BELOW = 1024     # packets shorter than this are logged in full
MIDI_BAUD_BYTES_PER_SEC = 3125.0   # 31.25 kbaud / 10 bits per byte


def wire_seconds(nbytes):
    """Time a packet of nbytes occupies a 5-pin DIN MIDI cable."""
    return nbytes / MIDI_BAUD_BYTES_PER_SEC


def hexdump(data, limit=None):
    n = len(data)
    if limit is None:
        limit = n if n < FULL_DUMP_BELOW else HEX_DUMP_LIMIT
    shown = data[:limit]
    body = " ".join(f"{b:02X}" for b in shown)
    if n > limit:
        return f"{n:>5} bytes: {body} ... (+{n - limit} more)"
    return f"{n:>5} bytes: {body}"


class SessionLog:
    """Append-only session log file; safe to write from any thread."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._fh = None
        try:
            self._fh = open(path, "a", buffering=1, encoding="utf-8")
        except OSError:
            self.path = None

    def line(self, text):
        if not self._fh:
            return
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        ms = int(now * 1000) % 1000
        with self._lock:
            try:
                self._fh.write(f"[{stamp}.{ms:03d}] {text}\n")
            except OSError:
                pass

    def close(self):
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
            except OSError:
                pass
            self._fh = None


# ===========================================================================
# Protocol layer -- 1:1 port of the EPS16 class in eps.js
# ===========================================================================
class EPS16:
    """Ensoniq EPS16+ sysex protocol driver (CoreMIDI via mido/rtmidi)."""

    def __init__(self, log_cb, error_cb, success_cb, packet_log=None,
                 diag_cb=None):
        self.packet_log = packet_log or (lambda direction, raw, note="": None)
        self.diag = diag_cb or (lambda line: None)   # session-log-only detail
        self.inst_num = 0
        self.layer_num = 0
        self.ws_bytes = [0x00, 0x01]
        self.in_port = None
        self.out_port = None
        self.in_port_name = None
        self.out_port_name = None
        self._messages = []              # stripped sysex bodies, oldest first
        self._msg_lock = threading.Lock()
        self.log = log_cb                # info-level logging
        self.error = error_cb
        self.success = success_cb
        self.cancel_flag = threading.Event()

    # ---------------- port management ----------------
    @staticmethod
    def list_inputs():
        try:
            return sorted(set(mido.get_input_names())) if mido else []
        except Exception:
            return []

    @staticmethod
    def list_outputs():
        try:
            return sorted(set(mido.get_output_names())) if mido else []
        except Exception:
            return []

    def set_input(self, name):
        if self.in_port:
            try:
                self.in_port.close()
            except Exception:
                pass
            self.in_port = None
        if not name:
            return
        self.in_port = mido.open_input(name, callback=self._on_midi_message)
        self.in_port_name = name
        self.log(f"MIDI input: {name}")

    def set_output(self, name):
        if self.out_port:
            try:
                self.out_port.close()
            except Exception:
                pass
            self.out_port = None
        if not name:
            return
        self.out_port = mido.open_output(name)
        self.out_port_name = name
        self.log(f"MIDI output: {name}")

    def close(self):
        for p in (self.in_port, self.out_port):
            try:
                if p:
                    p.close()
            except Exception:
                pass

    def _on_midi_message(self, msg):
        if msg.type != "sysex":
            return
        body = self._strip_sysex_header(list(msg.data))
        raw = [0xF0] + list(msg.data) + [0xF7]
        if body and body[0] == 0x01 and len(body) == 3:
            note = self.get_response_message(body)
        else:
            note = f"data ({len(body)} body bytes)"
        self.packet_log("RX", raw, note)
        with self._msg_lock:
            self._messages.append(body)
        # Mirror of the web version's live status logging: status packets
        # begin with 0x01 in the first body byte.
        if body and body[0] == 0x01:
            text = self.get_response_message(body)
            if "Error" in text:
                self.error(text)

    @staticmethod
    def _strip_sysex_header(data):
        # mido's msg.data excludes F0/F7; strip the 3-byte Ensoniq header
        # (0x0F mfr, 0x03 product, 0x00 channel). Equivalent to the JS
        # slice(4, len-1) on the raw packet.
        return data[3:]

    # ---------------- low-level I/O ----------------
    def send_data(self, body, label=""):
        """Wrap `body` in the Ensoniq sysex envelope and transmit."""
        if not self.out_port:
            raise RuntimeError("No MIDI output selected")
        packet = [0x0F, 0x03, 0x00] + list(body)  # mido adds F0/F7
        self.packet_log("TX", [0xF0] + packet + [0xF7], label)
        self.out_port.send(mido.Message("sysex", data=packet))
        self._sleep(SEND_DELAY)

    def send_ack(self):
        self.send_data([0x01, 0x00, 0x00], "ACK")

    def _sleep(self, seconds):
        # interruptible sleep so Cancel is responsive
        end = time.time() + seconds
        while time.time() < end:
            if self.cancel_flag.is_set():
                return
            time.sleep(min(0.05, end - time.time()))

    # ---------------- message queue helpers ----------------
    # DEPARTURE FROM eps.js: the original read "whatever is queued" and
    # inspected only the first message, so any unsolicited packet (the EPS
    # was observed sending unprompted 0x11 parameter packets) or a header
    # echo arriving in a separate poll could be mistaken for the reply.
    # Here every command drains stale packets first, and each wait accepts
    # only the packet shape it expects; everything else is logged and
    # skipped.
    @staticmethod
    def is_status(msg):
        return len(msg) == 3 and msg[0] == 0x01

    def _pop(self, deadline):
        while True:
            with self._msg_lock:
                if self._messages:
                    return self._messages.pop(0)
            if time.time() >= deadline or self.cancel_flag.is_set():
                return None
            time.sleep(0.01)

    def _describe(self, m):
        if not m:
            return "empty packet"
        if self.is_status(m):
            return f"status 0x{m[2]:02X} ({self.get_response_message(m)})"
        return f"{len(m)}-byte body starting 0x{m[0]:02X}"

    def drain(self, why="before command"):
        with self._msg_lock:
            stale, self._messages = self._messages, []
        for m in stale:
            self.diag(f"Discarded stale packet ({why}): {self._describe(m)}")
        return stale

    def wait_status(self, timeout=None):
        """Next status code (0x00 = ACK), or None on timeout.
        WAIT (0x01) extends the deadline by WAIT_TIMEOUT."""
        deadline = time.time() + (READ_TIMEOUT if timeout is None else timeout)
        while True:
            m = self._pop(deadline)
            if m is None:
                return None
            if self.is_status(m):
                if m[2] == 0x01:
                    self.diag("EPS sent WAIT; extending deadline")
                    deadline = time.time() + WAIT_TIMEOUT
                    continue
                return m[2]
            self.diag(f"Skipped while awaiting status: {self._describe(m)}")

    def wait_data(self, accept, timeout=None, what="data"):
        """Next non-status packet satisfying accept(body), or None.
        An error status aborts the wait; extra ACKs are skipped."""
        deadline = time.time() + (READ_TIMEOUT if timeout is None else timeout)
        while True:
            m = self._pop(deadline)
            if m is None:
                return None
            if self.is_status(m):
                if m[2] == 0x01:
                    deadline = time.time() + WAIT_TIMEOUT
                    continue
                if m[2] != 0x00:
                    return None   # error text already logged by callback
                self.diag(f"Skipped extra ACK while awaiting {what}")
                continue
            if accept(m):
                return m
            self.diag(f"Skipped while awaiting {what}: {self._describe(m)}")

    def _status_text(self, code):
        if code is None:
            return "no response"
        return RESPONSE_CODES.get(code, f"unknown status 0x{code:02X}")

    def _simple_command(self, cmd, label, ok_msg, err_msg):
        self.drain()
        self.send_data(self.create_midi_message(cmd), label)
        code = self.wait_status()
        if code == 0x00:
            self.success(ok_msg)
            return True
        self.error(f"{err_msg} ({self._status_text(code)})")
        return False

    def create_midi_message(self, command, data=None):
        header = [command, 0x00, self.inst_num, 0x00, self.layer_num] + list(self.ws_bytes)
        return header + (list(data) if data else [])

    @staticmethod
    def get_response_message(message):
        if len(message) < 3:
            return "Unknown!"
        return RESPONSE_CODES.get(message[2], "Unknown!")

    # ---------------- number conversions (verified against eps.js) --------
    @staticmethod
    def convert_to_signed_int(value):
        return value - 65536 if value > 32767 else value

    @staticmethod
    def convert_to_12_bit_midi(values, min_size=2):
        """Pack values into big-endian 6-bit groups (JS convertTo12BitMidi)."""
        bin_string = "".join(format(v, "b").zfill(16) for v in values)
        groups = []
        while len(bin_string) >= 6:
            groups.append(int(bin_string[-6:], 2))
            bin_string = bin_string[:-6]
        if bin_string and int(bin_string, 2) != 0:
            groups.append(int(bin_string, 2))
        while len(groups) < min_size:
            groups.append(0)
        groups.reverse()
        return groups

    @staticmethod
    def convert_to_16_bit_midi(samples):
        """Each 16-bit sample -> three bytes split 4/6/6 (JS convertTo16BitMidi)."""
        out = []
        for s in samples:
            v = s & 0xFFFF
            out.append((v & 0xF000) >> 12)
            out.append((v >> 6) & 0x3F)
            out.append(v & 0x3F)
        return out

    @staticmethod
    def convert_from_16_bit_midi(data):
        """Inverse of the above; out-of-range reads count as 0 (JS-safe)."""
        words = []
        for i in range(0, len(data), 3):
            b0 = data[i] if i < len(data) else 0
            b1 = data[i + 1] if i + 1 < len(data) else 0
            b2 = data[i + 2] if i + 2 < len(data) else 0
            words.append(((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F))
        return words

    @staticmethod
    def get_end_offset(params16):
        w1 = params16[119] << 16
        w2 = params16[120] << 8
        w3 = params16[121]
        w4 = params16[122] >> 8
        return (w1 | w2 | w3 | w4) >> 9

    # ---------------- selection ----------------
    def set_instrument_number(self, n):
        self.inst_num = n

    def set_layer_number(self, n):
        self.layer_num = n

    def set_wavesample_number(self, n):
        self.ws_bytes = self.convert_to_12_bit_midi([n], 2)
        return True

    # ---------------- EPS sysex commands ----------------
    def get_wavesample_params(self):
        self.drain()
        self.send_data(self.create_midi_message(0x05), "CMD 0x05 Get WS Params")
        code = self.wait_status()
        if code != 0x00:
            self.error("Error: Unable to get WaveSample Parameters "
                       f"({self._status_text(code)})")
            return []
        self.send_ack()
        # The EPS sends a short header echo, then the parameter block
        # (observed: 417 body bytes = 139 words). We need >= 123 words.
        m = self.wait_data(lambda b: len(b) >= 3 * 123, what="WS parameters")
        if m is None:
            self.error("Error: Unable to get WaveSample Parameters (no data)")
            return []
        words = self.convert_from_16_bit_midi(m)
        self._log_params(words, len(m))
        return words

    def _log_params(self, words, nbytes):
        self.diag(f"WS params: {len(words)} words from {nbytes} body bytes "
                  f"(inst {self.inst_num}, layer {self.layer_num}, "
                  f"ws bytes {self.ws_bytes})")
        for row in range(0, len(words), 16):
            chunk = " ".join(f"{w:04X}" for w in words[row:row + 16])
            self.diag(f"  w[{row:3d}] {chunk}")
        if len(words) >= 123:
            self.diag("  words 119..122 = "
                      + " ".join(f"{words[i]:04X}" for i in range(119, 123))
                      + f" -> end offset {self.get_end_offset(words)} "
                      "(decode ported from eps.js; hardware-unverified)")

    def create_instrument(self):
        return self._simple_command(0x15, "CMD 0x15 Create Instrument",
                                    "Success: Created instrument",
                                    "Error: Unable to create instrument")

    def create_layer(self):
        return self._simple_command(0x16, "CMD 0x16 Create Layer",
                                    "Success: Created layer",
                                    "Error: Unable to create layer")

    def create_sqr_wave(self):
        return self._simple_command(0x19, "CMD 0x19 Create SQR Wavesample",
                                    "Success: Created SQR wavesample",
                                    "Error: Unable to create SQR wavesample")

    def delete_instrument(self):
        return self._simple_command(0x1C, "CMD 0x1C Delete Instrument",
                                    "Success: Deleted instrument",
                                    "Error: Unable to delete instrument")

    def truncate_wavesample(self):
        return self._simple_command(0x1E, "CMD 0x1E Truncate Wavesample",
                                    "Success: Truncated wavesample",
                                    "Error: Unable to truncate wavesample")

    def set_parameter(self, param_group, param_byte, param_value):
        midi_value = self.convert_to_12_bit_midi([param_value], 4)
        msg = [param_group, param_byte] + midi_value
        self.send_data(self.create_midi_message(0x11, msg),
                       f"CMD 0x11 Set Param grp=0x{param_group:02X} "
                       f"byte=0x{param_byte:02X} value={param_value}")

    # ---------------- transfer: EPS -> Mac ----------------
    def get_wavesample_data(self, start, end):
        expected = 3 * (end - start)
        offsets = self.convert_to_12_bit_midi([start], 4) + self.convert_to_12_bit_midi([end], 4)
        self.drain()
        self.send_data(self.create_midi_message(0x06, offsets),
                       f"CMD 0x06 Get WS Data [{start}:{end}]")
        code = self.wait_status()
        if code != 0x00:
            self.error("Error: Unable to get wavesample data from EPS "
                       f"({self._status_text(code)})")
            return []
        self._sleep(1.0)   # eps.js pauses here before acknowledging
        self.send_ack()
        # Observed: a 15-byte header echo precedes the data packet, whose
        # body is exactly 3 bytes per sample. Accept the exact length; as a
        # fallback accept any long multiple-of-3 packet and flag it.
        m = self.wait_data(
            lambda b: len(b) == expected or (len(b) >= 30 and len(b) % 3 == 0),
            timeout=READ_TIMEOUT + wire_seconds(expected + 5),
            what=f"sample data [{start}:{end}]")
        if m is None:
            self.error("Error: Unable to get wavesample data from EPS (no data)")
            return []
        if len(m) != expected:
            self.diag(f"WARNING: data packet is {len(m)} bytes, expected "
                      f"{expected} for [{start}:{end}]")
        words = self.convert_from_16_bit_midi(m)
        data = [self.convert_to_signed_int(w) for w in words]
        self.success("Success: Getting wavesample data from EPS")
        return data

    def get_wavesample_data_chunked(self, chunk_size, plot_callback):
        wavedata = []
        params = self.get_wavesample_params()
        if not params:
            return []
        offset = self.get_end_offset(params)
        self.log(f"Wavesample end offset: {offset} samples")
        # Clamp each request to `offset` so we never ask the EPS for a range
        # that extends past the wavesample end (which returns "Invalid
        # Wavesample data range"). The JS version relied on a post-hoc
        # recovery request; clamping up front avoids the spurious error.
        start = 0
        while start < offset and not self.cancel_flag.is_set():
            end = min(start + chunk_size, offset)
            part = self.get_wavesample_data(start, end)
            if not part:
                return wavedata
            wavedata += part
            plot_callback(wavedata, (len(wavedata) / offset) if offset else 1.0)
            start = end
        return wavedata

    # ---------------- transfer: Mac -> EPS ----------------
    def put_wavesample_data(self, audio, start=0):
        midi_data = self.convert_to_16_bit_midi(audio)
        offsets = (self.convert_to_12_bit_midi([start], 4)
                   + self.convert_to_12_bit_midi([len(audio) + start], 4))
        self.drain()
        self.send_data(self.create_midi_message(0x0F, offsets),
                       f"CMD 0x0F Put WS Data [{start}:{len(audio) + start}]")
        code = self.wait_status()
        if code != 0x00:
            self.error("Error: Unable to initiate pushing a wavesample to the "
                       f"EPS ({self._status_text(code)})")
            return False
        self.send_data(midi_data, f"WS data payload ({len(audio)} samples)")
        # Timeout scales with cable time: a 15 KB payload alone takes ~4.8 s
        # on DIN MIDI, which left the original fixed 5 s timeout ~0.5 s of
        # margin in the July hardware log.
        code = self.wait_status(timeout=READ_TIMEOUT + wire_seconds(len(midi_data) + 5))
        if code == 0x00:
            self.success("Success: Wavesample data sent")
            return True
        if code is None:
            # eps.js treats silence after the payload as success; keep that,
            # but make it visible.
            self.diag("WARNING: no ACK after payload; continuing (eps.js behavior)")
            self.success("Success: Wavesample data sent (unacknowledged)")
            return True
        self.error(f"Error: EPS rejected wavesample data ({self._status_text(code)})")
        return False

    def put_wavesample_data_in_chunks(self, audio, chunk_size,
                                      num_waves=1, wave_index=0,
                                      progress_callback=lambda p: None):
        chunks = [audio[i:i + chunk_size] for i in range(0, len(audio), chunk_size)]
        start = 0
        for chunk in chunks:
            if self.cancel_flag.is_set():
                return False
            if not self.put_wavesample_data(chunk, start):
                # one retry, matching the web version
                if not self.put_wavesample_data(chunk, start):
                    self.error("Error: Unable to upload a portion of the wavesample")
                    return False
                self._sleep(2.0)
            pct = round((wave_index + (start + len(chunk)) / len(audio)) / num_waves * 100)
            progress_callback(pct)
            start += chunk_size
        self.send_ack()
        code = self.wait_status()   # eps.js waits here, ignoring the result
        self.diag(f"End-of-upload handshake reply: {self._status_text(code)}")
        self.send_ack()
        self._sleep(1.0)
        self.set_parameter(0x20, 0x18, len(audio))
        return True

    def upload_wav_to_eps(self, audio, num_waves=1, wave_index=0,
                          progress_callback=lambda p: None):
        self.set_parameter(0x20, 0x00, 2)   # loop mode: forward
        self.set_parameter(0x20, 0x19, 0)   # loop position
        self.set_parameter(0x20, 0x17, 0)   # loop start
        self.set_parameter(0x20, 0x15, 0)   # sample start
        self.set_parameter(0x20, 0x16, 1)   # sample end
        if self.truncate_wavesample() and self.put_wavesample_data_in_chunks(
                audio, CHUNK_SIZE, num_waves, wave_index, progress_callback):
            return True
        return False

    # ---------------- crossfade helper (morphing soundscape) --------------
    def get_cross_fade_break_points(self, length, step):
        section = 128 // ((length - 1) * 2)
        half = section // 2
        bp = {"pointA": 0, "pointB": 0, "pointC": 127, "pointD": 127}
        if step == 0:
            bp["pointC"] = half
            bp["pointD"] = half + section
        elif step == length - 1:
            prev = self.get_cross_fade_break_points(length, step - 1)
            bp["pointA"] = prev["pointC"]
            bp["pointB"] = prev["pointD"]
        else:
            prev = self.get_cross_fade_break_points(length, step - 1)
            bp["pointA"] = prev["pointC"]
            bp["pointB"] = prev["pointD"]
            bp["pointC"] = bp["pointB"] + section
            bp["pointD"] = bp["pointC"] + section
        return bp

    # ---------------- macros ----------------
    def _create_in_free_slot(self, with_layer_and_wave=True):
        """Create an instrument in the first slot that accepts it, searching
        from the current instrument number up to 7. Returns the slot index
        or None.

        FIX vs eps.js: the original set the instrument number to the loop
        index only AFTER a failed attempt, so it tried the starting slot
        twice and never tried slot 7.
        """
        for i in range(self.inst_num, 8):
            if self.cancel_flag.is_set():
                return None
            self.set_instrument_number(i)
            self.set_layer_number(0)
            self.set_wavesample_number(1)
            if not self.create_instrument():
                continue
            if not with_layer_and_wave or (self.create_layer()
                                           and self.create_sqr_wave()):
                self.log(f"Using instrument index {i} (0-based)")
                return i
            self.error(f"Instrument index {i} created but layer/wavesample "
                       "setup failed; trying next slot")
        return None

    def upload_to_different_instruments(self, wavetables, progress_callback):
        """Returns list of (file_index, slot) actually written."""
        placed = []
        for index, wave_data in enumerate(wavetables):
            if self.cancel_flag.is_set():
                break
            slot = self._create_in_free_slot()
            if slot is None:
                self.error(f"Error: No free instrument slot for file "
                           f"{index + 1}; stopping")
                break
            ok = self.upload_wav_to_eps(wave_data, len(wavetables), index,
                                        progress_callback)
            if ok:
                self.success(f"Success: File {index + 1} -> instrument index {slot}")
                placed.append((index, slot))
            else:
                self.error(f"Error: Upload of file {index + 1} to instrument "
                           f"index {slot} failed")
            self._sleep(0.5)
            self.set_instrument_number(slot + 1)   # next search starts after
        self.success(f"Complete: {len(placed)}/{len(wavetables)} samples uploaded")
        return placed

    def upload_as_transwave(self, wavetables, progress_callback):
        """Returns the instrument slot used, or None."""
        slot = self._create_in_free_slot()
        if slot is None:
            self.error("Error: Could not create an instrument for the transwave")
            return None
        transwave = []
        for w in wavetables:
            transwave += w
        if not self.upload_wav_to_eps(transwave, 1, 0, progress_callback):
            self.error("Error: Unable to upload transwave to EPS16")
            return None
        self._sleep(1.0)
        self.set_parameter(0x20, 0x18, len(wavetables[0]))   # loop end = one table
        self.set_parameter(0x20, 0x06, 0x07)                 # modulation: transwave
        self.set_parameter(0x20, 0x07, 0x0A)                 # mod source: wheel
        self.set_parameter(0x20, 0x08, len(wavetables) + 1)  # mod amount
        # eps.js treated ANY trailing message as failure; only an error
        # status counts here (unsolicited packets are skipped and logged).
        code = self.wait_status()
        if code is None or code == 0x00:
            self.success(f"Complete: Uploaded transwave ({len(wavetables)} "
                         f"frames) to instrument index {slot}")
            return slot
        self.error("Error: EPS reported a problem after transwave setup "
                   f"({self._status_text(code)})")
        return None

    def create_morphing_wave_table(self, wavetables, progress_callback):
        """Returns the instrument slot used, or None."""
        slot = self._create_in_free_slot(with_layer_and_wave=False)
        if slot is None:
            self.error("Error: Unable to create instrument for morphing waveforms")
            return None
        self.set_layer_number(0)
        self.set_wavesample_number(1)
        self.set_parameter(0x28, 0x00, 0xFF)  # enable all patches
        for i, wave_data in enumerate(wavetables):
            if i == 8:
                break
            if self.cancel_flag.is_set():
                return None
            bp = self.get_cross_fade_break_points(len(wavetables), i)
            self.set_layer_number(i)
            if not (self.create_layer() and self.create_sqr_wave()
                    and self.set_wavesample_number(i + 1)
                    and self.upload_wav_to_eps(wave_data, len(wavetables), i,
                                               progress_callback)):
                self.error("Error: Unable to update instrument parameters")
                return None
            self.diag(f"Soundscape layer {i}: crossfade points {bp}")
            self._sleep(0.5)
            self.set_parameter(0x18, 0x05, 1)             # crossfade: linear
            self.set_parameter(0x18, 0x03, bp["pointA"])
            self.set_parameter(0x18, 0x0B, bp["pointB"])
            self.set_parameter(0x18, 0x04, bp["pointC"])
            self.set_parameter(0x18, 0x0C, bp["pointD"])
            self.set_parameter(0x18, 0x07, 0)             # mod source: LFO
            self.set_parameter(0x18, 0x0A, 127)           # mod amount
            self.set_parameter(0x1C, 0x02, 15)            # LFO speed
            self.set_parameter(0x1C, 0x03, 127)           # LFO depth
            self.set_parameter(0x1C, 0x04, 0)             # LFO delay
            self.set_parameter(0x1C, 0x05, 1)             # LFO reset
            self.set_parameter(0x1C, 0x08, 0x0F)          # LFO mod source
            self.set_parameter(0x1C, 0x07, 0x0F)          # LFO mod source
            self.set_wavesample_number(1)
            self._sleep(1.0)
        self.success(f"Complete: Soundscape in instrument index {slot}")
        return slot


# ===========================================================================
# WAV helpers
# ===========================================================================
def load_wav(path):
    """Return (samples, framerate). Requires 16-bit mono PCM like the web app."""
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError("Only 16-bit WAV files are supported")
        if wf.getnchannels() != 1:
            raise ValueError("Only mono WAV files are supported")
        n = wf.getnframes()
        if n > MAX_SAMPLES:
            raise ValueError(
                f"File too big: {n} samples (max {MAX_SAMPLES})")
        raw = wf.readframes(n)
        samples = list(struct.unpack(f"<{n}h", raw[: n * 2]))
        return samples, wf.getframerate()


def save_wav(path, samples, sample_rate=44100):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        clipped = [max(-32768, min(32767, int(s))) for s in samples]
        wf.writeframes(struct.pack(f"<{len(clipped)}h", *clipped))


# ===========================================================================
# Transwave Atom Smasher -- native port
# ===========================================================================
# Based on "Transwave Atom Smasher" v0.2 by handsthatstrike (MIT license):
#   https://github.com/handsthatstrike/Transwave-Atom-Smasher
# The original is a Perl + SoX pipeline. This is a dependency-free Python
# reimplementation of its DSP:
#   1. conform to 16-bit mono, trim leading silence
#   2. slice into 256-sample frames, max 128 (trailing partial discarded)
#   3. parabolic 10-sample fade in/out per frame (zero-crossing safety)
#   4. concatenate
# The fade curve and rounding are byte-exact matches of SoX's
# `fade p 10s 256s 10s` (verified empirically against SoX 14.4.2).
# The silence trim approximates SoX's `silence 1 1 0.20%` -- same threshold,
# different detector -- so the auto-detected start offset may differ from
# the original pipeline by a small amount; it is user-adjustable in the UI.

SMASH_FRAME_LEN = 256
SMASH_MAX_FRAMES = 128
SMASH_FADE_LEN = 10
SMASH_TRIM_THRESHOLD = 0.002    # 0.20% of full scale
SMASH_TRIM_WINDOW = 32          # RMS window (samples) for trim detection


def _round_half_away(x):
    """Round half toward +infinity, matching SoX's net 16-bit conversion
    (gain applied in the 32-bit sample domain, then add-half-and-shift
    back to 16-bit with dithering off). Verified against SoX 14.4.2."""
    import math
    return math.floor(x + 0.5)


def load_wav_flexible(path):
    """Load any PCM WAV (8/16/24/32-bit, mono or stereo) as 16-bit mono.

    Mirrors the original tool's `sox channels 1 -b 16` conform step:
    stereo is averaged to mono; other bit depths are rescaled to 16-bit.
    No sample-rate conversion is performed (the original didn't either).
    Returns (samples, framerate).
    """
    with wave.open(path, "rb") as wf:
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        n = wf.getnframes()
        rate = wf.getframerate()
        raw = wf.readframes(n)
    if ch not in (1, 2):
        raise ValueError(f"Unsupported channel count: {ch}")
    if width == 1:      # unsigned 8-bit
        vals = [(b - 128) << 8 for b in raw]
    elif width == 2:
        vals = list(struct.unpack(f"<{len(raw)//2}h", raw))
    elif width == 3:    # signed 24-bit little-endian
        vals = []
        for i in range(0, len(raw) - 2, 3):
            v = raw[i] | (raw[i+1] << 8) | (raw[i+2] << 16)
            if v >= 1 << 23:
                v -= 1 << 24
            vals.append(v >> 8)
    elif width == 4:
        v32 = struct.unpack(f"<{len(raw)//4}i", raw)
        vals = [v >> 16 for v in v32]
    else:
        raise ValueError(f"Unsupported sample width: {width * 8}-bit")
    if ch == 2:
        vals = [_round_half_away((vals[i] + vals[i+1]) / 2)
                for i in range(0, len(vals) - 1, 2)]
    return vals, rate


def smash_trim_offset(samples,
                      threshold=SMASH_TRIM_THRESHOLD,
                      window=SMASH_TRIM_WINDOW):
    """First index where the moving RMS reaches the threshold, else 0."""
    if len(samples) < window:
        return 0
    csum = [0.0] * (len(samples) + 1)
    for i, v in enumerate(samples):
        f = v / 32768.0
        csum[i + 1] = csum[i] + f * f
    for i in range(len(samples) - window):
        rms_sq = (csum[i + window] - csum[i]) / window
        if rms_sq >= threshold * threshold:
            return i
    return 0


def smash_fade_frame(frame, fade_len=SMASH_FADE_LEN):
    """Apply SoX `fade p <L>s <N>s <L>s` to one frame, byte-exact.

    Fade-in:  gain(i) = 1 - (1 - i/L)^2          for i in [0, L)
    Fade-out: gain    = 1 - (1 - r/L)^2          where r = samples to end
    (so the first sample is 0 and the last sample is gain(1), matching SoX)
    Rounding: half away from zero.
    """
    n = len(frame)
    out = list(frame)
    for i in range(min(fade_len, n)):
        t = i / fade_len
        out[i] = _round_half_away(out[i] * (1 - (1 - t) ** 2))
    for i in range(n):
        r = n - i
        if r < fade_len:
            g = 1 - (1 - r / fade_len) ** 2
            out[i] = _round_half_away(frame[i] * g) if i >= fade_len else \
                _round_half_away(out[i] * g)
    return out


def smash_prep(samples, mode):
    """Optional half-length preprocessing for sources over the frame cap.

    'raw'      -- no change
    'bright'   -- decimate by 2 (no filter; keeps/aliases top end).
                  Approximates the character of the SoX `speed 2.0` prep.
    'filtered' -- windowed-sinc lowpass at half Nyquist, then decimate by 2.
                  Approximates the character of the SoX `rate 22050` prep.
    Both halve the sample count; playback pitch doubles, as with the
    original preps. These are NOT byte-identical to SoX's resampler.
    """
    if mode == "raw":
        return samples
    if mode == "bright":
        return samples[::2]
    if mode == "filtered":
        # 63-tap Blackman-windowed sinc, cutoff 0.25 * sample rate
        import math
        taps = 63
        m = taps - 1
        fc = 0.25
        kernel = []
        for k in range(taps):
            x = k - m / 2
            sinc = 2 * fc if x == 0 else math.sin(2 * math.pi * fc * x) / (math.pi * x)
            w = 0.42 - 0.5 * math.cos(2 * math.pi * k / m) \
                + 0.08 * math.cos(4 * math.pi * k / m)
            kernel.append(sinc * w)
        ksum = sum(kernel)
        kernel = [k / ksum for k in kernel]
        half = m // 2
        n = len(samples)
        out = []
        for i in range(0, n, 2):           # decimate while filtering
            acc = 0.0
            for k, kv in enumerate(kernel):
                j = i + k - half
                if 0 <= j < n:
                    acc += samples[j] * kv
            out.append(max(-32768, min(32767, _round_half_away(acc))))
        return out
    raise ValueError(f"Unknown prep mode: {mode}")


def smash(samples, prep="raw", start_offset=None):
    """Full Atom Smasher pipeline. Returns (frames, info).

    frames: list of 256-sample lists, faded, ready for upload_as_transwave.
    info:   dict with trim offset, frame count, truncation flag.
    """
    prepped = smash_prep(samples, prep)
    offset = smash_trim_offset(prepped) if start_offset is None else start_offset
    offset = max(0, min(offset, len(prepped)))
    trimmed = prepped[offset:]
    total_frames = len(trimmed) // SMASH_FRAME_LEN
    truncated = total_frames > SMASH_MAX_FRAMES
    n_frames = min(total_frames, SMASH_MAX_FRAMES)
    frames = []
    for i in range(n_frames):
        chunk = trimmed[i * SMASH_FRAME_LEN:(i + 1) * SMASH_FRAME_LEN]
        frames.append(smash_fade_frame(chunk))
    return frames, {
        "trim_offset": offset,
        "frames": n_frames,
        "frames_before_cap": total_frames,
        "truncated": truncated,
        "prep": prep,
    }


# ===========================================================================
# GUI
# ===========================================================================
def compare_waves(ref, got, max_lag=256, window=2048):
    """Compare a reference (what was uploaded) to what came back from the
    EPS. Returns a list of human-readable report lines."""
    lines = []
    n = min(len(ref), len(got))
    lines.append(f"reference {len(ref)} samples, downloaded {len(got)} samples")
    if n == 0:
        lines.append("RESULT: nothing to compare")
        return lines
    mism = [i for i in range(n) if ref[i] != got[i]]
    if not mism:
        lines.append(f"first {n} samples: EXACT MATCH")
    else:
        i0 = mism[0]
        lines.append(f"first {n} samples: {n - len(mism)} match, {len(mism)} "
                     f"differ; first difference at {i0} "
                     f"(ref {ref[i0]}, got {got[i0]})")
        # look for a constant offset between the two
        w = min(window, n)
        best_lag, best = 0, -1
        for lag in range(-max_lag, max_lag + 1):
            lo, hi = max(0, -lag), min(w, len(got) - lag)
            if hi <= lo:
                continue
            score = sum(1 for i in range(lo, hi) if ref[i] == got[i + lag])
            if score > best:
                best, best_lag = score, lag
        if best_lag != 0:
            lines.append(f"best alignment: downloaded data offset by "
                         f"{best_lag:+d} samples ({best}/{w} match in first {w})")
    if len(got) > len(ref):
        tail = got[len(ref):]
        zeros = sum(1 for v in tail if v == 0)
        lines.append(f"extra {len(tail)} samples past reference end: "
                     f"{zeros} zero, peak |{max(abs(v) for v in tail)}|, "
                     f"{len(set(tail))} distinct values; first 8: {tail[:8]}")
    elif len(got) < len(ref):
        lines.append(f"downloaded data is {len(ref) - len(got)} samples SHORT")
    exact = not mism and len(got) == len(ref)
    lines.append("RESULT: " + ("EXACT ROUND TRIP" if exact else
                 "MATCH (with extra/short length -- see above)" if not mism
                 else "MISMATCH"))
    return lines


import tkinter as tk
from tkinter import ttk, filedialog, messagebox


class App:
    BG = "#1e1e1e"
    FG = "#e0e0e0"
    ACCENT = "#4f9dde"
    WAVE_COLOR = "#59c1a0"

    def __init__(self, root):
        self.root = root
        root.title(f"EPS16plusSmasher {APP_VERSION}")
        root.minsize(760, 640)

        self.ui_queue = queue.Queue()
        self.worker = None
        self.verbose_midi = True  # plain bool: safe to read from MIDI threads

        # All MIDI object construction/enumeration runs on this dedicated
        # thread, NEVER on the Tk main thread. rtmidi's CoreMIDI backend
        # spins the current thread's run loop when creating a MidiIn/MidiOut
        # (MidiInCore::getPortCount -> CFRunLoopRunSpecific); on the main
        # thread that reentrantly services pending Tk draw events, which hit
        # Python scroll callbacks while the GIL is released -> Py_FatalError
        # -> abort (confirmed by crash report on macOS 12 / Tk 8.6). On a
        # background thread the spin runs that thread's empty run loop and
        # is harmless.
        self._midi_jobs = queue.Queue()
        self._midi_thread = threading.Thread(target=self._midi_service,
                                             daemon=True)
        self._midi_thread.start()

        # session log file next to the script (fallback: home dir)
        if getattr(sys, "frozen", False):
            # Built .app: never write inside the bundle. ~/Library/Logs is the
            # standard place and needs no folder-access permission prompt.
            log_dir = os.path.expanduser("~/Library/Logs/EPS16plusSmasher")
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError:
                log_dir = os.path.expanduser("~")
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
            if not os.access(log_dir, os.W_OK):
                log_dir = os.path.expanduser("~")
        log_name = time.strftime("eps16plussmasher_log_%Y%m%d_%H%M%S.txt")
        self.session_log = SessionLog(os.path.join(log_dir, log_name))
        self._write_session_header()

        self.eps = EPS16(self._log_threadsafe,
                         lambda m: self._log_threadsafe(m, "error"),
                         lambda m: self._log_threadsafe(m, "success"),
                         packet_log=self._packet_log,
                         diag_cb=lambda line: self.session_log.line("DIAG " + line))

        self.local_wave = []          # samples loaded from disk (single upload)
        self.eps_wave = []            # samples fetched from the EPS
        self.batch_files = []         # paths for multi-upload
        self.verify_ref = []          # reference samples for Verify
        self.eps_wave_source = None   # ((inst, layer, ws), start_time) of last download
        self.slot_written = {}        # (inst, layer, ws) -> time of last upload
        self.verify_ref_name = ""
        self.smash_source = []        # raw samples of loaded smasher source
        self.smash_source_name = ""
        self.smash_frames = []        # last smashed frames
        self.displayed_wave = []

        self._build_ui()
        self._refresh_ports()
        self.root.after(50, self._drain_ui_queue)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        if mido is None:
            self.log(f"MIDI backend unavailable: {MIDO_IMPORT_ERROR}", "error")
            self.log("Install dependencies:  pip3 install mido python-rtmidi", "error")
        self.log("On the EPS16+: Edit -> System-MIDI -> SYSEX-MIDI = ON")
        if self.session_log.path:
            self.log(f"Session log: {self.session_log.path}")
        else:
            self.log("Warning: could not open a session log file", "error")

    def _write_session_header(self):
        import platform
        self.session_log.line("=== EPS16plusSmasher session ===")
        self.session_log.line(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.session_log.line(f"App version: {APP_VERSION}")
        self.session_log.line(f"Python: {sys.version.split()[0]}  "
                              f"Platform: {platform.platform()}")
        for pkg in ("mido", "python-rtmidi"):
            try:
                from importlib.metadata import version
                self.session_log.line(f"{pkg}: {version(pkg)}")
            except Exception:
                self.session_log.line(f"{pkg}: version unknown")

    def _packet_log(self, direction, raw_bytes, note=""):
        """Called from MIDI callback / worker threads for every packet."""
        if not self.verbose_midi:
            return
        suffix = f"   [{note}]" if note else ""
        self.session_log.line(f"{direction} {hexdump(raw_bytes)}{suffix}")

    # ---------------- layout ----------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # --- MIDI ports ---
        ports = ttk.LabelFrame(main, text="MIDI Ports", padding=6)
        ports.pack(fill="x", **pad)
        ttk.Label(ports, text="Input:").grid(row=0, column=0, sticky="e")
        self.in_var = tk.StringVar()
        self.in_combo = ttk.Combobox(ports, textvariable=self.in_var,
                                     state="readonly", width=32)
        self.in_combo.grid(row=0, column=1, sticky="we", padx=4)
        self.in_combo.bind("<<ComboboxSelected>>", self._on_in_selected)
        ttk.Label(ports, text="Output:").grid(row=0, column=2, sticky="e")
        self.out_var = tk.StringVar()
        self.out_combo = ttk.Combobox(ports, textvariable=self.out_var,
                                      state="readonly", width=32)
        self.out_combo.grid(row=0, column=3, sticky="we", padx=4)
        self.out_combo.bind("<<ComboboxSelected>>", self._on_out_selected)
        ttk.Button(ports, text="Refresh", command=self._refresh_ports)\
            .grid(row=0, column=4, padx=4)
        ports.columnconfigure(1, weight=1)
        ports.columnconfigure(3, weight=1)

        # --- selection ---
        sel = ttk.LabelFrame(main, text="Instrument / Layer / Wavesample", padding=6)
        sel.pack(fill="x", **pad)
        self.inst_var = tk.IntVar(value=0)
        self.layer_var = tk.IntVar(value=0)
        self.ws_var = tk.IntVar(value=1)
        for col, (label, var, lo, hi) in enumerate(
                [("Instrument", self.inst_var, 0, 7),
                 ("Layer", self.layer_var, 0, 7),
                 ("Wavesample", self.ws_var, 1, 127)]):
            ttk.Label(sel, text=label + ":").grid(row=0, column=col * 2, sticky="e", padx=(8, 2))
            tk.Spinbox(sel, from_=lo, to=hi, textvariable=var, width=5,
                       command=self._apply_selection).grid(row=0, column=col * 2 + 1)
        ttk.Label(sel, text="(numbers are 0-based; wavesample defaults to 1)")\
            .grid(row=0, column=6, padx=10)

        # --- EPS functions ---
        funcs = ttk.LabelFrame(main, text="EPS Functions", padding=6)
        funcs.pack(fill="x", **pad)
        for i, (label, job) in enumerate([
                ("Create Instrument", lambda: self.eps.create_instrument()),
                ("Create Layer", lambda: self.eps.create_layer()),
                ("Create Wavesample", lambda: self.eps.create_sqr_wave()),
                ("Delete Instrument", lambda: self.eps.delete_instrument())]):
            ttk.Button(funcs, text=label,
                       command=lambda j=job: self._run_job(j)).grid(row=0, column=i, padx=4)

        # --- single transfer ---
        xfer = ttk.LabelFrame(main, text="Single Wavesample Transfer", padding=6)
        xfer.pack(fill="x", **pad)
        ttk.Button(xfer, text="Get Wavesample from EPS",
                   command=self._get_wavesample).grid(row=0, column=0, padx=4)
        ttk.Button(xfer, text="Save WAV...",
                   command=self._save_wav).grid(row=0, column=1, padx=4)
        ttk.Separator(xfer, orient="vertical").grid(row=0, column=2, sticky="ns", padx=8)
        ttk.Button(xfer, text="Load WAV...",
                   command=self._load_wav).grid(row=0, column=3, padx=4)
        ttk.Button(xfer, text="Upload to EPS16+",
                   command=self._upload_single).grid(row=0, column=4, padx=4)
        self.file_label = ttk.Label(xfer, text="No file loaded")
        self.file_label.grid(row=0, column=5, padx=8, sticky="w")
        ttk.Button(xfer, text="Verify vs Reference",
                   command=self._verify).grid(row=1, column=0, padx=4, pady=(4, 0))
        self.ref_label = ttk.Label(xfer, text="Reference: none")
        self.ref_label.grid(row=1, column=1, columnspan=5, sticky="w", padx=4)

        # --- batch upload ---
        batch = ttk.LabelFrame(main, text="Multi-Sample Upload", padding=6)
        batch.pack(fill="x", **pad)
        ttk.Button(batch, text="Add WAV Files...",
                   command=self._add_batch_files).grid(row=0, column=0, padx=4)
        ttk.Button(batch, text="Clear",
                   command=self._clear_batch).grid(row=0, column=1, padx=4)
        self.batch_label = ttk.Label(batch, text="0 files")
        self.batch_label.grid(row=0, column=2, padx=8)
        self.batch_mode = tk.StringVar(value="instruments")
        modes = [("One sample per instrument", "instruments"),
                 ("Combine into transwave (mod wheel)", "transwave"),
                 ("Morphing soundscape (max 8)", "soundscape")]
        for i, (label, value) in enumerate(modes):
            ttk.Radiobutton(batch, text=label, value=value,
                            variable=self.batch_mode).grid(row=1, column=i,
                                                           sticky="w", padx=4)
        ttk.Button(batch, text="Upload Samples",
                   command=self._upload_batch).grid(row=0, column=3, padx=4)

        # --- transwave smasher ---
        smash_f = ttk.LabelFrame(main, text="Transwave Smasher "
                                 "(native Atom Smasher port)", padding=6)
        smash_f.pack(fill="x", **pad)
        ttk.Button(smash_f, text="Load Source WAV...",
                   command=self._smash_load).grid(row=0, column=0, padx=4)
        ttk.Label(smash_f, text="Prep:").grid(row=0, column=1, sticky="e")
        self.smash_prep_var = tk.StringVar(value="raw")
        prep_combo = ttk.Combobox(smash_f, textvariable=self.smash_prep_var,
                                  state="readonly", width=16,
                                  values=["raw", "bright", "filtered"])
        prep_combo.grid(row=0, column=2, padx=4)
        prep_combo.bind("<<ComboboxSelected>>", lambda e: self._smash_update())
        ttk.Label(smash_f, text="Start offset:").grid(row=0, column=3, sticky="e")
        self.smash_offset_var = tk.IntVar(value=0)
        tk.Spinbox(smash_f, from_=0, to=10_000_000, increment=64, width=8,
                   textvariable=self.smash_offset_var,
                   command=self._smash_update).grid(row=0, column=4, padx=2)
        ttk.Button(smash_f, text="Auto",
                   command=self._smash_auto_offset).grid(row=0, column=5, padx=2)
        self.smash_label = ttk.Label(smash_f, text="No source loaded")
        self.smash_label.grid(row=1, column=0, columnspan=3, sticky="w", padx=4)
        ttk.Button(smash_f, text="Upload Transwave to EPS",
                   command=self._smash_upload).grid(row=1, column=3,
                                                    columnspan=2, padx=4)
        ttk.Button(smash_f, text="Save XWV...",
                   command=self._smash_save).grid(row=1, column=5, padx=4)

        # --- progress + cancel ---
        prog = ttk.Frame(main)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.cancel_btn = ttk.Button(prog, text="Cancel", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="right")
        self.hex_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(prog, text="Log MIDI hex", variable=self.hex_var,
                        command=lambda: setattr(self, "verbose_midi",
                                                self.hex_var.get()))\
            .pack(side="right", padx=6)

        # --- waveform display ---
        self.canvas = tk.Canvas(main, height=140, bg=self.BG,
                                highlightthickness=1, highlightbackground="#444")
        self.canvas.pack(fill="x", **pad)
        self.canvas.bind("<Configure>", lambda e: self._plot(self.displayed_wave))

        # --- log ---
        notef = ttk.Frame(main)
        notef.pack(fill="x", **pad)
        ttk.Label(notef, text="Log note:").pack(side="left")
        self.note_var = tk.StringVar()
        note_entry = ttk.Entry(notef, textvariable=self.note_var)
        note_entry.pack(side="left", fill="x", expand=True, padx=4)
        note_entry.bind("<Return>", lambda e: self._add_note())
        ttk.Button(notef, text="Add", command=self._add_note).pack(side="left")
        ttk.Button(notef, text="Show Log File",
                   command=self._reveal_log).pack(side="left", padx=(8, 0))

        logf = ttk.LabelFrame(main, text="Event Log", padding=4)
        logf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logf, height=10, bg=self.BG, fg=self.FG,
                                state="disabled", wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_config("error", foreground="#e57373")
        self.log_text.tag_config("success", foreground="#81c784")
        self.log_text.tag_config("info", foreground=self.FG)

    # ---------------- helpers ----------------
    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:
            self.log(f"Error: {exc}", "error")

    def _midi_service(self):
        while True:
            fn = self._midi_jobs.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as exc:
                self._log_threadsafe(f"Error: {exc}", "error")

    def _refresh_ports(self):
        if mido is None:
            return

        def job():
            ins, outs = EPS16.list_inputs(), EPS16.list_outputs()
            self.ui_queue.put(("ports", (ins, outs)))
        self._midi_jobs.put(job)

    def _apply_ports(self, ins, outs):
        """UI-thread continuation of _refresh_ports."""
        self.in_combo["values"] = ins
        self.out_combo["values"] = outs
        if not ins and not outs:
            self.log("No MIDI ports found -- connect your interface and "
                     "click Refresh", "error")
        if ins and not self.in_var.get():
            self.in_var.set(ins[0])
            self._midi_jobs.put(lambda n=ins[0]: self.eps.set_input(n))
        if outs and not self.out_var.get():
            self.out_var.set(outs[0])
            self._midi_jobs.put(lambda n=outs[0]: self.eps.set_output(n))

    def _on_in_selected(self, _event=None):
        name = self.in_var.get()
        self._midi_jobs.put(lambda: self.eps.set_input(name))

    def _on_out_selected(self, _event=None):
        name = self.out_var.get()
        self._midi_jobs.put(lambda: self.eps.set_output(name))

    def _apply_selection(self):
        try:
            self.eps.set_instrument_number(int(self.inst_var.get()))
            self.eps.set_layer_number(int(self.layer_var.get()))
            self.eps.set_wavesample_number(int(self.ws_var.get()))
        except (ValueError, tk.TclError):
            pass

    def _run_job(self, job, on_done=None):
        """Run a protocol job on a worker thread."""
        if mido is None:
            messagebox.showerror("MIDI unavailable",
                                 "mido / python-rtmidi is not installed.\n\n"
                                 "Run:  pip3 install mido python-rtmidi")
            return
        if self.worker and self.worker.is_alive():
            self.log("A transfer is already running", "error")
            return
        if not self.eps.out_port or not self.eps.in_port:
            self.log("Select MIDI input and output ports first", "error")
            return
        self._apply_selection()
        self.eps.cancel_flag.clear()
        self.cancel_btn.configure(state="normal")

        def wrapper():
            try:
                job()
            except Exception as exc:
                self._log_threadsafe(f"Error: {exc}", "error")
            finally:
                self.ui_queue.put(("job_done", on_done))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _cancel(self):
        self.eps.cancel_flag.set()
        self.log("Cancelling...", "error")

    def _on_close(self):
        self.eps.cancel_flag.set()
        self._midi_jobs.put(self.eps.close)
        self._midi_jobs.put(None)              # sentinel: stop service thread
        self._midi_thread.join(timeout=2.0)
        self.session_log.line("=== session end ===")
        self.session_log.close()
        self.root.destroy()

    # ---------------- thread-safe UI updates ----------------
    def _log_threadsafe(self, msg, tag="info"):
        self.ui_queue.put(("log", (msg, tag)))

    def _drain_ui_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self.log(*payload)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "plot":
                    self._plot(payload)
                elif kind == "written":
                    now = time.time()
                    for key in payload:
                        self.slot_written[tuple(key)] = now
                elif kind == "select":
                    inst, layer, ws = payload
                    self.inst_var.set(inst)
                    self.layer_var.set(layer)
                    self.ws_var.set(ws)
                    self.log(f"Selection set to instrument {inst}, layer "
                             f"{layer}, wavesample {ws} (where the data went)")
                elif kind == "ports":
                    self._apply_ports(*payload)
                elif kind == "job_done":
                    self.cancel_btn.configure(state="disabled")
                    self.progress["value"] = 0
                    if payload:
                        payload()
        except queue.Empty:
            pass
        self.root.after(50, self._drain_ui_queue)

    def log(self, msg, tag="info"):
        self.session_log.line(f"EVENT ({tag}): {msg}")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------- waveform plot ----------------
    def _plot(self, samples):
        self.displayed_wave = samples
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 2)
        h = max(c.winfo_height(), 2)
        mid = h / 2
        c.create_line(0, mid, w, mid, fill="#3a3a3a")
        if not samples:
            return
        n = len(samples)
        points = []
        if n <= w:
            for i, s in enumerate(samples):
                x = i / max(n - 1, 1) * (w - 1)
                points += [x, mid - (s / 32768.0) * (mid - 4)]
        else:
            # min/max envelope per pixel column
            per = n / w
            for x in range(w):
                seg = samples[int(x * per): int((x + 1) * per) + 1]
                if not seg:
                    continue
                points += [x, mid - (max(seg) / 32768.0) * (mid - 4),
                           x, mid - (min(seg) / 32768.0) * (mid - 4)]
        if len(points) >= 4:
            c.create_line(*points, fill=self.WAVE_COLOR)

    # ---------------- actions ----------------
    def _current_selection(self):
        try:
            return (int(self.inst_var.get()), int(self.layer_var.get()),
                    int(self.ws_var.get()))
        except (ValueError, tk.TclError):
            return None

    def _get_wavesample(self):
        sel = self._current_selection()

        def job():
            def plot_cb(data, frac):
                self.ui_queue.put(("plot", list(data)))
                self.ui_queue.put(("progress", frac * 100))
            started = time.time()
            self.eps_wave_source = None
            self.eps_wave = self.eps.get_wavesample_data_chunked(CHUNK_SIZE, plot_cb)
            if self.eps_wave and sel:
                self.eps_wave_source = (sel, started)
            if self.eps_wave:
                self._log_threadsafe(
                    f"Received {len(self.eps_wave)} samples from the EPS", "success")
        self._run_job(job)

    def _save_wav(self):
        if not self.eps_wave:
            self.log("No wavesample fetched from the EPS yet", "error")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".wav", initialfile="output.wav",
            filetypes=[("WAV files", "*.wav")])
        if not path:
            return
        try:
            save_wav(path, self.eps_wave, 44100)
            self.log(f"Saved {os.path.basename(path)} "
                     f"({len(self.eps_wave)} samples @ 44100 Hz)", "success")
        except Exception as exc:
            self.log(f"Error saving WAV: {exc}", "error")

    def _load_wav(self):
        path = filedialog.askopenfilename(filetypes=[("WAV files", "*.wav")])
        if not path:
            return
        try:
            self.local_wave, rate = load_wav(path)
        except Exception as exc:
            self.log(f"Error: {exc}", "error")
            return
        self.file_label.configure(text=f"{os.path.basename(path)} "
                                       f"({len(self.local_wave)} smp @ {rate} Hz)")
        self._plot(self.local_wave)
        self.log(f"Loaded {os.path.basename(path)}: "
                 f"{len(self.local_wave)} samples, {rate} Hz", "success")
        self._set_reference(self.local_wave, os.path.basename(path))

    def _upload_single(self):
        if not self.local_wave:
            self.log("Load a WAV file first", "error")
            return
        audio = list(self.local_wave)
        self._set_reference(audio, self.verify_ref_name or "loaded WAV")

        sel = self._current_selection()

        def job():
            ok = self.eps.upload_wav_to_eps(
                audio, 1, 0, lambda p: self.ui_queue.put(("progress", float(p))))
            if sel:
                self.ui_queue.put(("written", [sel]))
            if ok:
                self._log_threadsafe("Complete: Uploaded wavesample", "success")
        self._run_job(job)

    # ---------------- transwave smasher actions ----------------
    def _smash_load(self):
        path = filedialog.askopenfilename(filetypes=[("WAV files", "*.wav")])
        if not path:
            return
        try:
            self.smash_source, rate = load_wav_flexible(path)
        except Exception as exc:
            self.log(f"Error: {exc}", "error")
            return
        self.smash_source_name = os.path.splitext(os.path.basename(path))[0]
        if rate != 44100:
            self.log(f"Note: source is {rate} Hz; frames are sliced by sample "
                     "count, not time (same as the original tool)", "info")
        self.smash_offset_var.set(smash_trim_offset(self.smash_source))
        self._smash_update()

    def _smash_auto_offset(self):
        if self.smash_source:
            prepped = smash_prep(self.smash_source, self.smash_prep_var.get())
            self.smash_offset_var.set(smash_trim_offset(prepped))
            self._smash_update()

    def _smash_update(self):
        """Re-run the smash with current settings and refresh preview."""
        if not self.smash_source:
            return
        try:
            offset = int(self.smash_offset_var.get())
        except (ValueError, tk.TclError):
            offset = 0
        prep = self.smash_prep_var.get()
        self.smash_frames, info = smash(self.smash_source, prep=prep,
                                        start_offset=offset)
        txt = (f"{self.smash_source_name}: {info['frames']} frames x "
               f"{SMASH_FRAME_LEN} smp (trim {info['trim_offset']})")
        if info["truncated"]:
            txt += (f" -- TRUNCATED from {info['frames_before_cap']}; "
                    "try a half-length prep")
        if info["frames"] == 0:
            txt += " -- source too short!"
        self.smash_label.configure(text=txt)
        self._plot([s for fr in self.smash_frames for s in fr])
        self.log(f"Smashed: {txt}",
                 "error" if info["frames"] == 0 else "info")

    def _smash_upload(self):
        if not self.smash_frames:
            self.log("Load and smash a source WAV first", "error")
            return
        frames = [list(f) for f in self.smash_frames]
        self._set_reference([s for fr in frames for s in fr],
                            f"{self.smash_source_name} transwave "
                            f"({len(frames)} frames)")

        def job():
            slot = self.eps.upload_as_transwave(
                frames, lambda p: self.ui_queue.put(("progress", float(p))))
            if slot is not None:
                self.ui_queue.put(("written", [(slot, 0, 1)]))
                self.ui_queue.put(("select", (slot, 0, 1)))
        self._run_job(job)

    def _smash_save(self):
        if not self.smash_frames:
            self.log("Load and smash a source WAV first", "error")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".WAV",
            initialfile=f"{self.smash_source_name}-XWV.WAV",
            filetypes=[("WAV files", "*.wav *.WAV")])
        if not path:
            return
        flat = [s for fr in self.smash_frames for s in fr]
        try:
            save_wav(path, flat, 44100)
            self.log(f"Saved {os.path.basename(path)} "
                     f"({len(self.smash_frames)} frames)", "success")
        except Exception as exc:
            self.log(f"Error saving: {exc}", "error")

    def _add_batch_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("WAV files", "*.wav")])
        for p in paths:
            if p not in self.batch_files:
                self.batch_files.append(p)
        self.batch_label.configure(text=f"{len(self.batch_files)} files")

    def _clear_batch(self):
        self.batch_files = []
        self.batch_label.configure(text="0 files")

    def _upload_batch(self):
        if not self.batch_files:
            self.log("Add WAV files first", "error")
            return
        waves = []
        try:
            for p in self.batch_files:
                samples, _ = load_wav(p)
                waves.append(samples)
        except Exception as exc:
            self.log(f"Error: {exc}", "error")
            return
        mode = self.batch_mode.get()
        if mode == "soundscape" and len(waves) > 8:
            self.log("Soundscape mode is limited to 8 samples; "
                     "using the first 8", "error")

        def progress(p):
            try:
                self.ui_queue.put(("progress", float(p)))
            except (TypeError, ValueError):
                pass

        names = [os.path.basename(p) for p in self.batch_files]
        if mode == "transwave":
            self._set_reference([s for w in waves for s in w],
                                f"batch transwave ({len(waves)} files)")

        def job():
            if mode == "instruments":
                placed = self.eps.upload_to_different_instruments(waves, progress)
                self.ui_queue.put(("written", [(s, 0, 1) for _, s in placed]))
                for idx, slot in placed:
                    self._log_threadsafe(f"  {names[idx]} -> instrument {slot}")
                if placed:
                    self._log_threadsafe("To verify one: Load WAV it, select "
                                         "its instrument, Get Wavesample, Verify")
            elif mode == "transwave":
                slot = self.eps.upload_as_transwave(waves, progress)
                if slot is not None:
                    self.ui_queue.put(("written", [(slot, 0, 1)]))
                    self.ui_queue.put(("select", (slot, 0, 1)))
            else:
                slot = self.eps.create_morphing_wave_table(waves, progress)
                if slot is not None:
                    self.ui_queue.put(("written", [(slot, i, i + 1)
                                                   for i in range(min(len(waves), 8))]))
                    self._log_threadsafe(f"Soundscape layers 0..{min(len(waves), 8) - 1}"
                                         f" in instrument {slot}")
        self._run_job(job)

    # ---------------- verification + notes ----------------
    def _set_reference(self, samples, name):
        self.verify_ref = list(samples)
        self.verify_ref_name = name
        self.ref_label.configure(text=f"Reference: {name} "
                                      f"({len(self.verify_ref)} samples)")

    def _verify(self):
        if not self.verify_ref:
            self.log("No reference: Load WAV or upload something first", "error")
            return
        if self.worker and self.worker.is_alive():
            self.log("Not verifying: a transfer is still running. Wait for "
                     "it to finish, then Get Wavesample and Verify.", "error")
            return
        if not self.eps_wave or not self.eps_wave_source:
            self.log("Get Wavesample from EPS first, then Verify", "error")
            return
        # Refuse stale comparisons (a session once compared a file against
        # the previous test's download). The reference may be loaded before
        # or after the download; what matters is where the download came
        # from and whether that slot was rewritten since.
        (src_sel, started) = self.eps_wave_source
        cur = self._current_selection()
        if cur != src_sel:
            self.log(f"Not verifying: the last download came from instrument "
                     f"{src_sel[0]}, layer {src_sel[1]}, wavesample {src_sel[2]}, "
                     f"but the selection is now {cur}. Get Wavesample again.",
                     "error")
            return
        if self.slot_written.get(src_sel, 0) > started:
            self.log("Not verifying: that wavesample was uploaded to after the "
                     "last download. Get Wavesample again.", "error")
            return
        self.log(f"VERIFY vs {self.verify_ref_name} (download from inst "
                 f"{src_sel[0]}, layer {src_sel[1]}, ws {src_sel[2]} at "
                 f"{time.strftime('%H:%M:%S', time.localtime(started))}):")
        report = compare_waves(self.verify_ref, self.eps_wave)
        for line in report:
            tag = ("success" if "EXACT" in line or "MATCH (" in line else
                   "error" if "MISMATCH" in line or "SHORT" in line else "info")
            self.log("  " + line, tag)

    def _reveal_log(self):
        """Reveal the session log in Finder (for attaching to bug reports)."""
        if not self.session_log.path:
            self.log("No session log file is open", "error")
            return
        try:
            import subprocess
            subprocess.run(["open", "-R", self.session_log.path], check=False)
        except Exception as exc:
            self.log(f"Could not open Finder: {exc}", "error")

    def _add_note(self):
        text = self.note_var.get().strip()
        if not text:
            return
        self.log(f"NOTE: {text}")
        self.note_var.set("")


def main():
    # Create a CoreMIDI client BEFORE Tk exists and keep it alive.
    # Observed on macOS 12 (Monterey) + python.org 3.12 + Tk: making the
    # first-ever CoreMIDI call after the Tk UI is fully built aborts the
    # process (Abort trap: 6 inside rtmidi's get_devices). Initializing
    # CoreMIDI first, while no Tk state exists, avoids this; later calls
    # then work normally (verified by isolating init orders).
    global _coremidi_anchor
    _coremidi_anchor = None
    if mido is not None:
        try:
            import rtmidi
            _coremidi_anchor = rtmidi.MidiIn()   # persistent client
            mido.get_input_names()               # prime mido's path too
        except Exception:
            _coremidi_anchor = None
    root = tk.Tk()
    try:
        # native-ish theme
        style = ttk.Style()
        if "aqua" in style.theme_names():
            style.theme_use("aqua")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
