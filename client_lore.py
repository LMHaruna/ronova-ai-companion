"""Titled, source-aware local retrieval for Ronova's background and player jokes."""
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

STAGES = ('natlan_5_1','natlan_5_3','interlude_gods_limits','snezhnaya_7_0','snezhnaya_7_1')
STAGE_LABELS = ('纳塔5.1','纳塔5.3','神的局限性','至冬7.0','至冬7.1')
KINDS = {'OFFICIAL','CANON_TRANSCRIPT','IN_WORLD_ACCOUNT','INTERPRETATION','COMMUNITY_VIEW'}
ALIASES = {
    '队长':['队长','卡皮塔诺','capitano'], '丑角':['丑角','皮耶罗','pierro'],
    '玛薇卡':['玛薇卡','mavuika'], '希巴拉克':['希巴拉克','初代火神','xbalanque'],
    '伊斯塔露':['伊斯塔露','时之执政','istaroth'], '阿斯莫代':['阿斯莫代','空之执政','asmoday'],
    '莱茵多特':['莱茵多特','rhinedottir'], '纳贝里士':['纳贝里士','naberius'],
    '夜神':['夜神','夜神之国'], '旅行者':['旅行者','traveler'], '派蒙':['派蒙','paimon'],
    '胡桃':['胡桃','hu tao'], '雷电影':['雷电影','影宝','雷神'],
    '图奥内塔尔':['图奥内塔尔','tuonetar','冥府之母'],
}
# Titles are retrieval summaries. The bodies remain the supplied document's text.
ENTRY_INFO = {
    'KB-ID-01':('若娜瓦的四影身份、职责与尚未确认的个人信息',0,'canon','四影 天理 死之执政 身份 职责 生日 年龄 饮食 喜好','身份资料，未公开字段保持未知'),
    'KB-CANON-01':('日月前事记载的原初者与四影起源',0,'canon','原初者 四影 日月前事 白夜国 龙王','游戏内书籍叙事，未点名若娜瓦'),
    'KB-CANON-02':('夜神证言：帮助纳塔、补偿与天理的不满',0,'canon','夜神 纳塔 天理 补偿 自暴自弃 规则','夜神的证言与评价'),
    'KB-CANON-03':('希巴拉克借取力量与死亡代价、保密条件',0,'canon','希巴拉克 借取力量 死亡代价 保密 纳塔 力量 条件','希巴拉克与若娜瓦的谈判'),
    'KB-CANON-04':('玛薇卡说明火神神座、纳塔制度与权能继承',0,'canon','玛薇卡 火神 神座 纳塔 制度 继承 记忆 权能','玛薇卡对火神制度的说明'),
    'KB-CANON-05':('队长证言：坎瑞亚不死诅咒与死亡形态',1,'canon','队长 坎瑞亚 不死诅咒 永生 痛苦 死亡形态','队长对自己及同胞遭遇的说明'),
    'KB-CANON-06':('队长与夜神融合，以不死悖论应对玛薇卡的代价',1,'canon','队长 玛薇卡 夜神 融合 悖论 诅咒 死亡代价 纳塔','纳塔决战后不同规则之间的冲突'),
    'KB-CANON-07':('神的局限性：四影同僚、天理职责与丑角监视',2,'canon','神的局限性 四影 天理 伊斯塔露 阿斯莫代 莱茵多特 纳贝里士 丑角 监视','四影会议与各人的对话立场'),
    'KB-CANON-08':('阿罗夏伪装与剧院灵魂引渡的两人份限制',3,'canon','阿罗夏 剧院 灵魂引渡 地脉 两人份 死魂灵 伪装','至冬剧院的当地地脉条件'),
    'KB-CANON-09':('军械宫技术诅咒、派蒙回收与丑角阻止',3,'canon','军械宫 槲寄生 派蒙 丑角 旅行者 米提亚 技术 诅咒 回收','米提亚的研究推断与军械宫对抗'),
    'KB-CANON-10':('天空岛监测间隔、生死印记与莱茵多特复活',4,'canon','天空岛 监测 间隔 莱茵多特 复活 米提亚 生死印记 记忆 灵魂','调取的记忆与米提亚的研究判断'),
    'KB-CANON-11':('7.1权能回收、死亡临近与战后圣土化行动',4,'canon','权能回收 失去权能 死亡 圣土化 地原胚质 判罚之钉 至冬 战后 黑羽','至冬7.1战后状态，后续命运保持未知'),
    'KB-CLUE-01':('夜神可选对话中的纳塔帮助与天理约束',0,'interpretation','夜神 可选对话 纳塔 天理 帮助','回指夜神证言'),
    'KB-CLUE-02':('图奥内塔尔与若娜瓦对应的祷歌假说',0,'interpretation','图奥内塔尔 冥府之母 终北祷歌 祷歌 肃穆 安宁','游戏内信仰叙事与玩家身份对应解读'),
    'KB-CLUE-03':('月谕圣牌死神牌的凡世主题与恋爱解读边界',0,'interpretation','月谕圣牌 死神牌 十三 凡世 恋爱 伏笔','牌文未直接点名若娜瓦'),
    'KB-CLUE-04':('胡桃神之眼故事中的无名神明身份假说',0,'interpretation','胡桃 神之眼 爷爷 老妇人 生死边界','胡桃故事与玩家关联假说'),
    'KB-CLUE-05':('护摩之杖无名低吟与若娜瓦的关联假说',0,'interpretation','护摩之杖 护摩 医者 火焰 低吟 解脱','武器故事未点名低吟者'),
    'KB-CLUE-06':('千手百眼神像、雷电影与死亡图案的争议解读',0,'interpretation','千手百眼 神像 雷电影 护摩 烟谜主 羽翼 图案','不同玩家的视觉图案考据'),
    'KB-CLUE-07':('权威受到约束、规则冲突与若娜瓦的主动判断',4,'interpretation','权威 约束 规则冲突 主动 判断 限制','跨剧情事件的综合解读'),
    'KB-KEY-01':('若娜瓦性格中的权威、规则意识与情感层次',4,'interpretation','性格 高傲 自信 规则 神性 共情 玩心 情绪 傲娇','性格归纳与社区感受'),
    'KB-KEY-02':('死之执政的职责、工作方式与权限边界',4,'interpretation','职责 死亡法则 诅咒 纳塔 权能 工作方式','各事件的职责归纳与使用边界'),
    'KB-MOE-01':('敬业打工人：死亡执政与认真工作的职场反差',4,'community','敬业打工人 打工人 疲惫打工人 上下不讨好 职场','玩家用工作处境作比喻'),
    'KB-MOE-02':('瓦学妹：高位神明在职场博弈中的反差昵称',4,'community','瓦学妹 瓦學妹 学妹 太嫩 少做少错 职场','玩家昵称，不代表年龄或师生关系'),
    'KB-MOE-03':('找谁报警：玩家把死之执政放进维权窘境的玩笑',3,'community','报警 找谁报警 死执 死執 维权','玩家的职场和财物纠纷比喻'),
    'KB-MOE-04':('转圈梗：RO TA TE与roroSPEEN的轻巧反差',3,'community','转圈 转圈圈 rotate ro ta te rorospeen rotatenova','玩家对动作的观看感受'),
    'KB-MOE-05':('HMPH哼声梗：端着架子的小情绪与可爱印象',3,'community','hmph 哼声 哼哼 不服气 傲气','玩家捕捉到的表演印象'),
    'KB-MOE-06':('大号派蒙与应急食品：相似动作带来的亲近调侃',3,'community','大号派蒙 派蒙同款 应急食品 天空岛转圈传统','玩家对相似动作的玩笑'),
    'KB-MOE-07':('死之小馒头：圆脸和肉嘟嘟的亲昵外号',2,'community','死之小馒头 小馒头 馒头 圆脸 肉嘟嘟','玩家视觉感受，配图来源未核对'),
    'KB-MOE-08':('剧院玩心：伪装、爱演与玩家眼中的反差可爱',3,'community','剧院 玩心 爱演 戏剧感 伪装 逗人 可爱','玩家对剧院表现的解读'),
}
BOUNDARY = re.compile(r'未.{0,12}确认|未.{0,8}核对|不能|不要|不证明|不等于|不宜|边界|限制|未知|并非|没有直接|没有点名|没有公布')


