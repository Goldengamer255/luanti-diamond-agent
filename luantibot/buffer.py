"""Luanti wire-format primitives.

All integers are big endian. Floats are IEEE-754 f32 (protocol >= 37).
Strings are length-prefixed: u16 for `string`, u32 for `long_string`.
"""
import struct


class ReadError(Exception):
    pass


class Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ReadError(f"want {n} bytes at {self.pos}, have {self.remaining()}")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def _u(self, fmt, size):
        return struct.unpack(fmt, self.take(size))[0]

    def u8(self):  return self._u(">B", 1)
    def s8(self):  return self._u(">b", 1)
    def u16(self): return self._u(">H", 2)
    def s16(self): return self._u(">h", 2)
    def u32(self): return self._u(">I", 4)
    def s32(self): return self._u(">i", 4)
    def u64(self): return self._u(">Q", 8)
    def s64(self): return self._u(">q", 8)
    def f32(self): return self._u(">f", 4)
    def bool8(self): return self.u8() != 0

    def string(self) -> bytes:
        return self.take(self.u16())

    def long_string(self) -> bytes:
        return self.take(self.u32())

    def text(self) -> str:
        return self.string().decode("utf-8", "replace")

    def long_text(self) -> str:
        return self.long_string().decode("utf-8", "replace")

    def wide_string(self) -> str:
        n = self.u16()
        return self.take(n * 2).decode("utf-16-be", "replace")

    def v3s16(self):
        return (self.s16(), self.s16(), self.s16())

    def v3s32(self):
        return (self.s32(), self.s32(), self.s32())

    def v3f32(self):
        return (self.f32(), self.f32(), self.f32())

    def v2f32(self):
        return (self.f32(), self.f32())

    def rest(self) -> bytes:
        return self.take(self.remaining())


class Writer:
    __slots__ = ("buf",)

    def __init__(self, *initial: bytes):
        self.buf = bytearray()
        for b in initial:
            self.buf += b

    def raw(self, b: bytes):      self.buf += b; return self
    def u8(self, v):   self.buf += struct.pack(">B", v & 0xFF); return self
    def s8(self, v):   self.buf += struct.pack(">b", v); return self
    def u16(self, v):  self.buf += struct.pack(">H", v & 0xFFFF); return self
    def s16(self, v):  self.buf += struct.pack(">h", v); return self
    def u32(self, v):  self.buf += struct.pack(">I", v & 0xFFFFFFFF); return self
    def s32(self, v):  self.buf += struct.pack(">i", v); return self
    def u64(self, v):  self.buf += struct.pack(">Q", v); return self
    def f32(self, v):  self.buf += struct.pack(">f", v); return self
    def bool8(self, v): return self.u8(1 if v else 0)

    def string(self, s):
        b = s.encode("utf-8") if isinstance(s, str) else s
        if len(b) > 0xFFFF:
            raise ValueError("string too long for u16 prefix")
        return self.u16(len(b)).raw(b)

    def long_string(self, s):
        b = s.encode("utf-8") if isinstance(s, str) else s
        return self.u32(len(b)).raw(b)

    def wide_string(self, s: str):
        b = s.encode("utf-16-be")
        return self.u16(len(b) // 2).raw(b)

    def v3s16(self, v):
        return self.s16(v[0]).s16(v[1]).s16(v[2])

    def v3s32(self, v):
        return self.s32(v[0]).s32(v[1]).s32(v[2])

    def v3f32(self, v):
        return self.f32(v[0]).f32(v[1]).f32(v[2])

    def bytes(self) -> bytes:
        return bytes(self.buf)

    def __len__(self):
        return len(self.buf)
