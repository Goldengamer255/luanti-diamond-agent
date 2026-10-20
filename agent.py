"""An LLM agent that plays Luanti until a diamond is in its inventory.

    python agent.py                 # run the agent
    python agent.py --check         # connect, print one observation, exit

Design, in one paragraph: the model never sees the raw game. Every turn the
harness builds a fresh text observation (position, health, inventory, the
nearest ore it can see through the streamed map, nearby stations and mobs) and
sends it together with a small memory block that is rewritten in place -- the
model's own notes plus a one-line log of the last actions. There is no growing
transcript, so every request costs roughly the same number of tokens however
long the run lasts. The model answers with a tool call; tools are mid-level
("gather 3 logs", "craft a stone pickaxe", "staircase down to y=-55") so the
harness owns walking, digging, tool choice and crafting-table juggling, and the
model owns the plan. The harness, not the model, decides when the goal is met.

Configuration comes from the environment or a `.env` file next to this script:

    LUANTI_HOST, LUANTI_PORT, LUANTI_USER, LUANTI_PASSWORD
    LLM_BASE_URL   e.g. https://agent.elliottwen.info/v1
    LLM_API_KEY
    LLM_MODEL      e.g. MiniMax-M3
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load_dotenv(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

from luantibot import BotError, recipes                    # noqa: E402
from luantibot import mcp_server as M                      # noqa: E402

SESSION = M.SESSION

# --------------------------------------------------------------------------
# game vocabulary
# --------------------------------------------------------------------------

DIAMOND_ITEM = "mcl_core:diamond"
TABLE = "mcl_crafting_table:crafting_table"
FURNACE_ITEM = "mcl_furnaces:furnace"
FURNACE_NODES = ("mcl_furnaces:furnace", "mcl_furnaces:furnace_active")

# Friendly names the model can use; each maps to the node names it covers.
BLOCK_ALIASES = {
    "log": list(recipes.LOGS),
    "stone": ["mcl_core:stone", "mcl_deepslate:deepslate", "mcl_core:cobble"],
    "coal": ["mcl_core:stone_with_coal", "mcl_deepslate:deepslate_with_coal"],
    "iron": ["mcl_core:stone_with_iron", "mcl_deepslate:deepslate_with_iron"],
    "diamond": ["mcl_core:stone_with_diamond",
                "mcl_deepslate:deepslate_with_diamond"],
}
ORES_SHOWN = ("diamond", "iron", "coal")

PICKS = ["mcl_tools:pick_diamond", "mcl_tools:pick_iron",
         "mcl_tools:pick_stone", "mcl_tools:pick_wood"]


def resolve_blocks(name):
    return BLOCK_ALIASES.get(name.lower().strip(), [name])


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------

def _dist(bot, p):
    return math.dist(bot.position, p)


def nearest_station(bot, names, radius=24):
    hits = bot.find_blocks(list(names), radius=radius)
    return min(hits, key=lambda p: _dist(bot, p)) if hits else None


class Vitals:
    """Things that happen between turns which the model would otherwise miss."""
    last_hp = None
    deaths = []                 # (position, step-time) of each death
    reflex_kills = 0
    drops_seen = 0
    drops_missed = 0


def on_death_factory(bot):
    def on_death():
        Vitals.deaths.append(bot.node_pos)
    return on_death


def reflex(bot, _state={"next": 0.0}):
    """Runs on the bot thread while the model is thinking: punch back."""
    now = time.monotonic()
    if now < _state["next"] or bot.dead:
        return
    _state["next"] = now + 0.5
    if bot.hostiles(5):
        Vitals.reflex_kills += bot.defend(radius=6, timeout=10)


def observe(bot):
    """The compact text the model sees every turn."""
    x, y, z = bot.node_pos
    lines = [f"Position: ({x}, {y}, {z})   HP: {bot.hp}/20"
             + ("   ** DEAD (respawning) **" if bot.dead else "")]
    if Vitals.last_hp is not None and bot.hp < Vitals.last_hp:
        lines.append(f"!! Took {Vitals.last_hp - bot.hp} damage since last turn")
    Vitals.last_hp = bot.hp
    if Vitals.deaths:
        lines.append(f"!! You have died {len(Vitals.deaths)} time(s); last death at "
                     f"{Vitals.deaths[-1]} -- everything you carried dropped there "
                     f"and despawns after ~5 min")
    lines += [
             f"Standing on: {bot.block_at((x, y - 1, z))}",
             f"Map known down to y={bot.world.lowest_loaded_y()}"]

    counts = bot.item_counts()
    inv = ", ".join(f"{n.split(':')[-1]} x{c}"
                    for n, c in sorted(counts.items(), key=lambda kv: -kv[1]))
    lines.append(f"Inventory: {inv or 'empty'}")
    best = next((p for p in PICKS if counts.get(p)), None)
    lines.append(f"Best pickaxe: {best.split(':')[-1] if best else 'none'}")

    # The wallhack: anything in the streamed map is visible, walls or not.
    lines.append("Visible ore (nearest first):")
    for alias in ORES_SHOWN:
        hits = bot.scan(BLOCK_ALIASES[alias], limit=3)
        if hits:
            desc = "; ".join(f"({p[0]},{p[1]},{p[2]}) {_dist(bot, p):.0f}m"
                             for p in hits)
            lines.append(f"  {alias}: {desc}")
        else:
            lines.append(f"  {alias}: none in loaded map")
    logs = bot.scan(BLOCK_ALIASES["log"], limit=1)
    lines.append(f"  nearest tree log: "
                 + (f"{_dist(bot, logs[0]):.0f}m away" if logs else "none"))

    for label, names in (("crafting table", (TABLE,)), ("furnace", FURNACE_NODES)):
        p = nearest_station(bot, names)
        if p:
            lines.append(f"Placed {label} at {p} ({_dist(bot, p):.0f}m)")
    craft_grid = len(bot._inv_list("craft"))
    lines.append(f"Craft grid: {'3x3 (table open)' if craft_grid >= 9 else '2x2'}")

    mobs = bot.hostiles(16)
    if mobs:
        lines.append("HOSTILES: " + ", ".join(
            f"{m.name} {_dist(bot, m.position):.0f}m" for m in mobs[:5]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# tools -- each runs on the bot thread and returns a small dict
# --------------------------------------------------------------------------

def _run(job, timeout=600):
    return SESSION.submit(job, timeout=timeout)


def collect_drops(bot, radius=8.0, settle=1.2):
    """Walk over the item entities near us so the server hands them over.

    A log cut from high in a tree drops to the ground several nodes away, out
    of pickup range, so waiting where we stand loses about half of them.
    """
    bot.wait(settle)                          # let drops fall and land
    seen, skip = set(), set()
    for _ in range(6):
        drops = [e for e in bot.entities.values()
                 if "item" in (e.name or "") and not e.is_player
                 and e.id not in skip
                 and math.dist(e.position, bot.position) <= radius]
        if not drops:
            break
        target = min(drops, key=lambda e: math.dist(e.position, bot.position))
        seen.add(target.id)
        # The drop usually sits in the hole we just dug, which is not a
        # standable node; tolerance lets goto settle for a neighbour.
        if math.dist(target.position, bot.position) > 1.5:
            try:
                bot.goto(target.node_pos, tolerance=1, timeout=8)
            except Exception:
                pass
            bot.wait(0.5)
        # Punching a builtin item entity picks it up.
        if target.id in bot.entities and bot.in_reach(target.position):
            bot.attack(target)
            bot.wait(0.5)
        if target.id in bot.entities:
            skip.add(target.id)                # unreachable; move on
    Vitals.drops_seen += len(seen)
    Vitals.drops_missed += len(skip)


def t_gather_inner(bot, names, count=1, max_distance=48):
    """Walk to and dig the nearest `count` nodes of a type, collecting drops."""
    before = bot.item_counts()
    dug, failed, tried = 0, 0, set()
    while dug < int(count) and failed < 6:
        hits = [p for p in bot.scan(names, limit=60)
                if p not in tried and _dist(bot, p) <= float(max_distance)
                and bot.can_dig(p) and bot._opening_is_safe(p)]
        if not hits:
            break
        # prefer targets near our own height: less climbing, fewer falls
        hits.sort(key=lambda p: _dist(bot, p) + 2 * abs(p[1] - bot.node_pos[1]))
        target = hits[0]
        tried.add(target)
        try:
            ok = bot.dig(target, timeout=40)
        except BotError:
            ok = False
        if ok:
            dug += 1
            collect_drops(bot)
        else:
            failed += 1
        if bot.hostiles(5):
            bot.defend(radius=6, timeout=20)
    after = bot.item_counts()
    gained = {k.split(":")[-1]: v - before.get(k, 0) for k, v in after.items()
              if v > before.get(k, 0)}
    out = {"dug": dug, "gained": gained, "position": list(bot.node_pos)}
    print(f"   [drops] seen={Vitals.drops_seen} missed={Vitals.drops_missed}")
    if dug < int(count):
        out["note"] = ("no more reachable/safe targets within range"
                       if failed < 6 else "repeated dig failures")
    return out


def t_gather(block, count=1, max_distance=48):
    """Find and dig the nearest `count` nodes of a type, collecting the drops."""
    names = resolve_blocks(block)
    return _run(lambda: t_gather_inner(SESSION.require(), names, count,
                                       max_distance), timeout=900)


def _ensure_station(bot, node_names, item):
    """Return the position of a usable station, placing one if we carry it."""
    p = nearest_station(bot, node_names, radius=16)
    if p is not None:
        try:
            if bot.in_reach(p) or bot.approach(p, timeout=20):
                return p
        except BotError:
            pass                    # can't get there; build another below
    if bot.inventory.count(item) < 1:
        if item == TABLE and _have_planks(bot.item_counts()) + \
                4 * _have_logs(bot.item_counts()) >= 4:
            if _have_planks(bot.item_counts()) < 4:
                bot.craft(_adapt_recipe(bot, recipes.WOOD, None))
            bot.craft(_adapt_recipe(bot, TABLE, recipes.get(TABLE)[0]))
        else:
            return None
    spot = bot.find_placement_spot()
    if spot is None or not bot.place_block(spot, item=item):
        raise BotError(f"could not find anywhere to place {item}")
    bot.wait(0.5)
    return spot


# Each log species makes its own planks; tools and tables accept any of them,
# but recipes.py only knows oak.
PLANKS_OF = {"mcl_core:tree": "mcl_core:wood",
             "mcl_core:birchtree": "mcl_core:birchwood",
             "mcl_core:sprucetree": "mcl_core:sprucewood",
             "mcl_core:acaciatree": "mcl_core:acaciawood",
             "mcl_core:darktree": "mcl_core:darkwood",
             "mcl_core:jungletree": "mcl_core:junglewood"}
PLANK_NAMES = set(PLANKS_OF.values())


def _adapt_recipe(bot, item, rows):
    """Swap oak logs/planks in a recipe for whichever species we carry."""
    counts = bot.item_counts()
    if item in PLANK_NAMES:
        if not any(counts.get(l) for l in PLANKS_OF):
            raise BotError("no logs of any species in inventory")
        log = next((l for l, p in PLANKS_OF.items()
                    if p == item and counts.get(l)), None) \
            or max(PLANKS_OF, key=lambda l: counts.get(l, 0))
        return [[log]]
    planks = max(PLANK_NAMES, key=lambda p: counts.get(p, 0))
    return [[planks if c == recipes.WOOD else c for c in row] for row in rows]


def t_craft(item, times=1):
    def job():
        bot = SESSION.require()
        lookup = recipes.WOOD if item in PLANK_NAMES else item
        entry = recipes.get(lookup)
        if entry is None:
            raise BotError(f"no recipe known for {item}. Known: "
                           + ", ".join(recipes.RECIPES))
        rows, needs_table = entry
        rows = _adapt_recipe(bot, item, rows)
        if needs_table and len(bot._inv_list("craft")) < 9:
            table = _ensure_station(bot, (TABLE,), TABLE)
            if table is None:
                raise BotError(f"{item} needs a crafting table: craft "
                               f"{TABLE} first (4 planks)")
            bot.open_node(table)
        stack = bot.craft(rows, times=int(times))
        if stack is None:
            return {"crafted": None, "note": "server produced nothing"}
        return {"crafted": f"{stack.name.split(':')[-1]} x{stack.count}",
                "inventory_now": bot.inventory.count(stack.name)}
    return _run(job, timeout=180)


def t_smelt(item, fuel, count=1):
    def job():
        bot = SESSION.require()
        furnace = _ensure_station(bot, FURNACE_NODES, FURNACE_ITEM)
        if furnace is None:
            raise BotError("no furnace nearby and none in inventory: craft "
                           f"{FURNACE_ITEM} (8 cobble) first")
        out = bot.smelt(furnace, item, fuel, count=int(count),
                        timeout=15 * int(count) + 60)
        return {"furnace": list(furnace), "output": out}
    return _run(job, timeout=15 * int(count) + 240)


def t_mine_down(target_y, timeout=900):
    def job():
        bot = SESSION.require()
        start = bot.node_pos
        ok = bot.mine_to_depth(int(target_y), timeout=float(timeout))
        return {"reached": ok, "from": list(start), "position": list(bot.node_pos),
                "hp": bot.hp}
    return _run(job, timeout=float(timeout) + 120)


HEAD = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}


def t_tunnel_inner(bot, direction, length=16):
    """Dig a straight 2-high tunnel at the current height -- branch mining."""
    dx, dz = HEAD[direction]
    start = bot.node_pos
    steps, why = 0, "done"
    for _ in range(int(length)):
        p = bot.node_pos
        feet = (p[0] + dx, p[1], p[2] + dz)
        head = (feet[0], feet[1] + 1, feet[2])
        floor = (feet[0], feet[1] - 1, feet[2])
        if not (bot._opening_is_safe(feet) and bot._opening_is_safe(head)):
            why = "liquid or lava ahead, stopped"
            break
        fd = bot.world.get_def(floor)
        if fd is None or not fd.walkable:
            why = "drop or unloaded map ahead, stopped"
            break
        blocked = False
        for node in (head, feet):
            if bot.world.is_walkable(node):
                try:
                    if not bot.dig(node, approach=False, timeout=20):
                        blocked = True
                except BotError:
                    blocked = True
            if blocked:
                break
        if blocked or not bot.goto(feet, tolerance=0, timeout=10, replan=False):
            why = "blocked"
            break
        steps += 1
        if bot.hostiles(5):
            bot.defend(radius=6, timeout=20)
    return {"moved": steps, "from": list(start), "position": list(bot.node_pos),
            "stopped_because": why}


def t_tunnel(direction, length=16):
    """Dig a straight 2-high tunnel at the current height -- branch mining."""
    return _run(lambda: t_tunnel_inner(SESSION.require(), direction, length),
                timeout=60 + 30 * int(length))


# --------------------------------------------------------------------------
# macro tools: whole stretches of the tech tree as one call
# --------------------------------------------------------------------------
#
# Model latency (~12 s a call) dominates a speedrun, so these push the known
# recipe for getting a diamond into code and leave the model to sequence them
# and to recover when one fails. The fine-grained tools above stay available
# for exactly that.

def _sign(v):
    return (v > 0) - (v < 0)


def _clear(bot, node):
    """Dig a node if it is solid. False if unsafe or undiggable."""
    if not bot._opening_is_safe(node):
        return False
    if not bot.world.is_walkable(node):
        return True
    try:
        return bot.can_dig(node) and bot.dig(node, approach=False, timeout=20)
    except BotError:
        return False


def _tunnel_step(bot, p, dx, dz, dy):
    """Move one node horizontally and up to one node vertically by digging."""
    nxt = (p[0] + dx, p[1] + dy, p[2] + dz)
    floor = (nxt[0], nxt[1] - 1, nxt[2])
    fd = bot.world.get_def(floor)
    if fd is None:
        return False
    if not fd.walkable:
        # an open drop: fine if it is short, never into liquid
        depth = 0
        while depth < 4:
            d = bot.world.get_def((nxt[0], nxt[1] - 1 - depth, nxt[2]))
            if d is None or d.is_liquid or d.damage_per_second > 0:
                return False
            if d.walkable:
                break
            depth += 1
        if depth >= 4:
            return False
    need = [(nxt[0], nxt[1] + 1, nxt[2]), nxt]
    if dy > 0:
        need.insert(0, (p[0], p[1] + 2, p[2]))          # headroom to step up
    if dy < 0:
        need.insert(0, (nxt[0], nxt[1] + 2, nxt[2]))    # head passes here
    for node in need:
        if not _clear(bot, node):
            return False
    return bot.goto(nxt, tolerance=0, timeout=10, replan=False) or \
        bot.node_pos == nxt


def dig_to(bot, target, max_steps=200, deadline=None):
    """Tunnel a staircase until the bot stands in `target`'s space.

    Walking into the ore's own node guarantees its drop is picked up, which
    digging it from arm's length does not.
    """
    target = tuple(target)
    for _ in range(max_steps):
        if deadline and time.monotonic() > deadline:
            return False
        p = bot.node_pos
        if p == target or (p[0], p[1] + 1, p[2]) == target:
            return True
        if bot.dead:
            return False
        if bot.hostiles(4):
            bot.defend(radius=6, timeout=15)
        ddx, ddy, ddz = (target[i] - p[i] for i in range(3))
        # target directly below: dig down onto it
        if ddx == 0 and ddz == 0:
            if ddy < 0:
                below = (p[0], p[1] - 1, p[2])
                if not _clear(bot, below):
                    return False
                bot.wait_for(lambda: bot.node_pos[1] < p[1], timeout=4)
                continue
            if not _clear(bot, (p[0], p[1] + 2, p[2])):
                return False
            continue
        dy = _sign(ddy)
        # try the dominant axis first, then the other, then level
        axes = [(_sign(ddx), 0), (0, _sign(ddz))]
        if abs(ddz) > abs(ddx):
            axes.reverse()
        moved = False
        for dx, dz in axes:
            if dx == 0 and dz == 0:
                continue
            for vy in ((dy, 0) if dy else (0,)):
                if _tunnel_step(bot, p, dx, dz, vy):
                    moved = True
                    break
            if moved:
                break
        if not moved:
            return False
    return False


def _nearest(bot, names, max_dist=64):
    hits = [p for p in bot.scan(names, limit=40) if _dist(bot, p) <= max_dist]
    return min(hits, key=lambda p: _dist(bot, p)) if hits else None


def _mine_blocks(bot, names, count, deadline, max_dist=48):
    """dig_to the nearest matching blocks until `count` have been taken."""
    got, bad = 0, set()
    while got < count and time.monotonic() < deadline:
        hits = [p for p in bot.scan(names, limit=40)
                if p not in bad and _dist(bot, p) <= max_dist
                and bot.world.get_def(p) is not None]
        if not hits:
            return got
        t = min(hits, key=lambda p: _dist(bot, p))
        if dig_to(bot, t, deadline=deadline):
            got += 1
            collect_drops(bot, radius=3, settle=0.3)
        else:
            bad.add(t)
            if len(bad) > 6:
                return got
    return got


def _have_logs(c):
    return sum(c.get(l, 0) for l in PLANKS_OF)


def _have_planks(c):
    return sum(c.get(p, 0) for p in PLANK_NAMES)


def _craft(bot, item, times=1):
    rows, needs_table = recipes.get(recipes.WOOD if item in PLANK_NAMES else item)
    rows = _adapt_recipe(bot, item, rows)
    if needs_table and len(bot._inv_list("craft")) < 9:
        table = _ensure_station(bot, (TABLE,), TABLE)
        if table is None:
            if _have_planks(bot.item_counts()) < 4:
                _craft(bot, recipes.WOOD)
            _craft(bot, TABLE)
            table = _ensure_station(bot, (TABLE,), TABLE)
        bot.open_node(table)
    stack = bot.craft(rows, times=times)
    if stack is None:
        raise BotError(f"crafting {item} produced nothing")
    return stack


def t_make_wood_pickaxe():
    """Logs -> planks -> sticks -> table -> wooden pickaxe, plus spare wood."""
    def job():
        bot = SESSION.require()
        deadline = time.monotonic() + 300
        # 6 logs = 24 planks: pickaxe 3, sticks 4, three tables 12, fuel 3+
        def wood():
            c = bot.item_counts()
            return _have_logs(c) + _have_planks(c) / 4

        while wood() < 6 and time.monotonic() < deadline:
            # Trunks stand on open ground, so walking up and chopping works;
            # tunnelling (dig_to) is only the fallback for awkward trees.
            got = t_gather_inner(bot, list(PLANKS_OF), math.ceil(6 - wood()))
            if not got["dug"] and \
                    not _mine_blocks(bot, list(PLANKS_OF), 1, deadline, max_dist=40):
                break
        if wood() < 2:
            raise BotError(f"could only find {wood():.0f} logs' worth of wood")
        # one craft per species, since a recipe takes a single log type
        while _have_logs(bot.item_counts()):
            c = bot.item_counts()
            species = max(PLANKS_OF, key=lambda l: c.get(l, 0))
            _craft(bot, PLANKS_OF[species], times=c[species])
        if bot.item_counts().get(recipes.STICK, 0) < 6:
            _craft(bot, recipes.STICK, times=2)
        if not bot.item_counts().get("mcl_tools:pick_wood"):
            _craft(bot, "mcl_tools:pick_wood")
        return {"ok": True, "inventory": _short(bot.item_counts())}
    return _run(job, timeout=420)


def t_make_stone_pickaxe():
    """Dig down for cobble, craft a stone pickaxe and a furnace."""
    def job():
        bot = SESSION.require()
        deadline = time.monotonic() + 300
        COB = recipes.COBBLE
        # staircase down: every step yields cobble and we pass over the drops
        start_y = bot.node_pos[1]
        while bot.item_counts().get(COB, 0) < 11 and time.monotonic() < deadline:
            before = bot.item_counts().get(COB, 0)
            stone = _nearest(bot, BLOCK_ALIASES["stone"][:2], max_dist=12)
            if stone is not None:
                dig_to(bot, stone, max_steps=20, deadline=deadline)
                collect_drops(bot, radius=3, settle=0.3)
            if bot.item_counts().get(COB, 0) == before:
                bot.mine_to_depth(bot.node_pos[1] - 3, timeout=60)
        if not bot.item_counts().get("mcl_tools:pick_stone"):
            _craft(bot, "mcl_tools:pick_stone")
        if bot.item_counts().get(COB, 0) >= 8 and not bot.item_counts().get(FURNACE_ITEM):
            _craft(bot, FURNACE_ITEM)
        return {"ok": True, "descended": start_y - bot.node_pos[1],
                "inventory": _short(bot.item_counts())}
    return _run(job, timeout=420)


IRON_DROPS = ("mcl_raw_ores:raw_iron", "mcl_core:stone_with_iron",
              "mcl_deepslate:deepslate_with_iron")


def _smelt(bot, item, count, deadline):
    """Smelt with whatever fuel we have; bot.smelt under-fuels with planks."""
    c = bot.item_counts()
    fuel = "mcl_core:coal_lump" if c.get("mcl_core:coal_lump") else \
        max(PLANK_NAMES, key=lambda p: c.get(p, 0))
    n_fuel = math.ceil(count / 8) if "coal" in fuel else math.ceil(count / 1.5)
    if c.get(fuel, 0) < n_fuel:
        if _have_logs(c):
            _craft(bot, recipes.WOOD)
        fuel = max(PLANK_NAMES, key=lambda p: bot.item_counts().get(p, 0))
        n_fuel = math.ceil(count / 1.5)
    furnace = _ensure_station(bot, FURNACE_NODES, FURNACE_ITEM)
    if furnace is None:
        raise BotError("no furnace: need 8 cobble")
    if not bot.in_reach(furnace):
        bot.approach(furnace)
    bot.put_into_node(furnace, item, "src", count)
    bot.put_into_node(furnace, fuel, "fuel", n_fuel)
    got = 0
    while got < count and time.monotonic() < deadline:
        bot.wait(2.5)
        got += sum(bot.take_from_node(furnace, "dst").values())
    return got


def t_make_iron_pickaxe():
    """Mine 3 iron ore (seen through walls), smelt it, craft the pickaxe."""
    def job():
        bot = SESSION.require()
        deadline = time.monotonic() + 420
        if bot.item_counts().get("mcl_tools:pick_iron"):
            return {"ok": True, "note": "already have one"}

        def iron_held():
            c = bot.item_counts()
            return sum(c.get(i, 0) for i in IRON_DROPS)

        def ingots():
            return bot.item_counts().get(recipes.IRON, 0)

        while iron_held() + ingots() < 3 and time.monotonic() < deadline:
            if not _mine_blocks(bot, BLOCK_ALIASES["iron"], 1, deadline, max_dist=40):
                # none visible: go deeper, iron is commonest below y=16
                bot.mine_to_depth(bot.node_pos[1] - 8, timeout=90)
        if not bot.item_counts().get(FURNACE_ITEM) and \
                nearest_station(bot, FURNACE_NODES, 16) is None:
            if bot.item_counts().get(recipes.COBBLE, 0) < 8:
                _mine_blocks(bot, BLOCK_ALIASES["stone"][:2],
                             8 - bot.item_counts().get(recipes.COBBLE, 0), deadline)
            _craft(bot, FURNACE_ITEM)
        while ingots() < 3 and time.monotonic() < deadline:
            raw = next((i for i in IRON_DROPS if bot.item_counts().get(i)), None)
            if raw is None:
                break
            _smelt(bot, raw, min(bot.item_counts()[raw], 3 - ingots()), deadline)
        if ingots() < 3:
            raise BotError(f"only {ingots()} iron ingots")
        if bot.item_counts().get(recipes.STICK, 0) < 2:
            _craft(bot, recipes.STICK)
        _craft(bot, "mcl_tools:pick_iron")
        return {"ok": True, "inventory": _short(bot.item_counts())}
    return _run(job, timeout=540)


def t_get_diamond():
    """Tunnel to the nearest diamond ore the map shows, descending to find one."""
    def job():
        bot = SESSION.require()
        deadline = time.monotonic() + 600
        names = BLOCK_ALIASES["diamond"]
        tried = set()
        while time.monotonic() < deadline:
            if bot.inventory.count(DIAMOND_ITEM):
                return {"ok": True, "diamonds": bot.inventory.count(DIAMOND_ITEM),
                        "position": list(bot.node_pos)}
            if bot.hp <= 6:
                return {"ok": False, "note": "HP low, stopped", "hp": bot.hp}
            hits = [p for p in bot.scan(names, limit=20) if p not in tried]
            if hits:
                t = min(hits, key=lambda p: _dist(bot, p))
                tried.add(t)
                dig_to(bot, t, max_steps=250, deadline=deadline)
                collect_drops(bot, radius=4, settle=0.5)
                continue
            # nothing visible yet: head down; the map streams in as we go
            if bot.node_pos[1] > -50:
                bot.mine_to_depth(max(bot.node_pos[1] - 12, -55), timeout=120)
            else:
                t_tunnel_inner(bot, "north", 24)
        return {"ok": False, "note": "timed out", "position": list(bot.node_pos)}
    return _run(job, timeout=700)


def _short(counts):
    return {k.split(":")[-1]: v for k, v in counts.items()}


def t_goto(x, y, z):
    return M.t_goto(x, y, z, tolerance=1, timeout=120)


def t_dig(x, y, z):
    return M.t_dig(x, y, z, timeout=40)


def t_find(block, limit=8):
    def job():
        bot = SESSION.require()
        hits = bot.scan(resolve_blocks(block), limit=int(limit))
        return {"block": block, "positions": [
            f"({p[0]},{p[1]},{p[2]}) {_dist(bot, p):.0f}m" for p in hits]}
    return _run(job, timeout=60)


def t_chat(message):
    def job():
        bot = SESSION.require()
        replies = []
        bot.on("chat", lambda s, m, k: replies.append(f"{s}: {m}" if s else m))
        bot.chat(str(message))
        bot.wait(2.0)
        bot.listeners["chat"].pop()
        return {"sent": message, "server_replies": replies[-5:]}
    return _run(job, timeout=30)


def t_defend():
    return M.t_defend(radius=10, timeout=30)


# Memory tools are handled by the harness itself, not the bot.
NOTE_TOOLS = {"remember", "forget"}


def _num(desc):
    return {"type": "number", "description": desc}


def _str(desc, enum=None):
    d = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    return d


def _tool(name, fn, desc, props=None, required=()):
    return {"name": name, "fn": fn, "description": desc,
            "parameters": {"type": "object", "properties": props or {},
                           "required": list(required)}}


TOOLS = [
    _tool("make_wood_pickaxe", t_make_wood_pickaxe,
          "MACRO. Chop ~6 logs, craft planks, sticks, a crafting table and a "
          "wooden pickaxe. Run first, from bare hands."),
    _tool("make_stone_pickaxe", t_make_stone_pickaxe,
          "MACRO. Needs a wooden pickaxe. Digs down collecting 11 cobble, "
          "crafts a stone pickaxe and a furnace."),
    _tool("make_iron_pickaxe", t_make_iron_pickaxe,
          "MACRO. Needs a stone pickaxe. Tunnels to the nearest visible iron "
          "ore (going deeper if none), mines 3, smelts them, crafts an iron "
          "pickaxe."),
    _tool("get_diamond", t_get_diamond,
          "MACRO. Needs an iron pickaxe. Tunnels to the nearest visible "
          "diamond ore, descending toward y=-55 until one shows up."),
    _tool("dig_to", lambda x, y, z: _run(lambda: {
              "reached": dig_to(SESSION.require(), (int(x), int(y), int(z))),
              "position": list(SESSION.require().node_pos)}, timeout=600),
          "Tunnel a staircase through solid ground to a coordinate, e.g. a "
          "buried ore, ending standing in its space so its drop is collected.",
          {"x": _num("x"), "y": _num("y"), "z": _num("z")}, ["x", "y", "z"]),
    _tool("gather", t_gather,
          "Walk to and dig the nearest `count` blocks of a type, picking up "
          "the drops. Picks the best tool automatically. block is one of "
          "log, stone, coal, iron, diamond, or a full node name.",
          {"block": _str("what to dig"), "count": _num("how many blocks")},
          ["block"]),
    _tool("craft", t_craft,
          "Craft an item by full name. Places/opens a crafting table "
          "automatically when the recipe needs one. Known: "
          + ", ".join(recipes.RECIPES),
          {"item": _str("item name"), "times": _num("repeat the recipe N times")},
          ["item"]),
    _tool("smelt", t_smelt,
          "Smelt items in a furnace (placing one from the inventory if none is "
          "nearby) and collect the output.",
          {"item": _str("item to smelt, full name"),
           "fuel": _str("fuel item, full name, e.g. mcl_core:coal_lump"),
           "count": _num("how many")}, ["item", "fuel"]),
    _tool("mine_down", t_mine_down,
          "Dig a staircase down to a y level. Avoids lava and water, fights "
          "mobs, aborts if HP gets low.",
          {"target_y": _num("y to reach")}, ["target_y"]),
    _tool("tunnel", t_tunnel,
          "Dig a straight 2-high tunnel horizontally at the current height, "
          "to uncover more ore (branch mining). Stops at lava/water/drops.",
          {"direction": _str("compass direction",
                             ["north", "south", "east", "west"]),
           "length": _num("nodes, max ~32")}, ["direction"]),
    _tool("goto", t_goto, "Pathfind and walk to a coordinate.",
          {"x": _num("x"), "y": _num("y"), "z": _num("z")}, ["x", "y", "z"]),
    _tool("dig", t_dig, "Dig one specific block, walking into reach first.",
          {"x": _num("x"), "y": _num("y"), "z": _num("z")}, ["x", "y", "z"]),
    _tool("find", t_find,
          "List positions of a block type anywhere in the loaded map.",
          {"block": _str("alias or node name"), "limit": _num("max results")},
          ["block"]),
    _tool("defend", t_defend, "Fight hostile mobs within 10 nodes."),
    _tool("chat", t_chat,
          "Send a chat message or server command (e.g. /privs) and return "
          "what the server says back.",
          {"message": _str("text or /command")}, ["message"]),
    _tool("remember", None,
          "Save a short note to long-term memory (kept across every turn).",
          {"note": _str("the fact worth keeping")}, ["note"]),
    _tool("forget", None, "Delete a memory note by its number.",
          {"index": _num("note number")}, ["index"]),
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# LLM client (OpenAI-compatible chat completions, stdlib only)
# --------------------------------------------------------------------------

class LLM:
    def __init__(self):
        self.base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self.key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "")
        if not (self.base and self.model):
            raise SystemExit("set LLM_BASE_URL and LLM_MODEL (see .env.example)")
        self.prompt_tokens = self.completion_tokens = self.calls = 0

    def chat(self, messages, tools, retries=4):
        body = {"model": self.model, "messages": messages, "temperature": 0.3,
                "tools": [{"type": "function", "function": {
                    "name": t["name"], "description": t["description"],
                    "parameters": t["parameters"]}} for t in tools],
                "tool_choice": "auto"}
        req = urllib.request.Request(
            self.base + "/chat/completions", data=json.dumps(body).encode(),
            # Cloudflare in front of the gateway rejects urllib's default
            # User-Agent with error 1010
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}",
                     "User-Agent": "luanti-agent/1.0"})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.loads(r.read())
                break
            except (urllib.error.URLError, TimeoutError) as e:
                detail = e.read().decode()[:300] if hasattr(e, "read") else e
                print(f"   [llm] error ({detail}); retry {attempt + 1}")
                time.sleep(3 * (attempt + 1))
        else:
            raise SystemExit("LLM endpoint keeps failing")
        usage = data.get("usage") or {}
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.calls += 1
        return data["choices"][0]["message"], usage


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

SYSTEM = """You control a player in Luanti running MineClone2 (a Minecraft clone).
GOAL: get a diamond (mcl_core:diamond) into your inventory.

