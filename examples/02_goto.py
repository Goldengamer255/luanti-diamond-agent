"""Pathfind to a coordinate and walk there.

    python3 examples/02_goto.py 120 12 -40
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

goal = tuple(int(v) for v in sys.argv[1:4])
bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect()
bot.wait(10)
print("start:", bot.node_pos, "->", goal)

result = bot.path_to(goal)
print("plan:", result)
if result.path:
    print("  route:", " ".join(s.action for s in result.path[1:12]), "...")

print("walking ->", bot.goto(goal, tolerance=1, timeout=180))
print("arrived at", bot.node_pos, "hp", bot.hp)
bot.disconnect()
