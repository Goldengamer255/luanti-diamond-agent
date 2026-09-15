# luantibot

A Mineflayer-style bot library for **Luanti** (formerly Minetest) servers.
Pure Python, standard library only.

```python
from luantibot import Bot

bot = Bot("play.example.com", 30000, "username", "password")
bot.connect()

bot.goto((120, 12, -40))                  # A* pathfinding + walking
bot.dig(bot.find_blocks("mcl_core:tree", radius=20)[0])
bot.chat("hello")
```

## Why this is not a client-side mod

The original plan was a Luanti **client-side mod (CSM)**. That cannot work, and
it is worth being precise about why, because it is a deliberate design decision
in Luanti rather than a gap.

Checked against the Luanti 5.16.1 client binary directly:

* The `LocalPlayer` table exposes **only getters** — `get_pos`, `get_velocity`,
  `get_control`, `is_touching_ground`, `is_in_liquid`, `get_movement_speed`.
  There is no `set_pos`, no `set_control`, and no `set_keypress`.
* `dig_node`, `place_node` and `punch_node` exist only in the **server**
  environment, not the client one.
* The CSM API is perception plus chat: `get_node_or_nil`, `find_nodes_in_area`,
  `raycast`, `line_of_sight`, `get_node_light`, `send_chat_message`,
  `mod_channel_join`.
* SSCSM (server-sent CSM) exists in this build but is an unfinished skeleton and
  is sandboxed identically.

So a CSM can *see* and *plan*, but it can never *act*. This is anti-cheat by
design.

The key insight is that **Mineflayer is not a Minecraft mod either** — it is a
standalone client that speaks the network protocol and sends its own position
packets. This library does the same thing for Luanti, which is why it can
actually move.

In Luanti the client is authoritative over its own position: the server does not
simulate walking from key presses, it only sanity-checks how far you moved. So
the bot moves by streaming `TOSERVER_PLAYERPOS` with updated coordinates at a
believable speed.

## Install

Nothing to install. Python 3.8+ and a system `libzstd` (loaded through
`ctypes`), which any machine that runs Luanti already has.

```bash
git clone <this repo> && cd luanti-mod
LUANTI_HOST=play.example.com LUANTI_PORT=30000 \
LUANTI_USER=name LUANTI_PASSWORD=secret \
python3 examples/01_explore.py
```

## API

### Connecting

```python
bot = Bot(host, port=30000, username="bot", password="", verbose=False,
          max_drop=3, allow_swim=True, avoid_damage=True, can_dig=False)
bot.connect(timeout=60)
bot.disconnect()
```

All three Luanti authentication mechanisms are supported and are chosen
automatically from the server's `TOCLIENT_HELLO`:

| Mechanism | When | Status |
|---|---|---|
| `FIRST_SRP` | account does not exist yet — registers one | verified |
| `SRP` | normal modern account | verified |
| `LEGACY_PASSWORD` | account stored as `base64(sha1(name+password))` | verified |

### State

```python
bot.position        # feet position, floats
bot.node_pos        # the node the feet occupy
bot.hp              # 0..20
bot.dead            # respawns automatically
bot.inventory       # merged across incremental updates
bot.world           # the map model
bot.entities        # players, mobs, item drops
```

### Movement

```python
bot.goto((x, y, z), tolerance=0, timeout=120)   # plan + walk, replanning
bot.path_to((x, y, z))                          # just the plan
bot.walk_path(result.path)                      # walk a plan
bot.follow("PlayerName", distance=2)            # chase a player
bot.look_at((x, y, z))
```

`path_to` returns a `PathResult` with `.path` (a list of `Step`, each carrying
`.pos` and an `.action` of `walk`/`jump`/`fall`/`climb_up`/`climb_down`/`dig`),
`.reached_goal` and `.expanded`. When the goal is unreachable it still returns
the route to the closest node it found, so a bot can make progress on a
partially-streamed map instead of refusing to move.

### World

