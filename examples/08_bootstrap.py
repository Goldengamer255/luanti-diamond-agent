"""Bootstrap tools from bare hands: log -> planks -> sticks -> table -> pickaxe.

Stone in MineClone2 is `pickaxey`, not `handy`, so a freshly spawned bot cannot
mine at all. This chops wood by hand, crafts its way to a wooden pickaxe, and
then digs stone -- which proves the whole inventory/crafting path.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot, BotError, recipes

LOG, WOOD, STICK = "mcl_core:tree", "mcl_core:wood", "mcl_core:stick"
TABLE = "mcl_crafting_table:crafting_table"

bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect(timeout=90)
try:
    bot.wait(15)
    print("spawned at", bot.node_pos, "holding:", bot.inventory.non_empty())
    bot.clear_craft_grid()

    # 1. chop wood by hand -- logs are `handy`, so no tool is needed
    while bot.inventory.count(LOG) < 4:
        logs = [p for p in bot.scan(list(recipes.LOGS)) if p][:40]
        if not logs:
            raise SystemExit("no trees in range")
        got = False
        for target in logs:
            try:
                if bot.dig(target, timeout=30):
                    got = True
                    bot.wait(2)          # let the drop be collected
                    print(f"  chopped {target}, logs = {bot.inventory.count(LOG)}")
                    break
            except BotError:
                continue
        if not got:
            raise SystemExit("could not reach any tree")

    # 2. planks, sticks, crafting table -- all fit the 2x2 grid
    print("planks :", bot.craft_item(WOOD))
    print("sticks :", bot.craft_item(STICK))
    if bot.inventory.count(WOOD) < 3:
        print("planks :", bot.craft_item(WOOD))
    print("table  :", bot.craft_item(TABLE))
    while bot.inventory.count(WOOD) < 3:
        print("planks :", bot.craft_item(WOOD))

    # 3. place the table and open it -- that is what grows the grid to 3x3
    px, py, pz = bot.node_pos
    spot = None
    for dx in range(-2, 3):
        for dy in range(-1, 2):
            for dz in range(-2, 3):
                c = (px+dx, py+dy, pz+dz)
                if (dx or dy or dz) and bot.world.is_free(c) \
                        and bot.world.is_walkable((c[0], c[1]-1, c[2])) \
                        and bot.in_reach(c):
                    spot = spot or c
    bot.place_block(spot, item=TABLE)
    print("placed table at", spot, "->", bot.block_at(spot))
    print("craft grid is now", bot.open_node(spot), "slots")

    # 4. the pickaxe
    pick = bot.craft_item("mcl_tools:pick_wood")
    print("\n*** crafted", pick, "***")

    # 5. prove it: stone was unbreakable a minute ago
    stone = next((p for p in bot.scan("mcl_core:stone") if bot.in_reach(p)), None)
    if stone:
        bot.select_slot(bot.inventory.first_slot("mcl_tools:pick_wood"))
        bot.wait(1)
        print("digging stone at", stone, "->", bot.dig(stone, timeout=25),
              "| node is now", bot.block_at(stone))
    print("\nfinal inventory:", bot.inventory.non_empty())
finally:
    bot.disconnect()
