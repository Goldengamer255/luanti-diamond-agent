"""Parsing of Luanti's plain-text inventory serialisation.

TOCLIENT_INVENTORY carries the player's whole inventory as text, e.g.

    List main 36
    Width 9
    Item mcl_core:dirt 3
    Empty
    ...
    EndInventoryList
    KeepList craft
    EndInventory

Updates are incremental: a `List` block replaces that list, while `KeepList`
means the list is unchanged and the client should keep what it already has. So
inventory state must be merged across packets, not replaced.
"""


class ItemStack:
    __slots__ = ("name", "count", "wear", "metadata")

    def __init__(self, name="", count=0, wear=0, metadata=""):
        self.name = name
        self.count = count
        self.wear = wear
        self.metadata = metadata

    @property
    def empty(self):
        return not self.name or self.count <= 0

    @classmethod
    def parse(cls, itemstring: str):
        parts = itemstring.split(" ", 3)
        if not parts or not parts[0]:
            return cls()
        name = parts[0]
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        wear = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return cls(name, count, wear, parts[3] if len(parts) > 3 else "")

    def __repr__(self):
        return "<empty>" if self.empty else f"{self.name} x{self.count}"


class Inventory:
    def __init__(self):
        self.lists = {}          # list name -> [ItemStack]
        self.widths = {}         # list name -> grid width (crafting shape)

    @property
    def main(self):
        return self.lists.get("main", [])

    def find(self, name):
        """Slot indices in `main` holding an item, exact name or 'group:' free."""
        return [i for i, s in enumerate(self.main)
                if not s.empty and (s.name == name or s.name.endswith(":" + name))]

    def first_slot(self, name):
        hits = self.find(name)
        return hits[0] if hits else None

    def width(self, list_name, default=None):
        w = self.widths.get(list_name)
        if w:
            return w
        size = len(self.lists.get(list_name, ()))
        return default or (3 if size == 9 else 2 if size == 4 else size)

    def free_slot(self, list_name="main"):
        for i, s in enumerate(self.lists.get(list_name, ())):
            if s.empty:
                return i
        return None

    def count(self, name):
        return sum(s.count for s in self.main if not s.empty and s.name == name)

    def non_empty(self):
        return [(i, s) for i, s in enumerate(self.main) if not s.empty]

    def apply(self, text: str):
        """Merge one TOCLIENT_INVENTORY update into this inventory."""
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("List "):
                current = line.split()[1]
                self.lists[current] = []
            elif line.startswith("Width ") and current is not None:
                try:
                    self.widths[current] = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("KeepList "):
                current = None                 # unchanged; keep what we have
            elif line in ("EndInventoryList", "EndInventory"):
                current = None
            elif current is not None:
                if line == "Empty":
                    self.lists[current].append(ItemStack())
                elif line.startswith("Item "):
                    self.lists[current].append(ItemStack.parse(line[5:]))
                # anything else is ignored
        return self

    @classmethod
    def parse(cls, text: str):
        return cls().apply(text)

    def __repr__(self):
        return f"<Inventory {[(k, len(v)) for k, v in self.lists.items()]}>"
