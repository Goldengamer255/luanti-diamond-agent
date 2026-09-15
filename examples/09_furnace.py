"""Smelt ore in a furnace, and read what is inside nearby containers.

Node metadata carries a node's inventory, so a bot can see chest loot and
furnace contents without opening anything -- handy for deciding whether a
container is worth walking to.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

FURNACE = "mcl_furnaces:furnace"
ORE     = os.environ.get("ORE", "mcl_core:stone_with_iron")
FUEL    = os.environ.get("FUEL", "mcl_core:coal_lump")

bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect(timeout=90)
try:
    bot.wait(20)

    # what containers can we see from here, without opening them?
    print("containers in range:")
    found = 0
    for blk in bot.world.blocks.values():
        for idx, inv in blk.inventories.items():
            z, rem = divmod(idx, 256)
            y, x = divmod(rem, 16)
            pos = (blk.pos[0]*16 + x, blk.pos[1]*16 + y, blk.pos[2]*16 + z)
            items = bot.node_items(pos)
            if items:
                print(f"   {pos} {bot.block_at(pos)}: {items}")
                found += 1
            if found >= 8:
                break
    if not found:
        print("   (none with contents nearby)")

    if bot.inventory.count(ORE) == 0:
        print(f"\nno {ORE} to smelt; skipping")
        raise SystemExit

    if bot.inventory.count(FURNACE) == 0:
        print("\nno furnace in inventory; craft one from 8 cobble first")
        raise SystemExit
    pos = bot.find_placement_spot()
    if pos is None:
        print("\nnowhere to put a furnace nearby")
        raise SystemExit
    print(f"\nplacing furnace at {pos}: {bot.place_block(pos, item=FURNACE)}")

    n = min(3, bot.inventory.count(ORE))
    print(f"smelting {n} x {ORE} with {FUEL} ...")
    out = bot.smelt(pos, ORE, FUEL, count=n, timeout=120)
    print("got:", out)
    print("furnace now holds:", bot.node_items(pos))
finally:
    bot.disconnect()