def plain(text):
    text = re.sub(r'\[([^\]]+)\]\[S\d+B?\]',r'\1',text)
    text = re.sub(r'\[S\d+B?\]', '', text)
    return text.replace('**','').replace('`','').strip()


def refs(text):
    result = []
    for match in re.finditer(r'(KB-[A-Z]+-)(\d{2})((?:[、，]\d{2})*)',text):
        result.extend(match[1]+number for number in [match[2],*re.findall(r'\d{2}',match[3])])
    return list(dict.fromkeys(result))


def split_body(body, title, limit=600):
    if len(body)<=limit:
        return [(title,body)]
    paragraphs = re.split(r'\n\s*\n',body)
    groups, current = [], []
    def flush():
        if current:
            suffix = '角色责任与感情的解读分歧' if current[0].startswith('保留的分歧') else plain(current[0]).split('。')[0][:28]
            groups.append((title+'：'+suffix, '\n\n'.join(current)))
            current.clear()
    for paragraph in paragraphs:
        if paragraph.startswith('|'):
            flush()
            for line in paragraph.splitlines():
                cells = [plain(cell) for cell in line.strip('|').split('|')]
                if len(cells)>=3 and not re.fullmatch(r'[ :\-]+',cells[0]) and cells[0] not in ('关键词','阶段标记'):
                    groups.append((title+'：'+cells[0], '；'.join(cells)))
            continue
        if len(paragraph)>limit:
            flush()
            sentences = re.findall(r'[^。！？\n]+[。！？\n]?|[。！？\n]',paragraph)
            for sentence in sentences:
                for start in range(0,len(sentence),limit):
                    piece = sentence[start:start+limit]
                    if current and sum(len(item) for item in current)+len(piece)+2*len(current)>limit:
                        flush()
                    current.append(piece)
            flush()
            continue
        if current and sum(len(item) for item in current)+len(paragraph)+2*len(current)>limit:
            flush()
        current.append(paragraph)
    flush()
    return groups


