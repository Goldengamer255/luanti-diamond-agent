"""Luanti's reliable-UDP transport (the `mtp` layer).

Wire layout, per doc/protocol.txt and src/network/mtp/internal.h:

    base header      u32 protocol_id | u16 sender_peer_id | u8 channel
    TYPE_CONTROL  0  u8 controltype [+ u16 seqnum | u16 peer_id]
    TYPE_ORIGINAL 1  payload
    TYPE_SPLIT    2  u16 seqnum | u16 chunk_count | u16 chunk_num | payload
    TYPE_RELIABLE 3  u16 seqnum | <one of the above>

Reliable packets are per-channel ordered and must be acknowledged; this class
handles acking, reordering, split reassembly and retransmission, and hands
whole application payloads to the caller through a queue.
"""
import collections
import queue
import socket
import struct
import threading
import time

PROTOCOL_ID = 0x4F457403
PEER_ID_INEXISTENT = 0
PEER_ID_SERVER = 1

TYPE_CONTROL, TYPE_ORIGINAL, TYPE_SPLIT, TYPE_RELIABLE = 0, 1, 2, 3
CONTROLTYPE_ACK, CONTROLTYPE_SET_PEER_ID, CONTROLTYPE_PING, CONTROLTYPE_DISCO = 0, 1, 2, 3

SEQNUM_INITIAL = 65500
SEQNUM_MAX = 65536
CHANNEL_COUNT = 3

BASE_HEADER_SIZE = 7
MAX_UDP_PAYLOAD = 512          # conservative; server reassembles splits anyway
RESEND_TIMEOUT = 0.4
MAX_RESENDS = 12


