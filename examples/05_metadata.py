"""Dump the per-node metadata the server publishes for nearby blocks.

Node metadata is how a server attaches extra data to individual blocks (chest
contents, signs, or whatever a custom server chooses to expose). Only variables
the server marks non-private are sent to clients.
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect()
bot.wait(20)

entries = list(bot.world.all_metadata())
print(f"{len(entries)} nodes carry metadata across {len(bot.world.blocks)} blocks\n")

keys = collections.Counter()
for _pos, fields in entries:
    keys.update(fields)
print("metadata keys by frequency:")
for key, n in keys.most_common(25):
    print(f"   {n:5d}  {key}")

print("\nsamples:")
for pos, fields in entries[:10]:
    print(f"  {pos}  {bot.block_at(pos)}")
    for k, v in list(fields.items())[:6]:
        text = v.decode('utf-8', 'replace') if isinstance(v, bytes) else str(v)
        print(f"       {k} = {text[:70]!r}")
bot.disconnect()
