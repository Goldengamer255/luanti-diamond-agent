"""Mine down to the diamond layer and bring some back.

Ties the pieces together: pick the best tool for each node, cut a staircase
down, fight off whatever interrupts, and scan for ore as the map streams in.

In MineClone2 diamond spawns between y=-62 and y=-50, and a server only sends
map blocks within ~3 map blocks below you, so the ore is invisible until the
bot is physically deep. Mining an iron pickaxe or better is required to collect
it -- a wooden or stone pick will break the node but drop nothing.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

ORE = os.environ.get("ORE", "mcl_core:stone_with_diamond")
TARGET_Y = int(os.environ.get("TARGET_Y", -52))

bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.on("mining_step", lambda pos, hp, mobs:
       print(f"   y={pos[1]:4d} hp={hp:2d} {('mobs: ' + ', '.join(mobs)) if mobs else ''}"))
bot.on("mining_threat", lambda mobs, hp: print(f"   !! {mobs} (hp {hp})"))
bot.on("mining_aborted", lambda why: print(f"   aborted: {why}"))
bot.on("death", lambda: print("   *** died ***"))

bot.connect(timeout=90)
try:
    bot.wait(15)
    print("start", bot.node_pos, "hp", bot.hp)

    pick = next((n for n in ("mcl_tools:pick_diamond", "mcl_tools:pick_iron")
                 if bot.inventory.first_slot(n) is not None), None)
    if pick is None:
        print("no iron or diamond pickaxe: diamond ore will break but drop nothing")
    else:
        print("using", pick)

    hits = bot.scan(ORE)
    if not hits:
        print(f"\ndigging down to y={TARGET_Y} ...")
        t0 = time.time()
        reached = bot.mine_to_depth(TARGET_Y, timeout=1800)
        print(f"reached={reached} at {bot.node_pos} in {time.time()-t0:.0f}s")
        hits = bot.scan(ORE)

    print(f"\n{len(hits)} x {ORE} visible")
    for p in hits[:10]:
        print("   ", p)

    for target in hits[:5]:
        if bot.hp < 8:
            print("too hurt to keep mining")
            break
        print(f"mining {target} ...", end=" ")
        print("ok" if bot.dig(target, timeout=60) else "failed")
        bot.wait(2)

    print("\ncarrying:", {k: v for k, v in bot.item_counts().items()
                          if "diamond" in k or "iron" in k})
finally:
    bot.disconnect()
