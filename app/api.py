from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from datetime import UTC,datetime

from app.archive.appeal import issue
from app.archive.rerun import run
from app.clearing import Clearing,STAGES
from app.problems import DocketError
from app.ruling import award_minutes,groups,plan,reconcile,supplement_of
from app.source.packet import bundle

api=APIRouter(prefix='/api')


def case(request:Request,sample:str):
    if sample not in {'dirty','clean'}:
        raise DocketError('SAMPLE_BAD','找不到选定的清算样例',404)
    docket=request.app.state.docket
    return docket,Clearing(bundle(sample),docket.secret)


@api.get('/cases/{sample}/{stage}')
def preview(request:Request,sample:str,stage:str):
    if stage not in STAGES:
        raise DocketError('STAGE_BAD','没有这项清算工序',404)
    data=Clearing(bundle(sample),request.app.state.docket.secret)
    return getattr(data,stage)


@api.post('/dockets')
def archive(request:Request,payload:dict):
    record,reused=run(request.app.state.docket,payload)
    return {'key':record['manifest']['key'],'reused':reused,'manifest':record['manifest'],
            'totals':record['evidence']['settlement']['totals'],'money':record['evidence']['settlement']['money']}


@api.get('/dockets')
def history(request:Request):
    return {'dockets':request.app.state.docket.manifests()}


@api.get('/dockets/{key}')
def read(request:Request,key:str):
    return request.app.state.docket.require(key)


@api.get('/export/{key}')
def export(request:Request,key:str):
    item=issue(request.app.state.docket.require(key))
    return JSONResponse(item,headers={'Content-Disposition':f'attachment; filename="appeal-{key}.json"'})


@api.post('/rulings')
def submit_ruling(request:Request,payload:dict):
    sample=str(payload.get('sample',''))
    docket,clearing=case(request,sample)
    vehicle=str(payload.get('vehicle','')).strip()
    shift=str(payload.get('shift','')).strip()
    if not vehicle or not shift:
        raise DocketError('RULING_BAD','需要车辆代号与裁定班次')
    decided=None
    if payload.get('decided_at'):
        try:
            decided=datetime.fromisoformat(str(payload['decided_at']).replace('Z','+00:00'))
            if not decided.tzinfo:
                raise ValueError
            decided=decided.astimezone(UTC)
        except ValueError:
            raise DocketError('RULING_BAD','裁定时刻无效') from None
    ruling=plan(clearing,sample,str(payload.get('dispute_key','')),vehicle,shift,decided)
    book=reconcile(clearing,sample,[*docket.rulings_for(sample),ruling])
    supplement=supplement_of(ruling,book['rows'][-1])
    record,posted,reused=docket.rule(ruling,supplement,award_minutes(ruling))
    return {'reused':reused,'ruling':record,'supplement':posted,'reconciliation':book['rows'][-1]}


@api.get('/rulings/{sample}')
def list_rulings(request:Request,sample:str):
    docket,clearing=case(request,sample)
    stored=docket.rulings_for(sample)
    return {'disputes':groups(clearing.occupancy['disputes'],sample),'rulings':stored,
            'reconciliation':reconcile(clearing,sample,stored)}
