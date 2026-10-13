from fastapi.testclient import TestClient
from urllib.parse import unquote
import main

client=TestClient(main.app)

def setup_function():
    main.Base.metadata.drop_all(main.engine)
    main.Base.metadata.create_all(main.engine)

def test_dashboard_reads_database_and_routes_render():
    with main.Session.begin() as db:
        db.add(main.Recipient(email='a@example.com',name='小明'))
    response=client.get('/')
    assert response.status_code==200
    assert '小明' not in response.text
    assert '>1<' in response.text
    for path in ['/send','/recipients','/templates','/campaigns','/logs','/settings','/suppressions']:
        assert client.get(path).status_code==200

def test_csv_import_normalizes_deduplicates_and_counts_invalid():
    body='email,name,tags\nA@example.com,张三,客户\na@example.com,重复,客户\nbad,错误,\n'
    response=client.post('/recipients/import',files={'file':('recipients.csv',body.encode('utf-8-sig'),'text/csv')},follow_redirects=False)
    assert response.status_code==303
    with main.Session() as db:
        assert db.scalar(main.select(main.func.count()).select_from(main.Recipient))==1
    notice=unquote(response.headers['location'])
    assert '新增1' in notice
    assert '重复1' in notice
    assert '无效1' in notice

def test_add_recipient_duplicate_shows_notice_instead_of_internal_error():
    with main.Session.begin() as db:
        db.add(main.Recipient(email='existing@example.com',name='原有收件人'))
    response=client.post('/recipients/add',data={'email':'EXISTING@example.com','name':'重复用户'},follow_redirects=False)
    assert response.status_code==303
    assert '该邮箱已存在' in unquote(response.headers['location'])
    with main.Session() as db:
        assert db.scalar(main.select(main.func.count()).select_from(main.Recipient))==1

def test_template_uses_safe_autoescape_and_rejects_missing_variables():
    assert main.render('<b>{{ name }}</b>',{'name':'<script>x</script>'})=='<b>&lt;script&gt;x&lt;/script&gt;</b>'
    try: main.render('{{ missing }}',{})
    except Exception: pass
    else: assert False, 'missing variables should be explicit'

def test_suppression_blocks_single_send_before_smtp(monkeypatch):
    with main.Session.begin() as db:
        cfg=main.SMTPConfig(host='localhost',port=587,username='u',password='secret',from_email='sender@example.com')
        db.add(cfg); db.add(main.Suppression(email='blocked@example.com',reason='用户退订'))
    def fail(*args): raise AssertionError('SMTP must not be called')
    monkeypatch.setattr(main,'send_message',fail)
    response=client.post('/send',data={'email':'blocked@example.com','subject':'Hi','smtp_id':'1'})
    assert response.status_code==400

def test_campaign_creation_excludes_suppressions():
    with main.Session.begin() as db:
        db.add(main.MailTemplate(name='Hi',subject='Hello {{name}}',html_body='<p>Hi</p>',text_body='Hi'))
        db.add(main.SMTPConfig(host='localhost',port=587,username='u',password='secret',from_email='sender@example.com'))
        db.add_all([main.Recipient(email='ok@example.com'),main.Recipient(email='no@example.com'),main.Recipient(email='inactive@example.com',active=False)])
        db.add(main.Suppression(email='no@example.com',reason='退订'))
    response=client.post('/campaigns',data={'name':'batch','template_id':'1','smtp_id':'1','interval_seconds':'2'},follow_redirects=False)
    assert response.status_code==303
    with main.Session() as db:
        queued=db.scalars(main.select(main.SendQueue)).all()
        assert len(queued)==1 and queued[0].recipient_id==1

def test_unreferenced_template_and_smtp_can_be_deleted():
    with main.Session.begin() as db:
        db.add(main.MailTemplate(name='unused',subject='s',html_body='h',text_body='t'))
        db.add(main.SMTPConfig(host='smtp.example.com',port=587,username='u',password='secret',from_email='sender@example.com'))
    assert client.post('/templates/1/delete',follow_redirects=False).status_code==303
    assert client.post('/settings/smtp/1/delete',follow_redirects=False).status_code==303
    with main.Session() as db:
        assert db.get(main.MailTemplate,1) is None
        assert db.get(main.SMTPConfig,1) is None

def test_smtp_can_be_disabled_and_reenabled():
    with main.Session.begin() as db: db.add(main.SMTPConfig(host='smtp.example.com',port=587,username='u',password='secret',from_email='sender@example.com'))
    assert client.post('/settings/smtp/1/disable',follow_redirects=False).status_code==303
    with main.Session() as db: assert db.get(main.SMTPConfig,1).enabled is False
    assert client.post('/settings/smtp/1/enable',follow_redirects=False).status_code==303
    with main.Session() as db: assert db.get(main.SMTPConfig,1).enabled is True

def test_demo_data_is_chinese_idempotent_and_suppresses_optout():
    for _ in range(2):
        response=client.post('/demo-data',follow_redirects=False)
        assert response.status_code==303
    with main.Session() as db:
        assert db.scalar(main.select(main.func.count()).select_from(main.Recipient))==4
        assert db.scalar(main.select(main.func.count()).select_from(main.MailTemplate))==3
        assert db.scalar(main.select(main.func.count()).select_from(main.Suppression))==1
        assert db.scalar(main.select(main.Suppression).where(main.Suppression.email=='unsubscribe@example.invalid')) is not None

