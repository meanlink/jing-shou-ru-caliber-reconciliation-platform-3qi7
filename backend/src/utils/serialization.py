import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
def weekly_report_dict(r):
    return {'id':r.id,'name':r.name,'dataset_id':r.dataset_id,'filters':r.filters,'group_by':r.group_by,
            'audience':r.audience,'enabled':r.enabled,'next_run':r.next_run,'interval_seconds':r.interval_seconds,
            'status':r.status,'last_decision':r.last_decision,'created_at':r.created_at}
def weekly_delivery_dict(d):
    return {'id':d.id,'weekly_report_id':d.weekly_report_id,'audience':d.audience,'idempotency_key':d.idempotency_key,
            'status':d.status,'filename':d.filename,'checksum':d.checksum,'attempts':d.attempts,
            'decision':d.decision,'created_at':d.created_at,'updated_at':d.updated_at}
