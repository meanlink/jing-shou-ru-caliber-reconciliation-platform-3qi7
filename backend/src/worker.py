import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, WeeklyReport
from src.services.business import analyze
from src.services.reports import deliver
from src.services import weekly
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()
    tick_weekly()

def tick_weekly():
    # 单 worker 部署；Redis 锁保证同受众并发分发的串行化在多副本下也成立
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    lock=r.lock('weekly:publish:lock',timeout=30,blocking_timeout=2)
    if not lock.acquire(blocking=True):
        logging.warning('Weekly tick skipped: lock busy')
        return
    try:
        with Session() as db:
            due=list(db.scalars(select(WeeklyReport).where(
                WeeklyReport.enabled==True, WeeklyReport.next_run<=time.time()
            ).order_by(WeeklyReport.next_run.asc())))
            for wr in due:
                try:
                    d=weekly.publish_due(db,wr)
                    if d:record(db,'worker','weekly.publish',{'id':wr.id,'delivery':d.id,'status':d.status})
                except Exception:
                    logging.exception('Weekly publish failed for report %s',wr.id)
            db.commit()
    finally:
        try:lock.release()
        except Exception:logging.warning('Weekly lock already released')
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
