"""A* pathfinding over the bot's world model.

Coordinate convention
---------------------
A node (x, y, z) is the unit cube centred on those coordinates, so it spans
y-0.5 .. y+0.5. "Standing at node p" means the player's feet occupy p; the body
fills p and p+(0,1,0) (the player is 1.77 nodes tall), and p-(0,1,0) is the
block being stood on. The feet's world position is therefore (p.x, p.y-0.5, p.z).
"""
import heapq
import math

# movement costs, in arbitrary units where one straight step == 1
COST_STRAIGHT = 1.0
COST_DIAGONAL = math.sqrt(2)
COST_JUMP = 0.6          # added when climbing a step
COST_FALL = 1.2          # per node fallen; keeps drops short since a fall
                         # of more than 3 nodes starts costing health
COST_CLIMB = 1.4         # ladders are slower than walking
COST_SWIM = 1.8
COST_DIG = 6.0           # discourage, but allow, mining through terrain

HORIZONTAL = [(1, 0), (-1, 0), (0, 1), (0, -1)]
DIAGONAL = [(1, 1), (1, -1), (-1, 1), (-1, -1)]


class PathResult:
    def __init__(self, path, reached_goal, expanded, reason=""):
        self.path = path
        self.reached_goal = reached_goal
        self.expanded = expanded
        self.reason = reason

    def __bool__(self):
        return bool(self.path) and self.reached_goal

    def __repr__(self):
        return (f"<PathResult {len(self.path)} steps goal={self.reached_goal} "
                f"expanded={self.expanded} {self.reason}>")


