"""Two QC outputs: a compact human workbook and complete JSONL evidence."""
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from fractions import Fraction


def grouped_intervals(results):
    overlaps, gaps = defaultdict(list), defaultdict(list)
    for _,_,oo,gg,_ in results:
        for r in oo:
            overlaps[r['station'],r['left_group_id'],r['right_group_id'],r['start_utc'],r['end_exclusive_utc']].append(r)
        for r in gg:
            gaps[r['station'],r['left_group_id'],r['right_group_id'],r['start_utc'],r['end_exclusive_utc']].append(r)
    return overlaps,gaps


def short_group(gid):
    return gid.split('/',2)[-1]


def style(ws,widths,data_end):
    from openpyxl.styles import Font,Alignment,PatternFill
    from openpyxl.utils import get_column_letter
    ws.sheet_view.showGridLines=False
    ws.freeze_panes='C5'
    if data_end>=5:
        ws.auto_filter.ref=f'A4:{get_column_letter(len(widths))}{data_end}'
    ws.row_dimensions[1].height=30
    ws.row_dimensions[2].height=42
    ws.row_dimensions[4].height=44
    for c in ws[1]:
        c.font=Font(name='Arial',size=16,bold=True,color='16324F')
    for c in ws[2]:
        c.alignment=Alignment(wrap_text=True,vertical='center')
    for c in ws[4]:
        c.font=Font(name='Arial',size=10,bold=True,color='FFFFFF')
        c.fill=PatternFill('solid',fgColor='16324F')
        c.alignment=Alignment(wrap_text=True,vertical='center')
    for n in range(5,data_end+1):
        lines=max(math.ceil(len(str(ws.cell(n,i).value or ''))/max(8,width*0.9)) for i,width in enumerate(widths,1))
        ws.row_dimensions[n].height=max(45,14*lines+12)
        for c in ws[n]:
            c.font=Font(name='Arial',size=10,color='16324F')
            c.alignment=Alignment(wrap_text=True,vertical='center')
            if isinstance(c.value,(int,float)) or (isinstance(c.value,str) and c.value.replace('.','',1).isdigit()):
                c.alignment=Alignment(wrap_text=True,vertical='center',horizontal='center')
            if n%2==0:
                c.fill=PatternFill('solid',fgColor='F1F5F7')
    for i,width in enumerate(widths,1):
        ws.column_dimensions[get_column_letter(i)].width=width


def sheet(wb,name,title,note,columns):
    from openpyxl.utils import get_column_letter
    ws=wb.create_sheet(name)
    last=get_column_letter(len(columns))
    ws.append([title]);ws.merge_cells(f'A1:{last}1')
    ws.append([note]);ws.merge_cells(f'A2:{last}2')
    ws.append([]);ws.append(columns)
    return ws


