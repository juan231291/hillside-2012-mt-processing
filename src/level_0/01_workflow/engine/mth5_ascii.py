"""Read Step 07 tabular ASCII in bounded blocks with exact int64 verification."""
import hashlib
from pathlib import Path
import re
import warnings
import numpy as np
from station_metadata_json import require


def header_for(record, run, selection_hash):
    c = run['sample_coverage']
    fields = [('schema','mtl0.ascii_run/1'), ('station',record['station_id']), ('run_id',run['id']),
        ('start_utc_assumed',c['start_utc_assumed']), ('end_exclusive_utc_assumed',c['end_exclusive_utc_assumed']),
        ('last_sample_utc_assumed',c['last_sample_utc_assumed']), ('sample_rate_hz',f'{run["sample_rate"]:g}'),
        ('samples_per_channel',c['samples_per_channel']), ('columns',' '.join(x.upper() for x in run['ascii']['column_order'])),
        ('sample_representation','signed_int64'), ('values','unscaled source samples'),
        ('rate_basis','; '.join(c['rate_evidence'])),
        ('time_index','zero-based sample i occurs at start + i/sample_rate_hz'),
        ('selection_evidence_sha256',selection_hash)]
    require(all('\n' not in str(v) and '\r' not in str(v) for _,v in fields), 'Invalid header value')
    return ''.join(f'# {k}: {v}\n' for k,v in fields).encode('ascii')


def stamp(path):
    s = Path(path).stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


class CheckedAscii:
    def __init__(self, path, header, run, block_bytes=1024**2):
        self.path, self.header, self.run = Path(path), header, run
        self.block_bytes = block_bytes
        require(1024 <= block_bytes <= 16*1024**2, 'ASCII block size must be between 1 KiB and 16 MiB')
        self.receipt = None

    def blocks(self):
        require(self.receipt is None, 'ASCII reader is single-use')
        before = stamp(self.path)
        order = self.run['ascii']['column_order']
        token = rb'-?(?:0|[1-9][0-9]{0,18})'
        row = token + (rb'\t' + token)*(len(order)-1) + rb'\n'
        valid = re.compile(rb'(?:' + row + rb')+')
        total = 0
        file_hash = hashlib.sha256()
        hashes = {c:hashlib.sha256() for c in order}
        with self.path.open('rb') as stream:
            found = stream.read(len(self.header))
            require(found == self.header, 'ASCII header differs: ' + str(self.path))
            file_hash.update(found)
            while True:
                block = stream.read(self.block_bytes)
                if not block:
                    break
                if not block.endswith(b'\n'):
                    block += stream.readline(256)
                require(valid.fullmatch(block) is not None, 'Invalid ASCII row, column count or final newline')
                file_hash.update(block)
                rows = block.count(b'\n')
                if re.search(rb'[0-9]{19}', block):
                    integers = [int(v) for v in block.split()]
                    require(all(-(2**63) <= v <= 2**63-1 for v in integers), 'ASCII integer overflow')
                    data = np.asarray(integers, dtype='<i8')
                else:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error')
                        data = np.fromstring(block.decode('ascii'), dtype='<i8', sep=' ')
                require(data.size == rows*len(order), 'ASCII row/column count differs')
                data = data.reshape(rows, len(order))
                total += rows
                require(total <= self.run['sample_coverage']['samples_per_channel'], 'ASCII has extra samples')
                for i,c in enumerate(order):
                    hashes[c].update(data[:,i].astype('<i8',copy=False).tobytes())
                yield data
        observed = {c:h.hexdigest() for c,h in hashes.items()}
        require(total == self.run['sample_coverage']['samples_per_channel'], 'ASCII has missing samples')
        require(file_hash.hexdigest() == self.run['ascii']['sha256'], 'ASCII file checksum differs')
        require(observed == self.run['ascii']['sample_sha256_by_channel'], 'ASCII sample values differ')
        require(stamp(self.path) == before, 'ASCII file changed during reading')
        self.receipt = {'run_id':self.run['id'], 'relative_path':self.run['ascii']['relative_path'],
                        'ascii_sha256':file_hash.hexdigest(), 'samples_per_channel':total,
                        'sample_sha256_by_channel':observed, 'header_verified':True,
                        'source_unchanged_during_read':True}