class Pathfinder:
    def __init__(self, world, *, max_drop=3, allow_diagonal=True,
                 allow_swim=True, avoid_damage=True, max_expanded=60000,
                 can_dig=False, unknown_is_blocked=True):
        self.world = world
        self.max_drop = max_drop
        self.allow_diagonal = allow_diagonal
        self.allow_swim = allow_swim
        self.avoid_damage = avoid_damage
        self.max_expanded = max_expanded
        self.can_dig = can_dig
        self.unknown_is_blocked = unknown_is_blocked

    # ------------------------------------------------------------ predicates

    def _def(self, p):
        return self.world.get_def(p)

    def passable(self, p):
        """Can the player's body occupy this node?"""
        d = self._def(p)
        if d is None:
            return not self.unknown_is_blocked
        if d.walkable:
            return False
        if self.avoid_damage and d.damage_per_second > 0:
            return False
        if d.is_liquid and not self.allow_swim:
            return False
        return True

    def standable(self, p):
        """Is `p` a valid place to stand: body fits and something supports it?"""
        if not self.passable(p) or not self.passable((p[0], p[1] + 1, p[2])):
            return False
        below = (p[0], p[1] - 1, p[2])
        d = self._def(below)
        if d is None:
            return not self.unknown_is_blocked
        if self.avoid_damage and d.damage_per_second > 0:
            return False
        if d.walkable:
            return True
        # standing is also possible in a ladder or in water
        here = self._def(p)
        if here is not None and (here.climbable or (self.allow_swim and here.is_liquid)):
            return True
        return False

    def diggable(self, p):
        d = self._def(p)
        return bool(d and d.diggable and d.walkable)

    # ------------------------------------------------------------- neighbours

    def neighbours(self, p):
        """Yield (node, cost, action) reachable in one step from `p`."""
        x, y, z = p
        here = self._def(p)
        in_climb = bool(here and here.climbable)
        in_liquid = bool(here and here.is_liquid)

        # --- vertical: ladders and swimming
        up = (x, y + 1, z)
        if (in_climb or in_liquid) and self.passable(up) and \
                self.passable((x, y + 2, z)):
            yield up, (COST_CLIMB if in_climb else COST_SWIM), "climb_up"
        down = (x, y - 1, z)
        d_down = self._def(down)
        if d_down is not None and (d_down.climbable or
                                   (self.allow_swim and d_down.is_liquid)):
            if self.passable(down):
                yield down, (COST_CLIMB if d_down.climbable else COST_SWIM), "climb_down"

        dirs = HORIZONTAL + (DIAGONAL if self.allow_diagonal else [])
        for dx, dz in dirs:
            diagonal = dx and dz
            if diagonal and not self._corner_open(p, dx, dz):
                continue
            base = COST_DIAGONAL if diagonal else COST_STRAIGHT
            yield from self._column_moves(p, dx, dz, base)

    def _corner_open(self, p, dx, dz):
        """Refuse diagonal moves that would clip through a corner."""
        x, y, z = p
        for side in ((x + dx, y, z), (x, y, z + dz)):
            if not self.passable(side) or not self.passable((side[0], side[1] + 1, side[2])):
                return False
        return True

    def _column_moves(self, p, dx, dz, base):
        x, y, z = p
        tx, tz = x + dx, z + dz

        # step up one block (requires headroom above us to jump)
        step_up = (tx, y + 1, tz)
        if self.passable((x, y + 2, z)) and self.standable(step_up) \
                and not self.passable((tx, y, tz)):
            yield step_up, base + COST_JUMP, "jump"

        # walk level
        level = (tx, y, tz)
        if self.standable(level):
            yield level, base, "walk"
            return

        # walk off an edge and fall
        if self.passable(level) and self.passable((tx, y + 1, tz)):
            for drop in range(1, self.max_drop + 1):
                land = (tx, y - drop, tz)
                if not self.passable(land):
                    break
                if self.standable(land):
                    yield land, base + COST_FALL * drop, "fall"
                    break

        # optionally mine straight through
        if self.can_dig and self.diggable(level):
            target = (tx, y, tz)
            if self.diggable((tx, y + 1, tz)) or self.passable((tx, y + 1, tz)):
                yield target, base + COST_DIG, "dig"

    # ------------------------------------------------------------------- A*

    @staticmethod
    def heuristic(a, b):
        dx, dy, dz = abs(a[0]-b[0]), abs(a[1]-b[1]), abs(a[2]-b[2])
        # octile distance on the horizontal plane, plus vertical
        lo, hi = min(dx, dz), max(dx, dz)
        return (COST_DIAGONAL - COST_STRAIGHT) * lo + COST_STRAIGHT * hi + dy

    def find_path(self, start, goal, tolerance=0):
        """A* from `start` to `goal`, both standing positions.

        If the goal cannot be reached, returns the path to the closest node
        that was reached, with reached_goal False -- which lets a bot make
        progress across a partially-loaded map instead of refusing to move.
        """
        start = tuple(int(v) for v in start)
        goal = tuple(int(v) for v in goal)

        if not self.standable(start):
            fixed = self._nearest_standable(start)
            if fixed is None:
                return PathResult([], False, 0, "start position is not standable")
            start = fixed

        open_heap = [(self.heuristic(start, goal), 0.0, start)]
        came_from = {start: None}
        g_score = {start: 0.0}
        actions = {start: "start"}
        best, best_h = start, self.heuristic(start, goal)
        expanded = 0

        while open_heap:
            _f, g, current = heapq.heappop(open_heap)
            if g > g_score.get(current, math.inf):
                continue
            expanded += 1
            if expanded > self.max_expanded:
                return PathResult(self._rebuild(came_from, actions, best), False,
                                  expanded, "search limit reached")

            h = self.heuristic(current, goal)
            if h < best_h:
                best, best_h = current, h
            if h <= tolerance:
                return PathResult(self._rebuild(came_from, actions, current), True,
                                  expanded)

            for nxt, cost, action in self.neighbours(current):
                ng = g + cost
                if ng < g_score.get(nxt, math.inf):
                    g_score[nxt] = ng
                    came_from[nxt] = current
                    actions[nxt] = action
                    heapq.heappush(open_heap, (ng + self.heuristic(nxt, goal), ng, nxt))

        return PathResult(self._rebuild(came_from, actions, best), False, expanded,
                          "no route found")

    def explore(self, start, max_expanded=None):
        """Dijkstra flood from `start`: every node reachable, with its cost.

        Useful for questions like "how deep can I actually get from here?"
        without picking a destination in advance.
        """
        start = tuple(int(v) for v in start)
        if not self.standable(start):
            fixed = self._nearest_standable(start)
            if fixed is None:
                return {}
            start = fixed
        limit = max_expanded or self.max_expanded
        heap = [(0.0, start)]
        cost = {start: 0.0}
        expanded = 0
        while heap and expanded < limit:
            g, current = heapq.heappop(heap)
            if g > cost.get(current, math.inf):
                continue
            expanded += 1
            for nxt, step_cost, _action in self.neighbours(current):
                ng = g + step_cost
                if ng < cost.get(nxt, math.inf):
                    cost[nxt] = ng
                    heapq.heappush(heap, (ng, nxt))
        return cost

    def _rebuild(self, came_from, actions, node):
        out = []
        while node is not None:
            out.append(Step(node, actions.get(node, "walk")))
            node = came_from[node]
        out.reverse()
        return out

    def _nearest_standable(self, p, radius=4):
        """Snap to a standable node -- e.g. when the bot is mid-fall."""
        best = None
        for dy in range(0, -radius - 1, -1):        # prefer straight down
            q = (p[0], p[1] + dy, p[2])
            if self.standable(q):
                return q
        for r in range(1, radius + 1):
            for dx in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    for dy in range(-r, r + 1):
                        q = (p[0]+dx, p[1]+dy, p[2]+dz)
                        if self.standable(q):
                            d = dx*dx + dy*dy + dz*dz
                            if best is None or d < best[0]:
                                best = (d, q)
            if best:
                return best[1]
        return None


class Step:
    __slots__ = ("pos", "action")

    def __init__(self, pos, action):
        self.pos = pos
        self.action = action

    def __iter__(self):
        return iter(self.pos)

    def __repr__(self):
        return f"{self.action}{self.pos}"