def seqnum_higher(newer: int, older: int) -> bool:
    """Wrap-around aware `newer > older` over the 16-bit sequence space."""
    if newer == older:
        return False
    return ((newer - older) % SEQNUM_MAX) < (SEQNUM_MAX // 2)


class _Channel:
    def __init__(self):
        self.next_out = SEQNUM_INITIAL       # next seqnum we will send
        self.next_in = SEQNUM_INITIAL        # next seqnum we expect to receive
        self.incoming = {}                   # seqnum -> inner packet, held for reordering
        self.splits = collections.defaultdict(dict)   # seqnum -> {chunk_num: bytes}


class Disconnected(Exception):
    pass


class Connection:
    """One UDP session with a Luanti server."""

    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self.host = host
        # Resolve up front: replies are matched on source IP, so the address we
        # compare against must not still be a hostname.
        self.addr = (socket.gethostbyname(host), port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.1)
        self.peer_id = PEER_ID_INEXISTENT
        self.timeout = timeout

        self.channels = [_Channel() for _ in range(CHANNEL_COUNT)]
        self.incoming = queue.Queue()        # application payloads (bytes)
        self.unacked = {}                    # (channel, seqnum) -> [data, sent_at, tries]
        self.lock = threading.Lock()

        self.running = False
        self.disconnect_reason = None
        self._peer_id_set = threading.Event()
        self._thread = None
        self._last_send = 0.0

    # ---------------------------------------------------------------- sending

    def _send_raw(self, channel: int, body: bytes):
        pkt = struct.pack(">IHB", PROTOCOL_ID, self.peer_id, channel) + body
        self.sock.sendto(pkt, self.addr)
        self._last_send = time.monotonic()

    def _send_control(self, channel: int, controltype: int, value=None):
        body = struct.pack(">BB", TYPE_CONTROL, controltype)
        if value is not None:
            body += struct.pack(">H", value)
        self._send_raw(channel, body)

    def _ack(self, channel: int, seqnum: int):
        self._send_control(channel, CONTROLTYPE_ACK, seqnum)

    def send(self, data: bytes, channel: int = 0, reliable: bool = True):
        """Queue one application payload, splitting it if it exceeds the MTU."""
        chunks = self._split(data, channel, reliable)
        for inner in chunks:
            if reliable:
                self._send_reliable_inner(channel, inner)
            else:
                self._send_raw(channel, inner)

    def _split(self, data: bytes, channel: int, reliable: bool):
        overhead = BASE_HEADER_SIZE + (3 if reliable else 0)
        room = MAX_UDP_PAYLOAD - overhead - 1          # -1 for the ORIGINAL type byte
        if len(data) <= room:
            return [struct.pack(">B", TYPE_ORIGINAL) + data]

        room = MAX_UDP_PAYLOAD - overhead - 7          # SPLIT header is 7 bytes
        parts = [data[i:i + room] for i in range(0, len(data), room)]
        ch = self.channels[channel]
        seq = ch.next_out
        ch.next_out = (ch.next_out + 1) % SEQNUM_MAX   # split id shares the seqnum space
        return [struct.pack(">BHHH", TYPE_SPLIT, seq, len(parts), i) + p
                for i, p in enumerate(parts)]

    def _send_reliable_inner(self, channel: int, inner: bytes):
        with self.lock:
            ch = self.channels[channel]
            seq = ch.next_out
            ch.next_out = (ch.next_out + 1) % SEQNUM_MAX
            body = struct.pack(">BH", TYPE_RELIABLE, seq) + inner
            self.unacked[(channel, seq)] = [body, time.monotonic(), 0]
        self._send_raw(channel, body)

    # -------------------------------------------------------------- receiving

    def _handle_datagram(self, data: bytes):
        if len(data) < BASE_HEADER_SIZE + 1:
            return
        proto, peer_id, channel = struct.unpack(">IHB", data[:BASE_HEADER_SIZE])
        if proto != PROTOCOL_ID or channel >= CHANNEL_COUNT:
            return
        self._handle_inner(channel, data[BASE_HEADER_SIZE:])

    def _handle_inner(self, channel: int, body: bytes):
        if not body:
            return
        ptype = body[0]

        if ptype == TYPE_CONTROL:
            self._handle_control(channel, body)

        elif ptype == TYPE_ORIGINAL:
            if len(body) > 1:
                self.incoming.put(body[1:])

        elif ptype == TYPE_SPLIT:
            if len(body) < 7:
                return
            _, seq, count, num = struct.unpack(">BHHH", body[:7])
            ch = self.channels[channel]
            parts = ch.splits[seq]
            parts[num] = body[7:]
            if len(parts) == count:
                del ch.splits[seq]
                self.incoming.put(b"".join(parts[i] for i in range(count)))

        elif ptype == TYPE_RELIABLE:
            if len(body) < 3:
                return
            seq = struct.unpack(">H", body[1:3])[0]
            self._ack(channel, seq)          # ack even duplicates
            ch = self.channels[channel]
            if seq == ch.next_in:
                self._handle_inner(channel, body[3:])
                ch.next_in = (ch.next_in + 1) % SEQNUM_MAX
                while ch.next_in in ch.incoming:      # drain what arrived early
                    inner = ch.incoming.pop(ch.next_in)
                    ch.next_in = (ch.next_in + 1) % SEQNUM_MAX
                    self._handle_inner(channel, inner)
            elif seqnum_higher(seq, ch.next_in):
                ch.incoming[seq] = body[3:]
            # else: already delivered, drop

    def _handle_control(self, channel: int, body: bytes):
        if len(body) < 2:
            return
        ctype = body[1]
        if ctype == CONTROLTYPE_ACK:
            if len(body) >= 4:
                seq = struct.unpack(">H", body[2:4])[0]
                with self.lock:
                    self.unacked.pop((channel, seq), None)
        elif ctype == CONTROLTYPE_SET_PEER_ID:
            if len(body) >= 4:
                self.peer_id = struct.unpack(">H", body[2:4])[0]
                self._peer_id_set.set()
        elif ctype == CONTROLTYPE_PING:
            pass                              # receipt alone keeps the peer alive
        elif ctype == CONTROLTYPE_DISCO:
            self.disconnect_reason = "server sent DISCO"
            self.running = False

    # ------------------------------------------------------------------- loop

    def _run(self):
        last_ping = time.monotonic()
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                if addr[0] == self.addr[0]:
                    self._handle_datagram(data)
            except socket.timeout:
                pass
            except OSError:
                break

            now = time.monotonic()
            with self.lock:
                due = [(k, v) for k, v in self.unacked.items()
                       if now - v[1] > RESEND_TIMEOUT]
            for (channel, seq), entry in due:
                if entry[2] >= MAX_RESENDS:
                    self.disconnect_reason = f"no ack for seqnum {seq} on channel {channel}"
                    self.running = False
                    break
                entry[1], entry[2] = now, entry[2] + 1
                self._send_raw(channel, entry[0])

            if now - last_ping > 3.0 and now - self._last_send > 3.0:
                self._send_control(0, CONTROLTYPE_PING)
                last_ping = now

    def connect(self):
        """Perform the peer handshake and start the receive loop."""
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="luanti-net")
        self._thread.start()

        # A dummy reliable packet with peer_id 0 asks the server for a peer id.
        deadline = time.monotonic() + self.timeout
        while not self._peer_id_set.is_set():
            if time.monotonic() > deadline:
                self.close()
                raise Disconnected("timed out waiting for peer id")
            self._send_raw(0, struct.pack(">BHB", TYPE_RELIABLE,
                                          SEQNUM_INITIAL, TYPE_ORIGINAL))
            self._peer_id_set.wait(0.5)
        # The server's SET_PEER_ID consumed our channel-0 outgoing seqnum.
        self.channels[0].next_out = SEQNUM_INITIAL + 1
        return self.peer_id

    def recv(self, timeout=None):
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        if self.running:
            try:
                self._send_control(0, CONTROLTYPE_DISCO)
            except OSError:
                pass
        self.running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self.sock.close()
