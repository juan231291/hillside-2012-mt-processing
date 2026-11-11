"""Build the derived Level 0 workbook, retaining the source column schema."""
from collections import Counter,defaultdict
from datetime import datetime,date
from fractions import Fraction
import json
import re
import textwrap

from openpyxl import Workbook,load_workbook
from openpyxl.styles import Alignment,Font
from openpyxl.utils import get_column_letter


def require(condition,message):
    if not condition:
        raise ValueError(message)


def serializable(value):
    if isinstance(value,(datetime,date)):
        return value.isoformat()
    return value


def join_comments(original, additions):
    text='' if original is None else str(original).strip()
    for addition in dict.fromkeys(additions):
        if addition and addition not in text:
            separator=' ' if not text or text.endswith(('.', '!', '?', ';', ':')) else '. '
            text=(text+separator+addition).strip()
    return text or None


def build(original,context):
    from ascii_export import iso_ns
    from survey_settings import canonical_station
    source=load_workbook(original,data_only=False)
    candidates=[]
    for sheet in source:
        headers=[c.value for c in sheet[1]]
        if 'station.id' in headers and 'run.id' in headers:
            candidates.append((sheet,headers))
    require(len(candidates)==1,'Expected one metadata sheet with station.id and run.id headers')
    template,headers=candidates[0]
    require(all(isinstance(h,str) and h for h in headers),'Metadata has missing/non-text column headers')
    duplicates={h:n for h,n in Counter(headers).items() if n>1}
    require(set(duplicates)<= {'station.id'},'Unexpected repeated metadata headers')
    indices=defaultdict(list)
    for i,h in enumerate(headers):indices[h].append(i)
    mandatory={'station.id','station.channels_recorded','station.run_list','station.comments',
        'station.time_period.start','station.time_period.end','run.id','run.sample_rate',
        'run.time_period.start','run.time_period.end','run.channels_recorded_electric','run.channels_recorded_magnetic',
        'run.data_logger.id','electric.time_period.start','electric.time_period.end',
        'magnetic.time_period.start','magnetic.time_period.end'}
    require(mandatory<=set(headers),'Source metadata is missing required columns: '+', '.join(sorted(mandatory-set(headers))))
    stations=sorted({r['station'] for r in context['runs']})
    source_rows={}
    for row in template.iter_rows(min_row=2):
        ids=[row[i].value for i in indices['station.id']]
        if not any(ids):continue
        require(all(str(v).strip()==str(ids[0]).strip() for v in ids),'Duplicate station.id columns disagree')
        matches=[s for s in stations if canonical_station(str(ids[0]).strip()).casefold()==s.casefold()]
        if matches:
            station=matches[0]
            require(station not in source_rows,'Multiple original metadata rows match '+station)
            require(not any(c.data_type=='f' for c in row),'Formula-based source rows require a separate review')
            source_rows[station]=row
    require(set(source_rows)==set(stations),'Original metadata lacks a unique row for every exported station')
    from metadata_review import plan
    reviewed = plan(context, source_rows, indices)
    new_headers=list(headers)
    if 'run.comments' not in new_headers:new_headers.append('run.comments')
    new_indices=defaultdict(list)
    for i,h in enumerate(new_headers):new_indices[h].append(i)
    previous={r['group_id']:r for r in context['prior_decisions']}
    rows=[];changes=[];station_records=[];run_records=[]
    for station in stations:
        station_runs=sorted((r for r in context['runs'] if r['station']==station),key=lambda r:Fraction(r['plan']['start_ns']))
        ids=[r['run_id'] for r in station_runs]
        station_start=iso_ns(min(Fraction(r['plan']['start_ns']) for r in station_runs))
        station_end=iso_ns(max(Fraction(r['plan']['end_exclusive_ns'])-Fraction(10**9)/Fraction(r['plan']['rate_hz']) for r in station_runs))
        union=[c for c in ('ex','ey','bx','by','bz') if any(c in r['plan']['channels'] for r in station_runs)]
        source_row=source_rows[station]
        original_values=[c.value for c in source_row]
        base=original_values+[None]*(len(new_headers)-len(original_values))
        notes=[]
        for entry in context['prior_decisions']:
            d=entry['decision']
            if d['assigned_station']==station and d['decision']=='retain_for_qc' and d['metadata_comment']:
                notes.append(d['metadata_comment'])
            if d['source_folder']==station and d['decision']=='exclude_duplicate' and d['metadata_comment']:
                notes.append(d['metadata_comment'])
            if d['source_folder']==station and d['decision']=='exclude_reviewed':
                notes.append(d['metadata_comment'] or 'Reviewed recordings were excluded; see decision.csv for the reasons.')
        actions=Counter(s['action'] for s in context['selections'].values() if s['station']==station)
        if actions.get('exclude_startup'):notes.append('Initial lower-rate setup recordings were excluded.')
        if actions.get('exclude_timing_conflict'):
            notes.append('A recording with an unresolved timing conflict was excluded.' if actions['exclude_timing_conflict']==1
                         else 'Recordings with unresolved timing conflicts were excluded.')
        if actions.get('exclude_reviewed'):notes.append('Reviewed recordings were excluded; see the selection evidence for the reasons.')
        other={a for a,n in actions.items() if n and a not in ('include','exclude_startup','exclude_timing_conflict','exclude_reviewed')}
        require(not other,'Unhandled selection actions for metadata comments: '+', '.join(sorted(other)))
        logger=str(base[new_indices['run.data_logger.id'][0]]).strip()
        for export in station_runs:
            for gid in export['plan']['group_ids']:
                serials=set(filter(None,re.split(r'[;,\s]+',previous[gid]['recorder_review']['logged_serial_numbers'])))
                require(not serials or serials=={logger.lstrip('0') or '0'},'Recorder serial evidence differs from source metadata for '+gid)
        station_records.append({'record_type':'station_metadata_source','id':'metadata_source:'+station,
            'station':station,'source_sheet':template.title,'source_row':source_row[0].row,
            'original_values':[serializable(v) for v in original_values],
            'station_start_utc_assumed':station_start,'station_last_sample_utc_assumed':station_end,
            'run_ids':ids,'added_station_comments':list(dict.fromkeys(notes))})
        for export in station_runs:
            p=export['plan'];run_id=p['run_id'];values=list(base);output_row=len(rows)+2
            row_changes={}
            start=p['start_utc_assumed']
            end=iso_ns(Fraction(p['end_exclusive_ns'])-Fraction(10**9)/Fraction(p['rate_hz']))
            rate=Fraction(p['rate_hz']);require(Fraction(str(float(rate)))==rate,'Sampling rate cannot be represented exactly as an Excel number')
            channels=p['channels']
            electric=[c for c in channels if c.startswith('e')]
            magnetic=[c for c in channels if c.startswith('b') or c.startswith('h')]
            def set_value(field,value,reason):
                for i in new_indices[field]:
                    old=values[i]
                    if old!=value or type(old)!=type(value):
                        if i not in row_changes:
                            row_changes[i]={'record_type':'metadata_cell_change','id':f'cell:{station}/{run_id}/{i+1}',
                                'station':station,'run_id':run_id,'source_row':source_row[0].row,'metadata_row':output_row,
                                'column':i+1,'field':field,'old':serializable(base[i])}
                        row_changes[i].update(new=serializable(value),reason=reason)
                        values[i]=value
            set_value('station.id',station,'Use the reviewed canonical station identifier')
            set_value('station.channels_recorded',json.dumps(union),'Union of channels in retained runs')
            set_value('station.run_list',json.dumps(ids),'List the exported run identifiers')
            set_value('station.time_period.start',station_start,'Earliest retained sample for this station')
            set_value('station.time_period.end',station_end,'Latest retained sample for this station')
            set_value('station.comments',join_comments(base[new_indices['station.comments'][0]],notes),'Preserve source notes and append concise output-selection decisions')
            set_value('run.id',run_id,'Use the exported run identifier')
            set_value('run.sample_rate',int(rate) if rate.denominator==1 else float(rate),'Native rate from the verified run plan')
            set_value('run.time_period.start',start,'First sample of this exported run')
            set_value('run.time_period.end',end,'Last sample, not the exclusive end boundary')
            set_value('run.channels_recorded_electric',json.dumps(electric),'Available electric channels in this run')
            set_value('run.channels_recorded_magnetic',json.dumps(magnetic),'Available magnetic channels in this run')
            for family,present in (('electric',electric),('magnetic',magnetic)):
                set_value(family+'.time_period.start',start if present else None,'Time coverage of the exported channel family')
                set_value(family+'.time_period.end',end if present else None,'Last exported sample of the channel family')
                for c in present:
                    field=family+'.sample_rate_'+c
                    require(field in new_indices,'Missing per-channel sample-rate field: '+field)
                    set_value(field,int(rate) if rate.denominator==1 else float(rate),'Native exported channel sampling rate')
            missing=[c for c in ('ex','ey','bx','by','bz') if c not in channels]
            for field in new_headers:
                absent_family=(field.startswith('electric.') and not electric) or (field.startswith('magnetic.') and not magnetic)
                absent_channel=any(re.search('_'+re.escape(c)+r'(?:\.|$)',field) for c in missing)
                if absent_family or absent_channel:
                    set_value(field,None,'Channel absent from this run; preserve its original details only in the source record')
            run_notes=[]
            if 'sample_count_over_logged_duration_estimate' in p['rate_bases']:
                run_notes.append('Sampling rate inferred from sample counts and recorder durations.')
            if 'reviewed_workbook_and_filename_boundary_consistency' in p['rate_bases']:
                run_notes.append('Sampling rate accepted from the original workbook and consecutive filename/sample-count checks; no matching recorder duration was available.')
            run_notes.append('Timestamps assume UTC.')
            if missing:run_notes.append(', '.join(c.upper() for c in missing)+' absent from this exported run.')
            set_value('run.comments',join_comments(base[new_indices['run.comments'][0]],run_notes),'Document timing/rate basis and unavailable channels')
            for edit in reviewed.get(station, []):
                field = edit['field']
                # An absent channel stays blank even if its original station
                # metadata has a reviewed angle. Original values remain audited.
                if any(field.endswith('_' + c) for c in missing):
                    continue
                # Reviewed source comments retain the generated selection notes.
                value = join_comments(edit['value'], notes) if field == 'station.comments' else edit['value']
                # A station-level sensor-type correction cannot populate an absent family.
                if field.startswith('magnetic.') and not magnetic:
                    continue
                set_value(field, value, 'Reviewed metadata: ' + edit['reason'])
            # One evidence ID per final cell, measured from the original source.
            # A reviewed comment may replace a value already carrying selection
            # notes. Keep its original-to-final change, not duplicate event IDs.
            changes.extend(change for i,change in row_changes.items()
                           if base[i]!=values[i] or type(base[i])!=type(values[i]))
            rows.append({'values':values,'source_cells':source_row,'station':station,'run_id':run_id})
            run_records.append({'record_type':'run_metadata_row','id':f'metadata:{station}/{run_id}',
                'station':station,'run_id':run_id,'metadata_sheet':template.title,'metadata_row':output_row,
                'source_row':source_row[0].row,'channels':channels,'samples_per_channel':p['samples_per_channel'],
                'rate_hz':p['rate_hz'],'duration_seconds':p['duration_seconds'],
                'start_utc_assumed':start,'last_sample_utc_assumed':end,
                'end_exclusive_utc_assumed':p['end_exclusive_utc_assumed'],
                'ascii_file':export['relative_path'],'ascii_sha256':export['ascii_sha256'],
                'values':[serializable(v) for v in values]})
    return {'source_workbook':source,'source_sheet':template,'source_headers':headers,'headers':new_headers,
            'rows':rows,'station_records':station_records,'run_records':run_records,'changes':changes}


