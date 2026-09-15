"""Connect, stream the map, and report what the bot can see."""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

HOST = os.environ.get("LUANTI_HOST", "127.0.0.1")
PORT = int(os.environ.get("LUANTI_PORT", 30000))

bot = Bot(HOST, PORT, os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.on("chat", lambda sender, msg, kind: print(f"<{sender}> {msg}"))
bot.connect()
print("logged in as", bot.name, "at", bot.node_pos)

bot.wait(15)                       # let the server stream the map in

print(f"\nmap: {len(bot.world.blocks)} blocks loaded")
print(f"node types known: {len(bot.world.nodedefs.by_id)}")
print(f"hp: {bot.hp}   privileges: {sorted(bot.client.privileges)}")

x, y, z = bot.node_pos
print("\ncolumn under the bot:")
for dy in range(2, -5, -1):
    p = (x, y + dy, z)
    print(f"   y={p[1]:4d}  {bot.block_at(p)}")

kinds = collections.Counter(e.name for e in bot.entities.values())
print("\nnearby entities:")
for name, n in kinds.most_common(10):
    print(f"   {n:3d}  {name}")

bot.disconnect()
