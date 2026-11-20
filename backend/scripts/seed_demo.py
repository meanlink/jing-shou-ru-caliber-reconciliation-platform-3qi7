import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, WeeklyReport
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import weekly
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        # 周报脱敏专用演示批次：含访问不足百次的渠道日，独立于 180 行规范数据集
        demo_name='周报脱敏演示_含低访问渠道日.csv'
        wd=db.scalar(select(Dataset).where(Dataset.name==demo_name))
        if not wd:
            demo_rows=[
                {'day':'2026-11-02','channel':'直营内容','currency':'CNY','gross':12000.0,'refunds':300.0,'discount':700.0,'recognized_net':11000.0,'visits':420},
                {'day':'2026-11-03','channel':'直营内容','currency':'CNY','gross':9800.0,'refunds':200.0,'discount':600.0,'recognized_net':9000.0,'visits':86},
                {'day':'2026-11-02','channel':'合作渠道','currency':'CNY','gross':8600.0,'refunds':180.0,'discount':420.0,'recognized_net':7845.2,'visits':305},
                {'day':'2026-11-03','channel':'合作渠道','currency':'CNY','gross':5400.0,'refunds':90.0,'discount':310.0,'recognized_net':4902.8,'visits':64},
                {'day':'2026-11-02','channel':'机构许可','currency':'CNY','gross':7300.0,'refunds':0.0,'discount':300.0,'recognized_net':7000.0,'visits':260},
                {'day':'2026-11-03','channel':'机构许可','currency':'CNY','gross':6900.0,'refunds':0.0,'discount':200.0,'recognized_net':6700.0,'visits':240},
            ]
            wd=Dataset(name=demo_name,source='weekly-masking-demo.csv',records=demo_rows,checksum=checksum(demo_rows),created_at=time.time())
            db.add(wd); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            # 经营周报示例：渠道日口径，立即评估一次，演示百次遮挡与推断拦截留痕
            if not db.scalar(select(WeeklyReport).where(WeeklyReport.dataset_id==wd.id)):
                wr=WeeklyReport(name='经营周报 · 渠道日净收入',dataset_id=wd.id,
                    filters={'channels':[],'date_from':None,'date_to':None},group_by='channel_day',
                    audience='经营周会 · 本地收件箱',enabled=True,next_run=time.time()+604800,
                    interval_seconds=604800,status='queued',created_by=admin.id,created_at=time.time())
                db.add(wr);db.flush()
                weekly.publish_due(db,wr,now=time.time())
                # 第二份同受众、换筛选（仅高访问渠道）的周报，演示并发/差值推断台账
                wr2=WeeklyReport(name='经营周报 · 高访问渠道筛选',dataset_id=wd.id,
                    filters={'channels':['机构许可'],'date_from':None,'date_to':None},group_by='channel_day',
                    audience='经营周会 · 本地收件箱',enabled=True,next_run=time.time()+604800,
                    interval_seconds=604800,status='queued',created_by=admin.id,created_at=time.time())
                db.add(wr2);db.flush()
                weekly.publish_due(db,wr2,now=time.time())
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,WeeklyReport,Audit]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