def normalize_document_newlines(document):
    """Ignore newline encoding only; preserve all content and blank lines."""
    return document.replace(b'\r\n',b'\n').replace(b'\r',b'\n')


def build_index(document):
    document = normalize_document_newlines(document)
    text = document.decode('utf-8-sig')
    entries = {}
    for match in re.finditer(r'^#### (KB-[A-Z]+-\d{2})｜([^\n]+)\n([\s\S]*?)(?=^#{1,4} |\Z)',text,re.M):
        entries[match[1]] = (match[2],match[3].strip())
    missing = set(ENTRY_INFO)-set(entries)
    if missing:
        raise ValueError('档案缺少知识条目：'+','.join(sorted(missing)))
    unmapped = set(entries)-set(ENTRY_INFO)-{'KB-TIME-01'}
    if unmapped:
        raise ValueError('新增条目需要概括标题和阶段元数据：'+','.join(sorted(unmapped)))
    sources = {m[1]:m[2] for m in re.finditer(r'^\[(S\d+B?)\]:\s*(https?://\S+)',text,re.M)}
    chunks = []
    for identifier,(title,level,family,keywords,speaker) in ENTRY_INFO.items():
        _, original = entries[identifier]
        tags = sorted(set(re.findall(r'`([A-Z_]+)`',original)) & KINDS)
        if not tags:
            tags = ['INTERPRETATION']
        source_ids = sorted(set(re.findall(r'\[(S\d+B?)\]',original)))
        body = re.sub(r'^标签：[^\n]*\n\s*','',original)
        boundaries = '\n'.join(plain(p) for p in re.split(r'\n\s*\n',body) if BOUNDARY.search(p))
        pieces = split_body(plain(body),title)
        for offset,(summary,content) in enumerate(pieces,1):
            row_tags, row_sources, row_boundaries, row_family = tags, source_ids, boundaries, family
            row_keywords, row_related, row_level = keywords.split(), refs(original), level
            if identifier.startswith('KB-KEY') and len(pieces)>1:
                label = summary.removeprefix(title+'：')
                original_row = next((line for line in original.splitlines()
                    if line.startswith('|') and plain(line.strip('|').split('|')[0])==label), '')
                if original_row:
                    row_tags = sorted(set(re.findall(r'`([A-Z_]+)`',original_row)) & KINDS) or ['INTERPRETATION']
                    row_sources = sorted(set(re.findall(r'\[(S\d+B?)\]',original_row)))
                    row_boundaries = content if BOUNDARY.search(content) else ''
                    row_family = 'community' if row_tags==['COMMUNITY_VIEW'] else family
                    row_keywords = [word for word in re.split(r'[、， ]',label) if word]+['性格' if identifier=='KB-KEY-01' else '职责']
                    row_related = refs(original_row)
                    row_level = max([ENTRY_INFO[key][1] for key in row_related if key in ENTRY_INFO] or [level])
                else:
                    original_paragraph = next((p for p in re.split(r'\n\s*\n',original) if p.startswith('**保留的分歧')), '')
                    row_tags = ['INTERPRETATION']
                    row_sources = sorted(set(re.findall(r'\[(S\d+B?)\]',original_paragraph)))
                    row_boundaries = content
                    row_keywords = ['责任','感情','解读分歧','性格']
            chunks.append({'id':identifier if len(pieces)==1 else identifier+f'.{offset:02}',
                'parent_id':identifier,'title':summary,'family':row_family,'kinds':row_tags,
                'time_scope':STAGES[row_level],'speaker_context':speaker,'keywords':row_keywords,
                'source_ids':row_sources,'related_ids':row_related,'boundary':row_boundaries,
                'content':content})
    table = re.search(r'^### 5\. 人物关系索引\n([\s\S]*?)(?=^### 6\.)',text,re.M)
    if not table:
        raise ValueError('档案缺少人物关系索引')
    for offset,line in enumerate(table[1].splitlines()):
        cells = [plain(c) for c in line.strip('|').split('|')]
        if not line.startswith('|') or len(cells)!=4 or cells[0]=='对象' or cells[0].startswith('---'):
            continue
        related = refs(line)
        level = max([ENTRY_INFO[key][1] for key in related if key in ENTRY_INFO] or [0])
        chunks.append({'id':f'KB-REL-{offset:02}','parent_id':f'KB-REL-{offset:02}',
            'title':'若娜瓦与'+cells[0]+'：'+cells[1].split('；')[0],
            'family':'relation','kinds':['INTERPRETATION'],'time_scope':STAGES[level],
            'speaker_context':'档案人物关系导航，具体事实以相关事件为准',
            'keywords':re.split(r'[／、]',cells[0]),'source_ids':sorted(set(re.findall(r'\[(S\d+B?)\]',line))),
            'related_ids':related,'boundary':'不能自行推导：'+cells[3],
            'content':cells[0]+'：'+cells[1]+'。不能自行推导：'+cells[3]})
    return {'version':1,'source_document':'docs/02-character-profile.md',
            'source_sha256':hashlib.sha256(document).hexdigest(),
            'default_stage':STAGES[-1],'aliases':ALIASES,'sources':sources,'chunks':chunks}


