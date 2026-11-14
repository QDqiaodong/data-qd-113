"""Optional threshold stream network derived from fixed D8 directions.

Only cells whose ``upstream_cells`` reaches the threshold are part of the
network.  Edges are the already-computed D8 targets, so no flow direction is
recomputed or changed and no slope is compared here.
"""


def build(cells, target, accumulation, cell_size_m, threshold):
    """Return a threshold stream network as a JSON-ready dictionary.

    ``cells`` maps coordinate to elevation, ``target`` maps coordinate to its
    strict-descent downstream coordinate (or ``None``) and ``accumulation``
    maps coordinate to the number of cells draining through it, including
    itself.  Coordinates are ``(row, col)`` tuples.

    The network is downstream-closed: if a cell reaches the threshold then its
    downstream cell has at least the same accumulation and is therefore also a
    member.  Every edge of the network then lies on exactly one maximal
    directed chain between nodes.  Nodes are cells with an in-network
    in-degree other than one, or without a downstream cell; isolated network
    cells are emitted as nodes without a chain.
    """
    if type(threshold) is not int or threshold < 1:
        raise ValueError('stream threshold must be a positive integer')
    network = frozenset(p for p, count in accumulation.items() if count >= threshold)
    empty = {'threshold': threshold, 'cell_size_m': cell_size_m,
             'cell_count': 0, 'edge_count': 0, 'nodes': [], 'chains': []}
    if not network:
        return empty

    incoming = {p: [] for p in network}
    for p in network:
        q = target[p]
        if q is not None:
            incoming[q].append(p)
    edge_count = sum(len(upstream) for upstream in incoming.values())

    # Strahler order over in-network tributaries.  A cell without a network
    # tributary is order 1; it increases only when at least two tributaries
    # share the highest order.  Strict descent makes decreasing elevation a
    # topological order, so every tributary is already resolved.
    order = {}
    for p in sorted(network, key=lambda pos: (-cells[pos], pos)):
        if not incoming[p]:
            order[p] = 1
            continue
        highest = max(order[q] for q in incoming[p])
        order[p] = highest + (sum(order[q] == highest for q in incoming[p]) >= 2)

    node_set = frozenset(p for p in network
                         if len(incoming[p]) != 1 or target[p] is None)
    ordered_nodes = sorted(node_set)
    node_id = {p: index for index, p in enumerate(ordered_nodes)}
    nodes = [{'id': node_id[p], 'row': p[0], 'col': p[1],
              'upstream_cells': accumulation[p], 'strahler': order[p]}
             for p in ordered_nodes]

    chains = []
    for p in ordered_nodes:
        if target[p] is None:  # isolated stream cell: node only
            continue
        path = [p]
        cur = target[p]
        while cur not in node_set:
            path.append(cur)
            cur = target[cur]
        path.append(cur)
        orthogonal = diagonal = 0
        for a, b in zip(path, path[1:]):
            dr = abs(a[0] - b[0])
            dc = abs(a[1] - b[1])
            if dr == 1 and dc == 1:
                diagonal += 1
            else:
                orthogonal += 1
        chains.append({
            'id': len(chains),
            'start_node': node_id[p],
            'end_node': node_id[cur],
            'start': [p[0], p[1]],
            'end': [cur[0], cur[1]],
            'orthogonal_steps': orthogonal,
            'diagonal_steps': diagonal,
            # Exact length in metres; never evaluated as a float.
            'length_expression': '%d*(%d+%d*sqrt(2))'
                                 % (cell_size_m, orthogonal, diagonal),
            'strahler': order[p],
        })

    return {'threshold': threshold, 'cell_size_m': cell_size_m,
            'cell_count': len(network), 'edge_count': edge_count,
            'nodes': nodes, 'chains': chains}