def write_reports(out,manifest,results):
    from openpyxl import Workbook,load_workbook
    wb=Workbook();wb.remove(wb.active)
    overlap_sets,gap_sets=grouped_intervals(results)
    overview=sheet(wb,'Overview','MT survey | timing and sample QC',
        'Retained survey groups only. Review findings before selecting runs. ASCII rates are estimates; no samples, timestamps or decisions were changed.',
        ['Station','Groups scanned','Groups needing review','Empty/error/missing channels','Groups with header rates','Groups with ASCII estimates','Rates seen (Hz)','Overlap windows','Gap windows'])
    for summary,groups,overlaps,gaps,repeats in results:
        overview.append([summary['station'],summary['groups_scanned'],summary['groups_requiring_review'],
            summary['channels_empty']+summary['channels_with_errors_or_missing'],summary['groups_with_header_rates'],
            summary['groups_with_ascii_rate_estimates'],'; '.join(summary['observed_rates_hz']),
            sum(k[0]==summary['station'] for k in overlap_sets),sum(k[0]==summary['station'] for k in gap_sets)])
    style(overview,[14,15,18,22,20,22,24,18,18],overview.max_row)
    review=sheet(wb,'Recording review','Recordings needing review',
        'One row per flagged group. Complete evidence for every inspected group is in qc_evidence.jsonl. Corroborated ASCII rates remain estimates, not independently confirmed rates.',
        ['Station','Recording group','Samples by channel','Rate(s) Hz','Rate basis','Logged seconds','Neighbour corroborates ASCII rate','Findings'])
    for _,groups,_,_,_ in results:
        for g in groups:
            if not g['review_required']:
                continue
            cc=[v.get('sample_count') for v in g['channels'].values()]
            counts=f'{cc[0]:,} each (5 channels)' if len(set(cc))==1 and cc[0] is not None else '; '.join(f'{c.upper()}: {v["sample_count"] if v.get("sample_count") is not None else v["status"]}' for c,v in g['channels'].items())
            basis='ASCII estimate' if g['has_inferred_ascii_rate'] else 'MiniSEED header' if g['rates_hz'] else 'Unknown'
            if any(c.get('format')=='MiniSEED2' for c in g['channels'].values()) and g['has_inferred_ascii_rate']:
                basis='Mixed: header + ASCII estimate'
            review.append([g['station'],g['recording_group'],counts,'; '.join(g['rates_hz']),basis,
                           g['logged_max_seconds_consensus'],'Not applicable' if not g['has_inferred_ascii_rate'] else 'Yes' if g['ascii_rate_corroborated_by_neighbour'] else 'No',
                           '; '.join(flag.replace('_',' ') for flag in g['flags'])])
    style(review,[13,38,34,16,19,14,23,66],review.max_row)
    intervals=sheet(wb,'Intervals','Overlaps and gaps',
        'Overlap samples are compared exactly when rates/grids agree and timing is usable. Identical values do not automatically remove a copy. Gap durations use reported/candidate intervals.',
        ['Station','Type','Left recording','Right recording','Start UTC','End exclusive UTC','Seconds','Channels','Result','All five overlap channels equal'])
    for key,rr in sorted(overlap_sets.items()):
        station,left,right,start,end=key
        statuses=Counter(r['status'] for r in rr)
        channels=sorted({r['channel'] for r in rr})
        all_five=set(channels)=={'ex','ey','bx','by','bz'} and len(rr)==5 and all(r['status']=='identical_samples' for r in rr)
        intervals.append([station,'Overlap',short_group(left),short_group(right),start,end,rr[0]['duration_seconds'],
                          ', '.join(channels),'; '.join(f'{k}: {v}' for k,v in sorted(statuses.items())),'Yes' if all_five else 'No / not established'])
    for key,rr in sorted(gap_sets.items()):
        station,left,right,start,end=key
        intervals.append([station,'Gap',short_group(left),short_group(right),start,end,rr[0]['duration_seconds'],
                          ', '.join(sorted({r['channel'] for r in rr})),'Gap between reported/candidate intervals','Not applicable'])
    style(intervals,[13,12,38,38,25,25,14,22,42,28],intervals.max_row)
    expected={ws.title:[tuple(None if v=='' else v for v in row) for row in ws.values] for ws in wb}
    path=out/'qc_review.xlsx';wb.save(path)
    check=load_workbook(path,read_only=True,data_only=True)
    for ws in check:
        assert list(ws.values)==expected[ws.title], 'Workbook readback differs'
    check.close()
    manifest['qc_review_xlsx_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest['human_review_group_rows']=sum(s[0]['groups_requiring_review'] for s in results)
    manifest['channel_problems']=sum(s[0]['channels_empty']+s[0]['channels_with_errors_or_missing'] for s in results)
    manifest['all_channels_readable']=manifest['channel_problems']==0
    with (out/'qc_evidence.jsonl').open('x',encoding='utf-8',newline='\n') as f:
        f.write(json.dumps(manifest,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
        for summary,groups,overlaps,gaps,repeats in results:
            for record in [summary,*groups,*overlaps,*gaps,*repeats]:
                f.write(json.dumps(record,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
