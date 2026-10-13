"""Small, safe demo dataset; every address uses the reserved .invalid domain."""

RECIPIENTS = [
    ("lin.xiao@example.invalid", "林晓", "体验用户,新品"),
    ("chen.wei@example.invalid", "陈伟", "体验用户,活动"),
    ("wang.min@example.invalid", "王敏", "体验用户,新品"),
    ("unsubscribe@example.invalid", "退订演示", "退订测试"),
]

SUPPRESSION = ("unsubscribe@example.invalid", "用户退订", "内置演示数据")

TEMPLATES = [
    {
        "name": "演示｜欢迎邮件（变量替换）",
        "subject": "{{name}}，欢迎体验我们的邮件服务",
        "html_body": "<h2>你好，{{name}}！</h2><p>这是一封本地软件的<strong>演示邮件</strong>，用于检查中文、HTML 和变量替换。</p><p>你的标签：{{tags}}</p><p>演示邮箱使用保留的 .invalid 域名，不是真实收件地址。</p>",
        "text_body": "你好，{{name}}！\n\n这是一封本地软件的演示邮件，用于检查中文和变量替换。\n你的标签：{{tags}}\n演示地址不是可投递邮箱。",
    },
    {
        "name": "演示｜产品更新通知",
        "subject": "{{name}}，本周产品更新一览",
        "html_body": "<h2>{{name}}，你好</h2><p>这里是产品更新邮件模板演示。</p><ul><li>模板支持主题和正文变量</li><li>HTML 邮件同时带有纯文本版本</li></ul><p>本封邮件仅供本地测试。</p>",
        "text_body": "{{name}}，你好\n\n这里是产品更新邮件模板演示。模板支持主题和正文变量，HTML 邮件同时带有纯文本版本。本封邮件仅供本地测试。",
    },
    {
        "name": "演示｜纯文本兼容性检查",
        "subject": "文本版本检查 - {{email}}",
        "html_body": "<p>纯文本兼容性检查：你好，{{name}}。</p>",
        "text_body": "纯文本兼容性检查：你好，{{name}}。收件地址：{{email}}。",
    },
]


def seed_demo_data(db, Recipient, MailTemplate, Suppression, select):
    """Insert missing sample rows without modifying or deleting user data."""
    for email, name, tags in RECIPIENTS:
        if db.scalar(select(Recipient.id).where(Recipient.email == email)) is None:
            db.add(Recipient(email=email, name=name, tags=tags))
    email, reason, source = SUPPRESSION
    if db.scalar(select(Suppression.id).where(Suppression.email == email)) is None:
        db.add(Suppression(email=email, reason=reason, source=source))
    for values in TEMPLATES:
        if db.scalar(select(MailTemplate.id).where(MailTemplate.name == values["name"])) is None:
            db.add(MailTemplate(**values))
