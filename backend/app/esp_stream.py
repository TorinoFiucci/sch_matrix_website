"""Compact, flow-controlled frame stream for the ESP32 panel.

The browser preview gets full 13,824-byte RGB frames over /api/esp/ws. That is
fine on a fast link, but the ESP sits behind Cloudflare + nginx on WiFi, and
two things went wrong there:

  * Bandwidth. 25 fps of full frames is ~350 KB/s, more than the ESP's small
    TCP window gets through a tunnel. Yet during an animation only the 832
    facade pixels can ever be lit, and most of them don't change between two
    frames. Sending only the changed pixels cuts a typical frame to ~800 bytes.

  * Hidden backlog. The proxies in front of the backend accept data as fast as
    we write it and buffer it for the slow client, so the server-side check of
    the socket buffer never saw a backlog. Every frame was queued somewhere
    along the way and the ESP played them all, long after the animation ended.
    The fix is end-to-end flow control: the ESP acknowledges each frame after
    drawing it, and we never have more than ACK_WINDOW frames unacknowledged.
    A client that can't keep up gets fewer, but always current, frames.

Wire format (server -> ESP, one binary websocket message per frame):

    byte 0      PROTOCOL_VERSION
    byte 1      flags: FLAG_RESET  - start from an all-black image
                       FLAG_SMOOTH - slow-changing content (idle), the panel
                                     may cross-fade into it
    bytes 2..5  sequence number, uint32 little-endian
    then runs until the end of the message:
        varint  pixels to skip (unchanged since the previous message)
        varint  pixels that follow (changed)
        3 * n   RGB888 bytes of those pixels

Pixels are indexed row-major in the 48x96 physical frame. Varints are LEB128
(7 bits per byte, low bits first). A frame identical to the previous one is
just the 6-byte header; it still serves as a keep-alive.

Reply (ESP -> server, binary): uint32 LE sequence number of the latest frame
the panel has drawn, then one flags byte: ACK_FLAG_RESYNC asks for the next
frame to be sent as a full RESET frame (the ESP lost track of its image).
"""

import asyncio
import struct
import time
from typing import List, Optional

from fastapi import WebSocket

PROTOCOL_VERSION = 1

FLAG_RESET = 0x01
FLAG_SMOOTH = 0x02

ACK_FLAG_RESYNC = 0x01

# Frames that may be in flight to one ESP. Two lets the next frame travel while
# the previous one is drawn, without letting any noticeable lag build up.
ACK_WINDOW = 2

# If acknowledgements stop arriving for this long, assume they were lost and
# send anyway rather than freezing the panel.
ACK_TIMEOUT_S = 3.0

HEADER = struct.Struct("<BBI")


def _put_varint(value: int, out: bytearray):
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def encode_frame(prev: Optional[bytes], cur: bytes, seq: int, smooth: bool) -> bytes:
    """Encode `cur` as the changes relative to `prev` (None = start from black)."""
    flags = FLAG_SMOOTH if smooth else 0
    if prev is None:
        flags |= FLAG_RESET
        prev = bytes(len(cur))

    out = bytearray(HEADER.pack(PROTOCOL_VERSION, flags, seq & 0xFFFFFFFF))
    header_len = len(out)

    pixels = len(cur) // 3
    i = 0
    run_end = 0  # first pixel after the previous run
    while i < pixels:
        o = i * 3
        if cur[o:o + 3] == prev[o:o + 3]:
            i += 1
            continue
        j = i + 1
        while j < pixels and cur[j * 3:j * 3 + 3] != prev[j * 3:j * 3 + 3]:
            j += 1
        _put_varint(i - run_end, out)
        _put_varint(j - i, out)
        out += cur[o:j * 3]
        run_end = j
        i = j

    # Pathological content (every other pixel changing) could make the run list
    # bigger than the frame itself. A single run over everything is never larger
    # than raw size + a few bytes, which keeps the ESP's worst case bounded.
    if len(out) - header_len > len(cur):
        out = bytearray(HEADER.pack(PROTOCOL_VERSION, flags, seq & 0xFFFFFFFF))
        _put_varint(0, out)
        _put_varint(pixels, out)
        out += cur

    return bytes(out)


