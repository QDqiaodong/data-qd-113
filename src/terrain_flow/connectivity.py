"""Rising-water component-hierarchy forest over a raster grid.

At water level ``h`` every valid cell whose elevation is ``<= h`` is active and
cells are connected through their eight neighbours (the same 8-neighbourhood
used by the D8 pipeline); ``null`` cells are obstacles and no connection
leaves the grid.  As the level rises, the connected components evolve by:

* a new component appearing (a leaf node),
* a component growing into one batch of new cells without meeting another old
  component (the existing node expands, *no* new node is created),
* several old components being joined by one batch (a single multi-way node).

The result is a rooted forest in which every internal node merges all of its
children at once.  Cells sharing an elevation are always processed as one
indivisible batch, so parallel saddles cannot produce an artificial binary
chain.  All traversals are iterative: nothing recurses along the grid or the
forest.

Every value in the answer is an exact integer.  The storage volume of a
component at level ``h`` is ``sum(h - z) * cell_area`` over its cells.
"""

import json

ELEVATION_MIN = -10000
ELEVATION_MAX = 10000
MAX_CELLS = 100000
MAX_QUERIES = 100000
# Query documents can be larger than the 8 MB terrain cap because they may
# carry 100k integer triples; the terrain grid keeps the existing io cap.
DOCUMENT_LIMIT = 32_000_000

NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
              (0, 1), (1, -1), (1, 0), (1, 1))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def load_document(path):
    """Read and parse a JSON document with duplicate-key and NaN checks."""
    from pathlib import Path
    raw = Path(path).read_bytes()
    if len(raw) > DOCUMENT_LIMIT:
        raise ValueError('input exceeds %d bytes' % DOCUMENT_LIMIT)

    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)

    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)


def _integer(value, low, high, what):
    # bool is an int subclass; mirrors the strict grid validation.
    if type(value) is not int or not low <= value <= high:
        raise ValueError('%s must be an integer in %d..%d' % (what, low, high))
    return value


def parse_grid(data):
    """Validate a terrain document.

    Returns ``(elevations, height, width, cell_area)`` where ``elevations`` is
    a flat list holding the integer elevation or ``None`` for no-data cells.
    """
    if not isinstance(data, dict) or set(data) != {'cell_size_m', 'elevations'}:
        raise ValueError('expected fields: cell_size_m elevations')
    size = _integer(data['cell_size_m'], 1, 10000, 'cell_size_m')
    grid = data['elevations']
    if (not isinstance(grid, list) or not grid
            or not isinstance(grid[0], list) or not grid[0]):
        raise ValueError('nonempty rectangular grid required')
    height = len(grid)
    width = len(grid[0])
    if height * width > MAX_CELLS:
        raise ValueError('grid exceeds %d cells' % MAX_CELLS)
    elevations = [None] * (height * width)
    for r, row in enumerate(grid):
        if not isinstance(row, list) or len(row) != width:
            raise ValueError('ragged grid')
        base = r * width
        for c, value in enumerate(row):
            if value is None:
                continue
            elevations[base + c] = _integer(value, ELEVATION_MIN, ELEVATION_MAX,
                                            'elevation')
    return elevations, height, width, size * size


def parse_queries(document, height, width):
    """Validate the query document and return ``(level, row, col)`` triples."""
    if not isinstance(document, dict) or set(document) != {'queries'}:
        raise ValueError('expected fields: queries')
    items = document['queries']
    if not isinstance(items, list) or len(items) > MAX_QUERIES:
        raise ValueError('queries must be an array of at most %d entries'
                         % MAX_QUERIES)
    queries = []
    for index, item in enumerate(items):
        where = 'query %d' % index
        if not isinstance(item, dict) or set(item) != {'row', 'col', 'level'}:
            raise ValueError(where + ': expected fields: row col level')
        row = _integer(item['row'], 0, height - 1, where + ' row')
        col = _integer(item['col'], 0, width - 1, where + ' col')
        level = _integer(item['level'], ELEVATION_MIN, ELEVATION_MAX,
                         where + ' level')
        queries.append((level, row, col))
    return queries


def _find(parent, x):
    """Iterative DSU find with path compression; no recursion on grid depth."""
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != x:
        nxt = parent[x]
        parent[x] = root
        x = nxt
    return root