You act only through tools. Each turn you get a fresh observation plus your
memory; there is no other history, so record anything you will need later
with `remember`.

Useful facts about MineClone2:
- Logs -> planks -> sticks; 4 planks -> crafting table. Each tree species
  has its own planks (spruce log -> mcl_core:sprucewood); craft
  "mcl_core:wood" to turn whatever logs you hold into planks, and recipes
  use whichever planks you have.
- Stone needs a pickaxe; mining stone gives cobble. Diamond ore needs an
  IRON pickaxe or better, or it drops nothing.
- Iron ore must be smelted into mcl_core:iron_ingot (8 cobble -> furnace,
  coal_lump or planks as fuel). 3 ingots + 2 sticks -> iron pickaxe.
- Diamond ore is found roughly between y=-59 and y=-50. The map only streams
  in around you, so ore far below is invisible until you go deeper.
- You can see ore through walls ("Visible ore" in the observation).
- Keep HP above 8. Avoid lava.

THIS IS A SPEEDRUN. The fastest route is the four MACRO tools in order:
make_wood_pickaxe -> make_stone_pickaxe -> make_iron_pickaxe -> get_diamond.
Skip any whose result you already hold. Only fall back to the fine-grained
tools when a macro fails, then go back to the macros. At night the surface
fills with mobs: getting underground is safer.