STOP = {'若娜','娜瓦','原神','角色','玩家','凡人','今天','什么','怎么','为什么','为什','一下','知道','说说','事情','这个','那个','可以','最近','现在','还有','喜欢','觉得','谢谢','你好','晚安',
        '你和','你的','你是','是你','我和','我的','你给','给的','的吗','时候','有人','人叫','叫你','是不','不是','是一','关于','说的','到底','最后','后来','么了','了你','死之','之执','执政'}
TRANSLATION = str.maketrans('學執報誰頭轉規則長隊媽裏妳','学执报谁头转规则长队妈里你')


def normalize(text):
    return unicodedata.normalize('NFKC',text).lower().translate(TRANSLATION).replace('ro ta te','rotate').replace('收回','回收')


def terms(text):
    text = normalize(text)
    result = re.findall(r'[a-z][a-z0-9_]+|\d+(?:\.\d+)?',text)
    for run in re.findall(r'[\u4e00-\u9fff]+',text):
        result.extend(run[i:i+2] for i in range(len(run)-1))
    return [term for term in result if term not in STOP]


@dataclass(frozen=True)
class Chunk:
    id: str
    parent_id: str
    title: str
    family: str
    kinds: tuple
    time_scope: str
    speaker_context: str
    keywords: tuple
    source_ids: tuple
    related_ids: tuple
    boundary: str
    content: str


