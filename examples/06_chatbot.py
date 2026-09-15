"""A bot you drive from in-game chat.

Say these in chat and the bot obeys:

    bot come                 walk to whoever spoke
    bot goto <x> <y> <z>     walk to a coordinate
    bot where                report its position
    bot dig <x> <y> <z>      mine a node
    bot find <nodename>      report the nearest matching blocks
    bot look                 describe its surroundings
    bot stop                 cancel the current task
"""
import os, sys, threading, queue
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luantibot import Bot

bot = Bot(os.environ.get("LUANTI_HOST", "127.0.0.1"),
          int(os.environ.get("LUANTI_PORT", 30000)),
          os.environ.get("LUANTI_USER", "bot"),
          os.environ.get("LUANTI_PASSWORD", ""))

commands = queue.Queue()


def on_chat(sender, message, kind):
    text = message.strip()
    # server relays player chat as "<name> text"
    if text.startswith("<") and ">" in text:
        speaker, text = text[1:].split(">", 1)
        sender, text = speaker.strip(), text.strip()
    if text.lower().startswith("bot "):
        commands.put((sender, text[4:].strip()))


bot.on("chat", on_chat)
bot.connect()
bot.wait(5)
bot.chat("bot online - say 'bot come' or 'bot goto x y z'")
print("connected as", bot.name, "at", bot.node_pos)


def handle(sender, cmd):
    parts = cmd.split()
    if not parts:
        return
    verb, args = parts[0].lower(), parts[1:]

    if verb == "where":
        bot.chat(f"I am at {bot.node_pos}, hp {bot.hp}")

    elif verb == "come":
        target = bot.player(sender)
        if target is None:
            bot.chat(f"I cannot see {sender}")
            return
        bot.chat(f"coming to {sender}")
        bot.goto(target.node_pos, tolerance=2, timeout=120)
        bot.chat(f"arrived at {bot.node_pos}")

    elif verb == "goto" and len(args) == 3:
        goal = tuple(int(v) for v in args)
        bot.chat(f"walking to {goal}")
        ok = bot.goto(goal, tolerance=1, timeout=180)
        bot.chat(f"{'arrived' if ok else 'could not reach'} - now at {bot.node_pos}")

    elif verb == "dig" and len(args) == 3:
        pos = tuple(int(v) for v in args)
        bot.chat(f"digging {bot.block_at(pos)} at {pos}")
        bot.chat("dug it" if bot.dig(pos, timeout=30) else "could not dig that")

    elif verb == "find" and args:
        hits = bot.find_blocks(args[0], radius=20)
        bot.chat(f"{len(hits)} x {args[0]}" +
                 (f", nearest {hits[0]}" if hits else " nearby"))

    elif verb == "look":
        p = bot.node_pos
        bot.chat(f"standing on {bot.block_at((p[0], p[1]-1, p[2]))}, "
                 f"{len(bot.world.blocks)} blocks loaded, "
                 f"{len(bot.players_nearby())} players near")

    else:
        bot.chat("commands: come, goto x y z, where, dig x y z, find <node>, look")


def worker():
    while True:
        sender, cmd = commands.get()
        try:
            handle(sender, cmd)
        except Exception as e:
            bot.chat(f"error: {e}")


threading.Thread(target=worker, daemon=True).start()
try:
    bot.run_forever()
except KeyboardInterrupt:
    bot.disconnect()
