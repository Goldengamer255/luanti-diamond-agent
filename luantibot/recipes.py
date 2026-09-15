"""A small recipe book for the MineClone2 / VoxeLibre tool bootstrap.

Luanti never sends craft recipes to clients -- the server computes the result
and the client only sees it appear in `craftpreview`. So a bot either knows a
recipe in advance or discovers it by putting items in the grid and reading what
comes out. These are the handful needed to get from bare hands to an iron
pickaxe; anything else can be crafted with `Bot.craft(rows)` directly.

Each entry is (rows, needs_table). `needs_table` means the recipe is wider than
2x2 and requires standing at a crafting table.
"""

WOOD = "mcl_core:wood"
STICK = "mcl_core:stick"
COBBLE = "mcl_core:cobble"
IRON = "mcl_core:iron_ingot"
LOG = "mcl_core:tree"

RECIPES = {
    WOOD: ([[LOG]], False),
    STICK: ([[WOOD], [WOOD]], False),
    "mcl_crafting_table:crafting_table": ([[WOOD, WOOD], [WOOD, WOOD]], False),

    "mcl_tools:pick_wood":  ([[WOOD, WOOD, WOOD],
                              [None, STICK, None],
                              [None, STICK, None]], True),
    "mcl_tools:pick_stone": ([[COBBLE, COBBLE, COBBLE],
                              [None, STICK, None],
                              [None, STICK, None]], True),
    "mcl_tools:pick_iron":  ([[IRON, IRON, IRON],
                              [None, STICK, None],
                              [None, STICK, None]], True),

    "mcl_tools:axe_wood":   ([[WOOD, WOOD], [WOOD, STICK], [None, STICK]], True),
    "mcl_tools:shovel_wood": ([[WOOD], [STICK], [STICK]], True),
    "mcl_tools:sword_wood": ([[WOOD], [WOOD], [STICK]], True),

    "mcl_furnaces:furnace": ([[COBBLE, COBBLE, COBBLE],
                              [COBBLE, None, COBBLE],
                              [COBBLE, COBBLE, COBBLE]], True),
}

# Which logs count as "a log" -- MineClone2 has one per tree species.
LOGS = ("mcl_core:tree", "mcl_core:birchtree", "mcl_core:sprucetree",
        "mcl_core:acaciatree", "mcl_core:darktree", "mcl_core:jungletree")


def get(item):
    """Return (rows, needs_table) for an item, or None if unknown."""
    return RECIPES.get(item)