You may call up to 5 tools in one reply; they run in order, and if one
fails the rest are skipped. Batch steps you are confident about (e.g. craft
planks, sticks, table and pickaxe together) -- every reply costs ~10 seconds
of game time. Prefer the coarse tools (gather, craft, smelt, mine_down,
tunnel). A reflex fights mobs that come within 5 nodes while you think.
Think briefly about what the observation tells you and which milestone is
next."""

MAX_CALLS = 5


class Memory:
    """What survives between turns: notes the model keeps, and a short log."""

    def __init__(self, max_log=12):
        self.notes = []
        self.log = []
        self.max_log = max_log

    def record(self, step, name, args, result):
        text = json.dumps(result, default=str)
        if len(text) > 260:
            text = text[:257] + "..."
        arg = ", ".join(f"{k}={v}" for k, v in args.items())
        self.log.append(f"#{step} {name}({arg}) -> {text}")
        self.log = self.log[-self.max_log:]

    def render(self):
        notes = "\n".join(f"  [{i}] {n}" for i, n in enumerate(self.notes))
        return ("MEMORY NOTES:\n" + (notes or "  (none)") +
                "\n\nRECENT ACTIONS (oldest first):\n" +
                ("\n".join("  " + l for l in self.log) or "  (none yet)"))


def has_diamond():
    def job():
        bot = SESSION.require()
        return bot.inventory is not None and bot.inventory.count(DIAMOND_ITEM) > 0
    return _run(job, timeout=30)


def parse_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="connect, print one observation and the privileges, exit")
    ap.add_argument("--max-steps", type=int, default=200)
    a = ap.parse_args()

    print("connecting to", os.environ.get("LUANTI_HOST"), "as",
          os.environ.get("LUANTI_USER"))
    # Keep trying for ~5 minutes: the server may still be booting (start the
    # agent the moment you press restart -- the speedrun clock is running),
    # or a killed run may have left a ghost player that has not timed out.
    for attempt in range(60):
        try:
            info = M.t_connect(wait_for_map=8)
            break
        except Exception as e:
            if attempt == 59 or "password" in str(e).lower():
                raise
            print(f"   not in yet ({str(e)[:80]}); retrying in 5s")
            time.sleep(5)
    print("connected:", info)

    def setup():
        bot = SESSION.require()
        # Luanti's anticheat tolerates a little over walking speed; 5.5 is
        # about VoxeLibre's sprint and has not been rubber-banded.
        bot.walk_speed = float(os.environ.get("BOT_SPEED", 5.5))
        bot.on("death", on_death_factory(bot))
    _run(setup)
    SESSION.idle_hook = reflex
    print(_run(lambda: observe(SESSION.require())))
    if a.check:
        M.t_disconnect()
        return

    llm = LLM()
    mem = Memory()
    os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
    log_path = os.path.join(HERE, "runs", time.strftime("run-%Y%m%d-%H%M%S.jsonl"))
    log = open(log_path, "w", encoding="utf-8")
    t0 = time.time()
    tool_time = llm_time = 0.0
    print("logging to", log_path)

    stop_file = os.path.join(HERE, "runs", "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    for step in range(1, a.max_steps + 1):
        # Touch runs/STOP to end the run with a clean logout; killing the
        # process leaves a ghost player that blocks reconnecting for a minute.
        if os.path.exists(stop_file):
            os.remove(stop_file)
            print("\nstop requested")
            break
        obs = _run(lambda: observe(SESSION.require()))
        elapsed = time.time() - t0
        user = (f"{mem.render()}\n\nOBSERVATION (step {step}, "
                f"{elapsed / 60:.1f} min in):\n{obs}\n\nChoose your next tool call.")
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user}]

        t = time.time()
        msg, usage = llm.chat(messages, TOOLS)
        llm_time += time.time() - t
        thought = re.sub(r"<think>.*?</think>", "", msg.get("content") or "",
                         flags=re.S).strip()
        calls = msg.get("tool_calls") or []
        print(f"\n--- step {step}  [{elapsed:.0f}s, tokens in/out "
              f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')}]")
        if thought:
            print("   thinks:", thought[:400])

        if not calls:
            mem.record(step, "no_tool_call", {}, {"error": "you must call a tool"})
            continue

        # Up to MAX_CALLS actions per turn, run in order. Model latency is the
        # bottleneck, so letting it queue "planks, sticks, table, pickaxe" in
        # one reply saves several round trips. A failure skips the rest,
        # since they were planned assuming it would succeed.
        for call in calls[:MAX_CALLS]:
            name = call["function"]["name"]
            args = parse_args(call["function"].get("arguments"))
            print(f"   -> {name}({args})")
            t = time.time()
            try:
                if name == "remember":
                    mem.notes.append(str(args.get("note", ""))[:200])
                    mem.notes = mem.notes[-15:]
                    result = {"saved": True}
                elif name == "forget":
                    i = int(args.get("index", -1))
                    result = {"removed": mem.notes.pop(i)} \
                        if 0 <= i < len(mem.notes) else {"error": "no such note"}
                elif name in TOOLS_BY_NAME:
                    result = TOOLS_BY_NAME[name]["fn"](**args)
                else:
                    result = {"error": f"unknown tool {name}"}
            except TypeError as e:
                result = {"error": f"bad arguments: {e}"}
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            tool_time += time.time() - t
            print("   <-", json.dumps(result, default=str)[:400])
            mem.record(step, name, args, result)
            log.write(json.dumps({
                "step": step, "t": round(time.time() - t0, 1), "usage": usage,
                "thought": thought, "tool": name, "args": args,
                "result": result, "observation": obs}, default=str) + "\n")
            log.flush()
            if (isinstance(result, dict) and "error" in result) \
                    or _run(lambda: SESSION.require().dead):
                break
            if _run(lambda: SESSION.require().inventory.count(DIAMOND_ITEM)):
                break

        if has_diamond():
            total = time.time() - t0
            print(f"\n*** DIAMOND OBTAINED at step {step} after {total / 60:.1f} min ***")

            # The class portal only noticed a diamond picked up by punching
            # its item entity once the player moved it and stayed online, so
            # nudge the inventory and linger before logging out.
            def announce():
                bot = SESSION.require()
                s = bot.inventory.first_slot(DIAMOND_ITEM)
                free = bot.inventory.free_slot("main")
                if s is not None and free is not None:
                    bot.move_item("main", s, "main", free, 1)
                bot.wait(15)
            _run(announce, timeout=60)
            break
    else:
        print("\nstep budget exhausted without a diamond")

    total = time.time() - t0
    summary = {"steps": step, "minutes": round(total / 60, 1),
               "llm_calls": llm.calls, "prompt_tokens": llm.prompt_tokens,
               "completion_tokens": llm.completion_tokens,
               "llm_seconds": round(llm_time), "tool_seconds": round(tool_time),
               "diamond": has_diamond(), "notes": mem.notes}
    print(json.dumps(summary, indent=2))
    log.write(json.dumps({"summary": summary}) + "\n")
    log.close()
    print(_run(lambda: observe(SESSION.require())))
    M.t_disconnect()


if __name__ == "__main__":
    main()
