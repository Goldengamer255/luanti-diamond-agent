"""The bot's view of the map, assembled from the blocks the server sends."""
from .mapblock import MAP_BLOCKSIZE, MapBlock, node_index, parse_blockdata

CONTENT_IGNORE = 127


class World:
    def __init__(self, nodedefs=None):
        self.blocks = {}
        self.nodedefs = nodedefs
        self.air_id = 126
        self.decode_errors = 0
        self.last_decode_error = None

    # ------------------------------------------------------------ ingestion

    def handle_blockdata(self, bp, reader, ser_ver):
        block = parse_blockdata(bp, reader, ser_ver)
        self.blocks[bp] = block
        return block

    def set_node(self, p, param0, param1=0, param2=0):
        bp = (p[0] >> 4, p[1] >> 4, p[2] >> 4)
        block = self.blocks.get(bp)
        if block is None or block.param0 is None:
            return False
        i = node_index(p[0] & 15, p[1] & 15, p[2] & 15)
        param0_list = list(block.param0)
        param0_list[i] = param0
        block.param0 = tuple(param0_list)
        p1 = bytearray(block.param1); p1[i] = param1; block.param1 = bytes(p1)
        p2 = bytearray(block.param2); p2[i] = param2; block.param2 = bytes(p2)
        return True

    # -------------------------------------------------------------- queries

    def is_loaded(self, p):
        return (p[0] >> 4, p[1] >> 4, p[2] >> 4) in self.blocks

    def get_content(self, p):
        """Content id at a node position, or None if that block is unknown."""
        block = self.blocks.get((p[0] >> 4, p[1] >> 4, p[2] >> 4))
        if block is None or block.param0 is None:
            return None
        return block.param0[node_index(p[0] & 15, p[1] & 15, p[2] & 15)]

    def get_node(self, p):
        block = self.blocks.get((p[0] >> 4, p[1] >> 4, p[2] >> 4))
        if block is None or block.param0 is None:
            return None
        return block.get(p[0] & 15, p[1] & 15, p[2] & 15)

    def get_name(self, p):
        cid = self.get_content(p)
        if cid is None:
            return None
        return self.nodedefs.name_of(cid) if self.nodedefs else cid

    def get_def(self, p):
        cid = self.get_content(p)
        if cid is None or self.nodedefs is None:
            return None
        return self.nodedefs.get(cid)

    def set_meta(self, p, fields, inventory=None):
        """Record a metadata update pushed by the server."""
        bp = (p[0] >> 4, p[1] >> 4, p[2] >> 4)
        block = self.blocks.get(bp)
        if block is None:
            return False
        idx = node_index(p[0] & 15, p[1] & 15, p[2] & 15)
        if fields:
            block.metadata[idx] = fields
        else:
            block.metadata.pop(idx, None)
        if inventory is not None:
            block.inventories[idx] = inventory
        return True

    def get_meta(self, p):
        """Node metadata at a position -- the server's per-block extra data."""
        block = self.blocks.get((p[0] >> 4, p[1] >> 4, p[2] >> 4))
        if block is None:
            return None
        return block.meta_at(p[0] & 15, p[1] & 15, p[2] & 15)

    def get_node_inventory(self, p):
        """A node's inventory (chest, furnace, ...) if the server has sent it."""
        block = self.blocks.get((p[0] >> 4, p[1] >> 4, p[2] >> 4))
        if block is None:
            return None
        return block.inventory_at(p[0] & 15, p[1] & 15, p[2] & 15)

    def all_metadata(self):
        """Every node that carries metadata, as (position, fields) pairs."""
        for (bx, by, bz), block in self.blocks.items():
            for idx, fields in block.metadata.items():
                z, rem = divmod(idx, 256)
                y, x = divmod(rem, 16)
                yield (bx * 16 + x, by * 16 + y, bz * 16 + z), fields

    # ------------------------------------------------- pathfinding predicates

    def is_walkable(self, p):
        """True if the node blocks movement (i.e. you can stand on it)."""
        d = self.get_def(p)
        return bool(d and d.walkable)

    def is_free(self, p):
        """True if a player body can occupy this node."""
        d = self.get_def(p)
        return d is not None and not d.walkable

    def is_climbable(self, p):
        d = self.get_def(p)
        return bool(d and d.climbable)

    def is_dangerous(self, p):
        d = self.get_def(p)
        if d is None:
            return False
        return d.damage_per_second > 0

    def is_liquid(self, p):
        d = self.get_def(p)
        return bool(d and d.is_liquid)

    def count_nodes(self):
        return len(self.blocks) * 4096

    def find_nodes(self, names, center, radius):
        """Search loaded blocks for nodes with any of the given names."""
        if self.nodedefs is None:
            return []
        wanted = {self.nodedefs.id_of(n) for n in names}
        wanted.discard(None)
        if not wanted:
            return []
        cx, cy, cz = (int(c) for c in center)
        found = []
        for p in _iter_sphere(cx, cy, cz, radius):
            if self.get_content(p) in wanted:
                found.append(p)
        found.sort(key=lambda q: (q[0]-cx)**2 + (q[1]-cy)**2 + (q[2]-cz)**2)
        return found


    def find_all(self, names, origin=None, y_range=None, limit=None):
        """Scan every loaded block for nodes of the given types.

        Much faster than `find_nodes` for large searches: it tests each block's
        content-id array wholesale before walking it, so blocks that cannot
        contain the target are skipped at C speed.
        """
        if self.nodedefs is None:
            return []
        if isinstance(names, str):
            names = [names]
        wanted = {self.nodedefs.id_of(n) for n in names}
        wanted.discard(None)
        if not wanted:
            return []

        found = []
        for (bx, by, bz), block in self.blocks.items():
            p0 = block.param0
            if not p0:
                continue
            if y_range is not None:
                lo, hi = y_range
                if by * 16 > hi or by * 16 + 15 < lo:
                    continue
            if not any(cid in p0 for cid in wanted):
                continue
            for i, cid in enumerate(p0):
                if cid in wanted:
                    z, rem = divmod(i, 256)
                    y, x = divmod(rem, 16)
                    pos = (bx * 16 + x, by * 16 + y, bz * 16 + z)
                    if y_range is None or y_range[0] <= pos[1] <= y_range[1]:
                        found.append(pos)
        if origin is not None:
            ox, oy, oz = origin
            found.sort(key=lambda q: (q[0]-ox)**2 + (q[1]-oy)**2 + (q[2]-oz)**2)
        if limit:
            found = found[:limit]
        return found

    def lowest_loaded_y(self):
        return min((bp[1] * 16 for bp in self.blocks), default=None)


def _iter_sphere(cx, cy, cz, radius):
    r = int(radius)
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            for dz in range(-r, r + 1):
                if dx * dx + dy * dy + dz * dz <= r * r:
                    yield (cx + dx, cy + dy, cz + dz)