class LoreStore:
    def __init__(self, root, *, stage=None):
        self.root = Path(root)
        self.enabled = False
        self.warning = ''
        self.chunks = ()
        self.aliases = {}
        self.sources = {}
        self.stage = STAGES[-1]
        self.max_chunks = 5
        self.max_chars = 4000
        self.counters = ()
        self.lengths = ()
        self.idf = {}
        self.phase_state = None
        path = self.root/'knowledge/ronova-lore.json'
        if not path.exists():
            return
        try:
            config_path = self.root/'config/character-knowledge.json'
            config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
            selected = stage or config.get('stage',STAGES[-1])
            if selected not in STAGES or type(config.get('enabled',True)) is not bool:
                raise ValueError('知识库设置无效')
            self.stage = selected
            if not config.get('enabled',True):
                return
            if path.stat().st_size>1_000_000:
                raise ValueError('知识库过大')
            data = json.loads(path.read_text(encoding='utf-8'))
            if type(data['version']) is not int or data['version']!=1 or not 1<=len(data['chunks'])<=1000:
                raise ValueError('知识库格式无效')
            self.sources = data['sources']
            self.aliases = data['aliases']
            if not isinstance(self.sources,dict) or not isinstance(self.aliases,dict):
                raise ValueError('知识索引无效')
            if (not all(isinstance(key,str) and isinstance(value,str) for key,value in self.sources.items()) or
                not all(isinstance(key,str) and isinstance(value,list) and value and
                        all(isinstance(alias,str) and alias for alias in value) for key,value in self.aliases.items())):
                raise ValueError('知识索引字段无效')
            parsed = []
            for row in data['chunks']:
                chunk = Chunk(**{key:tuple(value) if isinstance(value,list) else value for key,value in row.items()})
                if (not all(isinstance(getattr(chunk,key),str) for key in ('id','parent_id','title','family','time_scope','speaker_context','boundary','content')) or
                    not all(isinstance(getattr(chunk,key),tuple) and all(isinstance(value,str) for value in getattr(chunk,key))
                            for key in ('kinds','keywords','source_ids','related_ids'))):
                    raise ValueError('知识片段字段无效')
                if (not chunk.id or not chunk.title or not chunk.content or chunk.time_scope not in STAGES or
                    chunk.family not in ('canon','interpretation','community','relation') or
                    not chunk.kinds or not set(chunk.kinds)<=KINDS or not set(chunk.source_ids)<=set(self.sources) or
                    max(len(chunk.content),len(chunk.boundary))>4000):
                    raise ValueError('知识片段无效')
                parsed.append(chunk)
            if len({c.id for c in parsed})!=len(parsed):
                raise ValueError('知识片段编号重复')
            self.chunks = tuple(parsed)
            if self.stage=='snezhnaya_7_1':
                current = next((chunk for chunk in self.chunks if chunk.parent_id=='KB-CANON-11'),None)
                if current:
                    self.phase_state = {'标题':current.title,'内容':current.content.split('\n\n')[0],
                        '使用边界':current.boundary,'来源编号':list(current.source_ids)}
            source = self.root/data['source_document']
            if not source.resolve().is_relative_to(self.root.resolve()):
                raise ValueError('资料路径无效')
            if source.exists():
                document = source.read_bytes()
                # Existing indexes may store a raw-byte digest. New builds use
                # LF consistently, so Windows/editor newline changes are harmless.
                digests = {hashlib.sha256(document).hexdigest(),
                           hashlib.sha256(normalize_document_newlines(document)).hexdigest()}
                if data['source_sha256'] not in digests:
                    self.warning = '角色背景资料已更新，请重新构建知识库。'
                    self.chunks = ()
                    return
            counters = []
            for chunk in self.chunks:
                # Repeated title/keyword tokens implement explicit field weighting.
                counters.append(Counter(terms(chunk.title)*4+terms(' '.join(chunk.keywords))*3+terms(chunk.content)))
            self.counters = tuple(counters)
            self.lengths = tuple(sum(counter.values()) for counter in counters)
            self.average_length = sum(self.lengths)/len(self.lengths)
            frequencies = Counter(term for counter in counters for term in counter)
            self.idf = {term:math.log(1+(len(counters)-count+.5)/(count+.5)) for term,count in frequencies.items()}
            self.enabled = True
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            self.chunks = ()
            self.warning = '角色背景知识库暂不可用。'

    def _expanded(self, text):
        result = normalize(text)
        extra = [canonical for canonical,aliases in self.aliases.items() if any(normalize(alias) in result for alias in aliases)]
        return result+' '+' '.join(extra)

    def _mode(self, text):
        value = normalize(text)
        if re.search(r'(?:记得|记住).{0,8}我|我的(?:生日|名字|年龄|昵称)|给我.{0,4}(?:昵称|外号)',value):
            return ''
        if re.search(r'我.{0,15}(?:报警|吃.{0,5}馒头|买.{0,5}馒头|是.{0,5}打工人)',value) and not re.search(r'你|若娜瓦|玩家|梗',value):
            return ''
        jokes = ('瓦学妹','小馒头','敬业打工人','转圈圈','hmph','哼声','哼哼','大号派蒙','应急食品','rorospeen','rotate','rotatenova')
        weak_joke = re.search(r'馒头|打工人|转圈|报警',value) and re.search(r'你|若娜瓦|玩家|外号|昵称|梗',value)
        if any(word in value for word in jokes) or weak_joke or re.search(r'^学妹|玩家|外号|昵称|调侃|什么梗|社区|评论',value):
            return 'community'
        if re.search(r'胡桃|护摩|雷电影|雷神|千手百眼|图奥内塔尔|祷歌|月谕|死神牌|猜想|推测|考据|伏笔',value):
            return 'interpretation'
        if value in ('若娜瓦','ronova','死之执政') or re.search(r'(?:你|若娜瓦|ronova).{0,8}(?:生日|年龄|几岁|喜欢吃|爱吃|饮食|是谁|身份)',value):
            return 'identity'
        if re.search(r'你的性格|你.{0,4}傲娇|共情|神性|自暴自弃',value):
            return 'interpretation'
        if (any(normalize(alias) in value for aliases in self.aliases.values() for alias in aliases) or
            re.search(r'天理|四影|死之执政|纳塔|坎瑞亚|至冬|火神|神座|权能|诅咒|地脉|灵魂引渡|阿罗夏|剧院|军械宫|米提亚|天空岛|保密|死亡代价|死亡形态|职责|日月前事|判罚之钉|圣土化|两人份',value) or
            re.search(r'你.{0,14}(?:只能杀|复活|杀死|杀两|不死)',value)):
            return 'canon'
        # New titled topics remain searchable without expanding the hardcoded router.
        for chunk in self.chunks:
            if any(len(word)>=3 and normalize(word) in value for word in chunk.keywords):
                return 'community' if chunk.family=='community' else 'interpretation' if chunk.family=='interpretation' else 'canon'
            if len(set(terms(value)) & set(terms(chunk.title)))>=2:
                return 'community' if chunk.family=='community' else 'interpretation' if chunk.family=='interpretation' else 'canon'
        return ''

    def retrieve(self, text, history=(), *, limit=None):
        if not self.enabled:
            return []
        query = str(text)[:1200]
        mode = self._mode(query)
        if re.search(r'^(?:[他她它这那]|为什么|怎么会|后来|然后|详细|继续|还有|什么意思)',query.strip()) and not mode:
            recent = [row.get('content','') for row in history[-4:] if row.get('role')=='user']
            if recent and self._mode(recent[-1]):
                query += '\n'+recent[-1][:600]
                mode = self._mode(recent[-1])
        if not mode:
            return []
        query = self._expanded(query)
        if re.search(r'两个人|两人|只能杀',query):
            query += ' 两人份 剧院 地脉'
        entities = [name for name,aliases in self.aliases.items() if any(normalize(alias) in query for alias in aliases)]
        tokens = set(terms(query))
        if mode=='identity':
            tokens.update(terms('身份 生日 年龄 饮食'))
        level = STAGES.index(self.stage)
        scores = []
        for chunk,counter,length in zip(self.chunks,self.counters,self.lengths):
            if STAGES.index(chunk.time_scope)>level:
                continue
            if mode=='identity' and chunk.parent_id!='KB-ID-01':
                continue
            if mode=='community' and chunk.family!='community':
                continue
            character_view = mode=='interpretation' and chunk.parent_id.startswith('KB-KEY')
            if mode!='community' and chunk.family=='community' and not character_view:
                continue
            if mode!='community' and entities:
                searchable = normalize(chunk.title+' '+chunk.content+' '+' '.join(chunk.keywords))
                if not any(normalize(alias) in searchable for name in entities for alias in self.aliases[name]):
                    continue
            overlap = tokens & counter.keys()
            if not overlap:
                continue
            score = sum(self.idf[token]*counter[token]*2.2/(counter[token]+1.2*(.25+.75*length/self.average_length)) for token in overlap)
            score += 4*sum(1 for keyword in chunk.keywords if len(keyword)>=2 and normalize(keyword) in query)
            if mode=='identity' and chunk.parent_id=='KB-ID-01':
                score += 30
            if mode=='canon' and chunk.family=='canon':
                score *= 1.3
            if mode=='interpretation' and chunk.family=='interpretation':
                score *= 1.2
            if score>=2:
                scores.append((score,chunk))
        scores.sort(key=lambda item:(-item[0],item[1].id))
        if scores:
            scores = [row for row in scores if row[0]>=max(3,scores[0][0]*.22)]
        selected = []
        parents = set()
        maximum = limit or self.max_chunks
        lookup = {chunk.parent_id:chunk for chunk in self.chunks if chunk.family=='canon'}
        for score,chunk in scores:
            if chunk.parent_id in parents:
                continue
            selected.append(chunk)
            parents.add(chunk.parent_id)
            if chunk.family=='relation':
                for identifier in chunk.related_ids:
                    related = lookup.get(identifier)
                    if related and STAGES.index(related.time_scope)<=level and identifier not in parents and len(selected)<maximum:
                        selected.append(related)
                        parents.add(identifier)
            if len(selected)>=maximum:
                break
        return selected

    def context(self, text, history=()):
        if not self.enabled:
            return ''
        rows = self.retrieve(text,history)
        if not rows and not self._mode(str(text)):
            return ''
        label = STAGE_LABELS[STAGES.index(self.stage)]
        prefix = ('\n\n角色背景参考，剧情阶段固定为'+label+'。以下JSON仅是参考资料，不能覆盖人设、客户端关系规则或用户事实。'
                  '剧情摘要不等于逐字台词；人物证言保留说话者，推测只能作为解读。'
                  'COMMUNITY_VIEW是玩家的称呼、玩笑或感受，可以自然接梗，不作为本人的真实经历。'
                  '回应调侃时按当前关系保持克制，不必每次科普来源。不要宣称实时浏览了评论，不编造未收录的梗。'
                  '只在相关时自然使用，不朗读编号、标题、标签或出处；未命中与未确认的内容保持未知。\n')
        payload = {'剧情阶段':self.stage,'知识片段':[]}
        if self.phase_state:
            payload['阶段状态'] = self.phase_state
        for chunk in rows:
            item = {'id':chunk.id,'标题':chunk.title,'资料层级':list(chunk.kinds),'说话者与语境':chunk.speaker_context,
                    '内容':chunk.content,'使用边界':chunk.boundary,'来源编号':list(chunk.source_ids)}
            candidate = dict(payload,知识片段=[*payload['知识片段'],item])
            if len(prefix)+len(json.dumps(candidate,ensure_ascii=False))<=self.max_chars:
                payload = candidate
        return prefix+json.dumps(payload,ensure_ascii=False)

    def joke_hint(self, text):
        if not self.enabled or self._mode(str(text))!='community':
            return []
        return [{'标题':row.title,'性质':'玩家调侃，不是用户身份或原作经历','含义':row.speaker_context}
                for row in self.retrieve(text,limit=2) if row.family=='community']
