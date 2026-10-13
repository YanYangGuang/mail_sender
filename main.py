from __future__ import annotations
import csv, io, os, re, smtplib, ssl, threading, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from fastapi import FastAPI, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, StrictUndefined, select_autoescape
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select, func, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sample_data import seed_demo_data

BASE=Path(__file__).resolve().parent; DATA=BASE/'data'; DATA.mkdir(exist_ok=True)
DB_URL=os.getenv('MAIL_DB_URL',f"sqlite:///{(DATA/'mail_sender.db').as_posix()}")
engine=create_engine(DB_URL,connect_args={'check_same_thread':False} if DB_URL.startswith('sqlite') else {})
Session=sessionmaker(engine,expire_on_commit=False); views=Jinja2Templates(directory=str(BASE/'templates'))
jinja=Environment(undefined=StrictUndefined,autoescape=select_autoescape(default=True))
EMAIL=re.compile(r'^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$')
def now(): return datetime.now(timezone.utc)
@asynccontextmanager
async def lifespan(application):
    start_worker()
    try: yield
    finally: stop_event.set()
app=FastAPI(title='本地邮件发送台',lifespan=lifespan)
class Base(DeclarativeBase): pass
class Recipient(Base):
    __tablename__='recipients'; id:Mapped[int]=mapped_column(primary_key=True); email:Mapped[str]=mapped_column(String(320),unique=True,index=True); name:Mapped[str]=mapped_column(String(200),default=''); tags:Mapped[str]=mapped_column(String(500),default=''); active:Mapped[bool]=mapped_column(Boolean,default=True,server_default=text('1')); deleted:Mapped[bool]=mapped_column(Boolean,default=False,server_default=text('0')); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class MailTemplate(Base):
    __tablename__='templates'; id:Mapped[int]=mapped_column(primary_key=True); name:Mapped[str]=mapped_column(String(200)); subject:Mapped[str]=mapped_column(String(500)); html_body:Mapped[str]=mapped_column(Text); text_body:Mapped[str]=mapped_column(Text); deleted:Mapped[bool]=mapped_column(Boolean,default=False,server_default=text('0'))
class SMTPConfig(Base):
    __tablename__='smtp_configs'; id:Mapped[int]=mapped_column(primary_key=True); name:Mapped[str]=mapped_column(String(120),default='SMTP'); host:Mapped[str]=mapped_column(String(255)); port:Mapped[int]=mapped_column(Integer); username:Mapped[str]=mapped_column(String(320)); password:Mapped[str]=mapped_column(Text); from_name:Mapped[str]=mapped_column(String(200),default=''); from_email:Mapped[str]=mapped_column(String(320)); use_tls:Mapped[bool]=mapped_column(Boolean,default=True); enabled:Mapped[bool]=mapped_column(Boolean,default=True); is_default:Mapped[bool]=mapped_column(Boolean,default=False)
class Campaign(Base):
    __tablename__='campaigns'; id:Mapped[int]=mapped_column(primary_key=True); name:Mapped[str]=mapped_column(String(200)); template_id:Mapped[int]=mapped_column(ForeignKey('templates.id')); smtp_id:Mapped[int]=mapped_column(ForeignKey('smtp_configs.id')); status:Mapped[str]=mapped_column(String(30),default='queued',index=True); scheduled_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); interval_seconds:Mapped[int]=mapped_column(Integer,default=2); archived:Mapped[bool]=mapped_column(Boolean,default=False,server_default=text('0')); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class SendQueue(Base):
    __tablename__='send_queue'; __table_args__=(UniqueConstraint('campaign_id','recipient_id',name='uq_queue_campaign_recipient'),); id:Mapped[int]=mapped_column(primary_key=True); campaign_id:Mapped[int]=mapped_column(ForeignKey('campaigns.id'),index=True); recipient_id:Mapped[int]=mapped_column(ForeignKey('recipients.id'),index=True); smtp_id:Mapped[int]=mapped_column(ForeignKey('smtp_configs.id')); status:Mapped[str]=mapped_column(String(30),default='pending',index=True); retries:Mapped[int]=mapped_column(Integer,default=0); error_type:Mapped[str]=mapped_column(String(100),default=''); error_summary:Mapped[str]=mapped_column(String(500),default=''); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); attempted_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); accepted_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class CampaignDetail(Base):
    __tablename__='campaign_details'; id:Mapped[int]=mapped_column(primary_key=True); queue_id:Mapped[int]=mapped_column(Integer,unique=True,index=True); campaign_id:Mapped[int]=mapped_column(ForeignKey('campaigns.id'),index=True); email:Mapped[str]=mapped_column(String(320)); name:Mapped[str]=mapped_column(String(200),default=''); status:Mapped[str]=mapped_column(String(30),index=True); retries:Mapped[int]=mapped_column(Integer,default=0); error_type:Mapped[str]=mapped_column(String(100),default=''); error_summary:Mapped[str]=mapped_column(String(500),default=''); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); attempted_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); accepted_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class Suppression(Base):
    __tablename__='suppressions'; id:Mapped[int]=mapped_column(primary_key=True); email:Mapped[str]=mapped_column(String(320),unique=True,index=True); reason:Mapped[str]=mapped_column(String(80)); source:Mapped[str]=mapped_column(String(200),default='管理员'); active:Mapped[bool]=mapped_column(Boolean,default=True,server_default=text('1')); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class Event(Base):
    __tablename__='events'; id:Mapped[int]=mapped_column(primary_key=True); kind:Mapped[str]=mapped_column(String(80)); detail:Mapped[str]=mapped_column(String(1000),default=''); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
