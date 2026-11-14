"""Integer-grid strict-descent D8 with exact squared-slope comparisons."""
from fractions import Fraction
from collections import defaultdict
from .io import fields, integer, array
from .stream_network import build as build_stream_network

def drain(data,stream_threshold=None):
    fields(data,'cell_size_m elevations');size=integer(data['cell_size_m'],1,10000)
    if stream_threshold is not None and (type(stream_threshold)!=int or stream_threshold<1):
        raise ValueError('stream threshold must be a positive integer')
    grid=array(data['elevations'])
    if not grid or not isinstance(grid[0],list) or not grid[0]:raise ValueError('nonempty rectangular grid required')
    height=len(grid);width=len(grid[0])
    if height*width>100000:raise ValueError('grid exceeds 100000 cells')
    cells={}
    for r,row in enumerate(grid):
        if not isinstance(row,list) or len(row)!=width:raise ValueError('ragged grid')
        for c,value in enumerate(row):
            if value is not None:cells[r,c]=integer(value,-10000,10000)
    target={}
    for pos,z in cells.items():
        r,c=pos;candidates=[]
        for dr in (-1,0,1):
            for dc in (-1,0,1):
                if dr==dc==0:continue
                other=(r+dr,c+dc)
                if other not in cells or cells[other]>=z:continue
                # Positive slopes compare exactly via squared rise / squared run.
                score=Fraction((z-cells[other])**2,dr*dr+dc*dc)
                candidates.append((-score,other))
        target[pos]=min(candidates)[1] if candidates else None
    accumulation={pos:1 for pos in cells}
    for pos in sorted(cells,key=lambda p:(-cells[p],p)):
        if target[pos] is not None:accumulation[target[pos]]+=accumulation[pos]
    outlet={}
    for pos in sorted(cells,key=lambda p:(cells[p],p)):
        outlet[pos]=outlet[target[pos]] if target[pos] is not None else pos
    catchments=defaultdict(list)
    for pos in sorted(cells):catchments[outlet[pos]].append(pos)
    result={'shape':[height,width],'cell_size_m':size,'valid_cells':len(cells),
            'total_area_m2':len(cells)*size*size,
            'cells':[{'row':p[0],'col':p[1],'elevation_m':cells[p],
                      'downstream':list(target[p]) if target[p] is not None else None,
                      'upstream_cells':accumulation[p],
                      'contributing_area_m2':accumulation[p]*size*size,
                      'outlet':list(outlet[p])} for p in sorted(cells)],
            'catchments':[{'outlet':list(p),'cells':len(members),'area_m2':len(members)*size*size}
                          for p,members in sorted(catchments.items())]}
    if stream_threshold is not None:
        result['stream_network']=build_stream_network(cells,target,accumulation,size,stream_threshold)
    return result
