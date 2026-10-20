"""How fast will the server let the bot move before anticheat pulls it back?

Walks a short reachable route at increasing speeds and counts the
TOCLIENT_MOVE_PLAYER corrections the server sends.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent                                    # noqa: F401  (loads .env)
from luantibot import Bot

bot = Bot(os.environ["LUANTI_HOST"], int(os.environ["LUANTI_PORT"]),
          os.environ["LUANTI_USER"], os.environ["LUANTI_PASSWORD"])
corrections = []
orig = bot.client._h_34
def counted(r):
    orig(r)
    corrections.append(tuple(round(v, 1) for v in bot.client.position))
bot.client._h_34 = counted
# the dispatcher may hold a bound reference; patch the table too if present
for attr in ("handlers", "_handlers"):
    table = getattr(bot.client, attr, None)
    if isinstance(table, dict) and 0x34 in table:
        table[0x34] = counted

bot.connect(timeout=90)
try:
    bot.wait(10)
    bot.settle()
    home = bot.node_pos
    reach = bot.reachable(max_expanded=6000)
    far = [p for p in reach if p[1] == home[1]
           and 10 <= abs(p[0]-home[0]) + abs(p[2]-home[2]) <= 16]
    print("home", home, "candidates", len(far))
    if not far:
        sys.exit("no flat route to test on")
    target = far[0]
    for speed in (float(s) for s in sys.argv[1:] or (5.5, 8, 12, 20)):
        bot.walk_speed = speed
        n = len(corrections)
        t = time.monotonic()
        ok = bot.goto(target, timeout=30)
        dt = time.monotonic() - t
        bot.wait(1.5)
        there = bot.node_pos
        print(f"speed {speed:5.1f}: arrived={ok} in {dt:4.1f}s, "
              f"corrections={len(corrections)-n} {corrections[n:n+3]}")
        target, home = home, there
finally:
    bot.disconnect()