def initialize():
    Base.metadata.create_all(engine)
    # Small additive migrations preserve existing local databases and records.
    if engine.dialect.name == 'sqlite':
        additions = {'recipients': {'active': 'BOOLEAN NOT NULL DEFAULT 1', 'deleted': 'BOOLEAN NOT NULL DEFAULT 0'}, 'templates': {'deleted': 'BOOLEAN NOT NULL DEFAULT 0'}, 'campaigns': {'archived': 'BOOLEAN NOT NULL DEFAULT 0'}, 'suppressions': {'active': 'BOOLEAN NOT NULL DEFAULT 1'}}
        with engine.begin() as conn:
            for table, columns in additions.items():
                existing = {column['name'] for column in inspect(conn).get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing: conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
    # Fill missing immutable task-detail snapshots from the old queue table after upgrades.
    with Session.begin() as db:
        existing={row[0] for row in db.execute(select(CampaignDetail.queue_id))}
        for item,recipient in db.execute(select(SendQueue,Recipient).join(Recipient)):
            if item.id not in existing:
                db.add(CampaignDetail(queue_id=item.id,campaign_id=item.campaign_id,email=recipient.email,name=recipient.name,status=item.status,retries=item.retries,error_type=item.error_type,error_summary=item.error_summary,created_at=item.created_at,attempted_at=item.attempted_at,accepted_at=item.accepted_at))
        # A message interrupted during SMTP DATA has ambiguous delivery; never auto-resend it.
        for item in db.scalars(select(SendQueue).where(SendQueue.status=='sending')).all():
            item.status='uncertain'; item.error_type='restart'; item.error_summary='程序重启时提交结果无法确认，请人工核对'
            recipient=db.get(Recipient,item.recipient_id)
            detail=db.scalar(select(CampaignDetail).where(CampaignDetail.queue_id==item.id))
            if detail:
                detail.status=item.status; detail.error_type=item.error_type; detail.error_summary=item.error_summary; detail.attempted_at=item.attempted_at
        db.query(Campaign).filter(Campaign.status=='running').update({Campaign.status:'paused'})
initialize()
def sync_campaign_detail(db,item,recipient):
    detail=db.scalar(select(CampaignDetail).where(CampaignDetail.queue_id==item.id))
    if detail is None:
        detail=CampaignDetail(queue_id=item.id,campaign_id=item.campaign_id,email=recipient.email,name=recipient.name,created_at=item.created_at)
        db.add(detail)
    detail.status=item.status; detail.retries=item.retries; detail.error_type=item.error_type; detail.error_summary=item.error_summary; detail.attempted_at=item.attempted_at; detail.accepted_at=item.accepted_at
def render(source,context): return jinja.from_string(source).render(**context)
def send_message(cfg,recipient,subject,html,text):
    if not EMAIL.match(recipient) or not EMAIL.match(cfg.from_email): raise ValueError('收件人或发件邮箱格式无效')
    if len(html.encode('utf-8'))>100*1024: raise ValueError('HTML 正文超过 100KB')
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=f'{cfg.from_name} <{cfg.from_email}>' if cfg.from_name else cfg.from_email; msg['To']=recipient
    msg.set_content(text or '此邮件含 HTML 内容。'); msg.add_alternative(html or f'<pre>{text}</pre>',subtype='html')
    context=ssl.create_default_context(); smtp=smtplib.SMTP_SSL(cfg.host,cfg.port,context=context,timeout=20) if cfg.port==465 else smtplib.SMTP(cfg.host,cfg.port,timeout=20)
    with smtp:
        smtp.ehlo()
        if cfg.port==587 or (cfg.use_tls and cfg.port!=465): smtp.starttls(context=context); smtp.ehlo()
        smtp.login(cfg.username,cfg.password); smtp.send_message(msg)

worker_lock=threading.Lock(); worker_started=False; stop_event=threading.Event()
def worker_loop():
    while not stop_event.is_set():
        if not worker_lock.acquire(False): time.sleep(.5); continue
        try:
            with Session() as db:
                campaign=db.scalar(select(Campaign).where(Campaign.status.in_(['queued','running'])).order_by(Campaign.id).limit(1))
                if campaign is None: time.sleep(.5); continue
                if campaign.scheduled_at:
                    schedule=campaign.scheduled_at.replace(tzinfo=timezone.utc) if campaign.scheduled_at.tzinfo is None else campaign.scheduled_at
                    if schedule>now(): time.sleep(1); continue
                campaign.status='running'; db.commit()
                item=db.scalar(select(SendQueue).where(SendQueue.campaign_id==campaign.id,SendQueue.status=='pending').order_by(SendQueue.id).limit(1))
                if item is None: campaign.status='completed'; db.commit(); continue
                rec=db.get(Recipient,item.recipient_id); cfg=db.get(SMTPConfig,item.smtp_id); tmpl=db.get(MailTemplate,campaign.template_id)
                if not rec.active:
                    item.status='cancelled'; item.error_type='recipient_inactive'; item.error_summary='收件人已停用，跳过发送'; sync_campaign_detail(db,item,rec); db.commit(); continue
                if db.scalar(select(Suppression.id).where(Suppression.email==rec.email,Suppression.active.is_(True))):
                    item.status='cancelled'; item.error_type='suppressed'; item.error_summary='命中抑制名单'; sync_campaign_detail(db,item,rec); db.commit(); continue
                item.status='sending'; item.attempted_at=now(); sync_campaign_detail(db,item,rec); db.commit()
                try:
                    if cfg is None or not cfg.enabled: raise ValueError('SMTP 配置不存在或未启用')
                    ctx={'name':rec.name,'email':rec.email,'tags':rec.tags}; send_message(cfg,rec.email,render(tmpl.subject,ctx),render(tmpl.html_body,ctx),render(tmpl.text_body,ctx))
                    item.status='submitted'; item.accepted_at=now(); item.error_summary='SMTP 已接受提交（不代表最终送达）'
                except smtplib.SMTPAuthenticationError as e: item.status='failed'; item.error_type='authentication'; item.error_summary=f'SMTP 认证失败（{e.smtp_code}）'
                except Exception as e:
                    item.status='uncertain' if isinstance(e,(TimeoutError,ConnectionError,smtplib.SMTPServerDisconnected)) else 'failed'; item.error_type=type(e).__name__[:100]; item.error_summary=str(e)[:500]
                sync_campaign_detail(db,item,rec); db.commit(); time.sleep(max(1,min(campaign.interval_seconds,3600)))
        finally: worker_lock.release()
def start_worker():
    global worker_started
    if not worker_started: threading.Thread(target=worker_loop,name='mail-queue',daemon=True).start(); worker_started=True
def page(request,title,**context): return views.TemplateResponse(request=request,name='page.html',context={'request':request,'title':title,**context})
@app.get('/',response_class=HTMLResponse)
def dashboard(request:Request):
    with Session() as db:
        count=lambda model,condition=None: db.scalar(select(func.count()).select_from(model).where(condition) if condition is not None else select(func.count()).select_from(model))
        counts={'收件人':count(Recipient,Recipient.active.is_(True)),'模板':count(MailTemplate),'任务':count(Campaign),'待发送':count(SendQueue,SendQueue.status=='pending'),'已提交':count(SendQueue,SendQueue.status=='submitted'),'提交失败':count(SendQueue,SendQueue.status=='failed'),'已取消/跳过':count(SendQueue,SendQueue.status=='cancelled'),'结果待确认':count(SendQueue,SendQueue.status=='uncertain'),'抑制名单':count(Suppression,Suppression.active.is_(True))}
        return page(request,'仪表盘',counts=counts)
@app.post('/demo-data')
def load_demo_data():
    with Session.begin() as db:
        seed_demo_data(db,Recipient,MailTemplate,Suppression,select)
    return RedirectResponse('/?notice=演示收件人、模板和退订抑制记录已准备好；请勿向示例域名发送真实邮件',303)
@app.get('/recipients',response_class=HTMLResponse)
def recipients_page(request:Request,q:str='',include_inactive:bool=False,include_deleted:bool=False):
    with Session() as db:
        stmt=select(Recipient).where(Recipient.email.contains(q)|Recipient.name.contains(q))
        if not include_inactive and not include_deleted: stmt=stmt.where(Recipient.active.is_(True))
        if not include_deleted: stmt=stmt.where(Recipient.deleted.is_(False))
        return page(request,'收件人',rows=db.scalars(stmt.order_by(Recipient.id.desc()).limit(500)).all(),include_inactive=include_inactive,include_deleted=include_deleted)
@app.post('/recipients/import')
async def import_csv(file:UploadFile=File(...)):
    added=duplicate=invalid=suppressed=0
    with Session() as db:
        reader=csv.DictReader(io.TextIOWrapper(file.file,encoding='utf-8-sig',newline=''))
        if not reader.fieldnames or 'email' not in [h.strip().lower() for h in reader.fieldnames]: raise HTTPException(400,'CSV 必须包含 email 列')
        for row in reader:
            values={k.strip().lower():(v or '').strip() for k,v in row.items() if k}; email=values.get('email','').lower()
            if not EMAIL.match(email): invalid+=1; continue
            if db.scalar(select(Suppression.id).where(Suppression.email==email)): suppressed+=1; continue
            try: db.add(Recipient(email=email,name=values.get('name','')[:200],tags=values.get('tags','')[:500])); db.commit(); added+=1
            except Exception: db.rollback(); duplicate+=1
    return RedirectResponse(f'/recipients?notice=新增{added}、重复{duplicate}、无效{invalid}、抑制{suppressed}',303)
@app.post('/recipients/add')
def add_recipient(email:str=Form(...),name:str=Form(''),tags:str=Form('')):
    email=email.strip().lower()
    if not EMAIL.match(email): raise HTTPException(400,'邮箱格式无效')
    try:
        with Session.begin() as db:
            db.add(Recipient(email=email,name=name[:200],tags=tags[:500]))
    except IntegrityError:
        return RedirectResponse('/recipients?notice='+quote('该邮箱已存在，请勿重复添加'),303)
    return RedirectResponse('/recipients',303)
@app.post('/recipients/{recipient_id}/update')
def update_recipient(recipient_id:int,name:str=Form(''),tags:str=Form('')):
    with Session.begin() as db:
        recipient=db.get(Recipient,recipient_id)
        if recipient is None: raise HTTPException(404,'收件人不存在')
        recipient.name=name.strip()[:200]; recipient.tags=tags.strip()[:500]
    return RedirectResponse('/recipients?notice=收件人信息已保存',303)
@app.post('/recipients/{recipient_id}/{action}')
def recipient_action(recipient_id:int,action:str):
    with Session.begin() as db:
        recipient=db.get(Recipient,recipient_id)
        if recipient is None: raise HTTPException(404,'收件人不存在')
        if action=='deactivate': recipient.active=False
        elif action=='restore': recipient.active=True
        elif action=='delete':
            if db.scalar(select(SendQueue.id).where(SendQueue.recipient_id==recipient_id).limit(1)):
                recipient.active=False; recipient.deleted=True
            else: db.delete(recipient)
        elif action=='undelete': recipient.deleted=False; recipient.active=True
        else: raise HTTPException(400,'操作无效')
    notice='收件人已删除；发送任务历史中的地址和详情仍保留。' if action=='delete' else ('已恢复收件人' if action=='undelete' else '收件人状态已更新')
    return RedirectResponse('/recipients?notice='+quote(notice),303)
@app.post('/recipients-bulk/{action}')
def bulk_recipient_action(action:str,recipient_ids:list[int]=Form(...)):
    if action not in {'deactivate','delete'}: raise HTTPException(400,'批量操作无效')
    selected=set(recipient_ids)
    changed=0
    with Session.begin() as db:
        recipients=db.scalars(select(Recipient).where(Recipient.id.in_(selected))).all()
        for recipient in recipients:
            if action=='deactivate':
                recipient.active=False; changed+=1
            elif db.scalar(select(SendQueue.id).where(SendQueue.recipient_id==recipient.id).limit(1)):
                recipient.active=False; recipient.deleted=True; changed+=1
            else:
                db.delete(recipient); changed+=1
    notice=(f'已停用 {changed} 位收件人' if action=='deactivate' else f'已删除 {changed} 位收件人')
    return RedirectResponse('/recipients?notice='+quote(notice),303)
@app.get('/templates',response_class=HTMLResponse)
def templates_page(request:Request,include_deleted:bool=False):
    with Session() as db:
        stmt=select(MailTemplate).order_by(MailTemplate.id.desc())
        if not include_deleted: stmt=stmt.where(MailTemplate.deleted.is_(False))
        return page(request,'邮件模板',rows=db.scalars(stmt).all(),include_deleted=include_deleted)
@app.post('/templates/add')
def add_template(name:str=Form(...),subject:str=Form(...),html_body:str=Form(...),text_body:str=Form(...)):
    with Session.begin() as db: db.add(MailTemplate(name=name[:200],subject=subject[:500],html_body=html_body,text_body=text_body))
    return RedirectResponse('/templates',303)
@app.post('/templates/{template_id}/update')
def update_template(template_id:int,name:str=Form(...),subject:str=Form(...),html_body:str=Form(...),text_body:str=Form(...)):
    with Session.begin() as db:
        item=db.get(MailTemplate,template_id)
        if item is None: raise HTTPException(404,'模板不存在')
        item.name=name.strip()[:200]; item.subject=subject.strip()[:500]; item.html_body=html_body; item.text_body=text_body
    return RedirectResponse('/templates?notice=模板已保存',303)
@app.post('/templates/{template_id}/delete')
def delete_template(template_id:int):
    with Session.begin() as db:
        item=db.get(MailTemplate,template_id)
        if item is None: raise HTTPException(404,'模板不存在')
        if db.scalar(select(Campaign.id).where(Campaign.template_id==template_id).limit(1)):
            item.deleted=True
            notice='模板已从模板列表移除；引用它的发送任务详情和历史保留。'
        else:
            db.delete(item); notice='模板已删除'
    return RedirectResponse('/templates?notice='+quote(notice),303)
@app.post('/templates/{template_id}/restore')
def restore_template(template_id:int):
    with Session.begin() as db:
        item=db.get(MailTemplate,template_id)
        if item is None: raise HTTPException(404,'模板不存在')
        item.deleted=False
    return RedirectResponse('/templates?include_deleted=true&notice='+quote('模板已恢复'),303)
@app.get('/settings',response_class=HTMLResponse)
def settings_page(request:Request):
    with Session() as db: return page(request,'SMTP 设置',rows=db.scalars(select(SMTPConfig)).all())
@app.post('/settings/smtp')
def add_smtp(name:str=Form('SMTP'),host:str=Form(...),port:int=Form(...),username:str=Form(...),password:str=Form(...),from_name:str=Form(''),from_email:str=Form(...),use_tls:Optional[str]=Form(None),is_default:Optional[str]=Form(None)):
    if not 1<=port<=65535 or not EMAIL.match(from_email): raise HTTPException(400,'端口或发件邮箱无效')
    with Session.begin() as db:
        if is_default: db.query(SMTPConfig).update({SMTPConfig.is_default:False})
        db.add(SMTPConfig(name=name[:120],host=host[:255],port=port,username=username[:320],password=password,from_name=from_name[:200],from_email=from_email.lower(),use_tls=bool(use_tls),is_default=bool(is_default)))
    return RedirectResponse('/settings',303)
@app.post('/settings/smtp/{config_id}/update')
def update_smtp(config_id:int,name:str=Form('SMTP'),host:str=Form(...),port:int=Form(...),username:str=Form(...),password:str=Form(''),from_name:str=Form(''),from_email:str=Form(...),use_tls:Optional[str]=Form(None),is_default:Optional[str]=Form(None)):
    if not 1<=port<=65535 or not EMAIL.match(from_email): raise HTTPException(400,'端口或发件邮箱无效')
    with Session.begin() as db:
        cfg=db.get(SMTPConfig,config_id)
        if cfg is None: raise HTTPException(404,'SMTP 配置不存在')
        if is_default: db.query(SMTPConfig).update({SMTPConfig.is_default:False})
        cfg.name=name[:120]; cfg.host=host[:255]; cfg.port=port; cfg.username=username[:320]
        if password.strip(): cfg.password=password
        cfg.from_name=from_name[:200]; cfg.from_email=from_email.lower(); cfg.use_tls=bool(use_tls)
        cfg.is_default=bool(is_default)
    return RedirectResponse('/settings?notice=SMTP配置已保存；密码留空表示保持原密码',303)
@app.post('/settings/smtp/{config_id}/{action}')
def smtp_action(config_id:int,action:str):
    with Session.begin() as db:
        cfg=db.get(SMTPConfig,config_id)
        if cfg is None: raise HTTPException(404,'SMTP 配置不存在')
        if action=='disable': cfg.enabled=False; cfg.is_default=False
        elif action=='enable': cfg.enabled=True
        elif action=='delete':
            if db.scalar(select(Campaign.id).where(Campaign.smtp_id==config_id).limit(1)) or db.scalar(select(SendQueue.id).where(SendQueue.smtp_id==config_id).limit(1)):
                raise HTTPException(409,'该 SMTP 配置已被任务或发送记录引用，不能删除；可以停用。')
            db.delete(cfg)
        else: raise HTTPException(400,'操作无效')
    return RedirectResponse('/settings?notice=SMTP配置状态已更新',303)
@app.post('/settings/test/{config_id}')
def test_smtp(config_id:int,recipient:str=Form(...)):
    if not EMAIL.match(recipient): raise HTTPException(400,'测试收件邮箱无效')
    with Session() as db:
        cfg=db.get(SMTPConfig,config_id)
        if cfg is None: raise HTTPException(404,'SMTP 配置不存在')
        try: send_message(cfg,recipient,'SMTP 测试邮件','<p>SMTP 测试成功。</p>','SMTP 测试成功。')
        except Exception as e: return RedirectResponse('/settings?result='+type(e).__name__+':连接或发送失败，请检查配置',303)
    return RedirectResponse('/settings?result=SMTP服务器已接受测试邮件提交（不代表最终送达）',303)
@app.get('/send',response_class=HTMLResponse)
def send_page(request:Request):
    with Session() as db: return page(request,'单发邮件',rows=db.scalars(select(SMTPConfig).where(SMTPConfig.enabled.is_(True))).all())
@app.post('/send')
def single_send(email:str=Form(...),subject:str=Form(...),html_body:str=Form(''),text_body:str=Form(''),smtp_id:int=Form(...)):
    email=email.strip().lower()
    if not EMAIL.match(email): raise HTTPException(400,'邮箱格式无效')
    with Session() as db:
        if db.scalar(select(Suppression.id).where(Suppression.email==email,Suppression.active.is_(True))): raise HTTPException(400,'该邮箱位于抑制名单，已阻止发送')
        cfg=db.get(SMTPConfig,smtp_id)
        if cfg is None: raise HTTPException(404,'SMTP 配置不存在')
        try: send_message(cfg,email,subject,html_body,text_body)
        except Exception as e:
            with Session.begin() as log_db: log_db.add(Event(kind='single_failed',detail=f'to={email}; subject={subject[:250]}; error={type(e).__name__}'))
            raise HTTPException(502,f'{type(e).__name__}: {str(e)[:250]}')
        try:
            with Session.begin() as log_db: log_db.add(Event(kind='single_submitted',detail=f'to={email}; subject={subject[:250]}; SMTP accepted submission'))
        except Exception:
            # SMTP has already accepted the message; never report send failure and invite a duplicate retry.
            return RedirectResponse('/logs?notice=SMTP已接受提交，但日志写入失败；请勿据此重发',303)
    return RedirectResponse('/logs?notice=SMTP服务器已接受提交（不代表最终送达）',303)
@app.get('/campaigns',response_class=HTMLResponse)
def campaigns_page(request:Request,include_archived:bool=False):
    with Session() as db:
        stmt=select(Campaign).order_by(Campaign.id.desc())
        stmt=stmt.where(Campaign.archived.is_(True) if include_archived else Campaign.archived.is_(False))
        return page(request,'批量任务',rows=db.scalars(stmt).all(),templates=db.scalars(select(MailTemplate).where(MailTemplate.deleted.is_(False))).all(),configs=db.scalars(select(SMTPConfig).where(SMTPConfig.enabled.is_(True))).all(),include_archived=include_archived)
@app.post('/campaigns')
def create_campaign(name:str=Form(...),template_id:int=Form(...),smtp_id:int=Form(...),interval_seconds:int=Form(2),scheduled_at:str=Form(''),tags:str=Form('')):
    if interval_seconds<1: raise HTTPException(400,'发送间隔至少为1秒')
    schedule=datetime.fromisoformat(scheduled_at).astimezone(timezone.utc) if scheduled_at else None
    with Session.begin() as db:
        template=db.get(MailTemplate,template_id)
        if template is None or template.deleted or not db.get(SMTPConfig,smtp_id): raise HTTPException(400,'模板或 SMTP 配置无效')
        c=Campaign(name=name[:200],template_id=template_id,smtp_id=smtp_id,scheduled_at=schedule,interval_seconds=min(interval_seconds,3600)); db.add(c); db.flush()
        stmt=select(Recipient).where(Recipient.active.is_(True),~Recipient.email.in_(select(Suppression.email).where(Suppression.active.is_(True))))
        if tags.strip(): stmt=stmt.where(Recipient.tags.contains(tags.strip()))
        for r in db.scalars(stmt):
            item=SendQueue(campaign_id=c.id,recipient_id=r.id,smtp_id=smtp_id); db.add(item); db.flush(); sync_campaign_detail(db,item,r)
    return RedirectResponse('/campaigns',303)
@app.post('/campaigns/{campaign_id}/{action}')
def campaign_action(campaign_id:int,action:str):
    with Session.begin() as db:
        c=db.get(Campaign,campaign_id)
        if c is None: raise HTTPException(404,'任务不存在')
        if action=='pause': c.status='paused'
        elif action=='resume': c.status='queued'
        elif action=='cancel':
            c.status='cancelled'
            for item,recipient in db.execute(select(SendQueue,Recipient).join(Recipient).where(SendQueue.campaign_id==c.id,SendQueue.status=='pending')):
                item.status='cancelled'; item.error_type='cancelled'; item.error_summary='任务已取消，未发送'; sync_campaign_detail(db,item,recipient)
        elif action=='archive':
            if c.status not in {'completed','cancelled'}: raise HTTPException(409,'只有已完成或已取消的任务可以归档。')
            c.archived=True
        elif action=='unarchive': c.archived=False
        else: raise HTTPException(400,'操作无效')
    return RedirectResponse('/campaigns',303)
@app.get('/campaigns/{campaign_id}',response_class=HTMLResponse)
def campaign_detail(request:Request,campaign_id:int):
    with Session() as db:
        c=db.get(Campaign,campaign_id)
        if c is None: raise HTTPException(404,'任务不存在')
        rows=db.scalars(select(CampaignDetail).where(CampaignDetail.campaign_id==c.id).order_by(CampaignDetail.id.desc()).limit(500)).all()
        return page(request,f'任务详情 #{campaign_id}',campaign=c,rows=rows)
@app.get('/logs',response_class=HTMLResponse)
def logs_page(request:Request,q:str='',status:str=''):
    with Session() as db:
        stmt=select(SendQueue,Recipient,Campaign).join(Recipient).join(Campaign).order_by(SendQueue.id.desc()).limit(500)
        if q: stmt=stmt.where(Recipient.email.contains(q))
        if status: stmt=stmt.where(SendQueue.status==status)
        event_stmt=select(Event).where(Event.kind.in_(['single_submitted','single_failed'])).order_by(Event.id.desc()).limit(500)
        if q: event_stmt=event_stmt.where(Event.detail.contains(q))
        if status=='submitted': event_stmt=event_stmt.where(Event.kind=='single_submitted')
        elif status=='failed': event_stmt=event_stmt.where(Event.kind=='single_failed')
        elif status: event_stmt=event_stmt.where(False)
        return page(request,'发送日志',rows=db.execute(stmt).all(),single_events=db.scalars(event_stmt).all())
@app.post('/logs/clear')
def clear_logs():
    terminal=('submitted','failed','cancelled','uncertain')
    with Session.begin() as db:
        result=db.query(SendQueue).filter(SendQueue.status.in_(terminal)).delete(synchronize_session=False)
    return RedirectResponse('/logs?notice='+quote(f'已清空 {result} 条已结束的批量发送日志；待发送和发送中的记录已保留'),303)
@app.post('/logs/clear-single')
def clear_single_logs():
    with Session.begin() as db:
        result=db.query(Event).filter(Event.kind.in_(('single_submitted','single_failed'))).delete(synchronize_session=False)
    return RedirectResponse('/logs?notice='+quote(f'已清空 {result} 条单发日志'),303)
@app.get('/logs/export')
def export_logs(q:str='',status:str=''):
    stmt=select(SendQueue,Recipient,Campaign).join(Recipient).join(Campaign).order_by(SendQueue.id.desc())
    if q: stmt=stmt.where(Recipient.email.contains(q))
    if status: stmt=stmt.where(SendQueue.status==status)
    output=io.StringIO(newline=''); writer=csv.writer(output)
    writer.writerow(['任务','邮箱','状态','重试次数','错误类型','错误摘要','创建时间','最后尝试时间','SMTP接受时间'])
    with Session() as db:
        for item,recipient,campaign in db.execute(stmt):
            writer.writerow([campaign.name,recipient.email,item.status,item.retries,item.error_type,item.error_summary,item.created_at,item.attempted_at,item.accepted_at])
        event_stmt=select(Event).where(Event.kind.in_(['single_submitted','single_failed']))
        if q: event_stmt=event_stmt.where(Event.detail.contains(q))
        if status=='submitted': event_stmt=event_stmt.where(Event.kind=='single_submitted')
        elif status=='failed': event_stmt=event_stmt.where(Event.kind=='single_failed')
        elif status: event_stmt=event_stmt.where(False)
        for item in db.scalars(event_stmt):
            email=item.detail.split(';',1)[0].removeprefix('to=')
            writer.writerow(['单发邮件',email,'submitted' if item.kind=='single_submitted' else 'failed',0,item.kind,item.detail,item.created_at,'',item.created_at if item.kind=='single_submitted' else ''])
    return Response(content='\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="mail_logs.csv"'})
