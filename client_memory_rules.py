"""Conservative local checks: append facts unless a specific correction is supported."""
import re

CORRECTION=re.compile(r'更正|纠正|改名|改成|改为|不再|不是|并非|之前.{0,12}(?:错|不对)|原来.{0,12}(?:错|不对)|(?:已经|已)毕业')
ADDITION=re.compile(r'顺便|另外|同时|补充|加一句|还(?:是|有|做|制作|开发)|也是|也(?:是|做|制作|开发)')
FIELDS={
    'name':r'名字|姓名|叫|改名', 'age':r'年龄|岁',
    'education':r'学历|大学|学院|本科|硕士|博士|研究生|研[一二三四1234]|毕业|读书',
    'school':r'学校|大学|学院|就读', 'major':r'专业|学的是|学.{0,8}(?:专业|学)|建筑学|计算机|医学|法学',
    'location':r'住在|居住|搬到|来自|家在|城市|地址',
    'birthday':r'生日|出生', 'family':r'家人|父亲|母亲|父母|兄弟|姐妹|孩子',
    'relationship':r'单身|恋爱|结婚|离婚|男友|女友|伴侣|婚姻',
}
IDENTITY_FRAME=r'(?:我(?:的(?:身份|职业|工作))?(?:目前|现在|已经)?(?:是|当|做))'


def canonical_topic(item):
    item=dict(item)
    # A software-making experience cannot replace a student's or architect's role.
    content=item['content']
    software=re.search(r'(?:制作|开发|编写|写|做).{0,8}(?:客户端|应用|软件|程序|网站|项目)|(?:客户端|应用|软件|程序|网站).{0,8}(?:制作者|开发者|作者)',content)
    if item['category']=='fact' and item['key'] in ('identity.role','identity.profession') and software:
        item.update(category='event',key='event.software_creation',operation='add',replace_id='',previous_content='',previous_updated=0)
    return item


def supported_correction(item, prior):
    """Require a precise target and explicit correction, not just the same model key."""
    if item['operation']!='update' or item['confidence']<.95: return False
    if item['category']!=prior['category'] or prior['slot'] not in ('',item['key']): return False
    evidence=item['evidence']
    if not CORRECTION.search(evidence) or ADDITION.search(evidence): return False
    old=prior['content']; new=item['content']; key=item['key']
    if key in ('identity.role','identity.profession'):
        if not re.search(IDENTITY_FRAME,old): return False
        # Distinct simultaneous roles are common; broad role keys alone prove nothing.
        identity=re.split(r'是|当|做',old,maxsplit=1)[-1]
        identity=re.sub(r'^(?:一个|一名|一位|个|名)|[。！!\s]+$','',identity)
        negation=bool(identity and re.search(r'(?:不再是|不是|不当|不做|并非).{0,3}'+re.escape(identity),evidence))
        graduation=bool(re.search(r'学生|大学生|本科生|研究生',old) and re.search(r'(?:已经|已)毕业',evidence))
        targeted=bool(re.search(r'(?:更正|纠正).{0,6}(?:身份|职业|工作)',evidence))
        return negation or graduation or targeted
    if key.startswith('identity.'):
        pattern=FIELDS.get(key.split('.',1)[1])
        # Old notes may contain school + major + year together. Correcting
        # just the year must not silently discard the other facts in that note.
        if key=='identity.education':
            school=re.search(r'([\u4e00-\u9fff]{2,24}(?:大学|学院))',old)
            if school:
                component=re.sub(r'^(?:我|现在|正在|在|就读于|就读|目前|的学校是)+','',school[1])
                if component and component not in new: return False
            major=re.search(r'(?:读|学)([\u4e00-\u9fff]{2,12}?学)(?:的|[，。]|$)',old)
            if major and major[1] not in new: return False
        return bool(pattern and re.search(pattern,old) and re.search(pattern,evidence))
    # Other topics must be keyed and explicitly mention a distinctive part of
    # the old fact. A model-supplied topic label alone cannot authorize a write.
    subject=re.sub(r'我|本人|的|是|喜欢|不喜欢|希望|不要|不准|别|叫|称呼|更正|纠正|一个|一名','',old)
    words=set(re.findall(r'[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}',subject))
    return bool(prior['slot']==key and any(word in evidence for word in words))
