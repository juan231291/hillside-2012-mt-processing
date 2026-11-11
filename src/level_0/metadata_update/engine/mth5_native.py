"""Map reviewed MT survey metadata to mt_metadata 1.0.4 without sample processing."""
from enum import Enum
import math
import re
from pydantic import AnyUrl, ValidationError
from mt_metadata.common import Comment
from mt_metadata.common.mttime import MTime
from mt_metadata.timeseries import Electric, Magnetic, Run, Station, Survey
from station_metadata_json import require

COLLECTIONS = {'channels','runs','stations','filters','run_list','channels_recorded',
               'channels_recorded_electric','channels_recorded_magnetic','channels_recorded_auxiliary'}


def flatten(values, prefix=''):
    for key,value in values.items():
        name = prefix+'.'+key if prefix else key
        if isinstance(value,dict):
            yield from flatten(value,name)
        else:
            yield name,value


def get(obj, path):
    for part in path.split('.'):
        obj = getattr(obj,part)
    return obj


def plain(value):
    if isinstance(value,Comment):return value.value
    if isinstance(value,MTime):return value.isoformat()
    if isinstance(value,AnyUrl):return str(value)
    if isinstance(value,Enum):return value.value
    if isinstance(value,float) and math.isnan(value):return {'unknown_numeric':True}
    if isinstance(value,(str,int,float,bool)) or value is None:return value
    if isinstance(value,(list,tuple)):return [plain(v) for v in value]
    raise TypeError('Unexpected native metadata value: '+str(type(value)))


def map_object(obj, source, kind):
    expected, source_only, assigned, normalizations, omitted = {}, [], set(), [], []
    for original,value in flatten(source):
        if original.split('.')[0] in COLLECTIONS:
            continue
        path = 'acquired_by.author' if kind == 'station' and original == 'acquired_by.name' else original
        try:
            current = get(obj,path)
        except AttributeError:
            source_only.append({'field':original,'value':value,'reason':'No corresponding native attribute'})
            continue
        if path.endswith('.doi') and value is not None and re.search(r'10\.\d{4,9}/\S+',str(value)) is None:
            source_only.append({'field':original,'value':value,'reason':'Source URL is not a DOI'})
            continue
        if value is None:
            if isinstance(current,float):
                obj.update_attribute(path,float('nan'))
                expected[path] = plain(get(obj,path))
            else:
                source_only.append({'field':original,'value':None,'reason':'Unknown in source; source JSON remains authoritative'})
            continue
        if isinstance(current,Comment):
            given = Comment(value=str(value).replace('|',';'))
        elif kind == 'channel' and path == 'units':
            require(value == 'ADC counts','Only unscaled ADC counts are supported')
            given = 'digital counts'
        else:
            given = value
        obj.update_attribute(path,given)
        assigned.add(path)
        expected[path] = plain(get(obj,path))
        if original != path or expected[path] != value:
            normalizations.append({'source_field':original,'native_field':path,'source_value':value,'native_value':expected[path]})
    # Unprovided floating-point fields are unknown, not measured zeros. This also
    # covers sensor/electrode locations and logger timing drift/uncertainty.
    for path,value in obj.to_dict(single=True,required=False).items():
        if path in assigned or path.split('.')[0] in COLLECTIONS:
            continue
        try:
            current = get(obj,path)
        except AttributeError:
            continue
        if isinstance(current,float):
            try:
                obj.update_attribute(path,float('nan'))
                expected[path] = plain(get(obj,path))
            except ValidationError:
                omitted.append(path)
    defaults = {k:plain(v) for k,v in obj.to_dict(single=True,required=False).items()
                if k not in expected and k.split('.')[0] not in COLLECTIONS and isinstance(v,(str,int,float,bool))}
    return {'expected_native_values':expected,'source_only_fields':source_only,'omitted_unknown_native_fields':omitted,
            'normalizations':normalizations,'unprovided_schema_defaults':defaults}


def native_station(record):
    survey = Survey(id=record['survey']['name'])
    station = Station(id=record['station_id'])
    survey_source = {**record['survey'],'id':record['survey']['name']}
    # The file contains one station. Its bounding box is derived from that
    # station's coordinates; the full survey date range is retained separately.
    location = record['station']['location']
    for corner in ('northwest_corner','southeast_corner'):
        survey_source[corner] = {k:location[k] for k in ('latitude','longitude','elevation')}
    station_source = {**record['station'],'location':{**location,'datum':record['survey']['datum']}}
    mapped = {'survey':map_object(survey,survey_source,'survey'),
              'station':map_object(station,station_source,'station'),'runs':{}}
    runs = {}
    for info in record['runs']:
        source = {k:v for k,v in info.items() if k not in ('channels','ascii','sample_coverage','deployment_measurements')}
        # Battery type belongs to the logger field, while measured ID/voltages
        # are supplied by the deployment measurements in this workbook.
        source = {**source,'data_logger':{**source['data_logger'],
                  'power_source':{**source['data_logger'].get('power_source',{}),
                      **{k:v for k,v in info['deployment_measurements']['battery'].items() if v is not None}}}}
        run = Run(id=info['id'],sample_rate=info['sample_rate'])
        mapping = map_object(run,source,'run')
        channels = {}
        channel_maps = {}
        for c,values in info['channels'].items():
            channel = Electric() if c.startswith('e') else Magnetic()
            channel_maps[c] = map_object(channel,values,'channel')
            require(channel.filters == [],'Unexpected channel response filters')
            channels[c] = channel
        runs[info['id']] = {'metadata':run,'channels':channels}
        mapped['runs'][info['id']] = {**mapping,'channels':channel_maps}
    return survey,station,runs,mapped


def verify_native(obj,mapping,label):
    for path,expected in mapping['expected_native_values'].items():
        actual = plain(get(obj,path))
        require(actual == expected,f'Native metadata differs at {label}.{path}: {actual!r} != {expected!r}')
