"""Local ceremony script; no model, voice service or final portrait dependency."""
DURATION=16.0
LINES=('我代表的可是死亡。','可这一次……我愿意把手交给你。','帮我保密。')
BEATS=((0.0,'邀请','你向她伸出掌心。'),
       (4.0,'回应','若娜瓦主动向前伸手。'),
       (7.2,'相接','她的手轻轻落入你的掌心。'),
       (10.4,'约定','掌心停稳，她抬眼直视你。'))
LINE_TIMES=(2.0,5.2,11.2)


def validate_ceremony():
    if len(LINES)!=3 or len(BEATS)!=4 or not all(isinstance(line,str) and line.strip() for line in LINES):
        raise RuntimeError('仪式字幕未就绪')
    if not (0==BEATS[0][0]<BEATS[1][0]<BEATS[2][0]<BEATS[3][0]<DURATION):
        raise RuntimeError('仪式分镜未就绪')


def beat_at(elapsed):
    return max(index for index,beat in enumerate(BEATS) if elapsed>=beat[0])