class EspClient:
    """One connected ESP panel.

    Keeps the newest frame it has been offered and sends it as soon as the
    ack window allows, encoded against the last frame actually sent. Frames
    offered while the window is full are simply superseded by newer ones.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self._latest: Optional[bytes] = None
        self._latest_smooth = False
        self._last_sent: Optional[bytes] = None
        self._seq = 0
        self._acked = 0
        self._last_ack_at = time.monotonic()
        self._wakeup = asyncio.Event()

        self.sent = 0
        self.skipped = 0
        self.bytes_sent = 0

    def offer(self, frame: bytes, smooth: bool):
        if self._latest is not None:
            self.skipped += 1
        self._latest = frame
        self._latest_smooth = smooth
        self._wakeup.set()

    def on_ack(self, data: bytes):
        if len(data) < 4:
            return
        seq = struct.unpack_from("<I", data)[0]
        flags = data[4] if len(data) > 4 else 0
        # Ignore stale or bogus acks (e.g. from before a resync)
        if self._acked < seq <= self._seq:
            self._acked = seq
        if flags & ACK_FLAG_RESYNC:
            # The ESP's image no longer matches ours: rebuild it from scratch
            # with the next frame, using the last one we sent if nothing newer.
            self._acked = self._seq
            if self._latest is None:
                self._latest = self._last_sent
            self._last_sent = None
        self._last_ack_at = time.monotonic()
        self._wakeup.set()

    def _window_full(self) -> bool:
        return self._seq - self._acked >= ACK_WINDOW

    async def run(self):
        try:
            while True:
                await self._wakeup.wait()
                self._wakeup.clear()

                if self._latest is None:
                    continue

                if self._window_full():
                    waited = time.monotonic() - self._last_ack_at
                    if waited < ACK_TIMEOUT_S:
                        # Come back when an ack arrives, or when it's overdue
                        asyncio.get_running_loop().call_later(
                            ACK_TIMEOUT_S - waited, self._wakeup.set)
                        continue
                    print(f"ESP {self.websocket.client}: no ack for {waited:.1f} s, sending anyway")
                    self._acked = self._seq
                    self._last_ack_at = time.monotonic()

                frame, self._latest = self._latest, None
                self._seq += 1
                message = encode_frame(self._last_sent, frame, self._seq, self._latest_smooth)
                self._last_sent = frame
                await self.websocket.send_bytes(message)
                self.sent += 1
                self.bytes_sent += len(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await self.websocket.close()
            except Exception:
                pass


esp_clients: List[EspClient] = []


def broadcast_esp(frame: bytes, smooth: bool):
    for client in esp_clients:
        client.offer(frame, smooth)


async def serve_esp(websocket: WebSocket, initial_frame: bytes):
    """Run one ESP connection until it closes."""
    await websocket.accept()
    client = EspClient(websocket)
    esp_clients.append(client)
    print(f"ESP32 connected (stream v{PROTOCOL_VERSION}): {websocket.client}")
    sender = asyncio.create_task(client.run())
    started = time.monotonic()
    try:
        client.offer(initial_frame, smooth=False)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data:
                client.on_ack(data)
    except Exception as e:
        print(f"ESP stream error: {e}")
    finally:
        try:
            esp_clients.remove(client)
        except ValueError:
            pass
        sender.cancel()
        secs = max(0.001, time.monotonic() - started)
        avg = client.bytes_sent / client.sent if client.sent else 0
        print(f"ESP32 disconnected: {websocket.client} | {client.sent} frames sent "
              f"({client.sent / secs:.1f}/s, avg {avg:.0f} B), "
              f"{client.skipped} superseded")
