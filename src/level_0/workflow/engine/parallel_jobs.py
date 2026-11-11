"""Station/task parallelism: static MPI across nodes; processes on one host."""
import os
import traceback
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

COMM=None


def enable_mpi():
    global COMM
    from mpi4py import MPI
    COMM=MPI.COMM_WORLD
    return COMM.Get_rank(),COMM.Get_size()


def _call(fn,args):
    try:return True,fn(*args)
    except Exception:return False,traceback.format_exc()


def worker_loop():
    while True:
        message=COMM.recv(source=0,tag=71)
        if message is None:return
        fn,jobs=message
        COMM.send([(i,_call(fn,args)) for i,args in jobs],dest=0,tag=72)


def stop_workers():
    if COMM is not None and COMM.Get_rank()==0:
        for rank in range(1,COMM.Get_size()):COMM.send(None,dest=rank,tag=71)


def map_jobs(fn,jobs,workers):
    jobs=list(jobs)
    if not jobs:return []
    if workers<1:raise ValueError('Workers must be positive')
    if COMM is not None and COMM.Get_size()>1:
        if COMM.Get_rank()!=0:raise RuntimeError('Only rank 0 schedules work')
        count=min(workers,COMM.Get_size(),len(jobs))
        chunks=[list(enumerate(jobs))[rank::count] for rank in range(count)]
        for rank in range(1,count):COMM.send((fn,chunks[rank]),dest=rank,tag=71)
        results=[(i,_call(fn,args)) for i,args in chunks[0]]
        for rank in range(1,count):results.extend(COMM.recv(source=rank,tag=72))
        errors=[value for _,(ok,value) in results if not ok]
        if errors:raise RuntimeError('Distributed task failed:\n'+'\n'.join(errors))
        return [value for _,(_,value) in sorted(results)]
    count=min(workers,len(jobs))
    if count==1:return [fn(*args) for args in jobs]
    if os.getenv('PBS_NODEFILE'):
        hosts=set(open(os.environ['PBS_NODEFILE']).read().splitlines())
        if len(hosts)>1:raise ValueError('Use --mpi for a multi-node PBS allocation')
    with ProcessPoolExecutor(max_workers=count,mp_context=multiprocessing.get_context('spawn')) as pool:
        return [f.result() for f in [pool.submit(fn,*args) for args in jobs]]