def write(path,built,original_name):
    wb=Workbook();wb.remove(wb.active)
    rr=built['run_records']
    overview=wb.create_sheet('Run overview')
    overview.append(['Station','Run','Start UTC assumed','Last sample UTC assumed',
        'Duration seconds','Samples per channel','Native rate Hz','Channels','ASCII file'])
    for r in rr:
        overview.append([r['station'],r['run_id'],r['start_utc_assumed'],r['last_sample_utc_assumed'],
            float(Fraction(r['duration_seconds'])),r['samples_per_channel'],float(Fraction(r['rate_hz'])),
            ', '.join(c.upper() for c in r['channels']),r['ascii_file']])
    for i,width in enumerate([13,15,29,29,19,22,18,25,44],1):
        overview.column_dimensions[get_column_letter(i)].width=width
    for cells in overview:
        for cell in cells:
            cell.font=Font(name='Arial',size=10,color='000000')
            cell.alignment=Alignment(vertical='top',wrap_text=True)
    for cells in overview.iter_rows(min_row=2,min_col=5,max_col=7):
        for cell in cells:
            cell.number_format='#,##0' if float(cell.value).is_integer() else '#,##0.000'
    overview.freeze_panes='B2'
    overview.sheet_view.showGridLines=True
    overview.auto_filter.ref=overview.dimensions
    table=wb.create_sheet(built['source_sheet'].title)
    table.append(built['headers'])
    for r in built['rows']:table.append(r['values'])
    for i,h in enumerate(built['headers'],1):
        if 'comments' in h or h.endswith('.summary'):width=100
        elif 'time_period' in h:width=30
        elif h.endswith('.doi') or '.pid' in h:width=46
        elif 'channels_recorded' in h or h=='station.run_list':width=45
        elif h.endswith('.id') or h in ('run.sample_rate',):width=19
        else:width=30
        table.column_dimensions[get_column_letter(i)].width=width
        cell=table.cell(1,i);cell.font=Font(name='Arial',size=10,color='000000')
        cell.alignment=Alignment(wrap_text=True,vertical='center')
    table.row_dimensions[1].height=62
    for row_index,row in enumerate(built['rows'],2):
        height=44
        for i,value in enumerate(row['values'],1):
            c=table.cell(row_index,i)
            if i<=len(row['source_cells']):c.number_format=row['source_cells'][i-1].number_format
            c.font=Font(name='Arial',size=10,color='000000')
            c.alignment=Alignment(wrap_text=True,vertical='top',
                horizontal='right' if isinstance(value,(int,float)) else 'left')
            if isinstance(value,str):c.data_type='s'
            width=table.column_dimensions[get_column_letter(i)].width
            lines=sum(max(1,len(textwrap.wrap(s,width=max(8,int(width*.85))))) for s in str(value or '').split('\n'))
            height=max(height,lines*14+16)
        table.row_dimensions[row_index].height=min(409,height)
    table.freeze_panes='B2';table.sheet_view.showGridLines=True
    table.auto_filter.ref=f'A1:{get_column_letter(len(built["headers"]))}{table.max_row}'
    expected={ws.title:[tuple(None if v=='' else v for v in row) for row in ws.values] for ws in wb}
    wb.save(path)
    check=load_workbook(path,read_only=True,data_only=False)
    try:
        actual={ws.title:list(ws.values) for ws in check}
        require(actual==expected,'Saved metadata workbook differs from constructed values')
        require(not any(c.data_type=='f' for ws in check for row in ws for c in row),'Unexpected metadata formula')
    finally:check.close()
    return {'sheet_names':list(expected),'metadata_sheet':table.title,'metadata_row_count':len(built['rows'])}