@app.get('/suppressions',response_class=HTMLResponse)
def suppression_page(request:Request,include_released:bool=False):
    with Session() as db:
        stmt=select(Suppression).order_by(Suppression.id.desc()).limit(500)
        if not include_released: stmt=stmt.where(Suppression.active.is_(True))
        return page(request,'抑制名单',rows=db.scalars(stmt).all(),include_released=include_released)
@app.post('/suppressions')
def add_suppression(email:str=Form(...),reason:str=Form('管理员手动抑制'),source:str=Form('管理员')):
    email=email.strip().lower()
    if not EMAIL.match(email): raise HTTPException(400,'邮箱格式无效')
    with Session.begin() as db:
        old=db.scalar(select(Suppression).where(Suppression.email==email))
        if old is None: db.add(Suppression(email=email,reason=reason[:80],source=source[:200]))
        else: old.reason=reason[:80]; old.source=source[:200]; old.active=True
    return RedirectResponse('/suppressions',303)
@app.post('/suppressions/{suppression_id}/update')
def update_suppression(suppression_id:int,reason:str=Form(...),source:str=Form('管理员')):
    with Session.begin() as db:
        item=db.get(Suppression,suppression_id)
        if item is None: raise HTTPException(404,'抑制记录不存在')
        item.reason=reason[:80]; item.source=source[:200]
    return RedirectResponse('/suppressions?notice=抑制记录已更新',303)
@app.post('/suppressions/{suppression_id}/release')
def release_suppression(suppression_id:int):
    with Session.begin() as db:
        item=db.get(Suppression,suppression_id)
        if item is None: raise HTTPException(404,'抑制记录不存在')
        if item.active:
            item.active=False
            db.add(Event(kind='suppression_released',detail=f'{item.email}；需确认已获再次联系授权'))
    return RedirectResponse('/suppressions?notice=已解除抑制；该地址可能重新进入后续发送范围',303)
