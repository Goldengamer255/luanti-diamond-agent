"""Parsing of TOCLIENT_ITEMDEF -- the server's item registry.

This is what tells a bot how fast a tool digs, what it can dig at all, and how
hard it punches. Without it a bot has to guess.

Container layout (CItemDefManager::serialize):
    u8  version (0)
    u16 count
    count * [ u16 length + ItemDefinition blob ]
    u16 alias count
    alias count * [ string16 alias, string16 target ]

Each ItemDefinition is length-prefixed, so it is safe to read the prefix we
understand and skip the rest.
"""
from .buffer import Reader
from .compress import decompress_stream

# ItemDefinition type
ITEM_NONE, ITEM_NODE, ITEM_CRAFT, ITEM_TOOL = 0, 1, 2, 3


class ToolGroupCap:
    __slots__ = ("uses", "maxlevel", "times")

    def __init__(self, uses=0, maxlevel=0, times=None):
        self.uses = uses
        self.maxlevel = maxlevel
        self.times = times or {}        # rating -> seconds

    def __repr__(self):
        return f"<cap uses={self.uses} maxlevel={self.maxlevel} times={self.times}>"


class ToolCapabilities:
    __slots__ = ("full_punch_interval", "max_drop_level", "groupcaps",
                 "damage_groups", "punch_attack_uses")

    def __init__(self):
        self.full_punch_interval = 1.0
        self.max_drop_level = 0
        self.groupcaps = {}             # group name -> ToolGroupCap
        self.damage_groups = {}         # group name -> damage
        self.punch_attack_uses = 0

    @classmethod
    def parse(cls, blob: bytes):
        self = cls()
        r = Reader(blob)
        r.u8()                          # version (5)
        self.full_punch_interval = r.f32()
        self.max_drop_level = r.s16()
        for _ in range(r.u32()):
            name = r.text()
            cap = ToolGroupCap(uses=r.s16(), maxlevel=r.s16())
            for _ in range(r.u32()):
                level = r.s16()
                cap.times[level] = r.f32()
            self.groupcaps[name] = cap
        for _ in range(r.u32()):
            # Read the key before the value: in `d[k] = v` Python evaluates the
            # value first, which would consume these fields in the wrong order.
            group = r.text()
            self.damage_groups[group] = r.s16()
        if r.remaining() >= 2:
            self.punch_attack_uses = r.u16()
        return self

    def damage_against(self, armour_groups=None):
        """Damage this tool deals; `fleshy` is the usual group for mobs."""
        if not self.damage_groups:
            return 0
        if armour_groups:
            return max((self.damage_groups.get(g, 0) for g in armour_groups),
                       default=0)
        return self.damage_groups.get("fleshy", max(self.damage_groups.values()))

    def __repr__(self):
        return (f"<ToolCapabilities punch={self.full_punch_interval} "
                f"groups={list(self.groupcaps)} dmg={self.damage_groups}>")


class ItemDef:
    __slots__ = ("name", "type", "description", "stack_max", "usable",
                 "liquids_pointable", "tool_capabilities", "groups", "exact")

    def __init__(self, name=""):
        self.name = name
        self.type = ITEM_NONE
        self.description = ""
        self.stack_max = 99
        self.usable = False
        self.liquids_pointable = False
        self.tool_capabilities = None
        self.groups = {}
        self.exact = False

    @property
    def is_tool(self):
        return self.tool_capabilities is not None

    def __repr__(self):
        return (f"<ItemDef {self.name!r} type={self.type} "
                f"stack={self.stack_max} tool={self.is_tool}>")


def parse_item_definition(blob: bytes) -> ItemDef:
    """Read the prefix of an ItemDefinition, up to groups.

    Everything through `groups` sits at a fixed offset; later fields are
    presentation details a bot does not need.
    """
    r = Reader(blob)
    version = r.u8()
    if version < 6:
        raise ValueError(f"ItemDefinition version {version} not supported")
    d = ItemDef()
    d.type = r.u8()
    d.name = r.text()
    d.description = r.text()
    r.string()                          # inventory_image
    r.string()                          # wield_image
    r.v3f32()                           # wield_scale
    d.stack_max = r.s16()
    d.usable = r.bool8()
    d.liquids_pointable = r.bool8()
    caps = r.string()
    if caps:
        d.tool_capabilities = ToolCapabilities.parse(caps)
    for _ in range(r.u16()):
        gname = r.text()
        d.groups[gname] = r.s16()
    d.exact = True
    return d


class ItemDefs:
    def __init__(self):
        self.by_name = {}
        self.aliases = {}
        self.inexact = 0

    @classmethod
    def parse(cls, blob: bytes, ser_ver: int = 29):
        plain, _ = decompress_stream(blob, ser_ver)
        self = cls()
        r = Reader(plain)
        version = r.u8()
        if version != 0:
            raise ValueError(f"unsupported itemdef container version {version}")
        for _ in range(r.u16()):
            raw = r.string()
            try:
                d = parse_item_definition(raw)
            except Exception:
                self.inexact += 1
                continue
            self.by_name[d.name] = d
        try:
            for _ in range(r.u16()):
                alias = r.text()            # key first; see note above
                self.aliases[alias] = r.text()
        except Exception:
            pass
        return self

    def get(self, name):
        name = self.aliases.get(name, name)
        return self.by_name.get(name)

    @property
    def hand(self):
        """The bare hand is registered as the item with an empty name."""
        return self.by_name.get("")

    def capabilities(self, item_name):
        """Tool capabilities for an item, falling back to the bare hand."""
        d = self.get(item_name) if item_name else None
        if d is not None and d.tool_capabilities is not None:
            return d.tool_capabilities
        hand = self.hand
        return hand.tool_capabilities if hand else None


def dig_time(node_groups, caps):
    """Seconds to dig a node with these tool capabilities, or None if it cannot.

    Mirrors Luanti's getDigParams: for each group the tool can work, look up
    the node's rating in that group, reject the tool if the node's `level`
    exceeds the tool's maxlevel, and take the fastest match.
    """
    if caps is None:
        return None
    level = node_groups.get("level", 0)
    best = None
    for gname, cap in caps.groupcaps.items():
        leveldiff = cap.maxlevel - level
        if leveldiff < 0:
            continue
        rating = node_groups.get(gname)
        if rating is None:
            continue
        t = cap.times.get(rating)
        if t is None:
            continue
        if leveldiff > 1:
            t /= leveldiff
        if best is None or t < best:
            best = t
    return best