def test_recipient_edit_deactivate_and_restore_preserves_history():
    with main.Session.begin() as db:
        db.add(main.Recipient(email='keep@example.com',name='旧名',tags='old'))
    assert client.post('/recipients/1/update',data={'name':'新名','tags':'vip'},follow_redirects=False).status_code==303
    assert client.post('/recipients/1/deactivate',follow_redirects=False).status_code==303
    with main.Session() as db:
        recipient=db.get(main.Recipient,1)
        assert recipient.name=='新名' and recipient.tags=='vip' and recipient.active is False
    assert 'keep@example.com' not in client.get('/recipients').text
    assert 'keep@example.com' in client.get('/recipients?include_inactive=true').text
    assert client.post('/recipients/1/restore',follow_redirects=False).status_code==303

def test_template_edit_and_referenced_template_cannot_be_deleted():
    with main.Session.begin() as db:
        db.add(main.MailTemplate(name='Before',subject='subject',html_body='<p>old</p>',text_body='old'))
    assert client.post('/templates/1/update',data={'name':'After','subject':'new','html_body':'<p>new</p>','text_body':'new'},follow_redirects=False).status_code==303
    with main.Session.begin() as db:
        db.add(main.SMTPConfig(host='localhost',port=587,username='u',password='secret',from_email='sender@example.com'))
        db.add(main.Campaign(name='history',template_id=1,smtp_id=1,status='completed'))
    assert client.post('/templates/1/delete',follow_redirects=False).status_code==409
    with main.Session() as db: assert db.get(main.MailTemplate,1).name=='After'

def test_smtp_update_keeps_password_when_blank_and_blocks_referenced_delete():
    with main.Session.begin() as db:
        db.add(main.MailTemplate(name='t',subject='s',html_body='h',text_body='t'))
        db.add(main.SMTPConfig(name='old',host='smtp.example.com',port=587,username='u',password='keep-me',from_email='sender@example.com'))
    response=client.post('/settings/smtp/1/update',data={'name':'new','host':'smtp.example.net','port':'465','username':'u2','password':'','from_name':'Name','from_email':'sender@example.com','use_tls':'on'},follow_redirects=False)
    assert response.status_code==303
    with main.Session.begin() as db: db.add(main.Campaign(name='history',template_id=1,smtp_id=1,status='completed'))
    assert client.post('/settings/smtp/1/delete',follow_redirects=False).status_code==409
    with main.Session() as db:
        cfg=db.get(main.SMTPConfig,1)
        assert cfg.host=='smtp.example.net' and cfg.password=='keep-me'

def test_suppression_release_is_audited_and_readd_reactivates():
    with main.Session.begin() as db: db.add(main.Suppression(email='optout@example.com',reason='退订'))
    assert client.post('/suppressions/1/release',follow_redirects=False).status_code==303
    assert 'optout@example.com' not in client.get('/suppressions').text
    assert 'optout@example.com' in client.get('/suppressions?include_released=true').text
    with main.Session() as db:
        assert db.scalar(main.select(main.func.count()).select_from(main.Event).where(main.Event.kind=='suppression_released'))==1
    assert client.post('/suppressions',data={'email':'optout@example.com','reason':'再次退订'},follow_redirects=False).status_code==303
    with main.Session() as db: assert db.get(main.Suppression,1).active is True

def test_campaign_archive_requires_terminal_status_and_can_be_reversed():
    with main.Session.begin() as db:
        db.add(main.MailTemplate(name='t',subject='s',html_body='h',text_body='t'))
        db.add(main.SMTPConfig(host='localhost',port=587,username='u',password='secret',from_email='sender@example.com'))
        db.add(main.Campaign(name='running',template_id=1,smtp_id=1,status='running'))
    assert client.post('/campaigns/1/archive',follow_redirects=False).status_code==409
    with main.Session.begin() as db: db.get(main.Campaign,1).status='completed'
    assert client.post('/campaigns/1/archive',follow_redirects=False).status_code==303
    assert 'running' not in client.get('/campaigns').text
    assert 'running' in client.get('/campaigns?include_archived=true').text
    assert client.post('/campaigns/1/unarchive',follow_redirects=False).status_code==303

def test_logs_export_respects_filters_and_returns_utf8_csv():
    with main.Session.begin() as db:
        db.add(main.MailTemplate(name='t',subject='s',html_body='h',text_body='t'))
        db.add(main.SMTPConfig(host='localhost',port=587,username='u',password='secret',from_email='sender@example.com'))
        recipient=main.Recipient(email='导出@example.com',name='小林')
        db.add(recipient); db.flush()
        campaign=main.Campaign(name='导出任务',template_id=1,smtp_id=1,status='completed')
        db.add(campaign); db.flush()
        db.add(main.SendQueue(campaign_id=campaign.id,recipient_id=recipient.id,smtp_id=1,status='submitted'))
    response=client.get('/logs/export?q=导出&status=submitted')
    assert response.status_code==200 and 'text/csv' in response.headers['content-type']
    assert response.content.startswith(b'\xef\xbb\xbf')
    assert '导出@example.com' in response.content.decode('utf-8-sig')
    assert client.get('/logs/export?status=failed').content.count(b'\n')==1

def test_single_send_is_logged_and_exported(monkeypatch):
    with main.Session.begin() as db:
        db.add(main.SMTPConfig(host='localhost',port=587,username='u',password='do-not-log',from_email='sender@example.com'))
    monkeypatch.setattr(main,'send_message',lambda *args: None)
    response=client.post('/send',data={'email':'self@example.com','subject':'Hello','text_body':'test','smtp_id':'1'},follow_redirects=False)
    assert response.status_code==303
    logs=client.get('/logs?q=self@example.com&status=submitted')
    assert logs.status_code==200 and 'self@example.com' in logs.text and 'Hello' in logs.text
    exported=client.get('/logs/export?q=self@example.com&status=submitted').content.decode('utf-8-sig')
    assert 'self@example.com' in exported and 'Hello' in exported and 'do-not-log' not in exported
