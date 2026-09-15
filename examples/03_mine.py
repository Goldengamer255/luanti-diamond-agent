"""Find the nearest block of a given type, walk to it, and mine it.

    python3 examples/03_mine.py mcl_core:tree
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

wanted = sys.argv[1] if len(sys.argv) > 1 else "mcl_core:tree"
bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect()
bot.wait(15)

targets = bot.find_blocks(wanted, radius=24)
print(f"found {len(targets)} x {wanted}")
if not targets:
    bot.disconnect(); raise SystemExit("none nearby")

target = targets[0]
print("nearest:", target)
# dig() walks into range by itself
print("digging", target, "from", bot.node_pos)
dug = bot.dig(target, timeout=45)
print("at", bot.node_pos, "- dug:", dug, "->", bot.block_at(target))
bot.wait(3)
print("inventory:", bot.inventory.non_empty())
bot.disconnect()
