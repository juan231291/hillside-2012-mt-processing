"""Cross-station content candidates supplement name matching; no exclusions."""
from collections import defaultdict


def add_global_sequence_review(results):
    signatures=defaultdict(list)
    for result in results:
        for group in result[1]:
            cc=group['channels']
            if all(c.get('sample_hash_encoding')=='signed_int64_little_endian_values_v1' and c.get('sample_count',0)>0 for c in cc.values()):
                signature=tuple((k,c['sample_count'],c['sample_sha256']) for k,c in sorted(cc.items()))
                signatures[signature].append(group)
    for groups in signatures.values():
        if len({g['station'] for g in groups})<2:continue
        ids=sorted(g['group_id'] for g in groups)
        for g in groups:
            g['cross_station_sample_matches']=ids
            g['flags']=sorted(set(g['flags'])|{'repeated_five_channel_sample_sequence'})
            g['review_required']=True
    for summary,groups,_,_,_ in results:
        summary['groups_requiring_review']=sum(g['review_required'] for g in groups)
        summary['cross_station_sequence_candidates']=sum(bool(g.get('cross_station_sample_matches')) for g in groups)