```python
bot.block_at((x, y, z))                  # 'mcl_core:stone'
bot.find_blocks("mcl_core:tree", radius=24)
bot.meta_at((x, y, z))                   # per-node metadata from the server
bot.world.all_metadata()                 # every node carrying metadata
bot.world.is_walkable(p) / is_climbable(p) / is_liquid(p) / is_dangerous(p)
```

Node properties come from the server's own `TOCLIENT_NODEDEF`, so the bot knows
what is solid, climbable, liquid or damaging on **any** game — MineClone2,
Minetest Game or a custom one — without hardcoded block lists.

### Actions

```python
bot.dig((x, y, z), timeout=12)
bot.place_block((x, y, z), item="mcl_core:cobble")   # places at that node
bot.place(against=(x, y, z), face=(0, 1, 0))         # low-level
bot.select_slot(0)
bot.use()
bot.chat("hi")
```

Note on placement: Luanti builds *against* an existing node, filling the node
you point at when that node is replaceable (air, grass, water) and otherwise the
one on the pointed face. `place_block` handles that for you and puts the block
where you asked.

### Inventory and crafting

```python
bot.inventory                       # merged across incremental updates
bot.inventory.non_empty()           # [(slot, ItemStack), ...]
bot.inventory.count("mcl_core:wood")
bot.move_item("main", 0, "craft", 0, count=1)
bot.drop_item("main", 3, count=5)

bot.craft([[LOG]])                                  # 2x2, the player's own grid
bot.open_node(table_pos)                            # right-click: grid becomes 3x3
bot.craft([[W, W, W], [None, S, None], [None, S, None]])
bot.craft_item("mcl_tools:pick_wood")               # via the built-in recipe book
```

Two things about Luanti crafting are easy to get wrong:

* `craftpreview` is **display only**. Moving items out of it does nothing. You
  put ingredients in `craft`, the server fills `craftpreview`, then you send a
  **`Craft`** action and the output appears in `craftresult` to be moved to
  `main`. `bot.craft()` does the whole dance.
* A crafting table has **no inventory of its own**. Right-clicking it makes the
  server resize *the player's* `craft` list from 4 to 9 slots, so 3x3 recipes
  need `open_node()` first.

Luanti never sends craft recipes to clients -- the server computes the result
and the client only ever sees it appear in `craftpreview`. So `luantibot/recipes.py`
carries the handful needed to get from bare hands to an iron pickaxe; for
anything else pass the grid shape to `craft()` yourself, or put candidate items
in the grid and read `craftpreview` to discover what they make.

### Containers and smelting

```python
bot.node_inventory((x, y, z))        # a chest/furnace inventory, or None
bot.node_items((x, y, z), "dst")     # {item name: count}
bot.put_into_node(pos, item, "src", count=3)
bot.take_from_node(pos, "dst")       # returns what actually arrived
bot.smelt(furnace_pos, "mcl_core:stone_with_iron", "mcl_core:coal_lump", count=3)
```

Node metadata carries the node's **inventory**, serialised as text after its
string variables, so chest loot and furnace contents are visible without
opening anything. `smelt()` loads `src` and `fuel`, watches `dst`, and collects
the output:

```python
>>> bot.smelt(pos, "mcl_core:stone_with_iron", "mcl_core:coal_lump", count=3)
{'mcl_core:iron_ingot': 3}
```

Two traps here. Inventory-action helpers default `to_inv` to `from_inv`, so
moving *out* of a node needs `to_inv="current_player"` spelled out or the items
go back where they came from. And the server does not reliably echo `ADDNODE`
to whoever placed a node -- real clients predict placement locally via
`node_placement_prediction` -- so `place_block()` mirrors that prediction rather
than trusting the map to update itself.

### Events

```python
bot.on("chat",  lambda sender, message, kind: ...)
bot.on("move",  lambda node: ...)
bot.on("dig",   lambda pos: ...)
bot.on("death", lambda: ...)
bot.on("block", lambda blockpos: ...)
```

Drive the bot with `bot.step()`, `bot.wait(seconds)`,
`bot.wait_for(predicate, timeout)` or `bot.run_forever()`.

## Examples

