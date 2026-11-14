import json, unittest, tempfile, subprocess, sys
from pathlib import Path
from terrain_flow.drainage import drain
ROOT=Path(__file__).resolve().parents[1]

# Hand-computed confluence grid (cell_size 4).  Two sources on row 0 join at
# [1,1]; the [2,3] tributary reaches the common outlet [3,3] with order 1, so
# the outlet keeps order 2 instead of increasing to 3.
CONFLUENCE={'cell_size_m':4,'elevations':[[10,10,None,None,None],
                                          [None,2,None,None,None],
                                          [None,None,1,5,None],
                                          [None,None,None,0,None]]}

# Two equal-order tributaries join at [2,2] (order 3).  [3,4] is a valid
# isolated stream cell: no tributary and no downstream.
EQUAL_AND_ISOLATED={'cell_size_m':7,'elevations':[[None,10,10,None,None],
                                                  [None,5,None,None,None],
                                                  [None,None,1,None,None],
                                                  [None,None,None,None,4]]}

# Two order-2 branches (each fed by two sources) meet at [2,2].
TWO_ORDER_TWO={'cell_size_m':2,'elevations':[[9,9,None,9,9],
                                             [None,5,None,5,None],
                                             [None,None,3,None,None]]}

class StreamNetworkTests(unittest.TestCase):
    def sn(self,data,threshold):
        return drain(data,threshold)['stream_network']

    def test_flag_is_optional_and_leaves_base_report_unchanged(self):
        data=json.loads((ROOT/'fixtures/terrain.json').read_text())
        base=drain(data);self.assertNotIn('stream_network',base)
        extended=drain(data,3);self.assertIn('stream_network',extended)
        del extended['stream_network'];self.assertEqual(extended,base)

    def test_hand_computed_join_and_unequal_tributary(self):
        sn=self.sn(CONFLUENCE,1)
        self.assertEqual(sn['cell_count'],6);self.assertEqual(sn['edge_count'],5)
        self.assertEqual([(n['row'],n['col'],n['upstream_cells'],n['strahler']) for n in sn['nodes']],
                         [(0,0,1,1),(0,1,1,1),(1,1,3,2),(2,3,1,1),(3,3,6,2)])
        self.assertEqual([(c['start'],c['end'],c['orthogonal_steps'],
                           c['diagonal_steps'],c['strahler']) for c in sn['chains']],
                         [([0,0],[1,1],0,1,1),([0,1],[1,1],1,0,1),
                          ([1,1],[3,3],0,2,2),([2,3],[3,3],1,0,1)])
        self.assertEqual(sn['chains'][2]['length_expression'],'4*(0+2*sqrt(2))')
        # Only two tributaries sharing the highest order raise the order.
        reach=[c for c in sn['chains'] if c['end']==[3,3] and c['start']==[1,1]][0]
        self.assertEqual(reach['strahler'],2)

    def test_equal_tributaries_raise_order_and_isolated_cell_has_no_chain(self):
        sn=self.sn(EQUAL_AND_ISOLATED,1)
        self.assertEqual(sn['cell_count'],5);self.assertEqual(sn['edge_count'],3)
        self.assertEqual([(n['id'],n['row'],n['col'],n['strahler']) for n in sn['nodes']],
                         [(0,0,1,1),(1,0,2,1),(2,1,1,2),(3,2,2,2),(4,3,4,1)])
        ids={c['start_node'] for c in sn['chains']};ends={c['end_node'] for c in sn['chains']}
        isolated=[n for n in sn['nodes'] if n['id'] not in ids and n['id'] not in ends]
        self.assertEqual([(n['row'],n['col'],n['strahler']) for n in isolated],[(3,4,1)])
        self.assertEqual(len(sn['chains']),3)
        self.assertEqual(sn['chains'][2]['length_expression'],'7*(0+1*sqrt(2))')

    def test_two_order_two_branches_make_order_three(self):
        sn=self.sn(TWO_ORDER_TWO,1)
        orders={(n['row'],n['col']):n['strahler'] for n in sn['nodes']}
        self.assertEqual(orders[(1,1)],2);self.assertEqual(orders[(1,3)],2)
        self.assertEqual(orders[(2,2)],3)
        self.assertEqual([(c['orthogonal_steps'],c['diagonal_steps']) for c in sn['chains']],
                         [(0,1),(1,0),(1,0),(0,1),(0,1),(0,1)])

    def test_threshold_above_grid_returns_empty_network(self):
        for data in (CONFLUENCE,{'cell_size_m':1,'elevations':[[None,None]]}):
            sn=self.sn(data,10**6)
            self.assertEqual(sn['threshold'],10**6)
            self.assertEqual((sn['cell_count'],sn['edge_count'],sn['nodes'],sn['chains']),
                             (0,0,[],[]))

    def test_plateau_negative_and_null_keep_semantics(self):
        data={'cell_size_m':1,'elevations':[[None,-3,-3],[-3,None,-3],[-3,-3,-3]]}
        base=drain(data)
        self.assertEqual(base['valid_cells'],7);self.assertEqual(len(base['catchments']),7)
        sn=self.sn(data,1)
        self.assertEqual(sn['cell_count'],7);self.assertEqual(sn['edge_count'],0)
        self.assertEqual(len(sn['nodes']),7);self.assertEqual(sn['chains'],[])
        self.assertTrue(all(n['strahler']==1 for n in sn['nodes']))
        self.assertNotIn((0,0),{(n['row'],n['col']) for n in sn['nodes']})

    def test_nodes_and_chains_are_stable_and_partition_edges(self):
        sn=self.sn(CONFLUENCE,1)
        report=drain(CONFLUENCE,1)
        cells={(c['row'],c['col']):c for c in report['cells']}
        network={(p,tuple(c['downstream'])) for p,c in cells.items()
                 if c['downstream'] is not None}
        self.assertEqual([n['id'] for n in sn['nodes']],list(range(len(sn['nodes']))))
        node_keys=[(n['row'],n['col']) for n in sn['nodes']]
        self.assertEqual(node_keys,sorted(node_keys))
        self.assertEqual([c['id'] for c in sn['chains']],list(range(len(sn['chains']))))
        self.assertEqual([tuple(c['start']) for c in sn['chains']],
                         sorted(tuple(c['start']) for c in sn['chains']))
        seen=[]
        for chain in sn['chains']:
            start=tuple(chain['start']);end=tuple(chain['end'])
            self.assertEqual((sn['nodes'][chain['start_node']]['row'],
                              sn['nodes'][chain['start_node']]['col']),start)
            self.assertEqual((sn['nodes'][chain['end_node']]['row'],
                              sn['nodes'][chain['end_node']]['col']),end)
            p=start;orthogonal=diagonal=0
            while p!=end:
                q=tuple(cells[p]['downstream'])
                if abs(p[0]-q[0])==1 and abs(p[1]-q[1])==1:diagonal+=1
                else:orthogonal+=1
                seen.append((p,q));p=q
            self.assertEqual((orthogonal,diagonal,orthogonal+diagonal),
                             (chain['orthogonal_steps'],chain['diagonal_steps'],
                              chain['orthogonal_steps']+chain['diagonal_steps']))
        self.assertEqual(len(seen),sn['edge_count'])
        self.assertEqual(len(seen),len(set(seen)))
        self.assertEqual(set(seen),network)
        self.assertEqual(sum(1 for a,b in seen),sn['edge_count'])

    def test_long_chain_does_not_recurse(self):
        width=20001
        r=drain({'cell_size_m':3,'elevations':[[10000-c for c in range(width)]]},1)
        sn=r['stream_network']
        self.assertEqual(len(sn['nodes']),2);self.assertEqual(len(sn['chains']),1)
        self.assertEqual(sn['cell_count'],width);self.assertEqual(sn['edge_count'],width-1)
        chain=sn['chains'][0]
        self.assertEqual((chain['orthogonal_steps'],chain['diagonal_steps']),(width-1,0))
        self.assertEqual(chain['length_expression'],'3*(%d+0*sqrt(2))'%(width-1))
        self.assertEqual(chain['strahler'],1)

    def test_invalid_threshold_rejected(self):
        for value in (0,-1,True,1.5,'3'):
            with self.assertRaises(ValueError):drain(CONFLUENCE,value)

    def test_cli_threshold_output_and_failure_preserves_file(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'report.json'
            base=[sys.executable,'-m','terrain_flow.cli','--output',str(out),'--source']
            self.assertEqual(subprocess.run(base+[str(ROOT/'fixtures/terrain.json')],
                                            capture_output=True).returncode,0)
            self.assertNotIn('stream_network',json.loads(out.read_text()))
            threshold=base+[str(ROOT/'fixtures/terrain.json'),'--stream-threshold','3']
            r=subprocess.run(threshold,capture_output=True);self.assertEqual(r.returncode,0,r.stderr)
            written=json.loads(out.read_text())
            self.assertIn('stream_network',written)
            self.assertEqual(written['stream_network'],
                             drain(json.loads((ROOT/'fixtures/terrain.json').read_text()),3)['stream_network'])
            before=out.read_bytes()
            for bad in ('0','-2','1.5','abc'):
                failed=subprocess.run(threshold[:-1]+[bad],capture_output=True)
                self.assertEqual(failed.returncode,2)
                self.assertEqual(out.read_bytes(),before)
            broken=Path(td)/'broken.json';broken.write_text('{}')
            failed=subprocess.run(base+[str(broken),'--stream-threshold','3'],capture_output=True)
            self.assertEqual(failed.returncode,2);self.assertEqual(out.read_bytes(),before)

if __name__=='__main__':unittest.main()
