"""Locate an ore by descending through the world and scanning what streams in.

    python3 examples/07_find_ore.py mcl_core:stone_with_diamond

A Luanti server only sends map blocks near the player, so ore that is deeper
than the send radius is simply not knowable yet. This walks the bot to the
deepest place it can actually reach, waits for the map around it to arrive,
scans every streamed block, and repeats -- following caves down.

No mining is required: ore is an ordinary node, so it shows up in block data as
soon as the server sends that block.
"""
import os, sys, time, math, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

ORE = sys.argv[1] if len(sys.argv) > 1 else "mcl_core:stone_with_diamond"
BUDGET = float(os.environ.get("BUDGET", 600))

bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect(timeout=90)
bot.wait(15)

print(f"looking for {ORE}")
print(f"start {bot.node_pos}, {len(bot.world.blocks)} blocks, "
      f"lowest loaded y = {bot.world.lowest_loaded_y()}\n")

deadline = time.time() + BUDGET
tried = set()
best_depth = bot.node_pos[1]
stuck_rounds = 0
DIAMOND_BAND = (-62, -50)

while time.time() < deadline:
    hits = bot.scan(ORE)
    if hits:
        print(f"\n*** FOUND {len(hits)} x {ORE} ***")
        for pos in hits[:10]:
            print(f"    {pos}   {math.dist(pos, bot.position):.1f} nodes away")
        break

    if bot.dead:
        print("    died; waiting for respawn")
        bot.wait_for(lambda: not bot.dead, timeout=20)
        bot.wait(5)
        tried.clear()
        continue

    here = bot.node_pos
    low = bot.world.lowest_loaded_y()
    reach = bot.reachable(max_expanded=30000)
    if not reach:
        # usually means we are mid-fall or the blocks under us have not arrived
        stuck_rounds += 1
        print(f"    no reachable nodes (round {stuck_rounds}); waiting for map")
        bot.wait(5)
        if stuck_rounds > 6:
            print("    giving up on this spot"); break
        continue
    stuck_rounds = 0

    deepest = min(reach, key=lambda p: p[1])
    print(f"at y={here[1]:4d}  blocks={len(bot.world.blocks):5d}  lowest_y={low:4d}  "
          f"reachable={len(reach):5d}  deepest={deepest[1]:4d} {deepest}  hp={bot.hp}")

    # good enough: the diamond band is inside the streamed region
    if low is not None and low <= DIAMOND_BAND[0]:
        print("    diamond band is now within streamed map")

    candidates = [p for p in reach if p[1] < here[1] - 1 and p not in tried]
    if candidates:
        # deepest first, but break ties by closeness so we do not cross the map
        candidates.sort(key=lambda p: (p[1], math.dist(p, here)))
        target = candidates[0]
        tried.add(target)
        print(f"    descending to {target}")
        bot.goto(target, tolerance=1, timeout=90)
    else:
        far = max(reach, key=lambda p: (p[0]-here[0])**2 + (p[2]-here[2])**2)
        if far in tried:
            print("    nowhere new to go"); break
        tried.add(far)
        print(f"    no way down; exploring outward to {far}")
        bot.goto(far, tolerance=2, timeout=90)

    best_depth = min(best_depth, bot.node_pos[1])
    bot.wait(6)
else:
    print("\nbudget exhausted")

print(f"\ndeepest point reached: y={best_depth}")
print(f"lowest block streamed: y={bot.world.lowest_loaded_y()}")
print(f"final position {bot.node_pos}, hp {bot.hp}, "
      f"{len(bot.world.blocks)} blocks known")
census = collections.Counter()
for name in ("mcl_core:stone_with_diamond", "mcl_core:stone_with_gold",
             "mcl_core:stone_with_redstone", "mcl_core:stone_with_lapis",
             "mcl_core:stone_with_iron", "mcl_core:stone_with_coal"):
    census[name] = len(bot.scan(name))
print("ore seen so far:", dict(census))
bot.disconnect()