| File | What it does |
|---|---|
| `examples/01_explore.py` | connect, stream the map, report surroundings |
| `examples/02_goto.py` | pathfind and walk to a coordinate |
| `examples/03_mine.py` | find the nearest block of a type and mine it |
| `examples/04_follow.py` | follow a player around |
| `examples/05_metadata.py` | dump the per-node metadata a server publishes |
| `examples/06_chatbot.py` | drive the bot from in-game chat |
| `examples/07_find_ore.py` | descend through caves scanning for an ore |
| `examples/08_bootstrap.py` | bare hands -> logs -> planks -> table -> pickaxe -> mine stone |
| `examples/09_furnace.py` | read nearby container contents, then smelt ore |

## How it is put together

| Module | Responsibility |
|---|---|
| `connection.py` | Luanti's reliable-UDP transport: acks, reordering, split reassembly, retransmission |
| `commands.py` | protocol command ids, generated from the client binary |
| `client.py` | login, packet dispatch, player actions, inventory actions |
| `srp.py` / `auth.py` | SRP-6a and the legacy password hash |
| `nodedef.py` | the server's node registry (`ContentFeatures`) |
| `mapblock.py` / `world.py` | block decoding and the map model |
| `entities.py` | players, mobs and item drops |
| `inventory.py` | the text inventory format, merged incrementally |
| `recipes.py` | the bootstrap recipe book (recipes are never sent to clients) |
| `pathfinder.py` | A* over standable positions |
| `bot.py` | the high-level API |

### On protocol accuracy

Luanti ships no formal protocol specification (`doc/protocol.txt` is a 2011
draft covering only the handshake), so the wire formats here were established
empirically rather than from documentation:

* **Command ids** are extracted from the client binary's own command tables by
  `tools/extract_protocol.py`, which walks the name pointers in the ELF and
  reconstructs the arrays in index order. Regenerate with
  `python3 tools/extract_protocol.py /usr/bin/luanti`.
* **`ContentFeatures` layout** (version 13) was decoded from a live server and
  cross-checked against the game's own Lua node definitions. The parser is
  self-verifying: every blob must be consumed exactly, and any that is not falls
  back to drawtype-derived defaults rather than producing wrong data. On
  MineClone2 all 2601 node definitions parse exactly.
* **The legacy password hash** was checked against Luanti's own
  `core.get_password_hash` output — it is unpadded base64, 27 characters.

## Limitations

* Digging time is not computed from tool capabilities; `dig()` asks the server
  to complete repeatedly until the node changes, which the server rate-limits.
  `TOCLIENT_ITEMDEF` is received but not yet parsed.
* Movement is interpolation, not physics: no momentum, no swimming currents, and
  jumps are straight-line rather than parabolic.
* Formspecs are detected but not parsed. Inventory *actions* work (including
  node inventories via `nodemeta:x,y,z`), and container *contents* are readable
  from node metadata, but the bot cannot read a formspec's layout -- so it must
  be told which lists a station uses.
* `TOCLIENT_ITEMDEF` is received but not parsed, so the bot has no tool
  capabilities: it cannot compute dig times (`dig()` retries until the server
  accepts), pick the best tool for a node, or know which tool tier a node needs.
* There is no combat: `interact(object_id=...)` exists, but nothing targets,
  attacks, or flees. Hostile mobs are a real hazard underground.
* No systematic mining (staircase or strip). Reaching deep ore currently depends
  on finding natural caves.
* The bot never sends `TOSERVER_DAMAGE`, so it takes no fall damage. That is
  effectively cheating, and a server running strict anticheat may object.
* No automatic reconnection, and the API is blocking and single-threaded.
* The pathfinder treats unloaded blocks as impassable, so long routes need the
  map to stream in first; `goto()` replans as more arrives. An unreachable goal
  makes A* explore its whole reachable region before giving up; lower
  `max_expanded` if you want it to give up sooner.
* The server does not send position updates for your *own* player object, so the
  entry for the bot in `bot.entities` stays at its spawn value. Use
  `bot.position` / `bot.node_pos`, which are authoritative.
* `dig()` and `place_block()` walk into range automatically (servers reject
  interaction beyond about 4.5 nodes); pass `approach=False` to opt out.
# luanti-bot
