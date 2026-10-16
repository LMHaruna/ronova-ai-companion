"""Prepare, enroll once, audition twice, then configure Ronova's Qwen voice.

Default: local preparation only. --create sends one prepared recording to the
existing Qwen provider. --audition generates two public fixed readouts.
--activate requires enrollment OK and both complete, identical-text readouts.
No microphone, chat history or private role prompt is uploaded by this script.
"""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import argparse
import html
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qwen_realtime import MODEL, RealtimeConfig, environment
from qwen_voice_clone import (EnrollmentError, VoiceEnrollment, activate,
                              inspect_audio, prepare_reference, valid_voice_id)


def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def review(directory, report, reference):
    from voice_audition_tools import make_review
    make_review(directory, report)
    page_path = directory / 'review.html'
    page = page_path.read_text(encoding='utf-8')
    page = page.replace('语音对照试听', '若娜瓦 · Qwen 3.1 复刻试听')
    page = page.replace(html.escape('Qwen 3.1 / ' + report['backends'][0]['voice']),
                        'Qwen 3.1 / 若娜瓦复刻声线')
    intro = ('<section><h2>若娜瓦原声参考 · 15.30 秒</h2>'
             '<p>来自当前 A 参考的原录音，另存为扩展样本。音色相似度请以试听判断。</p>'
             f'<audio controls src="{html.escape(reference.as_uri(), quote=True)}"></audio></section>')
    old = ROOT / 'voice-audition/realtime-comparison-20261005-125643'
    for index, text in enumerate(report['texts']):
        intro += f'<section><h2>同文旧样本 · {index+1}</h2><p>{html.escape(text)}</p>'
        for backend, label in (('gpt', 'GPT-SoVITS V2ProPlus / A'), ('qwen', 'Qwen 系统音色 longanqian')):
            audio = old / f'{backend}-{index+1}-source.wav'
            if audio.is_file():
                intro += f'<p>{label}</p><audio controls src="{html.escape(audio.as_uri(),quote=True)}"></audio>'
        intro += '</section>'
    page += intro
    page_path.write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--audition', action='store_true')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    manifest_path = ROOT / 'config/qwen-voice-enrollment.json'
    reference = ROOT / 'voice-reference/prepared/qwen-ronova.wav'
    source = ROOT / 'voice-reference/录音 (2).wav'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        if manifest.get('target_model') != MODEL:
            raise EnrollmentError('已保存的音色模型不匹配')
        info = inspect_audio(reference)
        if info['sha256'] != manifest['reference']['sha256']:
            raise EnrollmentError('样本哈希已变化；停止复用音色')
    else:
        info = prepare_reference(source, reference)
        manifest = {'created_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    'target_model': MODEL, 'enrollment_model': 'voice-enrollment',
                    'prefix': 'ronova', 'source': str(source.relative_to(ROOT)),
                    'reference_path': str(reference.relative_to(ROOT)), 'reference': info,
                    'state': 'prepared', 'create_attempts': 0, 'audition_requests': 0,
                    'reference_upload_performed': False, 'normal_turn_reference_upload': False,
                    'human_timbre_accepted': False}
        save(manifest_path, manifest)
    print('REFERENCE ' + json.dumps({k:info[k] for k in ('seconds','sample_rate','channels','bytes')}, ensure_ascii=False),flush=True)
    if args.create:
        client = VoiceEnrollment(environment('DASHSCOPE_API_KEY'))
        try:
            if not manifest.get('voice_id'):
                if manifest['create_attempts']:
                    raise EnrollmentError('此前已提交创建；请查询确认结果后继续，不能自动重复创建')
                url = client.upload(reference)
                manifest['reference_upload_performed'] = True
                manifest['create_attempts'] = 1
                manifest['state'] = 'create_requested'
                save(manifest_path, manifest)
                print('REFERENCE_UPLOADED private temporary storage; expires in 48h',flush=True)
                voice = client.create(url)
                manifest.update(voice_id=voice, state='created')
                save(manifest_path, manifest)
                print('VOICE_CREATED ' + voice,flush=True)
            else:
                valid_voice_id(manifest['voice_id'])
                print('VOICE_REUSED ' + manifest['voice_id'],flush=True)
            for attempt in range(10):
                status = client.query(manifest['voice_id'])
                manifest.update(status=status, state='ready' if status=='OK' else status.lower())
                save(manifest_path, manifest)
                print('VOICE_STATUS ' + status,flush=True)
                if status == 'OK': break
                if status == 'UNDEPLOYED':
                    raise EnrollmentError('音色审核未通过；保留当前系统音色')
                if attempt < 9: time.sleep(3)
            if status != 'OK':
                raise EnrollmentError('音色仍在处理中；ID 已保存，可稍后复用查询')
        finally:
            client.close()
    if args.audition:
        if manifest.get('status') != 'OK':
            raise EnrollmentError('请先完成创建并确认音色审核通过')
        if manifest.get('audition_report'):
            print('AUDITION_REUSED ' + manifest['audition_report'],flush=True)
        else:
            if manifest.get('audition_requests',0):
                raise EnrollmentError('此前试听已经提交；不能自动重复发送')
            from voice_audition_tools import TEXTS, READ_INSTRUCTIONS, realtime_runs
            current = RealtimeConfig.load()
            # Explicit enrollment voice wins over any system-voice environment override.
            config = RealtimeConfig(current.url, manifest['voice_id'], current.key_environment, MODEL)
            config.validate()
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
            directory = ROOT / 'voice-audition' / ('qwen-ronova-' + stamp)
            directory.mkdir()
            manifest.update(audition_requests=2, state='audition_requested')
            save(manifest_path, manifest)
            backend = realtime_runs(directory, config=config)
            report = {'date': datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
                      'texts':list(TEXTS), 'public_instructions':READ_INSTRUCTIONS,
                      'scope':'Two public fixed readouts with a cloned voice, real waveOut; no microphone or role prompt',
                      'reference_manifest':str(manifest_path), 'backends':[backend],
                      'limits':'Software frame completion and transcript checks; human voice similarity pending. First sound is an RMS estimate, without acoustic loopback.',
                      'review':str(directory/'review.html')}
            save(directory/'report.json', report)
            review(directory,report,reference)
            passed = (backend.get('live_requests')==2 and len(backend['runs'])==2
                      and all(run.get('same_words') and run.get('playback_drained')
                              and run.get('submitted_frames',0)>0
                              and run['submitted_frames']==run.get('completed_frames')
                              for run in backend['runs']))
            manifest.update(audition_report=str((directory/'report.json').relative_to(ROOT)),
                            audition_passed=passed, audition_completed=sum('source_wav' in r for r in backend['runs']),
                            review=str((directory/'review.html').relative_to(ROOT)), state='auditioned')
            save(manifest_path,manifest)
            print('AUDITION ' + str(directory/'review.html') + ' passed=' + str(passed),flush=True)
    if args.activate:
        if not manifest.get('audition_passed'):
            raise EnrollmentError('两次完整试听未通过，保留当前音色')
        if environment('QWEN_REALTIME_VOICE') and environment('QWEN_REALTIME_VOICE') != manifest['voice_id']:
            raise EnrollmentError('QWEN_REALTIME_VOICE 正在覆盖音色，请先清除此环境变量')
        activate(ROOT/'config/realtime-voice.json', manifest['voice_id'], manifest.get('status'))
        manifest.update(activated=True, state='active')
        save(manifest_path,manifest)
        print('VOICE_ACTIVE Ronova / ' + MODEL,flush=True)


if __name__ == '__main__':
    try: main()
    except EnrollmentError as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(1)
