"""Follow a player around.

    python3 examples/04_follow.py PlayerName
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

who = sys.argv[1]
bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))
bot.connect()
bot.wait(5)
bot.chat(f"following {who}")
print("following", who, "- Ctrl-C to stop")
try:
    bot.follow(who, distance=2, timeout=3600)
except KeyboardInterrupt:
    pass
bot.disconnect()
