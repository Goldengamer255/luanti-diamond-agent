"""Tracking of active objects: other players, mobs and item drops.

Objects arrive via TOCLIENT_ACTIVE_OBJECT_REMOVE_ADD (spawn/despawn) and are
then updated by TOCLIENT_ACTIVE_OBJECT_MESSAGES, a stream of per-object
commands. Only the ones a bot needs are decoded -- chiefly position updates and
health -- and anything unrecognised is skipped without disturbing the rest.
"""
import math

from .buffer import Reader

BS = 10.0

# ActiveObjectCommand, src/activeobject.h
AO_CMD_SET_PROPERTIES = 0
AO_CMD_UPDATE_POSITION = 1
AO_CMD_SET_TEXTURE_MOD = 2
AO_CMD_SET_SPRITE = 3
AO_CMD_PUNCHED = 4
AO_CMD_UPDATE_ARMOR_GROUPS = 5
AO_CMD_SET_ANIMATION = 6
AO_CMD_SET_BONE_POSITION = 7
AO_CMD_ATTACH_TO = 8
AO_CMD_SET_PHYSICS_OVERRIDE = 9
AO_CMD_SPAWN_INFANT = 11
AO_CMD_SET_ANIMATION_SPEED = 12


class Entity:
    __slots__ = ("id", "name", "is_player", "position", "velocity", "rotation",
                 "hp", "type")

    def __init__(self, id):
        self.id = id
        self.name = ""
        self.is_player = False
        self.position = (0.0, 0.0, 0.0)
        self.velocity = (0.0, 0.0, 0.0)
        self.rotation = (0.0, 0.0, 0.0)
        self.hp = 0
        self.type = 0

    @property
    def node_pos(self):
        import math
        return tuple(math.floor(c + 0.5) for c in self.position)

    def __repr__(self):
        kind = "player" if self.is_player else "entity"
        pos = tuple(round(c, 1) for c in self.position)
        return f"<{kind} {self.name!r} id={self.id} at {pos} hp={self.hp}>"


# Mobs that attack on sight. Names are matched as substrings so that variants
# (zombie_villager, cave_spider, baby_*) are covered too. Override
# `EntityTracker.hostile_keywords` for other games.
HOSTILE_KEYWORDS = (
    "zombie", "skeleton", "creeper", "stalker", "spider", "witch", "enderman",
    "slime", "husk", "drowned", "blaze", "ghast", "pillager", "vindicator",
    "evoker", "illusioner", "silverfish", "endermite", "guardian", "shulker",
    "phantom", "vex", "hoglin", "zoglin", "piglin", "wither", "magma_cube",
    "stray", "ravager", "warden",
)


def is_hostile(entity, keywords=HOSTILE_KEYWORDS) -> bool:
    if entity.is_player or not entity.name:
        return False
    name = entity.name.lower()
    # VoxeLibre attaches a glowing-eyes entity to spiders; it cannot be hurt
    if name.endswith("_eyes"):
        return False
    return any(k in name for k in keywords)


class EntityTracker:
    def __init__(self):
        self.entities = {}

    # ------------------------------------------------------------- ingestion

    def handle_remove_add(self, r: Reader):
        """TOCLIENT_ACTIVE_OBJECT_REMOVE_ADD."""
        removed, added = [], []
        for _ in range(r.u16()):
            oid = r.u16()
            removed.append(oid)
            self.entities.pop(oid, None)
        for _ in range(r.u16()):
            oid = r.u16()
            otype = r.u8()
            data = r.long_string()
            ent = self._parse_init(oid, otype, data)
            if ent is not None:
                self.entities[oid] = ent
                added.append(ent)
        return removed, added

    def _parse_init(self, oid, otype, data):
        """Decode GenericCAO initialisation data."""
        try:
            r = Reader(data)
            version = r.u8()
            if version != 1:
                return None
            ent = Entity(oid)
            ent.type = otype
            ent.name = r.text()
            ent.is_player = r.u8() != 0
            r.s16()                             # id, repeated
            pos = r.v3f32()
            ent.position = (pos[0] / BS, pos[1] / BS, pos[2] / BS)
            ent.rotation = r.v3f32()
            ent.hp = r.u16()
            # followed by an embedded message list; parse what we can
            count = r.u8()
            for _ in range(count):
                msg = r.long_string()
                self._apply_message(ent, msg)
            return ent
        except Exception:
            return None

    def handle_messages(self, r: Reader):
        """TOCLIENT_ACTIVE_OBJECT_MESSAGES: repeated (id, message)."""
        changed = []
        while r.remaining() >= 2:
            try:
                oid = r.u16()
                msg = r.string()
            except Exception:
                break
            ent = self.entities.get(oid)
            if ent is not None and self._apply_message(ent, msg):
                changed.append(ent)
        return changed

    @staticmethod
    def _apply_message(ent, msg: bytes) -> bool:
        if not msg:
            return False
        try:
            r = Reader(msg)
            cmd = r.u8()
            if cmd == AO_CMD_UPDATE_POSITION:
                pos = r.v3f32()
                vel = r.v3f32()
                r.v3f32()                       # acceleration
                ent.position = (pos[0] / BS, pos[1] / BS, pos[2] / BS)
                ent.velocity = (vel[0] / BS, vel[1] / BS, vel[2] / BS)
                ent.rotation = r.v3f32()
                return True
            if cmd == AO_CMD_PUNCHED:
                r.u16()                         # old hp
                ent.hp = r.u16()
                return True
            if cmd == AO_CMD_SET_PROPERTIES:
                return False
        except Exception:
            return False
        return False

    # --------------------------------------------------------------- queries

    def players(self, exclude=None):
        return [e for e in self.entities.values()
                if e.is_player and e.name != exclude]

    def by_name(self, name):
        for e in self.entities.values():
            if e.name == name:
                return e
        return None

    def hostiles(self, origin, radius=16.0, keywords=HOSTILE_KEYWORDS):
        """Hostile mobs within `radius`, nearest first."""
        out = []
        for e in self.entities.values():
            if not is_hostile(e, keywords):
                continue
            d = math.dist(e.position, origin)
            if d <= radius:
                out.append((d, e))
        out.sort(key=lambda pair: pair[0])
        return [e for _d, e in out]

    def nearest(self, origin, predicate=None):
        best, best_d = None, None
        for e in self.entities.values():
            if predicate and not predicate(e):
                continue
            d = sum((a - b) ** 2 for a, b in zip(e.position, origin))
            if best_d is None or d < best_d:
                best, best_d = e, d
        return best
