"""Explicit survey naming and fingerprinted review rules."""
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path


def configuration():
    path=os.getenv('MTL0_CONFIG')
    return _load(path)


@lru_cache(maxsize=4)
def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else {}


def canonical_station(name):
    name=str(name).strip()
    aliases=configuration().get('station_aliases',{})
    return {k.casefold():v for k,v in aliases.items()}.get(name.casefold(),name)


def fingerprint(group):
    values={c:v.get('file_sha256') for c,v in sorted(group['channels'].items())}
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def checked_group_rule(group,rule):
    if not rule.get('reason') or not rule.get('reviewer'):
        raise ValueError('Reviewed exclusions need a reason and reviewer')
    if rule.get('qc_fingerprint')!=fingerprint(group):
        raise ValueError('Reviewed rule does not match current channel content: '+group['group_id'])
