"""MapBlock decoding.

A block is 16x16x16 nodes. For serialisation format >= 29 the whole block is a
single zstd frame containing:

    u8   flags
    u16  lighting_complete
    u8   content_width  (2)
    u8   params_width   (2)
    4096 * u16 param0   (content ids)     -- bulk node data
    4096 * u8  param1   (light)
    4096 * u8  param2   (orientation/level)
    node metadata list

Older formats compress only the bulk node data, with the surrounding fields in
the clear; both are handled here.
"""
import struct

from .buffer import Reader
from .compress import decompress_stream, zlib_decompress_stream

MAP_BLOCKSIZE = 16
NODECOUNT = MAP_BLOCKSIZE ** 3
YSTRIDE = MAP_BLOCKSIZE
ZSTRIDE = MAP_BLOCKSIZE * MAP_BLOCKSIZE


def node_index(x, y, z):
    """Index of a node within its block, from block-relative coordinates."""
    return z * ZSTRIDE + y * YSTRIDE + x


class MapBlock:
    __slots__ = ("pos", "param0", "param1", "param2", "metadata", "inventories",
                 "underground", "generated")

    def __init__(self, pos):
        self.pos = pos
        self.param0 = None          # tuple of 4096 content ids
        self.param1 = None          # bytes, light
        self.param2 = None          # bytes
        self.metadata = {}          # node index -> {key: value}
        self.inventories = {}       # node index -> Inventory (chests, furnaces)
        self.underground = False
        self.generated = True

    def get(self, x, y, z):
        i = node_index(x, y, z)
        return self.param0[i], self.param1[i], self.param2[i]

    def content_at(self, x, y, z):
        return self.param0[node_index(x, y, z)]

    def meta_at(self, x, y, z):
        return self.metadata.get(node_index(x, y, z))

    def inventory_at(self, x, y, z):
        return self.inventories.get(node_index(x, y, z))


def parse_blockdata(pos, reader: Reader, ser_ver: int) -> MapBlock:
    """Decode the body of a TOCLIENT_BLOCKDATA packet (after the position)."""
    blob = reader.rest()
    block = MapBlock(pos)

    if ser_ver >= 29:
        plain, _used = decompress_stream(blob, ser_ver)
        r = Reader(plain)
        flags = r.u8()
        block.underground = bool(flags & 0x01)
        block.generated = not (flags & 0x08)
        r.u16()                                  # lighting_complete
        content_width = r.u8()
        params_width = r.u8()
        if content_width != 2 or params_width != 2:
            raise ValueError(f"unsupported widths {content_width}/{params_width}")
        _read_bulk(block, r)
        _read_metadata(block, r)
    else:
        r = Reader(blob)
        flags = r.u8()
        block.underground = bool(flags & 0x01)
        block.generated = not (flags & 0x08)
        if ser_ver >= 27:
            r.u16()
        content_width = r.u8()
        params_width = r.u8()
        bulk, used = zlib_decompress_stream(blob[r.pos:])
        _read_bulk(block, Reader(bulk))
        r.pos += used
        meta_plain, used2 = zlib_decompress_stream(blob[r.pos:])
        _read_metadata(block, Reader(meta_plain))

    return block


def _read_bulk(block: MapBlock, r: Reader):
    block.param0 = struct.unpack(f">{NODECOUNT}H", r.take(NODECOUNT * 2))
    block.param1 = r.take(NODECOUNT)
    block.param2 = r.take(NODECOUNT)


def read_inventory_text(r: Reader) -> bytes:
    """Consume a node's inventory, which is written as plain text with no
    length prefix, and return it.

    It ends at a line reading exactly `EndInventory`. Searching for
    b"EndInventory\n" is unambiguous because the per-list terminator is
    `EndInventoryList`, whose next byte is "L" rather than a newline.
    """
    start = r.pos
    end = r.data.find(b"EndInventory\n", start)
    if end >= 0:
        r.pos = end + len(b"EndInventory\n")
        return r.data[start:r.pos]
    end = r.data.find(b"EndInventory", start)       # tolerate a missing newline
    if end < 0:
        raise ValueError("node inventory is not terminated")
    r.pos = end + len(b"EndInventory")
    return r.data[start:r.pos]


def _read_metadata(block: MapBlock, r: Reader):
    """Node metadata: a server's per-node extra data.

    Each entry is a set of string variables followed by the node's *inventory*,
    serialised as text. The inventory is included even in network sends, which
    is what makes chest and furnace contents visible to a client.
    """
    if r.remaining() < 1:
        return
    version = r.u8()
    if version == 0:
        return
    if version != 2:
        raise ValueError(f"unsupported node metadata version {version}")
    count = r.u16()
    for _ in range(count):
        idx = r.u16()
        nvars = r.u32()
        fields = {}
        for _ in range(nvars):
            key = r.text()
            value = r.long_string()
            r.u8()                               # private flag
            fields[key] = value
        block.metadata[idx] = fields
        text = read_inventory_text(r)
        if text:
            from .inventory import Inventory
            block.inventories[idx] = Inventory.parse(
                text.decode("utf-8", "replace"))