def build_index(elevations, height, width, cell_area, queries):
    """Build the merge forest and answer every query in its original order.

    Returns a JSON-ready dictionary with the forest nodes and per-query
    answers.  Equal-elevation cells are processed as one batch; all graph and
    forest traversal is iterative.
    """
    n = height * width
    # Disjoint-set state sized once for the whole grid.  A cell is inactive
    # exactly while its parent slot is -1.
    parent = [-1] * n
    dsu_size = [0] * n
    dsu_min = [0] * n              # minimum flat index in each DSU root
    elevation_sum = [0] * n        # sum of cell elevations per DSU root
    root_node = [-1] * n           # forest node currently naming each root

    # Forest node arrays; a node id is its array index.
    node_level = []
    node_min = []                  # frozen post-event component minimum cell
    node_children = []

    def make_leaf(level, min_cell):
        node_id = len(node_level)
        node_level.append(level)
        node_min.append(min_cell)
        node_children.append([])
        return node_id

    def make_merge(level, min_cell, children):
        node_id = len(node_level)
        node_level.append(level)
        node_min.append(min_cell)
        node_children.append(children)
        return node_id

    def union(a, b):
        """Union by size; keep elevation sum and minimum cell exact."""
        ra = _find(parent, a)
        rb = _find(parent, b)
        if ra == rb:
            return ra
        if dsu_size[ra] < dsu_size[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        dsu_size[ra] += dsu_size[rb]
        elevation_sum[ra] += elevation_sum[rb]
        if dsu_min[rb] < dsu_min[ra]:
            dsu_min[ra] = dsu_min[rb]
        return ra

    # Group valid cells by elevation so equal elevations form one event.
    by_level = {}
    for index, z in enumerate(elevations):
        if z is not None:
            by_level.setdefault(z, []).append(index)
    levels = sorted(by_level)

    def process_batch(level, new_cells):
        """Activate all cells of one elevation as a single indivisible event."""
        # Phase 1: activate the new cells and union them among themselves.
        for index in new_cells:
            parent[index] = index
            dsu_size[index] = 1
            dsu_min[index] = index
            elevation_sum[index] = elevations[index]
            root_node[index] = -1
        for index in new_cells:
            r, c = divmod(index, width)
            for dr, dc in NEIGHBOURS:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < height and 0 <= nc < width):
                    continue
                other = nr * width + nc
                # Same-elevation active neighbours belong to this same batch.
                if parent[other] != -1 and elevations[other] == level:
                    union(index, other)

        # Phase 2: gather, per new-cell group, the distinct old component
        # nodes it touches.  No new-old union has happened yet, so every old
        # neighbour still resolves to its pre-event root.
        group_old = {}
        for index in new_cells:
            r, c = divmod(index, width)
            group = _find(parent, index)
            touched = group_old.setdefault(group, set())
            for dr, dc in NEIGHBOURS:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < height and 0 <= nc < width):
                    continue
                other = nr * width + nc
                if parent[other] != -1 and elevations[other] != level:
                    old_root = _find(parent, other)
                    touched.add(root_node[old_root])

        # Phase 3: join each new-cell group with the old components it meets.
        for group, old_nodes in group_old.items():
            for node_id in old_nodes:
                # The node's frozen minimum cell always stays inside its
                # component, so locating it yields the component's live root.
                union(group, node_min[node_id])

        # Phase 4: classify every final component containing new cells.
        finals = {}
        for index in new_cells:
            root = _find(parent, index)
            if root not in finals:
                finals[root] = set()
        for group, old_nodes in group_old.items():
            finals[_find(parent, group)].update(old_nodes)

        # Within one event, node ids follow the post-event component minimum
        # coordinate, which keeps all ids sorted by (event level, coordinate).
        for root in sorted(finals, key=dsu_min.__getitem__):
            old_nodes = finals[root]
            count = len(old_nodes)
            min_cell = dsu_min[root]
            if count == 0:
                # No pre-event component inside: a brand new leaf.
                root_node[root] = make_leaf(level, min_cell)
            elif count == 1:
                # Growing one existing component expands its node; the node
                # identity (id, event level, children) is preserved.
                root_node[root] = next(iter(old_nodes))
            else:
                # Several old components join at once: one multi-way node,
                # never a chain of artificial binary merges.
                root_node[root] = make_merge(level, min_cell,
                                             sorted(old_nodes))

    # Sweep levels and queries together.  A query at level h observes the
    # state after the batch at h (if any) has been processed.
    ordered = sorted((level, index)
                     for index, (level, _r, _c) in enumerate(queries))
    answers = [None] * len(queries)
    cursor = 0
    for level, query_index in ordered:
        while cursor < len(levels) and levels[cursor] <= level:
            process_batch(levels[cursor], by_level[levels[cursor]])
            cursor += 1
        _qlevel, row, col = queries[query_index]
        cell = row * width + col
        z = elevations[cell]
        if z is None or z > level:
            answers[query_index] = None
            continue
        root = _find(parent, cell)
        area_cells = dsu_size[root]
        # h * count - sum(z) stays integral for integer levels and elevations.
        storage = (level * area_cells - elevation_sum[root]) * cell_area
        answers[query_index] = {
            'node_id': root_node[root],
            'area_cells': area_cells,
            'area_m2': area_cells * cell_area,
            'storage_m3': storage,
        }
    # Finish levels no query reached so the emitted forest is always complete.
    while cursor < len(levels):
        process_batch(levels[cursor], by_level[levels[cursor]])
        cursor += 1

    roots = _forest_roots(node_children)
    forest = [
        {'id': node_id,
         'event_level': node_level[node_id],
         'min_cell': list(divmod(node_min[node_id], width)),
         'children': node_children[node_id]}
        for node_id in range(len(node_level))
    ]
    return {'shape': [height, width], 'cell_area_m2': cell_area,
            'node_count': len(node_level), 'roots': roots,
            'forest': forest, 'queries': answers}


def _forest_roots(node_children):
    """Iteratively collect nodes that are no other node's child."""
    is_child = [False] * len(node_children)
    for children in node_children:
        for child in children:
            is_child[child] = True
    return [node_id for node_id, marked in enumerate(is_child) if not marked]


def run(grid_path, queries_path):
    """Load both documents, validate them and build the index."""
    grid_doc = load_document(grid_path)
    elevations, height, width, cell_area = parse_grid(grid_doc)
    queries = parse_queries(load_document(queries_path), height, width)
    return build_index(elevations, height, width, cell_area, queries)
